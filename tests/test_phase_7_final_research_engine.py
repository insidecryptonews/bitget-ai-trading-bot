"""Tests for Phase 7 Final Research Engine sprint.

Covers:
- Track 1: aggregate PF + EV correct (no bug)
- Track 2: backtest breakdown grouping + decisions
- Track 3: policy builder gates (POLICY_READY/NEED_MORE_DATA/NO_EDGE/DATA_QUALITY_BLOCKER)
- Track 6: walk-forward folds + stability + decision
- Track 8: trade replay export JSON shape
- Track 9: CLI surface (commands registered, argparse accepts new flags)
- Safety: no exchange calls anywhere
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.backtest_breakdown import (
    DECISION_CANDIDATE_RESEARCH,
    DECISION_CANDIDATES_FOUND,
    DECISION_NEED_MORE_DATA,
    DECISION_NO_EDGE,
    DECISION_REJECT,
    DECISION_WATCH_ONLY,
    GroupSummary,
    TradeRecord,
    build_breakdown,
    parse_group_by,
    render_breakdown_text,
)
from app.config import load_config
from app.database import Database
from app.final_research_policy_builder import (
    DATA_QUALITY_BLOCKER,
    NEED_MORE_DATA,
    NO_EDGE_FOUND,
    POLICY_READY_FOR_PAPER,
    PolicyBuildInput,
    PolicyGates,
    build_policy,
    export_policy_json,
    render_policy_text,
)
from app.real_strategy_backtester import (
    _aggregate_total,
    real_strategy_backtest_multi,
)
from app.trade_replay_export import (
    build_replay_payload,
    export_replay_json,
    render_replay_summary,
)
from app.walk_forward_runner import (
    WF_FAIL,
    WF_NEED_MORE_FOLDS,
    WF_NOT_RUN,
    WF_PASS,
    build_walk_forward,
    render_walk_forward_text,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db(tmp_path: Path, monkeypatch) -> Database:
    monkeypatch.setattr("app.database.PROJECT_ROOT", tmp_path)
    cfg = load_config()
    instance = Database(cfg, logging.getLogger("test"))
    instance.sqlite_path = tmp_path / "test.db"
    Database._sqlite_wal_initialised = False
    instance.initialize()
    return instance


def _seed_ohlcv(db: Database, symbol: str, *, bars: int = 120) -> None:
    rows = []
    now = datetime.now(timezone.utc) - timedelta(minutes=bars * 5)
    price = 100.0
    for i in range(bars):
        ts = now + timedelta(minutes=i * 5)
        open_p = price
        close = price * (1 + (0.002 if i % 7 == 0 else -0.0005 if i % 11 == 0 else 0.0))
        high = max(open_p, close) * 1.002
        low = min(open_p, close) * 0.998
        rows.append({
            "symbol": symbol, "timeframe": "5m",
            "timestamp": ts.isoformat(),
            "open": open_p, "high": high, "low": low, "close": close,
            "volume": 1000.0, "quote_volume": 100_000.0,
        })
        price = close
    db.insert_ohlcv_batch(rows)


def _record(symbol: str, side: str, regime: str, score_bucket: str, net_pct: float, exit_reason: str = "TAKE_PROFIT", entry_index: int = 0, signal_type: str = "TREND") -> TradeRecord:
    return TradeRecord(
        symbol=symbol, side=side, regime=regime, score_bucket=score_bucket,
        signal_type=signal_type,
        setup_key=f"{symbol}|{side}|{regime}|{score_bucket}|5m|{signal_type}|current_exit|trade_signal",
        exit_reason=exit_reason,
        gross_return_pct=net_pct + 0.18,  # gross > net by typical cost
        net_return_pct=net_pct,
        entry_index=entry_index,
    )


# ---------------------------------------------------------------------------
# Track 1 — aggregate PF + EV correctness
# ---------------------------------------------------------------------------


def test_aggregate_total_with_all_negative_returns_correct_pf():
    """The bug was: PF=0 when all symbols negative even if trades had positive
    inside each. With the fix, PF is computed from per-trade gross_profit_sum
    and gross_loss_sum aggregated across symbols."""
    per_symbol = [
        {
            "symbol": "BTCUSDT", "trades": 10, "blocked_min_notional": 0,
            "net_ev": -0.10, "net_pf": 0.5, "win_rate": 0.3,
            "tp_pct": 0.1, "sl_pct": 0.2, "time_pct": 0.7,
            "same_bar_stop_tp_count": 0, "max_drawdown": 0.5,
            "gross_profit_sum": 2.0, "gross_loss_sum": 4.0,
            "total_pnl_sum": -1.0, "avg_trade_pnl": -0.1,
        },
        {
            "symbol": "ETHUSDT", "trades": 5, "blocked_min_notional": 0,
            "net_ev": -0.05, "net_pf": 0.7, "win_rate": 0.4,
            "tp_pct": 0.2, "sl_pct": 0.2, "time_pct": 0.6,
            "same_bar_stop_tp_count": 0, "max_drawdown": 0.3,
            "gross_profit_sum": 1.4, "gross_loss_sum": 2.0,
            "total_pnl_sum": -0.25, "avg_trade_pnl": -0.05,
        },
    ]
    total = _aggregate_total(per_symbol)
    assert total["trades"] == 15
    # Aggregate PF = (2.0+1.4) / (4.0+2.0) = 0.5667 — not zero
    assert total["net_pf"] == pytest.approx(3.4 / 6.0, abs=1e-6)
    # Net EV = (-1.0 - 0.25) / 15 = -0.0833...
    assert total["net_ev"] == pytest.approx(-1.25 / 15, abs=1e-6)


def test_aggregate_total_with_mixed_returns_correct():
    per_symbol = [
        {
            "symbol": "A", "trades": 10, "blocked_min_notional": 0,
            "net_ev": 0.20, "net_pf": 2.0, "win_rate": 0.7,
            "tp_pct": 0.5, "sl_pct": 0.2, "time_pct": 0.3,
            "same_bar_stop_tp_count": 0, "max_drawdown": 0.2,
            "gross_profit_sum": 4.0, "gross_loss_sum": 2.0,
            "total_pnl_sum": 2.0, "avg_trade_pnl": 0.2,
        },
        {
            "symbol": "B", "trades": 10, "blocked_min_notional": 0,
            "net_ev": -0.10, "net_pf": 0.5, "win_rate": 0.3,
            "tp_pct": 0.1, "sl_pct": 0.4, "time_pct": 0.5,
            "same_bar_stop_tp_count": 0, "max_drawdown": 0.4,
            "gross_profit_sum": 1.0, "gross_loss_sum": 2.0,
            "total_pnl_sum": -1.0, "avg_trade_pnl": -0.1,
        },
    ]
    total = _aggregate_total(per_symbol)
    assert total["net_pf"] == pytest.approx(5.0 / 4.0, abs=1e-6)  # 1.25
    assert total["net_ev"] == pytest.approx(1.0 / 20, abs=1e-6)


def test_aggregate_total_with_zero_trades_returns_zeros():
    per_symbol = [
        {"symbol": "A", "trades": 0, "blocked_min_notional": 0,
         "net_ev": 0.0, "net_pf": 0.0, "win_rate": 0.0,
         "tp_pct": 0.0, "sl_pct": 0.0, "time_pct": 0.0,
         "same_bar_stop_tp_count": 0, "max_drawdown": 0.0,
         "gross_profit_sum": 0.0, "gross_loss_sum": 0.0,
         "total_pnl_sum": 0.0, "avg_trade_pnl": 0.0},
    ]
    total = _aggregate_total(per_symbol)
    assert total["trades"] == 0
    assert total["net_pf"] == 0.0
    assert total["net_ev"] == 0.0


def test_multi_backtest_exposes_aggregate_debug_fields(db):
    _seed_ohlcv(db, "BTCUSDT")
    payload = real_strategy_backtest_multi(
        load_config(), db, hours=24, symbols=["BTCUSDT"], timeframe="5m",
    )
    total = payload["total"]
    for field_name in (
        "gross_profit_sum", "gross_loss_sum", "total_pnl_sum",
        "avg_trade_pnl", "trades_counted",
    ):
        assert field_name in total, f"missing {field_name} in TOTAL"


# ---------------------------------------------------------------------------
# Track 2 — backtest breakdown
# ---------------------------------------------------------------------------


def test_parse_group_by_accepts_valid_tokens():
    assert parse_group_by("symbol") == ["symbol"]
    assert parse_group_by("symbol,side") == ["symbol", "side"]
    assert parse_group_by("setup_key") == ["setup_key"]


def test_parse_group_by_rejects_invalid_tokens():
    with pytest.raises(ValueError):
        parse_group_by("not_a_real_token")


def test_parse_group_by_empty_defaults_to_symbol():
    assert parse_group_by("") == ["symbol"]


def test_build_breakdown_classifies_groups_correctly():
    records = [
        # Symbol A: 60 trades, all negative
        *[_record("A", "LONG", "RISK_ON", "85-89", -0.20, entry_index=i) for i in range(60)],
        # Symbol B: 60 trades, all positive
        *[_record("B", "LONG", "RISK_ON", "85-89", 0.30, entry_index=i) for i in range(60)],
        # Symbol C: 10 trades positive (small sample)
        *[_record("C", "LONG", "RISK_ON", "85-89", 0.50, entry_index=i) for i in range(10)],
        # Symbol D: 10 trades negative (small sample)
        *[_record("D", "LONG", "RISK_ON", "85-89", -0.20, entry_index=i) for i in range(10)],
    ]
    report = build_breakdown(records, group_by=["symbol"], min_trades=30)
    by_key = {g.group_key: g for g in (
        report.worst_groups + report.least_bad_groups
        + report.promising_watch_only_groups + report.candidate_research_groups
        + report.need_more_data_groups
    )}
    assert by_key["A"].decision == DECISION_REJECT
    assert by_key["B"].decision == DECISION_CANDIDATE_RESEARCH
    assert by_key["C"].decision == DECISION_WATCH_ONLY
    assert by_key["D"].decision == DECISION_NEED_MORE_DATA


def test_build_breakdown_decision_candidates_found_when_any_passes():
    records = [_record("A", "LONG", "RISK_ON", "85-89", 0.30, entry_index=i) for i in range(50)]
    report = build_breakdown(records, group_by=["symbol"], min_trades=30)
    assert report.decision == DECISION_CANDIDATES_FOUND


def test_build_breakdown_decision_no_edge_when_all_negative():
    records = [_record("A", "LONG", "RISK_ON", "85-89", -0.20, entry_index=i) for i in range(50)]
    report = build_breakdown(records, group_by=["symbol"], min_trades=30)
    assert report.decision == DECISION_NO_EDGE


def test_render_breakdown_text_includes_decision_and_no_live():
    records = [_record("A", "LONG", "RISK_ON", "85-89", -0.20, entry_index=i) for i in range(50)]
    report = build_breakdown(records, group_by=["symbol"], min_trades=30)
    text = render_breakdown_text(report)
    assert "REAL STRATEGY BACKTEST BREAKDOWN START" in text
    assert "decision: NO_EDGE_FOUND" in text
    assert "final_recommendation: NO LIVE" in text


def test_breakdown_compound_grouping_works():
    records = [
        *[_record("BTC", "LONG", "RISK_ON", "85-89", 0.30, entry_index=i) for i in range(40)],
        *[_record("BTC", "SHORT", "RISK_ON", "85-89", -0.20, entry_index=i) for i in range(40)],
    ]
    report = build_breakdown(records, group_by=["symbol", "side"], min_trades=30)
    # We expect "BTC|LONG" CANDIDATE and "BTC|SHORT" REJECT
    decisions = {g.group_key: g.decision for g in (
        report.candidate_research_groups + report.worst_groups + report.least_bad_groups
        + report.promising_watch_only_groups
    )}
    assert decisions.get("BTC|LONG") == DECISION_CANDIDATE_RESEARCH
    assert decisions.get("BTC|SHORT") == DECISION_REJECT


# ---------------------------------------------------------------------------
# Track 3 — policy builder
# ---------------------------------------------------------------------------


def _breakdown_with_candidate() -> "BreakdownReport":
    from app.backtest_breakdown import BreakdownReport
    candidate = GroupSummary(
        group_key="BTCUSDT|LONG|RISK_ON|85-89",
        trades=200, net_ev=0.30, net_pf=1.8, win_rate=0.55,
        tp_pct=0.40, sl_pct=0.30, time_pct=0.30,
        avg_pnl=0.30, gross_profit=50.0, gross_loss=20.0, max_drawdown=3.0,
        status="OK", decision=DECISION_CANDIDATE_RESEARCH,
    )
    return BreakdownReport(
        hours=720, timeframe="5m", group_by=["symbol", "side", "regime", "score_bucket"],
        min_trades=100, top_n=25, total_trades=200, total_groups=1,
        decision=DECISION_CANDIDATES_FOUND,
        candidate_research_groups=[candidate],
    )


def test_policy_builder_no_edge_when_breakdown_empty():
    from app.backtest_breakdown import BreakdownReport
    empty = BreakdownReport(
        hours=24, timeframe="5m", group_by=["symbol"], min_trades=30, top_n=25,
        total_trades=0, total_groups=0, decision=DECISION_NO_EDGE,
    )
    policy = build_policy(PolicyBuildInput(breakdown=empty))
    assert policy.decision == NO_EDGE_FOUND
    assert policy.paper_filter_enabled is False
    assert policy.can_send_real_orders is False


def test_policy_builder_data_quality_blocker_overrides():
    breakdown = _breakdown_with_candidate()
    policy = build_policy(PolicyBuildInput(
        breakdown=breakdown,
        data_quality_status="BAD",
        label_quality_status="OK",
        walk_forward_status=WF_PASS,
    ))
    assert policy.decision == DATA_QUALITY_BLOCKER
    assert not policy.allowed_symbols


def test_policy_builder_blocks_without_walk_forward_pass():
    breakdown = _breakdown_with_candidate()
    policy = build_policy(PolicyBuildInput(
        breakdown=breakdown,
        data_quality_status="OK",
        label_quality_status="OK",
        walk_forward_status=WF_NEED_MORE_FOLDS,
    ))
    assert policy.decision == NEED_MORE_DATA
    assert policy.paper_filter_enabled is False


def _phase8_pass_kwargs() -> dict[str, object]:
    return {
        "time_exit_autopsy_status": "PASS",
        "dynamic_hold_status": "PASS",
        "profit_protection_status": "PASS",
        "entry_exhaustion_status": "PASS",
        "reversal_lab_status": "RESEARCH_ONLY",
        "anti_overfit_status": "PASS",
        "phase8_candidate_validator_status": "PAPER_DEMO_READY_MANUAL_REVIEW_ONLY",
        "validation_hours": 720,
    }


def test_policy_builder_ready_for_paper_when_all_gates_pass():
    breakdown = _breakdown_with_candidate()
    policy = build_policy(PolicyBuildInput(
        breakdown=breakdown,
        data_quality_status="OK",
        label_quality_status="OK",
        walk_forward_status=WF_PASS,
        cost_stress_status="PASS",
        **_phase8_pass_kwargs(),
    ))
    assert policy.decision == POLICY_READY_FOR_PAPER
    # Still must NOT auto-activate
    assert policy.paper_filter_enabled is False
    assert policy.can_send_real_orders is False
    # Symbols/sides/regimes are extracted from group_key
    assert "BTCUSDT" in policy.allowed_symbols
    assert "LONG" in policy.allowed_sides
    assert "RISK_ON" in policy.allowed_regimes
    assert "85-89" in policy.allowed_score_buckets


def test_policy_builder_blocks_low_net_ev_below_gate():
    from app.backtest_breakdown import BreakdownReport
    weak = GroupSummary(
        group_key="X|LONG|RISK_ON|70-74",
        trades=200, net_ev=0.01, net_pf=1.01, win_rate=0.5,
        tp_pct=0.1, sl_pct=0.1, time_pct=0.8,
        avg_pnl=0.01, gross_profit=10.0, gross_loss=9.0, max_drawdown=5.0,
        status="OK", decision=DECISION_CANDIDATE_RESEARCH,
    )
    breakdown = BreakdownReport(
        hours=720, timeframe="5m", group_by=["symbol", "side", "regime", "score_bucket"],
        min_trades=100, top_n=25, total_trades=200, total_groups=1,
        decision=DECISION_CANDIDATES_FOUND,
        candidate_research_groups=[weak],
    )
    policy = build_policy(PolicyBuildInput(
        breakdown=breakdown,
        data_quality_status="OK", label_quality_status="OK",
        walk_forward_status=WF_PASS,
        cost_stress_status="PASS",
        **_phase8_pass_kwargs(),
    ))
    assert policy.decision == NO_EDGE_FOUND


def test_policy_builder_export_json_is_valid():
    breakdown = _breakdown_with_candidate()
    policy = build_policy(PolicyBuildInput(
        breakdown=breakdown,
        data_quality_status="OK", label_quality_status="OK",
        walk_forward_status=WF_PASS,
        cost_stress_status="PASS",
        **_phase8_pass_kwargs(),
    ))
    text = export_policy_json(policy)
    parsed = json.loads(text)
    assert parsed["decision"] == POLICY_READY_FOR_PAPER
    assert parsed["paper_filter_enabled"] is False
    assert parsed["can_send_real_orders"] is False


def test_policy_render_text_marks_no_live():
    breakdown = _breakdown_with_candidate()
    policy = build_policy(PolicyBuildInput(
        breakdown=breakdown,
        data_quality_status="OK", label_quality_status="OK",
        walk_forward_status=WF_PASS,
        cost_stress_status="PASS",
        **_phase8_pass_kwargs(),
    ))
    text = render_policy_text(policy)
    assert "POLICY_READY_FOR_PAPER" in text
    assert "final_recommendation: NO LIVE" in text
    assert "paper_filter_enabled: false" in text
    assert "auto_activation: never" in text


# ---------------------------------------------------------------------------
# Track 6 — walk-forward
# ---------------------------------------------------------------------------


def test_walk_forward_not_run_when_empty():
    report = build_walk_forward([], folds=4, min_trades_per_setup=100)
    assert report.overall_status == WF_NOT_RUN
    assert report.setups == []


def test_walk_forward_need_more_folds_when_sample_small():
    records = [_record("A", "LONG", "RISK_ON", "85-89", 0.30, entry_index=i) for i in range(20)]
    report = build_walk_forward(records, folds=4, min_trades_per_setup=100)
    assert report.overall_status in {WF_NEED_MORE_FOLDS, WF_FAIL}


def test_walk_forward_pass_when_all_windows_positive():
    records = [_record("A", "LONG", "RISK_ON", "85-89", 0.30, entry_index=i) for i in range(200)]
    report = build_walk_forward(records, folds=4, min_trades_per_setup=100)
    assert report.overall_status == WF_PASS
    setup = next(s for s in report.setups if "A|LONG" in s.group_key)
    assert setup.status == WF_PASS
    assert setup.positive_windows >= 2


def test_walk_forward_fail_when_test_window_negative():
    # First 150 positive, last 50 negative — last fold is the test window
    records = [_record("A", "LONG", "RISK_ON", "85-89", 0.30, entry_index=i) for i in range(150)]
    records += [_record("A", "LONG", "RISK_ON", "85-89", -0.20, entry_index=150+i) for i in range(50)]
    report = build_walk_forward(records, folds=4, min_trades_per_setup=100)
    setup = next(s for s in report.setups if "A|LONG" in s.group_key)
    assert setup.status == WF_FAIL
    assert any("test_net_ev" in r for r in setup.reasons)


def test_walk_forward_render_text_includes_no_live():
    report = build_walk_forward([], folds=4)
    text = render_walk_forward_text(report)
    assert "WALK FORWARD RUNNER START" in text
    assert "final_recommendation: NO LIVE" in text


# ---------------------------------------------------------------------------
# Track 8 — trade replay export
# ---------------------------------------------------------------------------


def test_trade_replay_export_returns_empty_payload_when_no_data(db):
    payload = build_replay_payload(load_config(), db, symbol="BTCUSDT", hours=24)
    assert payload.symbol == "BTCUSDT"
    assert payload.candles == []
    assert payload.trades == []
    assert payload.real_orders is False
    assert payload.exchange_calls is False


def test_trade_replay_export_emits_candles_when_data_present(db):
    _seed_ohlcv(db, "BTCUSDT")
    payload = build_replay_payload(load_config(), db, symbol="BTCUSDT", hours=24)
    assert len(payload.candles) > 0
    # JSON round-trip
    text = export_replay_json(payload)
    parsed = json.loads(text)
    assert parsed["symbol"] == "BTCUSDT"
    assert parsed["real_orders"] is False
    assert parsed["exchange_calls"] is False
    assert "final_recommendation" in parsed


def test_trade_replay_export_render_summary(db):
    _seed_ohlcv(db, "BTCUSDT")
    payload = build_replay_payload(load_config(), db, symbol="BTCUSDT", hours=24)
    text = render_replay_summary(payload)
    assert "TRADE REPLAY EXPORT START" in text
    assert "real_orders: false" in text
    assert "exchange_calls: false" in text
    assert "NO LIVE" in text


# ---------------------------------------------------------------------------
# Track 9 — CLI commands
# ---------------------------------------------------------------------------


def test_cli_lists_new_commands():
    source = Path("app/research_lab.py").read_text(encoding="utf-8")
    assert '"real-strategy-backtest-breakdown",' in source
    assert '"final-policy-builder",' in source
    assert '"trade-replay-export",' in source


def test_cli_argparse_accepts_new_flags():
    source = Path("app/research_lab.py").read_text(encoding="utf-8")
    for flag in ("--group-by", "--min-trades", "--top", "--folds",
                 "--data-quality-status", "--label-quality-status",
                 "--max-candles", "--max-trades"):
        assert flag in source, f"missing {flag} in CLI argparse"


def test_research_lab_exposes_new_methods(db):
    from app.research_lab import ResearchLab
    lab = ResearchLab(db, load_config(), logging.getLogger("test"))
    for attr in (
        "real_strategy_backtest_breakdown",
        "final_policy_builder",
        "trade_replay_export",
    ):
        assert hasattr(lab, attr), f"ResearchLab missing {attr}"


# ---------------------------------------------------------------------------
# Safety — no exchange calls anywhere in the new modules
# ---------------------------------------------------------------------------


def test_new_modules_have_no_execution_imports():
    import inspect
    import app.backtest_breakdown as bb
    import app.final_research_policy_builder as fb
    import app.walk_forward_runner as wf
    import app.trade_replay_export as tr

    for mod in (bb, fb, wf, tr):
        source = inspect.getsource(mod)
        for forbidden in (
            "BitgetClient(",         # no instantiation
            "ExecutionEngine(",
            "place_order(",
            "private_get(",
            "private_post(",
            "set_leverage(",
            "set_margin_mode(",
            "PaperTrader.open_position",
            "ENABLE_PAPER_POLICY_FILTER=True",
        ):
            assert forbidden not in source, f"forbidden token {forbidden} in {mod.__name__}"


def test_safety_flags_unchanged_by_new_modules():
    cfg = load_config()
    assert cfg.live_trading is False
    assert cfg.dry_run is True
    assert cfg.paper_trading is True
    assert cfg.enable_paper_policy_filter is False
    assert cfg.enable_candidate_shadow_monitor is False
    assert cfg.can_send_real_orders is False


def test_policy_builder_invariant_paper_filter_never_auto_activate():
    gates = PolicyGates()
    assert gates.paper_filter_never_auto_activate is True


def test_full_pipeline_with_negative_records_yields_no_edge():
    """End-to-end: bad data → breakdown REJECT → walk-forward NEED → policy NO_EDGE."""
    records = [_record("X", "LONG", "RISK_ON", "85-89", -0.20, entry_index=i) for i in range(200)]
    breakdown = build_breakdown(records, group_by=["symbol", "side", "regime", "score_bucket"], min_trades=100)
    wf = build_walk_forward(records, folds=4, min_trades_per_setup=100)
    policy = build_policy(PolicyBuildInput(
        breakdown=breakdown,
        data_quality_status="OK", label_quality_status="OK",
        walk_forward_status=wf.overall_status,
    ))
    # All trades negative → policy must NOT be paper-ready.
    assert policy.decision in {NO_EDGE_FOUND, NEED_MORE_DATA}
    assert policy.paper_filter_enabled is False
    assert policy.can_send_real_orders is False
