"""Live game companion: the current state of a game (ESPN feed, correctable by hand) and the 5 most likely states
after the next snap, from the team game simulator (nflsim/sim/), plus each starter's yardage outlook.

    uv run nflsim live serve            # http://localhost:8765

Also the most likely ways the drive in progress ends (the receiving team's drive if a kickoff is next).
Overtime is simulated (sim/engine.py); in overtime, whether each team has had its possession is inferred from the
score (a team ahead has had it), so a tied overtime is treated as if neither had.

Stdlib HTTP server, local only. Endpoints: / (every game at a glance, `hosted/index.html`), /game?event=<id>
(one game's page), /edges (model vs Kalshi, `hosted/edges.html`), /api/games, /api/state?event=<id>[&overrides], /api/sig?event=<id>
(the game's play and box-score signature, no simulation; the page re-requests the state when it changes),
/api/summary (the home page's cards: `summary` of each game's state), /api/kalshi (Kalshi's prices for this week's
games, kalshi.live_prices: from Kalshi's WebSocket when a key is set up, kalshi_ws.price_source, re-read after 1 s; else
polled after 5 s).
Overrides (any subset): pos=home|away, down, togo, yl (yards to the goal), clock (game seconds left), margin
(home - away), kick=1 (a kickoff is next; pos = receiving team), yds_home, yds_away.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np
import pandas as pd

from .. import kalshi
from ..data import Dataset, load
from ..sim import engine as E, pregame as PG, teams as TM
from . import espn

TEMPLATE = Path(__file__).with_name("live_template.html")
HOME = Path(__file__).parent.parent / "hosted" / "index.html"  # every game at a glance
EDGES = HOME.with_name("edges.html")  # the model vs Kalshi's prices, largest gaps first
WATCH = HOME.with_name("watch.html")  # the viewer's watched lines and positions (kept in their browser)
LINES = list(range(100, 401, 25))  # the page shows 150-350, plus any other line Kalshi lists
N_NEXT, N_DRIVE, N_GAME = 20000, 10000, 4000
# each line's range: the chance with the QB's form fixed at the 10th and 90th percentile of its (in-game) posterior
Z_RANGE = 1.2816


def field(yl: float, offense: str, defense: str) -> str:
    if yl is None or np.isnan(yl):
        return ""
    yl = int(round(yl))
    return "50" if yl == 50 else (f"{offense} {100 - yl}" if yl > 50 else f"{defense} {yl}")


def ordinal(d: int) -> str:
    return {1: "1st", 2: "2nd", 3: "3rd", 4: "4th"}.get(int(d), f"{d}th")


# yards-gained buckets for the next snap: (label, highest gain in the bucket)
RUN_BUCKETS = [("for a loss", -1), ("0–3 yds", 3), ("4–7 yds", 7), ("8–14 yds", 14), ("15+ yds", 99)]
COMP_BUCKETS = [("≤5 yds", 5), ("6–10 yds", 10), ("11–19 yds", 19), ("20–39 yds", 39), ("40+ yds", 99)]
N_BUCKETS = 8


def snap_buckets(event: np.ndarray, gain: np.ndarray) -> np.ndarray:
    """One label per simulated snap: runs and completions by yards gained, everything else by its event."""
    names = np.array([e[0].upper() + e[1:] for e in E.EVENTS] + ["No snap"], dtype=object)
    out = names[np.where(event >= 0, event, len(E.EVENTS))]
    for ev, label, bins in (("run", "Run", RUN_BUCKETS), ("completion", "Completion", COMP_BUCKETS)):
        m = event == E.EVENTS.index(ev)
        i = np.searchsorted([b for _, b in bins], gain[m], side="left").clip(0, len(bins) - 1)
        out[m] = [f"{label} {bins[j][0]}" for j in i]
    return out


def sig_id(s: dict) -> str:
    """Short id of a game_state()'s signature and box score: it changes with every play, and when the box score
    catches up with one."""
    return hashlib.sha1(repr((espn.signature(s), espn.box(s))).encode()).hexdigest()[:12]


def quarter_rows(s: dict, clock: float, qpts: np.ndarray | None) -> list[dict]:
    """Points scored in each regulation quarter (home, away): the actual points for quarters that are over (ESPN's
    line scores), else the points so far plus the simulated rest (`qpts`: (n, 4, 2)), with who wins the quarter."""
    ls = s.get("linescores") or [[], []]

    def actual(t, i):
        return ls[t][i] if i < len(ls[t]) else 0.0
    cur = {"pre": 0, "post": 5}.get(s["status"], min(4, int((3600 - clock) // 900) + 1))
    rows = []
    for i in range(4):
        if i + 1 < cur or qpts is None:
            h, a = actual(0, i), actual(1, i)
            rows.append({"q": i + 1, "state": "done", "home": h, "away": a, "homeWin": None, "awayWin": None,
                         "tie": None})
            continue
        so_far = (actual(0, i), actual(1, i)) if i + 1 == cur else (0.0, 0.0)
        h, a = so_far[0] + qpts[:, i, 0], so_far[1] + qpts[:, i, 1]
        rows.append({"q": i + 1, "state": "live" if i + 1 == cur else "next", "home": float(h.mean()),
                     "away": float(a.mean()), "homeWin": float(np.mean(h > a)), "awayWin": float(np.mean(a > h)),
                     "tie": float(np.mean(h == a))})
    return rows


def _pmf(v: np.ndarray) -> list:
    """[lowest value, how many simulated games ended on each whole number from there up, games simulated] of
    simulated points (or margins); the chance of a value is its count / the last number."""
    v = np.rint(v).astype(int)
    lo = int(v.min())
    return [lo, [int(c) for c in np.bincount(v - lo)], len(v)]


def score_chances(s: dict, clock: float, pts: np.ndarray, qpts: np.ndarray | None, bands: dict | None = None) -> dict:
    """The simulated final points of each part of the game, for pricing Kalshi's score markets (kalshi.SCORE_SERIES):
    {part: {"margin" (home − away), "total"[, "home", "away"]: _pmf, "now": [home, away] points so far}} for the game,
    each regulation quarter (1Q..4Q) and half; the 2nd half and the game count overtime. `pts`: (n, 2) points still to
    come; `qpts`: (n, 4, 2), per quarter (None: no simulated quarter is left, as in overtime or after the game).
    `bands`: {"low", "high", "home_up", "home_down": (pts, qpts)}, the same with both QBs' form at its low / high end
    and with the home QB's high and the away QB's low (or the reverse); they add, per part, the likely range of each
    chance: "margin_band" [home_down, home_up], "total_band" [low, high] (and for the game "home_band", "away_band")."""
    ls = s.get("linescores") or [[], []]
    score = np.array([s["home_score"], s["away_score"]], dtype=float)

    def parts_of(pts, qpts):
        q = np.zeros((len(pts), 4, 2))
        for t in (0, 1):
            q[:, :len(ls[t][:4]), t] = ls[t][:4]
        if qpts is not None:
            cur = {"pre": 0}.get(s["status"], min(4, int((3600 - clock) // 900) + 1))
            for i in range(max(cur, 1) - 1, 4):
                q[:, i] = (q[:, i] if i + 1 == cur else 0.0) + qpts[:, i]
        final = score + pts
        first = q[:, :2].sum(1)
        return {"game": final, "1H": first, "2H": final - first, **{f"{i + 1}Q": q[:, i] for i in range(4)}}

    parts = parts_of(pts, qpts)
    done = np.zeros((4, 2))  # points so far in each quarter (the current one included)
    for t in (0, 1):
        done[:len(ls[t][:4]), t] = ls[t][:4]
    now = {"game": score, "1H": done[:2].sum(0), "2H": score - done[:2].sum(0), **{f"{i + 1}Q": done[i] for i in range(4)}}
    out = {}
    for k, v in parts.items():
        out[k] = {"margin": _pmf(v[:, 0] - v[:, 1]), "total": _pmf(v.sum(1)), "now": [float(x) for x in now[k]]}
    out["game"].update(home=_pmf(parts["game"][:, 0]), away=_pmf(parts["game"][:, 1]))
    if bands:
        b = {name: parts_of(*pq) for name, pq in bands.items()}
        for k in parts:
            out[k]["margin_band"] = [_pmf(b[n][k][:, 0] - b[n][k][:, 1]) for n in ("home_down", "home_up")]
            out[k]["total_band"] = [_pmf(b[n][k].sum(1)) for n in ("low", "high")]
        for side, j in (("home", 0), ("away", 1)):
            out["game"][f"{side}_band"] = [_pmf(b[n]["game"][:, j]) for n in ("low", "high")]
    return out


def clock_text(clock: float, s: dict | None = None) -> str:
    if s and s.get("overtime") and s["status"] == "in":
        return "End of regulation" if s["period"] == 4 else f"OT {s['display_clock'] or ''}".strip()
    if clock <= 0:
        return "Final"
    q = min(4, int(1 + (3600 - clock) // 900)) if clock < 3600 else 1
    left = clock - (4 - q) * 900
    return f"Q{q} {int(left) // 60}:{int(left) % 60:02d}"


class LiveModel:
    def __init__(self, ds: Dataset):
        self.ds = ds
        self.lock = threading.Lock()
        self.setups: dict[int, PG.Setup] = {}
        self.week_preds: dict[tuple, pd.DataFrame] = {}
        self.params: dict[str, pd.Series] = {}
        self.last: dict[str, tuple] = {}  # event -> (signature, corrections, output) of the last compute
        self._computing: dict[str, threading.Lock] = {}  # event -> held while it's being computed

    def setup(self, season: int) -> PG.Setup:
        if season not in self.setups:
            self.setups[season] = PG.season_setup(self.ds, season)
        return self.setups[season]

    def _preds_path(self, season: int, week: int) -> Path:
        return self.ds.data_dir / "live" / f"live_v3_{season}_w{week:02d}.parquet"

    def compute_preds(self, season: int, week: int) -> pd.DataFrame:
        """v3's pregame expected yards for the week from the current data, one per team (the roster's starter).
        Slow (~15 s: weather forecasts, form); doesn't touch the cache, so it can run in another thread."""
        from ..v3.model import predict_week

        p = predict_week(self.ds, season, week)
        roster = set(self.ds.roster.player_id)
        p = p.assign(on_roster=p.player_id.isin(roster)).sort_values("on_roster", ascending=False)
        return p.drop_duplicates("team")[["game_id", "team", "player_id", "qb", "mu", "qb_starts"]]

    def preds(self, season: int, week: int) -> pd.DataFrame:
        """The week's pregame expected yards (cached on disk; computed the first time; `update_preds` refreshes)."""
        key = (season, week)
        if key not in self.week_preds:
            path = self._preds_path(season, week)
            if path.exists():
                p = pd.read_parquet(path)
            else:
                p = self.compute_preds(season, week)
                _write_parquet(p, path)
            self.week_preds[key] = p.set_index("team")
        return self.week_preds[key]

    def update_preds(self, season: int, week: int, fresh: pd.DataFrame, frozen: set[str]) -> dict[str, tuple]:
        """Replace the week's pregame expectations with `fresh` (compute_preds), except the teams in `frozen` (games
        under way or over keep the numbers they started with). Games whose numbers changed are re-anchored on their
        next computation. Returns {team: (old mu, new mu)} for the teams that changed."""
        with self.lock:
            old = self.preds(season, week)
            new = fresh.set_index("team")
            keep = old[old.index.isin(frozen) | ~old.index.isin(new.index)]
            merged = pd.concat([keep, new[~new.index.isin(keep.index)]])
            changed = {t: (float(old.loc[t, "mu"]) if t in old.index else None, float(merged.loc[t, "mu"]))
                       for t in merged.index if t not in keep.index
                       and (t not in old.index or abs(old.loc[t, "mu"] - merged.loc[t, "mu"]) > 0.05
                            or old.loc[t, "player_id"] != merged.loc[t, "player_id"])}
            _write_parquet(merged.reset_index(), self._preds_path(season, week))
            self.week_preds[(season, week)] = merged
            for gid in {merged.loc[t, "game_id"] for t in changed}:
                self.params.pop(gid, None)
            return changed

    def reset(self) -> None:
        """Forget the season set-ups and anchored games (after a data refresh); rebuilt on next use."""
        with self.lock:
            self.setups.clear()
            self.params.clear()
            self.last.clear()

    def game_params(self, s: dict) -> tuple[pd.Series, dict]:
        """Anchored simulator parameters for the game (cached), and the expected starters."""
        season, week, home, away = s["season"], s["week"], s["home"], s["away"]
        gid = f"{season}_{week:02d}_{away}_{home}"
        p = self.preds(season, week)
        qbs = {t: (p.loc[t, "qb"], p.loc[t, "player_id"], float(p.loc[t, "mu"])) if t in p.index else (None, None, 230.0)
               for t in (home, away)}
        if gid not in self.params:
            su = self.setup(season)
            sched = PG.implied(pd.read_csv(self.ds.data_dir / "nflverse" / "schedules.csv"))
            line = sched[sched.game_id == gid]
            imp0, imp1 = (float(line.imp0.iloc[0]), float(line.imp1.iloc[0])) if len(line) else (np.nan, np.nan)
            games = pd.DataFrame({"game_id": [gid], "home": [home], "away": [away], "mu0": [qbs[home][2]],
                                  "mu1": [qbs[away][2]], "imp0": [imp0], "imp1": [imp1]})
            an = PG.game_params(su, games, n=1500)
            an = an.assign(spread=line.spread_line.iloc[0] if len(line) else np.nan,
                           total=line.total_line.iloc[0] if len(line) else np.nan)
            self.params[gid] = an.iloc[0]
        return self.params[gid], qbs

    @staticmethod
    def _starter_yards(passers: list, name: str | None, latest: str | None = None) -> tuple[float, bool, str | None]:
        """(yards, out, name shown): the expected starter's yards; out if another QB has taken over — he threw the
        team's latest pass (`latest`, espn.last_passers: "K.Pickett"), or has 5+ attempts and more than half the
        starter's while the starter didn't throw the latest one."""
        if not passers:
            return 0.0, False, name
        last = (name or "").split()[-1].lower() if name else ""
        mine = [r for r in passers if last and r[0].split()[-1].lower() == last]
        if not mine:  # starter not in the box score: follow the leading passer
            lead = passers[0]
            return lead[3], False, lead[0]

        def threw_latest(full: str) -> bool:  # "Kenny Pickett" vs "K.Pickett"
            if not latest or "." not in latest:
                return False
            initial, surname = latest.split(".", 1)
            return full.split()[-1].lower() == surname.lower() and full[:1].upper() == initial[:1].upper()
        others = [r for r in passers if r is not mine[0]]
        if threw_latest(mine[0][0]):
            return mine[0][3], False, mine[0][0]
        out = any(threw_latest(r[0]) for r in others) or (
            bool(others) and others[0][2] >= 5 and others[0][2] > mine[0][2] / 2)
        return mine[0][3], out, mine[0][0]

    def compute(self, event: str, ov: dict) -> dict:
        """compute_state from the live feed; re-simulates only when the play, the box score or the corrections
        changed."""
        s = espn.game_state(event)
        sig, key = (espn.signature(s), espn.box(s)), tuple(sorted(ov.items()))
        with self.lock:
            busy = self._computing.setdefault(event, threading.Lock())
        with busy:  # pages asking for the same game at once share one simulation
            hit = self.last.get(event)
            if hit and hit[:2] == (sig, key):
                return hit[2]
            out = self.compute_state(s, ov)
            self.last[event] = (sig, key, out)
            return out

    def compute_state(self, s: dict, ov: dict | None = None) -> dict:
        """Everything the page shows, from an espn.game_state() dict and optional corrections (`ov`, the
        query-string overrides). Deterministic: the same state and corrections give the same output."""
        event = s["event"]
        ov = ov or {}
        with self.lock:
            an, qbs = self.game_params(s)
            su = self.setup(s["season"])
        home, away = s["home"], s["away"]
        latest = s.get("last_passer") or {}
        y0, out0, n0 = self._starter_yards(s["passers"].get(home, []), qbs[home][0], latest.get(home))
        y1, out1, n1 = self._starter_yards(s["passers"].get(away, []), qbs[away][0], latest.get(away))
        to = [3.0 if v is None else float(v) for v in (s.get("timeouts") or [None, None])]
        st = {"clock": s["clock"], "margin": s["margin"], "pos": s["pos"], "kick": s["kick"], "yl": s["yl"],
              "down": s["down"], "togo": s["togo"], "yds0": y0, "yds1": y1, "out0": out0, "out1": out1,
              "to0": to[0], "to1": to[1]}
        applied = {}
        for k, v in ov.items():
            if v in (None, ""):
                continue
            if k == "pos":
                st["pos"] = 0 if v in ("home", home, "0") else 1
            elif k == "kick":
                st["kick"] = v in ("1", "true", "yes")
            elif k in ("yds_home", "yds_away"):
                st["yds0" if k == "yds_home" else "yds1"] = float(v)
            elif k in ("down", "togo", "yl", "clock", "margin"):
                st[k] = float(v)
            else:
                continue
            applied[k] = v
        if "kick" in applied and not st["kick"] and (st["yl"] is None or np.isnan(st["yl"])):
            st["yl"] = 75.0
        ot = bool(s.get("overtime")) and s["status"] == "in"
        extra = {}
        if ot:
            post = bool(s.get("postseason"))
            if "clock" not in applied:  # seconds left in the overtime period (a tied regulation's end: all of it)
                st["clock"] = (900.0 if post else 600.0) if s["period"] == 4 else float(s.get("ot_clock") or 0.0)
            if st["pos"] is None:  # receiver not known yet: a coin toss in each simulated game
                st.update(pos=-1, kick=True)
            lead = 0 if st["margin"] > 0 else (1 if st["margin"] < 0 else None)
            extra = {"ot": True, "post": post, "ot_poss0": lead == 0, "ot_poss1": lead == 1}
        h2 = s["h2_receiver"]
        cols = [f"{c}{k}" for c in list(TM.METRICS) + ["delta", "mult"] for k in (0, 1)] + ["delta_sd"]
        row = {**st, **extra, "h2_receiver": h2, "pat": 0, **{c: float(an[c]) for c in cols}}
        for k, mu in ((0, an.mu0), (1, an.mu1)):
            fm, fs = TM.form_posterior([row[f"yds{k}"]], 0.0 if ot else row["clock"], [mu], su.noise)
            row[f"form_mean{k}"], row[f"form_sd{k}"] = float(np.ravel(fm)[0]), float(np.ravel(fs)[0])
        frame = pd.DataFrame([row])
        nxt = E.simulate(su.tb, frame, N_NEXT, seed=1, max_steps=1, record=True)
        full_y, full_p, full_q = E.simulate(su.tb, frame, N_GAME, seed=2, points=True, quarters=True)
        def form_at(z0, z1):  # each QB's form fixed at its posterior mean + z sd
            cols = {}
            for k, z in ((0, z0), (1, z1)):
                cols[f"form_mean{k}"], cols[f"form_sd{k}"] = row[f"form_mean{k}"] + z * row[f"form_sd{k}"], 0.0
            return frame.assign(**cols)
        z = Z_RANGE
        band = pd.concat([form_at(-z, -z), form_at(z, z), form_at(z, -z), form_at(-z, z)], ignore_index=True)
        # (4, n, 2): both QBs low, both high, home high + away low, home low + away high
        band_y, band_p, band_q = E.simulate(su.tb, band, N_GAME, seed=2, points=True, quarters=True)
        drv = E.simulate(su.tb, frame, N_DRIVE, seed=3, drive=True)
        teams = (home, away)

        def score_after(margin):
            return (f"{away} {int(round(s['away_score'] + max(0, -(margin - st['margin']))))}"
                    f" – {home} {int(round(s['home_score'] + max(0, margin - st['margin'])))}")
        # next-snap outcomes: play type and yards gained, bucketed
        k = pd.DataFrame({"pos": nxt["pos"][0], "down": nxt["down"][0], "togo": nxt["togo"][0],
                          "yl": np.round(nxt["yl"][0]), "kick": nxt["kick"][0], "margin": nxt["margin"][0],
                          "clock_end": nxt["done"][0], "event": nxt["event"][0], "gain": nxt["gain"][0],
                          "half": (st["clock"] > 1800) & (nxt["clock"][0] <= 1800), "on_downs": nxt["on_downs"][0]})
        k["bucket"] = snap_buckets(k.event.to_numpy(), k.gain.to_numpy())
        # the offense on this snap: the receiving team if a kickoff comes first
        k["first"] = k.event.isin([E.EVENTS.index("completion"), E.EVENTS.index("run")]) & (k.pos == st["pos"]) \
            & (k.down == 1) & ~k.kick
        top = []
        for bucket, g in sorted(k.groupby("bucket"), key=lambda kv: -len(kv[1])):
            if len(top) == N_BUCKETS or len(g) / N_NEXT < 0.01:
                break
            key = g.groupby(["pos", "down", "togo", "yl", "kick", "margin", "clock_end", "half"]).size().idxmax()
            pos, down, togo, yl, kick, margin, ended, half = key
            off, de = teams[int(pos)], teams[1 - int(pos)]
            if ended:
                situation = desc = "End of game"
            elif half:
                situation, desc = "halftime kickoff", f"Halftime · {off} receives"
            elif kick:
                situation, desc = "kickoff", f"Kickoff · {off} receives"
            else:
                situation = f"{ordinal(down)} & {'Goal' if togo >= yl else int(togo)} at {field(yl, off, de)}"
                desc = f"{off} ball · {situation}"
            gains = bucket.startswith(("Run", "Completion"))
            top.append({"prob": len(g) / N_NEXT, "outcome": bucket, "state": desc, "situation": situation,
                        "ball": None if ended else off, "kick": bool(kick or half), "change": bool(pos != st["pos"]),
                        "first": float(g["first"].mean()) if gains else None,
                        "onDowns": float(g.on_downs.mean()) if gains or bucket.startswith(("Incomplete", "Sack")) else None,
                        "score": score_after(margin) if margin != st["margin"] else None})
        mix = (pd.Series(nxt["event"][0]).map(lambda e: E.EVENTS[e] if e >= 0 else "no snap")
               .value_counts(normalize=True))
        qb = []
        for t, (name, y, out) in ((1, (n1, y1, out1)), (0, (n0, y0, out0))):
            fy = full_y[0, :, t]
            qb.append({"team": teams[t], "qb": name, "yds": y, "out": out, "mu": float(an[f"mu{t}"]),
                       "median": float(np.median(fy)), "p10": float(np.percentile(fy, 10)),
                       "p90": float(np.percentile(fy, 90)),
                       "lines": [{"line": L, "p": float(np.mean(fy >= L)), "lo": float(np.mean(band_y[0, :, t] >= L)),
                                  "hi": float(np.mean(band_y[1, :, t] >= L))} for L in LINES]})
        final_margin = st["margin"] + full_p[0, :, 0] - full_p[0, :, 1]
        home_win = float(np.mean(final_margin > 0) + 0.5 * np.mean(final_margin == 0))
        final = {"home": float(s["home_score"] + full_p[0, :, 0].mean()),
                 "away": float(s["away_score"] + full_p[0, :, 1].mean()), "homeWin": home_win, "awayWin": 1 - home_win}
        toss = st["pos"] < 0
        off, de = (None, None) if toss else (teams[int(st["pos"])], teams[1 - int(st["pos"])])
        # how the current drive ends, grouped by result (the receiving team's: before a coin toss, either team's)
        p = max(int(st["pos"]), 0)
        d = pd.DataFrame({"result": drv["result"][0], "plays": drv["plays"][0], "used": drv["used"][0],
                          "margin": drv["margin"][0], "net": drv["pts"][0, :, p] - drv["pts"][0, :, 1 - p]})
        d = d[d.result >= 0]
        outcomes = []
        for code, g in sorted(d.groupby("result"), key=lambda kv: -len(kv[1]))[:5]:
            margin = g.margin.mode().iloc[0]
            used = float(g.used.median())
            outcomes.append({"prob": len(g) / N_DRIVE, "result": E.DRIVE_RESULTS[int(code)],
                             "plays": float(g.plays.median()), "time": f"{int(used) // 60}:{int(used) % 60:02d}",
                             "score": score_after(margin) if margin != st["margin"] else None})
        drive = {"team": off, "kickoff": bool(st["kick"]), "outcomes": outcomes,
                 "expPts": float(d.net.sum() / N_DRIVE) if len(d) else None}
        situation = "kickoff" if st["kick"] else \
            f"{ordinal(st['down'])} & {'Goal' if st['togo'] >= st['yl'] else int(st['togo'])} at {field(st['yl'], off, de)}"
        now = f"Kickoff · {off} receives" if st["kick"] else f"{off} ball · {situation}"
        ball = {"team": off, "kick": bool(st["kick"]), "situation": situation}
        if toss:
            now, ball = "Overtime kickoff · coin toss next", None
        if s["status"] == "post":
            now, ball = "Final", None
        return {"event": event, "sig": sig_id(s), "home": home, "away": away, "status": s["status"], "detail": s["detail"],
                "clock": clock_text(st["clock"], s), "score": {"home": s["home_score"], "away": s["away_score"]},
                "margin": st["margin"], "now": now, "ball": ball, "lastPlay": s["last_play"], "feedTime": s["feed_time"],
                "fetchedAt": s["fetched_at"], "overrides": applied, "overtime": ot, "next": top, "drive": drive,
                "mix": [{"event": e, "prob": float(p)} for e, p in mix.head(6).items()],
                "qbs": qb, "homeWin": home_win, "final": final,
                "quarters": quarter_rows(s, st["clock"], None if s["status"] == "post" or ot else full_q[0]),
                "scores": score_chances(s, st["clock"], full_p[0], None if s["status"] == "post" or ot else full_q[0],
                                        {name: (band_p[i], None if s["status"] == "post" or ot else band_q[i])
                                         for i, name in enumerate(("low", "high", "home_up", "home_down"))}),
                "line": {"spread": _f(an.get("spread")), "total": _f(an.get("total"))},
                "raw": {k2: (_f(v) if isinstance(v, (float, np.floating)) else (bool(v) if isinstance(v, (bool, np.bool_)) else v))
                        for k2, v in st.items()}}


