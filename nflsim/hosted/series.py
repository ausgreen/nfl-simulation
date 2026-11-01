"""Each game's chances over time, for chart.html: the model's (from each published state's summary card) and Kalshi's
(the mid of each market's bid and ask, from kalshi.json), as compact step series that gain a point only when a value
moves by at least STEP, plus marks for quarters and scores. Rebuilt after a restart from the worker's history file.

series/<event>.json: {"event", "home", "away", "names": {"qb:<team>": passer}, "model": {key: [[t, value], ...]},
"kalshi": {key: [[t, value], ...]}, "marks": [[t, label], ...]}, t in Unix seconds; keys "win" (the home team wins the
game) and "qb:<team>:<line>" (his passing yards reach the line).
"""

from __future__ import annotations

from datetime import datetime

STEP = 0.01   # a series gains a point when its value moves this much (1 point of chance, 1 cent)


def _t(t) -> int:
    return int(datetime.fromisoformat(t).timestamp()) if isinstance(t, str) else int(t)


def _mid(q: dict) -> float | None:
    bid, ask, last = q.get("bid"), q.get("ask"), q.get("last")
    if bid is not None and ask is not None and ask >= bid:
        return (bid + ask) / 2
    return last if last is not None else ask


class Series:
    def __init__(self):
        self.games: dict[str, dict] = {}
        self.dirty: set[str] = set()

    def _game(self, event: str, home: str | None = None, away: str | None = None) -> dict:
        g = self.games.setdefault(event, {"event": event, "home": home, "away": away, "names": {}, "model": {},
                                          "kalshi": {}, "marks": [], "_period": None, "_score": None})
        g["home"], g["away"] = g["home"] or home, g["away"] or away
        return g

    def _add(self, g: dict, side: str, key: str, t: int, v: float | None) -> None:
        if v is None:
            return
        pts = g[side].setdefault(key, [])
        if not pts or abs(pts[-1][1] - v) >= STEP:
            pts.append([t, round(float(v), 4)])
            self.dirty.add(g["event"])

    def add_state(self, t, card: dict) -> None:
        """A game's summary card (live.app.summary) as published at t."""
        t = _t(t)
        g = self._game(card["event"], card.get("home"), card.get("away"))
        if card.get("homeWin") is not None and card.get("status") != "pre":
            self._add(g, "model", "win", t, card["homeWin"])
        for q in card.get("qbs") or []:
            if q.get("qb"):
                g["names"][f"qb:{q['team']}"] = q["qb"]
            for line in q.get("lines") or []:
                self._add(g, "model", f"qb:{q['team']}:{line['line']}", t, line["p"])
        period = (card.get("clock") or "").split(" ")[0] or None
        score = card.get("score") or {}
        score = (score.get("away"), score.get("home"))
        if card.get("status") == "in" and period and period != g["_period"]:
            g["marks"].append([t, period])
            self.dirty.add(g["event"])
        if g["_score"] is not None and score != g["_score"] and None not in score:
            g["marks"].append([t, f"{g['away']} {score[0]} – {g['home']} {score[1]}"])
            self.dirty.add(g["event"])
        g["_period"], g["_score"] = period or g["_period"], score

    def add_prices(self, t, quotes: dict) -> None:
        """kalshi.json at t (kalshi.live_prices): every game's markets."""
        t = _t(t)
        for event, teams in quotes.items():
            g = self.games.get(event)
            if g is None:  # only games with a published state (it names the teams)
                continue
            for team, rows in teams.items():
                if team == "scores":
                    for m in rows:
                        if m.get("part") == "game" and m.get("kind") == "win" and m.get("team") == g["home"]:
                            self._add(g, "kalshi", "win", t, _mid(m))
                    continue
                for m in rows:
                    for line, q in (m.get("lines") or {}).items():
                        self._add(g, "kalshi", f"qb:{team}:{int(float(line))}", t, _mid(q))

    def from_history(self, records: list[dict]) -> None:
        """Replay a worker history file's records (worker.read_history), oldest first."""
        for r in records:
            if r["kind"] == "state":
                self.add_state(r["t"], r["card"])
            elif r["kind"] == "kalshi":  # a partial record lists only the games whose prices changed: enough here
                self.add_prices(r["t"], r["prices"])
        self.dirty = set(self.games)

    def json(self, event: str) -> dict:
        return {k: v for k, v in self.games[event].items() if not k.startswith("_")}
