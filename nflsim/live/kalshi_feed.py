"""Game state from Kalshi's live game data (Stats Perform), a possibly faster source than ESPN's feed.

ESPN's feed shows a play ~28 s after it happens (2026-10-19/30 history log against ESPN's per-play wallclock), while
Kalshi's prices move ~7 s after it. Kalshi's own play-by-play (`/live_data/milestone/{id}/game_stats`, cached 1 s)
may be the data its traders see. This module turns it into the same state dict as espn.situation + add_summary, so
the simulator can use either:

- score, quarter and clock, points per quarter, timeouts, the latest play's text and time;
- passing yards per QB, from the play text ("X pass ... to Y for N yards", laterals credited to the passer): on the
  18 games of 2026-10-18/30 every starter's and backup's total matched ESPN's final box score exactly;
- the latest passer per team (for spotting a backup), and who received the opening kickoff;
- kickoff pending and who receives (after a score, a safety, or a kickoff or punt with no snap since); down,
  distance and the spot from Kalshi's live summary when it describes the latest play, else ESPN's.

ESPN's scoreboard still supplies the schedule (teams, season, week, status). Plays carry no spot of their own, only
text ("to the CAR 24"); kickoff plays carry the kicking team, scoring tries the scoring team.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import espn

BASE = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_CODES = {"LA": ("LAR", "LA"), "WAS": ("WAS", "WSH"), "JAX": ("JAX", "JAC")}  # nflverse -> Kalshi tickers
_milestones: dict[str, dict] = {}  # event -> {"id", "home_id", "away_id"} (a game not found yet is looked up again)

_YDS = re.compile(r" for (?:(-?\d+) yards?|no gain|a loss of (\d+) yards?)")
_LATERAL = re.compile(r"Lateral to .+?(?= for )( for (?:(-?\d+) yards?|no gain|a loss of (\d+) yards?))")
_PASSER = re.compile(r"^([A-Z][A-Za-z'.\-]*\.\s?[A-Z][A-Za-z'\-]+) (?:pass|incomplete pass|sacked)\b")
_SPOT = re.compile(r"(?:to|at) (?:the )?([A-Z]{2,3}) (\d{1,2})\b")
QUIET = {"defensive_time_out", "offense_time_out", "two_minute_warning", "end_of_period", "end_of_quarter"}


def _get(url: str) -> dict:
    return espn.source.get(url)  # same source as ESPN's requests: recorded and replayed with them


def milestone(event: str, away: str, home: str, kickoff: str) -> dict | None:
    """Kalshi's milestone for a game (the key of its live data), from the game market's ticker
    (KXNFLGAME-26OCT29CARGB: kickoff date in US Eastern time, Kalshi's team codes)."""
    if event in _milestones:
        return _milestones[event]
    day = datetime.fromisoformat(kickoff.replace("Z", "+00:00")).astimezone(ZoneInfo("America/New_York"))
    found = None
    for a in KALSHI_CODES.get(away, (away,)):
        for h in KALSHI_CODES.get(home, (home,)):
            ticker = f"KXNFLGAME-{day:%y}{day.strftime('%b').upper()}{day:%d}{a}{h}"
            ms = _get(f"{BASE}/milestones?limit=5&related_event_ticker={ticker}").get("milestones", [])
            if ms:
                d = ms[0].get("details") or _get(f"{BASE}/milestones/{ms[0]['id']}")["milestone"]["details"]
                found = {"id": ms[0]["id"], "home_id": d["home_team_id"], "away_id": d["away_team_id"]}
                break
        if found:
            break
    if found:
        _milestones[event] = found
    return found


def fetch(m: dict) -> tuple[dict, dict]:
    """(the live summary's details, the play-by-play) for a milestone()."""
    details = _get(f"{BASE}/live_data/football_game/milestone/{m['id']}")["live_data"]["details"]
    return details, _get(f"{BASE}/live_data/milestone/{m['id']}/game_stats")


def plays(game_stats: dict) -> list[dict]:
    """Every play, oldest first, with its period."""
    out = [dict(p, period=per["period_number"]) for per in (game_stats.get("pbp") or {}).get("periods", [])
           for d in per.get("events", []) for p in d.get("plays", [])]
    return sorted(out, key=lambda p: p["sequence"])


def _text(p: dict) -> str:
    return p.get("source_description") or p.get("description") or ""


def _surname(name: str) -> str:
    n = re.sub(r"\b(jr|sr|ii|iii|iv)\b\.?", "", name.lower()).replace(".", " ").split()
    return unicodedata.normalize("NFD", n[-1]).encode("ascii", "ignore").decode() if n else ""


def pass_yards(text: str) -> tuple[str, int, bool] | None:
    """(passer, yards credited, an attempt) for a play's text, None if no pass was thrown (two-point tries aren't
    passing yards). A completion's yards include what was gained after a lateral, as the box score credits them."""
    m = re.match(r"^(.+?) (?:pass|incomplete pass)\b", text)
    if not m or "2 pt" in text:
        return None
    qb, rest = m.group(1), text[m.end():]
    first = rest.split(". ")[0]
    if text[len(qb) + 1:].startswith("incomplete") or "INTERCEPTED" in first:
        return qb, 0, True
    y = _YDS.search(rest)
    if not y:
        return qb, 0, True
    val = lambda g1, g2: int(g1) if g1 else (-int(g2) if g2 else 0)
    total = val(y.group(1), y.group(2)) + sum(val(x.group(2), x.group(3)) for x in _LATERAL.finditer(rest))
    return qb, total, True


def passing(ps: list[dict], side: dict) -> tuple[dict, dict]:
    """({side: [(name, id, attempts, yards)]}, {side: latest passer as "B.Young"}) with side 0 home / 1 away."""
    totals: dict[tuple, list] = {}
    latest: dict[int, str] = {}
    for p in ps:
        s = side.get(p.get("possession"))
        if s is None:
            continue
        r = pass_yards(_text(p))
        if r is not None:
            key = (s, _surname(r[0]))
            row = totals.setdefault(key, [r[0], f"kalshi:{key[1]}", 0, 0.0])
            if len(r[0]) > len(row[0]):  # prefer the full name ("Bryce Young" over "B.Young")
                row[0] = r[0]
            row[2] += r[2]
            row[3] += r[1]
        m = _PASSER.match(p.get("description") or "")
        if m and "No Play" not in (p.get("description") or ""):
            latest[s] = m.group(1).replace(" ", "")
    out: dict[int, list] = {}
    for (s, _), row in totals.items():
        out.setdefault(s, []).append(tuple(row))
    return {s: sorted(rows, key=lambda r: -r[2]) for s, rows in out.items()}, latest


OWN_BALL = {"pass", "rush", "sack", "fumble", "punt", "field_goal", "blocked_field_goal", "lateral", "extra_point",
            "two_point_conversion", "missed_two_point_conversion"}  # plays whose `possession` is the team with the ball
ENDS_DRIVE = {"punt", "field_goal", "blocked_field_goal", "extra_point", "two_point_conversion",
              "missed_two_point_conversion"}


def ot_possessions(ps: list[dict], side: dict) -> list[bool]:
    """[home, away]: whether each team has had its overtime possession, from the plays of periods 5 and later: every
    run of plays by one team that another team's run (or a kick, a try or a score) followed."""
    done = [False, False]
    runs = []  # (side, its last play)
    for p in ps:
        if p["period"] < 5 or p["play_type"] not in OWN_BALL or " kicks " in _text(p):
            continue  # (a fumbled kickoff is a "fumble" credited to the kicking team)
        s = side.get(p.get("possession"))
        if s is None:
            continue
        if runs and runs[-1][0] == s:
            runs[-1] = (s, p)
        else:
            runs.append((s, p))
    for i, (s, last) in enumerate(runs):
        scored = "TOUCHDOWN" in _text(last).upper()
        if i < len(runs) - 1 or last["play_type"] in ENDS_DRIVE or scored:
            done[s] = True
    return done


def _game_clock(period: int, quarter_clock: float) -> float:
    return max(0, 4 - period) * 900 + quarter_clock if period <= 4 else 0.0


def _secs(clock: str | None) -> float | None:
    try:
        mm, ss = (clock or "").split(":")
        return int(mm) * 60 + float(ss)
    except ValueError:
        return None


def _spot(text: str, offense: str) -> float | None:
    """Yards to the goal for `offense` from the last spot named in a play's text ("to the CAR 24")."""
    found = _SPOT.findall(text)
    if "Touchback" in text and found:
        found = found[-1:]
    if not found:
        return None
    team, n = found[-1]
    return 100.0 - float(n) if espn.team_code(team) == offense else float(n)


def game_state(s: dict, m: dict, details: dict, game_stats: dict) -> dict:
    """The state espn.situation(...) + add_summary(...) would give, with what Kalshi's feed says on top: `s` is
    espn.situation for the game (schedule fields, and the spot and down if Kalshi's summary is behind its plays)."""
    s = dict(s)
    s.pop("_ids", None)
    side = {m["home_id"]: 0, m["away_id"]: 1}
    teams = (s["home"], s["away"])
    ps = plays(game_stats)
    passers, latest = passing(ps, side)
    s["passers"] = {teams[k]: v for k, v in passers.items()}
    s["last_passer"] = {teams[k]: v for k, v in latest.items()}
    kicks = [p for p in ps if p["play_type"] in ("kickoff", "onside_kickoff")]
    s["h2_receiver"] = side.get(kicks[0]["possession"], 1) if kicks else s.get("h2_receiver", 1)
    s["source"] = "kalshi"
    s["ot_poss"] = ot_possessions(ps, side)
    if not ps:
        return s
    last = ps[-1]
    fresh = (details.get("last_play") or {}).get("play_id") == last["play_id"]
    # score and points per quarter: each play carries the score after it
    home_pts = max(int(details.get("home_points") or 0) if fresh else 0, int(last.get("home_points") or 0))
    away_pts = max(int(details.get("away_points") or 0) if fresh else 0, int(last.get("away_points") or 0))
    s.update(home_score=home_pts, away_score=away_pts, margin=home_pts - away_pts)
    ends, prev = {}, (0, 0)
    for p in ps:
        ends[p["period"]] = (int(p.get("home_points") or 0), int(p.get("away_points") or 0))
    lines = [[], []]
    for per in sorted(ends):
        h, a = ends[per]
        lines[0].append(float(h - prev[0]))
        lines[1].append(float(a - prev[1]))
        prev = (h, a)
    s["linescores"] = lines
    # the clock: Kalshi's summary when it describes the latest play, else the latest play's (the time it was snapped)
    period = int(details.get("quarter") or last["period"]) if fresh else last["period"]
    qclock = _secs(details.get("clock")) if fresh else None
    if qclock is None:
        qclock = _secs(last.get("clock")) or 0.0
    if s.get("status") == "in":
        s.update(period=period, clock=_game_clock(period, qclock), ot_clock=qclock if period > 4 else None,
                 display_clock=f"{int(qclock) // 60}:{int(qclock) % 60:02d}")
    s["last_play"] = _text(last).strip()
    s["feed_time"] = datetime.fromtimestamp(last["wall_clock"], timezone.utc).isoformat(timespec="seconds") \
        if last.get("wall_clock") else s.get("feed_time")
    to = [details.get("home_timeouts_remaining"), details.get("away_timeouts_remaining")]
    if fresh and None not in to:
        s["timeouts"] = to
    # whose ball, where: the latest play that wasn't a timeout or a stoppage
    key = next((p for p in reversed(ps) if p["play_type"] not in QUIET), last)
    before = ps[ps.index(key) - 1] if ps.index(key) > 0 else None
    gained = [int(key.get("home_points") or 0) - (int(before.get("home_points") or 0) if before else 0),
              int(key.get("away_points") or 0) - (int(before.get("away_points") or 0) if before else 0)]
    scorer = 0 if gained[0] > gained[1] else (1 if gained[1] > gained[0] else None)
    text = _text(key).upper()
    sit = details.get("situation") or {}
    if scorer is not None and "SAFETY" in text:
        s.update(pos=scorer, kick=True, yl=float("nan"), down=1, togo=10.0)   # the scorers take the free kick
    elif scorer is not None or key["play_type"] in ("extra_point", "two_point_conversion", "missed_two_point_conversion"):
        kicker = scorer if scorer is not None else side.get(key.get("possession"), 0)
        s.update(pos=1 - kicker, kick=True, yl=float("nan"), down=1, togo=10.0)
    elif key["play_type"] in ("kickoff", "onside_kickoff", "punt") and "FUMBLE" not in text:
        kicker = side.get(key.get("possession"))
        if kicker is not None:
            offense = teams[1 - kicker]
            yl = _spot(_text(key), offense)
            s.update(pos=1 - kicker, kick=False, yl=yl if yl is not None else s.get("yl", 75.0), down=1, togo=10.0)
    elif fresh and sit.get("down") and sit.get("possession_team_id") in side:
        pos = side[sit["possession_team_id"]]
        n = float(sit.get("yardline") or 50)
        yl = 100.0 - n if sit.get("side_team_id") == sit["possession_team_id"] else n
        s.update(pos=pos, kick=False, yl=yl, down=int(sit["down"]), togo=float(yl if sit.get("goal_to_go") else sit.get("yfd") or 10))
    # else: ESPN's spot and down stand (they may trail the latest play)
    s["fetched_at"] = datetime.fromtimestamp(espn.source.now(), timezone.utc).isoformat(timespec="seconds")
    return s


_warned: set[str] = set()  # games whose Kalshi feed failed (said once)


def live_state(event: str, sb: dict | None = None, log=print) -> dict:
    """A game's state: from Kalshi's live data while it's in progress, else (or when that fails) espn.game_state."""
    sb = sb or espn.scoreboard()
    sit = espn.situation(event, sb)
    if sit["status"] == "in":
        try:
            g = next(x for x in espn.games(sb) if x["event"] == str(event))
            m = milestone(g["event"], g["away"], g["home"], g["kickoff"])
            if m is None:
                raise LookupError("no Kalshi milestone for this game")
            s = game_state(sit, m, *fetch(m))
            _warned.discard(str(event))
            return s
        except Exception as e:
            if str(event) not in _warned:
                _warned.add(str(event))
                log(f"{sit['away']} @ {sit['home']}: Kalshi feed: {type(e).__name__}: {e}; using ESPN")
    return espn.add_summary(sit)
