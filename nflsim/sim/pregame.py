"""Season set-up and per-game views for the team game simulator (used by the dashboard, the CLI and the experiments).

season_setup(season): tables from the 5 seasons before `season`, the yards->multiplier curve, team ratings through
the latest play-by-play, form/efficiency noise from the training seasons, and the league sensitivities. Cached in
data/live/sim_setup_<season>.pkl, keyed by the simulator's code and the play-by-play files' modification times.
"""

from __future__ import annotations

import hashlib
import os
import pickle
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from ..data import Dataset
from ..model.features import starter_games
from . import data as D, engine as E, tables as T, teams as TM

WINDOW = 5
LINES = list(range(125, 401, 25))


@dataclass
class Setup:
    season: int
    tb: T.Tables
    curve: dict                  # gain multiplier -> mean yards from kickoff
    ratings: pd.DataFrame        # per (game_id, team) before that game, plus game_id "next" = after the latest game
    noise: dict
    jac: np.ndarray

    def to_mult(self, mu):
        g, m = list(self.curve), list(self.curve.values())
        return np.interp(np.asarray(mu, float), m, g)


def starters_map(ds: Dataset) -> dict:
    sg = starter_games(ds)
    return {(g, t): pid for g, t, pid in zip(sg.game_id, sg.team, sg.player_id)}


def implied(sched: pd.DataFrame) -> pd.DataFrame:
    """Implied points per game from the over/under and spread (spread_line > 0: home favored)."""
    s = sched[sched.game_type == "REG"]
    return pd.DataFrame({"game_id": s.game_id.to_numpy(), "imp0": ((s.total_line + s.spread_line) / 2).to_numpy(),
                         "imp1": ((s.total_line - s.spread_line) / 2).to_numpy(), "spread_line": s.spread_line.to_numpy(),
                         "total_line": s.total_line.to_numpy(), "home_score": s.home_score.to_numpy(),
                         "away_score": s.away_score.to_numpy()})


def _noise_inputs(ds: Dataset, season: int, sched: pd.DataFrame):
    """Training-season starts (final yards vs pregame expectation) and team points vs implied points."""
    path = ds.data_dir / "live" / "wf_rows.parquet"
    starts = None
    if path.exists():
        wf = pd.read_parquet(path, columns=["game_id", "player_id", "season", "final_yards", "pregame_mu"])
        starts = wf[wf.season.between(season - WINDOW, season - 1)].drop_duplicates(["game_id", "player_id"])
    tr = sched[sched.season.between(season - WINDOW, season - 1) & (sched.game_type == "REG")]
    tr = tr.dropna(subset=["home_score", "total_line", "spread_line"])
    tg = pd.concat([pd.DataFrame({"points": tr.home_score, "implied": (tr.total_line + tr.spread_line) / 2}),
                    pd.DataFrame({"points": tr.away_score, "implied": (tr.total_line - tr.spread_line) / 2})])
    return starts, tg


def season_setup(ds: Dataset, season: int, starters: dict | None = None, cache: bool = True,
                 log=print) -> Setup:
    pbp = [D.sim_pbp_dir(ds) / f"pbp_{s}.parquet" for s in range(season - WINDOW, season + 1)]
    pbp = [p for p in pbp if p.exists()]
    code = hashlib.md5(b"".join((Path(__file__).parent / m).read_bytes()
                                for m in ("engine.py", "tables.py", "teams.py", "pregame.py"))).hexdigest()
    key = (code, tuple((p.name, p.stat().st_mtime) for p in pbp))
    path = ds.data_dir / "live" / f"sim_setup_{season}.pkl"
    if cache and path.exists():
        with open(path, "rb") as f:
            k, setup = pickle.load(f)
        if k == key:
            return setup
    log(f"sim: fitting tables and ratings for {season} ...")
    starters = starters or starters_map(ds)
    tb = T.fit(D.load(ds, range(season - WINDOW, season)), starters)
    _, curve = E.calibrate(tb)
    curve = {float(k): float(v) for k, v in curve.items()}
    seasons = [s for s in (season - 1, season) if (D.sim_pbp_dir(ds) / f"pbp_{s}.parquet").exists()]
    prep = T.prepare(D.load(ds, seasons))
    rt = TM.ratings(TM.play_residuals(tb, prep), TM.league_rates(tb, prep), upcoming=season)
    jac = TM.efficiency_slope(tb)
    sched = pd.read_csv(ds.data_dir / "nflverse" / "schedules.csv")
    starts, tg = _noise_inputs(ds, season, sched)
    setup = Setup(season, tb, curve, rt, {}, jac)
    if starts is not None and len(starts):
        setup.noise = TM.noise_scales(tb, setup.to_mult, starts, tg, jac)
    else:  # no walk-forward expectations on disk: typical values from reports 21 (2023-26)
        setup.noise = {"tau2": 0.10, "form_sd": 0.18, "delta_sd": 0.0}
    if cache:  # written whole then renamed: the worker's simulation processes may be reading it
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        with open(tmp, "wb") as f:
            pickle.dump((key, setup), f)
        os.replace(tmp, path)
    return setup


