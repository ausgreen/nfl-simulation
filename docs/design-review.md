# Frontend design review, 2026-11-01

Reviewed on branch `frontend-design-review`, with no page changes yet. Covers the five hosted pages at 1280, 820 and
390 px, in light and dark, during a real live game (PHI @ WAS) with real Kalshi prices.

Numbered worst first within each section. Severity: **high** = misleads the viewer or breaks the page; **medium** =
slows the viewer down; **low** = polish.

## How it was checked

- Each page is this branch's HTML, fed by the JSON of the local worker on :8787. That worker runs the older
  `app-quality` code.
- The chart was also rendered with CAR @ GB, rebuilt from the server's history log using `Series.from_history`, the
  same path a worker restart takes.
- Watch list rows were test data added to localStorage, using real tickers.
- Screenshots are in `.review/design-review/` (gitignored).
- Not checked: Safari, a real phone, a screen reader, the stale-data banner.
- `live/live_template.html` was only compared by diff and nav line. It is a near copy of `game.html`, so assume game
  page issues apply to it too.

## 1. Chart page

| # | Sev | Issue | Evidence |
|---|-----|-------|----------|
| 1.1 | high, fixed | **"Whole game" isn't the game.** The axis starts at the first price, about 23 hours before kickoff, so the game fills roughly the right 12% of the chart. Quarter labels and score dots pile up there. | `chart_full_1280.png` |
| 1.2 | high, fixed | **A finished game ends on a false gap.** Kalshi's line drops to 50% at the end, and the readout shows "Model 99%, Kalshi 50%, Gap +49". | `chart_full_1280.png` |
| 1.3 | high, fixed | **Phone layout is broken.** The page is 457 px wide in a 390 px viewport, because this page never got the `minmax(0, 1fr)` and nav fixes the others have. The right side of the chart, where the game is, gets cut off. | `chart_full_390.png` |
| 1.4 | high, fixed | **Time axis labels overlap into unreadable text** on desktop and phone. Tick spacing ignores chart width. | both `chart_full` shots |
| 1.5 | medium | **The cursor works by mouse only.** There are no touch or keyboard handlers, so on a phone only the latest values can be read. | `chart.html:191` |
| 1.6 | medium | **Model and Kalshi lines look alike where they overlap.** Their colors have 1.06:1 contrast against each other, so only the dashes separate them, and the dashes disappear when the line moves quickly. | contrast calculation |
| 1.7 | low | Score dots show their score only in an SVG `<title>`, with no team or play shown, so they can't be read on touch. | `chart.html:183` |

## 2. Numbers that read wrongly

| # | Sev | Issue | Evidence |
|---|-----|-------|----------|
| 2.1 | high, fixed | **Overtime points are missing from the recap.** ARI 33–30 DAL shows quarters adding up to 30–30, with no OT cell, so the box score doesn't add up to the final. | `game_post_light_1280.png` |
| 2.2 | high, fixed | **A live quarter's projection looks like its score.** "Q3 now: PHI 0.2 – WAS 3.1" is the projected quarter final, while "now" suggests the current score. The real one (0–3) isn't shown. | `game_live_light_1280.png` |
| 2.3 | medium, fixed | **Footers and banners credit the wrong source.** Home and game pages say "Updated from ESPN's live feed", and the stale banner says "last check of ESPN's feed", but Kalshi's data is now the default feed. | `index.html:89,198`, `game.html:168,248` |
| 2.4 | medium, fixed | **The same kickoff appears in two time zones.** Home says "Mon 7:15 PM" (viewer's time) and the game page says "11/2 - 8:15 PM EST" (the feed's text). | `home`, `game_pre` shots |
| 2.5 | medium | **The game menu lags the page.** It reloads every 5 minutes, so it can show an old clock and score just above the live ones. One capture had "15:00 - 4th" over "End of 3rd". | `game.html:531`, `score_1280.png` |
| 2.6 | medium | **"Bet" and "+22% return" contradict the fine print.** The column headings present recommendations; only the footer says gaps are ungraded and to "treat them as questions, not bets". The caveat should sit where the number is. | `edges_light_1280.png` |
| 2.7 | low, fixed | Decided lines take up rows: "125+ 100% 100¢" with 129 yards already thrown, and "350+ 0%". The Model vs Kalshi page already leaves these out. | `game_live_light_1280.png` |

