"""The live feed plumbing: replay of a recorded ESPN feed, state signatures, and the simulation split from the fetch.

tests/fixtures/week5_snapshot.jsonl.gz is one poll of 2026 week 5 (15 games: 14 final, BUF @ LA before kickoff),
recorded with `nflsim feed record <file> --once`.
"""

import gzip
import json
import math
from pathlib import Path

import pytest

from nflsim.live import app, espn, feed

SNAPSHOT = Path(__file__).parent / "fixtures" / "week5_snapshot.jsonl.gz"
SCOREBOARD = f"{espn.BASE}/scoreboard"


@pytest.fixture
def replay():
    rp = feed.Replay(SNAPSHOT)
    espn.use(rp)
    yield rp
    espn.use(feed.Live())


def _write(path, rows):
    with gzip.open(path, "wt", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def test_replay_returns_latest_body_at_or_before_the_clock(tmp_path):
    rec = tmp_path / "r.jsonl.gz"
    _write(rec, [{"t": 100, "url": "u", "body": {"n": 1}}, {"t": 110, "url": "u", "body": {"n": 2}},
                 {"t": 105, "url": "v", "body": {"n": 9}}])
    rp = feed.Replay(rec)
    assert rp.now() == 100 and rp.get("u") == {"n": 1}
    assert rp.get("v") == {"n": 9}  # before its first response: the earliest one
    rp.seek(109.9)
    assert rp.get("u") == {"n": 1}
    rp.advance(0.1)
    assert rp.get("u") == {"n": 2} and rp.finished
    with pytest.raises(KeyError):
        rp.get("w")


def test_recorder_skips_unchanged_responses(tmp_path, monkeypatch):
    bodies = iter([{"a": 1}, {"a": 1}, {"a": 2}])
    monkeypatch.setattr(feed, "fetch", lambda url: next(bodies))
    rec = feed.Recorder(tmp_path / "r.jsonl.gz")
    assert [rec.get("u") for _ in range(3)] == [{"a": 1}, {"a": 1}, {"a": 2}]
    rp = feed.Replay(tmp_path / "r.jsonl.gz")
    assert [b for b in rp.by_url["u"][1]] == [{"a": 1}, {"a": 2}]


def test_game_state_reuses_one_scoreboard(replay):
    sb = espn.scoreboard()
    games = espn.games(sb)
    assert len(games) == 15 and {g["state"] for g in games} == {"pre", "post"}
    for g in games:
        s = espn.game_state(g["event"], sb)
        assert json.dumps(s, sort_keys=True) == json.dumps(espn.game_state(g["event"]), sort_keys=True)  # NaN-safe
        assert s["fetched_at"] == espn.game_state(g["event"], sb, espn.summary(g["event"]))["fetched_at"]
        assert "_ids" not in s and s["passers"] is not None


def test_signature_is_comparable_with_nan_spots(replay):
    pre = next(g for g in espn.games() if g["state"] == "pre")
    s = espn.situation(pre["event"])
    assert s["kick"] and math.isnan(s["yl"])  # before kickoff: no spot
    assert espn.signature(s) == espn.signature(espn.situation(pre["event"]))
    assert espn.signature(s) == espn.signature(espn.game_state(pre["event"]))
    moved = {**s, "down": 2}
    assert espn.signature(moved) != espn.signature(s)


def _game(sb, event):
    ev = next(e for e in sb["events"] if e["id"] == event)
    return ev, {x["homeAway"]: x for x in ev["competitions"][0]["competitors"]}


def test_overtime_is_flagged_from_a_tied_end_of_regulation(replay):
    import copy

    event = next(g["event"] for g in espn.games() if g["state"] == "pre")  # BUF @ LA
    sb = copy.deepcopy(espn.scoreboard())
    ev, t = _game(sb, event)
    assert not espn.situation(event, sb)["overtime"]
    ev["status"].update(period=4, clock=0.0, displayClock="0:00")
    ev["status"]["type"].update(state="in")
    t["home"]["score"] = t["away"]["score"] = "34"
    ev["competitions"][0]["situation"] = {"down": 1, "distance": 10,
                                          "lastPlay": {"text": "END QUARTER 4", "type": {"text": "End of Regulation"}}}
    s = espn.situation(event, sb)
    assert s["overtime"] and s["kick"] and s["pos"] is None  # receiver not posted yet
    ev["status"].update(period=5, clock=600.0, displayClock="10:00")
    ev["competitions"][0]["situation"]["possession"] = t["away"]["team"]["id"]  # the coin toss: BUF receives
    s = espn.situation(event, sb)
    assert s["overtime"] and s["kick"] and s["pos"] == 1
    t["home"]["score"] = "37"
    ev["status"].update(period=4)
    assert not espn.situation(event, sb)["overtime"]  # regulation ended with a winner


def test_box_signature_follows_the_passing_lines():
    s = {"passers": {"LA": [("Matthew Stafford", "12483", 10, 88.0)], "BUF": []}}
    assert espn.box(s) == (("LA", "12483", 10, 88.0),)
    assert espn.box({"passers": {"LA": [("Matthew Stafford", "12483", 10, 100.0)]}}) != espn.box(s)


@pytest.mark.slow
def test_compute_state_matches_compute_and_is_deterministic(replay):
    from nflsim.data import load
    from nflsim.live import app

    m = app.LiveModel(load())
    event = next(g["event"] for g in espn.games() if g["state"] == "pre")
    a = m.compute(event, {})
    b = m.compute_state(espn.game_state(event))
    assert json.dumps(a, default=app._f, sort_keys=True) == json.dumps(b, default=app._f, sort_keys=True)
    assert 0 < a["homeWin"] < 1 and 1 <= len(a["next"]) <= app.N_BUCKETS and len(a["qbs"]) == 2
    # before kickoff the drive is the receiving team's first one
    from nflsim.sim.engine import DRIVE_RESULTS

    dv = a["drive"]
    assert dv["kickoff"] and dv["team"] in (a["home"], a["away"]) and len(dv["outcomes"]) == 5
    assert {o["result"] for o in dv["outcomes"]} <= set(DRIVE_RESULTS)
    assert sum(o["prob"] for o in dv["outcomes"]) <= 1
    assert all((o["score"] is not None) == (o["result"] in ("touchdown", "field goal")) for o in dv["outcomes"])


@pytest.mark.slow
def test_compute_reuses_the_result_until_the_play_or_corrections_change(replay):
    from nflsim.data import load
    from nflsim.live import app

    m = app.LiveModel(load())
    event = next(g["event"] for g in espn.games() if g["state"] == "pre")
    a = m.compute(event, {})
    assert m.compute(event, {}) is a
    assert a["sig"] == app.sig_id(espn.game_state(event))
    b = m.compute(event, {"down": "3"})
    assert b is not a and b["sig"] == a["sig"]


@pytest.mark.slow
def test_quarters_add_up_to_the_final_but_overtime(replay):
    from nflsim.data import load
    from nflsim.live import app

    m = app.LiveModel(load())
    pre = m.compute(next(g["event"] for g in espn.games() if g["state"] == "pre"), {})
    qs = pre["quarters"]
    assert [q["q"] for q in qs] == [1, 2, 3, 4] and all(q["state"] == "next" for q in qs)
    for side in ("home", "away"):  # the final also counts overtime points
        assert 0 <= pre["final"][side] - sum(q[side] for q in qs) < 0.5
    assert all(math.isclose(q["homeWin"] + q["awayWin"] + q["tie"], 1) for q in qs)
    post = m.compute(next(g["event"] for g in espn.games() if g["state"] == "post"), {})
    assert all(q["state"] == "done" for q in post["quarters"])
    assert sum(q["home"] for q in post["quarters"]) == post["score"]["home"] == post["final"]["home"]



@pytest.mark.slow
def test_theta_groups_the_same_games_by_the_next_snap(replay):
    from nflsim.data import load
    from nflsim.live import app

    m = app.LiveModel(load())
    a = m.compute(next(g["event"] for g in espn.games() if g["state"] == "pre"), {})
    for q in a["qbs"]:  # from kickoff: about as many dropbacks as real starters get (34 a game in 2025)
        assert 28 < q["theta"]["dropbacks"]["mean"] < 42 and q["theta"]["dropbacks"]["none"] < 0.01
    # before kickoff the next snap is the receiving team's
    mine = [q for q in a["qbs"] if q["theta"]["next"]]
    assert len(mine) == 1 and mine[0]["team"] == a["drive"]["team"]
    nx = mine[0]["theta"]["next"]
    assert math.isclose(sum(x["prob"] for x in nx), 1) and {x["outcome"] for x in nx} >= {"Completion", "Run"}
    for j, line in enumerate(mine[0]["lines"]):  # the chance now is the outcomes' chances weighted by their shares
        assert abs(sum(x["prob"] * x["p"][j] for x in nx) - line["p"]) < 0.02
    after = {x["outcome"]: x["p"][app.LINES.index(250)] for x in nx}
    assert after["Completion"] > after["Run"]

def test_a_backup_throwing_the_latest_pass_marks_the_starter_out():
    sm = {"drives": {"previous": [
        {"team": {"abbreviation": "CAR"}, "plays": [{"text": "(Shotgun) B.Young pass short left to X for 5 yards"}]},
        {"team": {"abbreviation": "TB"}, "plays": [{"text": "(Shotgun) B.Mayfield sacked at TB 30 for -6 yards"}]},
        {"team": {"abbreviation": "CAR"}, "plays": [{"text": "K.Pickett pass short right to T.Tremble for 8 yards"},
                                                     {"text": "C.Hubbard right tackle for 3 yards"}]}]}}
    latest = espn.last_passers(sm)
    assert latest == {"CAR": "K.Pickett", "TB": "B.Mayfield"}
    car = [("Bryce Young", "1", 27, 163.0), ("Kenny Pickett", "2", 1, 8.0)]
    assert app.LiveModel._starter_yards(car, "Bryce Young", latest["CAR"]) == (163.0, True, "Bryce Young")
    assert app.LiveModel._starter_yards(car, "Bryce Young", "B.Young") == (163.0, False, "Bryce Young")
    assert app.LiveModel._starter_yards(car, "Bryce Young", None) == (163.0, False, "Bryce Young")  # 1 attempt: not yet


def test_local_pages_point_at_this_server():
    for page in (app.HOME, app.EDGES, app.WATCH):
        html = app._local(page).decode()
        assert '"/watch"' in html and '"watch.html"' not in html and '"summary.json"' not in html


def test_the_team_scored_on_receives_even_after_a_timeout_hides_the_score():
    s = {"kick": True, "period": 2, "display_clock": "5:37", "home": "PHI", "away": "DAL"}
    td = {"scoringPlays": [{"team": {"abbreviation": "DAL"}, "type": {"text": "Passing Touchdown"},
                            "period": {"number": 2}, "clock": {"displayValue": "5:37"}}]}
    assert espn.kickoff_receiver(td, s) == "PHI"
    safety = {"scoringPlays": [{"team": {"abbreviation": "PHI"}, "type": {"text": "Safety"},
                                "period": {"number": 2}, "clock": {"displayValue": "5:37"}}]}
    assert espn.kickoff_receiver(safety, s) == "PHI"  # the free kick goes to the team that scored
    assert espn.kickoff_receiver(td, {**s, "display_clock": "4:10"}) is None  # plays since: not this score
