"""Tests for ResearchOps V8/V9 Research Foundation modules.

Covers:
- auto_data_enrichment NEED_DATA path without crashing
- exit_intelligence_lab baseline + delta + need_more_data with small samples
- strategy_experiment_registry register/transition/snapshot
- shadow_candidate_lifecycle gate evaluation + state transitions
- validation_gates_v9 NEED_MORE_DATA / PASS / FAIL paths
- safety invariants: research_only=True, can_send_real_orders=False
- no forbidden executables across the new modules
"""

from __future__ import annotations

import ast
import importlib
import pathlib

import pytest


# -- Auto Data Enrichment ----------------------------------------------------

def test_enrichment_empty_db_returns_need_data():
    from app.auto_data_enrichment import (
        ENRICHMENT_STATUS_NEED_DATA,
        enrich_snapshot,
    )

    class _NoopDB:
        pass

    snap = enrich_snapshot(_NoopDB(), symbol="BTCUSDT", timeframe="5m", hours=24)
    assert snap.overall_status == ENRICHMENT_STATUS_NEED_DATA
    assert snap.research_only is True
    assert snap.can_send_real_orders is False
    assert snap.final_recommendation == "NO LIVE"
    assert snap.no_private_endpoints_used is True
    # All sources must be NEED_DATA (no DB methods exist)
    assert all(s.status == ENRICHMENT_STATUS_NEED_DATA for s in snap.sources[:-1])


def test_enrichment_partial_when_some_sources_available():
    from app.auto_data_enrichment import (
        ENRICHMENT_STATUS_PARTIAL,
        enrich_snapshot,
    )

    class _PartialDB:
        def latest_funding_rate(self, symbol):
            return 0.0001
        def latest_bid_ask_spread_bps(self, symbol):
            raise RuntimeError("unavailable")

    snap = enrich_snapshot(_PartialDB(), symbol="ETHUSDT")
    # Funding ok, spread errored, others missing → partial.
    assert snap.overall_status == ENRICHMENT_STATUS_PARTIAL
    funding = next(s for s in snap.sources if s.name == "funding")
    assert funding.status == "OK"
    assert funding.value == pytest.approx(0.0001)


def test_summarise_enrichment_aggregates_symbols():
    from app.auto_data_enrichment import summarise_enrichment

    class _DB:
        pass

    out = summarise_enrichment(_DB(), symbols=["BTCUSDT", "ETHUSDT"], timeframe="15m", hours=12)
    assert out["timeframe"] == "15m"
    assert out["research_only"] is True
    assert "BTCUSDT" in out["symbols_need_data"]
    assert out["final_recommendation"] == "NO LIVE"


# -- Exit Intelligence Lab ---------------------------------------------------

def _build_exit_trade(**overrides):
    from app.exit_intelligence_lab import SimulatedTradeInput
    base = dict(
        symbol="BTCUSDT", side="LONG", entry_price=100.0,
        tp1_pct=0.5, sl_pct=0.5, bars_open=10,
        mfe_pct=0.6, mae_pct=-0.2, net_pnl_pct=0.3, gross_pnl_pct=0.4,
        stop_hit=False, tp_hit=True, time_hit=False,
        regime="TREND_UP", btc_aligned=True,
    )
    base.update(overrides)
    return SimulatedTradeInput(**base)


def test_exit_intelligence_baseline_present_and_delta_zero():
    from app.exit_intelligence_lab import (
        EXIT_POLICY_BASELINE,
        run_exit_intelligence,
    )

    trades = [_build_exit_trade() for _ in range(10)]
    report = run_exit_intelligence(trades)
    assert report.samples == 10
    base_result = next(p for p in report.policies if p.policy == EXIT_POLICY_BASELINE)
    assert base_result.delta_net_vs_baseline_pct == pytest.approx(0.0)


def test_exit_intelligence_empty_trades_marks_need_more_data():
    from app.exit_intelligence_lab import run_exit_intelligence

    report = run_exit_intelligence([])
    assert report.need_more_data is True
    assert report.samples == 0


def test_exit_intelligence_safety_invariants():
    from app.exit_intelligence_lab import run_exit_intelligence

    report = run_exit_intelligence([_build_exit_trade()])
    assert report.research_only is True
    assert report.paper_filter_enabled is False
    assert report.can_send_real_orders is False
    assert report.final_recommendation == "NO LIVE"


# -- Strategy Experiment Registry --------------------------------------------

