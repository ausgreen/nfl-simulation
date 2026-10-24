"""Command-line access to the game simulator and live companion: `nflsim <command> --help`."""

from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

from .data import load


def show(df: pd.DataFrame, a, title: str | None = None) -> None:
    if a.csv:
        df.to_csv(sys.stdout, index=False)
        return
    if title:
        print(f"\n{title}")
    with pd.option_context("display.max_rows", None, "display.max_columns", None, "display.width", 200):
        print(df.to_string(index=False, float_format=lambda v: f"{v:.3f}"))


def cmd_fetch(ds, a):
    """Refresh nflverse passing stats + schedules (lines, starters, roofs) and simulator play-by-play."""
    from . import nflverse
    from .sim import data as sim_data

    nflverse.fetch(ds, range(a.since, ds.current_season + 1) if a.since else None)
    sim_data.fetch(ds, range(a.since or ds.current_season, ds.current_season + 1))


def cmd_sim(ds, a):
    from .sim import data as sim_data, pregame

    if a.action == "fetch":
        sim_data.fetch(ds, range(a.since or ds.current_season, ds.current_season + 1))
        return
    if not a.game_id:
        raise SystemExit("usage: nflsim sim game <game_id>, e.g. 2026_05_TB_DAL")
    pre, x = pregame.replay_game(ds, a.game_id, n=a.n)
    h, w = pre["home"], pre["away"]
    print(f"{w} @ {h} · line {h} {-pre['spread']:+g}, total {pre['total']:g}")
    print(f"Pregame ({a.n * 2:,} simulated games): median score {w} {pre['simAway']:.0f} – {h} {pre['simHome']:.0f}; "
          f"{h} wins {pre['homeWin']:.0%}" + (f", covers {pre['homeCover']:.0%}" if pre["homeCover"] is not None else ""))
    lines = [175, 200, 225, 250, 275, 300]
    rows = []
    for k, team in ((1, w), (0, h)):
        q = pre["qbs"][k]
        rows.append({"team": team, "v3_mu": pre[f"mu{k}"], "sim_median": q["median"], "p10": q["p10"], "p90": q["p90"],
                     **{f"p_{L}": q["probs"][str(L)] for L in lines}, "actual": pre["final_yds"][k]})
    show(pd.DataFrame(rows), a, "Starters' passing yards")
    t = x.clock.to_numpy()
    q = np.minimum(4, 1 + (3600 - t) // 900).astype(int)
    left = t - (4 - q) * 900
    out = pd.DataFrame({"time": [f"Q{a_} {int(b) // 60}:{int(b) % 60:02d}" for a_, b in zip(q, left)],
                        "margin_home": x.margin.astype(int),
                        "ball": np.where(x.kick, "kickoff→" + np.where(x.pos == 0, h, w), np.where(x.pos == 0, h, w)),
                        f"{h}_win": x.home_win, f"{w}_yds": x.yds1, f"{w}_median": x.median1,
                        f"{h}_yds": x.yds0, f"{h}_median": x.median0})
    for k, team in ((1, w), (0, h)):
        for L in (200, 250, 300):
            out[f"p_{team}_{L}"] = x[f"p{k}_{L}"].to_numpy()
    show(out, a, "In game (simulated from each moment; margin = home − away)")
    print(f"\nFinal: {w} {pre['final_yds'][1]:.0f} yds, {h} {pre['final_yds'][0]:.0f} yds; margin {h} {pre['final_margin']:+.0f}")


def cmd_live(ds, a):
    from .live.app import serve

    serve(a.port, ds)


def cmd_worker(ds, a):
    from .hosted.publish import LocalDir, S3Compatible
    from .hosted.worker import Worker
    from .live import espn, feed

    until = None
    if a.replay:
        rp = feed.Replay(a.replay, speed=a.speed)
        espn.use(rp)
        until = lambda: rp.finished  # noqa: E731
        print(f"replaying {a.replay} at {a.speed:g}x")
    elif a.record:
        espn.use(feed.Recorder(a.record))
    if a.publish == "r2":
        pub = S3Compatible.from_env()
        print(f"publishing to bucket {pub.bucket}")
    else:
        pub = LocalDir(a.out)
        print(f"publishing to {a.out}/ (serve it: python -m http.server -d {a.out} 8000)")
    import threading

    from . import kalshi_ws

    if a.serve is not None:
        from .hosted.push import Hub, Tee

        hub = Hub(a.serve, root=a.out if a.publish == "local" else None, host=a.host,
                  allow_origin=a.events_origin).start()
        pub = Tee(pub, hub)
        print(f"pushing updates on http://{a.host}:{hub.port}/events" +
              (f" (pages: http://{a.host}:{hub.port}/)" if a.publish == "local" else ""))
    wake = threading.Event()  # a jump in a game's Kalshi market: read the feed now
    prices = None
    if not a.replay:
        prices, _ = kalshi_ws.price_source(log=ds.data_dir / "live" / "kalshi_ws", on_move=lambda ticker: wake.set())
    procs = a.procs if a.procs is not None else min(8, os.cpu_count() or 1)
    print(f"simulating in {procs} process{'es' if procs > 1 else ''}")
    w = Worker(ds, pub, every=a.every, only=set(a.game) if a.game else None, prices=prices, procs=procs, wake=wake,
               history=None if a.replay else ds.data_dir / "live" / "history",
               refresh=None if a.replay else lambda: _refresh_data(ds), preds_every=None if a.replay else 3 * 3600.0)
    if a.once:
        w.start()
        w.tick()
    else:
        w.run(until)


def _refresh_data(ds):
    """The worker's daily refresh: this season's stats, schedules and play-by-play."""
    from . import nflverse
    from .sim import data as sim_data

    season = range(ds.current_season, ds.current_season + 1)
    nflverse.fetch(ds, season)
    sim_data.fetch(ds, season)


def cmd_feed(ds, a):
    if a.action == "race":
        from . import race

        print(f"recording every fast source to {a.path} (Ctrl-C to stop)")
        race.record(a.path, hours=a.hours)
        return
    from .live import feed

    feed.record(a.path, every=a.every, hours=a.hours, once=a.once)


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="nflsim", description=__doc__)
    p.add_argument("--data", help="data directory (default: ./data or $NFLSIM_DATA)")
    p.add_argument("--csv", action="store_true", help="print tables as CSV")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("fetch", help="refresh nflverse stats, schedules and simulator play-by-play")
    s.add_argument("--since", type=int, help="only refetch seasons from this year on")
    s.set_defaults(fn=cmd_fetch)

    s = sub.add_parser("sim", help="play-by-play game simulator: fetch pbp, replay a game")
    s.add_argument("action", choices=["fetch", "game"])
    s.add_argument("game_id", nargs="?", help="for `game`: nflverse game id, e.g. 2026_05_TB_DAL")
    s.add_argument("--since", type=int, help="for `fetch`: first season (default: current)")
    s.add_argument("--n", type=int, default=5000, help="simulations per moment")
    s.set_defaults(fn=cmd_sim)

    s = sub.add_parser("live", help="live game companion: current state and the next likely states (web page)")
    s.add_argument("action", choices=["serve"])
    s.add_argument("--port", type=int, default=8765)
    s.set_defaults(fn=cmd_live)

    s = sub.add_parser("worker", help="hosted worker: poll ESPN, simulate changed games, publish static JSON + page")
    s.add_argument("--publish", choices=["local", "r2"], default="local",
                   help="local: a directory (--out); r2: the R2 bucket named by $R2_BUCKET (docs/deploy.md)")
    s.add_argument("--out", default="site", help="with --publish local: directory to publish into (default: site/)")
    s.add_argument("--every", type=float, default=5.0, help="seconds between scoreboard polls")
    s.add_argument("--replay", help="read the feed from a recording instead of ESPN")
    s.add_argument("--speed", type=float, default=10.0, help="with --replay: multiple of real time")
    s.add_argument("--record", help="also save the live feed to this recording")
    s.add_argument("--game", action="append", help="only this ESPN event id (repeatable)")
    s.add_argument("--once", action="store_true", help="one tick, then stop")
    s.add_argument("--procs", type=int, help="simulation processes (default: one per CPU, up to 8)")
    s.add_argument("--serve", type=int, metavar="PORT",
                   help="push every update to open pages on this port (/events); with --publish local, also serve "
                        "the pages there")
    s.add_argument("--host", default="127.0.0.1", help="with --serve: address to listen on")
    s.add_argument("--events-origin", help="with --serve: the pages' origin, when /events is on another host")
    s.set_defaults(fn=cmd_worker)

    s = sub.add_parser("feed", help="record the ESPN feed (scoreboard + changed games' summaries) for replay; "
                                    "race: every fast source side by side, to time them (needs the kalshi extra)")
    s.add_argument("action", choices=["record", "race"])
    s.add_argument("path", help="recording file, e.g. recordings/2026-10-12.jsonl.gz (appended to)")
    s.add_argument("--every", type=float, default=5.0, help="seconds between scoreboard polls")
    s.add_argument("--hours", type=float, help="stop after this many hours (default: until Ctrl-C)")
    s.add_argument("--once", action="store_true", help="one poll, then stop")
    s.set_defaults(fn=cmd_feed)

    a = p.parse_args(argv)
    a.fn(load(a.data), a)


if __name__ == "__main__":
    main()
