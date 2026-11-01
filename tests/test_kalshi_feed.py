"""Game state from Kalshi's live data (live/kalshi_feed.py)."""

import math

from nflsim.live import kalshi_feed as KF

HOME, AWAY = "home-id", "away-id"
M = {"id": "m", "home_id": HOME, "away_id": AWAY}


def _play(seq, period, clock, kind, pos, text, home=0, away=0, down=None, yfd=None):
    return {"sequence": seq, "period": period, "clock": clock, "play_type": kind, "possession": pos, "play_id": f"p{seq}",
            "description": text, "home_points": home, "away_points": away, "down": down, "yfd": yfd,
            "wall_clock": 1793300000 + seq}


def _stats(plays):
    periods = sorted({p["period"] for p in plays})
    return {"pbp": {"periods": [{"period_number": n, "events": [{"plays": [p for p in plays if p["period"] == n]}]}
                                for n in periods]}}


def _base():
    return {"event": "E", "home": "GB", "away": "CAR", "status": "in", "pos": 0, "kick": False, "yl": 50.0,
            "down": 1, "togo": 10.0, "_ids": {}}


def test_pass_yards_from_the_play_text():
    assert KF.pass_yards("Bryce Young pass complete to the middle to Tetairoa McMillan for 13 yards to the CAR 44.") == ("Bryce Young", 13, True)
    assert KF.pass_yards("Kyler Murray pass to the left to Aaron Jones Sr. for no gain to the IND 12.") == ("Kyler Murray", 0, True)
    assert KF.pass_yards("J.Love pass to the right to J.Jacobs for a loss of 3 yards to the GB 27.") == ("J.Love", -3, True)
    assert KF.pass_yards("Kyler Murray incomplete pass to the left intended for Tai Felton.") == ("Kyler Murray", 0, True)
    assert KF.pass_yards("Daniel Jones pass intended for Will Mallory INTERCEPTED by Harrison Smith and returned for 15 yards.") == ("Daniel Jones", 0, True)
    # the box score credits the passer with the yards after a lateral too
    assert KF.pass_yards("Michael Penix Jr. pass to the left to Drake London for 2 yards to the ATL 22. Lateral to "
                         "Bijan Robinson for 14 yards to the ATL 36. Tackled by Malik Mustapha.") == ("Michael Penix Jr.", 16, True)
    assert KF.pass_yards("Geno Smith 2 pt conversion pass to Mason Taylor is GOOD.") is None
    assert KF.pass_yards("Kyler Murray sacked at IND 36 for a loss of 8 yards by Adetomiwa Adebawore.") is None


def test_passing_totals_and_the_latest_passer():
    plays = [_play(1, 1, "14:00", "pass", AWAY, "B.Young pass complete to the middle to C.Hubbard for 9 yards to the GB 15."),
             _play(2, 1, "13:30", "pass", AWAY, "Bryce Young pass complete to the right to Jalen Coker for 11 yards to the GB 4."),
             _play(3, 1, "12:00", "pass", HOME, "J.Love incomplete pass to the right."),
             _play(4, 1, "11:00", "sack", AWAY, "A.Dalton sacked at GB 40 for a loss of 6 yards by D.Wyatt.")]
    passers, latest = KF.passing(plays, {HOME: 0, AWAY: 1})
    assert passers[1] == [("Bryce Young", "kalshi:young", 2, 20.0)]  # one passer: abbreviated and full names merged
    assert passers[0] == [("J.Love", "kalshi:love", 1, 0.0)]
    assert latest == {1: "A.Dalton", 0: "J.Love"}  # a sack names the passer too


def test_a_score_puts_a_kickoff_to_the_other_team_next():
    plays = [_play(1, 1, "15:00", "kickoff", AWAY, "R.Fitzgerald kicks off from CAR 35 to the GB END ZONE. Touchback at GB 35."),
             _play(2, 1, "14:55", "pass", HOME, "J.Love pass to the right to J.Jacobs for 65 yards for a TOUCHDOWN.", home=6, down=1, yfd=10),
             _play(3, 1, "14:45", "extra_point", HOME, "T.Smack extra point is GOOD.", home=7)]
    s = KF.game_state(_base(), M, {}, _stats(plays))
    assert s["kick"] and s["pos"] == 1 and math.isnan(s["yl"])  # CAR receives
    assert (s["home_score"], s["away_score"], s["margin"]) == (7, 0, 7) and s["linescores"] == [[7.0], [0.0]]
    assert s["h2_receiver"] == 1 and s["source"] == "kalshi"  # CAR kicked off first, so it receives in the 2nd half
    assert s["clock"] == 3 * 900 + 14 * 60 + 45 and s["last_play"] == "T.Smack extra point is GOOD."


def test_after_a_safety_the_scoring_team_receives():
    plays = [_play(1, 2, "8:00", "rush", AWAY, "C.Hubbard rush to the left for a loss of 2 yards. SAFETY.", home=2)]
    s = KF.game_state(_base(), M, {}, _stats(plays))
    assert s["kick"] and s["pos"] == 0


def test_after_a_kickoff_the_receiving_team_has_the_ball_at_the_spot():
    plays = [_play(1, 1, "15:00", "kickoff", AWAY, "R.Fitzgerald kicks off from CAR 35 to the GB 8. C.Owen return for 19 yards to GB 27. Tackled by J.Kuwatch.")]
    s = KF.game_state(_base(), M, {}, _stats(plays))
    assert not s["kick"] and s["pos"] == 0 and s["yl"] == 73.0 and (s["down"], s["togo"]) == (1, 10.0)


def test_between_snaps_the_live_summary_sets_the_spot_when_it_describes_the_latest_play():
    plays = [_play(1, 1, "15:00", "kickoff", AWAY, "R.Fitzgerald kicks off from CAR 35 to the GB END ZONE. Touchback at GB 35."),
             _play(2, 1, "14:55", "rush", HOME, "J.Jacobs rush to the left for 4 yards to the GB 39.", down=1, yfd=10)]
    details = {"last_play": {"play_id": "p2"}, "quarter": 1, "clock": "14:20", "home_points": 0, "away_points": 0,
               "home_timeouts_remaining": 3, "away_timeouts_remaining": 2,
               "situation": {"down": 2, "yfd": 6, "yardline": 39, "side_team_id": HOME, "possession_team_id": HOME}}
    s = KF.game_state(_base(), M, details, _stats(plays))
    assert (s["pos"], s["kick"], s["yl"], s["down"], s["togo"]) == (0, False, 61.0, 2, 6.0)
    assert s["clock"] == 3 * 900 + 14 * 60 + 20 and s["timeouts"] == [3, 2]
    # a summary describing an older play is ignored: ESPN's spot stands
    stale = dict(details, last_play={"play_id": "p1"})
    s = KF.game_state(_base(), M, stale, _stats(plays))
    assert (s["pos"], s["yl"], s["down"]) == (0, 50.0, 1) and s["clock"] == 3 * 900 + 14 * 60 + 55
