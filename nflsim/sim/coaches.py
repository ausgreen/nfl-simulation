"""Head coaches' end-of-game habits, as engine team columns (sim/engine.py).

Measured on the regular-season play-by-play of the `SEASONS` seasons before the one simulated, each as a coach's
average difference from the league in the same situation, shrunk toward zero by how much coaches really differ
beyond sampling noise (method of moments), then turned into the engine's units:

    lead_pass   leading by 1-16 in the last 5:00: logit shift of pass vs run (clock-killing)
    trail_pass  trailing by 1-16 in the last 5:00: logit shift of pass vs run
    def_to      on defense, trailing by 1-16 in the last 5:00: log-odds shift of calling a timeout after a play
    pull        4th quarter, a 17+ point margin either way: log multiplier on the starter's exit hazard

Coaches with no history in the window get zeros (league behavior). In the 2016-25 play-by-play the coach-to-coach
spread beyond noise was 4 points of pass rate leading or trailing late (even/odd-season reliability r = 0.36, 0.37),
13 points of the defense's timeout rate (r = 0.80, few coaches) and 10 points of backup dropbacks in blowouts
(r = 0.49).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..data import Dataset
from . import data as D

SEASONS = 10
COLS = ("lead_pass", "trail_pass", "def_to", "pull")
MIN_N = 20  # situations a coach needs to get an estimate
CAP = 1.0


def _shrunk(df: pd.DataFrame, y: str, coach: str, cells: list[str]) -> tuple[pd.Series, float]:
    """Each coach's shrunk mean residual of `y` vs the league in the same cell, and the league rate."""
    df = df.dropna(subset=[coach])
    if df.empty:
        return pd.Series(dtype=float), 0.0
    res = df[y] - df.groupby(cells)[y].transform("mean")
    g = res.groupby(df[coach]).agg(["size", "mean", "var"])
    g = g[g["size"] >= MIN_N]
    if len(g) < 3:
        return pd.Series(dtype=float), float(df[y].mean())
    noise = g["var"] / g["size"]
    true_var = max(g["mean"].var() - noise.mean(), 0.0)
    return g["mean"] * true_var / (true_var + noise), float(df[y].mean())


def fit(ds: Dataset, season: int) -> dict[str, dict[str, float]]:
    """{coach: {col: value}} from the SEASONS regular seasons before `season`."""
    from .. import nflverse

    seasons = [s for s in range(season - SEASONS, season) if (D.sim_pbp_dir(ds) / f"pbp_{s}.parquet").exists()]
    if not seasons:
        return {}
    pbp = pd.concat([pd.read_parquet(D.sim_pbp_dir(ds) / f"pbp_{s}.parquet") for s in seasons], ignore_index=True)
    sched = nflverse.load_schedules(ds)
    if "home_coach" not in sched:
        return {}
    pbp = pbp[pbp.season_type == "REG"].merge(sched[["game_id", "home_coach", "away_coach"]], on="game_id", how="left")
    pbp = pbp.sort_values(["game_id", "play_id"]).reset_index(drop=True)
    home = pbp.posteam == pbp.home_team
    pbp["off_coach"] = np.where(home, pbp.home_coach, pbp.away_coach)
    pbp["def_coach"] = np.where(home, pbp.away_coach, pbp.home_coach)
    pbp["margin"] = np.where(home, pbp.total_home_score - pbp.total_away_score, pbp.total_away_score - pbp.total_home_score)
    late = (pbp.qtr == 4) & (pbp.game_seconds_remaining <= 300)
    pbp["tbin"] = (pbp.game_seconds_remaining > 120).astype(int)
    pbp["dbin"] = np.digitize(pbp.ydstogo.fillna(10), [4, 8])
    pbp["mbin"] = np.digitize(pbp.margin.abs(), [4, 9])
    snap = pbp.play_type.isin(["pass", "run", "qb_kneel"]) & pbp.down.between(1, 3)
    pbp["pass_call"] = (pbp.qb_dropback == 1).astype(float)
    out: dict[str, dict[str, float]] = {}

    def put(col, values, scale):  # capped at ±1 logit: a few coaches with short histories come out larger
        for c, v in values.items():
            out.setdefault(c, dict.fromkeys(COLS, 0.0))[col] = float(np.clip(v / scale, -CAP, CAP))

    cells = ["down", "dbin", "tbin", "mbin"]
    v, p = _shrunk(pbp[late & snap & pbp.margin.between(1, 16)], "pass_call", "off_coach", cells)
    put("lead_pass", v, max(p * (1 - p), 0.05))
    v, p = _shrunk(pbp[late & snap & pbp.margin.between(-16, -1)], "pass_call", "off_coach", cells)
    put("trail_pass", v, max(p * (1 - p), 0.05))

    # the defense (trailing) calls a timeout after a run that kept the clock moving: its timeouts drop by the next snap
    nxt = pbp.groupby("game_id")
    same = nxt.defteam.shift(-1) == pbp.defteam
    run = (pbp.play_type == "run") & (pbp.out_of_bounds != 1)
    rows = pbp[late & run & pbp.margin.between(1, 16) & same & (pbp.defteam_timeouts_remaining > 0)].copy()
    rows["def_timeout"] = (nxt.defteam_timeouts_remaining.shift(-1)[rows.index] < rows.defteam_timeouts_remaining).astype(float)
    v, p = _shrunk(rows, "def_timeout", "def_coach", ["tbin", "mbin", "defteam_timeouts_remaining"])
    put("def_to", v, max(p * (1 - p), 0.05))

    # blowouts: share of 4th-quarter dropbacks by someone other than the game's main passer
    db = pbp[(pbp.qb_dropback == 1) & pbp.passer_player_id.notna()]
    main = db.groupby(["game_id", "posteam"]).passer_player_id.agg(lambda s: s.value_counts().index[0]).rename("main")
    q4 = db[(db.qtr == 4) & (db.margin.abs() >= 17)].join(main, on=["game_id", "posteam"])
    q4["backup"] = (q4.passer_player_id != q4["main"]).astype(float)
    q4["side"] = np.sign(q4.margin)
    v, p = _shrunk(q4, "backup", "off_coach", ["side"])
    for c, r in v.items():  # backup share base -> base + r, as a multiplier on the exit hazard
        out.setdefault(c, dict.fromkeys(COLS, 0.0))["pull"] = float(np.clip(np.log(max(p + r, 0.02) / max(p, 0.02)), -CAP, CAP))
    return out


def columns(effects: dict, home_coach, away_coach) -> dict[str, float]:
    """Engine columns for a game: {lead_pass0, lead_pass1, ...}; team 0 is the home team."""
    out = {}
    for k, coach in ((0, home_coach), (1, away_coach)):
        e = effects.get(coach, {}) if isinstance(coach, str) else {}
        for c in COLS:
            out[f"{c}{k}"] = float(e.get(c, 0.0))
    return out
