"""Season backtest of the simulator: pregame and in-game chances vs what happened, for one variant of the code.

usage: uv run python experiments/model_improve/backtest.py SEASON LABEL [--set NAME=VALUE ...] [--n N] [--limit G]

Each `--set` overrides a module constant before the season is set up (e.g. `--set tables.SCORE_EDGES=fine`,
`--set tables.SEASON_HALF_LIFE=2`), so one checkout runs every arm. The season's tables are fit on the 5 seasons
before it (out of sample), ratings come from that season's earlier games, and each game is anchored to v3's
walk-forward expected yards (data/live/analysis/model_improve/wf_base.parquet, the same numbers as wf_rows.parquet)
and the closing lines' implied points. Every arm uses the same seeds.

Writes data/live/analysis/model_improve/bt/<SEASON>_<LABEL>.parquet: one row per (game, checkpoint) with the
home win chance, the QB line chances, mean final margin and total, P(total > closing total), P(home covers), and
(pregame) P(regulation ends tied); plus the outcomes.
"""

from __future__ import annotations

import argparse
import importlib
import time
from pathlib import Path

import numpy as np
import pandas as pd

from nflsim.data import load
from nflsim.sim import data as D, engine as E, pregame as PG, states as SS

OUT = Path("data/live/analysis/model_improve/bt")
# code variants by label (T = nflsim.sim.tables, E = engine, TM = teams): exec'd before the season is set up
VARIANTS = {
    "fine": "",                                   # the current code (fine score bins are the default)
    "coarse": "T.set_score_bins(False)",          # the code before this branch ("base" in the first runs)
    "hl2": "T.set_score_bins(False); T.SEASON_HALF_LIFE = 2.0",   # recency weights, tried on the coarse bins
    "fine_hl2": "T.SEASON_HALF_LIFE = 2.0",
    "elt": "T.ELAPSED_BY_SCORE = True",
}
CHECK = (2700, 1800, 1200, 900, 600, 420, 300, 180, 120, 60)


def parse():
    ap = argparse.ArgumentParser()
    ap.add_argument("season", type=int)
    ap.add_argument("label")
    ap.add_argument("--set", action="append", default=[])
    ap.add_argument("--n", type=int, default=2000)
    ap.add_argument("--anchor-n", type=int, default=500)
    ap.add_argument("--limit", type=int)
    return ap.parse_args()


def apply_sets(sets):
    for s in sets:
        name, value = s.split("=", 1)
        mod, attr = name.rsplit(".", 1)
        m = importlib.import_module(f"nflsim.sim.{mod}")
        try:
            v = eval(value, {"np": np})
        except Exception:
            v = value
        setattr(m, attr, v)


