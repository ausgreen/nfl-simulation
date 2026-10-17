"""Transition tables for the game state machine, learned from nflverse play-by-play.

The state before a snap is binned as
    d  down 1-4                       (4)
    t  yards to go: 1, 2-3, 4-6, 7-10, 11+   (5)
    z  field position (yardline_100): 1-5, 6-10, 11-20, 21-35, 36-50, 51-65, 66-80, 81-99   (8), refined inside each
       zone as zc (the play call, 16) and zg (gains, 39: each yard inside the 20)
    c  clock phase: 1st half normal / its last 2:00 / 2nd half > 15 min / 15-5 min / 5-2 min / 2:00-0:31, then each
       half's 0:30-0:13 and last 12 seconds   (10)
    s  offense's score margin: <=-17, -16..-9, -8..-1, 0, 1..8, 9..16, >=17   (7)
    tlo, tld  timeouts left for the offense / defense (4); tox  timeout after the play: none, offense, defense (3)
Every table is filled on the full grid with hierarchical smoothing: a cell's probabilities shrink toward the next coarser
level (pseudo-count K), and empirical samplers fall back to a coarser level when a cell has few plays.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

DIMS = {"d": 4, "t": 5, "z": 8, "c": 10, "s": 7, "s3": 3, "cat": 11, "fgz": 13, "zc": 16, "zg": 39, "tox": 3, "tlo": 4, "tld": 4, "mt": 41}
T_EDGES = [1, 3, 6, 10]                       # togo <=1, <=3, <=6, <=10, else
Z_EDGES = [5, 10, 20, 35, 50, 65, 80]         # yardline_100 <=5, ..., else (81-99)
# finer zones inside z for the play call, where kick-or-punt flips within a few yards (each nests in one z bin)
ZC_EDGES = [5, 10, 20, 25, 30, 33, 35, 37, 39, 41, 44, 47, 50, 65, 80]
# finer zones inside z for gains: a gain can't exceed the distance to the goal, so pooling plays from a wide zone
# cut short the gains (and touchdowns) of plays from its far end. Each yard inside the 20, wider farther out.
ZG_EDGES = list(range(1, 21)) + [22, 24, 26, 28, 30, 32, 35, 38, 41, 44, 47, 50, 55, 60, 65, 72, 80, 90]
S_EDGES = [-17, -9, -1, 0, 8, 16]             # margin <=-17, <=-9, <=-1, ==0, <=8, <=16, else

CALLS = ["pass", "run", "punt", "fg", "kneel", "spike", "pen"]   # pen: an accepted penalty, no play
PASS_OUT = ["comp", "inc", "sack", "int", "int_td", "comp_fum", "sack_fum"]
RUN_OUT = ["run", "run_fum"]
# elapsed-time categories (clock used from this snap to the next one)
CATS = ["run", "comp", "inc", "sack", "turnover", "punt", "fg", "kneel", "spike", "score", "pen"]
K = 20.0
TD_GAIN = 100.0   # completion gain stored for touchdowns (capped at the distance to the goal when drawn)


def t_bin(togo):
    return np.searchsorted(T_EDGES, np.asarray(togo), side="left").clip(0, 4)


def z_bin(yl):
    return np.searchsorted(Z_EDGES, np.asarray(yl), side="left").clip(0, 7)


def zc_bin(yl):
    return np.searchsorted(ZC_EDGES, np.asarray(yl), side="left").clip(0, 15)


def zg_bin(yl):
    return np.searchsorted(ZG_EDGES, np.asarray(yl), side="left").clip(0, 38)


def s_bin(margin):
    return np.searchsorted(S_EDGES, np.asarray(margin), side="left").clip(0, 6)


def s3_bin(margin):
    m = np.asarray(margin)
    return np.where(m < 0, 0, np.where(m == 0, 1, 2))


def c_bin(clock):
    """Clock phase from game seconds remaining (3600 at kickoff, 1800 at halftime): 0 first half, 1 its last 2:00,
    2 third quarter, 3 15:00-5:00 left, 4 5:00-2:00, 5 2:00-0:31; then 0:30-0:13 of a half (6 first, 7 second) and its
    last 12 seconds (8, 9), when a team in range kicks on any down: in one 2:00 phase the simulator threw on most last
    snaps of the first half in field goal range (real teams kicked on 80-96%)."""
    g = np.asarray(clock)
    first = g > 1800
    h = np.where(first, g - 1800, g)
    late = np.where(h > 30, -1, np.where(h > 12, 6, 8)) + np.where(first, 0, 1)
    return np.where(h > 120, np.where(first, 0, np.where(g > 900, 2, np.where(g > 300, 3, 4))),
                    np.where(h > 30, np.where(first, 1, 5), late))


def fg_bin(yl):
    return (np.minimum(np.asarray(yl), 64) // 5).astype(int)


# coarser keys derived from finer ones, for tables too sparse for the fine ones: key -> (source key, mapping)
DERIVED = {"c4": ("c", np.array([0, 1, 2, 3, 4, 5, 1, 5, 1, 5])),   # clock: the last 2:00 of a half is one phase
           "s4": ("s", np.array([0, 0, 1, 2, 3, 3, 3])),            # margin: down 9+, down 1-8, tied, ahead
           "dd": ("d", np.array([0, 1, 2, 2]))}                     # down: 1st, 2nd, 3rd or 4th
DIMS.update({"c4": 6, "s4": 4, "dd": 3})


def _bin(bins: dict, k: str) -> np.ndarray:
    """A key's bins from cell(**bins), deriving coarse keys (DERIVED) from their source when not given."""
    if k not in bins and k in DERIVED:
        src, m = DERIVED[k]
        return m[np.asarray(bins[src], int)]
    return np.asarray(bins[k])


