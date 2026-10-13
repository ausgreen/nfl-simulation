"""In-game data: nflverse play-by-play -> each starting QB's state through his game.

data/nflverse/pbp/pbp_<season>.parquet  compact play-by-play (needed columns only)
data/live/states.parquet                one row per team-play for every regular-season starter-game:
    ts (UTC wall clock), secs_left, qtr, yards_so_far / att_so_far (the starter's own), score_diff (team's view),
    starter_recent_share (starter's share of the team's last 8 dropbacks: ~0 when benched or hurt),
    final_yards (the starter's game total: the target).
The pbp passing yards match nflverse's weekly stats exactly (checked on 2025: 654/654 QB-games).
"""

from __future__ import annotations

import io
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

from ..data import Dataset
from ..model.features import starter_games
from ..nflverse import FRANCHISE, nflverse_dir

PBP_URL = "https://github.com/nflverse/nflverse-data/releases/download/pbp/play_by_play_{season}.parquet"
COLS = ["game_id", "play_id", "season", "week", "season_type", "time_of_day", "posteam", "defteam", "home_team",
        "away_team", "qtr", "game_seconds_remaining", "passer_player_id", "passing_yards", "pass_attempt", "sack",
        "qb_dropback", "total_home_score", "total_away_score"]


def pbp_dir(ds: Dataset) -> Path:
    return nflverse_dir(ds) / "pbp"


def live_dir(ds: Dataset) -> Path:
    return ds.data_dir / "live"


def fetch_pbp(ds: Dataset, seasons, log=print) -> None:
    out = pbp_dir(ds)
    out.mkdir(parents=True, exist_ok=True)

    def one(season):
        req = urllib.request.Request(PBP_URL.format(season=season), headers={"User-Agent": "nflsim"})
        with urllib.request.urlopen(req, timeout=300) as r:
            p = pd.read_parquet(io.BytesIO(r.read()), columns=COLS)
        p = p[p.season_type == "REG"]
        p.to_parquet(out / f"pbp_{season}.parquet", index=False)
        return season, len(p)

    with ThreadPoolExecutor(4) as pool:
        for season, n in pool.map(one, seasons):
            log(f"pbp {season}: {n:,} plays")


def build_states(ds: Dataset, seasons, log=print) -> pd.DataFrame:
    """Starter states for every play of every regular-season game in `seasons`."""
    sg = starter_games(ds)
    sg = sg[sg.played & sg.season.isin(list(seasons))][["game_id", "season", "week", "team", "player_id", "yards"]]
    starters = {(g, t): (pid, y) for g, t, pid, y in zip(sg.game_id, sg.team, sg.player_id, sg.yards)}
    frames = []
    for season in seasons:
        p = pd.read_parquet(pbp_dir(ds) / f"pbp_{season}.parquet")
        for c in ("posteam", "defteam", "home_team", "away_team"):
            p[c] = p[c].replace(FRANCHISE)
        p = p.dropna(subset=["game_seconds_remaining"]).sort_values(["game_id", "play_id"])
        p["ts"] = pd.to_datetime(p.time_of_day, utc=True, errors="coerce")
        p["ts"] = p.groupby("game_id").ts.transform(lambda s: s.ffill().bfill())
        for gid, g in p.groupby("game_id", sort=False):
            for team in (g.home_team.iloc[0], g.away_team.iloc[0]):
                if (gid, team) not in starters:
                    continue
                pid, final = starters[(gid, team)]
                own = (g.passer_player_id == pid).to_numpy()
                yds = np.where(own, g.passing_yards.fillna(0).to_numpy(), 0.0)
                att = np.where(own, g.pass_attempt.fillna(0).to_numpy(), 0.0)
                home = team == g.home_team.iloc[0]
                diff = np.where(home, g.total_home_score - g.total_away_score, g.total_away_score - g.total_home_score)
                drop = ((g.posteam == team) & (g.qb_dropback.fillna(0) == 1)).to_numpy()
                own_drop = drop & own
                # starter's share of the team's last 8 dropbacks (rolling over dropback plays only)
                share = np.full(len(g), np.nan)
                idx = np.flatnonzero(drop)
                if len(idx):
                    s = pd.Series(own_drop[idx].astype(float)).rolling(8, min_periods=1).mean().to_numpy()
                    share[idx] = s
                share = pd.Series(share).ffill().fillna(1.0).to_numpy()
                frames.append(pd.DataFrame({
                    "game_id": gid, "season": season, "week": int(g.week.iloc[0]), "team": team, "player_id": pid,
                    "ts": g.ts.to_numpy(), "secs_left": g.game_seconds_remaining.to_numpy(), "qtr": g.qtr.to_numpy(),
                    "yards_so_far": np.cumsum(yds), "att_so_far": np.cumsum(att), "score_diff": diff,
                    "starter_recent_share": share, "final_yards": final}))
        log(f"states {season}: done")
    states = pd.concat(frames, ignore_index=True)
    out = live_dir(ds)
    out.mkdir(parents=True, exist_ok=True)
    states.to_parquet(out / "states.parquet", index=False)
    return states


def load_states(ds: Dataset) -> pd.DataFrame:
    return pd.read_parquet(live_dir(ds) / "states.parquet")
