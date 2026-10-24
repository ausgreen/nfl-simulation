"""Matching Kalshi's passing-yard and score markets to the scoreboard's games and QBs."""

from nflsim import kalshi


def _market(event, player, qb, line, ask):
    return {"event_ticker": event, "ticker": f"{event}-{player}-{line}", "title": f"{qb}: {line}+ passing yards",
            "strike_type": "greater", "floor_strike": line - 0.5, "yes_ask_dollars": f"{ask:.4f}",
            "yes_bid_dollars": f"{ask - 0.02:.4f}", "last_price_dollars": f"{ask - 0.01:.4f}"}


def test_markets_are_matched_to_games_and_teams_under_kalshi_codes():
    games = [{"event": "1", "away": "ARI", "home": "LA"}, {"event": "2", "away": "LAC", "home": "KC"}]
    markets = [_market("KXNFLPASSYDS-26OCT18ARILAR", "LARMSTAFFORD9", "Matthew Stafford", 250, 0.55),
               _market("KXNFLPASSYDS-26OCT18ARILAR", "LARMSTAFFORD9", "Matthew Stafford", 275, 0.40),
               _market("KXNFLPASSYDS-26OCT18ARILAR", "ARIJBRISSETT7", "Jacoby Brissett", 200, 0.62),
               _market("KXNFLPASSYDS-26OCT18LACKC", "LACJHERBERT10", "Justin Herbert", 250, 0.48),
               _market("KXNFLPASSYDS-26OCT18BUFLV", "BUFJALLEN17", "Josh Allen", 250, 0.6)]
    p = kalshi.live_prices(games, markets, scores=[])
    assert set(p) == {"1", "2"} and set(p["1"]) == {"LA", "ARI"} and set(p["2"]) == {"LAC"}
    (stafford,) = p["1"]["LA"]
    assert stafford["qb"] == "Matthew Stafford" and stafford["url"].endswith("/kxnflpassyds-26oct18arilar")
    assert stafford["lines"] == {"250": {"ask": 0.55, "bid": 0.53, "last": 0.54},
                                 "275": {"ask": 0.40, "bid": 0.38, "last": 0.39}}


def _score(series, game, code, strike, ask):
    m = {"event_ticker": f"{series}-{game}", "ticker": f"{series}-{game}-{code}", "yes_ask_dollars": f"{ask:.4f}",
         "yes_bid_dollars": f"{ask - 0.02:.4f}", "last_price_dollars": f"{ask - 0.01:.4f}"}
    if strike is not None:
        m.update(strike_type="greater", floor_strike=strike)
    return m


def test_score_markets_are_matched_to_games_parts_and_teams(monkeypatch):
    monkeypatch.setattr(kalshi, "_slug", lambda series: "slug")
    games = [{"event": "1", "away": "ARI", "home": "LA"}]
    scores = [_score("KXNFLGAME", "26OCT18ARILAR", "LAR", None, 0.7),
              _score("KXNFLSPREAD", "26OCT18ARILAR", "LAR7", 6.5, 0.45),
              _score("KXNFLTOTAL", "26OCT18ARILAR", "45", 44.5, 0.5),
              _score("KXNFLTEAMTOTAL", "26OCT18ARILAR", "ARI21", 20.5, 0.4),
              _score("KXNFL3Q", "26OCT18ARILAR", "TIE", None, 0.1),
              _score("KXNFL2HTOTAL", "26OCT18ARILAR", "21", 20.5, 0.55),
              _score("KXNFLSPREAD", "26OCT18BUFLV", "BUF3", 2.5, 0.6)]  # another game
    rows = kalshi.live_prices(games, [], scores)["1"]["scores"]
    assert [(r["part"], r["kind"], r["team"], r["x"]) for r in rows] == [
        ("game", "win", "LA", None), ("game", "spread", "LA", 6.5), ("game", "total", None, 44.5),
        ("game", "team", "ARI", 20.5), ("3Q", "win", "TIE", None), ("2H", "total", None, 20.5)]
    assert rows[1]["ask"] == 0.45 and rows[1]["url"] == "https://kalshi.com/markets/kxnflspread/slug/kxnflspread-26oct18arilar"