def _col(df: pd.DataFrame, k: str) -> np.ndarray:
    if k in DERIVED and k not in df:
        src, m = DERIVED[k]
        return m[df[src].to_numpy(int)]
    return df[k].to_numpy(int)


def _ravel(df: pd.DataFrame, keys) -> np.ndarray:
    return np.ravel_multi_index([_col(df, k) for k in keys], [DIMS[k] for k in keys])


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

    @classmethod
    def fit_product(cls, df: pd.DataFrame, outcome: str, labels: list, chains: list[list[list[str]]],
                    full: list[str]) -> "Categorical":
        """Like fit, but the prior of each `full` cell combines several smoothed chains (each fit level by level):
        the first chain times, for each other chain, its last level over its first (what it adds to keys the earlier
        chains already have), so the main effects of each chain survive even where full cells are sparse. Full cells
        are then smoothed toward that prior with K plays."""
        g = _grid(full)
        y = pd.Categorical(df[outcome], categories=labels).codes
        df = df.assign(_y=y)[y >= 0]
        onehot = np.eye(len(labels))[df._y.to_numpy()]
        prior = cls.fit(df, outcome, labels, chains[0]).probs[_ravel(g, chains[0][-1])].copy()
        for ch in chains[1:]:
            pt, base = cls.fit(df, outcome, labels, ch), cls.fit(df, outcome, labels, [ch[0]])
            prior *= pt.probs[_ravel(g, pt.keys)] / base.probs[_ravel(g, base.keys)]
        prior /= prior.sum(1, keepdims=True)
        counts = np.zeros((len(g), len(labels)))
        np.add.at(counts, _ravel(df, full), onehot)
        return cls(full, (counts + K * prior) / (counts.sum(1, keepdims=True) + K))

    def cell(self, **bins) -> np.ndarray:
        return np.ravel_multi_index([_bin(bins, k) for k in self.keys], [DIMS[k] for k in self.keys])

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
        return np.ravel_multi_index([_bin(bins, k) for k in self.keys], [DIMS[k] for k in self.keys])

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
    elapsed: Empirical           # seconds to the next snap, by whether a timeout was called (tox)
    fg_make: np.ndarray          # by fg_bin(yardline_100)
    exit_hazard: Categorical     # per starter dropback: the starter's last dropback of the game (pulled or hurt)
    kick_yl: np.ndarray          # receiving team's start after kickoffs (latest training season)
    p_xp: float
    seasons: tuple = field(default=())
    # rare kick and fumble outcomes, per kick or per lost strip sack (see kick_turnovers)
    kicks: dict = field(default_factory=dict)
    pen: Empirical | None = None # accepted penalties with no play: 2 * yards toward the goal + 1 if a new set of downs
    timeout: Categorical | None = None  # after a play: no timeout, the offense's, the defense's
    two_pt: Categorical | None = None   # go for two after a touchdown (keys mt, c)
    p_two: float = 0.5                  # two-point success rate
    onside: Categorical | None = None   # onside try on a kickoff (keys c, s of the kicking team)
    onside_rec: np.ndarray | None = None   # next snap's yardline_100 (kicking team's ball) after a recovered onside
    onside_fail: np.ndarray | None = None  # ... (receiving team's ball) after a failed one
    # game script, by coarse clock x score (script_cell): completion gains are scaled, runs shifted (yards)
    comp_scale: np.ndarray | None = None
    run_shift: np.ndarray | None = None


