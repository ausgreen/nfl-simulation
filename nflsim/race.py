"""Record every fast source side by side during games, to measure which one shows each play first (docs/data-speed.md,
section 6). One gzip JSONL file; every record has "src" and "recv" (wall clock on arrival):

    espn         ESPN's scoreboard, polled every `espn_every` s: a game's situation when it changes (clock, score,
                 down and distance, last play)
    espn_box     ESPN's summary, polled every `box_every` s per live game: each QB's passing yards when they change
    kalshi_live  Kalshi's live data (Stats Perform), `game_stats` polled every `live_every` s per live game: each new
                 play (Stats Perform's id, description, score, yards, Kalshi's "wall_clock") the first time it shows
    polymarket   Polymarket's sports WebSocket: every NFL game update (score, period, clock)
    kalshi_ws    Kalshi's WebSocket: every ticker (bid/ask/last) and trade message for the NFL markets

    uv run nflsim feed race recordings/race-2026-10-22.jsonl.gz --hours 4
"""

from __future__ import annotations

import asyncio
import gzip
import json
import time
import urllib.request
from pathlib import Path

from . import kalshi, kalshi_ws
from .live import espn

LIVE = "https://api.elections.kalshi.com/trade-api/v2"
POLY_SPORTS = "wss://sports-api.polymarket.com/ws"


class Sink:
    """Buffered writer: a gzip member every `every` seconds, so the file is readable while recording."""

    def __init__(self, path: Path, every: float = 2.0):
        self.path, self.every, self.buf, self.at = Path(path), every, [], time.time()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.counts: dict[str, int] = {}

    def add(self, src: str, **rec) -> None:
        self.buf.append({"src": src, "recv": rec.pop("recv", time.time()), **rec})
        self.counts[src] = self.counts.get(src, 0) + 1
        if time.time() - self.at >= self.every:
            self.flush()

    def flush(self) -> None:
        self.at = time.time()
        if self.buf:
            with gzip.open(self.path, "at") as f:
                f.writelines(json.dumps(r, separators=(",", ":"), default=str) + "\n" for r in self.buf)
            self.buf = []


