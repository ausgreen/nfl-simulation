# Model improvements (branch `model-improve`, 2026-11-14)

Two passes over the QB passing-yards model (v3) and the play-by-play simulator, measured on held-out seasons.
- **First pass:** kept finer score bins in the simulator's tables.
- **Second pass, aimed at 4th-quarter overconfidence:** kept 4th-quarter clock phases, a logistic play-call model
  and the clock by score.

v3 is unchanged. Everything tried is recorded here with its numbers; the final table is under "Final vs baseline".

## Protocol

- **Data.** nflverse stats, schedules and 2026 play-by-play refreshed through week 10 (commit `cc5b8ac`).
- **v3.** Walk-forward: each season predicted by a model fit on 2012 through the season before. Development seasons
  2019-23 to choose; 2024-25 held out; 2026 weeks 1-10 as the last check. Metrics: bias, MAE, CRPS (mean pinball loss
  over 99 quantiles x 2), Brier on 150-300 yard lines, coverage of the 10/50/90% quantiles. "Better in" = share of
  2,000 game resamples where the variant's mean CRPS is lower. My walk-forward reproduces `wf_rows.parquet`'s
  `pregame_mu` exactly (5,406 rows, max difference 6e-14), so it is the same model the live app uses.
- **Simulator.** `experiments/model_improve/backtest.py SEASON LABEL`: the season's tables from the 5 seasons before
  it (out of sample), ratings from that season's earlier games, every game anchored to v3's walk-forward expected yards
  and the closing lines' implied points (500 draws), then simulated (2,000 draws, fixed seeds, the same for every arm)
  from kickoff and from the state nearest 45:00, 30:00, 20:00, 15:00, 10:00, 7:00, 5:00, 3:00, 2:00 and 1:00 left.
  Scored: Brier of the home win chance, P(home covers the closing spread), P(over the closing total), and every QB
  line 125-400 not yet reached by a starter still in. `compare.py BASE ARM SEASONS...` prints the table and the
  share of 2,000 game resamples in which the arm is better; `reliability.py` the 4th-quarter favorites' reliability.
  Re-running an arm reproduces it exactly (checked for both the old and new code).
- **Seasons.** Development 2023-24 (544 games), test 2025 (272) and 2026 weeks 1-10 (137). Caveat: the diagnostics
  that suggested the kept change (`snaps.py`, `drives.py`, one-step and one-drive comparisons with real plays) pooled
  2023-25, so 2025 is not perfectly untouched; 2026 is.
- **Market check.** Kalshi pregame QB prices for 2026 weeks 9-10 (games from 2026-11-01; the last quote before
  kickoff, 28 games, 38 QBs, ~500 quotes) from the history logs, against actual yards.

Scratch scripts and outputs: `data/live/analysis/model_improve/` (gitignored).

## Baseline

### v3 (walk-forward)

| Seasons | n | Bias (yds) | MAE | CRPS | Brier 150-300 | Below p10 / p50 / p90 |
|---|---|---|---|---|---|---|
| Dev 2019-23 | 2,654 | -1.6 | 58.3 | 42.19 | 0.178 | 0.105 / 0.495 / 0.910 |
| Test 2024-25 | 1,088 | -6.0 | 57.3 | 41.03 | 0.181 | 0.104 / 0.523 / 0.929 |
| 2026 wk 1-10 | 274 | +0.5 | 57.9 | 41.72 | 0.179 | 0.102 / 0.464 / 0.905 |

Calibrated overall; the 300+ yard tail is a little heavy (P(>=300) 0.174 predicted vs 0.125 real in 2025, but
under-predicted in 2018, 2019 and 2023). Since 2021 the model runs ~4 yards high on average as league passing
declines.

**Against Kalshi** (2026 weeks 9-10, 502 quotes): Brier Kalshi 0.158, v3 0.163, the live app's simulator 0.164.
Medians implied by the quotes: v3 averaged 2 yards *above* Kalshi, not below; MAE of the implied median vs actual
yards 61.3 (v3) vs 59.1 (Kalshi). The Week 10 pattern of the model sitting 20-45 yards under Kalshi is in single
QBs (Love 230 vs 265, Stroud 226 vs 254, McCarthy 186 vs 212, Minshew 175 vs 196, Penix 191 vs 226), balanced by others
where it sat well above (D. Jones +29, Purdy +23, Goff +16, Bo Nix +15). Small sample: the market is better here, but
not by a margin these quotes can locate.

