"""Kalshi's WebSocket market data: every price change and trade as it happens.

The REST endpoint kalshi.py polls is cached for 15 s by Kalshi's CDN, so its prices are up to ~20 s old by the time
we read them. The WebSocket pushes each change instead. It needs an API key (account settings on kalshi.com):
KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH (the RSA private key's PEM file), from the environment or from .env
in the working directory, and the `kalshi` extra (`uv sync --extra kalshi`).

    async for msg in stream(["ticker", "trade"], tickers): ...

Each message is Kalshi's JSON ({"type": "ticker", "sid", "seq", "msg": {...}}) plus "recv" (wall clock on arrival).
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from collections.abc import AsyncIterator, Mapping
from pathlib import Path

URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
PATH = "/trade-api/ws/v2"  # what the handshake signs
CHUNK = 500  # market tickers per subscribe command


def _dotenv(path: Path = Path(".env")) -> dict[str, str]:
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def credentials(env: Mapping[str, str] | None = None) -> tuple[str, object]:
    """(key id, private key) from the environment, else .env."""
    from cryptography.hazmat.primitives import serialization

    env = {**_dotenv(), **os.environ} if env is None else env
    missing = [k for k in ("KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY_PATH") if not env.get(k)]
    if missing:
        raise SystemExit(f"Kalshi's WebSocket needs these environment variables: {', '.join(missing)}")
    pem = Path(os.path.expanduser(env["KALSHI_PRIVATE_KEY_PATH"])).read_bytes()
    return env["KALSHI_API_KEY_ID"], serialization.load_pem_private_key(pem, password=None)


def auth_headers(key_id: str, key, method: str = "GET", path: str = PATH, now_ms: int | None = None) -> dict:
    """Kalshi's signed request headers: RSA-PSS (SHA-256) over timestamp + method + path."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    ts = str(int(time.time() * 1000) if now_ms is None else now_ms)
    sig = key.sign((ts + method + path).encode(),
                   padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                   hashes.SHA256())
    return {"KALSHI-ACCESS-KEY": key_id, "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
            "KALSHI-ACCESS-TIMESTAMP": ts}


async def stream(channels: list[str], tickers: list[str] | None = None, creds=None,
                 retry: float = 1.0) -> AsyncIterator[dict]:
    """Messages from `channels` for `tickers` (None: every market), reconnecting after a dropped connection (each
    reconnect yields {"type": "reconnected", "recv"} first, since messages in between are lost)."""
    import websockets

    key_id, key = creds or credentials()
    wait, first = retry, True
    while True:
        try:
            async with websockets.connect(URL, additional_headers=auth_headers(key_id, key),
                                          ping_interval=10, max_size=None) as ws:
                parts = [None] if tickers is None else [tickers[i:i + CHUNK] for i in range(0, len(tickers), CHUNK)]
                for i, part in enumerate(parts):
                    params = {"channels": channels, **({"market_tickers": part} if part else {})}
                    await ws.send(json.dumps({"id": i + 1, "cmd": "subscribe", "params": params}))
                if not first:
                    yield {"type": "reconnected", "recv": time.time()}
                first, wait = False, retry
                async for raw in ws:
                    msg = json.loads(raw)
                    msg["recv"] = time.time()
                    yield msg
        except (OSError, websockets.ConnectionClosed, websockets.InvalidStatus) as e:
            if isinstance(e, websockets.InvalidStatus) and e.response.status_code in (401, 403):
                raise SystemExit(f"Kalshi refused the WebSocket key: HTTP {e.response.status_code}") from e
            await asyncio.sleep(wait)
            wait = min(wait * 2, 30.0)


def _mid(m: dict) -> float | None:
    try:
        return (float(m["yes_bid_dollars"]) + float(m["yes_ask_dollars"])) / 2
    except (KeyError, TypeError, ValueError):
        return None


