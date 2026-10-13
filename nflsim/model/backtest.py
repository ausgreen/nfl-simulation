"""Walk-forward backtest: train on earlier seasons only, predict the next one, score probabilities.

Scores are Brier score and log loss on 1[yards >= line] for each line, compared with
  - league: the training-set base rate (no information about the QB)
  - hist:   the QB's own hit rate over his starts in the prior three seasons (the
            dashboard's "fair price"), falling back to the league rate with no starts.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .core import P_MAX, P_MIN, YardsModel

LINES = (150, 175, 200, 225, 250, 275, 300)
FIRST_TRAIN = 2012          # 2010-11 serve only as feature warm-up
VALIDATION = range(2015, 2019)
TEST = range(2019, 2027)


def hist_baseline(df: pd.DataFrame, rows: pd.DataFrame, lines=LINES, fallback: dict | None = None) -> np.ndarray:
    """QB's empirical P(yards >= line) over starts in the three prior seasons."""
    played = df[df.played]
    by_qb = {pid: g for pid, g in played.groupby("player_id")}
    out = np.empty((len(rows), len(lines)))
    for i, r in enumerate(rows.itertuples()):
        g = by_qb.get(r.player_id)
        y = g[(g.season >= r.season - 3) & (g.season < r.season)].yards.to_numpy() if g is not None else []
        for j, line in enumerate(lines):
            out[i, j] = (np.mean(np.asarray(y) >= line) if len(y) else fallback[line])
    return np.clip(out, P_MIN, P_MAX)


def walk_forward(df: pd.DataFrame, features: str = "qb+def", kind: str = "ridge",
                 seasons=TEST, lines=LINES) -> pd.DataFrame:
    """Predictions for every played starter-game in `seasons`, each from a model fit on prior seasons."""
    data = df[df.played & (df.season >= FIRST_TRAIN)]
    parts = []
    for season in seasons:
        train, test = data[data.season < season], data[data.season == season]
        if test.empty:
            continue
        model = YardsModel(features=features, kind=kind).fit(train)
        test = test.dropna(subset=model.columns)
        league = {line: float(np.mean(train.yards >= line)) for line in lines}
        p = model.prob_ge(test, lines)
        h = hist_baseline(df, test, lines, league)
        part = test[["game_id", "season", "week", "player_id", "qb", "team", "opponent", "yards"]].copy()
        part["mu"] = model.predict_mu(test)
        for j, line in enumerate(lines):
            part[f"model_{line}"] = p[:, j]
            part[f"hist_{line}"] = h[:, j]
            part[f"league_{line}"] = league[line]
        parts.append(part)
    return pd.concat(parts, ignore_index=True)


def score(pred: pd.DataFrame, lines=LINES, methods=("model", "hist", "league")) -> pd.DataFrame:
    """Mean Brier and log loss per method and line, plus the average over lines."""
    rows = []
    for m in methods:
        for line in lines:
            y = (pred.yards >= line).to_numpy(float)
            p = pred[f"{m}_{line}"].to_numpy()
            rows.append({"method": m, "line": line, "brier": np.mean((p - y) ** 2),
                         "log_loss": -np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)), "n": len(y)})
    out = pd.DataFrame(rows)
    avg = out.groupby("method", sort=False)[["brier", "log_loss"]].mean().reset_index().assign(line="all")
    out = pd.concat([out, avg], ignore_index=True)
    base = out[out.method == "league"].set_index("line").brier
    out["brier_skill"] = 1 - out.brier / out.line.map(base)
    return out


def calibration(pred: pd.DataFrame, method: str = "model", lines=LINES, bins: int = 10) -> pd.DataFrame:
    """Predicted vs observed hit rate, pooled over lines, in equal-width probability bins."""
    p = np.concatenate([pred[f"{method}_{line}"].to_numpy() for line in lines])
    y = np.concatenate([(pred.yards >= line).to_numpy(float) for line in lines])
    b = np.minimum((p * bins).astype(int), bins - 1)
    t = pd.DataFrame({"bin": b, "p": p, "y": y}).groupby("bin").agg(predicted=("p", "mean"),
                                                                    observed=("y", "mean"), n=("y", "size"))
    t.index = [f"{i / bins:.0%}–{(i + 1) / bins:.0%}" for i in t.index]
    return t


def market_check(ds, df: pd.DataFrame, quotes: pd.DataFrame, features: str = "qb+def",
                 edge: float = 0.05) -> tuple[pd.DataFrame, dict]:
    """Model vs recorded prices for games already played (model fit on seasons before the quote's).

    Returns per-quote rows and a summary: Brier of model and of price, and the result of
    buying yes at the quoted price whenever model probability >= price + edge.
    """
    rows = []
    for season, qs in quotes.groupby("season"):
        model = YardsModel(features=features).fit(df[df.played & (df.season >= FIRST_TRAIN) & (df.season < season)])
        for q in qs.itertuples():
            x = df[(df.season == q.season) & (df.week == q.week) & (df.player_id == q.player_id) & df.played]
            if x.empty:
                continue
            p = float(model.prob_ge(x, [q.line])[0, 0])
            rows.append({"qb": q.qb, "week": q.week, "line": q.line, "price": q.yes_price, "model": p,
                         "yards": float(x.yards.iloc[0]), "hit": float(x.yards.iloc[0] >= q.line)})
    out = pd.DataFrame(rows)
    if out.empty:
        return out, {}
    bets = out[out.model >= out.price + edge]
    return out, {"n": len(out), "brier_model": float(((out.model - out.hit) ** 2).mean()),
                 "brier_price": float(((out.price - out.hit) ** 2).mean()),
                 "bets": len(bets), "bet_pnl_per_contract": float((bets.hit - bets.price).sum())}
