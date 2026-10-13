"""Pregame features for every starting QB in every regular-season game.

Each row describes a game using only games played in earlier weeks (no leakage).
QB form and defense form are exponentially weighted across seasons and shrunk
toward league averages when the history is short.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

import numpy as np
import pandas as pd

from ..data import Dataset
from ..nflverse import FRANCHISE, load_passing, load_schedules

QB_FEATURES = ["qb_starts_log", "qb_ewm_short", "qb_ewm_long", "qb_season_mean", "qb_prev_season",
               "qb_att_ewm", "qb_ypa_ewm", "qb_short_exit_rate", "qb_sd"]
DEF_FEATURES = ["def_allowed_ewm", "def_allowed_season", "def_adj_ewm", "def_adj_season"]
CONTEXT_FEATURES = ["home", "dome", "wind"]
LINE_FEATURES = ["implied_points", "spread_for"]


@dataclass
class FeatureParams:
    # Tuned on validation seasons 2015-18 (results were flat across the grid).
    qb_short_half_life: float = 4      # games
    qb_long_half_life: float = 16
    def_half_life: float = 16
    qb_shrink: float = 10              # pseudo-games of league average added to QB means
    def_shrink: float = 15
    season_shrink: float = 3
    newcomer_prior: bool = True        # shrink short histories toward new starters, not all starters
    newcomer_starts: int = 5


def _ewm(values: list[float], half_life: float) -> tuple[float, float]:
    """(weighted mean, effective weight) with the most recent value weighted 1."""
    if not values:
        return np.nan, 0.0
    v = np.asarray(values, dtype=float)
    w = 0.5 ** (np.arange(len(v))[::-1] / half_life)
    return float((w * v).sum() / w.sum()), float(w.sum())


def _shrunk(values, half_life, prior, k):
    mean, weight = _ewm(values, half_life)
    return prior if weight == 0 else (mean * weight + prior * k) / (weight + k)


def starter_games(ds: Dataset) -> pd.DataFrame:
    """One row per team per regular-season game: the starter and his passing yards.

    Starter = the schedule's listed QB when he passed in that game, else the team's
    primary passer (most attempts). Listed starters can be stale projections.
    """
    p = load_passing(ds)
    p = p[p.season_type == "REG"]
    s = load_schedules(ds)
    s = s[s.game_type == "REG"].copy()
    for c in ("home_team", "away_team"):
        s[c] = s[c].replace(FRANCHISE)
    by_game = {k: g for k, g in p.groupby(["game_id", "team"])}
    rows = []
    for g in s.itertuples():
        for side, other in (("home", "away"), ("away", "home")):
            team, opp = getattr(g, f"{side}_team"), getattr(g, f"{other}_team")
            passers = by_game.get((g.game_id, team))
            row = {"game_id": g.game_id, "season": g.season, "week": g.week, "gameday": g.gameday,
                   "team": team, "opponent": opp, "home": int(side == "home"),
                   "dome": int(g.roof in ("dome", "closed")),
                   "wind": 0.0 if g.roof in ("dome", "closed") else g.wind,
                   "spread_for": g.spread_line if side == "home" else -g.spread_line,
                   "total_line": g.total_line, "listed_qb": getattr(g, f"{side}_qb_id"),
                   "listed_name": getattr(g, f"{side}_qb_name"), "played": passers is not None}
            if passers is not None:
                listed = passers[passers.player_id == row["listed_qb"]]
                starter = listed.iloc[0] if len(listed) else passers.sort_values("attempts").iloc[-1]
                row.update(player_id=starter.player_id, qb=starter.player_display_name,
                           yards=float(starter.passing_yards), attempts=float(starter.attempts),
                           team_pass_yards=float(passers.passing_yards.sum()))
            else:
                row.update(player_id=row["listed_qb"], qb=row["listed_name"])
            rows.append(row)
    out = pd.DataFrame(rows)
    out["implied_points"] = (out.total_line + out.spread_for) / 2
    return out.sort_values(["season", "week", "game_id", "home"]).reset_index(drop=True)


def build_features(ds: Dataset, params: FeatureParams | None = None, games: pd.DataFrame | None = None
                   ) -> pd.DataFrame:
    """Add pregame QB/defense features to every starter-game, walking weeks in order.

    Unplayed games (e.g. next week's) get features too, so the same code predicts them.
    """
    prm = params or FeatureParams()
    games = starter_games(ds) if games is None else games
    wind_median = games.wind.median()
    qb_hist = defaultdict(list)       # player_id -> [(season, yards, attempts)]
    def_hist = defaultdict(list)      # defense -> [(season, total yards allowed, starter residual)]
    league_yards, league_att, league_allowed = [], [], []
    newcomer_yards = []               # yards in a QB's first few starts: the prior for short histories
    feats = []
    for (season, week), wk in games.groupby(["season", "week"], sort=True):
        prior_y = np.mean(league_yards[-1500:]) if league_yards else 220.0
        prior_new = np.mean(newcomer_yards[-600:]) if len(newcomer_yards) > 50 else prior_y - 25
        prior_a = np.mean(league_att[-1500:]) if league_att else 33.0
        prior_allowed = np.mean(league_allowed[-1500:]) if league_allowed else 230.0
        week_rows = []
        for r in wk.itertuples():
            h = qb_hist.get(r.player_id, [])
            ys = [x[1] for x in h]
            atts = [x[2] for x in h]
            this = [x[1] for x in h if x[0] == season]
            prev = [x[1] for x in h if x[0] == season - 1]
            long = _shrunk(ys, prm.qb_long_half_life, prior_new if prm.newcomer_prior else prior_y,
                           prm.qb_shrink)
            att = _shrunk(atts, prm.qb_long_half_life, prior_a, prm.qb_shrink)
            ypa_w = _ewm(atts, prm.qb_long_half_life)[1]
            ypa = (sum(ys[-24:]) / max(sum(atts[-24:]), 1)) if atts else prior_y / prior_a
            ypa = (ypa * ypa_w + (prior_y / prior_a) * prm.qb_shrink) / (ypa_w + prm.qb_shrink)
            short_exit = _shrunk([float(a < 20) for a in atts], prm.qb_long_half_life, 0.06, prm.qb_shrink)
            sd = float(np.std(ys[-24:])) if len(ys) >= 6 else 75.0
            d = def_hist.get(r.opponent, [])
            allowed = [x[1] for x in d]
            adj = [x[2] for x in d]
            d_this = [x for x in d if x[0] == season]
            def_ewm = _shrunk(allowed, prm.def_half_life, prior_allowed, prm.def_shrink)
            adj_ewm = _shrunk(adj, prm.def_half_life, 0.0, prm.def_shrink)
            feats.append({
                "qb_starts_log": np.log1p(len(h)),
                "qb_ewm_short": _shrunk(ys, prm.qb_short_half_life, long, prm.qb_shrink),
                "qb_ewm_long": long,
                "qb_season_mean": (sum(this) + long * prm.season_shrink) / (len(this) + prm.season_shrink),
                "qb_prev_season": (sum(prev) + long * prm.season_shrink) / (len(prev) + prm.season_shrink),
                "qb_att_ewm": att, "qb_ypa_ewm": ypa, "qb_short_exit_rate": short_exit, "qb_sd": sd,
                "def_allowed_ewm": def_ewm,
                "def_allowed_season": (sum(x[1] for x in d_this) + def_ewm * prm.season_shrink)
                                      / (len(d_this) + prm.season_shrink),
                "def_adj_ewm": adj_ewm,
                "def_adj_season": (sum(x[2] for x in d_this) + adj_ewm * prm.season_shrink)
                                  / (len(d_this) + prm.season_shrink),
                "def_games_season": len(d_this), "qb_starts": len(h),
                "wind_filled": r.wind if pd.notna(r.wind) else wind_median,
            })
            week_rows.append((r, long))
        # update histories only after the whole week is featurized
        for r, baseline in week_rows:
            if not r.played or pd.isna(r.player_id):
                continue
            qb_hist[r.player_id].append((season, r.yards, r.attempts))
            def_hist[r.opponent].append((season, r.team_pass_yards, r.yards - baseline))
            league_yards.append(r.yards)
            if len(qb_hist[r.player_id]) <= prm.newcomer_starts:
                newcomer_yards.append(r.yards)
            league_att.append(r.attempts)
            league_allowed.append(r.team_pass_yards)
    f = pd.DataFrame(feats, index=games.index)
    out = pd.concat([games, f], axis=1)
    out["wind"] = out.pop("wind_filled")
    return out
