"""Fit on every completed season and predict P(yards >= x) for an upcoming week."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from .. import kalshi
from ..data import Dataset
from .backtest import FIRST_TRAIN
from .core import YardsModel
from .features import build_features, starter_games

PRED_LINES = tuple(range(125, 401, 25))


def candidates(ds: Dataset, season: int, week: int) -> pd.DataFrame:
    """QBs to price: each roster starter plus any other QB Kalshi lists for that week."""
    rows = [(r.player_id, r.qb, r.team) for r in ds.roster.itertuples()]
    snap = kalshi.latest_snapshot(ds)
    if snap is not None:
        live = snap[(snap.season == season) & (snap.week == week)].dropna(subset=["player_id", "team"])
        rows += [(r.player_id, r.qb, r.team) for r in live.drop_duplicates("player_id").itertuples()]
    return pd.DataFrame(rows, columns=["player_id", "qb", "team"]).drop_duplicates("player_id")


def week_features(ds: Dataset, season: int, week: int) -> pd.DataFrame:
    """Feature rows for the week's games, one per candidate QB (backups get their own row)."""
    games = starter_games(ds)
    target = games[(games.season == season) & (games.week == week)]
    extra = []
    for c in candidates(ds, season, week).itertuples():
        g = target[target.team == c.team]
        if g.empty:
            continue  # bye
        row = g.iloc[0].copy()
        row["player_id"], row["qb"] = c.player_id, c.qb
        extra.append(row)
    rest = games[(games.season < season) | ((games.season == season) & (games.week < week))]
    combined = pd.concat([rest, pd.DataFrame(extra)], ignore_index=True)
    f = build_features(ds, games=combined)
    return f[(f.season == season) & (f.week == week)].reset_index(drop=True)


def fit(ds: Dataset, features: str = "qb+def", season: int | None = None) -> YardsModel:
    season = season or ds.current_season
    f = build_features(ds)
    train = f[f.played & (f.season >= FIRST_TRAIN) & (f.season < season)]
    return YardsModel(features=features).fit(train)


def predict_week(ds: Dataset, season: int | None = None, week: int | None = None,
                 features: str = "qb+def", lines=PRED_LINES) -> pd.DataFrame:
    """One row per candidate QB: expected yards, 10/50/90% quantiles, P(yards >= line) per line."""
    season, week = season or ds.current_season, week or ds.next_week
    model = fit(ds, features, season)
    x = week_features(ds, season, week)
    p = model.prob_ge(x, lines)
    q = model.quantiles(x)
    out = x[["season", "week", "game_id", "player_id", "qb", "team", "opponent", "home"]].copy()
    out["qb_starts"] = x.qb_starts.to_numpy()
    out["mu"] = model.predict_mu(x)
    out["p10"], out["p50"], out["p90"] = q[:, 0], q[:, 1], q[:, 2]
    for j, line in enumerate(lines):
        out[f"p_{line}"] = p[:, j]
    out.attrs["features"] = features
    return out


def versus_kalshi(ds: Dataset, pred: pd.DataFrame) -> pd.DataFrame:
    """Join model probabilities to the latest Kalshi asks for the same QB and line."""
    snap = kalshi.latest_snapshot(ds)
    if snap is None:
        return pd.DataFrame()
    q = kalshi.as_quotes(snap)
    rows = []
    by_pid = pred.set_index("player_id")
    for r in q.itertuples():
        if r.player_id not in by_pid.index or r.season != pred.season.iloc[0] or r.week != pred.week.iloc[0]:
            continue
        m = by_pid.loc[r.player_id]
        col = f"p_{int(r.line)}"
        if col not in m:
            continue
        rows.append({"qb": r.qb, "opponent": r.opponent, "line": int(r.line), "yes_bid": r.yes_bid,
                     "yes_ask": r.yes_price, "model_p": float(m[col]), "edge": float(m[col]) - r.yes_price,
                     "mu": float(m.mu), "volume": r.volume, "url": r.url})
    return pd.DataFrame(rows).sort_values("edge", ascending=False).reset_index(drop=True)


def save(ds: Dataset, pred: pd.DataFrame, vs: pd.DataFrame) -> Path:
    """Write the week's predictions (and Kalshi comparison) for later grading."""
    out = ds.data_dir / "model"
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    s, w, feats = int(pred.season.iloc[0]), int(pred.week.iloc[0]), pred.attrs.get("features", "qb+def")
    path = out / f"predictions_{s}_w{w:02d}_{feats.replace('+', '-')}_{stamp}.csv"
    pred.to_csv(path, index=False)
    if len(vs):
        vs.to_csv(path.with_name(path.stem + "_kalshi.csv"), index=False)
    return path
