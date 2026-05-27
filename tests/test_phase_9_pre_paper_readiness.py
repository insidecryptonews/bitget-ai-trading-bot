from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from app.data_freshness_gate import FreshnessVerdict, evaluate_freshness
from app.fast_signal_shadow import ACTIONABILITY_NO_ACTIONABLE_DATA_STALE, run_fast_signal_shadow
from app.health_server import _phase9_research_endpoint, _research_pack_endpoint
from app.net_profit_lock_lab import (
    COST_MAKER_MAKER_AUDIT_ONLY_PCT,
    _price_at_pct_distance,
    _simulate_one_ladder_trade,
)
from app.phase8_candidate_validator import validate_phase8_candidate_from_samples
from app.phase8_candidate_validator import Phase8PolicySample
from app.phase8_research_utils import ReplayTradeContext, ReplayLoadBundle
from app.phase9_paper_readiness_validator import (
    PHASE9_READY,
    PHASE9_REJECT_DATA_STALE,
    _verdict_from_phase8,
)
from app.real_strategy_backtester import RealBacktestTrade


def _samples(values: list[float], *, symbol: str = "DOTUSDT", gross: float = 0.70) -> list[Phase8PolicySample]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [
        Phase8PolicySample(
            symbol=symbol,
            policy_name="late_entry_block_plus_dynamic_hold",
            timestamp=start + timedelta(minutes=5 * index),
            gross_return_pct=gross,
            net_return_pct=value,
        )
        for index, value in enumerate(values)
    ]


def _fresh(status: str = "OK", actionable: bool = True) -> FreshnessVerdict:
    return FreshnessVerdict(
        symbol="DOTUSDT",
        timeframe="5m",
        status=status,
        newest_timestamp=datetime.now(timezone.utc).isoformat(),
        age_minutes=1.0,
        staleness_budget_minutes=20,
        actionable=actionable,
        reasons=["test"],
    )


def test_phase9_ready_only_when_all_gates_pass_and_never_activates():
    baseline = _samples([-0.05] * 260)
    policy = _samples([0.35] * 260, gross=0.80)
    phase8 = validate_phase8_candidate_from_samples(
        candidate_id="DOTUSDT::late_entry",
        symbols=["DOTUSDT"],
        policy_name="late_entry_block_plus_dynamic_hold",
        baseline_samples=baseline,
        policy_samples=policy,
        min_trades=250,
    )
    verdict = _verdict_from_phase8(
        phase8,
        {"DOTUSDT": _fresh()},
        min_trades=250,
        min_net_pf=1.15,
        validation_hours=720,
    )
    assert verdict.phase9_decision == PHASE9_READY
    assert verdict.paper_filter_enabled is False
    assert verdict.can_send_real_orders is False
    assert verdict.final_recommendation == "NO LIVE"


def test_phase9_blocks_walk_forward_warn_and_mixed_folds():
    baseline = _samples([-0.05] * 260)
    policy = _samples([-0.02] * 65 + [0.40] * 195, gross=0.80)
    phase8 = validate_phase8_candidate_from_samples(
        candidate_id="DOTUSDT::mixed",
        symbols=["DOTUSDT"],
        policy_name="late_entry_block_plus_dynamic_hold",
        baseline_samples=baseline,
        policy_samples=policy,
        min_trades=250,
    )
    verdict = _verdict_from_phase8(
        phase8,
        {"DOTUSDT": _fresh()},
        min_trades=250,
        min_net_pf=1.15,
        validation_hours=720,
    )
    assert verdict.phase9_decision != PHASE9_READY
    assert "walk_forward" in verdict.blocked_gates


def test_phase9_blocks_stale_data_even_if_phase8_passes():
    baseline = _samples([-0.05] * 260)
    policy = _samples([0.35] * 260, gross=0.80)
    phase8 = validate_phase8_candidate_from_samples(
        candidate_id="DOTUSDT::freshness",
        symbols=["DOTUSDT"],
        policy_name="late_entry_block_plus_dynamic_hold",
        baseline_samples=baseline,
        policy_samples=policy,
        min_trades=250,
    )
    verdict = _verdict_from_phase8(
        phase8,
        {"DOTUSDT": _fresh("STALE", False)},
        min_trades=250,
        min_net_pf=1.15,
        validation_hours=720,
    )
    assert verdict.phase9_decision == PHASE9_REJECT_DATA_STALE
    assert "data_freshness" in verdict.blocked_gates


def test_data_freshness_gate_blocks_stale_and_need_data(monkeypatch):
    now = datetime(2026, 5, 27, 12, tzinfo=timezone.utc)
    monkeypatch.setattr("app.data_freshness_gate._newest_ohlcv_timestamp", lambda *a, **k: now - timedelta(minutes=40))
    stale = evaluate_freshness(object(), symbol="DOTUSDT", timeframe="5m", now=now)
    assert stale.status == "STALE"
    assert stale.actionable is False

    monkeypatch.setattr("app.data_freshness_gate._newest_ohlcv_timestamp", lambda *a, **k: None)
    missing = evaluate_freshness(object(), symbol="DOTUSDT", timeframe="5m", now=now)
    assert missing.status == "NEED_DATA"
    assert missing.actionable is False


