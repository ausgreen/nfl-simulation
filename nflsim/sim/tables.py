"""Transition tables for the game state machine, learned from nflverse play-by-play.

The state before a snap is binned as
    d  down 1-4                       (4)
    t  yards to go: 1, 2-3, 4-6, 7-10, 11+   (5)
    z  field position (yardline_100): 1-5, 6-10, 11-20, 21-35, 36-50, 51-65, 66-80, 81-99   (8)
    c  clock phase: 1st half normal / 1st half last 2 min / 2nd half > 15 min / 15-5 min / 5-2 min / last 2 min   (6)
    s  offense's score margin: <=-17, -16..-9, -8..-1, 0, 1..8, 9..16, >=17   (7)
Every table is filled on the full grid with hierarchical smoothing: a cell's probabilities shrink toward the next coarser
level (pseudo-count K), and empirical samplers fall back to a coarser level when a cell has few plays.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

DIMS = {"d": 4, "t": 5, "z": 8, "c": 6, "s": 7, "s3": 3, "cat": 10, "fgz": 13}
T_EDGES = [1, 3, 6, 10]                       # togo <=1, <=3, <=6, <=10, else
Z_EDGES = [5, 10, 20, 35, 50, 65, 80]         # yardline_100 <=5, ..., else (81-99)
S_EDGES = [-17, -9, -1, 0, 8, 16]             # margin <=-17, <=-9, <=-1, ==0, <=8, <=16, else

CALLS = ["pass", "run", "punt", "fg", "kneel", "spike"]
PASS_OUT = ["comp", "inc", "sack", "int", "int_td", "comp_fum"]
RUN_OUT = ["run", "run_fum"]
# elapsed-time categories (clock used from this snap to the next one)
CATS = ["run", "comp", "inc", "sack", "turnover", "punt", "fg", "kneel", "spike", "score"]
K = 20.0


def t_bin(togo):
    return np.searchsorted(T_EDGES, np.asarray(togo), side="left").clip(0, 4)


def z_bin(yl):
    return np.searchsorted(Z_EDGES, np.asarray(yl), side="left").clip(0, 7)


def s_bin(margin):
    return np.searchsorted(S_EDGES, np.asarray(margin), side="left").clip(0, 6)


def s3_bin(margin):
    m = np.asarray(margin)
    return np.where(m < 0, 0, np.where(m == 0, 1, 2))


def c_bin(clock):
    """Clock phase from game seconds remaining (3600 at kickoff, 1800 at halftime)."""
    g = np.asarray(clock)
    first = g > 1800
    return np.where(first, np.where(g - 1800 > 120, 0, 1),
                    np.where(g > 900, 2, np.where(g > 300, 3, np.where(g > 120, 4, 5))))


def fg_bin(yl):
    return (np.minimum(np.asarray(yl), 64) // 5).astype(int)


def _ravel(df: pd.DataFrame, keys) -> np.ndarray:
    return np.ravel_multi_index([df[k].to_numpy(int) for k in keys], [DIMS[k] for k in keys])


def _grid(keys) -> pd.DataFrame:
    """Every cell of the grid over `keys`, in ravel order."""
    shape = [DIMS[k] for k in keys]
    idx = np.indices(shape).reshape(len(keys), -1)
    return pd.DataFrame({k: idx[i] for i, k in enumerate(keys)})


def sample_rows(rng, probs: np.ndarray) -> np.ndarray:
    """One draw per row of a (rows, outcomes) probability matrix."""
    cum = np.cumsum(probs, axis=1)
    return (rng.random(len(probs))[:, None] * cum[:, -1:] > cum[:, :-1]).sum(1)


@dataclass
class Categorical:
    """P(outcome | cell) on the full grid of `levels[-1]`, smoothed level by level."""
    keys: list
    probs: np.ndarray            # (cells, outcomes), rows sum to 1

    @classmethod
    def fit(cls, df: pd.DataFrame, outcome: str, labels: list, levels: list[list[str]]) -> "Categorical":
        y = pd.Categorical(df[outcome], categories=labels).codes
        df = df.assign(_y=y)[y >= 0]
        onehot = np.eye(len(labels))[df._y.to_numpy()]
        prior = onehot.mean(0) + 1e-6
        prior = prior / prior.sum()
        prev_keys, prev = [], np.tile(prior, (1, 1))
        for keys in levels:
            g = _grid(keys)
            counts = np.zeros((len(g), len(labels)))
            np.add.at(counts, _ravel(df, keys), onehot)
            parent = prev[_ravel(g, prev_keys)] if prev_keys else np.tile(prior, (len(g), 1))
            prev = (counts + K * parent) / (counts.sum(1, keepdims=True) + K)
            prev_keys = keys
        return cls(prev_keys, prev)

    def cell(self, **bins) -> np.ndarray:
        return np.ravel_multi_index([np.asarray(bins[k]) for k in self.keys], [DIMS[k] for k in self.keys])

    def sample(self, rng, cell) -> np.ndarray:
        return sample_rows(rng, self.probs[cell])


@dataclass
class Empirical:
    """Sample a value from the plays in a cell, falling back to coarser levels when a cell has < min_n plays."""
    keys: list
    values: np.ndarray
    offset: np.ndarray           # per full-grid cell
    count: np.ndarray

    @classmethod
    def fit(cls, df: pd.DataFrame, value: str, levels: list[list[str]], min_n: int = 40) -> "Empirical":
        full = levels[-1]
        g = _grid(full)
        offset = np.zeros(len(g), dtype=np.int64)
        count = np.zeros(len(g), dtype=np.int64)
        chunks, pos = [], 0
        assigned = np.zeros(len(g), dtype=bool)
        for keys in reversed(levels):  # most specific first
            cell_of_play = _ravel(df, keys) if keys else np.zeros(len(df), dtype=np.int64)
            cell_of_grid = _ravel(g, keys) if keys else np.zeros(len(g), dtype=np.int64)
            order = np.argsort(cell_of_play, kind="stable")
            vals = df[value].to_numpy(float)[order]
            n_cells = int(np.prod([DIMS[k] for k in keys])) if keys else 1
            cnt = np.bincount(cell_of_play, minlength=n_cells)
            off = np.concatenate([[0], np.cumsum(cnt)[:-1]]) + pos
            take = ~assigned & ((cnt[cell_of_grid] >= min_n) | (len(keys) == 0))
            offset[take], count[take] = off[cell_of_grid[take]], cnt[cell_of_grid[take]]
            assigned |= take
            chunks.append(vals)
            pos += len(vals)
        return cls(full, np.concatenate(chunks), offset, np.maximum(count, 1))

    def cell(self, **bins) -> np.ndarray:
        return np.ravel_multi_index([np.asarray(bins[k]) for k in self.keys], [DIMS[k] for k in self.keys])

    def sample(self, rng, cell) -> np.ndarray:
        return self.values[self.offset[cell] + (rng.random(len(cell)) * self.count[cell]).astype(np.int64)]

    def means(self) -> np.ndarray:
        """Mean value per full-grid cell (of the plays that cell samples from)."""
        cs = np.concatenate([[0.0], np.cumsum(self.values)])
        return (cs[self.offset + self.count] - cs[self.offset]) / self.count


@dataclass
class Tables:
    call: Categorical
    pass_out: Categorical
    run_out: Categorical
    comp_gain: Empirical
    run_gain: Empirical
    sack_gain: Empirical
    turnover_yl: Empirical       # new offense's yardline_100 after an interception or lost fumble
    punt_yl: Empirical           # receiving team's yardline_100 after a punt
    elapsed: Empirical           # seconds to the next snap
    fg_make: np.ndarray          # by fg_bin(yardline_100)
    exit_hazard: Categorical     # per starter dropback: the starter's last dropback of the game (pulled or hurt)
    kick_yl: np.ndarray          # receiving team's start after kickoffs (latest training season)
    p_xp: float
    seasons: tuple = field(default=())


def prepare(p: pd.DataFrame) -> pd.DataFrame:
    """Scrimmage snaps in regulation with their bins, outcome, clock used and what came next."""
    p = p[p.qtr <= 4].copy()
    snap = p.play_type.isin(["pass", "run", "punt", "field_goal", "qb_kneel", "qb_spike"]) & p.down.notna()
    s = p[snap].copy()
    nxt = s.groupby(["game_id", "game_half"]).shift(-1)
    s["next_posteam"], s["next_yl"], s["next_clock"] = nxt.posteam, nxt.yardline_100, nxt.game_seconds_remaining
    s["has_next"] = s.next_clock.notna()
    half_end = np.where(s.game_half == "Half1", 1800.0, 0.0)
    s["elapsed"] = np.where(s.has_next, s.game_seconds_remaining - s.next_clock,
                            s.game_seconds_remaining - half_end).clip(0, 60)
    margin = _pre_margin(s, p)
    s["d"] = s.down.astype(int) - 1
    s["t"] = t_bin(s.ydstogo)
    s["z"] = z_bin(s.yardline_100)
    s["c"] = c_bin(s.game_seconds_remaining)
    s["s"] = s_bin(margin)
    s["s3"] = s3_bin(margin)
    s["fgz"] = fg_bin(s.yardline_100)
    call = s.play_type.map({"pass": "pass", "run": "run", "punt": "punt", "field_goal": "fg", "qb_kneel": "kneel",
                            "qb_spike": "spike"})
    s["call"] = call
    po = np.select([s.sack == 1, (s.interception == 1) & (s.td_team == s.defteam), s.interception == 1,
                    (s.complete_pass == 1) & (s.fumble_lost == 1), s.complete_pass == 1],
                   ["sack", "int_td", "int", "comp_fum", "comp"], "inc")
    s["pass_out"] = np.where(call == "pass", po, None)
    s["run_out"] = np.where(call == "run", np.where(s.fumble_lost == 1, "run_fum", "run"), None)
    scored = (s.touchdown == 1) | (s.field_goal_result == "made") | (s.safety == 1)
    turnover = (s.interception == 1) | (s.fumble_lost == 1)
    s["cat"] = np.select([scored, turnover, call == "pass", call == "run"],
                         [CATS.index("score"), CATS.index("turnover"),
                          pd.Series(po, index=s.index).map({"comp": 1, "inc": 2, "sack": 3}).fillna(4).to_numpy(), 0],
                         pd.Series(call, index=s.index).map({"punt": 5, "fg": 6, "kneel": 7, "spike": 8}).fillna(0)
                         .to_numpy()).astype(int)
    s["gain"] = np.where((call == "pass") & (s.complete_pass == 1), s.passing_yards.fillna(s.yards_gained),
                         s.yards_gained).astype(float)
    return s


def _pre_margin(s: pd.DataFrame, p: pd.DataFrame) -> pd.Series:
    """Offense's margin before the snap (score at the end of the previous row of the game)."""
    prev = p.groupby("game_id")[["total_home_score", "total_away_score"]].shift(1).fillna(0)
    home, away = prev.total_home_score.loc[s.index], prev.total_away_score.loc[s.index]
    return np.where(s.posteam == s.home_team, home - away, away - home)


