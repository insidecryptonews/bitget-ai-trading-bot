"""V10.43C continuity: REST vs WS vs persistent WS, fail-closed blockers."""

from __future__ import annotations

import csv

from app.labs import ws_continuity_v10_43c as PWS

T0 = 1_700_000_000_000
COLS = ["timestamp", "symbol", "price", "size", "aggressor_side", "trade_id",
        "source_exchange"]


def _trade(tid, ts, sym="BTCUSDT"):
    return {"timestamp": ts, "symbol": sym, "price": "60000", "size": "0.01",
            "aggressor_side": "buy", "trade_id": tid, "source_exchange": "bybit_linear"}


def _write(tmp_path, rows):
    with open(tmp_path / "trades.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _bars(n, start=T0, gap_every=None):
    out = []
    ts = start
    for i in range(n):
        if gap_every and i and i % gap_every == 0:
            ts += 5 * 60_000
        out.append({"ts": ts, "bar_close_ts": ts, "available_at": ts,
                    "open": 100, "high": 101, "low": 99, "close": 100,
                    "volume": 1, "buy_volume": 1, "sell_volume": 0,
                    "n_trades": 1, "max_trade": 1, "symbol": "BTCUSDT"})
        ts += 60_000
    return out


def test_missing_persistent_dataset_fails_closed(tmp_path):
    r = PWS.load_persistent_bars("BTCUSDT", base=tmp_path)
    assert r["bars"] == []
    assert r["meta"]["exists"] is False
    h = PWS.ws_persistent_health("BTCUSDT", base=tmp_path)
    assert h["status"] == "NO_DATA"
    assert h["can_send_real_orders"] is False


def test_load_persistent_bars_has_source_tags_and_drops_corrupt(tmp_path):
    _write(tmp_path, [
        _trade("a", T0 + 1000),
        {"timestamp": "bad", "symbol": "BTCUSDT", "price": "x", "size": "y",
         "aggressor_side": "buy", "trade_id": "bad", "source_exchange": "bybit_linear"},
        _trade("b", T0 + 20_000),
    ])
    r = PWS.load_persistent_bars("BTCUSDT", base=tmp_path)
    assert r["meta"]["dropped_rows"] == 1
    assert r["bars"][0]["source_exchange"] == "bybit_linear_ws_persistent"
    assert r["bars"][0]["source_dataset"] == "ws_persistent_v10_43c"
    assert r["bars"][0]["available_at"] >= r["bars"][0]["bar_close_ts"]


def test_3way_compare_recommends_persistent_but_keeps_gappy_blocker(monkeypatch):
    monkeypatch.setattr(PWS.CE, "load_dataset", lambda _s: {"bars": _bars(20, gap_every=5)})
    monkeypatch.setattr(PWS.WS, "load_ws_bars", lambda _s: {"bars": _bars(40, gap_every=12)})
    monkeypatch.setattr(PWS, "load_persistent_bars",
                        lambda _s, base=None: {"bars": _bars(120, gap_every=20),
                                               "meta": {"n_trades_used": 120}})
    c = PWS.dataset_source_compare_3way("BTCUSDT")
    assert c["recommended_source"] == "ws_persistent"
    assert c["ready_for_shadow_forward"] is False
    assert "WS_TOO_GAPPY_FOR_SHADOW_FORWARD" in c["blockers"]


def test_continuity_audit_marks_improving_not_ready(monkeypatch, tmp_path):
    rows = []
    ts = T0
    for i in range(120):
        if i and i % 20 == 0:
            ts += 5 * 60_000
        rows.append(_trade(str(i), ts))
        ts += 60_000
    _write(tmp_path, rows)
    monkeypatch.setattr(PWS.WS, "load_ws_bars", lambda _s: {"bars": _bars(30, gap_every=10)})
    r = PWS.ws_continuity_audit("BTCUSDT", base=tmp_path)
    assert r["improving_vs_v1042"] is True
    assert r["fit_for_shadow_forward"] is False
    assert r["verdict"] in PWS.CONTINUITY_VERDICTS
    assert r["final_recommendation"] == "NO LIVE"
