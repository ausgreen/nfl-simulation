"""Team differences for the game simulator: ratings, anchoring to the betting lines, and form uncertainty.

1. Ratings. For every play, the league tables give an expected value (pass share of pass/run calls, sack rate,
   interception rate, completion rate, yards per run, seconds between snaps in normal clock phases). Each team's
   offense and defense accumulate (actual - expected) over earlier games only, fading with a half-life of HALF_LIFE
   games and halved at each new season, and shrunk toward the league with K pseudo-plays. Rates become logit shifts.
   A team on offense uses its offense rating plus the opponent's defense rating.
2. Line anchor. Per game, the simulator is run from kickoff and two numbers per team are adjusted until the simulated
   averages match: the QB's completion-gain multiplier (to his pregame expected yards, v3) and an overall efficiency
   `delta` (to the team's implied points from the over/under and spread).
3. Uncertainty. A simulation that knows each team's exact strength is overconfident. Each simulated game draws the
   QB's form (log scale of completion gains) and a team efficiency shock; their spreads are what real games vary
   beyond the simulator's own randomness, estimated on the training seasons. In-game, the QB's form is updated from
   his yards so far (normal approximation on the log scale).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import engine as E, tables as T

HALF_LIFE = 8.0      # games
SEASON_CARRY = 0.5   # weight kept from earlier seasons at each new season
SMOOTH = 20.0        # yards added to both sides of log ratios
# metric: (pseudo-plays K, logit scale?)
METRICS = {"pass": (400, True), "sack": (400, True), "int": (800, True), "comp": (400, True), "run": (300, False),
           "pace": (300, False)}


def play_residuals(tb: T.Tables, s: pd.DataFrame) -> pd.DataFrame:
    """Per-play (obs - expected) for each metric; NaN where a play doesn't count for it. `s` from tables.prepare."""
    out = pd.DataFrame({"game_id": s.game_id, "season": s.season, "week": s.week, "off": s.posteam,
                        "def": s.defteam})
    pc = tb.call.probs[tb.call.cell(d=s.d, t=s.t, z=s.z, c=s.c, s=s.s)]
    pr = s.call.isin(["pass", "run"]).to_numpy()
    out["pass"] = np.where(pr, (s.call == "pass") - pc[:, 0] / (pc[:, 0] + pc[:, 1]), np.nan)
    q = tb.pass_out.probs[tb.pass_out.cell(d=s.d, t=s.t, z=s.z, c=s.c)]
    ps = (s.call == "pass").to_numpy()
    po = s.pass_out.to_numpy()
    out["sack"] = np.where(ps, (po == "sack") - q[:, 2], np.nan)
    out["int"] = np.where(ps, np.isin(po, ["int", "int_td"]) - q[:, 3] - q[:, 4], np.nan)
    ci = ps & np.isin(po, ["comp", "inc", "comp_fum"])
    out["comp"] = np.where(ci, np.isin(po, ["comp", "comp_fum"]) - (q[:, 0] + q[:, 5]) / (q[:, 0] + q[:, 1] + q[:, 5]),
                           np.nan)
    rm = tb.run_gain.means()[tb.run_gain.cell(t=s.t, z=s.z)]
    out["run"] = np.where(s.call == "run", s.gain - rm, np.nan)
    em = tb.elapsed.means()[tb.elapsed.cell(cat=s.cat, c=s.c, s3=s.s3)]
    normal = s.c.isin([0, 2]).to_numpy() & np.isin(s.cat, [0, 1, 2]) & s.has_next.to_numpy()
    out["pace"] = np.where(normal, s.elapsed - em, np.nan)
    return out


def league_rates(tb: T.Tables, s: pd.DataFrame) -> dict:
    """Average rate of each binary metric (for converting rate shifts to logit shifts)."""
    po = s.pass_out[s.call == "pass"]
    pr = s.call[s.call.isin(["pass", "run"])]
    ci = po[po.isin(["comp", "inc", "comp_fum"])]
    return {"pass": (pr == "pass").mean(), "sack": (po == "sack").mean(), "int": po.isin(["int", "int_td"]).mean(),
            "comp": ci.isin(["comp", "comp_fum"]).mean()}


