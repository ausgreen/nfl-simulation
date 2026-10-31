# Decisions: `app-quality` branch

A running record of the major decisions made while bringing the live companion's pages up to standard
(usefulness, clarity, quality). Newest last. Each entry: what was decided, why, and what was left out.

## 2026-10-30: set-up

- **Worked in a separate worktree** (`.worktrees/app-quality` inside the repo, branch `app-quality` from `master` at
  ce0dd15), with an APFS clone of `data/`. It started as a sibling folder (`../nfl-simulation-app`); moved inside the
  repo at the user's request, so all work stays under the dev directory. `.worktrees/` and `.review/` are excluded
  through `.git/info/exclude` (local only), not `.gitignore`. `master`'s working tree has uncommitted work from another session (the chances-over-time
  chart, `series.py`, finer score/clock bins in `sim/tables.py`), and switching branches there would disturb it. Its own
  `data/` copy also keeps that session's simulator cache rebuilds from colliding with this one's.
- **Scope: the product, not the simulator.** The other session is changing `sim/`; this branch leaves `sim/` alone, and
  stays out of the chart page and `series.py`. Expected merge conflicts: the nav bar line in each page and a few lines
  of `worker.py`. They're small.
- **Reviewed by replaying** `recordings/2026-10-12_BUF_LA.jsonl.gz` through the worker (`--serve 8787`), taking
  screenshots at desktop (1200 px) and phone (390 px) widths with Playwright driving the system Chrome
  (`.review/shot.mjs`, `.review/wide.mjs`; not committed). For the Kalshi pages, a made-up `kalshi.json` is written
  into the review site only (replays have no prices).

## Review findings (before changes)

What a viewer sees, worst first:

1. **Finished games look broken.** The game page still shows "The next snap: No snap 100%, Kickoff", "Next drive: no
   drive in progress", and every passing-yard line as a 0% or 100% bar with "likely range 189–189". None of that
   means anything once the game is over. Nothing says how the game went against the pregame expectations, which is
   what a viewer wants to know after it ends (and what builds trust in the model).
2. **Home page cards have text glitches.** A pregame QB line reads "Allen BUF: · median 242" (stray separator); a
   "may be out" badge is followed by " : 87 yds". Final cards give yards with nothing to compare them to.
3. **Phone width overflows** by a few pixels (horizontal scroll), and the next-snap table's Result column wraps
   into 3-4 lines per row.
4. **Replays show a false "Updates have paused … 18 days ago" banner** and "last play 18 days ago", because the
   pages compare the replay's virtual clock with the browser's. Only affects replays and demos, but those are how the
   pages get reviewed.
5. "After the next snap, 300+: Completion 50% · Incomplete 42% · Run 46% · Other 51% · now 47%" makes the reader do
   the subtraction; the move from now is the point.

## Changes

1. **Finished games get a recap instead of empty live panels** (game page). The next-snap and drive panels are
   hidden; under the final score: the result against the pregame line ("Line DAL −9.5: TB won by 8, TB covered · total
   49.5: 40 points, under"); each QB: yards vs the pregame expectation (v3's `mu`, already in the state) and the
   highest line cleared / first line missed. The header drops "Final / Last play: —". A QB another passer took over
   from reads "replaced" rather than "may be out".
   *Why the pregame expectation and not the pregame simulated median/range:* `mu` is already in every state; keeping
   the pregame median would need the worker to remember (and persist across restarts) each game's first state. The two
   differ by a few yards. Revisit if the chart work stores pregame states anyway.
2. **Home page cards:** finals show the line result ("DAL −9.5 · TB covered · under 49.5") and each QB's yards vs
   expectation ("189 yds vs 216 expected (−27)"); fixed the stray "· median" and the badge-then-colon layout.
3. **Phone width:** every page's `.app` grid gets `grid-template-columns: minmax(0, 1fr)` (the nav bar's min-content
   width was widening the page by 4 px); the next-snap and drive tables drop the bar column under 620 px so the
   Result column isn't 3-4 lines per row.
