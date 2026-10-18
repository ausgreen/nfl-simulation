"""Matching Kalshi's passing-yard markets to the scoreboard's games and QBs."""

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
    p = kalshi.live_prices(games, markets)
    assert set(p) == {"1", "2"} and set(p["1"]) == {"LA", "ARI"} and set(p["2"]) == {"LAC"}
    (stafford,) = p["1"]["LA"]
    assert stafford["qb"] == "Matthew Stafford" and stafford["url"].endswith("/kxnflpassyds-26oct18arilar")
    assert stafford["lines"] == {"250": {"ask": 0.55, "bid": 0.53, "last": 0.54},
                                 "275": {"ask": 0.40, "bid": 0.38, "last": 0.39}}
