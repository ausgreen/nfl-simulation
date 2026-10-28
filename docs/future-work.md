# Future work

Findings that are real but not built into the simulator yet, with what was measured and why they wait. For now
improvements stay general (better tables, ratings, calibration) rather than situation-specific rules, which add
inputs and code paths for small gains.

## In-game pass lean (game plans persist)

Tested 2026-10-28, prompted by DAL @ PHI (week 8): Dallas, usually two-thirds pass, ran 179 yards, and Prescott
threw nothing that mattered on the last two drives, finishing 13 yards short of 225.

**Finding.** A team's run/pass mix in a game carries forward. Measured against the league in the same down, distance,
score and clock (2016-25, 5,278 team-games), a team's first-half lean predicts its second-half lean (correlation 0.31;
0.20 after removing the team's season tendency, slope 0.22). Teams stick with a game plan; a run-heavy first half
does not lead to more passing later.

| First half (vs normal) | 1st-half pass rate | 2nd-half pass rate |
|---|---|---|
| Most run-heavy 20% | -12.8 pts | -2.9 pts |
| Middle | +0.3 | +0.2 |
| Most pass-heavy 20% | +12.2 | +2.5 |

**Tried.** The simulator's pass lean (`pass{k}`) is fixed before kickoff; only QB form updates in-game. The update
shifted each team's lean by `S / (W + 13)` (logit), where S sums its pass/run calls so far minus their pregame
expectation (tables + ratings) and W sums the expected variance. Fit on 2021-24 calls, it predicted 2025 calls well
(most run-heavy fifth of second-half situations: -5.3 points predicted, -5.6 actual).

**Backtest** (2025, 272 games, 1,903 states from halftime to 2:00 left, 4,000 simulations each, same seeds with and
without the update; "better in" = share of game resamples):

| | Brier without -> with | Better in |
|---|---|---|
| QB lines, halftime-Q3 | 0.0898 -> 0.0890 | 95% |
| QB lines, Q4 | 0.0562 -> 0.0562 | 52% |
| Win chance, all | 0.0811 -> 0.0813 | 14% |

**Why it waits.** The gain is small and limited to halftime through the 3rd quarter; nothing in the 4th, where the
Dallas line was decided, and win chances got slightly worse (more running reads as less efficient offense in the
simulator, while real teams often run more because it works). Late-game passing is driven by score and clock, which
the simulator already has, and by coaches' habits. Worth revisiting if the simulator gets a general in-game update of
team parameters (not one rule for pass rate), or if a later backtest shows a Q4 effect.

**Where it is.** Branch `in-game-pass-lean` (worktree `../nfl-simulation-pass-lean`): `teams.pass_calls`,
`teams.pass_shift`, `PASS_K`, the hook in `pregame.replay`, a test, and the scripts and log in
`experiments/pass_lean/`. Not wired into the live app. Not yet replayed on DAL @ PHI itself (play-by-play on disk ended
at week 6).

## League trends the 5-season tables miss (pace)

Measured 2026-10-28 while looking for why 4th-quarter win chances are overconfident.

**Finding.** Real snaps have come slower every season (no-timeout, normal clock): runs 36.7 s in 2020, 37.0 in 2024,
37.8 in 2025; completions 35.7, 37.1, 37.6. Plays per game fell with them: 128.1 (2020), 123.7 (2024), 121.8 (2025).
The tables weight 2020-24 equally, so from kickoff with neutral teams the 2025 simulator runs 126.6 snaps a game
(runs ~1 s and completions ~1 s faster than real), and about 7% more completions and 13% more punts than real.
It still scores right (46.0 points, real 46.0), and its tables match real completions within 0.1 yd in every zone.
Because the extra snaps make extra passing yards (233 at multiplier 1.0, real starters 218.5), the anchoring shrinks
every completion (multiplier ~0.92-0.94), which is part of why completion and sack rates come out a little off.

**Tried.**
- Weighting the training seasons 1,1,2,3,4 (oldest first): snaps 126.0 -> 125.6 only; it halved the late-season
  anchoring drift in 2025 (weeks 15-18 delta -0.09 -> -0.02, multiplier 1.07 -> 1.03).
- Rating the league's drift on its own, so offense and defense ratings don't both carry it: no effect (summed pace
  ratings within 0.05 s either way), reverted. Team pace ratings already absorb part of the slowdown during a season
  (+0.2 s early, +0.6 s late in 2025).

**Not a simulator problem.** The 2025 late-season drift (weeks 15-18: delta -0.1, multiplier 1.07) is the QB targets
(v3's, or a flat 218.5 in the test) running above what the ratings imply: real starters averaged 203 yards in weeks 15-18 of 2025 against v3's 219, but
v3 was close late in 2023 (221 vs 224) and 2024 (221 vs 224), so that is one season's December, not a bias to fix.

**Why it waits.** A pace trend term (each season's league residual, unshrunk, carried into the next) is the general
fix; it needs a backtest showing it helps scores or QB lines, not only snap counts.
