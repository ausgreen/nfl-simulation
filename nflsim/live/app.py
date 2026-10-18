"""Live game companion: the current state of a game (ESPN feed, correctable by hand) and the 5 most likely states
after the next snap, from the team game simulator (nflsim/sim/), plus each starter's yardage outlook.

    uv run nflsim live serve            # http://localhost:8765

Also the most likely ways the drive in progress ends (the receiving team's drive if a kickoff is next).
Overtime is simulated (sim/engine.py); in overtime, whether each team has had its possession is inferred from the
score (a team ahead has had it), so a tied overtime is treated as if neither had.

Stdlib HTTP server, local only. Endpoints: / (page), /api/games, /api/state?event=<id>[&overrides], /api/sig?event=<id>
(the game's play and box-score signature, no simulation; the page re-requests the state when it changes),
/api/kalshi (Kalshi's passing-yard prices for this week's games, kalshi.live_prices; re-fetched after 30 s).
Overrides (any subset): pos=home|away, down, togo, yl (yards to the goal), clock (game seconds left), margin
(home - away), kick=1 (a kickoff is next; pos = receiving team), yds_home, yds_away.
"""

from __future__ import annotations

import hashlib
import json
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
LINES = list(range(100, 401, 25))  # the page shows 150-350, plus any other line Kalshi lists
N_NEXT, N_DRIVE, N_GAME = 20000, 10000, 4000


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

    def setup(self, season: int) -> PG.Setup:
        if season not in self.setups:
            self.setups[season] = PG.season_setup(self.ds, season)
        return self.setups[season]

    def preds(self, season: int, week: int) -> pd.DataFrame:
        """v3's pregame expected yards for the week (cached on disk; one per team: the roster's starter)."""
        key = (season, week)
        if key not in self.week_preds:
            path = self.ds.data_dir / "live" / f"live_v3_{season}_w{week:02d}.parquet"
            if path.exists():
                p = pd.read_parquet(path)
            else:
                from ..v3.model import predict_week

                p = predict_week(self.ds, season, week)
                roster = set(self.ds.roster.player_id)
                p = p.assign(on_roster=p.player_id.isin(roster)).sort_values("on_roster", ascending=False)
                p = p.drop_duplicates("team")[["game_id", "team", "player_id", "qb", "mu", "qb_starts"]]
                path.parent.mkdir(parents=True, exist_ok=True)
                p.to_parquet(path, index=False)
            self.week_preds[key] = p.set_index("team")
        return self.week_preds[key]

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
    def _starter_yards(passers: list, name: str | None) -> tuple[float, bool, str | None]:
        """(yards, out, name shown): the expected starter's yards; out if another QB has taken over."""
        if not passers:
            return 0.0, False, name
        last = (name or "").split()[-1].lower() if name else ""
        mine = [r for r in passers if last and r[0].split()[-1].lower() == last]
        if not mine:  # starter not in the box score: follow the leading passer
            lead = passers[0]
            return lead[3], False, lead[0]
        others = [r for r in passers if r is not mine[0]]
        out = bool(others) and others[0][2] >= 5 and others[0][2] > mine[0][2] / 2
        return mine[0][3], out, mine[0][0]

    def compute(self, event: str, ov: dict) -> dict:
        """compute_state from the live feed; re-simulates only when the play, the box score or the corrections
        changed."""
        s = espn.game_state(event)
        sig, key = (espn.signature(s), espn.box(s)), tuple(sorted(ov.items()))
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
        y0, out0, n0 = self._starter_yards(s["passers"].get(home, []), qbs[home][0])
        y1, out1, n1 = self._starter_yards(s["passers"].get(away, []), qbs[away][0])
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
                       "lines": [{"line": L, "p": float(np.mean(fy >= L))} for L in LINES]})
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
                "line": {"spread": _f(an.get("spread")), "total": _f(an.get("total"))},
                "raw": {k2: (_f(v) if isinstance(v, (float, np.floating)) else (bool(v) if isinstance(v, (bool, np.bool_)) else v))
                        for k2, v in st.items()}}


def _f(v):
    try:
        v = float(v)
        return None if np.isnan(v) else v
    except (TypeError, ValueError):
        return None


def serve(port: int = 8765, ds: Dataset | None = None) -> None:
    model = LiveModel(ds or load())
    prices = {"at": 0.0, "data": {}}

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
                    self._send(200, TEMPLATE.read_bytes(), "text/html; charset=utf-8")
                elif u.path == "/api/games":
                    self._send(200, espn.games())
                elif u.path == "/api/state":
                    ev = q.pop("event", None)
                    if not ev:
                        return self._send(400, {"error": "event required"})
                    self._send(200, model.compute(ev, q))
                elif u.path == "/api/kalshi":
                    if time.monotonic() - prices["at"] > 30:
                        prices.update(at=time.monotonic(), data=kalshi.live_prices(espn.games()))
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
