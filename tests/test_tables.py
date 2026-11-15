"""The learned play-call table."""

import numpy as np
import pytest

from nflsim.sim import tables as T


def test_call_zones_nest_in_the_coarse_zones():
    yl = np.arange(1, 100)
    z, zc = T.z_bin(yl), T.zc_bin(yl)
    assert all(len(set(z[zc == k])) == 1 for k in np.unique(zc))
    assert zc.max() == T.DIMS["zc"] - 1


@pytest.mark.slow
def test_fourth_down_kicks_in_range_and_punts_beyond():
    from nflsim.data import load
    from nflsim.sim.pregame import season_setup

    tb = season_setup(load(), load().current_season).tb
    fg, punt = T.CALLS.index("fg"), T.CALLS.index("punt")

    def call(yl):  # 4th and 5, 2nd quarter, tied
        return tb.call.probs[tb.call.cell(d=3, t=T.t_bin(5), z=T.z_bin(yl), zc=T.zc_bin(yl), c=T.c_bin(2500),
                                          s=T.s_bin(0), tlo=3)]

    assert call(36)[fg] > call(36)[punt]   # a 54-yard try
    assert call(44)[punt] > 10 * call(44)[fg]   # a 62-yard try


@pytest.mark.slow
def test_late_leads_run_and_late_deficits_pass():
    from nflsim.data import load
    from nflsim.sim.pregame import season_setup

    tb = season_setup(load(), load().current_season).tb

    def pass_share(margin):  # 1st and 10 at midfield, 2-5 minutes left
        p = tb.call.probs[tb.call.cell(d=0, t=T.t_bin(10), z=T.z_bin(50), zc=T.zc_bin(50), c=T.c_bin(200),
                                       s=T.s_bin(margin), tlo=3)]
        return p[0] / (p[0] + p[1])

    assert pass_share(-12) > 0.8 and pass_share(10) < 0.2   # real teams: 91% and 7%


@pytest.mark.slow
def test_overtime_after_a_tied_regulation():
    from nflsim.data import load
    from nflsim.sim import engine as E
    from nflsim.sim.pregame import season_setup

    tb = season_setup(load(), load().current_season).tb
    tied = E.kickoff_start().assign(clock=30.0, kick=False, yl=75.0, down=1, togo=10.0)
    _, reg = E.simulate(tb, tied, 4000, seed=1, points=True, overtime=False)
    yds, pts = E.simulate(tb, tied, 4000, seed=1, points=True)
    assert (reg[..., 0] == reg[..., 1]).mean() > 0.8
    assert (pts[..., 0] == pts[..., 1]).mean() < 0.3      # regular season: an overtime can end tied
    assert yds.mean() > 0
    _, post = E.simulate(tb, tied.assign(post=True), 4000, seed=1, points=True)
    assert (post[..., 0] != post[..., 1]).all()
    # in overtime, after both teams have had the ball, the next score ends it: a field goal try wins or loses
    sd = E.kickoff_start().assign(clock=300.0, kick=False, yl=20.0, down=4, togo=8.0, ot=True, ot_poss0=True,
                                  ot_poss1=True)
    _, p = E.simulate(tb, sd, 4000, seed=2, points=True)
    assert ((p[..., 0] == 3) & (p[..., 1] == 0)).mean() > 0.6


def test_weighted_empirical_draws_in_proportion_to_the_weights():
    import pandas as pd

    df = pd.DataFrame({"z": [0, 0, 0, 1], "v": [1.0, 2.0, 3.0, 9.0], "w": [1.0, 1.0, 2.0, 1.0]})
    e = T.Empirical.fit(df, "v", [[], ["z"]], min_n=1)
    cell = e.cell(z=np.zeros(40000, int))
    draws = e.sample(np.random.default_rng(0), cell)
    assert set(np.unique(draws)) == {1.0, 2.0, 3.0}
    assert abs((draws == 3.0).mean() - 0.5) < 0.02
    assert abs(e.means()[0] - (1 + 2 + 6) / 4) < 1e-9
    # without weights: each play equally likely, the same draws as before weights existed
    u = T.Empirical.fit(df.drop(columns="w"), "v", [[], ["z"]], min_n=1)
    assert u.cw is None and abs((u.sample(np.random.default_rng(0), cell) == 3.0).mean() - 1 / 3) < 0.02


