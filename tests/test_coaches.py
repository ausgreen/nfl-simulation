"""Head coaches' end-of-game habits: shrinkage toward the league and the engine columns."""

import numpy as np
import pandas as pd

from nflsim.sim import coaches as CO


def test_noise_only_coaches_shrink_to_zero_and_real_ones_keep_most_of_their_difference():
    rng = np.random.default_rng(0)
    rows = []
    for coach, rate in (("A", 0.2), ("B", 0.2), ("C", 0.2), ("D", 0.2)):  # no real differences
        rows += [{"coach": coach, "cell": 0, "y": float(rng.random() < rate)} for _ in range(60)]
    v, base = CO._shrunk(pd.DataFrame(rows), "y", "coach", ["cell"])
    assert abs(base - 0.2) < 0.05 and v.abs().max() < 0.03
    rows = []
    for coach, rate in (("A", 0.05), ("B", 0.2), ("C", 0.35), ("D", 0.5)):  # big, real differences
        rows += [{"coach": coach, "cell": 0, "y": float(rng.random() < rate)} for _ in range(400)]
    v, _ = CO._shrunk(pd.DataFrame(rows), "y", "coach", ["cell"])
    assert v["A"] < -0.1 and v["D"] > 0.1 and abs(v["D"] - v["A"]) > 0.35


def test_columns_put_the_home_coach_in_team_0_and_unknown_coaches_at_zero():
    eff = {"Home Coach": {"lead_pass": -0.3, "trail_pass": 0.1, "def_to": 0.5, "pull": 0.8}}
    cols = CO.columns(eff, "Home Coach", "New Coach")
    assert cols["lead_pass0"] == -0.3 and cols["pull0"] == 0.8
    assert all(cols[f"{c}1"] == 0.0 for c in CO.COLS)
    assert set(CO.columns(eff, None, float("nan"))) == {f"{c}{k}" for c in CO.COLS for k in (0, 1)}
