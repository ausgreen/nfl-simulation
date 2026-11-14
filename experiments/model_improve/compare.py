"""Compare backtest arms (experiments/model_improve/backtest.py output), same games and checkpoints.

usage: uv run python experiments/model_improve/compare.py BASE_LABEL ARM_LABEL SEASON [SEASON ...]

Brier scores (lower is better) for the home win chance, QB lines not yet reached (starter still in), the spread and
the total, by phase; "better in" = share of 2000 game resamples where the arm's summed Brier is lower than the base's.
Also pregame P(regulation ends tied) vs the real rate, and late-game favorites' expected vs actual upsets.
"""

from __future__ import annotations

import sys

import numpy as np
import pandas as pd

BT = "data/live/analysis/model_improve/bt"
LINES = list(range(125, 401, 25))
PHASES = {"pregame": [3600], "Q1-Q3": [2700, 1800, 1200], "Q4 15-5": [900, 600, 420], "Q4 last 5": [300, 180, 120, 60]}


def reg_tied(season) -> pd.Series:
    """Per game: regulation ended tied (the score after the last 4th-quarter row, point after included)."""
    p = pd.read_parquet(f"data/nflverse/pbp_sim/pbp_{season}.parquet",
                        columns=["game_id", "play_id", "qtr", "total_home_score", "total_away_score"])
    last = p[p.qtr <= 4].sort_values(["game_id", "play_id"]).groupby("game_id").last()
    return last.total_home_score == last.total_away_score


def load(season, label):
    r = pd.read_parquet(f"{BT}/{season}_{label}.parquet")
    r["season"] = season
    r["reg_margin"] = np.where(r.game_id.map(reg_tied(season)), 0, 1)
    num = [c for c in r.columns if c.startswith(("p0_", "p1_")) or c in (
        "home_win", "mean_margin", "mean_total", "p_over", "p_cover", "median0", "median1")]
    first = [c for c in r.columns if c not in num and c not in ("game_id", "check")]
    # pregame has two kickoff rows per game (each team receiving first): average them
    return r.groupby(["game_id", "check"], sort=False).agg({**{c: "mean" for c in num},
                                                             **{c: "first" for c in first}}).reset_index()


def scores(r: pd.DataFrame) -> pd.DataFrame:
    """Long table: game_id, check, kind, brier."""
    out = []
    hw = np.where(r.final_home > r.final_away, 1.0, np.where(r.final_home == r.final_away, 0.5, 0.0))
    out.append(pd.DataFrame({"game_id": r.game_id, "check": r.check, "kind": "win", "b": (r.home_win - hw) ** 2}))
    m = r.final_home - r.final_away
    cov = np.where(m > r.spread_line, 1.0, np.where(m == r.spread_line, np.nan, 0.0))
    out.append(pd.DataFrame({"game_id": r.game_id, "check": r.check, "kind": "spread", "b": (r.p_cover - cov) ** 2}))
    t = r.final_home + r.final_away
    ov = np.where(t > r.total_line, 1.0, np.where(t == r.total_line, np.nan, 0.0))
    out.append(pd.DataFrame({"game_id": r.game_id, "check": r.check, "kind": "total", "b": (r.p_over - ov) ** 2}))
    for k in (0, 1):
        for L in LINES:
            ok = (r[f"yds{k}"] < L) & ~r[f"out{k}"].astype(bool)
            hit = (r[f"final_yds{k}"] >= L).astype(float)
            out.append(pd.DataFrame({"game_id": r.game_id[ok], "check": r.check[ok], "kind": "qb",
                                     "b": ((r[f"p{k}_{L}"] - hit) ** 2)[ok]}))
    s = pd.concat(out, ignore_index=True).dropna()
    s["phase"] = s.check.map({c: p for p, cs in PHASES.items() for c in cs})
    return s


def boot(a: pd.DataFrame, b: pd.DataFrame, n=2000, seed=0) -> float:
    ga, gb = a.groupby("game_id").b.sum(), b.groupby("game_id").b.sum()
    d = (ga - gb.reindex(ga.index)).to_numpy()
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(d), (n, len(d)))
    return float(np.mean(d[idx].sum(1) > 0))


def table(base, arm):
    sa, sb = scores(base), scores(arm)
    key = ["game_id", "check", "kind"]
    rows = []
    for kind in ("win", "spread", "total", "qb"):
        for ph in list(PHASES) + ["all in-game"]:
            fa = (sa.kind == kind) & ((sa.phase == ph) if ph != "all in-game" else (sa.phase != "pregame"))
            fb = (sb.kind == kind) & ((sb.phase == ph) if ph != "all in-game" else (sb.phase != "pregame"))
            a, b = sa[fa], sb[fb]
            if not len(a):
                continue
            rows.append({"kind": kind, "phase": ph, "n": len(a), "base": a.b.mean(), "arm": b.b.mean(),
                         "diff": b.b.mean() - a.b.mean(), "better in": boot(a, b)})
    return pd.DataFrame(rows)


def extras(r, name):
    pre = r[r.check == 3600]
    late = r[r.check == 300]
    fav = np.maximum(late.home_win, 1 - late.home_win)
    fav_won = np.where(late.home_win >= 0.5, late.final_home > late.final_away, late.final_away > late.final_home)
    tie_final = late.final_home == late.final_away
    big = fav >= 0.8
    q4 = r[r.check.isin([900, 600, 420, 300, 180, 120, 60])]
    f4 = np.maximum(q4.home_win, 1 - q4.home_win)
    won4 = np.where(q4.home_win >= 0.5, q4.final_home > q4.final_away, q4.final_away > q4.final_home)
    b4 = f4 >= 0.8
    return {"arm": name, "games": r.game_id.nunique(), "sim tie (reg)": pre.pre_tie.mean(),
            "real tie (reg)": (pre.reg_margin == 0).mean(),
            "5:00 fav>=.8 n": int(big.sum()), "exp upsets": float((1 - fav[big]).sum()),
            "upsets": int((~fav_won[big] & ~tie_final[big]).sum()),
            "Q4 fav>=.8 exp": float((1 - f4[b4]).sum()), "Q4 upsets": int((~won4[b4] & (q4.final_home != q4.final_away)[b4]).sum()),
            "pre mean total": pre.mean_total.mean(), "real total": (pre.final_home + pre.final_away).mean()}


def main():
    base_label, arm_label, *seasons = sys.argv[1:]
    seasons = [int(s) for s in seasons]
    base = pd.concat([load(s, base_label) for s in seasons], ignore_index=True)
    arm = pd.concat([load(s, arm_label) for s in seasons], ignore_index=True)
    assert (base[["game_id", "check"]].to_numpy() == arm[["game_id", "check"]].to_numpy()).all()
    pd.set_option("display.width", 200)
    print(f"{arm_label} vs {base_label}, seasons {seasons}")
    print(table(base, arm).to_string(index=False, float_format=lambda v: f"{v:.5f}"))
    print(pd.DataFrame([extras(base, base_label), extras(arm, arm_label)]).round(3).to_string(index=False))


if __name__ == "__main__":
    main()
