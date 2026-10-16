"""Live game state from ESPN's public site API (scoreboard + game summary), in the game simulator's terms.

The feed runs a few seconds behind TV and can lag a score or a change of possession; the live app lets the viewer
correct the state by hand. Team codes are mapped to nflverse's (WSH -> WAS, LAR -> LA).
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

from . import feed

BASE = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"
TO_NFLVERSE = {"WSH": "WAS", "LAR": "LA"}

source = feed.Live()  # replace with feed.Recorder / feed.Replay via use()


def use(src) -> None:
    """Send every ESPN request through `src` (an object with get(url) and now())."""
    global source
    source = src


def _get(url: str) -> dict:
    return source.get(url)


def team_code(abbr: str) -> str:
    return TO_NFLVERSE.get(abbr, abbr)


def scoreboard() -> dict:
    return _get(f"{BASE}/scoreboard")


def summary(event: str) -> dict:
    return _get(f"{BASE}/summary?event={event}")


def games(sb: dict | None = None) -> list[dict]:
    """This week's games: event id, teams (nflverse codes), kickoff, status, score."""
    sb = sb or scoreboard()
    out = []
    for e in sb.get("events", []):
        c = e["competitions"][0]
        t = {x["homeAway"]: x for x in c["competitors"]}
        st = e["status"]
        out.append({"event": e["id"], "home": team_code(t["home"]["team"]["abbreviation"]),
                    "away": team_code(t["away"]["team"]["abbreviation"]), "kickoff": e["date"],
                    "state": st["type"]["state"], "detail": st["type"]["shortDetail"],
                    "homeScore": int(t["home"].get("score") or 0), "awayScore": int(t["away"].get("score") or 0)})
    order = {"in": 0, "pre": 1, "post": 2}
    return sorted(out, key=lambda g: (order.get(g["state"], 3), g["kickoff"]))


def _yards_to_goal(text: str | None, offense: str) -> float | None:
    """'JAX 47' with offense PHI -> 47 (opponent territory); 'PHI 30' with offense PHI -> 70; '50' -> 50."""
    if not text:
        return None
    parts = text.split()
    if len(parts) == 1:
        return float(parts[0]) if parts[0].isdigit() else None
    side, n = team_code(parts[0]), float(parts[1])
    return 100 - n if side == offense else n


def game_state(event: str, sb: dict | None = None, sm: dict | None = None) -> dict:
    """Current situation for one game. Keys: home, away, season, week, clock (game seconds left, regulation),
    period, display_clock, margin (home - away), linescores ([home, away] points per period), pos (0 home / 1 away), kick, yl (yards to the goal), down, togo,
    passers {team: [(name, espn_id, att, yds)]}, last_play, feed_time (wall clock of the last play), status,
    h2_receiver (0/1), overtime (in overtime, or regulation ended tied; the simulator stops at the end of
    regulation, so the live app shows the situation without simulating; pos is None before the overtime
    kickoff if ESPN hasn't posted the receiver). `sb`/`sm`: an already-fetched scoreboard / summary (fetched here otherwise)."""
    return add_summary(situation(event, sb), sm)


def _linescores(team: dict) -> list[float]:
    """Points per period (1-4 regulation, 5+ overtime) from a scoreboard competitor; periods not started are left out."""
    out = []
    for x in team.get("linescores") or []:
        i = int(x.get("period") or len(out) + 1) - 1
        out += [0.0] * (i + 1 - len(out))
        out[i] = float(x.get("value") or 0)
    return out


