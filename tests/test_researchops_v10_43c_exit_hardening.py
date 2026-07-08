"""V10.43C exit hardening: conservative partial TP and capture semantics."""

from __future__ import annotations

from app.labs import exit_optimization_v10_43b as EX

T0 = 1_700_000_000_000
BAR = EX.BAR_MS


def bar(i, o, h, l, c):
    ts = T0 + i * BAR
    return {"ts": ts, "open": o, "high": h, "low": l, "close": c}


def test_partial_tp1_then_tp2_sequence():
    bars = [
        bar(0, 100, 100, 100, 100),
        bar(1, 100, 100.7, 99.9, 100.6),   # TP1 reached, no stop
        bar(2, 100.6, 101.5, 100.4, 101.4), # TP2 reached
    ]
    o = EX._partial_tp(bars, 0, "long", 0.006, 0.014, 0.006, 5, costs={"fee_bps": 0, "slippage_bps": 0, "spread_bps": 0})
    assert o["valid"] is True
    assert o["exit_reason"] == "PARTIAL_TP2"
    assert o["tp1_reached"] is True
    assert round(o["gross"], 4) == 0.0100
    assert o["partial_tp_model"] == "approx_conservative"


def test_partial_tp1_then_sl_on_second_half():
    bars = [
        bar(0, 100, 100, 100, 100),
        bar(1, 100, 100.7, 99.9, 100.6),   # TP1 reached
        bar(2, 100.6, 100.7, 99.3, 99.4),  # second half hits SL
    ]
    o = EX._partial_tp(bars, 0, "long", 0.006, 0.014, 0.006, 5, costs={"fee_bps": 0, "slippage_bps": 0, "spread_bps": 0})
    assert o["exit_reason"] == "PARTIAL_TP1_THEN_SL"
    assert o["tp1_reached"] is True
    assert abs(o["gross"]) < 1e-12          # +0.6% half, -0.6% half


def test_no_tp1_then_sl_does_not_mark_partial():
    bars = [
        bar(0, 100, 100, 100, 100),
        bar(1, 100, 100.2, 99.3, 99.4),     # stop before TP1
        bar(2, 99.4, 99.6, 99.0, 99.1),
    ]
    o = EX._partial_tp(bars, 0, "long", 0.006, 0.014, 0.006, 5, costs={"fee_bps": 0, "slippage_bps": 0, "spread_bps": 0})
    assert o["exit_reason"] == "SL"
    assert o["tp1_reached"] is False
    assert round(o["gross"], 4) == -0.0060


def test_same_bar_stop_and_tp1_uses_stop_before_tp():
    bars = [
        bar(0, 100, 100, 100, 100),
        bar(1, 100, 100.8, 99.3, 100.3),    # same bar stop + TP1 -> stop wins
    ]
    o = EX._partial_tp(bars, 0, "long", 0.006, 0.014, 0.006, 5, costs={"fee_bps": 0, "slippage_bps": 0, "spread_bps": 0})
    assert o["exit_reason"] == "SL"
    assert o["tp1_reached"] is False


def test_stale_after_tp1_exits_second_half_at_previous_close():
    bars = [
        bar(0, 100, 100, 100, 100),
        bar(1, 100, 100.7, 99.9, 100.6),    # TP1 reached
        bar(5, 100.6, 101.0, 100.0, 100.8), # stale gap before this bar
    ]
    o = EX._partial_tp(bars, 0, "long", 0.006, 0.014, 0.006, 5, costs={"fee_bps": 0, "slippage_bps": 0, "spread_bps": 0})
    assert o["exit_reason"] == "PARTIAL_TP1_THEN_STALE"
    assert round(o["gross"], 4) == 0.0060


def test_capture_ratio_none_when_mfe_floor_not_met(monkeypatch):
    entries = {0: "long"}
    bars = [bar(0, 100, 100, 100, 100), bar(1, 100, 100, 99.4, 99.5)]
    feats = [{"realized_volatility": 0.0}, {"realized_volatility": 0.0}]
    r = EX._eval_variant(bars, feats, entries, {"name": "fixed", "tp": 0.006,
                                                "sl": 0.006, "trail": None,
                                                "be": None, "partial": None,
                                                "atr": False}, 5)
    assert r["capture_ratio"] is None


def test_cli_registered():
    import app.research_lab as RL
    assert "exit-optimization-v1043c" in RL.PUBLIC_RESEARCH_ONLY_COMMANDS
