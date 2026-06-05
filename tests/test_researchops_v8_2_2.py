"""Tests for V8.2.2 — analytical hotfixes before commit.

Two bugs flagged by the Codex audit:

1. ``missed_opportunities`` returned ``status=OK`` even when every candidate's
   ``would_have_worked_estimate`` was ``NEED_DATA``. The fix introduces honest
   accounting: NEED_DATA, PARTIAL, OK depending on how many candidates have
   future returns.

2. ``partial_50_plus_trailing`` under-counted losses when the stop fired
   before TP1 was taken. The fix applies the loss to the full position when
   no partial has been taken and respects STOP_BEFORE_TP same-bar.
"""

from __future__ import annotations

import pytest


def _bar(o, h, l, c):
    return {"open": o, "high": h, "low": l, "close": c}


def _row(*, side="SHORT", proposed="SHORT", regime="RISK_OFF",
         score=65, executed="NO_TRADE", ret_1h_pct=None, ret_4h_pct=None,
         reason="score_too_low"):
    return {
        "symbol": "BTCUSDT",
        "side": executed,
        "proposed_side": proposed,
        "market_regime": regime,
        "confidence_score": score,
        "reason": reason,
        "ret_15m_pct": None,
        "ret_30m_pct": None,
        "ret_1h_pct": ret_1h_pct,
        "ret_4h_pct": ret_4h_pct,
    }


# ---------------------------------------------------------------------------
# missed_opportunities status accounting
# ---------------------------------------------------------------------------

def test_missed_opportunities_all_need_data_returns_need_data_status():
    from app.labs.bidirectional_forensic_lab import missed_opportunities

    rows = [
        _row(ret_1h_pct=None, ret_4h_pct=None),
        _row(ret_1h_pct=None, ret_4h_pct=None),
        _row(ret_1h_pct=None, ret_4h_pct=None),
    ]
    report = missed_opportunities(None, side="SHORT", hours=24, rows=rows)
    assert report.status == "NEED_DATA"
    assert "missing_future_returns" in report.need_data_reasons
    assert report.calculable_count == 0
    assert report.need_data_count == 3
    assert report.need_data_ratio == pytest.approx(1.0)
    # All candidates flagged.
    for cand in report.candidates:
        assert cand["would_have_worked_estimate"] == "NEED_DATA"


def test_missed_opportunities_partial_when_mixed_returns():
    from app.labs.bidirectional_forensic_lab import missed_opportunities

    rows = [
        _row(ret_1h_pct=-1.5),    # calculable True
        _row(ret_1h_pct=2.0),     # calculable False
        _row(ret_1h_pct=None, ret_4h_pct=None),   # NEED_DATA
    ]
    report = missed_opportunities(None, side="SHORT", hours=24, rows=rows)
    assert report.status == "PARTIAL"
    assert report.calculable_count == 2
    assert report.need_data_count == 1
    assert report.need_data_ratio == pytest.approx(1 / 3)


def test_missed_opportunities_ok_when_all_calculable():
    from app.labs.bidirectional_forensic_lab import missed_opportunities

    rows = [
        _row(ret_1h_pct=-1.5),
        _row(ret_1h_pct=-0.8),
        _row(ret_1h_pct=2.0),
    ]
    report = missed_opportunities(None, side="SHORT", hours=24, rows=rows)
    assert report.status == "OK"
    assert report.calculable_count == 3
    assert report.need_data_count == 0
    assert report.need_data_ratio == pytest.approx(0.0)


def test_missed_opportunities_falls_back_to_4h_when_1h_missing():
    """Calculability uses 1h first, then 4h fallback."""
    from app.labs.bidirectional_forensic_lab import missed_opportunities

    rows = [_row(ret_1h_pct=None, ret_4h_pct=-3.0)]
    report = missed_opportunities(None, side="SHORT", hours=24, rows=rows)
    assert report.calculable_count == 1
    assert report.need_data_count == 0
    assert report.status == "OK"