def prepare(p: pd.DataFrame) -> pd.DataFrame:
    """Scrimmage snaps in regulation (and accepted penalties that wipe out the play) with their bins, outcome, clock
    used and what came next."""
    p = p[p.qtr <= 4].copy()
    snap = (p.play_type.isin(["pass", "run", "punt", "field_goal", "qb_kneel", "qb_spike"])
            | ((p.play_type == "no_play") & (p.penalty == 1))) & p.down.notna()
    s = p[snap].copy()
    nxt = s.groupby(["game_id", "game_half"]).shift(-1)
    s["next_posteam"], s["next_yl"], s["next_clock"] = nxt.posteam, nxt.yardline_100, nxt.game_seconds_remaining
    s["next_down"], s["next_togo"] = nxt.down, nxt.ydstogo
    # timeouts: each team's count before this snap and the next; a drop means it called one in between
    tlo = s.posteam_timeouts_remaining.fillna(3).clip(0, 3)
    tld = s.defteam_timeouts_remaining.fillna(3).clip(0, 3)
    home = s.posteam == s.home_team
    s["to_home"], s["to_away"] = np.where(home, tlo, tld), np.where(home, tld, tlo)
    nt = s.groupby(["game_id", "game_half"])[["to_home", "to_away"]].shift(-1)
    used_home = (s.to_home - nt.to_home).fillna(0) > 0
    used_away = (s.to_away - nt.to_away).fillna(0) > 0
    off_used, def_used = np.where(home, used_home, used_away), np.where(home, used_away, used_home)
    s["tox"] = np.where(off_used, 1, np.where(def_used, 2, 0))   # timeout after this play: none, offense, defense
    s["tlo"], s["tld"] = tlo.astype(int), tld.astype(int)
    s["has_next"] = s.next_clock.notna()
    half_end = np.where(s.game_half == "Half1", 1800.0, 0.0)
    s["elapsed"] = np.where(s.has_next, s.game_seconds_remaining - s.next_clock,
                            s.game_seconds_remaining - half_end).clip(0, 60)
    margin = _pre_margin(s, p)
    s["d"] = s.down.astype(int) - 1
    s["t"] = t_bin(s.ydstogo)
    s["z"] = z_bin(s.yardline_100)
    s["zc"] = zc_bin(s.yardline_100)
    s["zg"] = zg_bin(s.yardline_100)
    s["c"] = c_bin(s.game_seconds_remaining)
    s["s"] = s_bin(margin)
    s["s3"] = s3_bin(margin)
    s["fgz"] = fg_bin(s.yardline_100)
    call = s.play_type.map({"pass": "pass", "run": "run", "punt": "punt", "field_goal": "fg", "qb_kneel": "kneel",
                            "qb_spike": "spike", "no_play": "pen"})
    s["call"] = call
    po = np.select([(s.sack == 1) & (s.fumble_lost == 1), s.sack == 1,
                    (s.interception == 1) & (s.td_team == s.defteam), s.interception == 1,
                    (s.complete_pass == 1) & (s.fumble_lost == 1), s.complete_pass == 1],
                   ["sack_fum", "sack", "int_td", "int", "comp_fum", "comp"], "inc")
    s["pass_out"] = np.where(call == "pass", po, None)
    s["run_out"] = np.where(call == "run", np.where(s.fumble_lost == 1, "run_fum", "run"), None)
    scored = (s.touchdown == 1) | (s.field_goal_result == "made") | (s.safety == 1)
    turnover = (s.interception == 1) | (s.fumble_lost == 1)
    s["cat"] = np.select([scored, turnover, call == "pass", call == "run"],
                         [CATS.index("score"), CATS.index("turnover"),
                          pd.Series(po, index=s.index).map({"comp": 1, "inc": 2, "sack": 3}).fillna(4).to_numpy(), 0],
                         pd.Series(call, index=s.index).map({"punt": 5, "fg": 6, "kneel": 7, "spike": 8, "pen": 10}).fillna(0)
                         .to_numpy()).astype(int)
    s["gain"] = np.where((call == "pass") & (s.complete_pass == 1), s.passing_yards.fillna(s.yards_gained),
                         s.yards_gained).astype(float)
    return s


