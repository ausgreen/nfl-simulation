"""Where ESPN responses come from: the live API, a recorder that saves every response, or a replay of a recording.

A recording is a gzipped JSON-lines file, one line per response: {"t": unix seconds, "url": ..., "body": {...}}.
Replay answers each URL with the latest recorded body at or before its clock (the earliest one if the clock is
before it), so one recorded game can be stepped through or run faster than real time, any day of the week.
"""

from __future__ import annotations

import bisect
import gzip
import json
import time
import urllib.request
from pathlib import Path


def fetch(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "nflsim", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


class Live:
    def get(self, url: str) -> dict:
        return fetch(url)

    def now(self) -> float:
        return time.time()


class Recorder(Live):
    """Live feed that appends each response to `path` (created if missing; appending to an existing one is fine).
    A response identical to the previous one for the same URL is not written again: replay would return the
    earlier copy anyway."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.last: dict[str, str] = {}

    def get(self, url: str) -> dict:
        body = fetch(url)
        text = json.dumps(body, separators=(",", ":"), sort_keys=True)
        if self.last.get(url) != text:
            self.last[url] = text
            with gzip.open(self.path, "at", encoding="utf-8") as f:
                f.write(f'{{"t":{time.time()},"url":{json.dumps(url)},"body":{text}}}\n')
        return body


class Replay:
    """Recorded feed on a virtual clock. `speed` > 0 runs the clock from the first response at that multiple of
    real time; `speed=0` keeps it still until `seek`/`advance` moves it (for tests)."""

    def __init__(self, path: Path | str, speed: float = 0.0, start: float | None = None):
        self.by_url: dict[str, tuple[list[float], list[dict]]] = {}
        with gzip.open(path, "rt", encoding="utf-8") as f:
            for line in f:
                r = json.loads(line)
                ts, bodies = self.by_url.setdefault(r["url"], ([], []))
                ts.append(r["t"])
                bodies.append(r["body"])
        if not self.by_url:
            raise ValueError(f"{path}: empty recording")
        self.first = min(ts[0] for ts, _ in self.by_url.values())
        self.last = max(ts[-1] for ts, _ in self.by_url.values())
        self.speed = speed
        self._t = self.first if start is None else start
        self._wall = time.time()

    def now(self) -> float:
        if self.speed:
            return self._t + (time.time() - self._wall) * self.speed
        return self._t

    def seek(self, t: float) -> None:
        self._t, self._wall = t, time.time()

    def advance(self, seconds: float) -> None:
        self.seek(self.now() + seconds)

    @property
    def finished(self) -> bool:
        return self.now() >= self.last

    def times(self, url: str) -> list[float]:
        return self.by_url[url][0] if url in self.by_url else []

    def get(self, url: str) -> dict:
        if url not in self.by_url:
            raise KeyError(f"not in the recording: {url}")
        ts, bodies = self.by_url[url]
        return bodies[max(0, bisect.bisect_right(ts, self.now()) - 1)]


def record(path: Path | str, every: float = 5.0, hours: float | None = None, once: bool = False, log=print) -> None:
    """Poll ESPN like the hosted worker will and save every response: the scoreboard each `every` seconds, a game's
    summary whenever its signature changes (and once at the start). Stops after `hours`, or Ctrl-C."""
    from . import espn

    espn.use(Recorder(path))
    seen: dict[str, tuple] = {}
    stop = time.time() + hours * 3600 if hours else None
    try:
        while True:
            t0 = time.time()
            sb = espn.scoreboard()
            for e in sb.get("events", []):
                sig = espn.signature(espn.situation(e["id"], sb))
                if seen.get(e["id"]) != sig:
                    espn.summary(e["id"])
                    if e["id"] in seen:
                        log(f"{time.strftime('%H:%M:%S')} {e['shortName']}: {sig[-1][:70]!r}")
                    seen[e["id"]] = sig
            if once or (stop and time.time() >= stop):
                break
            time.sleep(max(0.0, every - (time.time() - t0)))
    except KeyboardInterrupt:
        pass
    log(f"recorded {len(seen)} games -> {path}")
