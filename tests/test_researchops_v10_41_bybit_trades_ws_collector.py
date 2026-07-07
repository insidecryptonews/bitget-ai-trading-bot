"""V10.41 Bybit public-trade WS collector CORE: parse/dedup/gap/merge, no live
loop, no keys, no orders."""

from __future__ import annotations

from pathlib import Path

from app.labs import bybit_trades_ws_collector_v10_41 as WS


def _frame(trades):
    return {"topic": "publicTrade.BTCUSDT", "type": "snapshot", "ts": 1,
            "data": trades}


def test_parse_public_trade_message_canonical_rows():
    msg = _frame([
        {"T": 1700000000000, "s": "BTCUSDT", "S": "Buy", "v": "0.01", "p": "50000.5", "i": "t1"},
        {"T": 1700000000500, "s": "BTCUSDT", "S": "Sell", "v": "0.02", "p": "50001", "i": "t2"}])
    rows = WS.parse_public_trade_message(msg)
    assert len(rows) == 2
    assert rows[0] == {"timestamp": 1700000000000, "symbol": "BTCUSDT",
                       "price": 50000.5, "size": 0.01, "aggressor_side": "buy",
                       "trade_id": "t1", "source_exchange": "bybit_linear"}
    assert rows[1]["aggressor_side"] == "sell"


def test_parse_rejects_non_public_trade_and_malformed():
    assert WS.parse_public_trade_message({"topic": "orderbook.1.BTCUSDT", "data": []}) == []
    assert WS.parse_public_trade_message({"topic": "publicTrade.BTCUSDT",
                                          "data": [{"T": 0, "p": "1", "v": "1", "i": "x"}]}) == []
    assert WS.parse_public_trade_message("not a dict") == []


def test_dedup_by_trade_id_cross_frame():
    rows = WS.parse_public_trade_message(_frame([
        {"T": 1, "s": "BTCUSDT", "S": "Buy", "v": "1", "p": "1", "i": "a"},
        {"T": 2, "s": "BTCUSDT", "S": "Buy", "v": "1", "p": "1", "i": "b"}]))
    uniq, seen = WS.dedup_by_trade_id(rows)
    assert len(uniq) == 2 and seen == {"a", "b"}
    # a second frame repeating 'b' + new 'c' -> only 'c' survives
    more = WS.parse_public_trade_message(_frame([
        {"T": 3, "s": "BTCUSDT", "S": "Buy", "v": "1", "p": "1", "i": "b"},
        {"T": 4, "s": "BTCUSDT", "S": "Buy", "v": "1", "p": "1", "i": "c"}]))
    uniq2, seen2 = WS.dedup_by_trade_id(more, seen)
    assert [r["trade_id"] for r in uniq2] == ["c"] and seen2 == {"a", "b", "c"}


def test_detect_gaps_reports_large_intertrade_gaps():
    rows = [{"timestamp": 0}, {"timestamp": 1000}, {"timestamp": 20000}]
    gaps = WS.detect_gaps(rows, gap_ms=5000)
    assert len(gaps) == 1 and gaps[0]["gap_ms"] == 19000


def test_merge_append_is_idempotent_and_sorted():
    existing = [{"timestamp": 2, "trade_id": "b"}, {"timestamp": 1, "trade_id": "a"}]
    new = [{"timestamp": 3, "trade_id": "b"}, {"timestamp": 4, "trade_id": "c"}]
    merged, added = WS.merge_append(existing, new)
    assert added == 1                                       # only 'c' is new
    assert [r["trade_id"] for r in merged] == ["a", "b", "c"]
    # running the same merge again adds nothing (idempotent)
    merged2, added2 = WS.merge_append(merged, new)
    assert added2 == 0 and len(merged2) == 3


def test_heartbeat_detects_stale_stream():
    assert WS.heartbeat(0, 10_000)["status"] == "ALIVE"
    stale = WS.heartbeat(0, 30_000)
    assert stale["status"] == "STALE_NO_FRAMES" and stale["stale"] is True


def test_plan_is_design_only_no_live_loop():
    p = WS.plan()
    assert p["core_implemented"] is True
    assert p["live_ws_loop_implemented"] is False
    assert p["uses_api_keys"] is False and p["sends_orders"] is False
    assert p["final_recommendation"] == "NO LIVE"


def test_module_has_no_network_import_or_order_primitives():
    src = Path(WS.__file__).read_text(encoding="utf-8")
    for imp in ["import websocket", "import requests", "import urllib",
                "from websocket", "websocket.create_connection", "socket.socket"]:
        assert imp not in src, imp
    # order/key USAGE primitives absent ("uses_api_keys: False" flag is allowed)
    for tok in ["place_order", "create_order", "private_get", "private_post",
                "set_leverage", "set_margin_mode", "load_dotenv", "os.environ"]:
        assert tok not in src, tok
