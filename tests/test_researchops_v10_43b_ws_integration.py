"""V10.43B WS dataset integration: robust, fail-closed, no lookahead."""

from __future__ import annotations

import csv

from app.labs import ws_dataset_integration_v10_43b as WS

BASE_MS = (1_700_000_000_000 // 60_000) * 60_000
COLS = ["timestamp", "symbol", "price", "size", "aggressor_side", "trade_id",
        "source_exchange"]


def _write_ws(tmp, rows):
    d = tmp / "external_data" / "staging" / "bybit_trades_ws_v10_42"
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "trades.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return tmp


def _t(tid, ms, sym="BTCUSDT", price="60000", size="0.01", side="buy"):
    return {"timestamp": ms, "symbol": sym, "price": price, "size": size,
            "aggressor_side": side, "trade_id": tid, "source_exchange": "bybit_linear"}


def test_missing_dataset_does_not_crash(tmp_path):
    r = WS.load_ws_bars("BTCUSDT", base=tmp_path)
    assert r["bars"] == [] and r["meta"]["exists"] is False
    v = WS.ws_forward_dataset_view("BTCUSDT", base=tmp_path)
    assert v["verdict"] == "NO_WS_DATA" and v["can_send_real_orders"] is False


def test_empty_dataset_does_not_crash(tmp_path):
    _write_ws(tmp_path, [])
    r = WS.load_ws_bars("BTCUSDT", base=tmp_path)
    assert r["bars"] == [] and r["meta"]["n_trades_raw"] == 0


def test_corrupt_rows_dropped(tmp_path):
    _write_ws(tmp_path, [
        _t("a", BASE_MS + 1000),
        {"timestamp": "bad", "symbol": "BTCUSDT", "price": "x", "size": "y",
         "aggressor_side": "buy", "trade_id": "b", "source_exchange": "bybit_linear"},
        _t("c", BASE_MS + 2000)])
    r = WS.load_ws_bars("BTCUSDT", base=tmp_path)
    assert r["meta"]["dropped_rows"] == 1
    assert r["meta"]["n_trades_used"] == 2


def test_out_of_order_trades_sorted(tmp_path):
    _write_ws(tmp_path, [_t("a", BASE_MS + 3 * 60_000), _t("b", BASE_MS),
                         _t("c", BASE_MS + 60_000)])
    bars = WS.load_ws_bars("BTCUSDT", base=tmp_path)["bars"]
    assert all(bars[i]["ts"] < bars[i + 1]["ts"] for i in range(len(bars) - 1))


def test_bars_have_available_at_at_close_no_lookahead(tmp_path):
    _write_ws(tmp_path, [_t("a", BASE_MS + 100), _t("b", BASE_MS + 50_000)])
    bars = WS.load_ws_bars("BTCUSDT", base=tmp_path)["bars"]
    for b in bars:
        assert b["available_at"] >= b["bar_close_ts"]
        assert b["ts"] == b["bar_close_ts"]
        assert b["source_exchange"] == "bybit_linear_ws"
        assert b["source_dataset"] == "ws_v10_42"


def test_unknown_symbol_fails_closed(tmp_path):
    _write_ws(tmp_path, [_t("a", BASE_MS)])
    r = WS.load_ws_bars("ETHUSDT", base=tmp_path)
    assert r["bars"] == [] and r["meta"]["n_trades_used"] == 0
    v = WS.ws_forward_dataset_view("ETHUSDT", base=tmp_path)
    assert v["verdict"] == "NO_WS_DATA"


def test_source_compare_no_crash_when_one_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(WS.CE, "load_dataset", lambda *a, **k: {"bars": []})
    _write_ws(tmp_path, [])                        # empty WS
    c = WS.dataset_source_compare("BTCUSDT", base=tmp_path)
    assert "NO_WS_DATA" in c["blockers"]
    assert c["recommended_source"] in ("ws", "rest")
    assert c["can_send_real_orders"] is False


def test_ws_forward_view_reports_real_metrics(tmp_path):
    # a contiguous run of minutes -> a real max_contiguous_run
    rows = []
    for m in range(120):
        for k in range(3):
            rows.append(_t(f"{m}-{k}", BASE_MS + m * 60_000 + k * 15_000,
                           price=str(60000 + m)))
    _write_ws(tmp_path, rows)
    v = WS.ws_forward_dataset_view("BTCUSDT", base=tmp_path)
    assert v["bars_created"] == 120
    assert v["max_contiguous_run"] == 120
    assert v["source"] == "ws_v10_42"
