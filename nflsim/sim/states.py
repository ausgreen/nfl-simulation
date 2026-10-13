"""Game situations from play-by-play: the state just after each row, in the simulator's terms.

Team 0 is the home team, team 1 the away team. For row i (any play row, ordered by play_id):
  - score after the row; if the row is a touchdown the point after is still pending (`pat` = +1/-1 for the scorer);
  - if a kickoff comes next (after a score, or at halftime), `kick` with `pos` = the receiving team;
  - otherwise the down, distance, field position and offense of the next snap (determined by plays already run);
  - each starter's passing yards through row i, and whether he is out (his team's last 3 dropbacks were by others).
`clock` is the game clock at row i's snap (what a viewer knew), and `ts` the row's wall-clock time.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

SNAP_TYPES = ["pass", "run", "punt", "field_goal", "qb_kneel", "qb_spike", "no_play"]


def game_states(g: pd.DataFrame, starter0, starter1) -> pd.DataFrame:
    g = g[g.qtr <= 4].sort_values("play_id").reset_index(drop=True)
    home, away = g.home_team.iloc[0], g.away_team.iloc[0]
    n = len(g)
    team = np.where(g.posteam.to_numpy() == home, 0, np.where(g.posteam.to_numpy() == away, 1, -1))
    snap = (g.down.notna() & g.play_type.isin(SNAP_TYPES)).to_numpy()
    ko = (g.kickoff_attempt.fillna(0) == 1).to_numpy()
    half = g.game_half.to_numpy()
    first_ko = np.flatnonzero(ko & (half == "Half1"))
    h2 = 1 - team[first_ko[0]] if len(first_ko) and team[first_ko[0]] >= 0 else 1

    passer = g.passer_player_id.to_numpy()
    py = g.passing_yards.fillna(0).to_numpy()
    yds = [np.cumsum(np.where(passer == pid, py, 0.0)) for pid in (starter0, starter1)]
    drop = (g.qb_dropback.fillna(0) == 1).to_numpy() & pd.notna(passer)
    out = []
    for k, pid in enumerate((starter0, starter1)):
        o = np.zeros(n, bool)
        idx = np.flatnonzero(drop & (team == k))
        if len(idx) >= 3:
            other = (passer[idx] != pid).astype(int)
            run3 = pd.Series(other).rolling(3).sum().to_numpy() == 3
            o[idx] = run3
            o = pd.Series(np.where(np.isin(np.arange(n), idx), o, np.nan)).ffill().fillna(0).to_numpy(bool)
        out.append(o)

    # next snap after each row (same game), and whether a kickoff row comes first
    nxt = np.full(n, -1)
    nxt_ko = np.full(n, -1)
    j_snap, j_ko = -1, -1
    for i in range(n - 1, -1, -1):
        nxt[i], nxt_ko[i] = j_snap, j_ko
        if snap[i]:
            j_snap = i
        if ko[i]:
            j_ko = i
    td = (g.touchdown.fillna(0) == 1).to_numpy() & ~g.play_type.isin(["extra_point"]).to_numpy()
    td_team = np.where(g.td_team.to_numpy() == home, 1, np.where(g.td_team.to_numpy() == away, -1, 0))

    margin = (g.total_home_score - g.total_away_score).to_numpy(float)
    rows = []
    for i in range(n):
        j, k = nxt[i], nxt_ko[i]
        kick_first = k >= 0 and (j < 0 or k < j)
        crosses = half[i] == "Half1" and (j < 0 or half[j] != "Half1")
        r = {"row": i, "clock": float(g.game_seconds_remaining.iloc[i]), "margin": margin[i],
             "pat": int(td_team[i]) if td[i] else 0, "h2_receiver": h2, "yds0": yds[0][i], "yds1": yds[1][i],
             "out0": out[0][i], "out1": out[1][i], "kick": False, "pos": 0, "yl": np.nan, "down": 1, "togo": 10.0}
        if crosses and not (kick_first and half[k] == "Half1"):
            r.update(kick=True, pos=h2, clock=1800.0, halftime=True)
        elif kick_first or td[i]:
            recv = team[k] if k >= 0 and team[k] >= 0 else (0 if td_team[i] == -1 else 1)
            r.update(kick=True, pos=int(recv))
        elif j >= 0 and team[j] >= 0:
            r.update(pos=int(team[j]), yl=float(g.yardline_100.iloc[j]), down=int(g.down.iloc[j]),
                     togo=float(g.ydstogo.iloc[j]))
        else:
            continue
        rows.append(r)
    st = pd.DataFrame(rows)
    st["halftime"] = st.get("halftime", False)
    st["halftime"] = st.halftime.fillna(False).astype(bool)
    st["ts"] = pd.to_datetime(g.time_of_day, utc=True, errors="coerce").ffill().bfill().to_numpy()[st.row]
    st["game_secs_row"] = g.game_seconds_remaining.to_numpy()[st.row]
    return st