def ratings(res: pd.DataFrame, rates: dict, upcoming: int | None = None) -> pd.DataFrame:
    """Offense and defense ratings for every (game_id, team) in `res`, from that team's earlier games only.
    With `upcoming` (a season), each team also gets a row with game_id "next": its rating after its latest game,
    for a game in that season."""
    cols = list(METRICS)
    agg = {}
    for side in ("off", "def"):
        g = res.groupby(["season", "week", "game_id", side])
        sums = g[cols].sum(min_count=1).fillna(0.0)
        cnts = g[cols].count()
        agg[side] = (sums, cnts)
    lam = 0.5 ** (1.0 / HALF_LIFE)
    rows = []
    for side in ("off", "def"):
        sums, cnts = agg[side]
        keys = sums.index.to_frame(index=False).rename(columns={side: "team"})
        sv, cv = sums.to_numpy(), cnts.to_numpy(float)
        for team, idx in keys.groupby("team").groups.items():
            order = keys.loc[idx].sort_values(["season", "week"]).index
            S, N, last_season = np.zeros(len(cols)), np.zeros(len(cols)), None
            for i in order:
                season = keys.season[i]
                if last_season is not None and season != last_season:
                    S, N = S * SEASON_CARRY, N * SEASON_CARRY
                last_season = season
                rating = S / (N + np.array([METRICS[c][0] for c in cols]))
                rows.append((keys.game_id[i], team, side, *rating))
                S, N = S * lam + sv[i], N * lam + cv[i]
            if upcoming is not None:
                if last_season is not None and upcoming != last_season:
                    S, N = S * SEASON_CARRY, N * SEASON_CARRY
                rows.append(("next", team, side, *(S / (N + np.array([METRICS[c][0] for c in cols])))))
    r = pd.DataFrame(rows, columns=["game_id", "team", "side", *cols])
    for c in cols:
        if METRICS[c][1]:
            r[c] = r[c] / (rates[c] * (1 - rates[c]))
    wide = r.pivot_table(index=["game_id", "team"], columns="side", values=cols)
    wide.columns = [f"{side}_{c}" for c, side in wide.columns]
    return wide.reset_index()


def team_params(games: pd.DataFrame, rt: pd.DataFrame) -> pd.DataFrame:
    """Engine columns pass0..pace1 for games (game_id, home, away): team k's offense + the other team's defense."""
    rt = rt.set_index(["game_id", "team"])
    out = games[["game_id"]].copy()
    for k, (me, opp) in enumerate((("home", "away"), ("away", "home"))):
        o = rt.reindex(pd.MultiIndex.from_arrays([games.game_id, games[me]])).fillna(0.0).to_numpy()
        d = rt.reindex(pd.MultiIndex.from_arrays([games.game_id, games[opp]])).fillna(0.0)
        cols = list(rt.columns)
        for c in METRICS:
            out[f"{c}{k}"] = o[:, cols.index(f"off_{c}")] + d[f"def_{c}"].to_numpy()
    return out


def kickoff_rows(params: pd.DataFrame) -> pd.DataFrame:
    """Two kickoff starts per game (each team receiving first once), carrying the game's engine columns."""
    base = pd.concat([E.kickoff_start()] * len(params), ignore_index=True)
    base = base.drop(columns=[c for c in base.columns if c in params.columns])
    a = pd.concat([base, params.reset_index(drop=True)], axis=1)
    b = a.assign(pos=1, h2_receiver=0)
    return pd.concat([a, b], ignore_index=True)


def efficiency_slope(tb: T.Tables, n: int = 4000) -> np.ndarray:
    """Sensitivities for a league-average team, by finite differences: 2x2 matrix
    [[d yards / d log mult, d yards / d delta], [d points / d log mult, d points / d delta]]."""
    def run(lm, dv):
        p = pd.DataFrame({"mult0": [float(np.exp(lm))], "mult1": [1.0], "delta0": [dv], "delta1": [0.0]})
        y, pt = E.simulate(tb, kickoff_rows(p), n, seed=7, points=True)
        return np.array([y[..., 0].mean(), pt[..., 0].mean()])
    dm = (run(0.15, 0) - run(-0.15, 0)) / 0.3
    dd = (run(0, 0.25) - run(0, -0.25)) / 0.5
    return np.column_stack([dm, dd])


def noise_scales(tb: T.Tables, to_mult, starts: pd.DataFrame, team_games: pd.DataFrame, slope: np.ndarray,
                 n: int = 8000) -> dict:
    """Spreads of the per-game form draws, from training seasons.
    starts: final_yards, pregame_mu (one row per starter-game); team_games: points, implied (one row per team-game).
    tau2 = the simulator's own variance of log yards for a full game (used for the in-game update)."""
    p = pd.DataFrame({"mult0": [1.0], "mult1": [1.0]})
    y = E.simulate(tb, kickoff_rows(p), n, seed=11)
    y0 = y[..., 0].ravel()
    tau2 = float(np.var(np.log((y0 + SMOOTH) / (y0.mean() + SMOOTH))))
    real = np.log((starts.final_yards + SMOOTH) / (starts.pregame_mu + SMOOTH))
    form_sd = float(np.sqrt(max(np.var(real) - tau2, 0.0)))
    # points variance the simulator already produces, with the QB form draws on
    pf = p.assign(form_sd0=form_sd, form_sd1=form_sd)
    _, pt = E.simulate(tb, kickoff_rows(pf), n, seed=12, points=True)
    p0 = pt[..., 0].ravel()
    real_pts = float(np.var(team_games.points - team_games.implied))
    extra_pts = max(real_pts - float(np.var(p0)), 0.0)
    return {"tau2": tau2, "form_sd": form_sd, "delta_sd": float(np.sqrt(extra_pts)) / float(slope[1, 1]),
            "real_logyds_var": float(np.var(real)), "sim_pts_var": float(np.var(p0)), "real_pts_var": real_pts}