def situation(event: str, sb: dict | None = None) -> dict:
    """The scoreboard part of game_state (no passers, feed_time or h2_receiver): one request covers every game."""
    sb = sb or scoreboard()
    ev = next((e for e in sb.get("events", []) if e["id"] == str(event)), None)
    if ev is None:
        raise KeyError(f"event {event} not on this week's scoreboard")
    c = ev["competitions"][0]
    t = {x["homeAway"]: x for x in c["competitors"]}
    ids = {t["home"]["team"]["id"]: 0, t["away"]["team"]["id"]: 1}
    home, away = team_code(t["home"]["team"]["abbreviation"]), team_code(t["away"]["team"]["abbreviation"])
    st = ev["status"]
    period = int(st.get("period") or 0)
    qclock = float(st.get("clock") or 0.0)
    clock = 3600.0 if period == 0 else (max(0, 4 - period) * 900 + qclock if period <= 4 else 0.0)
    margin = int(t["home"].get("score") or 0) - int(t["away"].get("score") or 0)
    sit = c.get("situation") or {}
    last = sit.get("lastPlay") or {}
    s = {"event": str(event), "home": home, "away": away, "season": int(sb["season"]["year"]),
         "week": int(sb["week"]["number"]), "clock": clock, "period": period,
         "display_clock": st.get("displayClock"), "status": st["type"]["state"], "detail": st["type"]["shortDetail"],
         "margin": margin, "home_score": int(t["home"].get("score") or 0),
         "away_score": int(t["away"].get("score") or 0), "last_play": (last.get("text") or "").strip(),
         "timeouts": [sit.get("homeTimeouts"), sit.get("awayTimeouts")],
         "linescores": [_linescores(t[k]) for k in ("home", "away")]}
    pos_id = sit.get("possession")
    down = sit.get("down")
    kind = (last.get("type") or {}).get("text") or ""
    if pos_id not in ids and down and down > 0 and kind == "Kickoff":
        # right after a return ESPN posts the down and spot but no possession; the returning team (the kickoff
        # play's team) has the ball (20 of 20 cases in the 2026-10-12 BUF @ LA recording)
        pos_id = (last.get("team") or {}).get("id")
    if pos_id in ids and down and down > 0:
        pos = ids[pos_id]
        offense = home if pos == 0 else away
        yl = _yards_to_goal(sit.get("possessionText"), offense)
        s.update(pos=pos, kick=False, yl=yl if yl is not None else 75.0, down=int(down),
                 togo=float(sit.get("distance") or 10))
    else:  # between plays after a score, or before a kickoff: the team not scoring last receives
        scorer = (last.get("team") or {}).get("id")
        if last.get("scoreValue") and scorer in ids:
            recv = 1 - ids[scorer]
        else:
            recv = ids.get(pos_id, 0)
        s.update(pos=recv, kick=True, yl=float("nan"), down=1, togo=10.0)
        s["halftime"] = kind == "End of Half" or (period == 2 and qclock <= 0)  # receiver set by add_summary
    s["overtime"] = period > 4 or (period == 4 and qclock <= 0 and margin == 0 and s["status"] == "in")
    if s["overtime"] and kind == "End of Regulation":  # before the overtime kickoff; possession = the receiver
        s.update(pos=ids.get(pos_id), kick=True, yl=float("nan"), down=1, togo=10.0)
    s["_ids"] = ids
    return s


def add_summary(s: dict, sm: dict | None = None) -> dict:
    """Add passers (box score), feed_time and h2_receiver from the game summary to a situation()."""
    s = dict(s)
    ids = s.pop("_ids")
    sm = sm if sm is not None else summary(s["event"])
    passers = {}
    for team in sm.get("boxscore", {}).get("players", []):
        code = team_code(team["team"]["abbreviation"])
        for stat in team.get("statistics", []):
            if stat.get("name") != "passing":
                continue
            lab = stat.get("labels", [])
            rows = []
            for a in stat.get("athletes", []):
                v = dict(zip(lab, a.get("stats", [])))
                att = int(str(v.get("C/ATT", "0/0")).split("/")[-1] or 0)
                rows.append((a["athlete"]["displayName"], a["athlete"]["id"], att, float(v.get("YDS", 0) or 0)))
            passers[code] = sorted(rows, key=lambda r: -r[2])
    s["passers"] = passers
    wall = None
    for d in ([(sm.get("drives") or {}).get("current")] + list(reversed((sm.get("drives") or {}).get("previous", [])))):
        if d and d.get("plays"):
            wall = d["plays"][-1].get("wallclock")
            break
    s["feed_time"] = wall
    # the team with the first drive received the opening kickoff, so the other team receives in the 2nd half
    drives = (sm.get("drives") or {}).get("previous") or []
    first = (drives[0].get("team") or {}).get("id") if drives else None
    s["h2_receiver"] = 1 - ids[first] if first in ids else 1
    if s.pop("halftime", False):
        s["pos"] = s["h2_receiver"]
    s["fetched_at"] = datetime.fromtimestamp(source.now(), timezone.utc).isoformat(timespec="seconds")
    return s


SIGNATURE = ("status", "period", "clock", "display_clock", "home_score", "away_score", "pos", "kick", "yl", "down", "togo", "last_play")


def signature(s: dict) -> tuple:
    """What the simulation depends on, from a situation() or game_state(): unchanged signature, unchanged output
    (passing yards change only with a play, and every play changes the clock, the spot or the last-play text)."""
    return tuple(None if isinstance(v, float) and math.isnan(v) else v for v in (s.get(k) for k in SIGNATURE))


def box(s: dict) -> tuple:
    """The box score's passing lines from a game_state(): (team, espn_id, att, yds) per passer. ESPN's box score
    often lags the scoreboard by a play (the yards of a completion arrive 20-60 s after it), so a game whose
    signature is unchanged still needs re-simulating when this changes."""
    return tuple(sorted((team, r[1], r[2], r[3]) for team, rows in (s.get("passers") or {}).items() for r in rows))
