"""4th-quarter favorites (chance >= 0.8 at a Q4 checkpoint): losses vs expected, per season, with a game-resampled
90% interval of (losses - expected) and a parametric check: how often would a perfectly calibrated model show an
excess this large? (Each game's outcome redrawn from the model's own chances, holding all checkpoints of a game to
one outcome draw.)

usage: uv run python experiments/model_improve/upsets.py LABEL SEASON [SEASON ...]
"""
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "experiments/model_improve")
import compare as C  # noqa: E402

label, *seasons = sys.argv[1:]
rng = np.random.default_rng(0)
rows = []
allq = []
for s in [int(x) for x in seasons] + [None]:
    if s is None:
        q = pd.concat(allq)
        name = "pooled"
    else:
        r = C.load(s, label)
        q = r[r.check.isin([900, 600, 420, 300, 180, 120, 60])].copy()
        allq.append(q)
        name = str(s)
    fav = np.maximum(q.home_win, 1 - q.home_win).to_numpy()
    home_fav = (q.home_win >= 0.5).to_numpy()
    won = np.where(home_fav, q.final_home > q.final_away, q.final_away > q.final_home).astype(float)
    won[(q.final_home == q.final_away).to_numpy()] = 0.5
    big = fav >= 0.8
    d = pd.DataFrame({"g": q.game_id.to_numpy()[big], "exp": 1 - fav[big], "loss": 1 - won[big]})
    gg = d.groupby("g")[["exp", "loss"]].sum()
    ex = (gg.loss - gg.exp).to_numpy()
    boot = np.array([ex[rng.integers(0, len(ex), len(ex))].sum() for _ in range(2000)])
    # calibrated null: the home team wins with the model's chance at the game's last checkpoint drawn ...
    # simpler and conservative: each checkpoint's outcome drawn independently given its chance, per game summed
    null = []
    for _ in range(2000):
        draw = rng.random(len(d)) < d.exp.to_numpy()
        null.append(draw.sum() - d.exp.sum())
    null = np.array(null)
    rows.append({"season": name, "games": len(gg), "states": len(d), "expected": d.exp.sum(), "losses": d.loss.sum(),
                 "excess": ex.sum(), "90% (game resamples)": f"{np.percentile(boot, 5):.1f} .. {np.percentile(boot, 95):.1f}",
                 "P(excess this big | calibrated, states independent)": float(np.mean(null >= ex.sum()))})
print(f"{label}: Q4 favorites at 0.8+")
print(pd.DataFrame(rows).round(2).to_string(index=False))
