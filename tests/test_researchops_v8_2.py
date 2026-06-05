"""Tests for ResearchOps V8.2 Bidirectional Forensics + Campaign + Exit Lab.

All labs run with rows passed directly (no DB needed). Safety scan confirms
no forbidden calls or literal assigns.
"""

from __future__ import annotations

import ast
import importlib
import pathlib

import pytest


# ---------------------------------------------------------------------------
# Bidirectional funnel
# ---------------------------------------------------------------------------

def _signal_row(proposed: str, regime: str, score: int, **overrides):
    """Build a synthetic signal row.

    ``proposed`` is the strategy-proposed side. By default executed ``side``
    equals ``proposed``; pass ``side="NO_TRADE"`` to override.
    """
    base = {
        "symbol": "BTCUSDT",
        "side": proposed,
        "proposed_side": proposed,
        "market_regime": regime,
        "confidence_score": score,
        "reason": overrides.pop("reason", ""),
        "gross_ev_pct": overrides.pop("gross_ev_pct", 0.0),
        "net_ev_pct": overrides.pop("net_ev_pct", 0.0),
        "mfe_pct": overrides.pop("mfe_pct", 0.0),
        "mae_pct": overrides.pop("mae_pct", 0.0),
        "first_barrier_hit": overrides.pop("first_barrier_hit", ""),
        "ret_15m_pct": overrides.pop("ret_15m_pct", None),
        "ret_30m_pct": overrides.pop("ret_30m_pct", None),
        "ret_1h_pct": overrides.pop("ret_1h_pct", None),
        "ret_4h_pct": overrides.pop("ret_4h_pct", None),
        "realized_pct": overrides.pop("realized_pct", None),
        "bars_open": overrides.pop("bars_open", None),
    }
    base.update(overrides)
    return base


def test_funnel_no_double_counting():
    from app.labs.bidirectional_forensic_lab import build_funnel

    rows = [
        _signal_row("LONG", "TREND_UP", 80),
        _signal_row("LONG", "RISK_ON", 75),
        _signal_row("SHORT", "RISK_OFF", 65),
        _signal_row("NO_TRADE", "RISK_OFF", 50),
        _signal_row("SHORT", "TREND_DOWN", 78),
    ]
    report = build_funnel(None, hours=24, rows=rows)
    assert report.total_signals == 5
    # by_side sum equals total
    assert sum(report.by_side.values()) == 5
    # by_regime sum equals total
    assert sum(report.by_regime.values()) == 5
    # by_score_bucket sum equals total
    assert sum(report.by_score_bucket.values()) == 5
    assert report.research_only is True
    assert report.final_recommendation == "NO LIVE"


def test_funnel_side_filter():
    from app.labs.bidirectional_forensic_lab import build_funnel

    rows = [
        _signal_row("LONG", "TREND_UP", 80),
        _signal_row("SHORT", "RISK_OFF", 78),
    ]
    report = build_funnel(None, hours=24, side_filter="SHORT", rows=rows)
    assert report.total_signals == 1
    assert report.by_side == {"SHORT": 1}


def test_funnel_need_data_when_db_empty():
    from app.labs.bidirectional_forensic_lab import build_funnel

    class _NoopDB:
        pass

    report = build_funnel(_NoopDB(), hours=24)
    assert report.status == "NEED_DATA"
    assert "signal_observations_method_missing_or_empty" in report.need_data_reasons


# ---------------------------------------------------------------------------
# Missed opportunities — direction logic
# ---------------------------------------------------------------------------

def test_missed_opportunities_short_with_negative_future_return_marks_true():
    from app.labs.bidirectional_forensic_lab import missed_opportunities

    rows = [
        _signal_row("SHORT", "RISK_OFF", 65, side="NO_TRADE", proposed_side="SHORT",
                    ret_1h_pct=-1.5, ret_4h_pct=-3.0, reason="score_too_low"),
    ]
    report = missed_opportunities(None, side="SHORT", hours=24, rows=rows)
    assert len(report.candidates) == 1
    assert report.candidates[0]["would_have_worked_estimate"] == "True"