def test_net_profit_lock_short_is_fee_aware_and_maker_maker_never_promotes():
    assert _price_at_pct_distance("SHORT", 100.0, 0.65) < 100.0
    candles = pd.DataFrame(
        [
            {"timestamp": "2026-01-01T00:00:00+00:00", "open": 100.0, "high": 100.1, "low": 99.2, "close": 99.4},
            {"timestamp": "2026-01-01T00:05:00+00:00", "open": 99.4, "high": 99.6, "low": 99.1, "close": 99.3},
        ]
    )
    trade = RealBacktestTrade(
        symbol="DOTUSDT",
        side="SHORT",
        signal_index=0,
        entry_index=0,
        exit_index=1,
        entry_price=100.0,
        exit_price=99.3,
        stop_loss=101.0,
        take_profit_1=99.0,
        gross_return_pct=0.7,
        net_return_pct=0.45,
        exit_reason="HORIZON_CLOSE",
        fee_cost_bps=12.0,
        slippage_cost_bps=3.0,
        funding_component_bps=0.0,
        total_cost_bps=15.0,
    )
    result = _simulate_one_ladder_trade(
        ReplayTradeContext("DOTUSDT", "5m", trade, candles),
        cost_pct=0.25,
        tp_fractions=(0.5, 1.0, 1.5),
        net_profit_lock_pct=0.40,
        break_even_buffer_pct=0.05,
        max_holding_bars=2,
    )
    assert result.net_return_pct > 0
    assert result.exit_price < 100.0
    assert COST_MAKER_MAKER_AUDIT_ONLY_PCT == 0.04


def test_fast_signal_shadow_stale_without_context_is_non_actionable(monkeypatch):
    monkeypatch.setattr(
        "app.fast_signal_shadow.load_replay_trade_contexts",
        lambda *a, **k: ReplayLoadBundle([], {}, [], 72, "5m", ["DOTUSDT"]),
    )
    monkeypatch.setattr(
        "app.fast_signal_shadow.evaluate_freshness",
        lambda *a, **k: _fresh("STALE", False),
    )
    report = run_fast_signal_shadow(SimpleNamespace(symbols=["DOTUSDT"]), object(), symbols=["DOTUSDT"])
    assert report.signals[0].actionability == ACTIONABILITY_NO_ACTIONABLE_DATA_STALE
    assert report.paper_filter_enabled is False
    assert report.can_send_real_orders is False


def test_phase9_dashboard_heavy_endpoint_skips_and_research_pack_is_safe_json():
    payload = _phase9_research_endpoint(
        None,
        None,
        {"hours": ["720"], "timeframe": ["5m"], "symbols": ["DOTUSDT"], "folds": ["4"]},
        "phase9_paper_readiness",
    )
    assert payload["status"] == "HEAVY_RESEARCH_SKIPPED"
    assert "phase9-paper-readiness" in payload["cli_command"]
    assert payload["paper_filter_enabled"] is False
    assert payload["final_recommendation"] == "NO LIVE"

    config = SimpleNamespace(
        live_trading=False,
        dry_run=True,
        paper_trading=True,
        enable_paper_policy_filter=False,
        enable_candidate_shadow_monitor=False,
        can_send_real_orders=False,
        main_timeframe="5m",
    )

    class EmptyDb:
        def table_exists(self, table: str) -> bool:
            return False

    pack = _research_pack_endpoint(config, EmptyDb(), {"hours": ["24"]})
    assert pack["safety"]["LIVE_TRADING"] is False
    assert pack["safety"]["ENABLE_PAPER_POLICY_FILTER"] is False
    assert pack["final_recommendation"] == "NO LIVE"
    assert "api_key=" not in str(pack).lower()
    assert "passphrase=" not in str(pack).lower()


def test_phase9_productive_modules_do_not_call_private_or_order_paths():
    root = Path(__file__).resolve().parents[1]
    modules = [
        root / "app" / "data_freshness_gate.py",
        root / "app" / "dot_regime_diagnosis.py",
        root / "app" / "dot_regime_filter_lab.py",
        root / "app" / "fast_signal_shadow.py",
        root / "app" / "net_profit_lock_lab.py",
        root / "app" / "paper_portfolio_allocator.py",
        root / "app" / "phase9_paper_readiness_validator.py",
        root / "app" / "research_pack.py",
    ]
    forbidden_calls = {
        "private_get",
        "private_post",
        "place_order",
        "set_leverage",
        "set_margin_mode",
        "open_position",
    }
    for module in modules:
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    assert node.func.id not in forbidden_calls, f"{node.func.id} call found in {module}"
                if isinstance(node.func, ast.Attribute):
                    assert node.func.attr not in forbidden_calls, f"{node.func.attr} call found in {module}"
                    if node.func.attr == "execute":
                        owner = node.func.value
                        assert not (
                            isinstance(owner, ast.Name) and owner.id == "ExecutionEngine"
                        ), f"ExecutionEngine.execute call found in {module}"
