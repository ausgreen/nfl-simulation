# Risks of running the live companion as a product

Written 2026-10-18, while the site is a private beta at `nfl.austingreen.net` (Cloudflare Access, a few testers).
Terms of service and prices change; re-check every "check" item below before opening the site to the public or
charging for it. This is a working list, not legal advice.

## Summary

| # | Risk | Likelihood | Impact | Status |
|---|---|---|---|---|
| 1 | ESPN blocks or changes its feed | Medium | **Site stops updating** | No fallback |
| 2 | Data use not permitted once public or paid (ESPN, Open-Meteo, nflverse, Kalshi) | Medium–high when commercial | Forced shutdown or paid licenses | Fine for a private beta; unresolved for public |
| 3 | nflverse stops publishing or changes formats | Low–medium | Weekly refresh breaks; live site keeps running on old data | Data copied locally |
| 4 | Kalshi API blocks, rate-limits or changes | Medium | Price column disappears | Optional feature |
| 5 | Open-Meteo forecast unavailable or rate-limited | Medium | Weather inputs fall back to neutral | Cached archive |
| 6 | Wrong starting QB (roster kept by hand) | High (several times a season) | Wrong QB numbers for a game | Manual |
| 7 | Gambling / financial-advice concerns from showing odds beside Kalshi prices | Medium once public | Legal exposure, app-store or ad bans | Disclaimer only |
| 8 | Single server, single person | Medium | Outage during games | Auto-restart, stale banner |
| 9 | Model wrong in a visible way | Certain at times | Trust | Caveats on page |
| 10 | Credentials leak (R2 keys, SSH) | Low | Defaced or deleted site | Keys scoped, `.env` gitignored |

## 1. Data sources (the biggest risk)

The live site depends on one source while games are on, and on several more for the weekly data.

| Source | What we use it for | How often | Official? | If it stops |
|---|---|---|---|---|
| **ESPN** `site.api.espn.com` (scoreboard, game summary) | Live score, clock, possession, down and distance, last play, passers' yards | Scoreboard every 5 s during games (60 s otherwise); each live game's summary every 10 s or on a new play | **No.** Undocumented; no published terms granting use | **The live site freezes** (the stale banner shows). Nothing else works without it |
| **nflverse** (GitHub releases and `nfldata`) | Player stats, schedules (lines, starters, roofs), play-by-play for training the simulator, injuries, snaps | Weekly (by hand until phase 4) | Open project; data licensed for reuse (check the exact licence of each file) | Live site keeps working with last week's data; lines, starters and team ratings go stale |
| **Kalshi** public API | Passing-yard prices shown beside the model's chances | Every 30 s | Public, documented API with rate limits (check its terms on display of prices) | The price column is empty; everything else works |
| **Open-Meteo** forecast and archive | Weather inputs (wind, cold, rain) | Forecast once per week's predictions; archive already downloaded | Public API; the **free tier is for non-commercial use** (check) | New weeks use neutral weather; small accuracy loss |
| **OpenStreetMap Nominatim** | Coordinates of a few unusual venues | One-off, 1 request/s | Public; strict usage policy | None in normal running (results are cached in `venues.csv`) |

### Why ESPN is the real exposure

- It's unofficial. ESPN can rename fields, add bot protection, require keys or block our server's IP at any time
  and owes us no notice.
- **Volume:** ~720 scoreboard requests an hour during games plus summaries. That's polite for one server, but it's
  a steady pattern from one data-center IP, which is easy to spot.
- **Terms:** ESPN's terms of use generally forbid automated collection and commercial reuse of its content. A
  private beta for friends is low profile; a public or paid product built on it is the kind of use a takedown
  targets.
- **Correctness:** we also inherit ESPN's errors and delays (box-score yards lag plays by 20–60 s).

### Mitigations, cheapest first

1. **Already in place:** one server talks to ESPN, however many viewers there are. A failing game is retried after 60 s, and the
   page shows a stale-data banner rather than wrong numbers. `live/feed.py` keeps the feed behind one interface, so a
   second source can be plugged in without touching the simulator.
2. **Be a good citizen:** a clear User-Agent, back off on errors (429/403) instead of retrying every 5 s, poll
   summaries only for games with a new play. Fewer requests mean fewer reasons to block us.
3. **Know immediately:** alert when ESPN returns errors or unparseable data for more than a few minutes during a game
   window (phase 4 monitoring). Keep the last week's recordings (`nflsim feed record`) so a format change can be
   fixed and tested against replays quickly.
4. **A second free source as a fallback**, for example NFL.com's public game-center feed. It is also unofficial and
   has the same risks, but both sources are unlikely to break on the same day. It needs its own parser into the same
   state format.
5. **A licensed feed** before going public or charging. This is the only real fix. Get quotes early, because the
   price decides whether a public version is affordable at all. See the shortlist below.
6. **Weekly data:** keep our own copies of every nflverse file we use (already in `data/`), pin the formats our
   loaders expect, and fail loudly on a schema change instead of silently training on bad data. The archive
   weather (`data/weather/archive/`) is expensive to rebuild; back it up.

### Paid live play-by-play feeds (researched 2026-10-18)

**Live play-by-play is a requirement.** The simulator needs, on every play: possession, down, distance, yard line,
clock, score and the last play, plus each QB's passing yards, all within seconds. A feed with live scores and box
scores only doesn't work.

The prices below come from vendor pages and third-party reviews, some of them months old. Confirm them with sales.