def kickoff_starts(p: pd.DataFrame) -> np.ndarray:
    """Receiving team's yardline_100 on its first snap after each kickoff (onside recoveries and return TDs dropped)."""
    p = p[p.qtr <= 4]
    rows = []
    for _, g in p.groupby("game_id", sort=False):
        ko = g.kickoff_attempt.fillna(0).to_numpy() == 1
        snap = (g.down.notna() & g.play_type.isin(["pass", "run", "punt", "field_goal", "qb_kneel", "qb_spike"])
                ).to_numpy()
        post, yl = g.posteam.to_numpy(), g.yardline_100.to_numpy()
        for i in np.flatnonzero(ko):
            j = np.flatnonzero(snap[i + 1:])
            if len(j) and post[i + 1 + j[0]] == post[i]:
                rows.append(yl[i + 1 + j[0]])
    return np.asarray(rows, dtype=float)


def starter_exits(s: pd.DataFrame, starters: dict) -> pd.DataFrame:
    """Starter dropbacks with exit = 1 on the last one when the team had >= 3 more dropbacks without him."""
    drop = s[(s.qb_dropback == 1) & s.passer_player_id.notna()]
    out = []
    for (gid, team), g in drop.groupby(["game_id", "posteam"], sort=False):
        pid = starters.get((gid, team))
        if pid is None:
            continue
        own = (g.passer_player_id == pid).to_numpy()
        if not own.any():
            continue
        last = np.flatnonzero(own)[-1]
        exit_ = (len(own) - 1 - last) >= 3 and not own[last + 1:].any()
        x = g[own].copy()
        x["exit"] = 0
        if exit_:
            x.iloc[-1, x.columns.get_loc("exit")] = 1
        out.append(x)
    return pd.concat(out, ignore_index=True)


