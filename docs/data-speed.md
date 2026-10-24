# Getting the data faster

How to get both inputs — what happened in the game, and what Kalshi's prices are — as fast as possible, and what the
model and the pages do with them after that. Developer time and complexity are left out on purpose; cost and
legality are not. Written 2026-10-20, after the WAS @ SF and DAL @ GB games.

## Summary

1. **Kalshi prices: switch to Kalshi's WebSocket feed (free, needs an API key).** Today's prices reach us up to ~20 s
   late, because the public REST endpoint we poll is cached for 15 s by Kalshi's CDN and we poll every 5 s. The
   WebSocket pushes every price change and trade as it happens, at no cost. This is the single largest fix, and it
   also makes the history log good enough to answer "who reacts first".
2. **Game data: test the free fast sources before paying for any.** Kalshi exposes its own play-by-play feed (from
   Stats Perform, per the feed's `provider` field) with a 1 s cache, and Polymarket runs a free live
   sports WebSocket. Either may beat ESPN, which is cached 5–6 s at the CDN and polled every 5 s by us. Measure all
   of them side by side on Thursday's game, then decide.
3. **Our own pipeline adds as much delay as the feeds.** Games are simulated one after another in a single loop
   (~2 s each), so on a Sunday with eight games changing at once the last one waits ~15 s. Run each game in its own
   process, react to events instead of a 5 s timer, and push updates to the page instead of having it poll every
   10 s.
4. **Paid game data is not worth it yet.** The cheapest real-time NFL feeds are ~$300/month; the fast ones (Sportradar
   push, Stats Perform, odds feeds) are quote-only and plausibly $1,000–$5,000+/month; the official NFL feed
   (Genius Sports) is sold to sportsbooks and media, not individuals. A feed pays for itself only if the model has an
   edge and enough money is bet on it: at a 3% edge after fees, $300/month needs about $10,000 a month staked just to
   break even. The grading so far (four QBs, two games) shows no edge over Kalshi. Section 6 has the test that
   would change that.

## 1. Where the time goes today

Measured 2026-10-19/20 from the worker's history log, response headers and the server.

| Stage | Delay | How we know |
|---|---|---|
| Kalshi REST `/markets?series_ticker=` CDN cache | 0–15 s (avg ~7.5 s) | `cache-control: public, max-age=15`, `x-cache: Hit from cloudfront` |
| Our Kalshi poll | 0–5 s (avg 2.5 s) | worker ticks every 5 s in a game window |
| Kalshi prices actually recorded | never < 15 s apart (median 35 s) | WAS @ SF history log; model updates were as close as 5 s apart |
| Score markets (new, not deployed) | 10 s fetch + same 15 s cache | `kalshi.SCORE_EVERY`, same endpoint |
| ESPN scoreboard / summary CDN cache | 0–6 s | `cache-control: max-age=6` (scoreboard), `max-age=5` (summary); measured after the game, may differ live |
| ESPN behind the field | unknown, likely 10–30 s | Kalshi moved 5–25 s before ESPN showed some plays (lead-lag study) |
| Our ESPN poll | 0–5 s; box score every 10 s | `Worker.every`, `box_every` |
| Simulation | ~2 s per game, games in sequence | worker log: "(2.1 s)" per update |
| Publish to R2 + page | 3 s cache + 5–10 s page poll | `publish.MAX_AGE`, page `EVERY_S` |
| Network, server ↔ Kalshi/ESPN | 5–50 ms (CDN edges in Europe); ~100 ms more to Kalshi's US origin | `curl` timings from the Hetzner server (nbg1, Nuremberg) |

What it means:

- Kalshi's price at any moment in our log is on average ~10 s old, up to ~20 s. The "model reacts 10 s faster than
  Kalshi" result is fully explained by this and should be thrown out; the "Kalshi moves on its own before ESPN"
  cases are real (they survive the delay).
- ESPN is the bigger unknown. Nothing we have measures ESPN against the field; the plan in section 6 does.
- Network distance is irrelevant at this scale (milliseconds against seconds).

## 2. Kalshi prices

| Option | Delay | Cost | Notes |
|---|---|---|---|
| **WebSocket `ticker` + `trade` channels** | push, sub-second | free | Needs a Kalshi API key (RSA key pair from account settings), signed handshake. `ticker` can subscribe to all markets or a list of tickers; carries bid/ask; `trade` carries each fill. `orderbook_delta` gives full depth. Recommended. |
| Uncached REST: `/markets/{ticker}`, `/markets/{ticker}/orderbook` | one request per market, no CDN cache | free | Checked: `x-cache: Miss`, no `cache-control`. Fine as a fallback for a few markets; ~150 score markets per game would hit the 20 reads/s Basic limit. |
| Batch order books `/markets/orderbooks` | up to 100 tickers per request | free, needs API key | Yes/no bids only. Use to resync after a WebSocket disconnect. |
| Higher rate-limit tier | Advanced: 30 reads/s | free (form) | Premier/Prime need 3.75%/7.5% of exchange volume: out of reach. Not needed with the WebSocket. |
| FIX API over AWS PrivateLink | sub-millisecond | institutional; contact Kalshi | For market makers. Pointless while our delays are in seconds. |
| Host in AWS us-east-2 (Ohio) | saves ~0.1 s | small | Third-party reports put Kalshi's origin in Ohio (others say Chicago). Only matters after everything else is fixed. |

Other prices as signals, not for betting on:

- **Polymarket market WebSocket** (`wss://ws-subscriptions-clob.polymarket.com/ws/market`): free, no
  authentication, book and trade events. If Polymarket lists the same NFL games, a jump there is a second market's
  reaction to a play.
- **Sportsbook odds feeds** (OpticOdds, SportsGameOdds, The Odds API): sportsbooks take the official NFL feed, so
  their live lines move on official data. OpticOdds advertises sub-second push but is quote-only (a competitor says
  from ~$5,000/month); SportsGameOdds lists a WebSocket tier from ~$1,000/month; The Odds API is cheap but polled and
  slower. Useful as an event trigger only if the game feeds below turn out slow.

## 3. Game data (what happened on the field)

### Free

| Source | Delay | Notes |
|---|---|---|
| ESPN site API (today) | CDN 5–6 s + ESPN's own lag | `site.api.espn.com`. The `cdn.espn.com` scoreboard is cached 58 s and the `sports.core.api.espn.com` play list 900 s: both worse. Polling faster than every 5 s gains little against the cache. |
| **Kalshi live data** (`/live_data/milestone/{id}`, `/game_stats`) | `game_stats` cached 1 s; the summary endpoint 3600 s | Provider: Stats Perform. Gives score, clock, down and distance, timeouts, last play, and full play-by-play with yards. Fetched without an API key. Archived games' per-play timestamps are backfilled, so its live speed has to be measured during a game. Strong candidate: it's likely the data behind the live game panel on Kalshi's site. |
| **Polymarket sports WebSocket** (`wss://sports-api.polymarket.com/ws`) | push | Free, public live game status and scores. Score and clock only, not yards per play, but as a trigger ("something happened, refetch now") it could cut seconds. |
| NFL's own app feeds | unknown | Unofficial endpoints behind the NFL app's tokens; against its terms. Not recommended. |

### Paid

| Source | Price | What you get | Fit |
|---|---|---|---|
| Goalserve NFL | $300/month listed ($1,200/year on sale) | "real-time" scores and stats, polled | Cheap test of whether a paid poll beats ESPN. Latency not published. |
| BALLDONTLIE NFL | from ~$10/month; webhooks on higher tiers (up to ~$300/month all-sports) | play-by-play, webhooks "within milliseconds" (vendor claim) | Cheap enough to test; claims unverified. |
| SportsDataIO real-time | quote only (self-serve tiers are a day late) | live play-by-play | Sales contact needed. |
| Sportradar NFL Realtime + Push | quote only; push is an add-on, not in the free trial | push feeds of every play, "Pulse" snap-level events (ball snapped, end of play) | The fastest commercial feed open to non-sportsbooks, likely $1,000s/month. |
| Stats Perform (Opta) direct | quote only | the feed behind Kalshi's live data | Only worth it if Kalshi's free copy turns out delayed. |
| Genius Sports (official NFL data, BetVision) | not sold to individuals | official play-by-play, Next Gen Stats, low-latency video | Exclusive NFL distributor to sportsbooks and media through 2029. Out of reach. |

### Broadcast

| Source | Delay behind the field | Notes |
|---|---|---|
| Over-the-air antenna | ~19 s (Super Bowl LX, 2026) | Lowest of any TV path. |
| Cable | ~38 s | |
| Streaming (YouTube TV, Hulu, DirecTV Stream, Peacock) | 50–60 s; YouTube TV varied by up to 126 s between viewers | Kalshi traders watching streams are slower than the data feeds. |
| Local radio broadcast | not measured; generally less than TV | Lowest-latency public description of each play, including yards. |

A computer could watch an antenna feed (capture card, ~$100 once) and read the score bug, or transcribe the local
radio call ("Purdy, deep right, Evans, to the 17 — a 52-yard gain") with speech-to-text. Radio may be the
fastest legal public source of yards per play, ahead of every free data feed. It's also the least reliable (spotting
errors, penalties announced later), so it would trigger an early provisional update, corrected by the data feed.