def test_blocked_counterfactual_propagates_need_data_accounting():
    from app.labs.bidirectional_forensic_lab import blocked_that_would_have_worked

    rows = [
        _row(score=70, ret_1h_pct=None, ret_4h_pct=None),
        _row(score=80, ret_1h_pct=None, ret_4h_pct=None),
    ]
    report = blocked_that_would_have_worked(
        None, side="SHORT", hours=24, top_n=10, min_score=60, rows=rows,
    )
    assert report.status == "NEED_DATA"
    assert report.calculable_count == 0
    assert report.need_data_count == 2
    assert "missing_future_returns" in report.need_data_reasons


# ---------------------------------------------------------------------------
# partial_50_plus_trailing — full-position loss when no partial taken
# ---------------------------------------------------------------------------

def test_partial_trailing_stop_before_tp1_long_full_position_loss():
    """LONG: stop fires in bar 1 before TP1 ever reached → full -1.0% loss."""
    from app.labs.profit_lock_simulator import (
        ExitTrade,
        POLICY_PARTIAL_50_TRAILING,
        _simulate_policy,
    )

    # Bar low touches the stop without high reaching TP1.
    bars = [_bar(100, 100.5, 99.0, 99.5)]
    trade = ExitTrade(
        symbol="X", side="LONG", entry=100.0, stop=99.0,
        tp1=101.0, tp2=102.0, bar_path=bars, fees_pct=0.0,
    )
    r = _simulate_policy(POLICY_PARTIAL_50_TRAILING, trade)
    assert r["exit_reason"] == "STOP_LOSS"
    assert r["realized_net_pct"] == pytest.approx(-1.0)


def test_partial_trailing_stop_before_tp1_short_full_position_loss():
    """SHORT: stop fires in bar 1 before TP1 reached → full -1.0% loss."""
    from app.labs.profit_lock_simulator import (
        ExitTrade,
        POLICY_PARTIAL_50_TRAILING,
        _simulate_policy,
    )

    # Bar high touches the stop without low reaching TP1.
    bars = [_bar(100, 101.0, 99.5, 100.5)]
    trade = ExitTrade(
        symbol="X", side="SHORT", entry=100.0, stop=101.0,
        tp1=99.0, tp2=98.0, bar_path=bars, fees_pct=0.0,
    )
    r = _simulate_policy(POLICY_PARTIAL_50_TRAILING, trade)
    assert r["exit_reason"] == "STOP_LOSS"
    assert r["realized_net_pct"] == pytest.approx(-1.0)


def test_partial_trailing_same_bar_long_stop_and_tp1_stop_wins_full_loss():
    """LONG: same-bar touches both stop and TP1. STOP_BEFORE_TP → stop wins;
    partial NOT considered taken; loss applies to full position.
    """
    from app.labs.profit_lock_simulator import (
        ExitTrade,
        POLICY_PARTIAL_50_TRAILING,
        _simulate_policy,
    )

    bars = [_bar(100, 101.5, 99.0, 100.5)]
    trade = ExitTrade(
        symbol="X", side="LONG", entry=100.0, stop=99.0,
        tp1=101.0, tp2=102.0, bar_path=bars, fees_pct=0.0,
    )
    r = _simulate_policy(POLICY_PARTIAL_50_TRAILING, trade)
    assert r["exit_reason"] == "STOP_LOSS"
    # Full-position loss, not half.
    assert r["realized_net_pct"] == pytest.approx(-1.0)


def test_partial_trailing_same_bar_short_stop_and_tp1_stop_wins_full_loss():
    """SHORT mirror of the LONG same-bar case."""
    from app.labs.profit_lock_simulator import (
        ExitTrade,
        POLICY_PARTIAL_50_TRAILING,
        _simulate_policy,
    )

    bars = [_bar(100, 101.0, 98.5, 99.5)]
    trade = ExitTrade(
        symbol="X", side="SHORT", entry=100.0, stop=101.0,
        tp1=99.0, tp2=98.0, bar_path=bars, fees_pct=0.0,
    )
    r = _simulate_policy(POLICY_PARTIAL_50_TRAILING, trade)
    assert r["exit_reason"] == "STOP_LOSS"
    assert r["realized_net_pct"] == pytest.approx(-1.0)


