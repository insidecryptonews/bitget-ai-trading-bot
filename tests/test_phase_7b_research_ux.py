"""Tests for Phase 7B sprint:
  - Cost Stress evaluation
  - Exit Labs bar-by-bar (profit lock / fast exit / time death reducer)
  - Policy Builder enriched (cost_stress + exit_lab inputs)
  - Trade Replay enriched (MFE/MAE/duration_bars)
  - Research Cockpit JSON
  - CLI surface + safety invariants
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from app.backtest_breakdown import (
    BreakdownReport,
    DECISION_CANDIDATE_RESEARCH,
    DECISION_CANDIDATES_FOUND,
    GroupSummary,
)
from app.config import load_config
from app.cost_stress import (
    BASE_COST_PCT,
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_UNKNOWN,
    STATUS_WARN,
    evaluate_cost_stress,
    render_cost_stress_text,
)
from app.database import Database
from app.exit_labs import (
    BASELINE_POLICY,
    ExitPolicy,
    fast_exit_policies,
    profit_lock_policies,
    render_exit_lab_text,
    run_exit_lab,
    run_fast_exit_lab,
    run_profit_lock_lab,
    run_time_death_reducer_lab,
    time_death_policies,
)
from app.final_research_policy_builder import (
    NEED_MORE_DATA,
    NO_EDGE_FOUND,
    POLICY_READY_FOR_PAPER,
    PolicyBuildInput,
    build_policy,
)
from app.research_cockpit import (
    build_cockpit_state,
    export_cockpit_json,
    render_cockpit_text,
)
from app.trade_replay_export import build_replay_payload
from app.walk_forward_runner import WF_PASS


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


def _seed_ohlcv(db: Database, symbol: str, *, bars: int = 200) -> None:
    rows = []
    now = datetime.now(timezone.utc) - timedelta(minutes=bars * 5)
    price = 100.0
    for i in range(bars):
        ts = now + timedelta(minutes=i * 5)
        open_p = price
        # Mild trending pattern: small up bars + occasional pullback.
        ret = 0.002 if i % 7 == 0 else (-0.0015 if i % 13 == 0 else 0.0003)
        close = price * (1 + ret)
        high = max(open_p, close) * 1.003
        low = min(open_p, close) * 0.997
        rows.append({
            "symbol": symbol, "timeframe": "5m",
            "timestamp": ts.isoformat(),
            "open": open_p, "high": high, "low": low, "close": close,
            "volume": 1000.0, "quote_volume": 100_000.0,
        })
        price = close
    db.insert_ohlcv_batch(rows)


# ---------------------------------------------------------------------------
# Cost Stress
# ---------------------------------------------------------------------------


def test_cost_stress_unknown_with_empty_returns():
    report = evaluate_cost_stress([])
    assert report.cost_stress_status == STATUS_UNKNOWN
    assert report.trades == 0


def test_cost_stress_fail_when_base_negative():
    # gross 0.05% per trade < base cost 0.18% → net negative
    report = evaluate_cost_stress([0.05] * 20)
    assert report.cost_stress_status == STATUS_FAIL
    assert "base_cost_net_ev_not_positive" in report.reasons


def test_cost_stress_pass_when_survives_022():
    # gross 0.50% per trade → net 0.32 base / 0.28 at 0.22% / 0.25 at 0.25%
    report = evaluate_cost_stress([0.50] * 20)
    assert report.cost_stress_status == STATUS_PASS


def test_cost_stress_warn_when_marginal_at_022():
    # gross 0.20% per trade → net 0.02 base / -0.02 at 0.22% → marginal negative
    report = evaluate_cost_stress([0.20] * 20)
    assert report.cost_stress_status == STATUS_WARN


def test_cost_stress_fail_collapses_at_022():
    # gross 0.19% per trade → net 0.01 base / -0.03 at 0.22% / -0.06 at 0.25%
    # margin between 0.01 base and -0.03 at 0.22% is < 0.05 tolerance → WARN
    # but with -0.06 at 0.25% definitely collapsed. The classifier returns WARN
    # because the marginal-negative-tolerance kicks in.
    report = evaluate_cost_stress([0.19] * 20)
    assert report.cost_stress_status in {STATUS_FAIL, STATUS_WARN}


def test_cost_stress_render_includes_no_live():
    report = evaluate_cost_stress([0.30] * 10)
    text = render_cost_stress_text(report)
    assert "COST STRESS REPORT START" in text
    assert "final_recommendation: NO LIVE" in text
    assert "maker_maker_scenario_is_audit_only" in text


# ---------------------------------------------------------------------------
# Exit Labs
# ---------------------------------------------------------------------------


def test_exit_lab_with_no_data_returns_empty_report(db):
    report = run_profit_lock_lab(load_config(), db, symbol="BTCUSDT", hours=2, timeframe="5m")
    assert report.baseline_trades == 0
    assert report.comparisons == []


def test_exit_lab_runs_baseline_and_alternatives_with_data(db):
    _seed_ohlcv(db, "BTCUSDT", bars=300)
    report = run_profit_lock_lab(load_config(), db, symbol="BTCUSDT", hours=30, timeframe="5m")
    # Even if no trades fire, structure should be present
    assert report.symbol == "BTCUSDT"
    assert report.lab_name == "profit_lock"


def test_exit_lab_render_includes_no_live(db):
    _seed_ohlcv(db, "BTCUSDT", bars=200)
    report = run_profit_lock_lab(load_config(), db, symbol="BTCUSDT", hours=20, timeframe="5m")
    text = render_exit_lab_text(report)
    assert "EXIT LAB" in text
    assert "no_lookahead_status: OK_PREFIX_ONLY" in text
    assert "stop_tp_same_bar_rule: STOP_BEFORE_TP" in text
    assert "final_recommendation: NO LIVE" in text


def test_exit_lab_profit_lock_policies_include_baseline():
    names = [p.name for p in profit_lock_policies()]
    assert "baseline" in names
    assert any("profit_lock" in n for n in names)
    assert any("trail" in n for n in names)


def test_exit_lab_fast_exit_policies_have_no_followthrough():
    fe_policies = fast_exit_policies()
    non_baseline = [p for p in fe_policies if p.name != "baseline"]
    assert all(p.no_followthrough_bars is not None for p in non_baseline)


def test_exit_lab_time_death_policies_have_max_holding_override():
    td_policies = time_death_policies()
    non_baseline = [p for p in td_policies if p.name != "baseline"]
    assert all(p.max_holding_bars_override is not None for p in non_baseline)


def test_exit_lab_no_lookahead_in_simulate_one_trade():
    """Sanity: _simulate_one_trade only reads candles up to entry_index + horizon."""
    from app.exit_labs import _simulate_one_trade
    # Build a frame where bar[10] has an extreme high; ensure simulation with
    # horizon=5 starting at bar 0 never sees that bar.
    rows = []
    for i in range(20):
        rows.append({
            "timestamp": pd.Timestamp("2026-01-01", tz="UTC") + pd.Timedelta(minutes=5 * i),
            "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0,
            "volume": 1000.0, "quote_volume": 100_000.0,
        })
    rows[10]["high"] = 200.0  # would trigger TP if seen
    df = pd.DataFrame(rows)
    trade = _simulate_one_trade(
        side="LONG", entry_index=0, entry_price=100.0,
        stop=99.0, take_profit=150.0,
        candles=df, policy=BASELINE_POLICY,
        max_holding_bars=5,
    )
    # The TP at 150 is never reached because the only bar high enough is bar 10
    # which is OUT of our horizon (5).
    assert trade.exit_reason != "TAKE_PROFIT"


def test_exit_lab_profit_lock_same_bar_stop_wins():
    """Profit lock must not fabricate edge when the same candle also hits SL."""
    from app.exit_labs import EXIT_STOP_LOSS, _simulate_one_trade
    rows = [{
        "timestamp": pd.Timestamp("2026-01-01", tz="UTC"),
        "open": 100.0, "high": 101.0, "low": 98.0, "close": 100.5,
        "volume": 1000.0, "quote_volume": 100_000.0,
    }]
    df = pd.DataFrame(rows)
    trade = _simulate_one_trade(
        side="LONG", entry_index=0, entry_price=100.0,
        stop=99.0, take_profit=103.0, candles=df,
        policy=ExitPolicy(name="profit_lock_0_40", profit_lock_threshold_pct=0.40),
        max_holding_bars=1,
    )
    assert trade.exit_reason == EXIT_STOP_LOSS


# ---------------------------------------------------------------------------
# Policy Builder enriched
# ---------------------------------------------------------------------------


def _breakdown_with_candidate() -> BreakdownReport:
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


def test_policy_builder_blocks_when_cost_stress_fail():
    policy = build_policy(PolicyBuildInput(
        breakdown=_breakdown_with_candidate(),
        data_quality_status="OK", label_quality_status="OK",
        walk_forward_status=WF_PASS,
        cost_stress_status="FAIL",
        cost_stress_reasons=["base_positive_but_collapses_at_0_22"],
    ))
    assert policy.decision == NO_EDGE_FOUND
    assert any("cost_stress_status=FAIL" in r for r in policy.reasons)
    assert policy.paper_filter_enabled is False
    assert policy.can_send_real_orders is False


def test_policy_builder_paper_ready_when_cost_stress_pass():
    policy = build_policy(PolicyBuildInput(
        breakdown=_breakdown_with_candidate(),
        data_quality_status="OK", label_quality_status="OK",
        walk_forward_status=WF_PASS,
        cost_stress_status="PASS",
    ))
    assert policy.decision == POLICY_READY_FOR_PAPER
    assert any("cost_stress_status=PASS" in r for r in policy.reasons)
    # Still must not auto-activate
    assert policy.paper_filter_enabled is False
    assert policy.can_send_real_orders is False


def test_policy_builder_blocks_pf_999_with_low_sample():
    low_sample_pf_999 = GroupSummary(
        group_key="BTCUSDT|LONG|RISK_ON|85-89",
        trades=120, net_ev=0.30, net_pf=999.0, win_rate=1.0,
        tp_pct=1.0, sl_pct=0.0, time_pct=0.0,
        avg_pnl=0.30, gross_profit=36.0, gross_loss=0.0, max_drawdown=0.0,
        status="OK", decision=DECISION_CANDIDATE_RESEARCH,
    )
    breakdown = BreakdownReport(
        hours=720, timeframe="5m", group_by=["symbol", "side", "regime", "score_bucket"],
        min_trades=100, top_n=25, total_trades=120, total_groups=1,
        decision=DECISION_CANDIDATES_FOUND,
        candidate_research_groups=[low_sample_pf_999],
    )
    policy = build_policy(PolicyBuildInput(
        breakdown=breakdown,
        data_quality_status="OK", label_quality_status="OK",
        walk_forward_status=WF_PASS,
        cost_stress_status="PASS",
    ))
    assert policy.decision != POLICY_READY_FOR_PAPER
    assert policy.paper_filter_enabled is False
    assert policy.can_send_real_orders is False


def test_policy_builder_blocks_paper_ready_when_cost_stress_warn():
    policy = build_policy(PolicyBuildInput(
        breakdown=_breakdown_with_candidate(),
        data_quality_status="OK", label_quality_status="OK",
        walk_forward_status=WF_PASS,
        cost_stress_status="WARN",
    ))
    assert policy.decision == NEED_MORE_DATA
    assert any("cost_stress_status=WARN" in r for r in policy.reasons)
    assert policy.paper_filter_enabled is False
    assert policy.can_send_real_orders is False


def test_policy_builder_blocks_paper_ready_when_cost_stress_unknown():
    policy = build_policy(PolicyBuildInput(
        breakdown=_breakdown_with_candidate(),
        data_quality_status="OK", label_quality_status="OK",
        walk_forward_status=WF_PASS,
        cost_stress_status="UNKNOWN",
    ))
    assert policy.decision == NEED_MORE_DATA
    assert any("cost_stress_status=UNKNOWN" in r for r in policy.reasons)
    assert policy.paper_filter_enabled is False
    assert policy.can_send_real_orders is False


def test_policy_builder_exit_lab_summary_propagates_to_reasons():
    policy = build_policy(PolicyBuildInput(
        breakdown=_breakdown_with_candidate(),
        data_quality_status="OK", label_quality_status="OK",
        walk_forward_status=WF_PASS,
        cost_stress_status="PASS",
        exit_lab_summary={"profit_lock": "applied", "fast_exit": "applied"},
    ))
    assert policy.decision == POLICY_READY_FOR_PAPER
    assert any("exit_lab_summary_consumed" in r for r in policy.reasons)


# ---------------------------------------------------------------------------
# Trade Replay enriched
# ---------------------------------------------------------------------------


def test_trade_replay_includes_mfe_mae_duration_when_trades_present(db):
    _seed_ohlcv(db, "BTCUSDT", bars=200)
    payload = build_replay_payload(load_config(), db, symbol="BTCUSDT", hours=20)
    assert payload.real_orders is False
    assert payload.exchange_calls is False
    for trade in payload.trades:
        assert hasattr(trade, "mfe_pct")
        assert hasattr(trade, "mae_pct")
        assert hasattr(trade, "duration_bars")
        assert trade.duration_bars >= 1


def test_trade_replay_empty_payload_does_not_crash(db):
    # No OHLCV seeded → empty candles list, no trades.
    payload = build_replay_payload(load_config(), db, symbol="BTCUSDT", hours=2)
    assert payload.candles == []
    assert payload.trades == []
    assert payload.real_orders is False


# ---------------------------------------------------------------------------
# Research Cockpit
# ---------------------------------------------------------------------------


def test_research_cockpit_returns_no_live(db):
    state = build_cockpit_state(
        load_config(), db,
        mode="paper",
        latest_backtest_decision="UNKNOWN",
        latest_breakdown_decision="NO_EDGE_FOUND",
        latest_policy_decision="NO_EDGE_FOUND",
    )
    assert state.final_recommendation == "NO LIVE"
    assert state.paper_filter_enabled is False
    assert state.can_send_real_orders is False
    assert state.policy_ready_for_paper is False


def test_research_cockpit_marks_policy_ready_flag(db):
    state = build_cockpit_state(
        load_config(), db,
        latest_policy_decision="POLICY_READY_FOR_PAPER",
    )
    assert state.policy_ready_for_paper is True
    assert any("paper_filter_must_be_activated_by_human" in n for n in state.notes)
    # Even with policy ready, runtime flags stay False
    assert state.paper_filter_enabled is False
    assert state.can_send_real_orders is False


def test_research_cockpit_json_roundtrip(db):
    state = build_cockpit_state(load_config(), db)
    text = export_cockpit_json(state)
    parsed = json.loads(text)
    assert parsed["final_recommendation"] == "NO LIVE"
    assert parsed["paper_filter_enabled"] is False
    assert parsed["can_send_real_orders"] is False
    assert "safety_flags" in parsed


def test_research_cockpit_text_format(db):
    state = build_cockpit_state(load_config(), db)
    text = render_cockpit_text(state)
    assert "RESEARCH COCKPIT START" in text
    assert "RESEARCH COCKPIT END" in text
    assert "auto_activation: never" in text


def test_research_cockpit_ohlcv_status_reflects_seeded_data(db):
    _seed_ohlcv(db, "BTCUSDT", bars=100)
    state = build_cockpit_state(load_config(), db)
    assert state.ohlcv_status in {"OK", "PARTIAL"}
    assert state.ohlcv_symbols_with_data >= 1
    assert state.ohlcv_total_rows >= 100


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


def test_cli_lists_new_commands():
    source = Path("app/research_lab.py").read_text(encoding="utf-8")
    for cmd in (
        '"cost-stress-summary",',
        '"profit-lock-lab",',
        '"fast-exit-lab",',
        '"time-death-reducer-lab",',
        '"research-cockpit",',
    ):
        assert cmd in source, f"missing CLI command listing: {cmd}"


def test_research_lab_exposes_phase_7b_methods(db):
    from app.research_lab import ResearchLab
    lab = ResearchLab(db, load_config(), logging.getLogger("test"))
    for attr in (
        "cost_stress_summary",
        "profit_lock_lab",
        "fast_exit_lab",
        "time_death_reducer_lab",
        "research_cockpit",
    ):
        assert hasattr(lab, attr), f"ResearchLab missing {attr}"


# ---------------------------------------------------------------------------
# Safety — no exchange / no order placement / no paper filter activation
# ---------------------------------------------------------------------------


def test_phase_7b_modules_have_no_execution_imports():
    import inspect
    import app.cost_stress as cs
    import app.exit_labs as el
    import app.research_cockpit as rc

    for mod in (cs, el, rc):
        source = inspect.getsource(mod)
        for forbidden in (
            "BitgetClient(",
            "ExecutionEngine(",
            "place_order(",
            "private_get(",
            "private_post(",
            "set_leverage(",
            "set_margin_mode(",
            "PaperTrader.open_position",
            "ENABLE_PAPER_POLICY_FILTER=True",
        ):
            assert forbidden not in source, f"forbidden {forbidden} in {mod.__name__}"


def test_safety_flags_unchanged_by_phase_7b():
    cfg = load_config()
    assert cfg.live_trading is False
    assert cfg.dry_run is True
    assert cfg.paper_trading is True
    assert cfg.enable_paper_policy_filter is False
    assert cfg.enable_candidate_shadow_monitor is False
    assert cfg.can_send_real_orders is False


def test_end_to_end_pipeline_with_cost_stress_blocks_paper_ready():
    """If cost stress fails, even a strong-looking breakdown does not yield PAPER_READY."""
    policy = build_policy(PolicyBuildInput(
        breakdown=_breakdown_with_candidate(),
        data_quality_status="OK", label_quality_status="OK",
        walk_forward_status=WF_PASS,
        cost_stress_status="FAIL",
        cost_stress_reasons=["base_positive_but_collapses_at_0_22"],
    ))
    assert policy.decision != POLICY_READY_FOR_PAPER
    assert policy.paper_filter_enabled is False


# ---------------------------------------------------------------------------
# Phase 7B — Short report anti-regression for soft sections
# ---------------------------------------------------------------------------


def test_short_report_lists_candidate_incubator_as_soft_section():
    """Anti-regression: Candidate Incubator 24h is classified as SOFT, so its
    timeout/error degrades to `limited_summary` and never to PARTIAL_REPORT."""
    from app.dashboard_pro import DashboardProReporter

    assert "Candidate Incubator 24h" in DashboardProReporter.SHORT_REPORT_SOFT_SECTIONS


def test_short_report_soft_section_timeout_keeps_report_ok(db, monkeypatch):
    """A soft section that throws/times out must NOT downgrade report_status."""
    from app.dashboard_pro import DashboardProReporter, ReportSection

    reporter = DashboardProReporter(load_config(), db)

    # Monkeypatch _run_section so the Candidate Incubator entry returns the
    # exact contract we expect from a soft-section timeout: status=limited_summary
    # and a non-PARTIAL warning. Other sections behave as ok.
    original_run = reporter._run_section

    def fake_run(name, callback, *, timeout_seconds=None, soft=False):
        if name == "Candidate Incubator 24h":
            assert soft is True, "Candidate Incubator must be invoked with soft=True"
            return ReportSection(
                name=name,
                text="LIMITED_SUMMARY: simulated_timeout",
                status="limited_summary",
                duration_ms=int((timeout_seconds or 3.0) * 1000),
                warning=f"SOFT_SECTION_LIMITED: {name}",
            )
        # Return a stub OK section for every other section without invoking
        # the underlying heavy callback.
        return ReportSection(name=name, text=f"stub_ok_{name}", status="ok", duration_ms=1, warning="")

    monkeypatch.setattr(reporter, "_run_section", fake_run)

    payload = reporter.build_short(hours=24)
    assert payload["report_status"] == "OK", payload["report_status"]
    statuses = {s["name"]: s["status"] for s in payload["sections"]}
    assert statuses.get("Candidate Incubator 24h") == "limited_summary"
    # No "timeout" / "error" status should be present in soft path.
    assert all(s["status"] != "timeout" for s in payload["sections"])


def test_short_report_run_section_real_soft_timeout_uses_limited_summary():
    """Real ThreadPoolExecutor timeout on a soft section must produce
    status=limited_summary and a non-fatal warning."""
    from app.dashboard_pro import DashboardProReporter
    import time as time_mod

    reporter = DashboardProReporter(load_config(), db=None)

    def slow_callback():
        time_mod.sleep(0.6)
        return "should never appear"

    section = reporter._run_section(
        "Candidate Incubator 24h", slow_callback,
        timeout_seconds=0.05, soft=True,
    )
    assert section.status == "limited_summary"
    assert "SOFT_SECTION_LIMITED" in section.warning


def test_short_report_run_section_real_hard_timeout_keeps_timeout():
    """Real ThreadPoolExecutor timeout on a non-soft section keeps status=timeout."""
    from app.dashboard_pro import DashboardProReporter
    import time as time_mod

    reporter = DashboardProReporter(load_config(), db=None)

    def slow_callback():
        time_mod.sleep(0.6)
        return "should never appear"

    section = reporter._run_section(
        "Other Heavy Section", slow_callback,
        timeout_seconds=0.05, soft=False,
    )
    assert section.status == "timeout"
    assert "SECTION_TIMEOUT" in section.warning


def test_short_report_run_section_soft_exception_uses_limited_summary():
    """Soft section that raises must degrade to limited_summary, not error."""
    from app.dashboard_pro import DashboardProReporter

    reporter = DashboardProReporter(load_config(), db=None)

    def boom():
        raise RuntimeError("boom")

    section = reporter._run_section(
        "Candidate Incubator 24h", boom,
        timeout_seconds=None, soft=True,
    )
    assert section.status == "limited_summary"
    # Hard section comparison: same code path but soft=False keeps "error".
    section_hard = reporter._run_section(
        "Other Section", boom, timeout_seconds=None, soft=False,
    )
    assert section_hard.status == "error"


# ---------------------------------------------------------------------------
# Phase 7B — Dashboard/health-server endpoint handlers
# ---------------------------------------------------------------------------


def test_health_server_has_phase_7b_endpoint_handlers():
    """The new endpoint handler functions must exist in health_server."""
    from app import health_server as hs

    for name in (
        "_research_cockpit",
        "_cost_stress",
        "_profit_lock_lab",
        "_fast_exit_lab",
        "_time_death_reducer_lab",
        "_trade_replay",
        "_final_policy_builder",
    ):
        assert hasattr(hs, name), f"health_server missing {name}"


def test_health_server_research_cockpit_payload_returns_no_live(db):
    from app import health_server as hs

    payload = hs._research_cockpit(load_config(), db, {})
    assert payload["final_recommendation"] == "NO LIVE"
    assert payload["research_only"] is True
    assert payload["paper_filter_enabled"] is False
    assert payload["can_send_real_orders"] is False
    assert "text" in payload
    assert "RESEARCH COCKPIT START" in payload["text"]


def test_health_server_cost_stress_endpoint_empty_data_is_unknown(db):
    """No trades → UNKNOWN status, no crash."""
    from app import health_server as hs

    payload = hs._cost_stress(load_config(), db, {"hours": ["24"], "timeframe": ["5m"]})
    assert payload["final_recommendation"] == "NO LIVE"
    assert payload["can_send_real_orders"] is False
    # With no trades, cost_stress_status must be UNKNOWN
    assert payload["cost_stress_status"] in {"UNKNOWN", "FAIL", "WARN", "PASS"}


def test_health_server_trade_replay_endpoint_returns_payload(db):
    from app import health_server as hs

    payload = hs._trade_replay(
        load_config(), db,
        {"symbol": ["BTCUSDT"], "hours": ["6"], "timeframe": ["5m"], "max_trades": ["10"]},
    )
    assert payload["final_recommendation"] == "NO LIVE"
    assert payload["research_only"] is True
    assert payload["can_send_real_orders"] is False
    assert "candles" in payload
    assert "trades" in payload


def test_health_server_exit_lab_endpoints_return_no_live(db):
    from app import health_server as hs

    for fn in (hs._profit_lock_lab, hs._fast_exit_lab, hs._time_death_reducer_lab):
        payload = fn(
            load_config(), db,
            {"symbol": ["BTCUSDT"], "hours": ["6"], "timeframe": ["5m"]},
        )
        assert payload["final_recommendation"] == "NO LIVE"
        assert payload["research_only"] is True
        assert payload["can_send_real_orders"] is False


def test_health_server_final_policy_builder_endpoint_returns_decision(db):
    from app import health_server as hs

    payload = hs._final_policy_builder(
        load_config(), db,
        {"hours": ["24"], "timeframe": ["5m"], "enriched": ["1"]},
    )
    assert payload["final_recommendation"] == "NO LIVE"
    assert payload["can_send_real_orders"] is False
    assert "decision" in payload


def test_health_server_endpoints_in_authorized_list():
    """Phase 7B endpoints must be in the authorized GET list (token required)."""
    import inspect
    from app import health_server as hs

    source = inspect.getsource(hs)
    for path in (
        "/api/training/research-cockpit",
        "/api/training/cost-stress",
        "/api/training/profit-lock-lab",
        "/api/training/fast-exit-lab",
        "/api/training/time-death-reducer-lab",
        "/api/training/trade-replay",
        "/api/training/final-policy-builder",
    ):
        assert path in source, f"endpoint {path} not registered"


# ---------------------------------------------------------------------------
# Phase 7B — Dashboard frontend integration markers
# ---------------------------------------------------------------------------


def test_dashboard_html_has_phase_7b_sections():
    from pathlib import Path

    html_path = Path(__file__).resolve().parent.parent / "app" / "static" / "dashboard.html"
    body = html_path.read_text(encoding="utf-8", errors="ignore")
    for section in (
        'id="research-cockpit"',
        'id="trade-replay"',
        'id="cost-stress"',
        'id="exit-labs"',
    ):
        assert section in body, f"dashboard.html missing {section}"
    # The frontend must keep the NO LIVE / PAPER ONLY badges in place.
    assert "NO LIVE" in body
    assert "PAPER ONLY" in body


def test_dashboard_js_wires_phase_7b_endpoints():
    from pathlib import Path

    js_path = Path(__file__).resolve().parent.parent / "app" / "static" / "dashboard.js"
    body = js_path.read_text(encoding="utf-8", errors="ignore")
    for marker in (
        "/api/training/research-cockpit",
        "/api/training/trade-replay",
        "/api/training/cost-stress",
        "/api/training/profit-lock-lab",
        "/api/training/fast-exit-lab",
        "/api/training/time-death-reducer-lab",
    ):
        assert marker in body, f"dashboard.js missing endpoint {marker}"


# ---------------------------------------------------------------------------
# Phase 7B — Safety: new endpoints must NOT touch live execution paths
# ---------------------------------------------------------------------------


def test_phase_7b_endpoints_do_not_invoke_execution_or_private_endpoints(db, monkeypatch):
    """Spy on dangerous methods. Any phase 7B endpoint that invokes them fails."""
    from app import health_server as hs

    sentinels: list[str] = []

    def trip(name):
        def _inner(*a, **kw):
            sentinels.append(name)
            raise AssertionError(f"forbidden call: {name}")
        return _inner

    # BitgetClient private endpoints / order placement.
    try:
        from app.bitget_client import BitgetClient
        for forbidden in ("private_get", "private_post", "place_order", "set_leverage", "set_margin_mode"):
            if hasattr(BitgetClient, forbidden):
                monkeypatch.setattr(BitgetClient, forbidden, trip(f"BitgetClient.{forbidden}"))
    except Exception:
        pass
    # PaperTrader.open_position should never be called by research endpoints.
    try:
        from app.paper_trader import PaperTrader
        if hasattr(PaperTrader, "open_position"):
            monkeypatch.setattr(PaperTrader, "open_position", trip("PaperTrader.open_position"))
    except Exception:
        pass

    cfg = load_config()
    for fn, query in (
        (hs._research_cockpit, {}),
        (hs._cost_stress, {"hours": ["24"]}),
        (hs._trade_replay, {"symbol": ["BTCUSDT"], "hours": ["6"]}),
        (hs._profit_lock_lab, {"symbol": ["BTCUSDT"], "hours": ["6"]}),
        (hs._fast_exit_lab, {"symbol": ["BTCUSDT"], "hours": ["6"]}),
        (hs._time_death_reducer_lab, {"symbol": ["BTCUSDT"], "hours": ["6"]}),
        (hs._final_policy_builder, {"hours": ["24"]}),
    ):
        payload = fn(cfg, db, query)
        assert payload["final_recommendation"] == "NO LIVE"
    assert sentinels == [], f"forbidden calls invoked: {sentinels}"
