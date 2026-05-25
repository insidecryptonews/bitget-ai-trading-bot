from __future__ import annotations

from app.backtest_breakdown import BreakdownReport, DECISION_CANDIDATE_RESEARCH, DECISION_CANDIDATES_FOUND, GroupSummary
from app.final_research_policy_builder import NEED_MORE_DATA, POLICY_READY_FOR_PAPER, PolicyBuildInput, build_policy
from app.walk_forward_runner import WF_PASS


def _breakdown() -> BreakdownReport:
    candidate = GroupSummary(
        group_key="BNBUSDT|LONG|RISK_ON|80-84",
        trades=600,
        net_ev=0.25,
        net_pf=2.0,
        win_rate=0.60,
        tp_pct=0.35,
        sl_pct=0.20,
        time_pct=0.45,
        avg_pnl=0.25,
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


def _base_input(**overrides):
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
        validation_hours=720,
    )
    data.update(overrides)
    return PolicyBuildInput(**data)


def test_72h_good_result_cannot_override_720h_gate():
    policy = build_policy(_base_input(validation_hours=72))
    assert policy.decision == NEED_MORE_DATA
    assert any("validation_hours=72_below_720" in reason for reason in policy.reasons)
    assert policy.paper_filter_enabled is False


def test_phase8_fail_blocks_paper_ready():
    policy = build_policy(_base_input(time_exit_autopsy_status="FAIL"))
    assert policy.decision == NEED_MORE_DATA
    assert any("time_exit_autopsy_status=FAIL" in reason for reason in policy.reasons)


def test_unknown_dynamic_hold_blocks_paper_ready():
    policy = build_policy(_base_input(dynamic_hold_status="UNKNOWN"))
    assert policy.decision == NEED_MORE_DATA
    assert any("dynamic_hold_status=UNKNOWN" in reason for reason in policy.reasons)


def test_all_phase8_gates_pass_still_does_not_auto_activate():
    policy = build_policy(_base_input())
    assert policy.decision == POLICY_READY_FOR_PAPER
    assert policy.paper_filter_enabled is False
    assert policy.can_send_real_orders is False
    assert "phase8_research_gates_passed" in policy.reasons
