"""The live feed plumbing: replay of a recorded ESPN feed, state signatures, and the simulation split from the fetch.

tests/fixtures/week5_snapshot.jsonl.gz is one poll of 2026 week 5 (15 games: 14 final, BUF @ LA before kickoff),
recorded with `nflsim feed record <file> --once`.
"""

import gzip
import json
import math
from pathlib import Path

import pytest

from nflsim.live import espn, feed

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
