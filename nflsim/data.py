"""Load the CSV inputs under data/ into one Dataset."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

import pandas as pd

DEFAULT_DATA_DIR = Path(os.environ.get("NFLSIM_DATA", Path(__file__).resolve().parent.parent / "data"))

GAME_COLUMNS = ["season", "week", "player_id", "qb", "team", "opponent", "home_away",
                "attempts", "pass_yards", "game_id"]


def _read(path: Path) -> pd.DataFrame:
    # Player IDs look numeric-ish ("00-0033106"); keep every text column as str.
    return pd.read_csv(path, dtype={"player_id": str}, keep_default_na=True)


@dataclass
class Dataset:
    data_dir: Path = DEFAULT_DATA_DIR

    def _load(self, name: str) -> pd.DataFrame:
        return _read(self.data_dir / name)

    # --- sourced ---------------------------------------------------------------
    @cached_property
    def games(self) -> pd.DataFrame:
        """Game Log: rostered-QB passing appearances, 2023 onward (attempts >= 1)."""
        return self._load("qb_games.csv")

    @cached_property
    def games_archive(self) -> pd.DataFrame:
        """Career Archive: earlier appearances for rostered/returning QBs (accepted yards)."""
        return self._load("qb_games_archive.csv")

    @cached_property
    def historical_games(self) -> pd.DataFrame:
        """Completed-career cohort (currently Drew Brees)."""
        return self._load("historical_qb_games.csv")

    @cached_property
    def game_context(self) -> pd.DataFrame:
        return self._load("game_context.csv")

    @cached_property
    def team_byes(self) -> pd.DataFrame:
        return self._load("team_byes.csv")

    # --- curated ---------------------------------------------------------------
    @cached_property
    def roster(self) -> pd.DataFrame:
        return self._load("curated/roster.csv").sort_values("slot").reset_index(drop=True)

    @cached_property
    def return_pool(self) -> pd.DataFrame:
        return self._load("curated/return_pool.csv")

    @cached_property
    def picks(self) -> pd.DataFrame:
        return self._load("curated/picks.csv")

    @cached_property
    def market_quotes(self) -> pd.DataFrame:
        return self._load("curated/market_quotes.csv")

    @cached_property
    def combos(self) -> pd.DataFrame:
        return self._load("curated/combos.csv")

    @cached_property
    def miss_reviews(self) -> pd.DataFrame:
        return self._load("curated/miss_reviews.csv")

    @cached_property
    def historical_players(self) -> pd.DataFrame:
        return self._load("curated/historical_players.csv")

    # --- derived helpers ---------------------------------------------------------
    @cached_property
    def career_games(self) -> pd.DataFrame:
        """Game Log ∪ Career Archive; the archive holds no rows that overlap the log."""
        return pd.concat([self.games[GAME_COLUMNS], self.games_archive[GAME_COLUMNS]],
                         ignore_index=True)

    @cached_property
    def career_players(self) -> pd.DataFrame:
        """Roster QBs followed by return-pool QBs not currently holding a roster slot."""
        extra = self.return_pool[~self.return_pool.player_id.isin(self.roster.player_id)]
        return pd.concat([self.roster[["qb", "player_id"]], extra[["qb", "player_id"]]],
                         ignore_index=True)

    @property
    def current_season(self) -> int:
        return int(self.games.season.max())

    @property
    def next_week(self) -> int:
        """The first current-season week with no recorded appearance yet."""
        cur = self.games[self.games.season == self.current_season]
        return int(cur.week.max()) + 1

    def player_id(self, qb: str) -> str:
        for df in (self.roster, self.career_games, self.historical_games):
            hit = df.loc[df.qb == qb, "player_id"]
            if len(hit):
                return hit.iloc[0]
        raise KeyError(f"unknown QB: {qb!r}")

    def team_of(self, player_id: str) -> str:
        hit = self.roster.loc[self.roster.player_id == player_id, "team"]
        if len(hit):
            return hit.iloc[0]
        g = self.games[self.games.player_id == player_id].sort_values(["season", "week"])
        return g.team.iloc[-1]

    def game_for(self, team: str, season: int, week: int) -> pd.Series | None:
        gc = self.game_context
        hit = gc[(gc.season == season) & (gc.week == week) & ((gc.home == team) | (gc.away == team))]
        return None if hit.empty else hit.iloc[0]


def load(data_dir: Path | str | None = None) -> Dataset:
    return Dataset(Path(data_dir) if data_dir else DEFAULT_DATA_DIR)