def anchor(tb: T.Tables, params: pd.DataFrame, to_mult, slope: np.ndarray, noise: dict, n: int = 500,
           iters: int = 9, seed: int = 0, damp: float = 0.8) -> pd.DataFrame:
    """Per game: mult0/1 so simulated yards match mu0/mu1, delta0/1 so simulated points match imp0/imp1.
    params: game_id, mu0, mu1, imp0, imp1 and the rating columns. Returns params with mult*, delta*, form/delta sd.
    All four targets are solved jointly (efficiency also adds yards, the multiplier also adds points, and each team's
    settings move the other team's numbers) with each game's own 4x4 sensitivity matrix, re-measured every third
    step by nudging one setting at a time; `slope` (league average, per team) is the fallback where a measured
    matrix is near singular. Steps are damped (the response is not linear far from the start)."""
    p = params.copy().reset_index(drop=True)
    p["mult0"], p["mult1"] = to_mult(p.mu0), to_mult(p.mu1)
    p["delta0"], p["delta1"] = 0.0, 0.0
    p["form_sd0"] = p["form_sd1"] = noise["form_sd"]
    p["delta_sd"] = noise["delta_sd"]
    g = len(p)

    def measure(q, sd):
        y, pt = E.simulate(tb, kickoff_rows(q), n, seed=sd, points=True)
        return (y[:g].mean(1) + y[g:].mean(1)) / 2, (pt[:g].mean(1) + pt[g:].mean(1)) / 2

    names = ["mult0", "delta0", "mult1", "delta1"]
    steps = {"mult0": 0.15, "delta0": 0.25, "mult1": 0.15, "delta1": 0.25}
    fallback = np.zeros((4, 4))
    fallback[:2, :2] = fallback[2:, 2:] = slope
    inv = None
    for it in range(iters):
        ym, pm = measure(p, seed)
        resp = np.stack([ym[:, 0], pm[:, 0], ym[:, 1], pm[:, 1]], -1)       # (games, 4): yds0, pts0, yds1, pts1
        if it % 3 == 0:  # each game's full sensitivity matrix: both teams' settings -> both teams' yards and points
            J = np.zeros((g, 4, 4))
            for j, c in enumerate(names):
                q = p.assign(**{c: p[c] * np.exp(steps[c]) if c.startswith("mult") else p[c] + steps[c]})
                y2, p2 = measure(q, seed)
                r2 = np.stack([y2[:, 0], p2[:, 0], y2[:, 1], p2[:, 1]], -1)
                J[:, :, j] = (r2 - resp) / steps[c]
            bad = np.abs(np.linalg.det(J)) < 0.1 * np.linalg.det(fallback)
            J[bad] = fallback
            inv = np.linalg.inv(J)
        target = np.stack([p.mu0, p.imp0.fillna(pd.Series(pm[:, 0])), p.mu1, p.imp1.fillna(pd.Series(pm[:, 1]))], -1)
        step = damp * np.einsum("gij,gj->gi", inv, target - resp)
        for j, c in enumerate(names):
            if c.startswith("mult"):
                p[c] = np.clip(p[c] * np.exp(step[:, j]), 0.4, 2.0)
            else:
                p[c] = np.clip(p[c] + step[:, j], -1.5, 1.5)
    ym, pm = measure(p, seed)
    p["sim_yds0"], p["sim_yds1"] = ym[:, 0], ym[:, 1]
    p["sim_pts0"], p["sim_pts1"] = pm[:, 0], pm[:, 1]
    return p


def form_posterior(yds, clock, mu, noise: dict):
    """In-game update of the QB's log form from his yards so far: (mean, sd) per state."""
    g = np.clip(1 - np.asarray(clock, float) / 3600.0, 0.0, 1.0)
    z = np.log((np.asarray(yds, float) + SMOOTH) / (np.asarray(mu, float) * g + SMOOTH))
    prior = max(noise["form_sd"] ** 2, 1e-6)
    obs = noise["tau2"] / np.maximum(g, 0.05)
    prec = 1 / prior + np.where(g > 0, 1 / obs, 0.0)
    mean = np.where(g > 0, z / obs, 0.0) / prec
    return mean, np.sqrt(1 / prec)