### Simulator (old code, "coarse")

| Seasons | Win, in-game | Spread, in-game | Total, in-game | QB lines, pregame | QB lines, in-game | Regulation tie (sim / real) |
|---|---|---|---|---|---|---|
| 2023-24 | 0.10511 | 0.10285 | 0.10920 | 0.12697 | 0.07128 | 3.6% / 5.3% |
| 2025 | 0.11681 | 0.11610 | 0.10946 | 0.11920 | 0.06705 | 3.6% / 5.1% |
| 2026 wk 1-10 | 0.11841 | 0.12546 | 0.12599 | 0.12434 | 0.07003 | 3.7% / 8.8% |

The simulator's pregame QB-line chances match v3's own distribution (Brier within 0.0005 every season), so pregame QB
accuracy is v3's. 4th-quarter favorites are overconfident: in 2025-26, at checkpoints where the favorite had 0.95-0.99,
it lost 25.5 times against 9.2 expected; over all Q4 checkpoints with a favorite at 0.8+, 105 losses vs 77 expected.

## What was tried

### v3: no change kept

All on development seasons 2019-23, CRPS (base 42.192):

| Variant | CRPS | Better in |
|---|---|---|
| + team's passing yards (ewm, any QB) | 42.182 | 72% |
| + team passing (ewm + season), new-team flag, starts with team | 42.205 | 34% |
| + pbp volume: team dropbacks, neutral pass rate, opponent dropbacks | 42.184 (alpha 100) | 58% |
| + pbp efficiency: team and opponent yards per dropback | 42.179 (alpha 100) | 72% |
| Ridge alpha 3 / 30 / 100 (now 10) | 42.198 / 42.181 / 42.167 | 4% / 93% / 88% |
| Recency-weighted training (half-life 3 / 6 / 10 seasons) | 42.264 / 42.219 / 42.206 | 2% / 5% / 8% |
| Gradient boosting instead of ridge | 42.873 | 0% |
| Spread (sigma) also from QB's sd, starts, abs spread | 42.185 | 73% |

The two best (alpha 30, + team passing) checked once on 2024-26: CRPS 41.166 -> 41.164 and 41.158 (better in 66%,
64%). Nothing clears noise; with the public inputs on disk (form, opponent, weather, lines) v3 is at its floor,
~0.02% of CRPS separating variants. The "new team" and "few starts" leads don't hold up: in 2019-23 QBs on a new team
came in 8.9 yards *under* v3 (92 starts) and in 2024-26 QBs with 1-5 starts came in 14-20 yards under, i.e. v3 is
too high on them, not too low. What the market has that v3 lacks is news (injuries, receivers, game plans), not a
better use of the same data.

### Simulator: kept — finer score bins (`tables.set_score_bins`, default on)

The tables keyed the offense's margin as <=-17, -16..-9, -8..-1, 0, 1..8, 9..16, >=17, and the game-script
adjustments (completion-gain scale, run shift), the pass results' score effect and the 4th-down model's
interactions used an even coarser key that pooled down 9-16 with down 17+ and down 1-3 with down 4-8. Real teams
behave differently in those groups (a field goal ties or wins only within 3). Now: 9 bins (down/up 1-3 and 4-8
apart), and those effects use the full score bin.

Evidence on real plays (each season simulated one snap or one drive from every real state, 2023-24):

| 4th downs, last 5:00, offense | Field goal real / old / new | Punt real / old / new |
|---|---|---|
| down 4-8 | 3.8% / 21.0% / 5.2% | 12.5% / 16.1% / 15.5% |
| down 1-3 | 46.4% / 27.4% / 48.5% | 12.0% / 10.4% / 11.3% |
| down 9-16 | 13.5% / 10.9% / 12.6% | 2.1% / 11.7% / 5.2% |
| down 17+ | 3.7% / 5.1% / 3.7% | 18.5% / 9.1% / 18.9% |