def main():
    a = parse()
    from nflsim.sim import engine as E_, tables as T_, teams as TM_
    exec(VARIANTS.get(a.label.split("@")[0], ""), {"T": T_, "E": E_, "TM": TM_, "np": np})
    apply_sets(a.set)
    t0 = time.time()
    ds = load()
    starters = PG.starters_map(ds)
    su = PG.season_setup(ds, a.season, starters, cache=False, log=lambda *_: None)
    print(f"setup {time.time() - t0:.0f}s", flush=True)
    pbp = D.load(ds, [a.season])
    pbp = pbp[pbp.season_type == "REG"]
    wf = pd.read_parquet("data/live/analysis/model_improve/wf_base.parquet", columns=["game_id", "player_id", "mu"])
    mu = {(g, p): m for g, p, m in wf.itertuples(index=False)}
    sched = PG.implied(pd.read_csv(ds.data_dir / "nflverse" / "schedules.csv"))
    sched = sched[sched.game_id.str.startswith(str(a.season))].set_index("game_id")
    games = []
    for gid, g in pbp.groupby("game_id"):
        h, aw = g.home_team.iloc[0], g.away_team.iloc[0]
        s0, s1 = starters.get((gid, h)), starters.get((gid, aw))
        if s0 is None or s1 is None or (gid, s0) not in mu or (gid, s1) not in mu or gid not in sched.index:
            continue
        if pd.isna(sched.home_score[gid]):
            continue
        games.append(dict(game_id=gid, home=h, away=aw, s0=s0, s1=s1, mu0=mu[(gid, s0)], mu1=mu[(gid, s1)],
                          imp0=sched.imp0[gid], imp1=sched.imp1[gid]))
    games = pd.DataFrame(games)
    if a.limit:
        games = games.head(a.limit)
    an = PG.game_params(su, games, n=a.anchor_n)
    an = an.merge(sched[["spread_line", "total_line"]], left_on="game_id", right_index=True)
    print(f"{len(games)} games anchored {time.time() - t0:.0f}s", flush=True)

    # pregame: both kickoff orders, regulation only for the tie rate, then with overtime
    ko = PG.kickoff_frame(an)
    g = len(an)
    _, pt_reg = E.simulate(su.tb, ko, a.n // 2, seed=3, points=True, overtime=False)
    tie = (np.concatenate([pt_reg[:g], pt_reg[g:]], 1)[..., 0] == np.concatenate([pt_reg[:g], pt_reg[g:]], 1)[..., 1]).mean(1)

    rows = []
    for gm in games.itertuples():
        gg = pbp[pbp.game_id == gm.game_id]
        st = SS.game_states(gg, gm.s0, gm.s1).assign(game_id=gm.game_id, starter0=gm.s0, starter1=gm.s1)
        secs = st.game_secs_row.to_numpy()
        pick = sorted({int(np.flatnonzero(secs >= t)[-1]) for t in CHECK if (secs >= t).any()})
        x = st.iloc[pick].copy()
        x["check"] = [min(CHECK, key=lambda c: abs(c - s)) for s in secs[pick]]
        reg = gg[gg.qtr <= 4].sort_values("play_id").reset_index(drop=True)
        x["cur_total"] = (reg.total_home_score + reg.total_away_score).to_numpy()[x.row]
        fin = st.iloc[-1]
        sc = sched.loc[gm.game_id]
        x["final_yds0"], x["final_yds1"] = fin.yds0, fin.yds1
        x["final_home"], x["final_away"] = sc.home_score, sc.away_score
        # regulation score: last state of regulation (pbp's qtr <= 4)
        x["reg_margin"] = fin.margin
        pre = PG.kickoff_frame(an[an.game_id == gm.game_id]).copy()  # each team receiving first (average them)
        pre = pre.assign(game_id=gm.game_id, starter0=gm.s0, starter1=gm.s1, check=3600, row=-1,
                         final_yds0=fin.yds0, final_yds1=fin.yds1, final_home=sc.home_score,
                         final_away=sc.away_score, reg_margin=fin.margin, cur_total=0.0)
        rows.append(pd.concat([pre[[c for c in pre.columns if c in x.columns or c in ("pos",)]], x], ignore_index=True))
    X = pd.concat(rows, ignore_index=True)
    for c in ("halftime", "out0", "out1", "kick"):
        X[c] = X[c].fillna(False).astype(bool)
    X["pat"] = X.pat.fillna(0)
    X["margin"] = X.margin.fillna(0)
    X["yds0"], X["yds1"] = X.yds0.fillna(0), X.yds1.fillna(0)
    X["clock"] = X.clock.fillna(3600)
    print(f"{len(X)} states {time.time() - t0:.0f}s", flush=True)
    X = X.merge(pd.DataFrame({"game_id": an.game_id, "pre_tie": tie, "spread_line": an.spread_line,
                              "total_line": an.total_line}), on="game_id")
    parts = []
    for i in range(0, len(X), 200):
        parts.append(replay(su, an, X.iloc[i:i + 200], a.n))
    R = pd.concat(parts, ignore_index=True)
    OUT.mkdir(parents=True, exist_ok=True)
    keep = [c for c in R.columns if c.startswith(("p0_", "p1_")) or c in (
        "game_id", "check", "clock", "margin", "yds0", "yds1", "out0", "out1", "final_yds0", "final_yds1",
        "final_home", "final_away", "reg_margin", "home_win", "mean_margin", "mean_total", "p_over", "p_cover",
        "pre_tie", "spread_line", "total_line", "median0", "median1", "cur_total")]
    R[keep].to_parquet(OUT / f"{a.season}_{a.label}.parquet")
    print(f"done {time.time() - t0:.0f}s", flush=True)


def replay(su, an, states, n):
    """PG.replay plus margin/total distributions (final score = current score + points from here)."""
    from nflsim.sim import coaches as CO, teams as TM

    mu = an.set_index("game_id")[["mu0", "mu1"]]
    cols = [f"{c}{k}" for c in list(TM.METRICS) + ["delta", "mult", *CO.COLS] for k in (0, 1)] + ["delta_sd"]
    cols = [c for c in cols if c in an.columns]
    x = states.drop(columns=[c for c in cols if c in states.columns]).merge(an[["game_id"] + cols], on="game_id")
    for k in (0, 1):
        fm, fs = TM.form_posterior(x[f"yds{k}"], x.clock, mu.loc[x.game_id, f"mu{k}"].to_numpy(), su.noise)
        x[f"form_mean{k}"], x[f"form_sd{k}"] = fm, fs
    y, pt = E.simulate(su.tb, x, n, seed=1, points=True)
    m = x.margin.to_numpy()[:, None] + pt[..., 0] - pt[..., 1]
    tot = x.cur_total.to_numpy()[:, None] + pt.sum(-1)
    x["home_win"] = (m > 0).mean(1) + 0.5 * (m == 0).mean(1)
    x["mean_margin"] = m.mean(1)
    x["mean_total"] = tot.mean(1)
    # nflverse spread_line > 0: home favored by that many
    x["p_cover"] = (m > x.spread_line.to_numpy()[:, None]).mean(1) + 0.5 * (m == x.spread_line.to_numpy()[:, None]).mean(1)
    x["p_over"] = (tot > x.total_line.to_numpy()[:, None]).mean(1) + 0.5 * (tot == x.total_line.to_numpy()[:, None]).mean(1)
    for k in (0, 1):
        x[f"median{k}"] = np.median(y[..., k], axis=1)
        for L in PG.LINES:
            x[f"p{k}_{L}"] = np.mean(y[..., k] >= L, axis=1)
    return x


if __name__ == "__main__":
    main()