def test_registry_register_and_transition(tmp_path):
    from app.strategy_experiment_registry import (
        EXP_STATE_NEED_MORE_DATA,
        EXP_STATE_SHADOW_CANDIDATE,
        StrategyExperimentRegistry,
    )

    reg = StrategyExperimentRegistry(path=tmp_path / "exp.json")
    rec = reg.register(
        strategy_id="exp1", family="A", hypothesis="trend continuation",
        parameters={"ema": 200}, symbols=["BTCUSDT"], timeframe="5m",
    )
    assert rec.state == EXP_STATE_NEED_MORE_DATA
    out = reg.transition("exp1", new_state=EXP_STATE_SHADOW_CANDIDATE, reason="enough_samples")
    assert out is not None
    assert out.state == EXP_STATE_SHADOW_CANDIDATE
    assert len(out.history) == 2


def test_registry_snapshot_invariants(tmp_path):
    from app.strategy_experiment_registry import StrategyExperimentRegistry

    reg = StrategyExperimentRegistry(path=tmp_path / "snap.json")
    reg.register(strategy_id="e1", family="A", hypothesis="h", symbols=["X"])
    snap = reg.snapshot()
    assert snap["research_only"] is True
    assert snap["can_send_real_orders"] is False
    assert snap["final_recommendation"] == "NO LIVE"
    assert snap["total"] == 1


def test_registry_invalid_state_falls_back():
    from app.strategy_experiment_registry import (
        EXP_STATE_NEED_MORE_DATA,
        StrategyExperimentRegistry,
    )

    reg = StrategyExperimentRegistry()
    rec = reg.register(strategy_id="bad", family="A", hypothesis="h", state="ACTIVATE")
    assert rec.state == EXP_STATE_NEED_MORE_DATA


def test_registry_persists_to_disk(tmp_path):
    from app.strategy_experiment_registry import StrategyExperimentRegistry

    path = tmp_path / "persist.json"
    reg1 = StrategyExperimentRegistry(path=path)
    reg1.register(strategy_id="abc", family="F", hypothesis="h")
    reg2 = StrategyExperimentRegistry(path=path)
    assert reg2.get("abc") is not None


# -- Shadow Candidate Lifecycle ----------------------------------------------

def test_lifecycle_hard_gate_failure_keeps_need_more_data():
    from app.shadow_candidate_lifecycle import (
        LC_STATE_DETECTED,
        LC_STATE_NEED_MORE_DATA,
        evaluate_candidate,
    )

    verdict = evaluate_candidate(
        candidate_id="c1",
        current_state=LC_STATE_DETECTED,
        metrics={
            "data_quality_ok": False,
            "ohlcv_fresh": True,
            "no_duplicates": True,
            "no_lookahead": True,
            "samples_clean": 0,
        },
    )
    assert verdict.proposed_state == LC_STATE_NEED_MORE_DATA
    assert verdict.blockers, "blockers must list failed gates"


def test_lifecycle_all_gates_pass_promotes_to_label_only():
    from app.shadow_candidate_lifecycle import (
        LC_STATE_PAPER_CANDIDATE_LABEL_ONLY,
        evaluate_candidate,
    )

    verdict = evaluate_candidate(
        candidate_id="c2",
        current_state="DETECTED",
        metrics={
            "data_quality_ok": True,
            "ohlcv_fresh": True,
            "no_duplicates": True,
            "no_lookahead": True,
            "net_ev_pct": 0.5,
            "net_pf": 1.6,
            "samples_clean": 200, "min_samples": 150,
            "cost_stress_ok": True,
            "slippage_stress_ok": True,
            "funding_stress_ok": True,
            "walk_forward_ok": True,
            "regime_stability_ok": True,
            "symbol_stability_ok": True,
            "time_stability_ok": True,
            "no_single_fold_dominance": True,
        },
    )
    assert verdict.proposed_state == LC_STATE_PAPER_CANDIDATE_LABEL_ONLY
    assert verdict.can_send_real_orders is False
    assert verdict.final_recommendation == "NO LIVE"


def test_lifecycle_safety_invariants():
    from app.shadow_candidate_lifecycle import evaluate_candidate

    verdict = evaluate_candidate(candidate_id="c", current_state="DETECTED", metrics={})
    assert verdict.research_only is True
    assert verdict.paper_filter_enabled is False
    assert verdict.can_send_real_orders is False


# -- Validation Gates V9 -----------------------------------------------------