def test_partial_trailing_tp1_first_then_trailing_correct_split():
    """LONG: TP1 fires in bar 1, then stops out via trailing in bar 2.

    Expected: 50% closed at TP1 (=+0.5% on half), 50% closed at trailing stop
    (assume BE after TP1, no advance → 0% on half). Total ≈ +0.5% net.
    """
    from app.labs.profit_lock_simulator import (
        ExitTrade,
        POLICY_PARTIAL_50_TRAILING,
        _simulate_policy,
    )

    # Bar 1: low does NOT touch stop, high reaches TP1.
    # Bar 2: drops back to entry (BE stop after TP1 → exits at BE).
    bars = [
        _bar(100, 101.5, 99.5, 101.2),  # high >= 101 (TP1), low > 99 (no stop)
        _bar(101.2, 101.3, 99.9, 100.0),  # drops back through entry → BE hit
    ]
    trade = ExitTrade(
        symbol="X", side="LONG", entry=100.0, stop=99.0,
        tp1=101.0, tp2=103.0, bar_path=bars, fees_pct=0.0,
    )
    r = _simulate_policy(POLICY_PARTIAL_50_TRAILING, trade)
    # Partial pnl = 0.5 * +1.0% = +0.5
    # Rest pnl at BE (=entry) = 0
    # Sum ≈ +0.5 (allow some slack for trailing maths)
    assert r["realized_net_pct"] == pytest.approx(0.5, abs=0.05)
    assert r["exit_reason"] in {"TRAILING_STOP", "HORIZON_CLOSE"}


def test_partial_trailing_horizon_close_with_partial_taken_keeps_split():
    """LONG: TP1 hits in bar 1, never stops out; horizon close at last bar.

    Expected: partial 50% at +1.0% (= +0.5) plus rest 50% at final close
    return.
    """
    from app.labs.profit_lock_simulator import (
        ExitTrade,
        POLICY_PARTIAL_50_TRAILING,
        _simulate_policy,
    )

    bars = [
        _bar(100, 101.5, 99.5, 101.0),
        _bar(101.0, 102.0, 100.5, 101.5),  # last close 101.5 → rest gives +1.5*0.5
    ]
    trade = ExitTrade(
        symbol="X", side="LONG", entry=100.0, stop=99.0,
        tp1=101.0, tp2=103.0, bar_path=bars, fees_pct=0.0,
    )
    r = _simulate_policy(POLICY_PARTIAL_50_TRAILING, trade)
    # Either trailing exits during bar 2, or horizon close. In either case
    # the partial split must be visible — total must be > pure-TP1 baseline.
    assert r["realized_net_pct"] > 0.5  # at least the partial portion


# ---------------------------------------------------------------------------
# Safety re-check (V8.2.2 must not introduce new dangerous patterns)
# ---------------------------------------------------------------------------

import ast
import importlib
import pathlib


FORBIDDEN_CALL_NAMES = {
    "place_order", "set_leverage", "set_margin_mode",
    "private_get", "private_post",
}

FORBIDDEN_ASSIGN_LITERALS = {
    "LIVE_TRADING", "ENABLE_PAPER_POLICY_FILTER",
    "can_send_real_orders", "allow_real_writes",
}


def test_v82_2_modules_have_no_forbidden_calls():
    mods = [
        "app.labs.bidirectional_forensic_lab",
        "app.labs.profit_lock_simulator",
    ]
    for mod in mods:
        path = pathlib.Path(importlib.import_module(mod).__file__)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = getattr(fn, "attr", None) or getattr(fn, "id", None)
                assert name not in FORBIDDEN_CALL_NAMES, f"{mod} calls {name}"


def test_v82_2_modules_have_no_forbidden_literal_true_assigns():
    mods = [
        "app.labs.bidirectional_forensic_lab",
        "app.labs.profit_lock_simulator",
    ]
    for mod in mods:
        path = pathlib.Path(importlib.import_module(mod).__file__)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    name = getattr(tgt, "id", None) or getattr(tgt, "attr", None)
                    if name in FORBIDDEN_ASSIGN_LITERALS and isinstance(node.value, ast.Constant):
                        assert node.value.value is not True, f"{mod} {name}=True"