class Book:
    """Kalshi's open NFL markets (kalshi.SERIES and SCORE_SERIES), kept current from the WebSocket in a background
    thread: a REST listing first (re-read every `relist` seconds for new and closed markets; prices already updated
    by the feed are kept), then every ticker message. `live_prices(games)` is kalshi.live_prices over it, a drop-in
    for the worker's and the local app's `prices`. With `log` (a directory), every ticker and trade message is also
    appended to <log>/<UTC date>.jsonl.gz as it arrives, for timing the market exactly."""

    MOVE = 0.03  # a game-winner market's midpoint moving this much in one message calls `on_move`

    def __init__(self, relist: float = 600.0, log: Path | None = None, creds=None, on_move=None):
        import threading

        self.relist, self.log, self.creds = relist, log, creds
        self.on_move = on_move  # called with the ticker when a game-winner market jumps: something just happened
        self.lock = threading.Lock()
        self.markets: dict[str, dict] = {}  # ticker -> REST market dict, prices updated in place
        self.received: float | None = None  # wall clock of the last message
        self.error: str | None = None
        self._thread = None
        self._buf: list[dict] = []  # messages not yet written to `log`
        self._flushed = 0.0

    def start(self) -> "Book":
        import threading

        self.creds = self.creds or credentials()
        self.list_markets()
        self._thread = threading.Thread(target=lambda: asyncio.run(self._run()), name="kalshi-ws", daemon=True)
        self._thread.start()
        return self

    def list_markets(self) -> bool:
        """Re-read the open markets over REST; True if the set of tickers changed."""
        from . import kalshi

        fresh = {m["ticker"]: m for s in (kalshi.SERIES, *kalshi.SCORE_SERIES) for m in kalshi.fetch_markets(s)}
        with self.lock:
            changed = fresh.keys() != self.markets.keys()
            self.markets = {t: self.markets.get(t, m) for t, m in fresh.items()}
        return changed

    def apply(self, msg: dict) -> None:
        """One WebSocket message: a ticker message updates its market's bid, ask and last price."""
        self.received = msg.get("recv", time.time())
        if msg.get("type") != "ticker":
            return
        m = msg["msg"]
        with self.lock:
            market = self.markets.get(m.get("market_ticker"))
            if market is None:
                return
            before = _mid(market)
            for k_ws, k_rest in (("yes_bid_dollars", "yes_bid_dollars"), ("yes_ask_dollars", "yes_ask_dollars"),
                                 ("price_dollars", "last_price_dollars")):
                if m.get(k_ws) is not None:
                    market[k_rest] = m[k_ws]
            after = _mid(market)
        if (self.on_move and m["market_ticker"].startswith("KXNFLGAME-") and None not in (before, after)
                and abs(after - before) >= self.MOVE):
            self.on_move(m["market_ticker"])

    def series(self, prefix: str) -> list[dict]:
        with self.lock:
            return [dict(m) for t, m in self.markets.items() if t.split("-", 1)[0] == prefix]

    def live_prices(self, games: list[dict]) -> dict:
        from . import kalshi

        if self._thread is not None and not self._thread.is_alive():  # the feed stopped (key refused): poll REST
            return kalshi.live_prices(games)
        with self.lock:
            ms = [dict(m) for m in self.markets.values()]
        passing = [m for m in ms if m["ticker"].startswith(kalshi.SERIES + "-")]
        scores = [m for m in ms if m["ticker"].split("-", 1)[0] in kalshi.SCORE_SERIES]
        return kalshi.live_prices(games, passing, scores)

    def _write(self, msg: dict, every: float = 5.0) -> None:
        """Buffer the message; every `every` seconds append the buffer to the day's file as one gzip member, so the
        file stays readable while the feed runs and at most a few seconds are lost if it stops."""
        import gzip
        from datetime import datetime, timezone

        if self.log is None:
            return
        if msg.get("type") in ("ticker", "trade", "reconnected"):
            self._buf.append(msg)
        if self._buf and msg["recv"] - self._flushed >= every:
            self._flushed = msg["recv"]
            by_day: dict[str, list] = {}
            for m in self._buf:
                by_day.setdefault(f"{datetime.fromtimestamp(m['recv'], timezone.utc):%Y-%m-%d}", []).append(m)
            Path(self.log).mkdir(parents=True, exist_ok=True)
            for day, ms in by_day.items():
                with gzip.open(Path(self.log) / f"{day}.jsonl.gz", "at") as f:
                    f.writelines(json.dumps(m, separators=(",", ":")) + "\n" for m in ms)
            self._buf = []

    async def _run(self) -> None:
        while True:
            with self.lock:
                tickers = sorted(self.markets)
            listed = time.monotonic()
            try:
                async for msg in stream(["ticker", "trade"], tickers, self.creds):
                    self.apply(msg)
                    self._write(msg)
                    self.error = None
                    if time.monotonic() - listed >= self.relist:
                        listed = time.monotonic()
                        if await asyncio.to_thread(self.list_markets):
                            break  # new or closed markets: subscribe again
            except SystemExit as e:  # a refused key: stop, the caller falls back to REST
                self.error = str(e)
                return
            except Exception as e:  # REST listing failed: keep streaming the old set after a pause
                self.error = f"{type(e).__name__}: {e}"
                await asyncio.sleep(5)


def price_source(log: Path | None = None, on_move=None, say=lambda s: print(s, flush=True)):
    """(prices, book): the best `prices` callable available — a started Book's live_prices when the key and the
    `kalshi` extra are there, else kalshi.live_prices (REST, cached up to 15 s by Kalshi's CDN) and None."""
    from . import kalshi

    try:
        book = Book(log=log, on_move=on_move).start()
    except (SystemExit, ImportError, OSError) as e:
        say(f"Kalshi prices: REST polling ({e})")
        return kalshi.live_prices, None
    say(f"Kalshi prices: WebSocket feed, {len(book.markets)} markets" + (f", logged to {log}/" if log else ""))
    return book.live_prices, book
