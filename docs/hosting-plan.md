# Hosting plan: live game companion on the web

Status (2026-10-13): phases 1–2 built and checked against a full recorded game (BUF @ LA, 2026-10-12, through
overtime): 384 in-game updates with no errors, 0.08 s median per simulation (0.17 s max). Fixes from that replay:
possession right after kickoff returns and at halftime, overtime shown without simulating, and box-score polling
(ESPN posted a completion's yards 20–60 s after the play in 23 of 24 cases). Phase 3 code is built (R2 publisher, Dockerfile, `compose.yaml`; set-up steps in `deploy.md`). Cloudflare (DNS, R2,
custom domain, Access) is set up; a first deploy to Fly.io ran but its shared CPU was throttled to 1/16 of a core, so
the worker moves to a Hetzner server.
Nothing is deployed.

## Goal

Anyone with the link can follow a live NFL game: current state, the 5 most likely states after the next snap,
win chance and each starter's passing-yard outlook, updated within a few seconds of ESPN's feed. Cost and CPU
should not grow with the number of viewers.

## Why not just put `nflsim live serve` online

Every `/api/state` request runs ~24,000 simulations (~0.4 s CPU on an M-series Mac, more on a cloud vCPU), and the
page re-requests on a timer, so CPU grows with viewers: 1,000 viewers at a 15 s refresh is roughly 30–50 cores.
Everyone watching the same game needs the same answer, though. Computing it once per new play makes the cost
depend on the number of live games (at most ~16, usually ≤ 9 at once), which is ~0.1–0.2 cores.

## Architecture

```
ESPN scoreboard (1 request / 5 s, all games)
        │
        ▼
┌─────────────────────────────┐  new play in game G?  →  fetch G's summary, simulate once
│ worker (1 small machine)    │──────────────────────────────────────────────┐
│ nflsim + data/ + caches     │                                              ▼
└─────────────────────────────┘               bucket behind a CDN (Cloudflare R2):
                                                index.html            (static page)
                                                games.json            (this week's games)
                                                state/<event>.json    (one per game)
                                                status.json           (worker heartbeat)
                                                        │
                                                        ▼
                                              viewers' browsers poll the JSON
```

- **Worker**: one always-on process. It is the only thing that talks to ESPN, runs the simulator, or holds data.
- **CDN bucket**: holds static files only. Viewers never reach the worker, so viewer count does not touch it.
- **No corrections.** The hosted version shows ESPN's feed as is; viewers cannot change down, distance, clock or
  score (decided 2026-10-12). The worker accepts no input from viewers at all, so there is nothing to abuse.
  (The local `nflsim live serve` page no longer has its corrections form either; the API still takes overrides.)

## Components to build

### 1. Split the computation from the ESPN fetch (`live/app.py`, `live/espn.py`)

- `espn.game_state(event)` fetches the scoreboard on every call. Change it to accept an already-fetched scoreboard,
  so one scoreboard request serves all games per tick.
- Pull the body of `LiveModel.compute` into `compute_state(s, overrides)` that takes a state dict. `live serve`
  keeps working unchanged on top of it.
- Add a **state signature**: (period, clock, possession, down, distance, spot, score, kickoff flag, last play text).
  The worker re-simulates a game only when this changes.

### 2. Worker (`nflsim/hosted/worker.py`, CLI `nflsim worker`)

Loop every 5 s:

1. Fetch the scoreboard; write `games.json` if it changed.
2. For each game in progress whose signature changed: fetch its summary (passers), simulate, write
   `state/<event>.json`. Also re-read each live game's summary every 10 s and re-simulate when its passing lines
   change: the box score lags the scoreboard by about a play.
3. Pregame games: compute the kickoff state once, and again hourly so line changes show up.
4. Finished games: write a final state once, then stop polling them.
5. Write `status.json` (`{"updated": ..., "live_games": n, "last_error": ...}`) every tick as a heartbeat.

At start-up: load the season's simulator set-up and warm `game_params` for every game on the scoreboard, so the
first update of a game doesn't take ~3 s.

Errors in one game are logged and skipped; they don't stop the loop. If ESPN fails, keep the last files and
record the error in `status.json`.

### 3. Publisher (`nflsim/hosted/publish.py`)

One interface, `put(path, bytes, content_type, max_age)`, with two back-ends:

- `LocalDir(path)`: for development; serve it with `python -m http.server`.
- `S3Compatible(endpoint, bucket, keys)`: works with Cloudflare R2 and AWS S3 (adds the `boto3` dependency).

Cache headers: `state/*.json` and `games.json` `max-age=3`, `status.json` `max-age=0`, `index.html` `max-age=300`.
The CDN then absorbs nearly all viewer requests.

### 4. Static page (`nflsim/hosted/index.html`)

A copy of `live_template.html` that:

- reads `games.json` and `state/<event>.json` (relative URLs, so it works in any bucket or domain);
- drops the corrections form, and the note under it becomes "ESPN's feed runs a few seconds behind TV";
- shows a **"data is stale"** banner when `status.json` is more than 60 s old, or the game's state is more than
  2 minutes behind during play;