def test_missed_opportunities_long_with_positive_future_return_marks_true():
    from app.labs.bidirectional_forensic_lab import missed_opportunities

    rows = [
        _signal_row("LONG", "RANGE", 65, side="NO_TRADE", proposed_side="LONG",
                    ret_1h_pct=1.5, reason="score_too_low"),
    ]
    report = missed_opportunities(None, side="LONG", hours=24, rows=rows)
    assert report.candidates[0]["would_have_worked_estimate"] == "True"


def test_missed_opportunities_short_with_positive_future_return_marks_false():
    from app.labs.bidirectional_forensic_lab import missed_opportunities

    rows = [
        _signal_row("SHORT", "RISK_OFF", 65, side="NO_TRADE", proposed_side="SHORT",
                    ret_1h_pct=2.0, reason="score_too_low"),
    ]
    report = missed_opportunities(None, side="SHORT", hours=24, rows=rows)
    assert report.candidates[0]["would_have_worked_estimate"] == "False"


def test_missed_opportunities_need_data_when_future_returns_missing():
    from app.labs.bidirectional_forensic_lab import missed_opportunities

    rows = [
        _signal_row("SHORT", "RISK_OFF", 65, side="NO_TRADE", proposed_side="SHORT"),
    ]
    report = missed_opportunities(None, side="SHORT", hours=24, rows=rows)
    assert report.candidates[0]["would_have_worked_estimate"] == "NEED_DATA"


# ---------------------------------------------------------------------------
# Score asymmetry audit + simulations
# ---------------------------------------------------------------------------

def test_score_asymmetry_audit_detects_gap_with_mock():
    from app.labs.score_asymmetry_audit import audit

    rows = [
        _signal_row("LONG", "RISK_ON", 85),
        _signal_row("LONG", "RISK_ON", 90),
        _signal_row("LONG", "TREND_UP", 78),
        _signal_row("SHORT", "RISK_OFF", 55),
        _signal_row("SHORT", "RISK_OFF", 60),
        _signal_row("SHORT", "TREND_DOWN", 65),
    ]
    r = audit(None, hours=24, rows=rows)
    assert r.status == "OK"
    assert r.median_long > r.median_short
    assert r.gap_long_minus_short > 0


def test_symmetric_regime_simulation_only_changes_short():
    from app.labs.score_asymmetry_audit import simulate_symmetric_regime

    rows = [
        _signal_row("LONG", "RISK_ON", 80),
        _signal_row("LONG", "RISK_ON", 82),
        _signal_row("SHORT", "RISK_OFF", 60),   # baseline NO, sim adds 15 → 75 (PASS)
        _signal_row("SHORT", "RISK_OFF", 50),   # 50+15=65, still NO
    ]
    r = simulate_symmetric_regime(None, hours=24, min_score=72, rows=rows)
    assert r.delta_long_pass == 0  # LONG path unchanged
    assert r.delta_short_pass >= 1  # at least 1 SHORT passes now


def test_atr_softening_does_not_reduce_pass_count_vs_baseline():
    from app.labs.score_asymmetry_audit import simulate_atr_softening

    rows = [
        _signal_row("LONG", "RISK_ON", 80, normalized_atr=0.030),
        _signal_row("SHORT", "RISK_OFF", 70, normalized_atr=0.040),
    ]
    r = simulate_atr_softening(None, hours=24, min_score=72, rows=rows)
    # Softening always relaxes the score (less penalty or zero) → cannot reduce pass count.
    assert r.delta_long_pass >= 0
    assert r.delta_short_pass >= 0


def test_high_vol_directional_allows_correct_side_with_mock():
    from app.labs.score_asymmetry_audit import simulate_high_vol_directional

    rows = [
        # SHORT in HIGH_VOL with negative momentum: aligned, should add +20.
        _signal_row("SHORT", "HIGH_VOLATILITY", 60, momentum_15=-0.07),
        # LONG in HIGH_VOL with positive momentum: aligned, +20.
        _signal_row("LONG", "HIGH_VOLATILITY", 60, momentum_15=0.07),
        # SHORT in HIGH_VOL with POSITIVE momentum: not aligned, no bonus.
        _signal_row("SHORT", "HIGH_VOLATILITY", 60, momentum_15=0.07),
    ]
    r = simulate_high_vol_directional(None, hours=24, min_score=72, rows=rows)
    assert r.delta_short_pass >= 1
    assert r.delta_long_pass >= 1


