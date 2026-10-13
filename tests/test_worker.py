"""Hosted worker on recorded feeds: what it publishes, when it re-simulates, and that one game's failure doesn't
stop the others. In-game states are synthesized from the week-5 snapshot (BUF @ LA, recorded before kickoff)."""

import copy
import gzip
import json
from pathlib import Path

import pytest

from nflsim.hosted.publish import LocalDir, max_age
from nflsim.hosted.worker import PAGE, Worker
from nflsim.live import espn, feed

SNAPSHOT = Path(__file__).parent / "fixtures" / "week5_snapshot.jsonl.gz"
PRE = "401872994"  # BUF @ LA, before kickoff in the snapshot
POST = "401872980"  # TB @ DAL, final
SCOREBOARD = f"{espn.BASE}/scoreboard"

pytestmark = pytest.mark.slow  # runs the simulator


def _snapshot():
    with gzip.open(SNAPSHOT, "rt", encoding="utf-8") as f:
        return {r["url"]: r for r in map(json.loads, f)}


def _in_game(sb, event, *, period, clock, down, togo, spot, text, home_score=0, away_score=0):
    """The scoreboard with `event` in progress: LA ball at `spot` (LA's own side)."""
    sb = copy.deepcopy(sb)
    ev = next(e for e in sb["events"] if e["id"] == event)
    ev["status"].update(period=period, clock=float(clock), displayClock=f"{clock // 60}:{clock % 60:02d}")
    ev["status"]["type"].update(state="in", shortDetail=f"{clock // 60}:{clock % 60:02d} - {period}")
    c = ev["competitions"][0]
    t = {x["homeAway"]: x for x in c["competitors"]}
    t["home"]["score"], t["away"]["score"] = str(home_score), str(away_score)
    c["situation"] = {"possession": t["home"]["team"]["id"], "down": down, "distance": togo,
                      "possessionText": f"LA {spot}", "lastPlay": {"text": text}}
    return sb