Play-call log loss on held-out 2023-24 plays: 0.77329 -> 0.77195. Pass rate in the last 5:00 minus real, down 1-3 /
down 4-8: +7.9 / -5.8 points before, +1.5 / -2.4 after.

Backtest (Brier; "better in" = share of game resamples the new code wins):

| | Dev 2023-24 | Test 2025 | Test 2026 wk 1-10 | 2025-26 pooled |
|---|---|---|---|---|
| Win, in-game | 0.10511 -> 0.10519 (38%) | 0.11681 -> 0.11623 (93%) | 0.11841 -> 0.11777 (91%) | 0.11735 -> 0.11675 (97%) |
| Win, Q4 15:00-5:00 | 0.09943 -> 0.09934 (61%) | 0.11749 -> 0.11707 (80%) | 0.11201 -> 0.11035 (99%) | 0.11565 -> 0.11482 (98%) |
| Total, in-game | 0.10920 -> 0.10907 (62%) | 0.10946 -> 0.10809 (99.5%) | 0.12599 -> 0.12528 (84%) | 0.11501 -> 0.11386 (99.7%) |
| Spread, in-game | 0.10285 -> 0.10303 (37%) | 0.11610 -> 0.11614 (49%) | 0.12546 -> 0.12485 (74%) | 0.11916 -> 0.11899 (61%) |
| QB lines, in-game | 0.07128 -> 0.07125 (68%) | 0.06705 -> 0.06700 (73%) | 0.07003 -> 0.06995 (68%) | 0.06805 -> 0.06798 (77%) |
| QB lines, pregame | 0.12697 -> 0.12689 (67%) | 0.11920 -> 0.11895 (85%) | 0.12434 -> 0.12427 (59%) | 0.12092 -> 0.12073 (84%) |
| Win, pregame | 0.21046 -> 0.21188 (2%) | 0.21243 -> 0.21235 (53%) | 0.21578 -> 0.21625 (39%) | 0.21355 -> 0.21366 (45%) |
| Spread, pregame | 0.24984 -> 0.24973 (55%) | 0.25031 -> 0.25179 (12%) | 0.24815 -> 0.24996 (15%) | 0.24960 -> 0.25119 (6%) |
| Total, pregame | 0.25083 -> 0.25150 (20%) | 0.25002 -> 0.25074 (26%) | 0.25023 -> 0.25111 (28%) | 0.25009 -> 0.25086 (20%) |
| Regulation ties (real) | 3.6% -> 4.4% (5.3%) | 3.6% -> 4.6% (5.1%) | 3.7% -> 4.9% (8.8%) | |
| Q4 fav >= 0.8: expected / actual losses | 107.9 / 99 -> 111.3 / 98 | 49.9 / 73 -> 49.7 / 68 | 27.1 / 32 -> 27.8 / 32 | 77.0 / 105 -> 77.5 / 100 |

In-game the change helps or is neutral everywhere on the test seasons, most for totals and 4th-quarter win chances;
on development seasons it was neutral. Pregame win, spread and total chances get slightly worse in some splits
(spread 2025-26: 0.0016, better in 6%). Anchoring noise check: the old code re-run with 501 instead of 500 anchoring
draws (2023-24) moved pregame Brier by 0.0003 (win), 0.0001 (spread), 0.0004 (total), 0.0002 (QB lines) and
in-game by <= 0.0002, so the pregame losses of 0.0014-0.0016 are probably real but small; and the game resamples
don't include this anchoring noise, so "better in" overstates the certainty of any difference under ~0.0003.
Pregame chances come from anchoring to the closing lines; the change shifts how the simulator's margin distribution
falls around those means.

Against Kalshi (2026 weeks 9-10, 509 quotes on lines that are multiples of 25): Kalshi 0.1600, old 0.1640, new 0.1642.

### Simulator: tried, not kept