def game_params(setup: Setup, games: pd.DataFrame, n: int = 500) -> pd.DataFrame:
    """Anchored engine parameters per game. games: game_id, home, away, mu0, mu1 (home/away QB expected yards),
    imp0, imp1 (implied points; NaN = no line). Games not yet played use each team's latest ('next') rating."""
    rt = setup.ratings
    played = set(rt.game_id)
    lookup = games[["game_id", "home", "away"]].copy()
    rows = []
    for g in lookup.itertuples():
        for team in (g.home, g.away):
            src = rt[(rt.game_id == g.game_id) & (rt.team == team)] if g.game_id in played else \
                rt[(rt.game_id == "next") & (rt.team == team)]
            if len(src):
                rows.append(src.iloc[[0]].assign(game_id=g.game_id))
    rr = pd.concat(rows, ignore_index=True) if rows else rt.iloc[:0]
    params = TM.team_params(lookup, rr)
    params = params.merge(games[["game_id", "mu0", "mu1", "imp0", "imp1"]], on="game_id")
    return TM.anchor(setup.tb, params, setup.to_mult, setup.jac, setup.noise, n=n)


def kickoff_frame(an: pd.DataFrame) -> pd.DataFrame:
    """Two kickoff states per game (each team receiving first), with engine columns and prior form draws."""
    ko = TM.kickoff_rows(an)
    ko["form_mean0"] = ko["form_mean1"] = 0.0
    return ko


def pregame(setup: Setup, an: pd.DataFrame, n: int = 5000, seed: int = 0, lines=LINES) -> list[dict]:
    """Simulated distributions from kickoff for each anchored game: scores, win/cover chances, QB line probabilities."""
    y, pt = E.simulate(setup.tb, kickoff_frame(an), n, seed=seed, points=True)
    g = len(an)
    out = []
    for i, r in enumerate(an.itertuples()):
        yy = np.concatenate([y[i], y[g + i]])
        pp = np.concatenate([pt[i], pt[g + i]])
        margin = pp[:, 0] - pp[:, 1]
        spread = getattr(r, "spread_line", np.nan)
        rec = {"gameId": r.game_id, "simHome": float(np.median(pp[:, 0])), "simAway": float(np.median(pp[:, 1])),
               "meanHome": float(pp[:, 0].mean()), "meanAway": float(pp[:, 1].mean()),
               "homeWin": float(np.mean(margin > 0) + 0.5 * np.mean(margin == 0)),
               "homeCover": None if pd.isna(spread) else float(np.mean(margin > spread)),
               "qbs": []}
        for k in (0, 1):
            q = yy[:, k]
            rec["qbs"].append({"median": float(np.median(q)), "p10": float(np.percentile(q, 10)),
                               "p90": float(np.percentile(q, 90)), "mean": float(q.mean()),
                               "probs": {str(L): float(np.mean(q >= L)) for L in lines}})
        out.append(rec)
    return out


