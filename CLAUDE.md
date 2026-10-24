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
uv run nflsim feed race <file.jsonl.gz> [--hours H]   # every fast source side by side, to time them (docs/data-speed.md)
uv run nflsim worker [--out site] [--replay <rec> --speed 10] [--once]   # hosted worker (see below)
uv run nflsim worker --publish r2 [--once]   # publish to R2 (needs `uv sync --extra hosted` and R2_* env vars)
uv run nflsim worker --out site --serve 8787 [--procs N]   # fastest local set-up: pages + pushed updates on :8787
deploy/push.sh <server-ip>               # deploy the worker to the Hetzner server (docs/deploy.md) — ask first
uv run pytest                            # ~11 s; `-m "not slow"` skips the tests that run the simulator
```

Kalshi's WebSocket feed needs `uv sync --extra kalshi` and `KALSHI_API_KEY_ID` + `KALSHI_PRIVATE_KEY_PATH` (environment
or `.env`; the key file lives outside the repo). Without them, prices fall back to REST polling.

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
- `hosted/` — the web version (docs/hosting-plan.md). `worker.py`: `Worker.tick()` reads the scoreboard once
  (every 2 s while a game is in progress, `live_every`; sooner when `wake` is set, which a jump in a game's Kalshi
  market does), fetches the changed games' summaries in parallel, and re-simulates a game when `espn.signature`
  changes, or when its box score (`espn.box`, re-read every 4 s while the game is in progress) catches up with a
  play (pregame: hourly), in `procs` spawned processes (`--procs`, default one per CPU; each holds its own LiveModel and
  always gets the same games, so it anchors each once; changed expectations are forgotten per game
  (`_child_forget`), a data refresh restarts them), publishing each game as its simulation finishes. It publishes `games.json`, `state/<event>.json`, `status.json` (heartbeat + per-game errors), `kalshi.json` (Kalshi's
  passing-yard prices and its game/half/quarter winner, spread and total markets, `kalshi.live_prices`, every tick; not
  in replays. The prices come from `kalshi_ws.Book`, kept current by Kalshi's WebSocket and logging every message to
  `data/live/kalshi_ws/<UTC date>.jsonl.gz`, when a key is set up (`kalshi_ws.price_source`); else from REST, which
  Kalshi's CDN caches for 15 s, with the score markets re-read every 10 s) and
  the pages; unchanged bytes are not re-published; polls every 60 s between game windows, retries a failing game
  after 60 s. `publish.py`: `LocalDir` (atomic writes), `S3Compatible` (R2 via boto3, `hosted` extra) and cache times.
  `index.html` (home page): every game at a glance, live first, from `summary.json` (`live.app.summary`);
  `game.html?event=<id>`: one game; `edges.html`: the
  model's chances vs Kalshi's prices (passing yards and score markets), largest gaps first. The score markets are
  priced in the page from each state's `scores` (simulated points per part of the game, `app.score_chances`). All read the JSON by relative URL and show a stale-data banner. Locally: `/`, `/game`, `/edges`.
  `push.py`: with `--serve PORT`, `Hub` streams every published JSON file to open pages (Server-Sent Events,
  `/events?only=...`; with `--publish local` it serves the pages too); the pages poll only while no stream is up,
  so on R2 (no route to the worker yet) they poll as before.
  No viewer input reaches the worker. Every published price and state change is also appended to
  `data/live/history/<UTC date>.jsonl.gz` (`Worker.history`, `read_history`; price records after a file's first list
  only the games that changed, `prices_at` fills them in; not in replays) for grading the model vs Kalshi.

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
- `live_v3_<season>_wNN.parquet` (weekly v3 predictions) is computed the first time a week is used. The hosted worker
  recomputes it every 3 h while a game is still to start (`LiveModel.update_preds`; games under way keep their
  numbers) and re-downloads stats, schedules and pbp daily when no game is on (`Worker.refresh`); status.json's `data`
  has both times. The local app does neither: delete the file (or run `nflsim fetch`) to recompute.
- `wf_rows.parquet` has no generator in this repo (copied from floormodel); don't delete it.

`data/curated/roster.csv` (expected starters, preferred by the live app) and `venues.csv` are maintained by hand.

## Plans

`docs/hosting-plan.md`: host the live companion as a single worker that polls ESPN, re-simulates only on state
changes, and writes static JSON to a CDN bucket. No corrections in the hosted version. Phases 1–2 (fetch/compute
split, recorder, replay, worker, local publisher, static page) are built; phase 3 (R2 publisher, Docker,
deployment) is deployed: `nfl.austingreen.net` (R2 + Cloudflare Access), worker on a Hetzner server (Docker Compose);
`docs/deploy.md` has the set-up steps.