def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "nflsim"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def _plays(game_stats: dict) -> list[dict]:
    out = []

    def walk(o):
        if isinstance(o, dict):
            if o.get("type") == "football_play":
                out.append(o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(game_stats)
    return out


class Race:
    def __init__(self, path: Path, hours: float | None = None, espn_every: float = 2.0, box_every: float = 3.0,
                 live_every: float = 1.0, say=print):
        self.sink = Sink(path)
        self.until = time.time() + hours * 3600 if hours else None
        self.espn_every, self.box_every, self.live_every, self.say = espn_every, box_every, live_every, say
        self.live: dict[str, dict] = {}  # event -> espn.games row, for games in progress

    def running(self) -> bool:
        return self.until is None or time.time() < self.until

    async def espn(self) -> None:
        last: dict[str, tuple] = {}
        while self.running():
            try:
                sb = await asyncio.to_thread(espn.scoreboard)
                recv = time.time()
                games = espn.games(sb)
                self.live = {g["event"]: g for g in games if g["state"] == "in"}
                for ev in self.live:
                    s = espn.situation(ev, sb)
                    key = (s["home_score"], s["away_score"], s["period"], s.get("display_clock"), s["last_play"],
                           s["down"], s["togo"], s["yl"], s["pos"])
                    if key != last.get(ev):
                        last[ev] = key
                        self.sink.add("espn", recv=recv, event=ev, home=s["home"], away=s["away"],
                                      score=[s["home_score"], s["away_score"]], period=s["period"],
                                      clock=s.get("display_clock"), down=s["down"], togo=s["togo"], yl=s["yl"],
                                      pos=s["pos"], play=s["last_play"])
            except Exception as e:
                self.sink.add("error", where="espn", error=f"{type(e).__name__}: {e}")
            await asyncio.sleep(self.espn_every if self.live else 30.0)

    async def espn_box(self) -> None:
        last: dict[str, tuple] = {}
        while self.running():
            for ev in list(self.live):
                try:
                    s = await asyncio.to_thread(lambda: espn.add_summary(espn.situation(ev), espn.summary(ev)))
                    b = espn.box(s)
                    if b != last.get(ev):
                        last[ev] = b
                        self.sink.add("espn_box", event=ev, box=b)
                except Exception as e:
                    self.sink.add("error", where="espn_box", event=ev, error=f"{type(e).__name__}: {e}")
            await asyncio.sleep(self.box_every)

    async def kalshi_live(self) -> None:
        milestones: dict[str, str | None] = {}
        seen: dict[str, set] = {}
        while self.running():
            for ev, g in list(self.live.items()):
                try:
                    if ev not in milestones:
                        milestones[ev] = await asyncio.to_thread(self._milestone, g)
                        self.sink.add("kalshi_live_milestone", event=ev, milestone=milestones[ev])
                    mid = milestones[ev]
                    if not mid:
                        continue
                    stats = await asyncio.to_thread(_get, f"{LIVE}/live_data/milestone/{mid}/game_stats")
                    recv = time.time()
                    for p in _plays(stats):
                        pid = p.get("play_id")
                        if pid in seen.setdefault(ev, set()):
                            continue
                        seen[ev].add(pid)
                        self.sink.add("kalshi_live", recv=recv, event=ev, play_id=pid, desc=p.get("description"),
                                      score=[p.get("home_points"), p.get("away_points")], clock=p.get("clock"),
                                      down=p.get("down"), yfd=p.get("yfd"), type=p.get("play_type"),
                                      wall_clock=p.get("wall_clock"), wall_clock_source=p.get("wall_clock_source"))
                except Exception as e:
                    self.sink.add("error", where="kalshi_live", event=ev, error=f"{type(e).__name__}: {e}")
            await asyncio.sleep(self.live_every)

    @staticmethod
    def _milestone(g: dict) -> str | None:
        """Kalshi's milestone id for the game (its live data's key), from the game market's event ticker."""
        for m in kalshi.fetch_markets("KXNFLGAME"):
            if kalshi._teams(m["event_ticker"], g["away"], g["home"]):
                ms = _get(f"{LIVE}/milestones?limit=5&related_event_ticker={m['event_ticker']}").get("milestones", [])
                return ms[0]["id"] if ms else None
        return None

    async def polymarket(self) -> None:
        import websockets

        while self.running():
            try:
                async with websockets.connect(POLY_SPORTS, ping_interval=None, max_size=None) as ws:
                    async for raw in ws:
                        if raw == "ping":
                            await ws.send("pong")
                            continue
                        recv = time.time()
                        try:
                            msg = json.loads(raw)
                        except ValueError:
                            continue
                        for m in msg if isinstance(msg, list) else [msg]:
                            if isinstance(m, dict) and m.get("leagueAbbreviation", "").upper() == "NFL":
                                self.sink.add("polymarket", recv=recv, msg=m)
                        if not self.running():
                            return
            except Exception as e:
                self.sink.add("error", where="polymarket", error=f"{type(e).__name__}: {e}")
                await asyncio.sleep(5)

    async def kalshi_ws(self) -> None:
        tickers = [m["ticker"] for s in (kalshi.SERIES, *kalshi.SCORE_SERIES) for m in kalshi.fetch_markets(s)]
        async for msg in kalshi_ws.stream(["ticker", "trade"], tickers):
            if msg.get("type") in ("ticker", "trade", "reconnected"):
                self.sink.add("kalshi_ws", recv=msg.pop("recv"), msg=msg)
            if not self.running():
                return

    async def report(self) -> None:
        while self.running():
            await asyncio.sleep(60)
            self.say(f"{time.strftime('%H:%M:%S')} live games {len(self.live)} · " +
                     " · ".join(f"{k} {v}" for k, v in sorted(self.sink.counts.items())))

    async def run(self) -> None:
        tasks = [asyncio.create_task(c()) for c in (self.espn, self.espn_box, self.kalshi_live, self.polymarket,
                                                     self.kalshi_ws, self.report)]
        try:
            while self.running():
                await asyncio.sleep(1)
        finally:
            for t in tasks:
                t.cancel()
            self.sink.flush()


def record(path: Path | str, hours: float | None = None) -> None:
    try:
        asyncio.run(Race(Path(path), hours).run())
    except KeyboardInterrupt:
        pass
