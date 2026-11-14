"""Win-chance reliability in the 4th quarter for one backtest arm: favorite's chance bins vs how often it won.

usage: uv run python experiments/model_improve/reliability.py LABEL SEASON [SEASON ...]
"""
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "experiments/model_improve")
import compare as C  # noqa: E402

label, *seasons = sys.argv[1:]
r = pd.concat([C.load(int(s), label) for s in seasons], ignore_index=True)
q4 = r[r.check.isin([900, 600, 420, 300, 180, 120, 60])].copy()
fav = np.maximum(q4.home_win, 1 - q4.home_win)
won = np.where(q4.home_win >= 0.5, q4.final_home > q4.final_away, q4.final_away > q4.final_home).astype(float)
won[(q4.final_home == q4.final_away).to_numpy()] = 0.5
q4["fav"], q4["won"] = fav, won
q4["bin"] = pd.cut(q4.fav, [0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 0.99, 1.0], include_lowest=True)
t = q4.groupby("bin", observed=True).agg(n=("won", "size"), predicted=("fav", "mean"), observed=("won", "mean"))
t["exp losses"] = (1 - t.predicted) * t.n
t["losses"] = (1 - t.observed) * t.n
print(f"{label} {seasons}: Q4 checkpoints, favorite's win chance")
print(t.round(3).to_string())
