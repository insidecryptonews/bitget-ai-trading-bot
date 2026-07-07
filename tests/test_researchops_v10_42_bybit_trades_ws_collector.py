"""V10.42 Bybit trades WS collector RUNNER: deterministic loop over injected
frames (no network), idempotent dataset append. No keys, no orders."""

from __future__ import annotations

from pathlib import Path

from app.labs import bybit_trades_ws_collector_v10_42 as WS


def _frame(trades, sym="BTCUSDT"):
    return {"topic": f"publicTrade.{sym}", "type": "snapshot", "ts": 1, "data": trades}


def _t(tid, ms, side="Buy", p="50000", v="0.01"):
    return {"T": ms, "s": "BTCUSDT", "S": side, "v": v, "p": p, "i": tid}


def test_subscribe_message_is_public_no_auth():
    m = WS.subscribe_message(["BTCUSDT", "ETHUSDT"])
    assert m == {"op": "subscribe", "args": ["publicTrade.BTCUSDT", "publicTrade.ETHUSDT"]}
    assert "api_key" not in str(m) and "sign" not in str(m)


def test_collect_from_frames_parses_dedups_and_flushes():
    clock = [1000]
    frames = [_frame([_t("a", 1), _t("b", 2)]),
              _frame([_t("b", 2), _t("c", 3)]),          # 'b' duplicate
              _frame([_t("d", 4)])]
    flushed = []
    rep = WS.collect_from_frames(frames, flush_every=2,
                                 on_flush=lambda rows: flushed.append(list(rows)),
                                 now_ms_fn=lambda: clock[0])
    assert rep["n_new_trades"] == 4                 # a,b,c,d
    assert rep["n_duplicate_trades"] == 1           # the repeated 'b'
    ids = [r["trade_id"] for batch in flushed for r in batch]
    assert sorted(ids) == ["a", "b", "c", "d"]
    assert rep["can_send_real_orders"] is False


def test_out_of_order_trades_sorted_on_flush():
    frames = [_frame([_t("a", 30), _t("b", 10), _t("c", 20)])]
    flushed = []
    WS.collect_from_frames(frames, flush_every=1,
                           on_flush=lambda rows: flushed.append([r["timestamp"] for r in rows]))
    assert flushed[0] == [10, 20, 30]               # ascending timestamp


def test_malformed_frames_counted_not_crashing():
    frames = [{"topic": "orderbook.1.BTCUSDT", "data": []},
              {"garbage": True}, _frame([_t("a", 1)])]
    rep = WS.collect_from_frames(frames, flush_every=10)
    assert rep["n_new_trades"] == 1
    assert rep["n_malformed_frames"] >= 1


def test_append_rows_is_idempotent(tmp_path):
    rows = [{"timestamp": 2, "symbol": "BTCUSDT", "price": 1.0, "size": 1.0,
             "aggressor_side": "buy", "trade_id": "b", "source_exchange": "bybit_linear"},
            {"timestamp": 1, "symbol": "BTCUSDT", "price": 1.0, "size": 1.0,
             "aggressor_side": "sell", "trade_id": "a", "source_exchange": "bybit_linear"}]
    r1 = WS.append_rows(rows, tmp_path)
    assert r1["rows_added"] == 2 and r1["total_rows"] == 2
    # re-append same rows -> nothing added (idempotent), still sorted
    r2 = WS.append_rows(rows, tmp_path)
    assert r2["rows_added"] == 0 and r2["total_rows"] == 2
    import csv
    got = list(csv.DictReader(open(Path(tmp_path) / "trades.csv", encoding="utf-8")))
    assert [g["trade_id"] for g in got] == ["a", "b"]        # ascending ts


def test_status_is_research_only():
    s = WS.status()
    assert s["loop_implemented"] is True and s["live_connect_guarded"] is True
    assert s["uses_api_keys"] is False and s["sends_orders"] is False
    assert s["final_recommendation"] == "NO LIVE"


def test_module_has_no_order_or_key_primitives():
    src = Path(WS.__file__).read_text(encoding="utf-8")
    for tok in ["place_order", "create_order", "private_get", "private_post",
                "set_leverage", "set_margin_mode", "load_dotenv", "os.environ"]:
        assert tok not in src, tok
    # websocket is imported ONLY inside the guarded live function (lazy import)
    assert "import websocket  # type: ignore" in src
    assert "\nimport websocket" not in src         # not a top-level import
