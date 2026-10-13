"""Hosted worker: the only process that talks to ESPN or runs the simulator. Viewers read the files it publishes
(docs/hosting-plan.md):

    index.html          the page (no corrections)
    games.json          this week's games (espn.games)
    state/<event>.json  LiveModel.compute_state for the game, from ESPN's feed as is
    status.json         heartbeat: when the worker last checked, per-game errors

Each tick reads the scoreboard once and re-simulates a game when its signature changes. ESPN's box score lags the
scoreboard (in the 2026-10-12 BUF @ LA recording, 23 of 24 completions' yards arrived 20-60 s after the play), so
the summary of each game in progress is also re-read every `box_every` seconds, and the game re-simulated when its
passing lines change. Games before kickoff are recomputed every `pregame_every` seconds. A file is re-published only when its bytes
change. Errors in one game are logged and reported in status.json; they don't stop the others.
"""

from __future__ import annotations

import hashlib
import json
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..data import Dataset
from ..live import espn
from ..live.app import LiveModel, _f
from .publish import max_age

PAGE = Path(__file__).with_name("index.html")


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")


@dataclass
class GameTrack:
    sig: tuple | None = None  # signature of the last computed state
    box: tuple | None = None  # its box score (espn.box)
    computed: float | None = None  # feed clock of the last computation
    polled: float | None = None  # feed clock of the last summary fetch
    error: str | None = None


@dataclass
class Worker:
    ds: Dataset
    publisher: object
    every: float = 5.0
    box_every: float = 10.0
    pregame_every: float = 3600.0
    only: set[str] | None = None  # restrict to these events (tests)
    log: object = print
    model: LiveModel = None
    games: dict[str, GameTrack] = field(default_factory=dict)
    published: dict[str, str] = field(default_factory=dict)  # path -> hash of the bytes last published
    computations: int = 0

    def __post_init__(self):
        self.model = self.model or LiveModel(self.ds)

    # --- publishing ---------------------------------------------------------------------------------------------

    def put(self, path: str, data: bytes, content_type: str = "application/json") -> bool:
        h = hashlib.sha256(data).hexdigest()
        if self.published.get(path) == h:
            return False
        self.publisher.put(path, data, content_type, max_age(path))
        self.published[path] = h
        return True

    def put_json(self, path: str, obj) -> bool:
        return self.put(path, json.dumps(obj, default=_f, separators=(",", ":"), sort_keys=True).encode())

    # --- the loop -----------------------------------------------------------------------------------------------

    def start(self) -> None:
        """Publish the page and set up every game on the scoreboard, so the first update of a game is fast."""
        self.put("index.html", PAGE.read_bytes(), "text/html; charset=utf-8")
        sb = espn.scoreboard()
        for g in self._games(sb):
            try:
                self.model.game_params(espn.situation(g["event"], sb))
            except Exception as e:  # reported again, per game, by tick()
                self.log(f"warm-up {g['away']} @ {g['home']}: {type(e).__name__}: {e}")

    def _games(self, sb: dict) -> list[dict]:
        return [g for g in espn.games(sb) if self.only is None or g["event"] in self.only]

    def _due(self, g: dict, t: GameTrack, sig: tuple, now: float) -> bool:
        """The state must be recomputed (the box score aside)."""
        if t.sig is None or sig != t.sig:
            return True
        return g["state"] == "pre" and now - t.computed >= self.pregame_every

    def _poll_box(self, g: dict, t: GameTrack, now: float) -> bool:
        return g["state"] == "in" and (t.polled is None or now - t.polled >= self.box_every)

    def tick(self) -> int:
        """One pass: scoreboard, games.json, changed games, status.json. Returns the number of games computed."""
        now = espn.source.now()
        sb = espn.scoreboard()
        games = self._games(sb)
        self.put_json("games.json", games)
        n = 0
        for g in games:
            ev = g["event"]
            t = self.games.setdefault(ev, GameTrack())
            try:
                sit = espn.situation(ev, sb)
                sig = espn.signature(sit)
                due = self._due(g, t, sig, now)
                if not due and not self._poll_box(g, t, now):
                    continue
                s = espn.add_summary(sit, espn.summary(ev))
                t.polled = now
                box = espn.box(s)
                if not due and box == t.box:
                    continue
                started = time.perf_counter()
                out = self.model.compute_state(s)
                self.computations += 1
                n += 1
                t.sig, t.box, t.computed, t.error = sig, box, now, None
                wrote = self.put_json(f"state/{ev}.json", out)
                self.log(f"{g['away']} @ {g['home']} {out['clock']}: {out['now']} "
                         f"({time.perf_counter() - started:.1f} s{'' if wrote else ', unchanged'})")
            except Exception as e:
                t.error = f"{type(e).__name__}: {e}"
                self.log(f"{g['away']} @ {g['home']}: {t.error}")
                traceback.print_exc()
        self.put_json("status.json", {
            "updated": _iso(now), "every": self.every, "live": sum(g["state"] == "in" for g in games),
            "errors": {ev: t.error for ev, t in self.games.items() if t.error}})
        return n

    def run(self, until=None) -> None:
        """Tick every `every` seconds (wall clock) until `until()` is true or Ctrl-C."""
        self.start()
        try:
            while not (until and until()):
                t0 = time.monotonic()
                try:
                    self.tick()
                except Exception as e:  # the scoreboard itself failed: keep the last files, try again
                    self.log(f"tick failed: {type(e).__name__}: {e}")
                time.sleep(max(0.0, self.every - (time.monotonic() - t0)))
        except KeyboardInterrupt:
            pass