def _pre_margin(s: pd.DataFrame, p: pd.DataFrame) -> pd.Series:
    """Offense's margin before the snap (score at the end of the previous row of the game)."""
    prev = p.groupby("game_id")[["total_home_score", "total_away_score"]].shift(1).fillna(0)
    home, away = prev.total_home_score.loc[s.index], prev.total_away_score.loc[s.index]
    return np.where(s.posteam == s.home_team, home - away, away - home)


def _fourth_features(df: pd.DataFrame) -> np.ndarray:
    """One-hot distance, fine zone, clock and score, plus clock x score, field range x clock x score and distance x
    score interactions (coarse clock and score: c4, s4)."""
    t, zc, c, sm = (df[k].to_numpy(int) for k in ("t", "zc", "c", "s"))
    c4, s4 = _col(df, "c4"), _col(df, "s4")
    rng_ = np.digitize(df["zc"].to_numpy(int), [8, 13])          # opp 37 or closer / 38-50 / farther
    parts = [(t, 5), (zc, 16), (c, 10), (sm, 7), (c4 * 4 + s4, 24), ((rng_ * 6 + c4) * 4 + s4, 72), (t * 4 + s4, 20)]
    return np.hstack([np.eye(k)[v] for v, k in parts])


def fourth_down_calls(s4: pd.DataFrame, cells: pd.DataFrame) -> np.ndarray:
    """P(call) on 4th down for each grid cell in `cells`: multinomial logistic regression on the 4th-down plays."""
    from sklearn.linear_model import LogisticRegression

    y = pd.Categorical(s4.call, categories=CALLS).codes
    keep = y >= 0
    lr = LogisticRegression(C=1.0, max_iter=2000).fit(_fourth_features(s4[keep]), y[keep])
    out = np.zeros((len(cells), len(CALLS)))
    out[:, lr.classes_] = lr.predict_proba(_fourth_features(cells))
    return out


SCRIPT_K = 300.0   # plays of shrinkage toward no game-script effect


def script_cell(c, s) -> np.ndarray:
    """Coarse clock x score cell (24) of the game-script gain adjustments."""
    return DERIVED["c4"][1][np.asarray(c, int)] * 4 + DERIVED["s4"][1][np.asarray(s, int)]


def script_gains(comp: pd.DataFrame, rs: pd.DataFrame, comp_gain: Empirical, run_gain: Empirical,
                 draws: int = 10) -> tuple[np.ndarray, np.ndarray]:
    """What the tables (which ignore the score) miss: per script_cell, actual completion gains over the tables'
    expectation (a ratio) and run gains minus it (yards), shrunk toward none with SCRIPT_K plays. Trailing late,
    defenses give up more on runs and completions; leaders' runs gain less."""
    rng = np.random.default_rng(0)
    yl = comp.yardline_100.to_numpy(float)
    cell = comp_gain.cell(d=comp.d.to_numpy(), t=comp.t.to_numpy(), z=comp.z.to_numpy(), zg=comp.zg.to_numpy(),
                          c=comp.c.to_numpy())
    exp = np.mean([np.minimum(comp_gain.sample(rng, cell), yl) for _ in range(draws)], axis=0)
    act = np.minimum(comp.gain.to_numpy(float), yl)
    sc = script_cell(comp.c, comp.s)
    m = exp.mean()
    num, den = np.bincount(sc, act, 24), np.bincount(sc, exp, 24)
    n = np.bincount(sc, minlength=24)
    comp_scale = (num + SCRIPT_K * m) / (den + SCRIPT_K * m)
    rm = run_gain.means()[run_gain.cell(t=rs.t, z=rs.z, zg=rs.zg)]
    rc = script_cell(rs.c, rs.s)
    run_shift = np.bincount(rc, rs.gain.to_numpy(float) - rm, 24) / (np.bincount(rc, minlength=24) + SCRIPT_K)
    return comp_scale, run_shift