| Provider | Live play-by-play | Price | Notes |
|---|---|---|---|
| **Rolling Insights (DataFeeds)** | Yes: sequence, quarter, down, distance, yard line, clock, possession per play | NFL Live Feed listed at $7,200/yr (another table shows a $600 live tier, so the two figures conflict); 30-day free trial | **First candidate**: right fields, clearest pricing, a trial long enough to cover live games |
| **SportsDataIO** | Yes on the production tier (REST + WebSocket) | Quote only; estimates $500–1,000+/mo; Vendr median ~$16.5k/yr across all products | The $99–149/mo self-serve tier is delayed to the next day and the free trial is scrambled, so neither works live. Its Replay tool plays back real past games any time, for testing |
| **Sportradar** | Yes | B2B contract, estimated $500–1,000+/mo per sport | Later or larger scale |
| **Genius Sports** | Yes; the NFL's exclusive official data partner | Enterprise contract | Only if "official data" matters (partners, sportsbooks) |
| **BigBalls Sports Data** | Pro tier only, with WebSocket | Pro price not published | Unproven; ask |
| **Entity Digital Sports** | Unclear how live | $100–500, or $5,000/season | Ask before considering |

Ruled out:
- BallDontLie, Tank01, Highlightly and MySportsFeeds: no clearly live per-play detail, or a non-commercial cheap tier.
- Goalserve: scores and box scores only.
- EndzoneAPI: built on nflverse, not clearly real-time.
- FTN: historical data only. It could replace nflverse, not ESPN.

**How to evaluate:** start Rolling Insights' trial and SportsDataIO's Replay. Record ESPN and the trial feed side
by side for one Sunday (`nflsim feed record`), then compare how far behind each runs and which fields are missing.
Then ask each finalist:
- whether the licence covers a public site and display next to betting-market prices;
- the call limits at our polling rate, or whether a push feed is available;
- the guaranteed latency (SLA).

A new provider needs one parser behind `live/feed.py` into the existing state format.

Sources:
- [Rolling Insights NFL API](https://rolling-insights.com/datafeeds/nfl-api/)
- [Rolling Insights prices](https://rolling-insights.com/datafeeds/price-plans/)
- [SportsDataIO APIs](https://sportsdata.io/developers/apis)
- [Vendr: SportsDataIO](https://www.vendr.com/marketplace/sportsdataio)
- [SharpAPI comparison](https://sharpapi.io/compare/sports-data-apis)
- [Moneyline review](https://www.moneylineapp.com/blog/best-sports-apis)
- [BigBalls NFL API](https://bigballsdata.com/nfl-api)
- [Entity Digital Sports](https://www.entitysport.com/nfl-api/)
- [EndzoneAPI](https://endzoneapi.com/)
- [Goalserve prices](https://www.goalserve.com/en/sport-data-feeds/NFL-api/prices)

### Decision points

| Stage | Data needed |
|---|---|
| Private beta (now) | Current sources are acceptable |
| Public and free | Read ESPN's, Open-Meteo's and Kalshi's terms; add monitoring and the fallback source; consider a paid Open-Meteo plan |
| Paid, ads or sponsorship | Licensed live feed; commercial weather plan; written OK or terms review for displaying Kalshi prices |

## 2. Legal and reputational

- **Betting adjacency.** The page shows win chances and passing-yard chances next to live Kalshi prices with links to
  Kalshi. Once public, this can read as betting advice or as promotion of a prediction market. Rules differ by
  state, and ad networks and app stores have gambling policies. Keep the "information and entertainment only"
  wording, add an age note, don't take referral payments without advice, and get a lawyer's view before
  monetizing.
- **NFL marks.** Team names and abbreviations are used as plain facts; no logos or NFL branding. Keep it that way,
  and don't call the site "NFL" anything in a public product name without checking.
- **Privacy.** Cloudflare Access stores testers' email addresses. A public version without login collects nothing,
  except whatever analytics are added later.

## 3. Operations

- **Wrong starter.** `data/curated/roster.csv` is edited by hand. A late scratch gives wrong QB numbers until someone
  pushes a fix. Mitigation: flag games where ESPN's active passer differs from the roster's QB (planned), and
  make the roster editable on the server without a full deploy.
- **Single server.** One Hetzner CX22 runs everything. Docker restarts the worker, and the page shows a stale banner,
  but a host outage on a Sunday means no updates until it's fixed. A rebuild from `deploy/cloud-init.yaml` and
  `deploy/push.sh` should take well under an hour (not yet timed). Keep that documented and tested, and keep the server's `data/live/` caches
  reproducible.
- **One maintainer.** Weekly data, roster fixes and incidents all depend on one person being available on game days.
  Phase 4 (automatic weekly refresh and alerts) reduces this.
- **Costs.** Today: about €5 a month for the server, with R2 and Access free. Viewer growth costs little: the CDN
  serves cached files. A licensed data feed would cost far more than everything else combined.

## 4. Model

- Win chances are still somewhat overconfident in the 4th quarter (2025: 11 upsets where it expected 4). Regulation
  ends tied less often than in real games.
- The QB yards model trailed Kalshi's prices in the one week we compared, and runs high for QBs with few starts.
- Showing the model's numbers beside market prices invites people to treat the gaps as bets. Most gaps are the
  model's error, not the market's.

## 5. Security

- R2 keys can only reach the `nflsim` bucket. They live in `.env` (gitignored, mode 600) on the laptop and the
  server. Rotate them if either machine is lost.
- The server accepts SSH keys only, has root login off, and its firewall allows only SSH. The worker takes no input
  from viewers.
- Cloudflare Access protects the site while it's private. Before going public, set a rate limit or a WAF rule
  on the bucket's domain, so a scraper can't run up R2 read costs (reads are cheap but not unlimited).
