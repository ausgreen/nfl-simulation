# Future work

Findings that are real but not built into the models yet, with what was measured and why they wait. For now
improvements stay general (better tables, ratings, calibration) rather than situation-specific rules, which add
inputs and code paths for small gains. Each section has a **Finding**, what was **Tried**, **Why it waits** and the
**Next step**. Backtest numbers and methods for the 2026-11-14 entries are in `docs/model-improvements.md`.

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
`experiments/pass_lean/`. Not wired into the live app.

**Next step.** Re-run the backtest on the current simulator: the 2026-11-14 changes (4th-quarter clock phases, the
logistic play-call model) change the calls this update adjusts, so the 2025 numbers above are from older code.
Play-by-play now runs through week 10, so DAL @ PHI can be replayed too.

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
- Recency-weighted tables (2026-11-14; half-life 2 seasons, `tables.SEASON_HALF_LIFE`), backtested on 2023-24: QB
  lines and totals a little better, win chances before the 4th quarter a little worse, held-out play-call log loss
  worse. Left off; the switch stays in the code.

**Not a simulator problem.** The 2025 late-season drift (weeks 15-18: delta -0.1, multiplier 1.07) is the QB targets
(v3's, or a flat 218.5 in the test) running above what the ratings imply: real starters averaged 203 yards in weeks
15-18 of 2025 against v3's 219, but v3 was close late in 2023 (221 vs 224) and 2024 (221 vs 224), so that is one
season's December, not a bias to fix.

**Update 2026-11-14.** 2026 partly reversed 2025: runs 37.4 s and completions 36.2 s per snap, 123.2 snaps a game,
4th-down go rate 18.3% (2025: 21.6%; 2016: 12.1%). A carried-forward 2025 residual would have overshot 2026. Also
seen: since the 2025 kickoff rule the clock after a score runs to the next snap in 9.9 s (2020-24: 6.4-7.7 s), ~20 s
a game the tables (2021-25) only partly have.

**Why it waits.** A pace trend term (each season's league residual, unshrunk, carried into the next) is the general
fix, but 2026's reversal shows a carried residual can overshoot, and it needs a backtest showing it helps scores or
QB lines, not only snap counts.

**Next step.** Rerun the snap and clock comparison once 2026 is complete; if the trend resumes, backtest the trend term
on 2024-26.

## Late deficits: teams down 9-16 still score too little

Measured 2026-11-14 (second pass in `docs/model-improvements.md`).

**Already built.** The calls and the clock were the first cause: the 4th quarter's 15:00-5:00 was one clock phase,
though real trailing teams pass and hurry more and more within it. The phase is now split at 10:00 and 7:30, calls
on downs 1-3 come from a logistic regression (score x clock x down x distance, timeouts), and the clock is keyed by
score. For teams down 9-16 in the last 8:00: pass rate 72% -> 75% (real 77%), 16.9 s -> 15.2 s between snaps (real
15.5). 4th-quarter favorites' excess losses over 2023-26: +22 -> +4 of ~186 expected.

**Finding.** Those teams' drives still score about 0.7 points less than real ones (TD 21% vs 28%, 218 drives
2023-24). Per play, completions, gains past the sticks and runs are right for this group when team effects are off.
The remaining gap is in:
- **4th downs.** They punt 7% of drives vs 3% real, and turn it over on downs 29% vs 23%. The 4th-down model learns
  the training seasons' lower go rates (all 4th downs: 12% in 2016, 22% in 2025, 18% in 2026).
- **First downs.** They convert fewer per snap: 25% vs 28%.
- **Scrambles.** nflverse labels scrambles as runs, so the simulator draws them from run gains. Designed runs average
  4.4 yards and scrambles 7.3 (2018-22). For teams down 9-16 late, 31% of "runs" are scrambles (8.7 yards, 56% first
  downs), yet their overall run conversion is right (42% vs 40%).

**Tried.** The play-call model's joint-terms regression applied to 4th-down calls: worse held-out log loss (0.567 vs
0.555 for the current model). Not kept.

**Why it waits.** The 4th-down fix needs more data per cell, or a trend term that survives 2026's reversal (see the
pace section). Scrambles as their own dropback outcome is the general fix for the third item. It's a modest gain for
a change that touches the engine's events and the team ratings.

**Next step.** Start with 4th downs, the largest part of the gap.

## Score correlation between the teams

Measured 2026-11-14.

**Finding.** Simulated team scores are negatively correlated (-0.12, neutral teams, 2024 tables); real ones
positively (+0.08 in games with a spread of 3 or less, 2019-23). Per team the variance is right (sd 9.5 vs 9.4-9.7).
The margin is therefore too spread out pregame: win chances imply a margin spread of ~14 points around the spread,
real outcomes ~11. Real catch-up is stronger: the Q4 margin pulls back toward zero against the Q1-3 margin with slope
-0.064 real, -0.047 simulated.

**Tried.** Nothing built. Pseudo-count and smoothing changes to the tables don't touch it.

**Why it waits.** A shared efficiency shock for both teams would only add variance. The fix has to come from game
dynamics, which is a larger project: trailing teams' catch-up efficiency (partly addressed by the late-deficit work
above), and leaders easing off.

**Next step.** Re-measure the correlation on the current code; then the late-deficit fixes, which are the first lever.

## QB passing yards (v3) needs new information

Measured 2026-11-14.

**Finding.** Against Kalshi's last pregame prices (2026 weeks 9-10, ~500 quotes), Kalshi's Brier was 0.158 and v3's
0.163. v3's implied median averaged 2 yards *above* Kalshi's, with large misses either way on single QBs. QBs on a
new team and QBs with 1-5 starts came in 9-20 yards *under* v3, so v3 is too high on them, not too low.

**Tried.** Eight variants on 2019-23 (team passing, play-by-play volume and efficiency, ridge strength, recency
weighting, gradient boosting, a spread model); none cleared noise (CRPS differences ~0.02%).

**Why it waits.** With form, opponent, weather and lines, v3 is at its floor. What the market has is news.

**Next step.** New inputs: injuries and receivers available, or a season of Kalshi closing prices as a feature.
