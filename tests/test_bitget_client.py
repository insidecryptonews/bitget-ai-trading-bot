from app.bitget_client import BitgetClient
from app.config import BotConfig


class DummyLogger:
    def __init__(self):
        self.lines = []

    def info(self, *args):
        self.lines.append(args[0] % args[1:] if len(args) > 1 else args[0])


def make_client():
    client = object.__new__(BitgetClient)
    client.config = BotConfig()
    client.logger = DummyLogger()
    calls = []

    def fake_place_order(**kwargs):
        calls.append(kwargs)
        return kwargs

    client.place_order = fake_place_order
    return client, calls


def test_close_position_market_long_uses_bitget_mix_hedge_close_semantics():
    client, calls = make_client()
    client.close_position_market("BTCUSDT", "LONG", "0.1", "oid")
    assert calls[0]["side"] == "buy"
    assert calls[0]["trade_side"] == "close"
    assert calls[0]["reduce_only"] is True


def test_close_position_market_short_uses_bitget_mix_hedge_close_semantics():
    client, calls = make_client()
    client.close_position_market("BTCUSDT", "SHORT", "0.1", "oid")
    assert calls[0]["side"] == "sell"
    assert calls[0]["trade_side"] == "close"
    assert calls[0]["reduce_only"] is True


def test_close_position_market_rejects_invalid_side():
    client, _ = make_client()
    try:
        client.close_position_market("BTCUSDT", "BUY", "0.1", "oid")
    except ValueError:
        return
    raise AssertionError("Expected ValueError for invalid close side")