# ---------------------------------------------------------------------------
# Regime router simulator
# ---------------------------------------------------------------------------

def test_regime_router_overrides_to_no_trade_on_news_red():
    from app.labs.regime_router_simulator import (
        RouterInputs,
        STATE_NO_TRADE,
        decide,
    )

    d = decide(RouterInputs(
        timestamp="t", btc_bias_1h="bearish", btc_bias_4h="bearish",
        pct_universe_up=0.0, pct_universe_down=0.9,
        regime_current="RISK_OFF", news_risk_red=True,
    ))
    assert d.state == STATE_NO_TRADE
    assert d.override_reason == "news_risk_gate_red"


def test_regime_router_short_only_on_strong_bear():
    from app.labs.regime_router_simulator import (
        RouterInputs,
        STATE_SHORT_ONLY,
        decide,
    )

    d = decide(RouterInputs(
        timestamp="t", btc_bias_1h="bearish", btc_bias_4h="bearish",
        pct_universe_up=0.0, pct_universe_down=0.85,
        regime_current="RISK_OFF",
    ))
    assert d.state == STATE_SHORT_ONLY
    assert d.allowed_sides == ["SHORT"]


def test_regime_router_long_only_on_strong_bull():
    from app.labs.regime_router_simulator import (
        RouterInputs,
        STATE_LONG_ONLY,
        decide,
    )

    d = decide(RouterInputs(
        timestamp="t", btc_bias_1h="bullish", btc_bias_4h="bullish",
        pct_universe_up=0.85, pct_universe_down=0.0,
        regime_current="RISK_ON",
    ))
    assert d.state == STATE_LONG_ONLY
    assert d.allowed_sides == ["LONG"]


def test_regime_router_simulation_aggregates():
    from app.labs.regime_router_simulator import RouterInputs, simulate_router

    stream = [
        RouterInputs(timestamp=f"t{i}",
                     btc_bias_1h="bearish", btc_bias_4h="bearish",
                     pct_universe_up=0.0, pct_universe_down=0.8,
                     regime_current="RISK_OFF")
        for i in range(10)
    ]
    r = simulate_router(None, hours=10, inputs_stream=stream)
    assert r.samples == 10
    assert r.by_state["SHORT_ONLY_RESEARCH"] == 10
    assert r.final_recommendation == "NO LIVE"


# ---------------------------------------------------------------------------
# Trend campaign simulator
# ---------------------------------------------------------------------------

def _bar(o, h, l, c):
    return {"open": o, "high": h, "low": l, "close": c}


def test_campaign_no_add_when_base_is_losing():
    """Verify the simulator does not add to losers."""
    from app.labs.trend_campaign_simulator import CampaignTrade, simulate_campaign

    # SHORT entry at 100. Price keeps going UP (base losing). Should never add.
    bars = [_bar(100, 101, 99.5, 100.8),
            _bar(100.8, 102, 100.5, 101.5),
            _bar(101.5, 103, 101, 102.5)]
    trade = CampaignTrade(symbol="X", side="SHORT", entry=100.0,
                          stop=103.0, bar_path=bars, atr_pct_at_entry=0.5)
    result = simulate_campaign(trade, max_adds=8)
    assert result["adds_executed"] == 0


def test_campaign_can_add_when_base_is_winning():
    from app.labs.trend_campaign_simulator import CampaignTrade, simulate_campaign

    # SHORT entry at 100. Price goes DOWN (continuation). Should be able to add.
    bars = [
        _bar(100, 100.1, 99.0, 99.2),  # base in profit by 0.8%
        _bar(99.2, 99.3, 98.0, 98.2),  # continuation, may add
        _bar(98.2, 98.3, 97.0, 97.2),  # continuation, may add
        _bar(97.2, 97.3, 96.5, 96.8),  # continuation
    ]
    trade = CampaignTrade(symbol="X", side="SHORT", entry=100.0,
                          stop=102.0, bar_path=bars, atr_pct_at_entry=0.5)
    result = simulate_campaign(trade, max_adds=3)
    assert result["adds_executed"] >= 1


