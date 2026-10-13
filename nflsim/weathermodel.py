"""v2 model: the v1 probability model plus game weather (cold, wind, rain).

Builds on nflsim/model/ without modifying it (that code is frozen for forward test v1).
The feature set "qb+def+weather" = qb+def + WX_FEATURES; weather is 0 indoors, and the dome
flag follows the actual roof for past games and the assumed roof for upcoming ones.
Feature choice (rain, cold, wind; not gusts or snow) was made after the weather experiment
(scripts/experiments/weather.py), so only the forward test can validate it.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import weather
from .data import Dataset
from .model import core
from .model.backtest import FIRST_TRAIN
from .model.features import build_features
from .model.predict import PRED_LINES, week_features
from .weather import WX_FEATURES

FEATURES = "qb+def+weather"
core.FEATURE_SETS[FEATURES] = core.FEATURE_SETS["qb+def"] + WX_FEATURES


def add_history(f: pd.DataFrame, hist: pd.DataFrame) -> pd.DataFrame:
    """Join historical weather; games without weather data get 0 (same as a mild day)."""
    out = f.drop(columns=[c for c in WX_FEATURES if c in f.columns]).join(hist[WX_FEATURES], on="game_id")
    out[WX_FEATURES] = out[WX_FEATURES].fillna(0.0)
    return out


def training_frame(ds: Dataset, hist: pd.DataFrame | None = None) -> pd.DataFrame:
    hist = weather.historical(ds) if hist is None else hist
    return add_history(build_features(ds), hist)


def fit(ds: Dataset, season: int, hist: pd.DataFrame | None = None) -> core.YardsModel:
    f = training_frame(ds, hist)
    return core.YardsModel(features=FEATURES).fit(f[f.played & (f.season >= FIRST_TRAIN) & (f.season < season)])


def predict_week(ds: Dataset, season: int, week: int, lines=PRED_LINES,
                 fc: pd.DataFrame | None = None, hist: pd.DataFrame | None = None) -> pd.DataFrame:
    """Like model.predict.predict_week, with forecast weather and the assumed roof per game."""
    model = fit(ds, season, hist)
    x = week_features(ds, season, week)
    fc = weather.forecast(ds, x.game_id.unique()) if fc is None else fc
    x = x.join(fc[WX_FEATURES + ["roof_assumed"]], on="game_id")
    x[WX_FEATURES] = x[WX_FEATURES].fillna(0.0)
    x["dome"] = (x.roof_assumed == "closed").astype(int)
    x["wind"] = np.where(x.dome == 1, 0.0, x.wind)
    p = model.prob_ge(x, lines)
    out = x[["season", "week", "game_id", "player_id", "qb", "team", "opponent", "home", "qb_starts",
             "roof_assumed", *WX_FEATURES]].copy()
    out["mu"] = model.predict_mu(x)
    for j, line in enumerate(lines):
        out[f"p_{line}"] = p[:, j]
    out.attrs["features"] = FEATURES
    return out