def test_validation_gates_v9_empty_returns_need_more_data():
    from app.validation_gates_v9 import GATE_NEED_MORE_DATA, run_validation_gates_v9

    report = run_validation_gates_v9(strategy_id="s1", net_returns=[])
    assert report.overall_status == GATE_NEED_MORE_DATA
    assert report.need_data_count >= 1


def test_validation_gates_v9_passes_with_clean_positive_returns():
    from app.validation_gates_v9 import run_validation_gates_v9

    returns = [0.3] * 60 + [-0.05] * 10
    folds = [{"net_ev": 0.2} for _ in range(8)]
    in_sample = [0.2] * 20
    out_sample = [0.15] * 20
    partitions = {"TREND_UP": [0.3] * 20, "TREND_DOWN": [0.2] * 20}
    report = run_validation_gates_v9(
        strategy_id="s2",
        net_returns=returns,
        folds=folds,
        in_sample=in_sample,
        out_sample=out_sample,
        partitions_by_regime=partitions,
        partitions_by_symbol=partitions,
        partitions_by_session=partitions,
    )
    assert report.research_only is True
    assert report.final_recommendation == "NO LIVE"
    # At least some gates must pass with such a clean sample.
    assert report.pass_count > 0


def test_validation_gates_v9_safety_invariants():
    from app.validation_gates_v9 import run_validation_gates_v9

    report = run_validation_gates_v9(strategy_id="s3", net_returns=[])
    assert report.research_only is True
    assert report.paper_filter_enabled is False
    assert report.can_send_real_orders is False


# -- Safety AST scan across new V8/V9 modules --------------------------------

FORBIDDEN_CALL_NAMES = {
    "place_order", "set_leverage", "set_margin_mode",
    "private_get", "private_post",
}

FORBIDDEN_ASSIGN_LITERALS = {
    "LIVE_TRADING": True,
    "ENABLE_PAPER_POLICY_FILTER": True,
    "can_send_real_orders": True,
    "allow_real_writes": True,
    "apply": True,
}

V8V9_MODULES = [
    "app.auto_data_enrichment",
    "app.exit_intelligence_lab",
    "app.strategy_experiment_registry",
    "app.shadow_candidate_lifecycle",
    "app.validation_gates_v9",
]


def _module_path(modname: str) -> pathlib.Path:
    mod = importlib.import_module(modname)
    return pathlib.Path(mod.__file__)


def test_v8v9_modules_have_no_forbidden_call_names():
    for modname in V8V9_MODULES:
        tree = ast.parse(_module_path(modname).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = getattr(fn, "attr", None) or getattr(fn, "id", None)
                assert name not in FORBIDDEN_CALL_NAMES, (
                    f"{modname} must not call {name}"
                )


def test_v8v9_modules_have_no_forbidden_literal_assigns():
    for modname in V8V9_MODULES:
        tree = ast.parse(_module_path(modname).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    name = getattr(tgt, "id", None) or getattr(tgt, "attr", None)
                    if name in FORBIDDEN_ASSIGN_LITERALS:
                        if isinstance(node.value, ast.Constant) and node.value.value is True:
                            raise AssertionError(
                                f"{modname}: forbidden literal {name}=True"
                            )


def test_v8v9_modules_emit_final_recommendation_no_live():
    # Each module exposes at least one report shape that ends in NO LIVE.
    from app.auto_data_enrichment import EnrichmentSnapshot
    from app.exit_intelligence_lab import ExitIntelligenceReport
    from app.shadow_candidate_lifecycle import LifecycleVerdict
    from app.validation_gates_v9 import ValidationGatesV9Report
    from app.strategy_experiment_registry import StrategyExperimentRegistry

    for inst in [
        EnrichmentSnapshot(
            symbol="X", timeframe="5m", hours=1, generated_at="t",
        ),
        ExitIntelligenceReport(hours=1, timeframe="5m", symbols=[]),
        LifecycleVerdict(candidate_id="c", current_state="DETECTED", proposed_state="DETECTED"),
        ValidationGatesV9Report(strategy_id="s", hours=1, timeframe="5m", samples=0),
    ]:
        assert getattr(inst, "final_recommendation") == "NO LIVE"
        assert getattr(inst, "paper_filter_enabled") is False
        assert getattr(inst, "can_send_real_orders") is False

    snap = StrategyExperimentRegistry().snapshot()
    assert snap["final_recommendation"] == "NO LIVE"
