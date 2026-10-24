"""Hosted worker: the only process that talks to ESPN or runs the simulator. Viewers read the files it publishes
(docs/hosting-plan.md):

    index.html          every game at a glance, live games first (the home page)
    game.html           one game (`?event=<id>`; no corrections)
    edges.html          the model's passing-yard chances vs Kalshi's prices (summary.json + kalshi.json)
    summary.json        its cards (live.app.summary of each game's last computed state)
    games.json          this week's games (espn.games)
    state/<event>.json  LiveModel.compute_state for the game, from ESPN's feed as is
    status.json         heartbeat: when the worker last checked, per-game errors
    kalshi.json         Kalshi's passing-yard prices for the games (`prices`, e.g. kalshi.live_prices; each tick,
                        or every `prices_every` seconds; not published when `prices` is None, as in replays)

Each tick reads the scoreboard once and re-simulates a game when its signature changes. ESPN's box score lags the
scoreboard (in the 2026-10-12 BUF @ LA recording, 23 of 24 completions' yards arrived 20-60 s after the play), so
the summary of each game in progress is also re-read every `box_every` seconds, and the game re-simulated when its
passing lines change. Games before kickoff are recomputed every `pregame_every` seconds. A file is re-published only when its bytes
change. Errors in one game are logged and reported in status.json; they don't stop the others, and the game is
retried after `retry` seconds (a failing upstream, e.g. Open-Meteo for the week's predictions, isn't hammered).
With no game in progress or starting within `soon` seconds, the scoreboard is polled every `idle_every` seconds.

With `history` (a directory), every change is also appended to <history>/<UTC date>.jsonl.gz, one JSON record per
line: {"t", "kind": "kalshi", "prices"} (kalshi.json) or {"t", "kind": "state", "card"} (the game's summary card:
clock, score, win chance, each QB's yards and line chances, the score distributions). A file's first "kalshi" record
has every game's prices; later ones (with "partial": true) only the games whose prices changed, and "gone": the games
no longer listed. `read_history` reads them back; `prices_at` replays the "kalshi" records into whole snapshots.

Data upkeep (both off unless set, as in replays and tests): with `refresh`, the stats, schedules and play-by-play are
re-downloaded every `refresh_every` seconds while no game is on or about to start, and everything is recomputed from
them. With `preds_every`, the pregame expected yards of the week's QBs are recomputed (in a background thread, ~15 s)
that often while a game is still to start; each game keeps the numbers it had at kickoff. status.json's `data` says
when each last happened.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..data import Dataset
from ..live import espn
from ..live.app import LiveModel, _f, summary
from .publish import max_age

PAGE = Path(__file__).with_name("index.html")
GAME_PAGE = Path(__file__).with_name("game.html")
EDGES_PAGE = Path(__file__).with_name("edges.html")


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")


@dataclass
class GameTrack:
    sig: tuple | None = None  # signature of the last computed state
    box: tuple | None = None  # its box score (espn.box)
    computed: float | None = None  # feed clock of the last computation
    polled: float | None = None  # feed clock of the last summary fetch
    error: str | None = None
    failed: float | None = None  # feed clock of the last error
    out: dict | None = None  # the last computed state


@dataclass
class Worker:
    ds: Dataset
    publisher: object
    every: float = 5.0  # seconds between ticks while a game is about to start
    live_every: float = 2.0  # ... while a game is in progress
    box_every: float = 4.0
    pregame_every: float = 3600.0
    idle_every: float = 60.0
    soon: float = 900.0
    retry: float = 60.0
    only: set[str] | None = None  # restrict to these events (tests)
    prices: Callable[[list], dict] | None = None  # games -> market prices (kalshi.live_prices)
    history: Path | None = None  # where to append every published change (None: nowhere)
    prices_every: float = 0.0  # 0: every tick, as often as the scoreboard is read
    priced: float | None = None  # wall clock of the last price fetch
    _logged: tuple[str, dict] | None = None  # (history file, prices) of the last "kalshi" record
    refresh: Callable[[], None] | None = None  # re-downloads stats, schedules and play-by-play (cli: nflverse)
    refresh_every: float = 86400.0
    refreshed: float | None = None  # wall clock of the last data refresh (default: when passing.csv was written)
    preds_every: float | None = None  # seconds between recomputations of the week's pregame expectations
    preds_at: float | None = None  # wall clock the last recomputation started
    projected: float | None = None  # wall clock the last recomputation was applied
    weeks: set = field(default_factory=set)  # (season, week) of the games on the scoreboard
    _job: threading.Thread | None = None
    _job_out: dict | None = None
    log: object = print
    model: LiveModel = None
    games: dict[str, GameTrack] = field(default_factory=dict)
    published: dict[str, str] = field(default_factory=dict)  # path -> hash of the bytes last published
    computations: int = 0
    interval: float = 0.0  # seconds until the next tick, set by tick()
    procs: int = 1  # simulation processes (1: in this one)
    wake: threading.Event = field(default_factory=threading.Event)  # set it to tick now (e.g. a market moved)
    _pool: object = None

    def __post_init__(self):
        self.model = self.model or LiveModel(self.ds)
        if self.refreshed is None:
            stats = self.ds.data_dir / "nflverse" / "passing.csv"
            self.refreshed = stats.stat().st_mtime if stats.exists() else 0.0

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

    def record(self, kind: str, t: float, **fields) -> None:
        """Append one record to today's history file (a gzip member per record; gzip readers read them all)."""
        if self.history is None:
            return
        path = Path(self.history) / f"{datetime.fromtimestamp(t, timezone.utc):%Y-%m-%d}.jsonl.gz"
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"t": _iso(t), "kind": kind, **fields}, default=_f, separators=(",", ":"), sort_keys=True)
        with gzip.open(path, "at") as f:
            f.write(line + "\n")

    def _record_prices(self, t: float, quotes: dict) -> None:
        """A "kalshi" record: every game's prices in a file's first one, after that only the games that changed."""
        day = f"{datetime.fromtimestamp(t, timezone.utc):%Y-%m-%d}"
        if self._logged is None or self._logged[0] != day:
            self.record("kalshi", t, prices=quotes)
        else:
            last = self._logged[1]
            changed = {ev: v for ev, v in quotes.items() if last.get(ev) != v}
            gone = sorted(ev for ev in last if ev not in quotes)
            self.record("kalshi", t, prices=changed, partial=True, **({"gone": gone} if gone else {}))
        self._logged = (day, json.loads(json.dumps(quotes, default=_f)))

    # --- the loop -----------------------------------------------------------------------------------------------

    def start(self) -> None:
        """Publish the page and set up every game on the scoreboard, so the first update of a game is fast."""
        self.put("index.html", PAGE.read_bytes(), "text/html; charset=utf-8")
        self.put("game.html", GAME_PAGE.read_bytes(), "text/html; charset=utf-8")
        self.put("edges.html", EDGES_PAGE.read_bytes(), "text/html; charset=utf-8")
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

    def _busy(self, games: list[dict], now: float) -> bool:
        """A game is in progress or starts within `soon` seconds."""
        def starting(g):
            try:
                return datetime.fromisoformat(g["kickoff"].replace("Z", "+00:00")).timestamp() - now <= self.soon
            except (KeyError, ValueError):
                return True
        return any(g["state"] == "in" or (g["state"] == "pre" and starting(g)) for g in games)

    def _upkeep(self, games: list[dict], now: float) -> None:
        """The daily data refresh (when quiet) and the pregame expectations' recomputation (see the module doc)."""
        wall = time.time()
        if self.refresh and self._job is None and wall - self.refreshed >= self.refresh_every and not self._busy(games, now):
            try:
                self.refresh()
                self.refreshed = wall
                self.log("data: refreshed stats, schedules and play-by-play")
            except Exception as e:  # try again in an hour
                self.refreshed = wall - self.refresh_every + 3600
                self.log(f"data refresh failed: {type(e).__name__}: {e}")
            else:
                self.model.reset()
                self._restart_pool()
                self.preds_at = None  # recompute the expectations from the new data now
                for t in self.games.values():
                    t.sig = None
        if self._job is not None and not self._job.is_alive():
            out, self._job, self._job_out = self._job_out or {}, None, None
            frozen = {team for g in games if g["state"] != "pre" for team in (g["home"], g["away"])}
            changed = {}
            for (season, week), fresh in out.items():
                if isinstance(fresh, Exception):
                    self.log(f"projections {season} week {week} failed: {type(fresh).__name__}: {fresh}")
                    continue
                changed |= self.model.update_preds(season, week, fresh, frozen)
            self.projected = wall
            moved = [f"{t} {a:.0f}->{b:.0f}" for t, (a, b) in sorted(changed.items()) if a is None or abs(a - b) >= 1]
            self.log(f"projections: {', '.join(moved) if moved else 'no change'}")
            if changed:
                self._restart_pool()  # its processes re-read the expectations
            for g in games:
                if g["state"] == "pre" and (g["home"] in changed or g["away"] in changed):
                    self.games.setdefault(g["event"], GameTrack()).sig = None
        if (self.preds_every and self._job is None and self.weeks and any(g["state"] == "pre" for g in games)
                and (self.preds_at is None or wall - self.preds_at >= self.preds_every)):
            self.preds_at, weeks = wall, sorted(self.weeks)

            def job():
                out = {}
                for key in weeks:
                    try:
                        out[key] = self.model.compute_preds(*key)
                    except Exception as e:
                        out[key] = e
                self._job_out = out
            self._job = threading.Thread(target=job, daemon=True)
            self._job.start()

    def _interval(self, games: list[dict], now: float) -> float:
        if any(g["state"] == "in" for g in games):
            return self.live_every
        return self.every if self._busy(games, now) else max(self.every, self.idle_every)

    # --- simulation: in this process, or in `procs` processes ---------------------------------------------------

    def _restart_pool(self, terminate: bool = False) -> None:
        """New processes after the model changed (they hold their own copy of it); `terminate`: stop them now,
        mid-simulation (on exit: otherwise Python waits for the running ones to finish)."""
        if self._pool is not None:
            procs = list(getattr(self._pool, "_processes", {}).values()) if terminate else []
            self._pool.shutdown(wait=False, cancel_futures=True)
            for p in procs:
                p.terminate()
            self._pool = None

    def _computed(self, states: list[dict]):
        """(index, state or exception, seconds) for each game state, as each simulation finishes."""
        if self.procs <= 1 or len(states) <= 1 and self._pool is None:
            for i, s in enumerate(states):
                started = time.perf_counter()
                try:
                    yield i, self.model.compute_state(s), time.perf_counter() - started
                except Exception as e:
                    yield i, e, time.perf_counter() - started
            return
        if self._pool is None:
            import multiprocessing
            from concurrent.futures import ProcessPoolExecutor

            self._pool = ProcessPoolExecutor(self.procs, mp_context=multiprocessing.get_context("spawn"),
                                             initializer=_child_init, initargs=(str(self.ds.data_dir),))
        from concurrent.futures import as_completed

        futures = {self._pool.submit(_child_compute, s): i for i, s in enumerate(states)}
        for f in as_completed(futures):
            try:
                out, secs = f.result()
                yield futures[f], out, secs
            except Exception as e:
                yield futures[f], e, 0.0

    def _poll_box(self, g: dict, t: GameTrack, now: float) -> bool:
        return g["state"] == "in" and (t.polled is None or now - t.polled >= self.box_every)

    def tick(self) -> int:
        """One pass: scoreboard, games.json, changed games, status.json. Returns the number of games computed."""
        now = espn.source.now()
        sb = espn.scoreboard()
        games = self._games(sb)
        self.put_json("games.json", games)
        self._upkeep(games, now)  # before the games: what it changes is recomputed in this tick
        n = 0
        polls = []  # (g, track, situation, signature, due): games whose summary is read this tick
        for g in games:
            ev = g["event"]
            t = self.games.setdefault(ev, GameTrack())
            try:
                if t.error and now - t.failed < self.retry:
                    continue
                sit = espn.situation(ev, sb)
                self.weeks.add((sit["season"], sit["week"]))
                sig = espn.signature(sit)
                due = self._due(g, t, sig, now)
                if due or self._poll_box(g, t, now):
                    polls.append((g, t, sit, sig, due))
            except Exception as e:
                self._failed(g, t, e, now)
        jobs = []  # (g, track, state, signature, box)
        for (g, t, sit, sig, due), sm in zip(polls, self._summaries([p[0]["event"] for p in polls])):
            try:
                if isinstance(sm, Exception):
                    raise sm
                s = espn.add_summary(sit, sm)
                t.polled = now
                box = espn.box(s)
                if due or box != t.box:
                    jobs.append((g, t, s, sig, box))
            except Exception as e:
                self._failed(g, t, e, now)
        for i, out, secs in self._computed([j[2] for j in jobs]):
            g, t, s, sig, box = jobs[i]
            if isinstance(out, Exception):
                self._failed(g, t, out, now)
                continue
            self.computations += 1
            n += 1
            t.sig, t.box, t.computed, t.error, t.out = sig, box, now, None, out
            wrote = self.put_json(f"state/{g['event']}.json", out)
            if wrote:
                self.record("state", now, card=summary(out, g))
            self.log(f"{g['away']} @ {g['home']} {out['clock']}: {out['now']} ({secs:.1f} s{'' if wrote else ', unchanged'})")
        self.put_json("summary.json", [summary(self.games[g["event"]].out, g) for g in games])
        if self.prices and (self.priced is None or time.monotonic() - self.priced >= self.prices_every):
            self.priced = time.monotonic()
            try:
                quotes = self.prices(games)
                if self.put_json("kalshi.json", quotes):
                    self._record_prices(now, quotes)
            except Exception as e:  # the page keeps the last prices
                self.log(f"prices: {type(e).__name__}: {e}")
        self.interval = self._interval(games, now)
        self.put_json("status.json", {
            "updated": _iso(now), "every": self.interval, "live": sum(g["state"] == "in" for g in games),
            "errors": {ev: t.error for ev, t in self.games.items() if t.error},
            "data": {"stats": _iso(self.refreshed) if self.refreshed else None,
                     "projections": _iso(self.projected) if self.projected else None}})
        return n

    def _summaries(self, events: list[str]) -> list:
        """ESPN's summary of each game (or the exception), fetched in parallel."""
        if len(events) <= 1:
            out = []
            for ev in events:
                try:
                    out.append(espn.summary(ev))
                except Exception as e:
                    out.append(e)
            return out
        from concurrent.futures import ThreadPoolExecutor

        def one(ev):
            try:
                return espn.summary(ev)
            except Exception as e:
                return e
        with ThreadPoolExecutor(min(8, len(events))) as pool:
            return list(pool.map(one, events))

    def _failed(self, g: dict, t: GameTrack, e: Exception, now: float) -> None:
        t.error, t.failed = f"{type(e).__name__}: {e}", now
        self.log(f"{g['away']} @ {g['home']}: {t.error}")
        if e.__traceback__ is not None:
            traceback.print_exception(e)

    def run(self, until=None) -> None:
        """Tick every `interval` seconds (wall clock) until `until()` is true, Ctrl-C or SIGTERM (docker stop)."""
        import signal

        if threading.current_thread() is threading.main_thread():
            def stop(*_):
                raise KeyboardInterrupt
            signal.signal(signal.SIGTERM, stop)  # so the simulation processes are shut down too
        self.start()
        try:
            while not (until and until()):
                t0 = time.monotonic()
                try:
                    self.tick()
                except Exception as e:  # the scoreboard itself failed: keep the last files, try again
                    self.log(f"tick failed: {type(e).__name__}: {e}")
                # the next tick: after `interval`, or as soon as something sets `wake`
                self.wake.wait(max(0.0, (self.interval or self.every) - (time.monotonic() - t0)))
                self.wake.clear()
        except KeyboardInterrupt:
            pass
        finally:
            self._restart_pool(terminate=True)


_MODEL: LiveModel | None = None  # in a simulation process


def _child_init(data_dir: str) -> None:
    global _MODEL
    from ..data import load

    _MODEL = LiveModel(load(data_dir))


def _child_compute(s: dict) -> tuple[dict, float]:
    started = time.perf_counter()
    return _MODEL.compute_state(s), time.perf_counter() - started


def read_history(path: Path | str) -> list[dict]:
    """The records of one history file (Worker.history), oldest first."""
    with gzip.open(path, "rt") as f:
        return [json.loads(line) for line in f if line.strip()]


def prices_at(records: list[dict]):
    """(t, every game's prices) after each "kalshi" record of read_history, partial records filled in."""
    prices = {}
    for r in records:
        if r["kind"] != "kalshi":
            continue
        if not r.get("partial"):
            prices = {}
        prices = {**{ev: v for ev, v in prices.items() if ev not in r.get("gone", ())}, **r["prices"]}
        yield r["t"], prices
