"""Tests for the V7.5 Clean Strategy Lab samples_clean=0 fix.

If clean_sample_count == 0 globally, no family can report:
- positive net_ev_pct,
- positive net_pf,
- the legacy PF=999.0 placeholder.

The fix forces NEED_MORE_DATA + ``why_not=no_clean_samples`` in that case.
"""

from __future__ import annotations

import pytest

from app.clean_strategy_lab import (
    DECISION_NEED_MORE_DATA,
    _build_family_result,
    _net_pf,
)
from app.shadow_multi_trade_learning import ShadowVirtualTrade


def _trade(side: str = "LONG", regime: str = "TREND_UP", net: float = 0.5,
           gross: float = 0.6, tp: bool = True) -> ShadowVirtualTrade:
    return ShadowVirtualTrade(
        shadow_id="sh1",
        symbol="DOTUSDT",
        timeframe="5m",
        side=side,
        setup_id="setup-x",
        entry_index=0,
        exit_index=10,
        entry_price=10.0,
        stop_price=9.95,
        tp1=10.05, tp2=10.10, tp3=10.15,
        score=70,
        regime=regime,
        cost_model="default",
        capital_scenario_id="c40",
        created_at="2026-06-01T00:00:00+00:00",
        closed_at="2026-06-01T00:10:00+00:00",
        data_freshness_status="OK",
        actionability="ACTIONABLE",
        status="CLOSED_TP1",
        reason="tp_hit",
        no_execution=True,
        mfe_pct=0.8,
        mae_pct=-0.1,
        bars_open=10,
        tp1_hit=tp,
        tp2_hit=False,
        tp3_hit=False,
        stop_hit=False,
        time_hit=False,
        net_profit_lock_hit=False,
        break_even_after_fees_hit=False,
        gross_pnl_pct=gross,
        net_pnl_pct=net,
        gross_pnl_usdt=0.0,
        net_pnl_usdt=0.0,
    )


def test_net_pf_never_returns_999_with_wins_only():
    # All-positive returns: the legacy code returned 999.0, the fix returns 0.0.
    values = [0.1, 0.2, 0.3, 0.4]
    pf = _net_pf(values)
    assert pf != 999.0, "PF=999 must never appear"
    assert pf == 0.0, "wins_only must return 0.0 per the V7.5 fix"


def test_net_pf_empty_returns_zero():
    assert _net_pf([]) == 0.0


def test_net_pf_normal_case():
    pf = _net_pf([1.0, 1.0, -0.5])
    # 2.0 / 0.5 = 4.0
    assert pf == pytest.approx(4.0)


def test_family_result_with_clean_sample_count_zero_forces_zero_metrics():
    trades = [_trade(net=0.5), _trade(net=0.7)]
    result = _build_family_result(
        family="X_dummy",
        description="dummy family",
        timeframe="5m",
        closed_trades=trades,
        side_filter=None,
        regime_filter=None,
        clean_metrics_dict={},
        trade_signal_clean=0,
        market_probe=0,
        raw_sample_count=100,
        clean_sample_count=0,  # <-- the bug condition
        data_quality_bad=True,
        ohlcv_stale=False,
    )
    assert result.samples_clean == 0
    assert result.net_ev_pct == 0.0, "net_ev must be 0 when samples_clean=0"
    assert result.net_pf == 0.0, "net_pf must be 0 when samples_clean=0"
    assert result.net_pf != 999.0, "PF=999 must never appear with samples_clean=0"
    assert result.decision == DECISION_NEED_MORE_DATA
    assert result.why_not == "no_clean_samples"


def test_family_result_with_clean_sample_count_nonzero_keeps_metrics():
    trades = [_trade(net=0.5), _trade(net=-0.2)]
    result = _build_family_result(
        family="Y_real",
        description="real family",
        timeframe="5m",
        closed_trades=trades,
        side_filter=None,
        regime_filter=None,
        clean_metrics_dict={},
        trade_signal_clean=200,
        market_probe=0,
        raw_sample_count=200,
        clean_sample_count=200,
        data_quality_bad=False,
        ohlcv_stale=False,
    )
    assert result.samples_clean == 200
    # With one win 0.5 and one loss -0.2, PF = 0.5 / 0.2 = 2.5
    assert result.net_pf == pytest.approx(2.5)


def test_family_result_with_empty_filtered_keeps_zero_and_no_promotion():
    result = _build_family_result(
        family="Z_empty",
        description="no trades",
        timeframe="5m",
        closed_trades=[],
        side_filter=None,
        regime_filter=None,
        clean_metrics_dict={},
        trade_signal_clean=0,
        market_probe=0,
        raw_sample_count=0,
        clean_sample_count=0,
        data_quality_bad=True,
        ohlcv_stale=False,
    )
    assert result.net_ev_pct == 0.0
    assert result.net_pf == 0.0
    assert result.decision == DECISION_NEED_MORE_DATA


def test_family_research_only_invariants_preserved():
    trades = [_trade()]
    result = _build_family_result(
        family="W_inv",
        description="invariants",
        timeframe="5m",
        closed_trades=trades,
        side_filter=None,
        regime_filter=None,
        clean_metrics_dict={},
        trade_signal_clean=0,
        market_probe=0,
        raw_sample_count=0,
        clean_sample_count=0,
        data_quality_bad=True,
        ohlcv_stale=False,
    )
    assert result.research_only is True
    assert result.paper_filter_enabled is False
    assert result.can_send_real_orders is False
    assert result.final_recommendation == "NO LIVE"
