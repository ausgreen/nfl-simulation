"""The v3 model: QB form + opponent defense + weather + betting lines.

Chosen by scripts/experiments/lab.py (reports/experiments/). Inputs beyond v1:
  - betting lines: over/under (`total_line_c`), point spread for the team (`spread_for`),
    and squared terms for lopsided games (`implied_sq`, `spread_sq`);
  - weather: cold, wind above 12 mph, rain capped at 6 mm (0 indoors; forecast for upcoming games).
    Uncapped rain over-penalized downpours: in >=12 mm games QBs beat the linear model by
    +43 +/- 20 yds (report 08_rain_cap).
Ridge regression for expected yards + the empirical residual distribution (core.YardsModel).
Builds on nflsim/model/ without changing it (that code is frozen by the forward tests).

For upcoming games the lines come from the nflverse schedule (refresh with
`nflsim model fetch --since <season>`); games without a line yet use the league median.
"""

from __future__ import annotations

import pandas as pd

from .. import weather
from ..data import Dataset
from ..model import core
from ..model.backtest import FIRST_TRAIN
from ..model.features import build_features
from ..model.predict import PRED_LINES, week_features
from ..weathermodel import add_history

FEATURES = "v3"
COLUMNS = (core.FEATURE_SETS["qb+def"] + ["total_line_c", "spread_for", "implied_sq", "spread_sq"]
           + weather.WX_FEATURES)
core.FEATURE_SETS[FEATURES] = COLUMNS


RAIN_CAP_MM = 6.0


def derive(f: pd.DataFrame, total_median: float | None = None) -> pd.DataFrame:
    f = f.copy()
    f["precip_mm"] = f.precip_mm.clip(upper=RAIN_CAP_MM)
    med = f.total_line.median() if total_median is None else total_median
    f["total_line_c"] = f.total_line.fillna(med)
    f["spread_for"] = f.spread_for.fillna(0.0)
    implied = (f.total_line_c + f.spread_for) / 2
    f["implied_sq"] = (implied - 22.5) ** 2
    f["spread_sq"] = f.spread_for ** 2
    return f


def training_frame(ds: Dataset, hist: pd.DataFrame | None = None) -> pd.DataFrame:
    hist = weather.historical(ds) if hist is None else hist
    return derive(add_history(build_features(ds), hist))


def fit(ds: Dataset, season: int, hist: pd.DataFrame | None = None) -> tuple[core.YardsModel, float]:
    f = training_frame(ds, hist)
    train = f[f.played & (f.season >= FIRST_TRAIN) & (f.season < season)]
    return core.YardsModel(features=FEATURES).fit(train), float(train.total_line.median())


def predict_week(ds: Dataset, season: int | None = None, week: int | None = None, lines=PRED_LINES,
                 fc: pd.DataFrame | None = None, hist: pd.DataFrame | None = None) -> pd.DataFrame:
    """P(yards >= line) for each candidate QB of a week, with forecast weather and current lines."""
    season, week = season or ds.current_season, week or ds.next_week
    model, total_median = fit(ds, season, hist)
    x = week_features(ds, season, week)
    fc = weather.forecast(ds, x.game_id.unique()) if fc is None else fc
    x = x.join(fc[weather.WX_FEATURES + ["roof_assumed"]], on="game_id")
    x[weather.WX_FEATURES] = x[weather.WX_FEATURES].fillna(0.0)
    x["dome"] = (x.roof_assumed == "closed").astype(int)
    x.loc[x.dome == 1, "wind"] = 0.0
    x = derive(x, total_median)
    p = model.prob_ge(x, lines)
    q = model.quantiles(x)
    out = x[["season", "week", "game_id", "player_id", "qb", "team", "opponent", "home", "qb_starts",
             "total_line", "spread_for", "roof_assumed", *weather.WX_FEATURES]].copy()
    out["line_missing"] = x.total_line.isna()
    out["mu"] = model.predict_mu(x)
    out["p10"], out["p50"], out["p90"] = q[:, 0], q[:, 1], q[:, 2]
    for j, line in enumerate(lines):
        out[f"p_{line}"] = p[:, j]
    out.attrs["features"] = FEATURES
    return out