4. **Replay clock:** `status.json` carries `skew` (wall clock − feed clock, only when over a minute, i.e. replays) and
   the pages subtract it, so a replay no longer shows "Updates have paused … 18 days ago". Live output is unchanged.
5. **Pregame:** no "0 yds now", "pregame 245" next to the median, or "Last play: —".
6. **"After the next snap"** now reads "300+ is 45% now; after the next snap: Completion 48% (+3) · Incomplete 43%
   (−2) …": the move is the information.
7. **Model vs Kalshi:** a "Bets" filter, defaulting to *positive return after fees* (Return > 0; gap > 0 still listed bets
   the fee turns negative). Before, every line was listed both ways, so half the rows were bets the model rates below
   their price. A **clear** tag marks gaps whose whole
   likely range is above the price (the honest "confident" signal; with real prices most ranges span zero). Footer
   rewritten as three short paragraphs.
8. **Model vs Kalshi, one row per opinion:** a "Lines" filter, defaulting to *best per market*. On real prices one QB's
   ladder filled the list (six Prescott rows), though those lines move together and are one bet. The footer now says
   Return assumes the model is right and that gaps are ungraded (first-recorded game: the market was right). "Worth it"
   was dropped from the label for the same reason.
9. **Local page kept in step:** `live serve` serves `live/live_template.html`, a near copy of `game.html`, so items 1,
   3, 5 and 6 were ported to it. The duplication stays for now; merging the two pages is a larger change than this pass.

## Second review: states not yet looked at (Score tab, dark mode, watch list with entries, failures, phones)

10. **Watch list inputs no longer fight the viewer.** Leaving Contracts redrew the table and destroyed the field just
    clicked, and a focused side menu froze all updates until the viewer clicked elsewhere. Now rows redraw freely, keep
    the field being typed in (value and cursor), and redraw 200 ms after an edit, once focus has landed. Checked with
    scripted typing, tabbing, side change and Remove at both widths. A value counts when its field is left.
11. **Before kickoff the page stops claiming who receives.** The state assumes the home team receives so it can simulate,
    but no coin toss has happened. Pregame hides the next-snap and drive panels, the possession underline and "after
    the next snap", which were all built on that assumption. The server's state is unchanged; this is display only,
    and easy to restore. Their last-play and "yds to" lines go too.
12. **Phones get cards on Model vs Kalshi and the watch list.** Bet, Price, Gap and P/L were off-screen behind a sideways
    scroll. CSS only: one card per row under 620 px. All four nav tabs now fit, and the update dot has no line of its own.
13. **Failures read as failures.** A failing update stacked one more error line every refresh; now there is one. Home stayed
    on "Loading…" beside the error, and the game page showed empty panels and never retried; now they say so and retry.
14. **Finished games drop the market tabs,** so a viewer whose saved tab was Score still sees the recap.
15. **Small:** light-mode muted text darkened to 4.8:1 contrast (was below 4.5:1); the browser tab shows the game and score.

Looked at and left alone: dark mode (reads well), the Score tab. The auto-refresh controls mean little while updates are
pushed, but they matter on R2, which still polls.

## Known limits (not fixed here)

- **The recap's "expected" belongs to the expected starter.** If a backup is shown because the starter never threw
  (`_starter_yards`), `mu` is still the starter's number. Fixing it means a v3 number for the backup.
- **Pregame, "clear" will rarely show:** the likely range uses the QB's form at its 10th/90th percentiles, which is wide
  before kickoff. It's meant for live games. Not yet seen on real prices.
- **Not reviewed with a real live game plus real prices:** live review used a replay (no prices); price review used
  this week's pregame markets. This worktree's copy of `data/` is also behind (stats from 10/19), so the gaps seen
  during review say nothing about the model.
- **Merging with `master`:** expect conflicts on each page's nav line and in `worker.py`'s `tick` (the chart work).
