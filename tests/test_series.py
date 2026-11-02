"""Chances over time for chart.html (hosted/series.py)."""

from nflsim.hosted.series import Series


def _card(clock, home_win, p250, score=(0, 0), status="in"):
    return {"event": "E", "home": "KC", "away": "BUF", "status": status, "clock": clock, "homeWin": home_win,
            "score": {"away": score[0], "home": score[1]},
            "qbs": [{"team": "KC", "qb": "Patrick Mahomes", "lines": [{"line": 250, "p": p250}]}]}


def _quotes(win_bid, win_ask, line_bid, line_ask):
    return {"E": {"KC": [{"qb": "Patrick Mahomes", "lines": {"250": {"bid": line_bid, "ask": line_ask, "last": None}}}],
                  "scores": [{"part": "game", "kind": "win", "team": "KC", "bid": win_bid, "ask": win_ask},
                             {"part": "1H", "kind": "win", "team": "KC", "bid": 0.1, "ask": 0.2}]}}


def test_a_point_is_added_only_when_the_value_moves_a_point():
    s = Series()
    s.add_state(100, _card("Q1 15:00", 0.600, 0.40))
    s.add_state(110, _card("Q1 14:20", 0.605, 0.40))   # moved less than a point: no new point
    s.add_state(120, _card("Q1 13:40", 0.62, 0.45))
    g = s.json("E")
    assert g["model"]["win"] == [[100, 0.6], [120, 0.62]]
    assert g["model"]["qb:KC:250"] == [[100, 0.4], [120, 0.45]]
    assert g["names"] == {"qb:KC": "Patrick Mahomes"}


def test_kalshi_points_are_the_mid_of_the_game_market_and_each_line():
    s = Series()
    s.add_state(100, _card("Q1 15:00", 0.6, 0.4))
    s.add_prices(101, _quotes(0.58, 0.62, 0.30, 0.34))
    s.add_prices(102, _quotes(0.585, 0.62, 0.30, 0.34))  # mid moved under a cent
    s.add_prices(103, {"OTHER": {}})                      # a game with no state yet is skipped
    g = s.json("E")
    assert g["kalshi"] == {"win": [[101, 0.6]], "qb:KC:250": [[101, 0.32]]}
    assert "OTHER" not in s.games


def test_a_closed_market_keeps_its_last_price():
    s = Series()
    s.add_state(100, _card("Q4 0:10", 0.99, 0.4))
    s.add_prices(101, _quotes(0.98, 0.99, 0.30, 0.34))
    closed = _quotes(0.0, 1.0, 0.0, 1.0)  # no bids or offers left, nothing traded here: not a 50% chance
    s.add_prices(102, closed)
    closed["E"]["scores"][0]["last"] = 0.99
    s.add_prices(103, closed)
    g = s.json("E")
    assert g["kalshi"]["win"] == [[101, 0.985]]
    assert g["kalshi"]["qb:KC:250"] == [[101, 0.32]]


def test_quarters_and_scores_are_marked():
    s = Series()
    s.add_state(100, _card("Q1 15:00", 0.6, 0.4))
    s.add_state(200, _card("Q1 9:00", 0.5, 0.4, score=(7, 0)))
    s.add_state(300, _card("Q2 15:00", 0.5, 0.4, score=(7, 0)))
    assert s.json("E")["marks"] == [[100, "Q1"], [200, "BUF 7 – KC 0"], [300, "Q2"]]


def test_the_history_file_rebuilds_the_same_series():
    live, rebuilt = Series(), Series()
    records = [{"t": "2026-10-29T00:20:00+00:00", "kind": "state", "card": _card("Q1 15:00", 0.6, 0.4)},
               {"t": "2026-10-29T00:20:05+00:00", "kind": "kalshi", "prices": _quotes(0.58, 0.62, 0.3, 0.34)},
               {"t": "2026-10-29T00:21:00+00:00", "kind": "state", "card": _card("Q1 14:00", 0.7, 0.5)},
               {"t": "2026-10-29T00:21:05+00:00", "kind": "kalshi", "partial": True, "prices": _quotes(0.68, 0.72, 0.3, 0.34)}]
    for r in records:
        (live.add_state(r["t"], r["card"]) if r["kind"] == "state" else live.add_prices(r["t"], r["prices"]))
    rebuilt.from_history(records)
    assert rebuilt.json("E") == live.json("E") and rebuilt.dirty == {"E"}
    assert rebuilt.json("E")["kalshi"]["win"][-1][1] == 0.7
