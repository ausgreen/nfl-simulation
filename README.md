# nfl-simulation

The play-by-play NFL game simulator and the live game companion, split out of `floormodel` (2026-10-12).
The code is a copy of `floormodel/sim/`, `floormodel/live/` (app, ESPN feed, pbp loader) and what they import:
the v3 passing-yards model (`v3/model.py`, `model/`, `weather.py`, `weathermodel.py`, `nflverse.py`, `kalshi.py`)
and `data.py`. `floormodel` keeps its own copy, because its forward tests fingerprint those model files.

## Commands

```sh
uv sync
uv run nflsim live serve                 # live game companion at http://localhost:8765
uv run nflsim sim game 2026_05_TB_DAL    # replay a game through the simulator, pregame and play by play
uv run nflsim sim fetch                  # refresh play-by-play (data/nflverse/pbp_sim/)
uv run nflsim fetch --since 2026         # refresh nflverse stats + schedules, then play-by-play
uv run nflsim feed record recordings/<name>.jsonl.gz --hours 4   # save the ESPN feed for replay (live/feed.py)
uv run nflsim worker --out site          # hosted worker: poll ESPN, publish static JSON + page into site/
uv run nflsim worker --replay <recording> --speed 10   # the same, from a recorded feed
python -m http.server -d site 8000       # view the published site locally
deploy/push.sh <server-ip>               # deploy the worker to the server (docs/deploy.md)
uv run pytest                            # tests (tests/fixtures/ holds a recorded week-5 snapshot)
```

`--data <dir>` (or `$NFLSIM_DATA`) points at another data directory; `--csv` before a subcommand prints CSV.

## How it fits together

- `sim/tables.py` learns transition tables (play call, result, clock used, punts, FGs, kickoffs, turnovers,
  starter exits) from nflverse pbp for the 5 seasons before the predicted one; `engine.py` simulates thousands of
  games in parallel from any state; `teams.py` adds team ratings, anchors each game to v3's expected yards and the
  lines' implied points, and draws per-game QB form; `states.py` turns pbp rows into simulator states;
  `pregame.py` is the shared set-up.
- `live/app.py` (stdlib server, 127.0.0.1 only): ESPN scoreboard/summary -> simulator state, the 5 most likely
  states after the next snap, the most likely ways the current drive ends, QB yardage chances.
- Not modeled: penalties, timeouts (only inside clock-used samples), onside kicks, overtime. Overconfident in the
  4th quarter.

## Data (`data/`)

| Path | What | Refresh |
|---|---|---|
| `qb_games*.csv`, `game_context.csv`, `team_byes.csv`, `historical_qb_games.csv` | workbook-era game data; sets "current season" | copied from floormodel |
| `curated/roster.csv` | each team's expected starter (the live app prefers these) | by hand |
| `nflverse/*.csv` | passing stats, schedules (lines, listed starters, roofs), players, injuries, snaps | `nflsim fetch` |
| `nflverse/pbp_sim/` | slim play-by-play per season | `nflsim sim fetch` |
| `venues.csv`, `experiments/stadiums.csv` | stadium coordinates and roof types | by hand |
| `weather/archive/` | historical game weather (v3 training features) | Open-Meteo, rate limited; keep it |
| `live/wf_rows.parquet` | walk-forward pregame expectations, used to size per-game QB noise | copied; no generator here |
| `live/sim_setup_<season>.pkl`, `live/live_v3_<season>_wNN.parquet` | caches; rebuilt on first use (~45 s for a season) | automatic |

The set-up cache is keyed by the simulator's code and the pbp files' modification times, so editing `sim/` or
refetching pbp rebuilds it. Week predictions are cached once per week and are not refreshed when lines move;
delete the parquet to recompute.

v3 also adds backup QBs listed in the latest Kalshi snapshot (`data/kalshi/`, not copied). The live app keeps one
QB per team and prefers the roster's, so this does not change its output.