def fit(p: pd.DataFrame, starters: dict) -> Tables:
    s = prepare(p)
    full = ["d", "t", "z", "c", "s"]
    call = Categorical.fit(s, "call", CALLS, [["d"], ["d", "t", "z"], ["d", "t", "z", "c"], full])
    ps = s[s.call == "pass"]
    pass_out = Categorical.fit(ps, "pass_out", PASS_OUT, [["z"], ["t", "z"], ["d", "t", "z"], ["d", "t", "z", "c"]])
    rs = s[s.call == "run"]
    run_out = Categorical.fit(rs, "run_out", RUN_OUT, [["z"]])
    comp = ps[ps.pass_out.isin(["comp", "comp_fum"])]
    comp_gain = Empirical.fit(comp, "gain", [[], ["z"], ["t", "z"], ["t", "z", "c"]])
    run_gain = Empirical.fit(rs, "gain", [[], ["z"], ["t", "z"]])
    sack_gain = Empirical.fit(ps[ps.pass_out == "sack"], "gain", [[], ["z"]])
    to = s[s.pass_out.isin(["int", "comp_fum"]) | (s.run_out == "run_fum")]
    to = to[to.has_next & (to.next_posteam != to.posteam)]
    turnover_yl = Empirical.fit(to, "next_yl", [[], ["z"]])
    pu = s[(s.call == "punt") & s.has_next & (s.next_posteam != s.posteam)]
    punt_yl = Empirical.fit(pu, "next_yl", [[], ["z"]])
    elapsed = Empirical.fit(s, "elapsed", [[], ["cat"], ["cat", "c"], ["cat", "c", "s3"]], min_n=30)
    fg = s[s.call == "fg"]
    from sklearn.linear_model import LogisticRegression
    x = fg.yardline_100.to_numpy(float)[:, None]
    lr = LogisticRegression(C=1e3).fit(np.hstack([x, x ** 2 / 100]), fg.field_goal_result == "made")
    mid = np.arange(DIMS["fgz"]) * 5 + 2.0
    fg_make = lr.predict_proba(np.column_stack([mid, mid ** 2 / 100]))[:, 1]
    fg_make = np.minimum.accumulate(fg_make)  # never better from farther out
    ex = starter_exits(s, starters)
    exit_hazard = Categorical.fit(ex.assign(e=ex["exit"].astype(str)), "e", ["0", "1"], [["c"], ["c", "s"]])
    last = p[p.season == p.season.max()]
    xp = last[last.play_type == "extra_point"]
    return Tables(call, pass_out, run_out, comp_gain, run_gain, sack_gain, turnover_yl, punt_yl, elapsed, fg_make,
                  exit_hazard, kickoff_starts(last), float((xp.extra_point_result == "good").mean()),
                  tuple(sorted(p.season.unique())))