## 3. Hierarchy and layout

| # | Sev | Issue | Evidence |
|---|-----|-------|----------|
| 3.1 | high, fixed | **The game page has no clear main thing.** Win chance, probably the first thing a viewer wants, is a tiny pill. There is no win bar, though the home page has one. "The next snap" gets the biggest panel on the page, and the QB lines start about 960 px down on desktop and much further on a phone. | `game_live_light_1280.png` |
| 3.2 | high, fixed | **The Score tab is one flat list of 52 rows,** with no breaks between winner, spread, total and team points. It runs 3,047 px on desktop and 5,236 px on a phone, where every label wraps. | `score_1280.png`, `score_390.png` |
| 3.3 | medium, fixed | **Phones bury the game page.** It's 3,375 px tall, with the next-snap and drive tables ahead of the QB lines and no way to jump or collapse. | `game_live_light_390.png` |
| 3.4 | medium, fixed | **The biggest button does nothing useful.** The only solid-color control on the page is Refresh, beside auto-refresh and an interval menu, on a page that receives pushed updates. They only matter as a fallback when polling. | game page shots |
| 3.5 | medium | **The live game doesn't stand out on home.** Live cards are the same size as finals, and one live card leaves two-thirds of the row empty while 12 finals fill the page. | `home_light_1280.png` |
| 3.6 | medium | **Game and Chart are tabs that lose track of the game.** The page only remembers a game picked in its menu, not one opened from a home card. Open a game from home, click Chart, and you can land on another game. The game page has no direct link to that game's chart. | `game.html:195`, `chart.html:248` |
| 3.7 | medium | **Model vs Kalshi needs its footer to be read.** Five filter menus sit above the table, three paragraphs explain the columns below it, and the gap bar has no scale or legend. | `edges_light_1280.png` |
| 3.8 | medium | **Empty watch list looks like a broken portfolio.** It shows six zero totals ("+$0.00") and a 13-column header around one line of help. | `watch_empty_light_1280.png` |
| 3.9 | low | Watch list inputs look disabled: gray fill, faint border, "—" as placeholder. | `watch_light_1280.png` |
| 3.10 | low, fixed | Pages have different widths (1100 px game, 1200 px others), so the logo and tabs jump 50 px when moving to or from the game page. | `game.html:30` |

## 4. Visual system

| # | Sev | Issue | Evidence |
|---|-----|-------|----------|
| 4.1 | medium | **No identity.** The name is generic, the type is the system sans plus system mono, and everything is white rounded panels on pale gray. Nothing says football: teams have no colors or marks, and the score isn't treated as a scoreboard. It could be any admin dashboard. | all shots |
| 4.2 | medium | **Green and amber each mean too many things.** Green is the brand, live, bars, "yes", gains, possession and every pill. Amber is warnings, "no", losses, the Kalshi mark, score dots, "replaced" and the update flash. So a "no" bet and a loss both look like warnings. | CSS tokens |
| 4.3 | medium | **Live updates flash much of the page.** In a 45 s sample of a live game, up to 19 elements flashed at once in the warning color, so the flash doesn't direct attention to what matters. | measured |
| 4.4 | medium | **Pills are everywhere and mean nothing specific.** Next-play mix, score results, possession, status and Watch buttons share one shape, so read-only text looks clickable. | `game_live_light_1280.png` |
| 4.5 | medium, fixed | **The score doesn't read as a score.** "PHI 10 — WAS 16" in mono has a wide dash that sits against the numbers. Possession is an underline across team and score with no label, and it's easy to miss. | head of game page |
| 4.6 | medium, fixed | **The odds bar is overloaded.** Fill, two dots and a tick share an 8 px bar. Near 0% they collide into a blob, and the dots and tick have low contrast on the fill (tick 1.19:1, dots 2.55:1 in light; tick 1.55:1, dots 2.12:1 in dark). | game page shots |
| 4.7 | low | **Too many type sizes.** The game page uses 10 distinct sizes, including fractional ones (11, 11.5, 12, 12.5), and the h2s are tracked all-caps labels. | measured |
| 4.8 | low, fixed | **Styles are copied into every page, and have drifted.** Tokens and nav are duplicated in all six HTML files. Chart missed the earlier muted-text contrast fix (see 5.1), and `h1`, panel padding and Watch button sizes differ by page. This is the cause of 1.3 and 3.10. Scoring and watch-list JS is copied as well. | diff of the files |