def test_campaign_simulator_marks_high_risk_for_more_than_3_adds():
    from app.labs.trend_campaign_simulator import CampaignTrade, run_campaign_simulation

    bars = [_bar(100, 100.2, 99.0, 99.3),
            _bar(99.3, 99.4, 98.0, 98.2),
            _bar(98.2, 98.3, 97.0, 97.2),
            _bar(97.2, 97.3, 96.0, 96.2),
            _bar(96.2, 96.3, 95.0, 95.2)]
    trade = CampaignTrade(symbol="X", side="SHORT", entry=100.0,
                          stop=102.0, bar_path=bars, atr_pct_at_entry=0.5)
    r = run_campaign_simulation(None, side="SHORT", hours=24,
                                max_adds_variants=(0, 1, 3, 5, 8),
                                trades=[trade])
    high_risk_variants = [v for v in r.variants if v.get("high_risk_flag")]
    assert any(v["adds_max"] >= 5 for v in high_risk_variants)


def test_campaign_research_only_invariants():
    from app.labs.trend_campaign_simulator import CampaignTrade, run_campaign_simulation

    bars = [_bar(100, 100.1, 99.0, 99.3)]
    trade = CampaignTrade(symbol="X", side="LONG", entry=99.5,
                          stop=99.0, bar_path=bars)
    r = run_campaign_simulation(None, side="LONG", hours=24, trades=[trade])
    assert r.research_only is True
    assert r.paper_filter_enabled is False
    assert r.can_send_real_orders is False
    assert r.final_recommendation == "NO LIVE"


# ---------------------------------------------------------------------------
# Profit lock simulator
# ---------------------------------------------------------------------------

def test_profit_lock_baseline_respects_stop_before_tp():
    from app.labs.profit_lock_simulator import (
        ExitTrade,
        POLICY_BASELINE,
        _simulate_baseline,
    )

    # LONG entry 100, stop 99, tp1 101, tp2 102.
    # A bar that touches BOTH 99 (stop) and 102 (tp2) → stop wins.
    bars = [_bar(100, 102.0, 99.0, 100.5)]
    trade = ExitTrade(
        symbol="X", side="LONG", entry=100.0, stop=99.0,
        tp1=101.0, tp2=102.0, bar_path=bars, fees_pct=0.0,
    )
    r = _simulate_baseline(trade)
    # Stop wins, realized = (99-100)/100*100 = -1.0%
    assert r["exit_reason"] == "STOP_LOSS"
    assert r["realized_net_pct"] == pytest.approx(-1.0)


def test_profit_lock_simulation_includes_baseline():
    from app.labs.profit_lock_simulator import (
        ExitTrade,
        POLICY_BASELINE,
        run_profit_lock_simulation,
    )

    bars = [_bar(100, 101, 99.5, 100.5), _bar(100.5, 102, 100, 101.5)]
    trade = ExitTrade(
        symbol="X", side="LONG", entry=100.0, stop=99.0,
        tp1=101.0, tp2=102.0, bar_path=bars,
    )
    r = run_profit_lock_simulation(None, side="LONG", hours=24,
                                   policies=["trailing_atr"],
                                   trades=[trade])
    policies = {p["policy"] for p in r.policies}
    assert POLICY_BASELINE in policies  # baseline is always added
    assert r.final_recommendation == "NO LIVE"


def test_profit_lock_research_only_invariants():
    from app.labs.profit_lock_simulator import ExitTrade, run_profit_lock_simulation

    trade = ExitTrade(symbol="X", side="LONG", entry=100, stop=99, tp1=101, tp2=102,
                      bar_path=[_bar(100, 101, 99.5, 100.5)])
    r = run_profit_lock_simulation(None, side="LONG", hours=24, trades=[trade])
    assert r.research_only is True
    assert r.paper_filter_enabled is False
    assert r.can_send_real_orders is False


# ---------------------------------------------------------------------------
# Forensic helpers — failed_executed / good_not_monetized
# ---------------------------------------------------------------------------