def _recording(path, *scoreboards, step=5.0, extra=()):
    """A recording: the snapshot at t0, then each scoreboard `step` seconds apart, plus `extra` rows
    ({"t": seconds after t0, "url", "body"})."""
    snap = _snapshot()
    t0 = snap[SCOREBOARD]["t"]
    rows = list(snap.values()) + [{"t": t0 + step * (i + 1), "url": SCOREBOARD, "body": sb}
                                  for i, sb in enumerate(scoreboards)] + [{**r, "t": t0 + r["t"]} for r in extra]
    with gzip.open(path, "wt", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return t0


class Spy(LocalDir):
    def __init__(self, root):
        super().__init__(root)
        self.puts = []

    def put(self, path, data, content_type, max_age):
        self.puts.append(path)
        super().put(path, data, content_type, max_age)


@pytest.fixture(scope="module")
def model():
    from nflsim.data import load
    from nflsim.live.app import LiveModel

    return LiveModel(load())


@pytest.fixture
def replay():
    def use(path):
        rp = feed.Replay(path)
        espn.use(rp)
        return rp
    yield use
    espn.use(feed.Live())


def _worker(tmp_path, model, only):
    from nflsim.data import load

    return Worker(load(), Spy(tmp_path / "site"), only=set(only), model=model, log=lambda *a: None)


def test_first_tick_publishes_page_games_state_and_status(tmp_path, model, replay):
    replay(SNAPSHOT)
    w = _worker(tmp_path, model, [PRE, POST])
    w.start()
    assert w.tick() == 2
    site = tmp_path / "site"
    assert (site / "index.html").read_bytes() == PAGE.read_bytes()
    assert {g["event"] for g in json.loads((site / "games.json").read_text())} == {PRE, POST}
    pre, post = (json.loads((site / "state" / f"{e}.json").read_text()) for e in (PRE, POST))
    assert pre["status"] == "pre" and pre["now"] == "Kickoff · LA receives" and 0 < pre["homeWin"] < 1
    assert post["now"] == "Final" and post["homeWin"] in (0.0, 1.0)
    status = json.loads((site / "status.json").read_text())
    assert status["errors"] == {} and status["live"] == 0


def test_unchanged_games_are_not_recomputed_or_republished(tmp_path, model, replay):
    rp = replay(SNAPSHOT)
    w = _worker(tmp_path, model, [PRE, POST])
    w.start()
    w.tick()
    before = len(w.publisher.puts)
    rp.advance(5)
    assert w.tick() == 0
    assert w.publisher.puts[before:] == ["status.json"]  # the heartbeat moves; nothing else changed


def _with_passing(summary, team, name, espn_id, comp_att, yds):
    """The summary with one passer's line in the box score."""
    summary = copy.deepcopy(summary)
    summary.setdefault("boxscore", {})["players"] = [{"team": {"abbreviation": team}, "statistics": [
        {"name": "passing", "labels": ["C/ATT", "YDS"],
         "athletes": [{"athlete": {"displayName": name, "id": espn_id}, "stats": [comp_att, str(yds)]}]}]}]
    return summary


def test_a_new_play_is_simulated_once_and_again_when_the_box_score_catches_up(tmp_path, model, replay):
    snap = _snapshot()
    sb, url = snap[SCOREBOARD]["body"], f"{espn.BASE}/summary?event={PRE}"
    kick = _in_game(sb, PRE, period=1, clock=900, down=1, togo=10, spot=25, text="Kickoff, touchback.")
    play = _in_game(sb, PRE, period=1, clock=861, down=1, togo=10, spot=37, text="Stafford pass to Nacua for 12 yards.")
    late = _with_passing(snap[url]["body"], "LAR", "Matthew Stafford", "12483", "1/1", 12)
    t0 = _recording(tmp_path / "r.jsonl.gz", kick, play, extra=[{"t": 22, "url": url, "body": late}])
    rp = replay(tmp_path / "r.jsonl.gz")
    w = _worker(tmp_path, model, [PRE])
    w.start()
    counts = []
    # kickoff state at +5, the pass at +10; its yards reach the box score at +22, read by the 10 s poll at +30
    for t in (0, 5, 10, 15, 20, 25, 30, 35, 40):
        rp.seek(t0 + t)
        counts.append(w.tick())
    assert counts == [1, 1, 1, 0, 0, 0, 1, 0, 0]
    state = json.loads((tmp_path / "site" / "state" / f"{PRE}.json").read_text())
    assert state["status"] == "in" and state["now"] == "LA ball · 1st & 10 at LA 37"
    assert next(q for q in state["qbs"] if q["team"] == "LA")["yds"] == 12
    assert json.loads((tmp_path / "site" / "status.json").read_text())["live"] == 1


def test_overtime_shows_the_situation_without_chances(tmp_path, model, replay):
    sb = _snapshot()[SCOREBOARD]["body"]
    ot = _in_game(sb, PRE, period=5, clock=480, down=2, togo=5, spot=40, text="Run for 3 yards.",
                  home_score=34, away_score=34)
    t0 = _recording(tmp_path / "r.jsonl.gz", ot)
    rp = replay(tmp_path / "r.jsonl.gz")
    w = _worker(tmp_path, model, [PRE])
    w.start()
    rp.seek(t0 + 5)
    assert w.tick() == 1
    state = json.loads((tmp_path / "site" / "state" / f"{PRE}.json").read_text())
    assert state["overtime"] and state["clock"] == "OT 8:00" and state["now"] == "LA ball · 2nd & 5 at LA 40"
    assert state["homeWin"] is None and state["next"] == [] and state["drive"] is None
    assert all(q["median"] is None and q["lines"] == [] for q in state["qbs"])


def test_pregame_states_refresh_hourly(tmp_path, model, replay):
    rp = replay(SNAPSHOT)
    w = _worker(tmp_path, model, [PRE])
    w.start()
    w.tick()
    rp.advance(3599)
    assert w.tick() == 0
    rp.advance(1)
    assert w.tick() == 1


def test_one_failing_game_does_not_stop_the_others(tmp_path, model, replay, monkeypatch):
    replay(SNAPSHOT)
    w = _worker(tmp_path, model, [PRE, POST])
    real = model.compute_state
    monkeypatch.setattr(model, "compute_state",
                        lambda s, ov=None: (_ for _ in ()).throw(RuntimeError("boom")) if s["event"] == POST else real(s, ov))
    w.start()
    w.tick()
    site = tmp_path / "site"
    assert (site / "state" / f"{PRE}.json").exists() and not (site / "state" / f"{POST}.json").exists()
    assert json.loads((site / "status.json").read_text())["errors"] == {POST: "RuntimeError: boom"}
    monkeypatch.setattr(model, "compute_state", real)
    w.tick()  # retried on the next tick, error cleared
    assert (site / "state" / f"{POST}.json").exists()
    assert json.loads((site / "status.json").read_text())["errors"] == {}


@pytest.mark.parametrize("path,seconds", [("state/1.json", 3), ("games.json", 3), ("status.json", 0), ("index.html", 300)])
def test_cache_times(path, seconds):
    assert max_age(path) == seconds


def test_page_has_no_corrections_and_uses_relative_urls():
    html = PAGE.read_text()
    for gone in ("<form", "/api/", "overrides", "live:fix"):
        assert gone not in html
    for used in ('getJSON("games.json")', "getJSON(`state/", 'getJSON("status.json")'):
        assert used in html
