"""Play-by-play for the game simulator: nflverse pbp with the situation columns the state machine needs.

data/nflverse/pbp_sim/pbp_<season>.parquet   regular-season plays, columns in COLS (when the season has them).
Kept apart from data/nflverse/pbp/ (the compact files the stage-1 in-game model reads).
"""

from __future__ import annotations

import io
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

from ..data import Dataset
from ..live.data import PBP_URL
from ..nflverse import FRANCHISE, nflverse_dir

COLS = ["game_id", "play_id", "season", "week", "season_type", "time_of_day", "posteam", "defteam", "home_team",
        "away_team", "qtr", "game_half", "game_seconds_remaining", "half_seconds_remaining", "down", "ydstogo",
        "yardline_100", "play_type", "pass_attempt", "rush_attempt", "qb_dropback", "sack", "complete_pass",
        "incomplete_pass", "interception", "fumble_lost", "yards_gained", "passing_yards", "passer_player_id",
        "touchdown", "td_team", "safety", "field_goal_result", "kick_distance", "extra_point_result",
        "two_point_attempt", "two_point_conv_result", "kickoff_attempt", "punt_attempt", "qb_kneel", "qb_spike",
        "penalty", "timeout", "posteam_timeouts_remaining", "defteam_timeouts_remaining", "total_home_score",
        "total_away_score", "drive", "out_of_bounds"]


def sim_pbp_dir(ds: Dataset) -> Path:
    return nflverse_dir(ds) / "pbp_sim"


def fetch(ds: Dataset, seasons, log=print) -> None:
    out = sim_pbp_dir(ds)
    out.mkdir(parents=True, exist_ok=True)

    def one(season):
        req = urllib.request.Request(PBP_URL.format(season=season), headers={"User-Agent": "nflsim"})
        with urllib.request.urlopen(req, timeout=600) as r:
            raw = io.BytesIO(r.read())
        have = set(pq.read_schema(raw).names)
        p = pd.read_parquet(raw, columns=[c for c in COLS if c in have])
        p = p[p.season_type == "REG"]
        p.to_parquet(out / f"pbp_{season}.parquet", index=False)
        return season, len(p), sorted(set(COLS) - have)

    with ThreadPoolExecutor(4) as pool:
        for season, n, missing in pool.map(one, seasons):
            log(f"pbp_sim {season}: {n:,} plays" + (f" (missing {missing})" if missing else ""))


def load(ds: Dataset, seasons) -> pd.DataFrame:
    p = pd.concat([pd.read_parquet(sim_pbp_dir(ds) / f"pbp_{s}.parquet") for s in seasons], ignore_index=True)
    for c in ("posteam", "defteam", "home_team", "away_team", "td_team"):
        p[c] = p[c].replace(FRANCHISE)
    return p.sort_values(["game_id", "play_id"]).reset_index(drop=True)