def replay(setup: Setup, an: pd.DataFrame, states: pd.DataFrame, n: int = 5000, seed: int = 1) -> pd.DataFrame:
    """In-game probabilities from game states (nflsim/sim/states.py rows, with starter0/1 and game_id)."""
    mu = an.set_index("game_id")[["mu0", "mu1"]]
    cols = [f"{c}{k}" for c in list(TM.METRICS) + ["delta", "mult"] for k in (0, 1)] + ["delta_sd"]
    x = states.drop(columns=[c for c in cols if c in states.columns]).merge(an[["game_id"] + cols], on="game_id")
    for k in (0, 1):
        fm, fs = TM.form_posterior(x[f"yds{k}"], x.clock, mu.loc[x.game_id, f"mu{k}"].to_numpy(), setup.noise)
        x[f"form_mean{k}"], x[f"form_sd{k}"] = fm, fs
    y, pt = E.simulate(setup.tb, x, n, seed=seed, points=True)
    x["home_win"] = np.mean(x.margin.to_numpy()[:, None] + pt[..., 0] - pt[..., 1] > 0, axis=1)
    for k in (0, 1):
        x[f"median{k}"] = np.median(y[..., k], axis=1)
        for L in LINES:
            x[f"p{k}_{L}"] = np.mean(y[..., k] >= L, axis=1)
    return x


def _expected_yards(ds: Dataset, game_id: str, pids) -> dict:
    """Pregame expected yards per starter: walk-forward v3 rows if on disk, else v3 refit for that week."""
    path = ds.data_dir / "live" / "wf_rows.parquet"
    out = {}
    if path.exists():
        wf = pd.read_parquet(path, columns=["game_id", "player_id", "pregame_mu"])
        wf = wf[wf.game_id == game_id].drop_duplicates("player_id").set_index("player_id").pregame_mu
        out = {p: float(wf[p]) for p in pids if p in wf.index}
    if len(out) < len(pids):
        from ..v3.model import predict_week

        season, week = int(game_id[:4]), int(game_id[5:7])
        p3 = predict_week(ds, season, week).set_index("player_id")
        out.update({p: float(p3.loc[p, "mu"]) for p in pids if p not in out and p in p3.index})
    return out


def replay_game(ds: Dataset, game_id: str, n: int = 5000) -> tuple[dict, pd.DataFrame]:
    """Pregame view and in-game probabilities at each score change, quarter break and the last minutes."""
    from . import states as S

    season = int(game_id[:4])
    starters = starters_map(ds)
    setup = season_setup(ds, season, starters)
    g = D.load(ds, [season])
    g = g[g.game_id == game_id]
    if g.empty:
        raise SystemExit(f"{game_id}: not in data/nflverse/pbp_sim (try `nflsim sim fetch`)")
    home, away = g.home_team.iloc[0], g.away_team.iloc[0]
    s0, s1 = starters.get((game_id, home)), starters.get((game_id, away))
    st = S.game_states(g, s0, s1).assign(game_id=game_id, starter0=s0, starter1=s1)
    mu = _expected_yards(ds, game_id, [s0, s1])
    sched = implied(pd.read_csv(ds.data_dir / "nflverse" / "schedules.csv"))
    line = sched[sched.game_id == game_id].iloc[0]
    games = pd.DataFrame({"game_id": [game_id], "home": [home], "away": [away], "mu0": [mu.get(s0, np.nan)],
                          "mu1": [mu.get(s1, np.nan)], "imp0": [line.imp0], "imp1": [line.imp1]})
    an = game_params(setup, games, n=2000).assign(spread_line=line.spread_line)
    pre = pregame(setup, an, n=n)[0]
    pre.update(home=home, away=away, mu0=mu.get(s0), mu1=mu.get(s1), spread=line.spread_line, total=line.total_line)
    secs = st.game_secs_row.to_numpy()
    pick = set(np.flatnonzero(np.diff(st.margin.to_numpy()) != 0) + 1)
    for t in (2700, 1800, 900, 600, 300, 180, 120, 60):
        i = np.flatnonzero(secs >= t)
        if len(i):
            pick.add(int(i[-1]))
    x = replay(setup, an, st.loc[sorted(pick)], n=n)
    yds = st[["yds0", "yds1"]].iloc[-1]
    pre["final_yds"] = [float(yds.yds0), float(yds.yds1)]
    pre["final_margin"] = float(st.margin.iloc[-1])
    return pre, x