def kickoff_starts(p: pd.DataFrame) -> np.ndarray:
    """Receiving team's yardline_100 on its first snap after each kickoff (onside kicks and return TDs dropped)."""
    p = p[(p.qtr <= 4) & ~(p.kick_distance <= ONSIDE_DIST)]
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


ONSIDE_DIST = 20   # a kickoff this short (yards) is an onside try


def _pre_scores(p: pd.DataFrame) -> pd.DataFrame:
    """Home and away score before each row of the game."""
    return p.groupby("game_id")[["total_home_score", "total_away_score"]].shift(1).fillna(0)


def tries(p: pd.DataFrame) -> tuple[Categorical, float]:
    """Going for two after a touchdown, by the scorer's margin after the touchdown (mt: -20..20) and clock phase;
    and how often a two-point try succeeds."""
    p = p[p.qtr <= 4]
    tr = p[(p.play_type == "extra_point") | (p.two_point_attempt == 1)]
    pre = _pre_scores(p).loc[tr.index]  # after the touchdown, before the try
    home = tr.posteam == tr.home_team
    mt = np.where(home, pre.total_home_score - pre.total_away_score, pre.total_away_score - pre.total_home_score)
    tr = tr.assign(mt=np.clip(mt, -20, 20).astype(int) + 20, c=c_bin(tr.game_seconds_remaining.fillna(0)),
                   two=(tr.two_point_attempt == 1).astype(int).astype(str))
    two = tr[tr.two == "1"]
    return (Categorical.fit(tr, "two", ["0", "1"], [["mt"], ["mt", "c"]]),
            float((two.two_point_conv_result == "success").mean()) if len(two) else 0.5)


def onside(p: pd.DataFrame) -> tuple[Categorical, np.ndarray, np.ndarray]:
    """Onside tries by the kicking team's margin (s) and clock phase; where the next snap is, from the new offense's
    side, when the kicking team recovers and when it doesn't."""
    p = p[p.qtr <= 4]
    snap = p.down.notna()
    nxt = p[["posteam", "yardline_100"]].where(snap).groupby(p.game_id).bfill().groupby(p.game_id).shift(-1)
    ko = p[(p.kickoff_attempt == 1) & p.kick_distance.notna()]
    pre = _pre_scores(p).loc[ko.index]
    kicker_home = ko.defteam == ko.home_team
    km = np.where(kicker_home, pre.total_home_score - pre.total_away_score, pre.total_away_score - pre.total_home_score)
    ko = ko.assign(s=s_bin(km), c=c_bin(ko.game_seconds_remaining), ons=(ko.kick_distance <= ONSIDE_DIST).astype(int).astype(str))
    tries_ = ko[ko.ons == "1"]
    n = nxt.loc[tries_.index]
    rec = (n.posteam == tries_.defteam).to_numpy()
    yl = n.yardline_100.to_numpy(float)
    ok = ~np.isnan(yl)
    return (Categorical.fit(ko, "ons", ["0", "1"], [["c"], ["c", "s"]]), yl[ok & rec], yl[ok & ~rec])


