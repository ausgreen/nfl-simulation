# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Origin

Split out of the `floormodel` repo on 2026-10-12: a copy of `floormodel/sim/`, `floormodel/live/` and what they import
(the v3 QB passing-yards model and its dependencies). Some docstrings still name floormodel-only things that don't
exist here (`scripts/experiments/`, `reports/`, `nflsim model fetch`, "frozen by the forward tests"). `floormodel`
keeps its own copy of the model files, so changes here don't need to stay in sync with it.

## Commands

```sh
uv sync                                  # install (Python >= 3.11, deps: numpy, pandas, pyarrow, scikit-learn)
uv run nflsim live serve [--port 8765]   # live game companion, http://localhost:8765 (all games; /game?event=<id>) (127.0.0.1 only)
uv run nflsim sim game 2026_05_TB_DAL    # replay one game: pregame sim, then simulated from every play
uv run nflsim sim fetch [--since YEAR]   # refresh slim play-by-play -> data/nflverse/pbp_sim/
uv run nflsim fetch [--since YEAR]       # refresh nflverse stats + schedules, then play-by-play
uv run nflsim --csv sim game <id>        # global flags (--csv, --data DIR) go before the subcommand
uv run nflsim feed record <file.jsonl.gz> [--hours H | --once]   # record the ESPN feed for replay
uv run nflsim worker [--out site] [--replay <rec> --speed 10] [--once]   # hosted worker (see below)
uv run nflsim worker --publish r2 [--once]   # publish to R2 (needs `uv sync --extra hosted` and R2_* env vars)
deploy/push.sh <server-ip>               # deploy the worker to the Hetzner server (docs/deploy.md) — ask first
uv run pytest                            # ~11 s; `-m "not slow"` skips the tests that run the simulator
```

`$NFLSIM_DATA` or `--data` selects another data directory (default `./data`). Tests are in `tests/` (pytest, dev group); there is
no linter/formatter config.

## Architecture

Pipeline: nflverse data -> v3 expected passing yards per QB -> season set-up (learned tables + team ratings +
calibration) -> vectorized simulation from any game state -> CLI replay or live web page.

- `data.py` — `Dataset`: lazily loaded CSVs under `data/` (`cached_property` per file). Everything takes a `Dataset`;
  `load(dir)` builds one. `current_season` comes from the workbook-era `qb_games*.csv`.
- `model/` + `v3/model.py` — QB passing-yards model (Ridge on QB form, opponent defense, weather, betting lines,
  plus empirical residual distribution). v3 builds on `model/` without modifying it. `weather.py`/`weathermodel.py`
  supply weather features (Open-Meteo, rate-limited; `data/weather/archive/` is expensive to regenerate).
- `sim/tables.py` — learns transition tables (play call, result, clock used, punts, FGs, kickoffs, turnovers, starter
  exit hazard) from pbp of the 5 seasons before the predicted season.
- `sim/engine.py` — `simulate(tables, start_states, n)` runs thousands of games in parallel with numpy, one snap per
  step for every unfinished game. Conventions: teams indexed 0/1 (0 = home), `margin` = team 0 − team 1, optional
  per-team columns (`pass{k}`, `run{k}`, `delta{k}`, `form_mean{k}`, ...) shift behavior — see the module docstring.
  `calibrate` maps expected yards to a completion-gain multiplier `mult`.
- `sim/teams.py` — team ratings from play residuals, anchoring each game to v3's expected yards and the lines'
  implied points, per-game QB form noise (sized from `data/live/wf_rows.parquet`).
- `sim/pregame.py` — shared set-up: `season_setup(ds, season)` -> `Setup` (tables, yards->mult curve, ratings, noise),
  then `pregame`, `replay`, `replay_game`. `sim/states.py` converts pbp rows into simulator start states.
- `live/` — `espn.py` (scoreboard/summary feed -> state: `situation()` needs only the scoreboard, `add_summary()`
  adds passers; `signature()` says when a game's state changed), `feed.py` (where ESPN responses come from:
  `Live`, `Recorder`, or `Replay` of a recording on a virtual clock; switch with `espn.use(...)`), `app.py` (stdlib
  `ThreadingHTTPServer`; `LiveModel.compute_state(state, overrides)` returns the current state, 5 most likely
  next-snap states, the current drive's most likely outcomes (`simulate(..., drive=True)`), QB yardage chances, deterministically; `compute(event, overrides)` fetches then calls it;
  query-string overrides correct the feed's state; the page no longer has a form for them), `live_template.html` (the page).
- `hosted/` — the web version (docs/hosting-plan.md). `worker.py`: `Worker.tick()` reads the scoreboard once,
  re-simulates a game when `espn.signature` changes, or when its box score (`espn.box`, re-read every 10 s while
  the game is in progress) catches up with a play (pregame: hourly) and publishes `games.json`, `state/<event>.json`, `status.json` (heartbeat + per-game errors), `kalshi.json` (Kalshi's
  passing-yard prices, `kalshi.live_prices`, every 30 s; not in replays) and
  the pages; unchanged bytes are not re-published; polls every 60 s between game windows, retries a failing game
  after 60 s. `publish.py`: `LocalDir` (atomic writes), `S3Compatible` (R2 via boto3, `hosted` extra) and cache times.
  `index.html` (home page): every game at a glance, live first, from `summary.json` (`live.app.summary`);
  `game.html?event=<id>`: one game; `edges.html`: the
  model's passing-yard chances vs Kalshi's prices, largest gaps first. All read the JSON by relative URL and show a stale-data banner. Locally: `/`, `/game`, `/edges`.
  No viewer input reaches the worker.

Modeled since 2026-10-16: accepted penalties that wipe out the play, timeouts, two-minute warnings, overtime (NFL rules
since 2025; the live app infers whose OT possession it is from the score), strip-sack and return fumbles, return TDs,
two-point tries and onside kicks (decisions learned by score and clock), score effects on pass results and gains.
4th-down calls are a logistic regression (`tables.fourth_down_calls`); other calls a product of smoothed tables.
Not modeled: penalties on plays that stand. The final score includes overtime; the
quarter projections are regulation only. Regulation ends tied less often than real games do (3.6% vs 6.2%). Win
chances are still somewhat overconfident in the 4th quarter (2025: 11 upsets where the model expected 4).

## Caches (`data/live/`, gitignored except `wf_rows.parquet`)

- `sim_setup_<season>.pkl` is keyed by a hash of the simulator's code and the pbp files' mtimes, so editing `sim/`
  or refetching pbp triggers a rebuild (~45 s per season) on next use.
- `live_v3_<season>_wNN.parquet` (weekly v3 predictions) is computed once per week and NOT refreshed when lines
  move — delete it to recompute.
- `wf_rows.parquet` has no generator in this repo (copied from floormodel); don't delete it.

`data/curated/roster.csv` (expected starters, preferred by the live app) and `venues.csv` are maintained by hand.

## Plans

`docs/hosting-plan.md`: host the live companion as a single worker that polls ESPN, re-simulates only on state
changes, and writes static JSON to a CDN bucket. No corrections in the hosted version. Phases 1–2 (fetch/compute
split, recorder, replay, worker, local publisher, static page) are built; phase 3 (R2 publisher, Docker,
deployment) is deployed: `nfl.austingreen.net` (R2 + Cloudflare Access), worker on a Hetzner server (Docker Compose);
`docs/deploy.md` has the set-up steps.