- **Recency-weighted tables** (`tables.SEASON_HALF_LIFE`, plays weighted 0.5^(seasons back / 2)). The league keeps
  changing (fewer snaps, more 4th-down tries, 2025's kickoff rule), so weighting recent seasons is the general way to
  follow it. Dev 2023-24: QB lines slightly better (in-game 0.07128 -> 0.07123, 82%), totals slightly better (77%),
  win chances slightly worse before the 4th quarter (0.15507 -> 0.15575, better in 5%); held-out play-call log loss
  worse (0.77195 -> 0.77400) because the effective sample shrinks. Snaps per game barely move (126.9 -> 126.6 for
  2024, real 123.7). Off by default; the machinery stays (weighted categorical tables, weighted empirical samplers,
  weighted 4th-down and field-goal fits), since it is tested and costs nothing when off.
- **Score-keyed clock table** (`tables.ELAPSED_BY_SCORE`). In the 4th quarter's 15:00-5:00, real teams down 9-16
  snap every 33.0 s and teams up 9-16 every 40.1 s, but the clock table only knew trailing / tied / leading. Keying it
  by the score bin fixes the held-out residuals (c=3: from -1.8..+1.5 s to within +-0.6 s by margin), and on
  development seasons it improved Q4 QB lines (99% of resamples). On 2025-26 it was mixed: QB lines worse pregame
  (0.12073 -> 0.12107, better in 5%) and Q1-Q3 (2%), better in the last 5:00 (99%); win chances unchanged (57%);
  Q4 totals worse (6%). Off.
- **Richer score effect in the play call** (adding distance, then distance and field position, to the call table's
  score chain): held-out call log loss worse (0.77195 -> 0.77901, 0.78570): the product of chains double-counts
  distance. Reverted.

## Second pass: 4th-quarter overconfidence

Arm labels below are `experiments/model_improve/backtest.py` VARIANTS: `coarse` = master before this branch,
`fine` = after the first pass, `q4lr` and `q4lre` = this pass. Same seeds, games and checkpoints throughout.

### Diagnosis: teams down 9-16 in the last 8:00

Each real 2023-24 snap simulated one step (and each drive to its end) from its exact state, with the game's anchored
engine parameters (`anchor_save.py`, then `snaps.py` / `drives.py` with `AN=1`). Teams down 9-16 in the last 8:00,
`fine`:

| | Real | Sim |
|---|---|---|
| Pass rate, downs 1 / 2 / 3 | 81.7% / 80.2% / 75.7% | 73.1% / 74.8% / 74.5% |
| First-down rate per snap | 28.2% | 25.1% |
| Seconds between snaps (no timeout) | 15.5 | 16.9 |
| Sacks per snap | 6.7% | 5.5% |
| 4th down: punt / first down or TD | 6.3% / 31.7% | 11.4% / 19.7% |
| Drives (n = 218): TD / FG / punt / downs | 28.0% / 11.5% / 3.2% / 23.4% | 21.3% / 8.5% / 7.3% / 27.5% |
| Net points per drive | 2.28 | 1.56 |

Per play, the tables themselves were right for this group: with no team effects, completion rate on passes (54-59%
real vs 58-59% table), gains past the line to gain on completions (52% vs 53-54%) and run conversion (42% vs 40%) match.
What was off is the calls and the clock, and both trace to **one clock phase covering the 4th quarter's 15:00-5:00**:
inside it, real teams down 9-16 pass 65% of early downs at 15:00-12:30 and 80% at 8:00-5:00, and snap every 35.1 s
then 29.7 s (2019-24). Teams up 9-16 go the other way: 50% passes falling to 36%, and 38.7 s rising to 41.5 s
between snaps. The call table and the clock table each saw only the phase average. Late in that phase the trailing
team passed too little and played too slowly, and the leader ran too little clock.

### Tried in this pass

1. **4th-quarter clock phases** (`tables.set_clock_bins([600, 450])`: 15:00-10:00, 10:00-7:30, 7:30-5:00), alone
   (`q4`), dev 2023-24 vs `fine`: neutral (win in-game 0.10519 -> 0.10522, 44%; totals in the last 5:00 worse, 1%).
   Not kept alone; it is the basis of 2 and 3.
2. **Logistic play-call model** (`tables.early_down_calls`, `CALL_MODEL = "lr"`), the next step proposed in the first
   pass. It is a multinomial logistic regression on downs 1-3 with joint one-hot terms, and it replaces the product
   of smoothed tables. The terms:
   - down x distance, down x field position, distance x field position;
   - down x clock, field position x clock, clock x score;
   - down x coarse clock x score, distance x coarse clock x score;
   - clock x offense timeouts, clock x coarse score x offense timeouts.

   The call grid gains the offense's timeouts. Held-out log loss of all calls (with the clock phases):
   0.77195 -> 0.76263 (2023-24) and 0.78460 -> 0.77437 (2025-26). Adding down x distance x field position was no
   better (downs 1-3: 0.78918 vs 0.78930); C = 0.3. Pass rate of teams down 9-16 in the last 8:00:
   72.5% -> 75.4% (real 77.2%); leaders up 9+: 20.8% -> 16.7% (real 16.4%). Per-drive points barely moved (1.51 -> 1.54
   without anchoring).
3. **Clock by score** (`ELAPSED_BY_SCORE`), which the first pass tried on the single phase and dropped, now with the
   finer phases. Seconds between snaps, down 9-16 in the last 8:00: 16.9 -> 15.2 (real 15.5).
4. **Line bias in the anchor** (`teams.ANCHOR_LINE_BIAS`: aim each team's mean points at its implied points plus the
   training seasons' average miss, since closing lines sit near the median score). Dev 2023-24 vs `q4lr`: pregame
   spread worse (0.24911 -> 0.25085, better in 2%), total unchanged. Not kept.
5. **4th-down model with the same joint terms** (timeouts included): held-out log loss worse than the current one
   (0.5670 at C = 0.3, 0.5799 at C = 1.0, against 0.5546). Not kept. Its late punts (10.0% predicted vs 6.5% real, down
   9-16 in the last 8:00) reflect the training seasons' lower go rates (see next steps).

Backtests (Brier, "better in" = share of 2,000 game resamples):

| `q4lr` vs `fine` | Dev 2023-24 | Test 2025 | Test 2026 wk 1-10 |
|---|---|---|---|
| Win, in-game | 0.10519 -> 0.10516 (54%) | 0.11623 -> 0.11644 (26%) | 0.11777 -> 0.11784 (47%) |
| Spread, in-game | 0.10303 -> 0.10279 (86%) | 0.11614 -> 0.11629 (32%) | 0.12485 -> 0.12479 (57%) |
| Total, in-game | 0.10907 -> 0.10883 (86%) | 0.10809 -> 0.10811 (48%) | 0.12528 -> 0.12536 (46%) |
| QB lines, Q4 15:00-5:00 | 0.07318 -> 0.07311 (65%) | 0.06798 -> 0.06749 (98%) | 0.07349 -> 0.07281 (98%) |
| QB lines, pregame | 0.12689 -> 0.12695 (36%) | 0.11895 -> 0.11929 (6%) | 0.12427 -> 0.12418 (61%) |
| Win, pregame | 0.21188 -> 0.21123 (84%) | 0.21235 -> 0.21111 (90%) | 0.21625 -> 0.21548 (69%) |

| `q4lre` vs `q4lr` | Dev 2023-24 | Test 2025-26 | All 2023-26 |
|---|---|---|---|
| Win, in-game | 0.10516 -> 0.10516 (52%) | 0.11691 -> 0.11662 (87%) | 0.11021 -> 0.11008 (76%) |
| Spread, in-game | 0.10279 -> 0.10293 (28%) | 0.11907 -> 0.11839 (99.4%) | 0.10984 -> 0.10963 (88%) |
| Total, in-game | 0.10883 -> 0.10889 (37%) | 0.11390 -> 0.11383 (63%) | 0.11102 -> 0.11103 (47%) |
| QB lines, in-game | 0.07123 -> 0.07109 (99%) | 0.06788 -> 0.06789 (46%) | 0.06976 -> 0.06969 (95%) |
| QB lines, pregame | 0.12695 -> 0.12669 (94%) | 0.12093 -> 0.12120 (7%) | 0.12436 -> 0.12434 (60%) |

Kept: 1 + 2 + 3 (`q4lre`, now the code's default; a re-run reproduces it exactly).
- The call model is kept on its held-out likelihood, which improved in both test periods, and on 4th-quarter QB
  lines, better in 98% of resamples in 2025 and again in 2026.
- Clock by score is kept because its held-out clock residuals are right by score and the pooled backtest favors it.

Caveats:
- On development seasons `q4lre`'s gain was in QB lines; on the test seasons it was in win and spread chances. No
  single market improved in every period.
- Clock by score had already been tried on 2025-26 in the first pass, so this second look at the same test seasons is
  not a clean test of it. 2026 alone is neutral (win 51%, spread 72%, total 74%, QB lines 42%).
- Pregame QB lines got worse on 2025 (0.11895 -> 0.11929). The line-by-line calibration is unchanged, and over
  2023-26 pooled the difference is 0.00009 (better in 24%).

### Is the Q4 overconfidence real?

This counts every 4th-quarter checkpoint where the favorite had 0.8 or more, with a 90% interval of the excess from game
resamples (`experiments/model_improve/upsets.py`):

| Season | `coarse`: losses / expected | Excess (90%) | Final `q4lre`: losses / expected | Excess (90%) |
|---|---|---|---|---|
| 2023 | 67.0 / 57.2 | +9.8 (-9.7 .. 31.0) | 59.0 / 58.5 | +0.6 (-17.0 .. 20.0) |
| 2024 | 32.0 / 50.7 | -18.7 (-33.2 .. -3.5) | 32.0 / 52.2 | -20.2 (-34.6 .. -4.8) |
| 2025 | 73.0 / 49.9 | +23.1 (0.1 .. 48.8) | 65.0 / 48.5 | +16.5 (-4.5 .. 39.1) |
| 2026 wk 1-10 | 35.0 / 27.1 | +7.9 (-9.3 .. 25.5) | 34.5 / 27.0 | +7.5 (-7.8 .. 25.1) |
| 2023-26 | 207.0 / 184.9 | +22.1 (-15.6 .. 62.8) | 190.5 / 186.1 | +4.4 (-31.3 .. 43.1) |

The first pass's "100 vs 78" was mostly 2025. In 2024 the model was *under*confident. Pooled over four seasons, the
excess went from +22 to +4, well inside its interval. Checkpoints within a game share one outcome, so these counts
are far noisier than they look: one game's comeback adds up to 7 losses.

### Pregame: what the first pass's "regression" was

- **Noise, not a defect.** The closing line sits at the median, so the simulated P(cover) and P(over) are 0.50 +- 0.02
  (sd over games). Their Brier differences of 0.001-0.002 between arms are noise around a coin flip, and the arms swap
  places between seasons. `fine` was worse than `coarse` on 2025-26 pregame spread; `q4lre` is better than both
  (0.24960 -> 0.24809, better in 95%).
- **A real, older issue: pregame win chances are too timid.** The real home-win rate rises with the spread as if
  final margins were spread about 11 points around it; the simulator's chances imply about 14.
- **Its cause: the two teams' scores are correlated the wrong way.** In the simulator they are *negatively*
  correlated (-0.12 for neutral teams, 2024 tables); in real games, positively (+0.08 in games with a spread of 3 or
  less, 2019-23). Per team the variance is about right (sd 9.5 vs 9.4-9.7 real).
- Real scores correlate through shared conditions and catch-up. In real games the Q4 margin pulls back toward zero
  against the Q1-3 margin with slope -0.064; in the simulator, -0.047.
- **Why there's no quick fix.** A shared game shock can't fix this (it only adds variance), and pseudo-count or
  smoothing changes don't touch it. The trailing team's catch-up, which the changes above partly fix, is the lever.
  Not fixed here: see next steps.

## Final vs baseline

Kept over the branch: the data refresh, finer score bins, 4th-quarter clock phases, the logistic play-call model and
the clock by score. v3 unchanged.

| | Baseline (`coarse`) | Final (`q4lre`) | Better in |
|---|---|---|---|
| v3 CRPS, test 2024-25 / 2026 | 41.03 / 41.72 | same | no v3 change |
| Win, in-game, test 2025-26 | 0.11735 | 0.11662 | 99% |
| Win, Q4 15:00-5:00, test 2025-26 | 0.11565 | 0.11484 | 96% |
| Spread, in-game, test 2025-26 | 0.11916 | 0.11839 | 94% |
| Total, in-game, test 2025-26 | 0.11501 | 0.11383 | 99% |
| QB lines, in-game, test 2025-26 | 0.06805 | 0.06789 | 89% |
| QB lines, Q4 15:00-5:00, test 2025-26 | 0.06983 | 0.06930 | 99% |
| QB lines, pregame, test 2025-26 | 0.12092 | 0.12120 | 8% (worse) |
| Win, pregame, test 2025-26 | 0.21355 | 0.21236 | 94% |
| Spread, pregame, test 2025-26 | 0.24960 | 0.24809 | 95% |
| Total, pregame, test 2025-26 | 0.25009 | 0.25157 | 6% (worse) |
| Dev 2023-24: win / total / QB lines, in-game | 0.10511 / 0.10920 / 0.07128 | 0.10516 / 0.10889 / 0.07109 | 42% / 73% / 97% |
| Play-call log loss, held-out 2023-24 | 0.77329 | 0.76263 | |
| Regulation ties, 2023-26 (real 5.8%) | 3.6% | 4.6% | |
| Q4 favorites at 0.8+, 2023-26: losses vs expected | 207 vs 185 | 190.5 vs 186 | |
| Down 9-16, last 8:00: pass rate / s per snap / pts per drive (real 77% / 15.5 / 2.28) | 72% / 16.9 / 1.51* | 75% / 15.2 / 1.58* | |
| Kalshi weeks 9-10 QB Brier (Kalshi 0.1600) | 0.1640 | 0.1642 | |

\* Without anchoring (QB multipliers from v3, no efficiency anchor). With the anchor, `fine` gave 1.56.

**Verdict on the Q4 gap.** The 4th-quarter overconfidence is substantially closed on held-out data:
- Over the four seasons, the favorites' excess losses went from +22 to +4 (of ~186 expected), inside a 90% interval
  of -31 to +43.
- In-game win Brier on the test seasons improved in 99% of game resamples.
- Most of what was left in 2025 is that season's noise; 2024 had the opposite sign.

What is not closed is at the drive level. Teams down 9-16 late still score 0.7 points per drive less than real ones.
The gap now comes from 4th downs (punts 7% vs 3%, turnovers on downs 29% vs 23%) and a lower first-down rate per snap
(25% vs 28%), not from calls or the clock.

## Remaining weaknesses and next steps

1. **Late 4th downs of trailing teams.** The 4th-down model learns the 5 training seasons' average, and go rates rose
   through 2025 (all 4th downs: 12% in 2016, 22% in 2025, 18% in 2026), so trailing teams punt late about twice as
   often as real ones. The joint-terms regression was worse on held-out log loss. It needs more data per cell, or a
   trend term that survives 2026's reversal.
2. **Score correlation.** Simulated team scores are negatively correlated and real ones positively. This widens
   pregame margins (win chances too timid: implied spread ~14 vs ~11), and it was the larger pregame issue all along.
3. **Scrambles are runs.** nflverse labels scrambles as runs, so the simulator draws them from run gains (2018-22:
   designed runs average 4.4 yards, scrambles 7.3). For teams down 9-16 late, 31% of "runs" are scrambles (8.7 yards,
   56% first downs). Modeling a scramble as a dropback outcome is the general fix. That group's overall run
   conversion is already right (42% vs 40%), so the gain is small.
4. **League trends** (fewer snaps, 4th-down aggressiveness, the 2025 kickoff rule) are followed only through team
   ratings.
5. **v3** needs new information (injuries, receivers, a season of Kalshi closing prices), not new transforms.

## Shipping notes (live app / worker)

- No interface changes: engine columns, `Setup`, states and the pages are unchanged. The engine now looks up the play
  call with the offense's timeouts, which every start state already carries (`to0` / `to1`, default 3).
- `sim_setup_<season>.pkl` rebuilds by itself, because the cache key hashes `tables.py`. It grows from ~27 MB to
  ~50 MB (the call grid has the timeouts key, stored as float32), and each worker process loads it.
- `tables.set_score_bins`, `set_clock_bins`, `CALL_MODEL`, `ELAPSED_BY_SCORE`, `SEASON_HALF_LIFE` and
  `teams.ANCHOR_LINE_BIAS` are module switches for experiments. Changing them at run time after a set-up is cached
  mismatches the cached tables, so the live app must not.
- The engine's `record=True` output also returns `timeouts` (left per team); existing callers ignore it.
- `uv run pytest` passes (65 tests, slow ones included).