def kick_turnovers(p: pd.DataFrame) -> dict:
    """Per kickoff / punt: the receiving team loses a fumble or returns it for a touchdown; per lost strip sack:
    returned for a touchdown. nflverse's posteam is the receiving team on kickoffs and the kicking team on punts."""
    p = p[p.qtr <= 4]
    ko, pu = p[p.kickoff_attempt == 1], p[p.play_type == "punt"]
    sf = p[(p.sack == 1) & (p.fumble_lost == 1)]
    td = p.touchdown == 1
    return {"ko_fum": float(((ko.fumble_lost == 1) & ~td[ko.index]).mean()),
            "ko_td": float((td[ko.index] & (ko.td_team == ko.posteam)).mean()),
            "punt_fum": float(((pu.fumble_lost == 1) & ~td[pu.index]).mean()),
            "punt_td": float((td[pu.index] & (pu.td_team == pu.defteam)).mean()),
            "sack_fum_td": float((td[sf.index] & (sf.td_team == sf.defteam)).mean()) if len(sf) else 0.0}


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
    full = ["d", "t", "z", "zc", "c", "s"]
    # three parts, each adding its own effect: field position and distance; the clock given down and zone (kicks on
    # any down in range in a half's last seconds); the score given down and clock (in one nested chain the score
    # effect was shrunk away: late leads passed far more than real teams do)
    call = Categorical.fit_product(s, "call", CALLS, [[["d"], ["d", "z"], ["d", "t", "z"], ["d", "t", "z", "zc"]],
                                                      [["d", "z"], ["d", "z", "c"]], [["d", "c"], ["d", "c", "s"]]],
                                   full)
    # 4th downs on their own: kick or go depends on field position, score and clock together (down 1-3 late, kick
    # the tying field goal; trailing farther out, go for it), which a product of smoothed tables caps
    g = _grid(full)
    fourth = g.d.to_numpy() == 3
    call.probs[fourth] = fourth_down_calls(s[s.d == 3], g[fourth])
    ps = s[s.call == "pass"]
    # pass results by situation, times what the score adds given the clock (trailing late: more interceptions)
    pass_out = Categorical.fit_product(ps, "pass_out", PASS_OUT,
                                       [[["z"], ["t", "z"], ["d", "t", "z"], ["d", "t", "z", "c"]], [["c4"], ["c4", "s4"]]],
                                       ["d", "t", "z", "c", "s"])
    rs = s[s.call == "run"]
    run_out = Categorical.fit(rs, "run_out", RUN_OUT, [["z"]])
    comp = ps[ps.pass_out.isin(["comp", "comp_fum"])]
    # a touchdown's gain is the distance it had to go: store it as reaching the end zone from anywhere in its zone
    comp = comp.assign(gain=np.where(comp.gain >= comp.yardline_100, TD_GAIN, comp.gain))
    # by down: on 3rd and 4th down receivers run past the line to gain (4th and 7-10: 76% of completions reached it;
    # pooled with earlier downs, 55%)
    comp_gain = Empirical.fit(comp, "gain", [[], ["z"], ["z", "zg"], ["t", "z", "zg"], ["d", "t"], ["d", "t", "z"],
                                             ["d", "t", "z", "zg"], ["d", "t", "z", "zg", "c"]])
    run_gain = Empirical.fit(rs, "gain", [[], ["z"], ["z", "zg"], ["t", "z", "zg"]])
    sack_gain = Empirical.fit(ps[ps.pass_out.isin(["sack", "sack_fum"])], "gain", [[], ["z"]])
    comp_scale, run_shift = script_gains(comp, rs, comp_gain, run_gain)
    to = s[s.pass_out.isin(["int", "comp_fum"]) | (s.run_out == "run_fum")]
    to = to[to.has_next & (to.next_posteam != to.posteam)]
    turnover_yl = Empirical.fit(to, "next_yl", [[], ["z"]])
    pu = s[(s.call == "punt") & s.has_next & (s.next_posteam != s.posteam)]
    punt_yl = Empirical.fit(pu, "next_yl", [[], ["z"]])
    # snaps whose next snap came at a two-minute warning: the engine stops the clock there itself
    warned = s.has_next & s.next_clock.isin([1920.0, 120.0]) & (s.game_seconds_remaining > s.next_clock)
    elapsed = Empirical.fit(s[~warned], "elapsed", [[], ["cat"], ["cat", "tox"], ["cat", "tox", "c"],
                                                    ["cat", "tox", "c", "s3"]], min_n=30)
    # who calls a timeout after a play, by clock phase, score, the play and the timeouts each side has left
    tm = s[s.has_next].assign(tx=lambda x: x.tox.astype(str))
    timeout = Categorical.fit_product(tm, "tx", ["0", "1", "2"],
                                      [[["c"], ["c", "tlo", "tld"], ["c", "s", "tlo", "tld"]], [["c"], ["c", "cat"]]],
                                      ["c", "s", "cat", "tlo", "tld"])
    # an accepted penalty: yards the ball moved toward the goal and whether the offense got a new set of downs
    # (else the down is replayed), joined into one code: 2 * yards + new set
    pn = s[(s.call == "pen") & s.has_next & (s.next_posteam == s.posteam) & s.next_yl.notna()]
    adv = (pn.yardline_100 - pn.next_yl).astype(int)
    new_set = (pn.next_down == 1) & (pn.next_togo == np.minimum(10, pn.next_yl))
    pen = Empirical.fit(pn.assign(code=2 * adv + new_set.astype(int)), "code", [[], ["d"], ["d", "t"], ["d", "t", "z"]])
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
                  tuple(sorted(p.season.unique())), kick_turnovers(p), pen, timeout, *tries(p), *onside(p),
                  comp_scale, run_shift)