## 5. Accessibility and touch

| # | Sev | Issue | Evidence |
|---|-----|-------|----------|
| 5.1 | high, fixed | **Failing text contrast.** White on the dark-mode Refresh button is 2.54:1. Chart page muted text is 3.82:1 on panels and 3.49:1 on the page. The target is 4.5:1. | contrast calculation |
| 5.2 | medium | **Key information exists only in hover tooltips,** so phones never see it: Kalshi bid and last price, likely ranges, the next-snap breakdown, column definitions. | `title=` attributes |
| 5.3 | medium | **Nav is clipped on phones.** With the fifth tab, "Watch list" cuts off at 390 px with no sign the bar scrolls, and the earlier fix for this assumed four tabs. | `home_light_390.png` |
| 5.4 | medium | **Small tap targets.** Below 24 px tall: 33 of 151 controls on the live game page, 9 of 19 on Model vs Kalshi, 9 of 23 on the watch list. | measured |
| 5.5 | medium | **Screen readers would get constant interruptions (from the markup, not tested).** `aria-live` wraps regions that are rebuilt on every update, including the whole home page. | `index.html:88`, `game.html:137` |
| 5.6 | medium | **Motion and focus aren't handled.** No page respects `prefers-reduced-motion`, which matters with 4.3. None defines `:focus-visible`, so focus falls back to the browser default. | grep |
| 5.7 | medium | **Weak non-text contrast.** Bar tracks are about 1.5:1 against panels, and input borders about 1.35:1. The update dot is 1.26:1, and its meaning is only in a tooltip. | contrast calculation |
| 5.8 | low | The game page has no `h1`. Its market tabs use `role="tab"` without panel links or arrow keys. | measured, `game.html:154` |
| 5.9 | low | Yes and no on one line share a ticker, so watching one marks both as Watching and only one is kept. Under "both sides" this looks like a bug. | `edges.html:266` |

## Works well: keep

- Dark mode is solid apart from 4.6 and the Refresh button in 5.1.
- Card layouts on phones for Model vs Kalshi and the watch list put price and P/L in view.
- Recap copy on finished games ("ARI covered · total 54.5: 63 points, over") is clear and useful.
- "Best per market" and the positive-return default make for a short list that's quick to scan.
- Only the chart page overflows on phones.

## Suggested order

Step 1 is done on this branch, not yet deployed. The chart's price fix (1.2) is in `hosted/series.py`, so it needs the
worker restarted with the new code.

Step 2 is done too: common styles, nav and helpers live in `hosted/shared.css` and `hosted/shared.js`, and
`hosted/pages.py` inlines them into each page when it is published or served.

Step 3 is done. The local app now serves `hosted/game.html`; `live_template.html` is gone, and `/state/<event>.json`
re-simulates only when the play changes. The game page reads top to bottom as game status (score, win chance, box
score), then markets, then next snap and drive (collapsible, and closed on phones). The Score tab is grouped, with
ladders folded to the 5 lines nearest a coin flip. `test_pages_run_in_a_browser` drives every page in headless Chrome.

1. **Fix what misleads or breaks:** 1.1–1.4, 2.1–2.4, 5.1. These are mostly small, targeted changes.
2. **Share one stylesheet, nav and JS across the pages** (4.8). It's cheap now and stops further drift, and everything
   after it then needs changing only once.
3. **Rework the game page around win chance and the score:** 3.1, 3.2, 3.3, 3.4, 4.5, 4.6.
4. **Give it a visual identity and a color system that assigns one meaning per color:** 4.1, 4.2, 4.3, 4.4, 4.7.