CARD = ("event", "home", "away", "status", "clock", "score", "now", "ball", "lastPlay", "homeWin", "final", "line",
        "overtime", "feedTime", "scores")


def summary(out: dict | None, game: dict) -> dict:
    """The home page's card for a game: the espn.games row, plus compute_state's headline numbers when there are any."""
    card = dict(game)
    if out:
        card.update({k: out[k] for k in CARD})
        card["qbs"] = [{k: q[k] for k in ("team", "qb", "yds", "out", "mu", "median", "p10", "p90", "lines")}
                       for q in out["qbs"]]
    return card


def _write_parquet(df: pd.DataFrame, path: Path) -> None:
    """Written whole then renamed: the worker's simulation processes may be reading it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    df.to_parquet(tmp, index=False)
    os.replace(tmp, path)


def _local(page: Path) -> bytes:
    """A hosted page (hosted/*.html) pointed at this server's endpoints and paths instead of the published files."""
    html = page.read_text()
    for published, here in (("summary.json", "/api/summary"), ("kalshi.json", "/api/kalshi"), ("game.html", "/game"),
                            ("edges.html", "/edges"), ("watch.html", "/watch"), ("./", "/")):
        html = html.replace(f'"{published}"', f'"{here}"')
    return html.encode()


def _f(v):
    try:
        v = float(v)
        return None if np.isnan(v) else v
    except (TypeError, ValueError):
        return None


def serve(port: int = 8765, ds: Dataset | None = None) -> None:
    model = LiveModel(ds or load())
    from .. import kalshi_ws

    source, book = kalshi_ws.price_source()
    prices = {"at": 0.0, "data": {}, "every": 1.0 if book else 5.0}

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body, default=_f).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[-1] for k, v in parse_qs(u.query).items()}
            try:
                if u.path == "/":
                    self._send(200, _local(HOME), "text/html; charset=utf-8")
                elif u.path == "/edges":
                    self._send(200, _local(EDGES), "text/html; charset=utf-8")
                elif u.path == "/game":
                    self._send(200, _local(TEMPLATE), "text/html; charset=utf-8")
                elif u.path == "/watch":
                    self._send(200, _local(WATCH), "text/html; charset=utf-8")
                elif u.path == "/api/games":
                    self._send(200, espn.games())
                elif u.path == "/api/state":
                    ev = q.pop("event", None)
                    if not ev:
                        return self._send(400, {"error": "event required"})
                    self._send(200, model.compute(ev, q))
                elif u.path == "/api/summary":
                    cards = []
                    for g in espn.games():
                        try:
                            cards.append(summary(model.compute(g["event"], {}), g))
                        except Exception:  # the card without the model's numbers
                            traceback.print_exc()
                            cards.append(summary(None, g))
                    self._send(200, cards)
                elif u.path == "/api/kalshi":
                    if time.monotonic() - prices["at"] > prices["every"]:
                        prices.update(at=time.monotonic(), data=source(espn.games()))
                    self._send(200, prices["data"])
                elif u.path == "/api/sig":
                    if not q.get("event"):
                        return self._send(400, {"error": "event required"})
                    s = espn.game_state(q["event"])  # scoreboard + summary: the box score can lag a play
                    self._send(200, {"sig": sig_id(s), "status": s["status"]})
                else:
                    self._send(404, {"error": "not found"})
            except Exception as e:  # report to the page instead of dropping the connection
                traceback.print_exc()
                self._send(500, {"error": f"{type(e).__name__}: {e}"})

        def log_message(self, fmt, *args):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Live game companion: http://localhost:{port}  (Ctrl-C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