- refreshes every 5–10 s by default (cheap now: it's a cached file).

### 5. Weekly data refresh (`nflsim refresh`, run by the worker's scheduler)

| When (ET) | What |
|---|---|
| Tue 06:00 | `nflsim fetch --since <season>` (stats, schedules, last week's play-by-play); rebuild the season set-up; delete the week's v3 prediction cache |
| Daily 09:00 in game weeks | refresh schedules (lines move) and recompute this week's v3 predictions |
| By hand, before kickoff | update `data/curated/roster.csv` when a starter changes (injury, benching) |

The roster is the weak point. The live app uses it to pick each team's QB, and nothing updates it automatically.
A later improvement: flag games where ESPN's listed or active passer differs from the roster's QB.

### 6. Packaging and deployment

- `Dockerfile`: `uv sync --frozen`, copy `nflsim/` and `data/`, `CMD nflsim worker --publish r2`. The image is
  ~300 MB, mostly the weather archive. Docker `COPY` keeps file modification times, so the simulator's set-up
  cache stays valid.
- Mount a small volume (1 GB) at `data/live/` for caches, so restarts don't rebuild them.
- Secrets (R2 keys) come from environment variables, never the image.

## Hosting choices

| Piece | Recommended | Alternative | Rough cost |
|---|---|---|---|
| Worker | Hetzner CX22 (2 vCPU, 4 GB) + Docker Compose | Hetzner server + Docker + systemd | $5–10/mo |
| Bucket + CDN | Cloudflare R2 + a custom domain on Cloudflare | AWS S3 + CloudFront | ~$0–5/mo at 10k viewers |
| Access | Public, or Cloudflare Access (free up to 50 users) for a private beta | Basic auth | $0 |

R2's free `r2.dev` URL is rate-limited and not meant for production; a custom domain needs the domain's DNS on
Cloudflare. Check current prices before signing up.

## Testing (before anything is deployed)

1. **Recorded games.** Games happen only on Thu/Sun/Mon, so add `nflsim worker --record <dir>` to save every
   ESPN response during one real game. Then add `--replay <dir>` to run the worker against that recording at
   10× speed. This becomes the main integration test and works in the off-season.
2. **Parity.** For a recorded game, the hosted `state/<event>.json` must match `live serve` without corrections
   for the same feed (same seeds → same numbers).
3. **Local end to end.** Run the worker with `LocalDir`, serve with `python -m http.server`, and open the page on a
   phone. Stop the worker and check that the stale banner appears.
4. **Load check.** Not needed for the CDN side. For the worker, measure CPU over a recorded Sunday 1 pm window
   (9 games) and make sure each update finishes in well under 5 s.

## Monitoring

- `status.json` heartbeat, checked by a free uptime service (e.g. Healthchecks.io or UptimeRobot), alerting you if
  it's older than 2 minutes during game windows.
- Fly.io restarts the machine if the process exits.
- Worker logs: one line per game update (event, play, compute time) and every error.

## Risks

| Risk | Mitigation |
|---|---|
| ESPN's API is unofficial and could change or block us | One scoreboard request per 5 s total, plus one summary per live game per 10 s or new play; keep the last good state and show the stale banner |
| Wrong QB because `roster.csv` is stale | Before-kickoff check (above); show "may be out" as now |
| Worker is a single point of failure | Automatic restart, heartbeat alerts, stale banner; a second machine isn't worth it yet |
| Model limits (no penalties, timeouts, onside kicks, overtime; overconfident in the 4th quarter) | Keep the footer's caveats on the public page; in overtime the page shows the situation and yards with no chances |
| ESPN's week numbering (e.g. Monday night in week 5) | Already handled: the app uses ESPN's season and week |

## Phases

| # | Work | Done when |
|---|---|---|
| 1 | Refactor (component 1), recorder and replay | A recorded game replays and matches `live serve` |
| 2 | Worker, publisher (local), static page | Local end-to-end test passes on a replayed game |
| 3 | Dockerfile, R2 publisher, deploy to Fly.io + R2 behind a private Cloudflare Access login | One live Sunday followed from a phone with no errors in `status.json` |
| 4 | Weekly refresh schedule, monitoring, open to the public | A full week runs without manual steps except the roster |

## Decisions (2026-10-13)

1. **Domain**: `austingreen.net` (2026-10-13), on a subdomain such as `nfl.austingreen.net`. Its DNS is at
   Squarespace (Google Cloud DNS name servers) and must move to Cloudflare for an R2 custom domain. The records to
   carry over: `www` CNAME `ausgreen.github.io` and the apex A records (GitHub Pages), Google Workspace MX records,
   and the SPF TXT record.
   DNS moves to Cloudflare; the site is `nfl.austingreen.net`.
2. **Worker host**: a Hetzner server (2 vCPU, 4 GB), Docker Compose (2026-10-15). Fly.io was tried first: its
   shared CPU gave the worker 6.5% of a core, and start-up hadn't finished after 12 minutes.
3. **Audience**: private (Cloudflare Access) to start.
