"""League-wide training data from nflverse: every passer in every game, plus schedules.

The Game Log only tracks rostered QBs, so it cannot say what a defense allowed in
past seasons. These files cover every team, which the model needs.
"""

from __future__ import annotations

import io
import urllib.request
from pathlib import Path

import pandas as pd

from .data import Dataset

STATS_URL = "https://github.com/nflverse/nflverse-data/releases/download/stats_player/stats_player_week_{season}.csv"
SCHEDULE_URL = "https://github.com/nflverse/nfldata/raw/refs/heads/master/data/games.csv"
FIRST_SEASON = 2010

PASSING_COLUMNS = ["season", "week", "season_type", "game_id", "player_id", "player_display_name", "position",
                   "team", "opponent_team", "attempts", "completions", "passing_yards", "sacks_suffered"]
SCHEDULE_COLUMNS = ["game_id", "season", "game_type", "week", "gameday", "gametime", "away_team", "home_team",
                    "away_score", "home_score", "location", "away_rest", "home_rest", "spread_line",
                    "total_line", "div_game", "roof", "surface", "temp", "wind", "away_qb_id", "home_qb_id",
                    "away_qb_name", "home_qb_name", "stadium_id", "stadium"]

# Relocated franchises, so a defense's prior seasons follow it to the new city.
FRANCHISE = {"OAK": "LV", "SD": "LAC", "STL": "LA"}


def _download(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "nflsim"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return r.read()


def nflverse_dir(ds: Dataset) -> Path:
    return ds.data_dir / "nflverse"


def fetch(ds: Dataset, seasons=None, log=print) -> None:
    """Download passing rows for each season and the schedule; writes data/nflverse/*.csv."""
    out = nflverse_dir(ds)
    out.mkdir(parents=True, exist_ok=True)
    seasons = list(seasons or range(FIRST_SEASON, ds.current_season + 1))
    frames = []
    for season in seasons:
        raw = pd.read_csv(io.BytesIO(_download(STATS_URL.format(season=season))), low_memory=False)
        rows = raw[raw.attempts.fillna(0) >= 1][PASSING_COLUMNS]
        frames.append(rows)
        log(f"{season}: {len(rows)} passing rows")
    passing = pd.concat(frames, ignore_index=True)
    existing = out / "passing.csv"
    if existing.exists():  # keep seasons not refetched
        old = pd.read_csv(existing, dtype={"player_id": str})
        passing = pd.concat([old[~old.season.isin(seasons)], passing], ignore_index=True)
    passing.sort_values(["season", "week", "game_id", "team"]).to_csv(existing, index=False)
    n = fetch_schedules(ds)
    log(f"wrote {existing} ({len(passing)} rows) and schedules.csv ({n} games)")


def fetch_schedules(ds: Dataset) -> int:
    """Refresh schedules.csv (kickoffs, listed starters, betting lines, roofs)."""
    sched = pd.read_csv(io.BytesIO(_download(SCHEDULE_URL)), low_memory=False)
    sched = sched[sched.season >= FIRST_SEASON][SCHEDULE_COLUMNS]
    sched.to_csv(nflverse_dir(ds) / "schedules.csv", index=False)
    return len(sched)


def load_passing(ds: Dataset) -> pd.DataFrame:
    return pd.read_csv(nflverse_dir(ds) / "passing.csv", dtype={"player_id": str})


def load_schedules(ds: Dataset) -> pd.DataFrame:
    return pd.read_csv(nflverse_dir(ds) / "schedules.csv", dtype={"away_qb_id": str, "home_qb_id": str})
