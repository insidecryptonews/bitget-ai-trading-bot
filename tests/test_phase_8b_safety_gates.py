from __future__ import annotations

from pathlib import Path

from app.backtest_breakdown import BreakdownReport, DECISION_CANDIDATE_RESEARCH, DECISION_CANDIDATES_FOUND, GroupSummary
from app.final_research_policy_builder import NEED_MORE_DATA, POLICY_READY_FOR_PAPER, PolicyBuildInput, build_policy
from app.walk_forward_runner import WF_PASS


def _breakdown() -> BreakdownReport:
    candidate = GroupSummary(
        group_key="DOTUSDT|LONG|RISK_ON|80-84",
        trades=600,
        net_ev=0.30,
        net_pf=2.0,
        win_rate=0.60,
        tp_pct=0.35,
        sl_pct=0.20,
        time_pct=0.45,
        avg_pnl=0.30,
        gross_profit=200.0,
        gross_loss=100.0,
        max_drawdown=5.0,
        status="OK",
        decision=DECISION_CANDIDATE_RESEARCH,
    )
    return BreakdownReport(
        hours=720,
        timeframe="5m",
        group_by=["symbol", "side", "regime", "score_bucket"],
        min_trades=100,
        top_n=25,
        total_trades=600,
        total_groups=1,
        decision=DECISION_CANDIDATES_FOUND,
        candidate_research_groups=[candidate],
    )


def _input(**overrides):
    data = dict(
        breakdown=_breakdown(),
        data_quality_status="OK",
        label_quality_status="OK",
        walk_forward_status=WF_PASS,
        cost_stress_status="PASS",
        time_exit_autopsy_status="PASS",
        dynamic_hold_status="PASS",
        profit_protection_status="PASS",
        entry_exhaustion_status="PASS",
        reversal_lab_status="RESEARCH_ONLY",
        anti_overfit_status="PASS",
        phase8_candidate_validator_status="PAPER_DEMO_READY_MANUAL_REVIEW_ONLY",
        validation_hours=720,
    )
    data.update(overrides)
    return PolicyBuildInput(**data)


def test_policy_builder_blocks_unknown_phase8_candidate_validator():
    policy = build_policy(_input(phase8_candidate_validator_status="UNKNOWN"))
    assert policy.decision == NEED_MORE_DATA
    assert any("phase8_candidate_validator_status=UNKNOWN" in reason for reason in policy.reasons)
    assert policy.paper_filter_enabled is False
    assert policy.can_send_real_orders is False


def test_policy_builder_allows_manual_review_only_but_never_activates():
    policy = build_policy(_input())
    assert policy.decision == POLICY_READY_FOR_PAPER
    assert policy.paper_filter_enabled is False
    assert policy.can_send_real_orders is False


def test_phase8b_modules_do_not_call_private_or_order_paths():
    root = Path(__file__).resolve().parents[1]
    modules = [
        root / "app" / "phase8_candidate_validator.py",
        root / "app" / "exit_policy_v2.py",
        root / "app" / "dynamic_hold_lab.py",
        root / "app" / "time_exit_autopsy_v2.py",
        root / "app" / "entry_exhaustion_lab.py",
        root / "app" / "reversal_candidate_lab.py",
    ]
    forbidden = [
        "private_get(",
        "private_post(",
        "place_order(",
        "set_leverage(",
        "set_margin_mode(",
        "ExecutionEngine.execute",
        "PaperTrader.open_position",
        "can_send_real_orders=True",
        "LIVE_TRADING=True",
        "ENABLE_PAPER_POLICY_FILTER=True",
    ]
    for module in modules:
        text = module.read_text(encoding="utf-8")
        for needle in forbidden:
            assert needle not in text, f"{needle} found in {module}"