def test_failed_executed_classifies_time_death_with_uncaptured_mfe():
    from app.labs.bidirectional_forensic_lab import failed_executed

    rows = [
        _signal_row("SHORT", "RISK_OFF", 75, side="SHORT",
                    first_barrier_hit="TIME", realized_pct=-0.15,
                    mfe_pct=2.5, mae_pct=-0.5, bars_open=30),
    ]
    r = failed_executed(None, side="SHORT", hours=24, rows=rows)
    assert len(r.failures) == 1
    assert r.failures[0]["failure_reason"] == "time_death_with_uncaptured_mfe"


def test_good_not_monetized_detects_tp_capped_upside():
    from app.labs.bidirectional_forensic_lab import good_not_monetized

    rows = [
        _signal_row("LONG", "TREND_UP", 80, side="LONG",
                    first_barrier_hit="TP", realized_pct=0.8,
                    mfe_pct=3.0, mae_pct=-0.1, bars_open=15),
    ]
    r = good_not_monetized(None, side="LONG", hours=24, rows=rows)
    assert len(r.cases) == 1
    assert r.cases[0]["likely_cause"] == "tp_fixed_capped_upside"


# ---------------------------------------------------------------------------
# Safety AST scan
# ---------------------------------------------------------------------------

FORBIDDEN_CALL_NAMES = {
    "place_order", "set_leverage", "set_margin_mode",
    "private_get", "private_post",
}

FORBIDDEN_ASSIGN_LITERALS = {
    "LIVE_TRADING", "ENABLE_PAPER_POLICY_FILTER",
    "can_send_real_orders", "allow_real_writes", "apply",
}

V82_MODULES = [
    "app.labs",
    "app.labs.bidirectional_forensic_lab",
    "app.labs.score_asymmetry_audit",
    "app.labs.regime_router_simulator",
    "app.labs.trend_campaign_simulator",
    "app.labs.profit_lock_simulator",
    "app.labs.research_pack_bidirectional_v1",
]


def _module_path(modname: str) -> pathlib.Path:
    return pathlib.Path(importlib.import_module(modname).__file__)


def test_v82_modules_have_no_forbidden_calls():
    for mod in V82_MODULES:
        tree = ast.parse(_module_path(mod).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = getattr(fn, "attr", None) or getattr(fn, "id", None)
                assert name not in FORBIDDEN_CALL_NAMES, (
                    f"{mod} must not call {name}"
                )


def test_v82_modules_have_no_forbidden_literal_true_assigns():
    for mod in V82_MODULES:
        tree = ast.parse(_module_path(mod).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    name = getattr(tgt, "id", None) or getattr(tgt, "attr", None)
                    if name in FORBIDDEN_ASSIGN_LITERALS and isinstance(node.value, ast.Constant):
                        assert node.value.value is not True, (
                            f"{mod} contains forbidden {name}=True"
                        )


def test_v82_all_outputs_carry_no_live():
    from app.labs.bidirectional_forensic_lab import (
        FailedExecutedReport,
        FunnelReport,
        GoodNotMonetizedReport,
        MissedOpsReport,
    )
    from app.labs.profit_lock_simulator import ProfitLockReport
    from app.labs.regime_router_simulator import RouterSimulationReport
    from app.labs.score_asymmetry_audit import AsymmetryReport, SimulationReport
    from app.labs.trend_campaign_simulator import CampaignSimulationReport

    instances = [
        FunnelReport(hours=1, generated_at="t", side_filter=None),
        MissedOpsReport(hours=1, generated_at="t", side="LONG", top_n=1),
        FailedExecutedReport(hours=1, generated_at="t", side="LONG", top_n=1),
        GoodNotMonetizedReport(hours=1, generated_at="t", side="LONG", top_n=1),
        AsymmetryReport(hours=1),
        SimulationReport(hours=1, name="x"),
        RouterSimulationReport(hours=1, samples=0),
        CampaignSimulationReport(hours=1, side="LONG", samples=0),
        ProfitLockReport(hours=1, side="LONG", samples=0, baseline_policy="b"),
    ]
    for inst in instances:
        assert inst.final_recommendation == "NO LIVE"
        assert inst.research_only is True
        assert inst.paper_filter_enabled is False
        assert inst.can_send_real_orders is False
