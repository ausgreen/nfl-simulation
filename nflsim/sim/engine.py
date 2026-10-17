"""Vectorized game simulator: play the rest of a game forward thousands of times from a live state.

Teams are indexed 0 and 1; `margin` is team 0's score minus team 1's. Each step every unfinished game runs one snap:
the offense picks a play from the call table, the play's result and the clock it uses are drawn from the learned
tables, then possession, down and distance, score and the clock update. Kickoffs and punts (with return fumbles and
touchdowns), turnovers (strip sacks included), field goals, touchdowns (with a point after), safeties, turnovers on
downs, accepted penalties that wipe out the play, halftime, two-minute warnings, timeouts (3 per team per half, called
after a play from a learned table), two-point tries, onside kicks and overtime are handled; penalties on plays that
stand are not.

Each team's completed-pass gains are scaled by its multiplier `mult` (from the pregame expected yards, see
`calibrate`), and its starter can leave the game (pulled or hurt) after any dropback via the exit hazard table.

Optional team columns (default 0 = league average), for team k on offense against the other team's defense
(see nflsim/sim/teams.py):
  pass{k}  logit shift of pass vs run          sack{k}/int{k}/comp{k}  logit shifts of those pass results
  run{k}   yards added to each run              pace{k}  seconds added between snaps (normal clock phases)
  delta{k} overall efficiency (completions up, sacks and interceptions down, runs +4 yards per unit)
  form_mean{k}/form_sd{k}  each simulated game draws log(form) ~ Normal and scales completion gains by exp(form)
  delta_sd                 each simulated game draws a team efficiency shock ~ Normal(0, delta_sd)
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import tables as T

TEAM_COLS = ["pass", "sack", "int", "comp", "run", "pace", "delta", "form_mean", "form_sd"]
# what happened on a snap (record=True): codes into EVENTS
EVENTS = ["completion", "incomplete", "sack", "interception", "fumble lost", "run", "punt", "field goal",
          "missed field goal", "kneel", "spike", "touchdown pass", "touchdown run", "defensive touchdown", "safety",
          "penalty"]
RUN_YDS_PER_DELTA = 4.0
WARN_SNAP = 6.0   # a play snapped at least this many seconds before a two-minute warning ends before it
# end of a half, when a field goal helps (first half; later, when it ties or takes the lead): from FG_RANGE yards or
# closer the last snap (LAST_SNAP seconds left or less) is a kick, and a play that would run out the clock is
# stopped PLAY_SECS after the snap with a timeout (incompletions stop it anyway). Real teams kicked on 80-96% of
# last first-half snaps in range; with the clock tables alone the simulator kicked on 20-36%.
FG_RANGE, LAST_SNAP, PLAY_SECS = 40, 6.0, 5.0
# how the drive in progress ended (drive=True): codes into DRIVE_RESULTS, checked in this order
DRIVE_RESULTS = ["defensive touchdown", "safety", "touchdown", "field goal", "missed field goal", "punt",
                 "turnover on downs", "interception", "fumble lost", "end of half", "end of game"]


def simulate(tb: T.Tables, start: pd.DataFrame, n: int, seed: int = 0, max_steps: int = 400,
             points: bool = False, record: bool = False, drive: bool = False, quarters: bool = False,
             overtime: bool = True):
    """Final passing yards of both starters: array (rows, n, 2); with points=True also each team's points
    scored from the start state on (rows, n, 2). With record=True (use a small max_steps), returns a dict of the
    state after the last step instead: yds, pts, pos, yl, down, togo, clock, margin, kick, done, event (code into
    EVENTS, -1 if no snap), gain, on_downs; each shaped (rows, n[, 2]). With drive=True, each game stops when the
    drive in progress ends (the receiving team's drive if a kickoff is pending) and the result is a dict: result
    (code into DRIVE_RESULTS, -1 if no snap), plays, used (seconds), pts, yds, margin, clock. With points=True and
    quarters=True, also the points each team scored in each regulation quarter (rows, n, 4, 2), credited to the
    quarter in which the scoring snap started (overtime points count in pts, in no quarter).

    overtime=True plays overtime after a tied regulation (NFL rules since 2025: each team gets a possession, then the
    next score wins; the regular season's 10-minute period can end tied, a postseason game (`post`) plays 15-minute
    periods until someone scores). A start state in overtime has `ot` set, `clock` = seconds left in the period and
    `ot_poss0`/`ot_poss1` = whether that team has had its possession; `pos` = -1 with a kickoff pending is a coin
    toss."""
    rng = np.random.default_rng(seed)
    r = len(start)
    rid = np.repeat(np.arange(r), n)

    def rep(col, dtype):
        return np.repeat(start[col].to_numpy(dtype), n)

    def team(col):
        """(rows, 2) parameter for team 0 / team 1 on offense; zeros when absent."""
        return np.stack([start[f"{col}{k}"].to_numpy(float) if f"{col}{k}" in start else np.zeros(r)
                         for k in (0, 1)], 1)

    clock, margin = rep("clock", float), rep("margin", float)
    pos, kick = rep("pos", np.int8), rep("kick", bool)
    ot = rep("ot", bool) if "ot" in start else np.zeros(r * n, bool)
    ot_poss = np.stack([rep(f"ot_poss{k}", bool) if f"ot_poss{k}" in start else np.zeros(r * n, bool)
                        for k in (0, 1)], 1)
    post = rep("post", bool) if "post" in start else np.zeros(r * n, bool)
    # timeouts left per team (3 a half; 2 in overtime)
    tl = np.stack([rep(f"to{k}", float) if f"to{k}" in start else np.full(r * n, 3.0) for k in (0, 1)], 1)
    tl = np.nan_to_num(tl, nan=3.0).clip(0, 3).astype(np.int8)
    toss = pos < 0
    pos[toss] = rng.integers(0, 2, toss.sum())
    yl, down, togo = rep("yl", float), rep("down", np.int8), rep("togo", float)
    h2 = rep("h2_receiver", np.int8)
    yds = np.stack([rep("yds0", float), rep("yds1", float)], 1)
    pts = np.zeros((r * n, 2))
    qpts = np.zeros((r * n, 4, 2)) if quarters else None
    out = np.stack([rep("out0", bool), rep("out1", bool)], 1)
    mult = np.stack([rep("mult0", float), rep("mult1", float)], 1)
    tp = {c: team(c) for c in TEAM_COLS}
    delta = tp["delta"][rid]
    if "delta_sd" in start:
        delta = delta + rng.normal(size=(r * n, 2)) * rep("delta_sd", float)[:, None]
    if (tp["form_sd"] > 0).any() or (tp["form_mean"] != 0).any():
        mult = mult * np.exp(tp["form_mean"][rid] + rng.normal(size=(r * n, 2)) * tp["form_sd"][rid])
    done = clock <= 0

    def try_pts(mt, ck_):
        """Points from the try after a touchdown: an extra point, or two (go for it by the scorer's margin after
        the touchdown, `mt`, and the clock)."""
        u = rng.random(len(mt))
        kick_pts = (u < tb.p_xp).astype(float)
        if tb.two_pt is None:
            return kick_pts
        go = tb.two_pt.probs[tb.two_pt.cell(mt=np.clip(mt, -20, 20).astype(int) + 20, c=T.c_bin(ck_)), 1]
        return np.where(rng.random(len(mt)) < go, 2.0 * (u < tb.p_two), kick_pts)

    if "pat" in start:  # a touchdown whose point after is still to come (+1 team 0 scored, -1 team 1)
        pat = rep("pat", float)
        v = np.zeros(r * n)
        has = pat != 0
        v[has] = try_pts(margin[has] * pat[has], clock[has])
        margin += pat * v
        pts[:, 0] += np.where(pat > 0, v, 0)
        pts[:, 1] += np.where(pat < 0, v, 0)
        if quarters:
            q0 = np.clip((3600 - clock) // 900, 0, 3).astype(int)
            qpts[np.arange(r * n), q0, 0] += np.where(pat > 0, v, 0)
            qpts[np.arange(r * n), q0, 1] += np.where(pat < 0, v, 0)
    # missing situation fields (e.g. a kick pending) get safe defaults
    yl = np.where(np.isnan(yl), 75, yl)
    togo = np.where(np.isnan(togo), 10, togo)
    down = np.where(down < 1, 1, down).astype(np.int8)
    event = np.full(r * n, -1, dtype=np.int8)
    last_gain = np.zeros(r * n)
    last_downs = np.zeros(r * n, bool)
    clock0 = clock.copy()
    result = np.full(r * n, -1, dtype=np.int8)
    plays = np.zeros(r * n, dtype=np.int16)

    for _ in range(max_steps):
        a = np.flatnonzero(~done)
        if not len(a):
            break
        # pending kickoffs: the receiving team (pos) starts a drive
        k = a[kick[a]]
        if len(k):
            yl[k] = tb.kick_yl[(rng.random(len(k)) * len(tb.kick_yl)).astype(int)]
            down[k], togo[k], kick[k] = 1, 10, False
            # onside try, by the kicking team's margin and the clock (not within a single-drive simulation)
            ons = np.zeros(len(k), bool)
            if tb.onside is not None and not drive and len(tb.onside_rec) and len(tb.onside_fail):
                kicker = 1 - pos[k]
                km = margin[k] * np.where(kicker == 0, 1.0, -1.0)
                ons = rng.random(len(k)) < tb.onside.probs[tb.onside.cell(c=T.c_bin(clock[k]), s=T.s_bin(km)), 1]
                n_rec, n_fail = len(tb.onside_rec), len(tb.onside_fail)
                rec = ons & (rng.random(len(k)) < n_rec / (n_rec + n_fail))
                fail = ons & ~rec
                yl[k[rec]] = tb.onside_rec[rng.integers(0, n_rec, rec.sum())]
                pos[k[rec]] = kicker[rec]
                yl[k[fail]] = tb.onside_fail[rng.integers(0, n_fail, fail.sum())]
            u = rng.random(len(k))
            u[ons] = 1.0  # no return touchdown or fumble on an onside kick
            ret_td = k[u < tb.kicks.get("ko_td", 0.0)]          # returned for a touchdown: the scorers kick off
            lost = k[(u >= tb.kicks.get("ko_td", 0.0)) & (u < tb.kicks.get("ko_td", 0.0) + tb.kicks.get("ko_fum", 0.0))]
            if len(ret_td):
                rp = pos[ret_td].astype(int)
                sc = 6 + try_pts(margin[ret_td] * np.where(rp == 0, 1.0, -1.0) + 6, clock[ret_td])
                pts[ret_td, rp] += sc
                margin[ret_td] += np.where(rp == 0, sc, -sc)
                if quarters:
                    reg = ~ot[ret_td]
                    qpts[ret_td[reg], np.clip((3600 - clock[ret_td[reg]]) // 900, 0, 3).astype(int), rp[reg]] += sc[reg]
                ot_poss[ret_td[ot[ret_td]], rp[ot[ret_td]]] = True
                pos[ret_td], kick[ret_td] = 1 - rp, True
            if len(lost):  # the kicking team recovers where the return ended
                pos[lost], yl[lost] = 1 - pos[lost], 100 - yl[lost]
            if drive:
                for code, rows in (("touchdown", ret_td), ("fumble lost", lost)):
                    result[rows], done[rows] = DRIVE_RESULTS.index(code), True
            a = a[~kick[a] & ~done[a]]
            if not len(a):
                continue
        p = pos[a].astype(int)
        ra = rid[a]
        dl = delta[a, p]
        sign = np.where(p == 0, 1.0, -1.0)
        mg = margin[a] * sign
        y, dn, tg, ck = yl[a], down[a], togo[a], clock[a]
        d, t, z, zg = dn - 1, T.t_bin(tg), T.z_bin(y), T.zg_bin(y)
        c, s, s3 = T.c_bin(ck), T.s_bin(mg), T.s3_bin(mg)
        pc = tb.call.probs[tb.call.cell(d=d, t=t, z=z, zc=T.zc_bin(y), c=c, s=s)].copy()
        lean = np.exp(tp["pass"][ra, p])
        both = pc[:, 0] + pc[:, 1]
        pc[:, 0] = both * pc[:, 0] * lean / (pc[:, 0] * lean + pc[:, 1])
        pc[:, 1] = both - pc[:, 0]
        call = T.sample_rows(rng, pc)
        left = np.where(ck > 1800, ck - 1800, ck)
        wants_fg = (ck > 1800) | ((mg >= -3) & (mg <= 0))
        call[(left <= LAST_SNAP) & (y <= FG_RANGE) & wants_fg] = 3
        m = len(a)
        gain = np.zeros(m)
        td = np.zeros(m, bool)        # offense touchdown
        dtd = np.zeros(m, bool)       # defense touchdown
        turn = np.zeros(m, bool)      # possession changes at new_yl (for the new offense)
        new_yl = np.full(m, np.nan)
        fg_good = np.zeros(m, bool)
        cat = np.zeros(m, int)

        ev = np.full(m, 5, dtype=np.int8)
        ip = np.flatnonzero(call == 0)
        if len(ip):
            q = tb.pass_out.probs[tb.pass_out.cell(d=d[ip], t=t[ip], z=z[ip], c=c[ip], s=s[ip])]
            rp, pp, dp = ra[ip], p[ip], dl[ip]
            f_comp = np.exp(tp["comp"][rp, pp] + dp)
            f_sack = np.exp(tp["sack"][rp, pp] - dp)
            f_int = np.exp(tp["int"][rp, pp] - dp)
            # outcomes: comp, inc, sack, int, int_td, comp_fum, sack_fum
            q = q * np.column_stack([f_comp, np.ones(len(ip)), f_sack, f_int, f_int, f_comp, f_sack])
            o = T.sample_rows(rng, q)
            ev[ip] = np.array([0, 1, 2, 3, 3, 4, 4], dtype=np.int8)[o]  # comp, inc, sack, int, int (TD), fumble x2
            comp = (o == 0) | (o == 5)
            ic = ip[comp]
            g = tb.comp_gain.sample(rng, tb.comp_gain.cell(d=d[ic], t=t[ic], z=z[ic], zg=zg[ic], c=c[ic]))
            if tb.comp_scale is not None:
                g = g * tb.comp_scale[T.script_cell(c[ic], s[ic])]
            g = np.minimum(np.floor(g * mult[a[ic], p[ic]] + rng.random(len(ic))), y[ic])  # unbiased rounding
            gain[ic] = g
            credit = ~out[a[ic], p[ic]]
            yds[a[ic][credit], p[ic][credit]] += g[credit]
            td[ic] = g >= y[ic]
            fum = ip[o == 5]
            fum = fum[~td[fum]]
            turn[fum] = True
            isk = ip[(o == 2) | (o == 6)]
            gain[isk] = tb.sack_gain.sample(rng, tb.sack_gain.cell(z=z[isk]))
            # strip sack: the defense recovers at the spot, now and then returning it for a touchdown
            isf = ip[o == 6]
            ret = rng.random(len(isf)) < tb.kicks.get("sack_fum_td", 0.0)
            dtd[isf[ret]] = True
            turn[isf[~ret]] = True
            new_yl[isf] = 100 - np.minimum(y[isf] - gain[isf], 99)
            ii = ip[o == 3]
            turn[ii] = True
            dtd[ip[o == 4]] = True
            to = np.concatenate([fum, ii])
            new_yl[to] = tb.turnover_yl.sample(rng, tb.turnover_yl.cell(z=z[to]))
            cat[ip] = np.select([comp, o == 1, o == 2], [1, 2, 3], 4)  # a strip sack is a turnover
            # the starter may leave after this dropback
            on = ~out[a[ip], p[ip]]
            h = tb.exit_hazard.probs[tb.exit_hazard.cell(c=c[ip], s=s[ip]), 1]
            leave = on & (rng.random(len(ip)) < h)
            out[a[ip][leave], p[ip][leave]] = True

        ir = np.flatnonzero(call == 1)
        if len(ir):
            o = tb.run_out.sample(rng, tb.run_out.cell(z=z[ir]))
            g = tb.run_gain.sample(rng, tb.run_gain.cell(t=t[ir], z=z[ir], zg=zg[ir]))
            # stochastic rounding: a +0.4 yard shift adds a yard to 40% of runs (plain rounding would add 0 or 1 to all)
            if tb.run_shift is not None:
                g = g + tb.run_shift[T.script_cell(c[ir], s[ir])]
            g = np.floor(g + tp["run"][ra[ir], p[ir]] + RUN_YDS_PER_DELTA * dl[ir] + rng.random(len(ir)))
            g = np.minimum(g, y[ir])
            gain[ir] = g
            fum = ir[o == 1]
            turn[fum] = True
            ev[fum] = 4
            new_yl[fum] = tb.turnover_yl.sample(rng, tb.turnover_yl.cell(z=z[fum]))
            td[ir[o == 0]] = g[o == 0] >= y[ir[o == 0]]
            cat[ir] = 0

        iu = np.flatnonzero(call == 2)
        turn[iu] = True
        new_yl[iu] = tb.punt_yl.sample(rng, tb.punt_yl.cell(z=z[iu]))
        cat[iu] = 5
        # the returner fumbles (the punting team keeps the ball where the return ended) or scores
        u = rng.random(len(iu))
        p_td, p_fum = tb.kicks.get("punt_td", 0.0), tb.kicks.get("punt_fum", 0.0)
        dtd[iu[u < p_td]] = True
        turn[iu[u < p_td]] = False
        muff = iu[(u >= p_td) & (u < p_td + p_fum)]

        ifg = np.flatnonzero(call == 3)
        fg_good[ifg] = rng.random(len(ifg)) < tb.fg_make[T.fg_bin(y[ifg])]
        miss = ifg[~fg_good[ifg]]
        turn[miss] = True
        new_yl[miss] = np.minimum(100 - (y[miss] + 7), 80)
        cat[ifg] = 6

        # accepted penalty, no play: the ball moves (never into or out of an end zone); a new set of downs or the
        # same down again
        ipn = np.flatnonzero(call == 6)
        pen_set = np.zeros(m, bool)
        if len(ipn):
            code = tb.pen.sample(rng, tb.pen.cell(d=d[ipn], t=t[ipn], z=z[ipn])).astype(int)
            pen_set[ipn] = np.mod(code, 2) == 1
            gain[ipn] = np.clip((code - np.mod(code, 2)) // 2, y[ipn] - 99, y[ipn] - 1)
            cat[ipn] = 10
            ev[ipn] = 15

        ik = np.flatnonzero(call == 4)
        gain[ik] = -1
        cat[ik] = 7
        cat[call == 5] = 8

        after = y - gain
        safety = (after >= 100) & ~turn & ~td & ~dtd
        score = td | dtd | fg_good | safety
        cat[score] = 9
        cat[turn & ~score & (call <= 1)] = 4
        xp = np.zeros(m)
        it, idt = np.flatnonzero(td), np.flatnonzero(dtd)
        xp[it], xp[idt] = try_pts(mg[it] + 6, ck[it]), try_pts(-mg[idt] + 6, ck[idt])
        off_pts = np.where(td, 6 + xp, 0) + np.where(fg_good, 3, 0)
        def_pts = np.where(dtd, 6 + xp, 0) + np.where(safety, 2, 0)
        margin[a] += (off_pts - def_pts) * sign
        pts[a, p] += off_pts
        pts[a, 1 - p] += def_pts
        if quarters:
            q = np.clip((3600 - ck) // 900, 0, 3).astype(int)
            reg = ~ot[a]
            qpts[a[reg], q[reg], p[reg]] += off_pts[reg]
            qpts[a[reg], q[reg], 1 - p[reg]] += def_pts[reg]

        # possession, field position, down and distance
        np_ = p.copy()
        ny, nd, nt = after.copy(), dn + 1, tg - gain
        nk = np.zeros(m, bool)
        first = ~turn & (gain >= tg)
        nd[first], nt[first] = 1, np.minimum(10, after[first])
        if len(ipn):
            first[ipn] = pen_set[ipn]
            nd[ipn] = np.where(pen_set[ipn], 1, dn[ipn])
            nt[ipn] = np.where(pen_set[ipn], np.minimum(10, after[ipn]), tg[ipn] - gain[ipn])
        on_downs = ~turn & ~first & (nd > 4)
        np_[on_downs] = 1 - p[on_downs]
        ny[on_downs] = 100 - after[on_downs]
        nd[on_downs], nt[on_downs] = 1, np.minimum(10, ny[on_downs])
        np_[turn] = 1 - p[turn]
        ny[turn] = np.clip(new_yl[turn], 1, 99)
        nd[turn], nt[turn] = 1, np.minimum(10, ny[turn])
        np_[muff], ny[muff] = p[muff], np.clip(100 - new_yl[muff], 1, 99)
        nt[muff] = np.minimum(10, ny[muff])
        # scores: kickoff to the other team (after a defensive TD the offense receives; after a safety, the scorers)
        kicked = td | fg_good | safety
        np_[kicked] = 1 - p[kicked]
        np_[dtd] = p[dtd]
        nk[kicked | dtd] = True

        # clock (the offense's pace applies in normal clock phases only)
        # a timeout after the play (only by a team with one left), then the clock to the next snap
        tlo, tld = tl[a, p], tl[a, 1 - p]
        tox = np.zeros(m, np.int8)
        if tb.timeout is not None:
            pt = tb.timeout.probs[tb.timeout.cell(c=c, s=s, cat=cat, tlo=tlo, tld=tld)].copy()
            pt[:, 1] *= tlo > 0
            pt[:, 2] *= tld > 0
            tox = T.sample_rows(rng, pt).astype(np.int8)
            caller = np.where(tox == 1, p, 1 - p)
            used = tox > 0
            tl[a[used], caller[used]] -= 1
        dt = tb.elapsed.sample(rng, tb.elapsed.cell(cat=cat, tox=tox, c=c, s3=s3))
        normal = ((c == 0) | (c == 2)) & (dt > 0)
        dt = np.where(normal, np.maximum(dt + tp["pace"][ra, p], 1), dt)
        nc = ck - dt
        # two-minute warning: a play snapped well before it stops the clock there (overtime's is not modeled)
        for warn in (1920.0, 120.0):
            stop = (ck - warn >= WARN_SNAP) & (nc < warn) & ~ot[a]
            nc[stop] = warn
        # save a snap for the kick
        end = np.where(ck > 1800, 1800.0, 0.0)
        save = ((nc <= end) & (ck - end > PLAY_SECS) & (np_ == p) & ~nk & (ny <= FG_RANGE) & wants_fg
                & ((tl[a, p] > 0) | (tox == 1) | (cat == 2) | (cat == 8)))
        spend = save & (tox != 1) & (cat != 2) & (cat != 8)
        tl[a[spend], p[spend]] -= 1
        nc[save] = ck[save] - PLAY_SECS
        half = (ck > 1800) & (nc <= 1800)
        nc[half] = 1800
        np_[half] = h2[a][half]
        nk[half] = True
        tl[a[half]] = 3
        ended = nc <= 0
        period_end = ended.copy()  # the drive in progress ends with the period, overtime or not
        if overtime:
            in_ot = ot[a]
            # overtime: a possession is over when the ball changes hands or a kickoff follows
            over_ot = in_ot & ((np_ != p) | nk)
            ot_poss[a[over_ot], p[over_ot]] = True
            mg_now = margin[a]
            won = in_ot & (mg_now != 0) & ((ot_poss[a, 0] & ot_poss[a, 1]) | dtd | safety)
            # a tied regulation (or a tied postseason overtime period) goes on: new period, coin toss
            more = ended & (mg_now == 0) & (~in_ot | post[a])
            nc[more] = np.where(post[a[more]], 900.0, 600.0)
            start_ot = more & ~in_ot
            np_[start_ot], nk[start_ot] = rng.integers(0, 2, start_ot.sum()), True
            ot[a[start_ot]] = True
            tl[a[more]] = 2
            ended = (ended & ~more) | won

        pos[a], yl[a], down[a], togo[a], kick[a], clock[a] = np_, ny, nd, np.maximum(nt, 1), nk, nc
        done[a[ended]] = True
        if drive:
            over = turn | on_downs | kicked | dtd | half | ended | period_end
            code = np.select([dtd, safety, td, fg_good, call == 3, call == 2, on_downs, ev == 3, ev == 4, half,
                              ended | period_end],
                             np.arange(len(DRIVE_RESULTS)), -1)
            plays[a] += 1
            result[a[over]] = code[over]
            done[a[over]] = True
        if record:
            ev[iu], ev[ifg] = 6, np.where(fg_good[ifg], 7, 8)
            ev[ik], ev[call == 5] = 9, 10
            ev[td & (call == 0)], ev[td & (call == 1)], ev[dtd], ev[safety] = 11, 12, 13, 14
            event[a], last_gain[a], last_downs[a] = ev, gain, on_downs

    if drive:
        sh = (r, n)
        return {"result": result.reshape(sh), "plays": plays.reshape(sh), "used": (clock0 - clock).reshape(sh),
                "pts": pts.reshape(r, n, 2), "yds": yds.reshape(r, n, 2), "margin": margin.reshape(sh),
                "clock": clock.reshape(sh)}
    if record:
        sh = (r, n)
        return {"yds": yds.reshape(r, n, 2), "pts": pts.reshape(r, n, 2), "pos": pos.reshape(sh),
                "yl": yl.reshape(sh), "down": down.reshape(sh), "togo": togo.reshape(sh), "clock": clock.reshape(sh),
                "margin": margin.reshape(sh), "kick": kick.reshape(sh), "done": done.reshape(sh),
                "event": event.reshape(sh), "gain": last_gain.reshape(sh), "on_downs": last_downs.reshape(sh)}
    if points and quarters:
        return yds.reshape(r, n, 2), pts.reshape(r, n, 2), qpts.reshape(r, n, 4, 2)
    if points:
        return yds.reshape(r, n, 2), pts.reshape(r, n, 2)
    return yds.reshape(r, n, 2)


def kickoff_start(mult0: float = 1.0, mult1: float = 1.0) -> pd.DataFrame:
    return pd.DataFrame({"clock": [3600.0], "margin": [0.0], "pos": [0], "kick": [True], "yl": [np.nan],
                         "down": [1], "togo": [10.0], "h2_receiver": [1], "yds0": [0.0], "yds1": [0.0],
                         "out0": [False], "out1": [False], "mult0": [mult0], "mult1": [mult1]})


def calibrate(tb: T.Tables, n: int = 4000, grid=(0.6, 0.8, 1.0, 1.2, 1.4)):
    """Map pregame expected yards -> gain multiplier: simulate full games with team 0's multiplier on a grid
    (team 1 at 1.0, both receiving first half the time) and invert mean yards by interpolation."""
    means = []
    for gm in grid:
        st = pd.concat([kickoff_start(gm, 1.0), kickoff_start(gm, 1.0).assign(pos=1, h2_receiver=0)],
                       ignore_index=True)
        means.append(simulate(tb, st, n, seed=int(gm * 100))[..., 0].mean())
    means = np.asarray(means)

    def to_mult(mu):
        return np.interp(np.asarray(mu, float), means, grid, left=None, right=None)

    return to_mult, dict(zip(grid, means))
