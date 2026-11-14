# Model improvements (branch `model-improve`, 2026-11-14)

A pass over the QB passing-yards model (v3) and the play-by-play simulator, measured on held-out seasons. One change
was kept (finer score bins in the simulator's tables); everything else tried is recorded here with its numbers.

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

## Final vs baseline

Kept: the data refresh and the finer score bins. v3 unchanged.

| | Baseline | Final | |
|---|---|---|---|
| v3 CRPS, test 2024-25 / 2026 | 41.03 / 41.72 | same | no v3 change |
| Sim win Brier in-game, 2025-26 | 0.11735 | 0.11675 | better in 97% |
| Sim total Brier in-game, 2025-26 | 0.11501 | 0.11386 | better in 99.7% |
| Sim QB lines in-game, 2025-26 | 0.06805 | 0.06798 | better in 77% |
| Sim QB lines pregame, 2025-26 | 0.12092 | 0.12073 | better in 84% |
| Sim spread pregame, 2025-26 | 0.24960 | 0.25119 | worse (better in 6%) |
| Regulation ties, 2023-26 (real 5.8%) | 3.6% | 4.6% | |
| Q4 favorites >= 0.8, 2025-26: losses vs expected | 105 vs 77 | 100 vs 78 | still overconfident |
| Kalshi weeks 9-10 QB Brier (Kalshi 0.1600) | 0.1640 | 0.1642 | |

These are small gains. The model was near its floor on the public data before this pass, and it still is.

## Remaining weaknesses and next steps

1. **4th-quarter overconfidence remains** (2025-26: 100 losses where 78 were expected among Q4 favorites at 0.8+).
   The drive comparison still shows teams down 9-16 in the last 10:00 scoring 0.7 points per drive more in real games
   than in the simulator (TD 28% vs 22%, 2023-24, se 0.19). Their play calls pass 2-3 points less than real teams in
   the 4th quarter's 15:00-5:00 (the call table's score effect is a little weak there) and they convert 4-5 points
   fewer first downs on 2nd and 3rd down. Next: a call model that learns the score x clock x down interaction
   directly (like the 4th-down logistic regression) instead of a product of smoothed tables.
2. **League trends** (fewer snaps, more 4th-down tries until 2025, the 2025 kickoff rule adding ~2.5 s after every
   score) are only followed through the team ratings. 2026 partly reversed 2025 (4th-down go rate 21.6% -> 18.3%,
   snaps 121.8 -> 123.2), so extrapolating a trend would have hurt; recency weighting did not clearly help.
3. **v3** needs new information, not new transforms of the same data: injury reports (receivers, linemen), snap
   counts of the receiving corps, or market lines for the QB's own yards (Kalshi history only starts 2026-10-18; once a
   season of closing prices exists, they are the strongest candidate feature).
4. **Pregame spread/total** got slightly worse with the change; worth re-measuring after a full season of 2026.

## Shipping notes (live app / worker)

- No interface changes: engine columns, `Setup`, states and the pages are unchanged.
- `sim_setup_<season>.pkl` rebuilds by itself (the cache key hashes `tables.py`), ~15-45 s per season the first time.
- `tables.set_score_bins(False)` and `SEASON_HALF_LIFE` / `ELAPSED_BY_SCORE` are module switches for experiments;
  changing them at run time after a set-up is cached would mismatch the cached tables, so the live app must not.
- The engine's `record=True` output also returns `timeouts` (left per team); existing callers ignore it.
- The data refresh (through week 10) is committed; the server refreshes its own data daily anyway.