def test_season_weights_halve_per_half_life_and_average_one():
    old = T.SEASON_HALF_LIFE
    try:
        T.SEASON_HALF_LIFE = 2.0
        w = T.season_weights(np.array([2020, 2022, 2024]))
        assert np.allclose(w / w[-1], [0.25, 0.5, 1.0]) and abs(w.mean() - 1) < 1e-12
        T.SEASON_HALF_LIFE = None
        assert np.allclose(T.season_weights(np.array([2020, 2024])), 1.0)
    finally:
        T.SEASON_HALF_LIFE = old


def test_fourth_quarter_clock_splits_nest_in_the_coarse_phase():
    old = list(T.Q4_SPLITS)
    try:
        T.set_clock_bins([600, 450])
        c = T.c_bin(np.array([800, 600, 500, 450, 301, 300, 2000, 100]))
        assert list(c[:5]) == [3, 10, 10, 11, 11] and c[5] == 4 and c[6] == 0 and c[7] == 5
        assert T.DIMS["c"] == 12 and list(T.DERIVED["c4"][1][[3, 10, 11]]) == [3, 3, 3]
        T.set_clock_bins([])
        assert T.c_bin(500) == 3 and T.DIMS["c"] == 10
    finally:
        T.set_clock_bins(old)


def test_early_down_calls_follow_score_and_clock_together():
    import pandas as pd

    rng = np.random.default_rng(0)
    n = 6000
    df = pd.DataFrame({"d": rng.integers(0, 3, n), "t": rng.integers(0, 5, n), "zc": rng.integers(0, 16, n),
                       "c": rng.choice([3, 4], n), "s": rng.choice([1, 4], n), "tlo": rng.integers(0, 4, n)})
    df["z"] = T.z_bin(np.array(T.ZC_EDGES + [99]))[df.zc]
    # trailing by 9-16 (s=1) passes far more in the last 5:00 (c=4) than earlier; tied (s=4) doesn't change
    p = np.where(df.s == 1, np.where(df.c == 4, 0.9, 0.6), 0.5)
    df["call"] = np.where(rng.random(n) < p, "pass", "run")
    tb = T.early_down_calls(df)
    pr = tb.probs[tb.cell(d=1, t=3, z=4, zc=T.zc_bin(45), c=np.array([3, 4, 3, 4]), s=np.array([1, 1, 4, 4]), tlo=2)]
    share = pr[:, 0] / (pr[:, 0] + pr[:, 1])
    assert np.allclose(pr.sum(1), 1, atol=1e-5)
    assert share[1] > share[0] + 0.15 and abs(share[3] - share[2]) < 0.1


def test_fine_score_bins_split_one_score_games_at_three():
    fine = T.SCRIPT_S == "s"
    try:
        T.set_score_bins(True)
        assert T.DIMS["s"] == 9
        assert len(set(T.s_bin([-3, -4]))) == 2 and len(set(T.s_bin([3, 4]))) == 2
        assert T.s_bin(-1) == T.s_bin(-3) and T.s_bin(4) == T.s_bin(8)
        # the coarse groups (down 9+, down 1-8, tied, ahead) are unchanged
        s4 = T.DERIVED["s4"][1][T.s_bin(np.array([-20, -12, -6, -2, 0, 2, 6, 12, 20]))]
        assert list(s4) == [0, 0, 1, 1, 2, 3, 3, 3, 3]
        assert T.script_cells() == 6 * 9
        T.set_score_bins(False)
        assert T.DIMS["s"] == 7 and T.s_bin(-3) == T.s_bin(-4) and T.script_cells() == 24
    finally:
        T.set_score_bins(fine)
