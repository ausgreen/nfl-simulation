"""Kalshi's WebSocket feed: the signed handshake, the price book and its log, and the race recorder's parts."""

import base64
import gzip
import json

import pytest

pytest.importorskip("cryptography")

from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import padding, rsa  # noqa: E402

from nflsim import kalshi, kalshi_ws, race  # noqa: E402


def test_handshake_signature_verifies_with_the_public_key():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    h = kalshi_ws.auth_headers("key-1", key, now_ms=1792545234000)
    assert h["KALSHI-ACCESS-KEY"] == "key-1" and h["KALSHI-ACCESS-TIMESTAMP"] == "1792545234000"
    key.public_key().verify(base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]), b"1792545234000GET/trade-api/ws/v2",
                            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                            hashes.SHA256())


def test_missing_credentials_say_which():
    with pytest.raises(SystemExit, match="KALSHI_PRIVATE_KEY_PATH"):
        kalshi_ws.credentials({"KALSHI_API_KEY_ID": "x"})


def _market(ticker, ask, bid, strike=None):
    m = {"ticker": ticker, "event_ticker": ticker.rsplit("-", 1)[0], "yes_ask_dollars": f"{ask:.4f}",
         "yes_bid_dollars": f"{bid:.4f}", "last_price_dollars": f"{bid:.4f}"}
    if strike is not None:
        m.update(strike_type="greater", floor_strike=strike)
    return m


def test_book_applies_ticker_messages_and_prices_like_rest(tmp_path, monkeypatch):
    monkeypatch.setattr(kalshi, "_slug", lambda series: "slug")
    book = kalshi_ws.Book(log=tmp_path)
    book.markets = {m["ticker"]: m for m in (
        _market("KXNFLSPREAD-26OCT22NECHI-CHI3", 0.50, 0.48, 2.5),
        {**_market("KXNFLPASSYDS-26OCT22NECHI-CHICWILLIAMS18-250", 0.40, 0.38, 249.5),
         "event_ticker": "KXNFLPASSYDS-26OCT22NECHI", "title": "Caleb Williams: 250+ passing yards"})}
    games = [{"event": "1", "away": "NE", "home": "CHI"}]
    before = book.live_prices(games)
    book.apply({"type": "ticker", "recv": 100.0, "msg": {"market_ticker": "KXNFLSPREAD-26OCT22NECHI-CHI3",
                                                          "yes_bid_dollars": "0.6000", "yes_ask_dollars": "0.6200",
                                                          "price_dollars": "0.6100"}})
    book.apply({"type": "ticker", "recv": 101.0, "msg": {"market_ticker": "UNKNOWN", "yes_bid_dollars": "0.1"}})
    after = book.live_prices(games)
    assert before["1"]["scores"][0]["ask"] == 0.50
    assert after["1"]["scores"][0] == {**before["1"]["scores"][0], "ask": 0.62, "bid": 0.60, "last": 0.61}
    assert after["1"]["CHI"] == before["1"]["CHI"]
    assert after == kalshi.live_prices(games, book.series(kalshi.SERIES), book.series("KXNFLSPREAD"))
    assert book.received == 101.0


def test_book_log_is_written_in_readable_batches(tmp_path):
    book = kalshi_ws.Book(log=tmp_path)
    t0 = 1792545234.0
    for i in range(4):
        book._write({"type": "ticker", "recv": t0 + 2 * i, "msg": {"i": i}}, every=5.0)
    book._write({"type": "subscribed", "recv": t0 + 9}, every=5.0)  # not logged, but flushes
    (day,) = tmp_path.iterdir()
    lines = [json.loads(x) for x in gzip.open(day, "rt")]
    assert [x["msg"]["i"] for x in lines] == [0, 1, 2, 3] and book._buf == []


def test_race_finds_plays_anywhere_and_writes_records(tmp_path):
    stats = {"pbp": {"periods": [{"events": [{"plays": [{"type": "football_play", "play_id": "a"},
                                                        {"type": "timeout"}]},
                                             {"type": "football_play", "play_id": "b"}]}]}}
    assert [p["play_id"] for p in race._plays(stats)] == ["a", "b"]
    sink = race.Sink(tmp_path / "r.jsonl.gz")
    sink.add("espn", recv=1.0, event="1")
    sink.flush()
    assert [json.loads(x) for x in gzip.open(tmp_path / "r.jsonl.gz", "rt")] == [{"src": "espn", "recv": 1.0, "event": "1"}]


def test_a_jump_in_a_game_market_wakes_the_worker():
    moved = []
    book = kalshi_ws.Book(on_move=moved.append)
    book.markets = {t: _market(t, 0.50, 0.48) for t in ("KXNFLGAME-26OCT22NECHI-CHI", "KXNFLTOTAL-26OCT22NECHI-45")}

    def tick(t, bid, ask):
        book.apply({"type": "ticker", "recv": 1.0, "msg": {"market_ticker": t, "yes_bid_dollars": f"{bid:.4f}",
                                                           "yes_ask_dollars": f"{ask:.4f}"}})
    tick("KXNFLGAME-26OCT22NECHI-CHI", 0.49, 0.51)  # 1¢: noise
    tick("KXNFLTOTAL-26OCT22NECHI-45", 0.70, 0.72)  # not a game-winner market
    tick("KXNFLGAME-26OCT22NECHI-CHI", 0.60, 0.62)  # 11¢: something happened
    assert moved == ["KXNFLGAME-26OCT22NECHI-CHI"]
