"""P(passing yards >= x): a mean model plus an empirical, heteroscedastic residual distribution.

1. A regressor predicts expected yards mu from pregame features.
2. Out-of-fold residuals (grouped by season) give a scale model sigma(mu) and the
   empirical distribution of standardized residuals z = (y - mu) / sigma, which keeps
   the real left tail (injury exits, blowouts) instead of assuming a normal curve.
3. P(Y >= x) = share of training z at or above (x - 0.5 - mu) / sigma.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.linear_model import LinearRegression, Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from .features import CONTEXT_FEATURES, DEF_FEATURES, LINE_FEATURES, QB_FEATURES

FEATURE_SETS = {
    "qb": QB_FEATURES + CONTEXT_FEATURES,
    "qb+def": QB_FEATURES + DEF_FEATURES + CONTEXT_FEATURES,
    "qb+def+lines": QB_FEATURES + DEF_FEATURES + CONTEXT_FEATURES + LINE_FEATURES,
}
P_MIN, P_MAX = 0.003, 0.997


def make_regressor(kind: str):
    if kind == "ridge":
        return make_pipeline(StandardScaler(), Ridge(alpha=10.0))
    if kind == "gbm":
        return HistGradientBoostingRegressor(max_depth=3, learning_rate=0.04, max_iter=300,
                                             min_samples_leaf=60, l2_regularization=1.0, random_state=0)
    raise ValueError(kind)


@dataclass
class YardsModel:
    features: str = "qb+def"
    kind: str = "ridge"
    folds: int = 5
    reg: object = None
    scale: LinearRegression = None
    z: np.ndarray = field(default=None, repr=False)

    @property
    def columns(self) -> list[str]:
        return FEATURE_SETS[self.features]

    def _sigma(self, mu: np.ndarray) -> np.ndarray:
        return np.maximum(self.scale.predict(mu.reshape(-1, 1)), 25.0)

    def fit(self, df: pd.DataFrame) -> "YardsModel":
        df = df.dropna(subset=self.columns + ["yards"])
        X, y = df[self.columns].to_numpy(), df.yards.to_numpy()
        seasons = df.season.to_numpy()
        groups = np.array_split(np.unique(seasons), min(self.folds, len(np.unique(seasons))))
        oof = np.empty_like(y)
        for g in groups:
            test = np.isin(seasons, g)
            oof[test] = make_regressor(self.kind).fit(X[~test], y[~test]).predict(X[test])
        resid = y - oof
        self.scale = LinearRegression().fit(oof.reshape(-1, 1), np.abs(resid))
        self.z = np.sort(resid / np.maximum(self.scale.predict(oof.reshape(-1, 1)), 25.0))
        self.reg = make_regressor(self.kind).fit(X, y)
        return self

    def predict_mu(self, df: pd.DataFrame) -> np.ndarray:
        return self.reg.predict(df[self.columns].to_numpy())

    def prob_ge(self, df: pd.DataFrame, lines) -> np.ndarray:
        """Matrix [rows x lines] of P(yards >= line)."""
        mu = self.predict_mu(df)
        sigma = self._sigma(mu)
        t = (np.asarray(lines, dtype=float)[None, :] - 0.5 - mu[:, None]) / sigma[:, None]
        below = np.searchsorted(self.z, t.ravel(), side="left").reshape(t.shape)
        return np.clip(1 - below / len(self.z), P_MIN, P_MAX)

    def quantiles(self, df: pd.DataFrame, qs=(0.1, 0.5, 0.9)) -> np.ndarray:
        mu = self.predict_mu(df)
        return mu[:, None] + self._sigma(mu)[:, None] * np.quantile(self.z, qs)[None, :]
