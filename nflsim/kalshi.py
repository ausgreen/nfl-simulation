"""Kalshi passing-yard markets: fetch public prices and save timestamped snapshots.

Uses the public market-data endpoint (no API key). Each Kalshi market is one QB at
one threshold ("Dak Prescott: 250+ passing yards"); the yes ask is what buying
yes costs right now, before fees.
"""

from __future__ import annotations

import json
import re
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .data import Dataset

API = "https://api.elections.kalshi.com/trade-api/v2"
SERIES = "KXNFLPASSYDS"
SERIES_SLUG = "pro-football-passing-yards"  # Kalshi's page slug for the series title
MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}
COLUMNS = ["fetched_at", "ticker", "event_ticker", "qb", "player_id", "season", "week", "team",
           "opponent", "home_away", "line", "yes_bid", "yes_ask", "yes_bid_size", "yes_ask_size",
           "last_price", "volume",
           "open_interest", "close_time", "status"]


def _get(path: str, params: dict) -> dict:
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "nflsim"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def fetch_markets(series: str = SERIES, status: str = "open") -> list[dict]:
    """All markets in a series, following the pagination cursor."""
    out, cursor = [], None
    while True:
        params = {"series_ticker": series, "status": status, "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        page = _get("/markets", params)
        out += page.get("markets", [])
        cursor = page.get("cursor")
        if not cursor:
            return out


def event_url(event_ticker: str, series: str = SERIES, slug: str = SERIES_SLUG) -> str:
    """Kalshi web page for an event (one game; lists every QB and threshold)."""
    return f"https://kalshi.com/markets/{series.lower()}/{slug}/{event_ticker.lower()}"


def _norm(name: str) -> str:
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    name = re.sub(r"\b(jr|sr|ii|iii|iv)\b\.?", "", name.lower())
    return re.sub(r"[^a-z]", "", name)


def _event_date(event_ticker: str) -> str | None:
    """KXNFLPASSYDS-26OCT11BALATL -> 2026-10-11 (the game's scheduled date)."""
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})", event_ticker)
    if not m or m.group(2) not in MONTHS:
        return None
    return f"20{m.group(1)}-{MONTHS[m.group(2)]:02d}-{m.group(3)}"


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def to_frame(ds: Dataset, markets: list[dict], fetched_at: str) -> pd.DataFrame:
    """One row per market, matched to a player ID and scheduled game where possible."""
    names = {}
    for df in (ds.career_games, ds.roster):
        for qb, pid in zip(df.qb, df.player_id):
            names.setdefault(_norm(qb), pid)
    gc = ds.game_context
    rows = []
    for m in markets:
        qb = m.get("title", "").split(":")[0].strip()
        pid = names.get(_norm(qb))
        row = {"fetched_at": fetched_at, "ticker": m["ticker"], "event_ticker": m["event_ticker"],
               "qb": qb, "player_id": pid, "line": round(_num(m.get("floor_strike")) + 0.5)
               if m.get("strike_type") == "greater" else None,
               "yes_bid": _num(m.get("yes_bid_dollars")), "yes_ask": _num(m.get("yes_ask_dollars")),
               "yes_bid_size": _num(m.get("yes_bid_size_fp")), "yes_ask_size": _num(m.get("yes_ask_size_fp")),
               "last_price": _num(m.get("last_price_dollars")), "volume": _num(m.get("volume_fp")),
               "open_interest": _num(m.get("open_interest_fp")), "close_time": m.get("close_time"),
               "status": m.get("status")}
        day = _event_date(m["event_ticker"])
        if pid and day:
            team = ds.team_of(pid)
            g = gc[(gc.game_date == day) & ((gc.home == team) | (gc.away == team))]
            if len(g):
                g = g.iloc[0]
                row.update(season=int(g.season), week=int(g.week), team=team,
                           opponent=g.away if g.home == team else g.home,
                           home_away="H" if g.home == team else "A")
        rows.append(row)
    return pd.DataFrame(rows, columns=COLUMNS)


def snapshot_dir(ds: Dataset) -> Path:
    return ds.data_dir / "kalshi"


def save_snapshot(ds: Dataset, series: str = SERIES) -> Path:
    fetched = datetime.now(timezone.utc).replace(microsecond=0)
    frame = to_frame(ds, fetch_markets(series), fetched.isoformat())
    out = snapshot_dir(ds) / f"{series}_{fetched:%Y%m%dT%H%M%SZ}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(out, index=False)
    return out


def latest_snapshot(ds: Dataset, series: str = SERIES) -> pd.DataFrame | None:
    files = sorted(snapshot_dir(ds).glob(f"{series}_*.csv"))
    if not files:
        return None
    return pd.read_csv(files[-1], dtype={"player_id": str})


def as_quotes(snap: pd.DataFrame) -> pd.DataFrame:
    """Matched markets in the shape markets.scan_quotes expects (priced at the yes ask)."""
    s = snap.dropna(subset=["player_id", "season", "week", "line", "yes_ask"])
    return pd.DataFrame({
        "quoted_on": s.fetched_at.str[:10], "season": s.season.astype(int), "week": s.week.astype(int),
        "market_set": "kalshi", "qb": s.qb, "player_id": s.player_id, "opponent": s.opponent,
        "home_away": s.home_away, "line": s.line.astype(int), "yes_price": s.yes_ask,
        "yes_bid": s.yes_bid, "volume": s.volume, "source": s.ticker,
        "url": s.event_ticker.map(event_url),
    })


TO_NFLVERSE = {"LAR": "LA", "JAC": "JAX", "WSH": "WAS"}  # Kalshi team codes that differ from nflverse's


def _teams(event_ticker: str, away: str, home: str) -> tuple[str, str] | None:
    """Kalshi's (away, home) codes if the event (KXNFLPASSYDS-26OCT18CHIATL) is this matchup."""
    pair = event_ticker.rsplit("-", 1)[-1][7:]
    for i in range(2, len(pair) - 1):
        a, h = pair[:i], pair[i:]
        if TO_NFLVERSE.get(a, a) == away and TO_NFLVERSE.get(h, h) == home:
            return a, h
    return None


def live_prices(games: list[dict], markets: list[dict] | None = None) -> dict:
    """Open markets per game on the scoreboard (espn.games rows), for the page to show beside the model's chances:
    {event: {team: [{"qb", "url", "lines": {line: {"ask", "bid", "last"}}}]}}, prices in dollars."""
    markets = fetch_markets() if markets is None else markets
    out = {}
    for g in games:
        for m in markets:
            codes = _teams(m["event_ticker"], g["away"], g["home"])
            if not codes or m.get("strike_type") != "greater" or _num(m.get("floor_strike")) is None:
                continue
            player = m["ticker"].split("-")[2]
            code = max((c for c in codes if player.startswith(c)), key=len, default=None)
            if code is None:
                continue
            team = TO_NFLVERSE.get(code, code)
            qb = m.get("title", "").split(":")[0].strip()
            rows = out.setdefault(g["event"], {}).setdefault(team, [])
            row = next((r for r in rows if r["qb"] == qb), None)
            if row is None:
                row = {"qb": qb, "url": event_url(m["event_ticker"]), "lines": {}}
                rows.append(row)
            row["lines"][str(round(_num(m["floor_strike"]) + 0.5))] = {
                "ask": _num(m.get("yes_ask_dollars")), "bid": _num(m.get("yes_bid_dollars")),
                "last": _num(m.get("last_price_dollars"))}
    return out