### Excluded

- **Data from inside the stadium (courtsiding).** Banned by league and venue terms and illegal in some states.
- **Defeating ESPN's or Kalshi's caches** with cache-busting query strings. Against the spirit of their terms; the
  WebSocket and uncached endpoints above make it unnecessary.

## 4. What we do with the data

Built 2026-10-23 (local, not deployed): one process per game (`--procs`), the event-driven loop (2 s ticks while
live, an immediate tick when a game's Kalshi market jumps, summaries fetched in parallel, the box score every 4 s),
and pushed updates (`--serve`). Measured on a 10-core Mac: 8 games simulated in 6.3 s in one process, 3.2 s in 2,
1.8 s in 4, 1.3 s in 8. The server has 2 cores and 3.8 GB (the worker uses ~1 GB), so it gets ~2×; 8 cores
(one of Hetzner's 8-vCPU plans; check current prices) would make a busy Sunday as fast as a single game. Pushing to the hosted
pages also needs a route from nfl.austingreen.net to the worker (e.g. a Cloudflare Tunnel behind the same Access
policy).

| Change | Saves | Notes |
|---|---|---|
| Event-driven worker | up to 5 s per event | React to WebSocket messages (Kalshi prices, Polymarket scores) at once instead of waiting for the next 5 s tick; a big jump in Kalshi's game market triggers an immediate ESPN or Kalshi-live-data fetch. |
| One process per game | up to ~15 s on Sundays | Today eight games changing at once are simulated one after another (~2 s each). |
| Faster first answer | ~1.5 s | Publish a quick 500-game simulation at once, then the full 4,000-game run a second later. |
| Precompute likely next states | ~2 s on most plays | The worker already lists the five most likely next snaps; simulate those before the play lands and publish the matching one instantly. |
| Box score on every play | up to 10 s | Fetch the summary on every scoreboard change, not every 10 s (or use a feed that carries yards per play). |
| Stamp at fetch time, log raw feeds | — | Exact timing for grading; record every source's arrival time per play. |
| Push to the page (Server-Sent Events or WebSocket) | 5–10 s for the viewer | Replace the 10 s page poll and the R2 cache; Cloudflare Durable Objects or a small relay on the server. |
| Host near Kalshi and ESPN (US East) | ~0.1 s | Last, and only if everything above is done. |

## 5. Should we pay for data?

What a faster feed is worth depends on whether the model beats the price. So far:

- Average Brier score over four QBs and two games: model 0.107, Kalshi 0.102. No edge, and when the two disagreed by
  a real margin (Love, Purdy) the market was right.
- On big plays the model moves within one update; Kalshi's real reaction time is unmeasured (section 1).

Break-even: monthly data cost ÷ net edge per dollar bet = dollars that must be bet per month.

| Feed cost / month | Edge after fees 2% | 3% | 5% |
|---|---|---|---|
| $300 (Goalserve) | $15,000 | $10,000 | $6,000 |
| $1,000 (odds WebSocket) | $50,000 | $33,000 | $20,000 |
| $5,000 (premium push) | $250,000 | $167,000 | $100,000 |

(Kalshi's fee is ~1.75¢ per contract at 50¢, so a 2–5% net edge needs a model meaningfully better than the market.)

Paying makes sense when all three hold: the grading shows a positive return on bets the model would make, the free
sources have been measured and are slower than the paid one by enough to matter, and the bankroll covers the
break-even volume. None hold today.

## 6. The test to run first (free)

On the next game (Thursday), run all free sources side by side and log, for every play, when each one first showed
it:

- ESPN scoreboard and summary, polled every 2 s;
- Kalshi live data `game_stats`, polled every 1 s;
- Polymarket sports WebSocket;
- Kalshi WebSocket `ticker` for the game, spread, total and passing-yard markets;
- optionally, the radio call, transcribed.

Result: a table of each source's delay per play (and, with the Kalshi WebSocket, the true time Kalshi's prices move).
That decides which game feed to use, whether ESPN is the bottleneck, and whether any paid feed could be faster than
the best free one. A Kalshi API key is the only thing needed beforehand.

## Sources

- Kalshi: [WebSocket quick start](https://docs.kalshi.com/getting_started/quick_start_websockets),
  [WebSocket connection](https://docs.kalshi.com/websockets/websocket-connection.md),
  [rate limits](https://docs.kalshi.com/getting_started/rate_limits),
  [multiple order books](https://docs.kalshi.com/api-reference/market/get-multiple-market-orderbooks.md),
  [live data](https://docs.kalshi.com/api-reference/live-data/get-live-data-with-type),
  [milestones](https://docs.kalshi.com/getting_started/targets_and_milestones),
  [FIX connectivity](https://docs.kalshi.com/fix-margin/connectivity.md),
  server location claims: [Glassnode](https://latency.glassnode.com/prediction-markets/about),
  [QuantVPS](https://www.quantvps.com/blog/kalshi-servers-location)
- Polymarket: [WebSocket overview](https://docs.polymarket.com/developers/CLOB/websocket/wss-overview),
  [API](https://docs.polymarket.com/getting-started/api.md)
- NFL official data: [NFL–Genius Sports extension](https://www.geniussports.com/newsroom/the-national-football-league-expands-and-extends-strategic-partnership-with-genius-sports-in-multi-year-deal/),
  [NFL.com](https://www.nfl.com/news/nfl-extends-strategic-partnership-with-genius-sports),
  [Genius official data API](https://www.geniussports.com/engage/official-sports-data-api/)
- Sportradar: [NFL push feeds](https://developer.sportradar.com/football/docs/nfl-ig-push),
  [Push Pulse](https://developer.sportradar.com/football/reference/nfl-push-pulse),
  [NFL API basics](https://developer.sportradar.com/football/docs/nfl-ig-api-basics)
- Other feeds: [Goalserve NFL prices](https://www.goalserve.com/sport-data-feeds/nfl-api/prices),
  [SportsDataIO](https://sportsdata.io/developers/getting-started),
  [BALLDONTLIE overview](https://highlightly.net/blogs/best-nfl-apis-in-2026),
  [BALLDONTLIE tiers](https://docs.stackone.com/connectors/balldontlie/guides/link-account/api-key),
  [OpticOdds vs SportsGameOdds](https://sportsgameodds.com/blog/optic-odds-vs-sports-game-odds),
  [OpticOdds](https://opticodds.com/sportsbook-operators-traders)
- Broadcast latency: [Super Bowl LX (Awful Announcing)](https://awfulannouncing.com/tv/antenna-cable-win-super-bowl-latency-battle-streamers-lag-behind.html),
  [Super Bowl LIX (SVG)](https://www.sportsvideo.org/2025/02/10/super-bowl-lix-tubi-leads-all-streaming-platforms-in-least-lag-time-according-to-phenix-report/)
- Our measurements: the server's history log (`data/live/history/2026-10-19.jsonl.gz`, `2026-10-20.jsonl.gz`),
  response headers fetched 2026-10-20, `curl` timings from the Hetzner server (nbg1).
