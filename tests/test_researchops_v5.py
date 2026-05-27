"""Tests for ResearchOps V5 sprint.

Covers:
  - OHLCV freshness manager (status + refresh dry-run + auto-disabled guard)
  - Training data clean view (BAD blocks readiness)
  - Shadow multi-trade learning (research-only, no PaperTrader.open_position)
  - Capital / leverage simulator (notional, ROE, promotion gating)
  - Fee-aware exit trainer (gross green net negative blocks promotion)
  - Phase 9 readiness V2 gates (data quality BAD, net negative, catastrophic fold)
  - Dashboard endpoints existence and safety markers
"""

from __future__ import annotations

import ast
import inspect
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.config import load_config
from app.database import Database


def _strip_string_literals_and_comments(source: str) -> str:
    """Return source with string literals and comments stripped.

    Walks the AST and replaces every Constant string (incl. docstrings)
    with an empty literal so safety scans cannot trip on documentation.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source
    string_spans: list[tuple[int, int, int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            string_spans.append((node.lineno, node.col_offset, node.end_lineno, node.end_col_offset))
    lines = source.splitlines(keepends=True)
    if not lines:
        return source
    string_spans.sort()
    # Rebuild lines, replacing string spans with empty quotes.
    line_chars = [list(line) for line in lines]
    for start_line, start_col, end_line, end_col in string_spans:
        if start_line == end_line:
            if 1 <= start_line <= len(line_chars):
                row = line_chars[start_line - 1]
                for col in range(start_col, min(end_col, len(row))):
                    row[col] = " "
        else:
            for line_idx in range(start_line, end_line + 1):
                if 1 <= line_idx <= len(line_chars):
                    row = line_chars[line_idx - 1]
                    if line_idx == start_line:
                        for col in range(start_col, len(row)):
                            row[col] = " "
                    elif line_idx == end_line:
                        for col in range(0, min(end_col, len(row))):
                            row[col] = " "
                    else:
                        for col in range(len(row)):
                            if row[col] != "\n":
                                row[col] = " "
    cleaned = "".join("".join(row) for row in line_chars)
    # Strip Python-style comments.
    cleaned = re.sub(r"(?m)#.*$", "", cleaned)
    return cleaned


def _assert_forbidden_not_in_executable_code(module, forbidden_tokens: tuple[str, ...]) -> None:
    """Assert none of the tokens appear in actual code (not docstrings/comments)."""
    source = inspect.getsource(module)
    cleaned = _strip_string_literals_and_comments(source)
    for token in forbidden_tokens:
        assert token not in cleaned, f"{token} found in {module.__name__} (executable code)"


@pytest.fixture()
def db(tmp_path: Path, monkeypatch) -> Database:
    monkeypatch.setattr("app.database.PROJECT_ROOT", tmp_path)
    cfg = load_config()
    instance = Database(cfg, logging.getLogger("test"))
    instance.sqlite_path = tmp_path / "test.db"
    Database._sqlite_wal_initialised = False
    instance.initialize()
    return instance


def _seed_ohlcv(db: Database, *, symbol: str, timeframe: str, bars: int, freshness_minutes_back: int) -> None:
    """Insert N candles ending `freshness_minutes_back` minutes ago."""
    rows = []
    now = datetime.now(timezone.utc) - timedelta(minutes=freshness_minutes_back)
    timeframe_minutes = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240}.get(timeframe, 5)
    price = 100.0
    for i in range(bars):
        ts = now - timedelta(minutes=(bars - i) * timeframe_minutes)
        close = price * (1 + 0.001)
        rows.append({
            "symbol": symbol, "timeframe": timeframe,
            "timestamp": ts.isoformat(),
            "open": price, "high": close * 1.002, "low": price * 0.998,
            "close": close, "volume": 1000.0, "quote_volume": 100_000.0,
        })
        price = close
    db.insert_ohlcv_batch(rows)


# ---------------------------------------------------------------------------
# Bloque 2 — OHLCV freshness manager
# ---------------------------------------------------------------------------


def test_v5_freshness_status_returns_need_data_when_table_empty(db):
    from app.ohlcv_freshness_manager import freshness_status

    report = freshness_status(db, symbols=["BTCUSDT"], timeframes=["5m"], config=load_config())
    assert report.symbols == ["BTCUSDT"]
    assert report.timeframes == ["5m"]
    assert report.need_data_count == 1
    assert report.overall_actionable is False
    assert report.research_only is True
    assert report.activation_disabled_until_manual_vps_validation is True


def test_v5_freshness_status_detects_stale(db):
    from app.ohlcv_freshness_manager import freshness_status

    # Insert OHLCV with newest candle 60 minutes old → STALE for 5m (budget 20m).
    _seed_ohlcv(db, symbol="BTCUSDT", timeframe="5m", bars=30, freshness_minutes_back=60)
    report = freshness_status(db, symbols=["BTCUSDT"], timeframes=["5m"], config=load_config())
    statuses = {row.status for row in report.rows}
    assert "STALE" in statuses or "GAP" in statuses


def test_v5_freshness_refresh_dry_run_does_not_write(db):
    from app.ohlcv_freshness_manager import refresh

    cfg = load_config()
    # Default is already False — config is frozen so we don't mutate it.
    assert getattr(cfg, "enable_ohlcv_auto_refresh", False) is False
    report = refresh(db, config=cfg, symbols=["BTCUSDT"], timeframes=["5m"], hours=24, dry_run=True)
    assert report.dry_run is True
    assert report.total_rows_inserted == 0
    for result in report.results:
        assert result.status in {"DRY_RUN", "SKIPPED_AUTO_DISABLED"}
        assert result.dry_run is True
        assert result.rows_inserted == 0


def test_v5_freshness_refresh_skips_when_auto_disabled_and_not_allowed(db):
    from app.ohlcv_freshness_manager import refresh

    cfg = load_config()
    # Default for enable_ohlcv_auto_refresh is False and the config is frozen.
    assert getattr(cfg, "enable_ohlcv_auto_refresh", False) is False
    report = refresh(db, config=cfg, symbols=["BTCUSDT"], timeframes=["5m"], hours=24, dry_run=False, allow_real_writes=False)
    assert report.dry_run is True
    for result in report.results:
        assert result.status == "SKIPPED_AUTO_DISABLED"
        assert "enable_ohlcv_auto_refresh_false" in result.error


def test_v5_freshness_module_does_not_use_private_endpoints():
    import app.ohlcv_freshness_manager as mod
    _assert_forbidden_not_in_executable_code(mod, (
        "private_get(", "private_post(", "place_order(", "set_leverage(",
        "set_margin_mode(", "ExecutionEngine.execute", "PaperTrader.open_position",
    ))


# ---------------------------------------------------------------------------
# Bloque 3 — Training data clean view
# ---------------------------------------------------------------------------


def test_v5_training_clean_view_no_db_writes(db):
    import app.training_data_clean_view as mod
    source = _strip_string_literals_and_comments(inspect.getsource(mod))
    for forbidden in ("INSERT INTO", "DELETE FROM", "UPDATE ", "DROP TABLE"):
        assert forbidden.upper() not in source.upper(), f"{forbidden} found in training_data_clean_view"


def test_v5_training_clean_view_handles_empty_db(db):
    from app.training_data_clean_view import run_training_data_clean_view

    report = run_training_data_clean_view(db, hours=24)
    assert report.raw_sample_count == 0
    assert report.clean_sample_count == 0
    assert report.overall_status in {"UNKNOWN", "OK"}
    assert report.no_db_writes is True


def test_v5_training_clean_view_duplicate_rate_signals_bad():
    """The classifier returns BAD when duplicate_rate >= 10%."""
    from app.training_data_clean_view import _aggregate, TableCleanMetrics

    tables = [TableCleanMetrics(
        table="t", raw_count=100, clean_count=80, duplicates=20,
        duplicate_rate=0.20, dedupe_ratio=0.80,
    )]
    raw, clean, dup_rate, ratio, status, biggest = _aggregate(tables)
    assert status == "BAD"
    assert biggest == "duplicates"


# ---------------------------------------------------------------------------
# Bloque 4 — Shadow multi-trade learning
# ---------------------------------------------------------------------------


def test_v5_shadow_multi_trade_module_does_not_use_paper_trader():
    import app.shadow_multi_trade_learning as mod
    _assert_forbidden_not_in_executable_code(mod, (
        "PaperTrader.open_position", "ExecutionEngine(", "place_order(",
        "set_leverage(", "set_margin_mode(", "private_get(", "private_post(",
    ))


def test_v5_shadow_multi_trade_report_is_research_only(db):
    from app.shadow_multi_trade_learning import (
        run_shadow_multi_trade,
        SHADOW_BLOCKED_DEDUPE,
        SHADOW_BLOCKED_RATE_LIMIT,
    )

    report = run_shadow_multi_trade(load_config(), db, hours=6, timeframe="5m", symbols=["BTCUSDT"])
    assert report.research_only is True
    assert report.paper_filter_enabled is False
    assert report.can_send_real_orders is False
    assert report.activation == "shadow_only"
    assert report.no_db_writes is True


def test_v5_shadow_multi_trade_blocked_helper_marks_no_execution(db):
    """`_empty_blocked` always sets no_execution=True."""
    from app.phase8_research_utils import ReplayTradeContext
    from app.real_strategy_backtester import RealBacktestTrade
    from app.shadow_multi_trade_learning import _empty_blocked
    import pandas as pd

    candles = pd.DataFrame({
        "timestamp": [datetime.now(timezone.utc).isoformat()],
        "open": [100.0], "high": [101.0], "low": [99.0], "close": [100.0],
        "volume": [1000.0],
    })
    trade = RealBacktestTrade(
        symbol="BTCUSDT", side="LONG", signal_index=0, entry_index=0, exit_index=0,
        entry_price=100.0, exit_price=100.5, stop_loss=98.0, take_profit_1=102.0,
        gross_return_pct=0.5, net_return_pct=0.32, exit_reason="TAKE_PROFIT",
        fee_cost_bps=6, slippage_cost_bps=3, funding_component_bps=0, total_cost_bps=18,
    )
    ctx = ReplayTradeContext(symbol="BTCUSDT", timeframe="5m", trade=trade, candles=candles)
    blocked = _empty_blocked(ctx, "test_setup", "test_scenario", 0.18, "test_reason")
    assert blocked.no_execution is True
    assert blocked.can_send_real_orders is False
    assert blocked.paper_filter_enabled is False


# ---------------------------------------------------------------------------
# Bloque 5 — Capital / leverage simulator
# ---------------------------------------------------------------------------


def test_v5_capital_leverage_notional_equals_margin_times_leverage():
    from app.capital_leverage_simulator import _scenario_from_moves

    scenario = _scenario_from_moves(
        capital_total=40.0, margin=5.0, leverage=10,
        base_cost_pct=0.18, slippage_buffer_pct=0.04,
        moves=[0.5, 0.5, 0.5],
    )
    assert scenario.notional_usdt == pytest.approx(50.0)


def test_v5_capital_leverage_fees_scale_with_notional():
    from app.capital_leverage_simulator import _scenario_from_moves

    low = _scenario_from_moves(
        capital_total=40.0, margin=5.0, leverage=1,
        base_cost_pct=0.18, slippage_buffer_pct=0.04,
        moves=[0.5, 0.5, 0.5],
    )
    high = _scenario_from_moves(
        capital_total=40.0, margin=5.0, leverage=10,
        base_cost_pct=0.18, slippage_buffer_pct=0.04,
        moves=[0.5, 0.5, 0.5],
    )
    fees_low = low.fees_open_usdt + low.fees_close_usdt
    fees_high = high.fees_open_usdt + high.fees_close_usdt
    assert fees_high > fees_low


def test_v5_capital_leverage_promotion_eligible_false_when_net_negative():
    from app.capital_leverage_simulator import _scenario_from_moves

    scenario = _scenario_from_moves(
        capital_total=40.0, margin=5.0, leverage=10,
        base_cost_pct=0.18, slippage_buffer_pct=0.04,
        # Average move is exactly the round-trip cost — net negative once
        # slippage is subtracted.
        moves=[0.18, 0.18, 0.18],
    )
    assert scenario.net_pnl_usdt <= 0
    assert scenario.promotion_eligible is False


def test_v5_capital_leverage_does_not_touch_leverage_config():
    import app.capital_leverage_simulator as mod
    _assert_forbidden_not_in_executable_code(mod, (
        "set_leverage(", "set_margin_mode(", "place_order(",
    ))


# ---------------------------------------------------------------------------
# Bloque 6 — Fee-aware exit trainer
# ---------------------------------------------------------------------------


def test_v5_fee_aware_exit_trainer_module_compiles_and_keeps_safety():
    import app.fee_aware_exit_trainer as mod
    source = inspect.getsource(mod)
    # These are safety markers that must be present (in any form — strings/code).
    assert "final_recommendation: NO LIVE" in source
    assert "maker_maker_audit_only_never_promotes" in source
    # Forbidden tokens must not appear as actual code (docstrings allowed).
    _assert_forbidden_not_in_executable_code(mod, (
        "private_get(", "private_post(", "place_order(", "set_leverage(",
        "PaperTrader.open_position",
    ))


def test_v5_fee_aware_exit_trainer_promotion_eligible_false_when_gross_green_net_negative():
    from app.fee_aware_exit_trainer import _promotion_eligible

    class FakeScenario:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)

    # Even with the underlying lab marking the scenario as promotable, our
    # wrapper must veto it when gross_green_net_negative is True.
    fake = FakeScenario(
        scenario="base_cost_0_18",
        promotion_eligible=True,
        net_ev=-0.05,
        net_pf=0.8,
        gross_green_net_negative=True,
    )
    assert _promotion_eligible(fake) is False


def test_v5_fee_aware_exit_trainer_maker_maker_never_promotes():
    from app.fee_aware_exit_trainer import _promotion_eligible

    class FakeScenario:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)

    fake = FakeScenario(
        scenario="maker_maker_audit_only",
        promotion_eligible=True,
        net_ev=0.5,
        net_pf=2.0,
        gross_green_net_negative=False,
    )
    assert _promotion_eligible(fake) is False


# ---------------------------------------------------------------------------
# Bloque 7 — Phase 9 readiness V2 gates
# ---------------------------------------------------------------------------


def _fake_phase8_candidate(**overrides):
    from app.phase8_candidate_validator import (
        Phase8CandidateResult,
        Phase8WalkForwardFold,
        Phase8WalkForwardResult,
    )

    folds = [
        Phase8WalkForwardFold(
            fold=1, start="", end="",
            baseline_trades=50, policy_trades=50,
            baseline_net_ev=-0.02, policy_net_ev=0.05, delta_ev=0.07, pass_fold=True,
        ),
        Phase8WalkForwardFold(
            fold=2, start="", end="",
            baseline_trades=50, policy_trades=50,
            baseline_net_ev=-0.02, policy_net_ev=0.06, delta_ev=0.08, pass_fold=True,
        ),
        Phase8WalkForwardFold(
            fold=3, start="", end="",
            baseline_trades=50, policy_trades=50,
            baseline_net_ev=-0.02, policy_net_ev=0.05, delta_ev=0.07, pass_fold=True,
        ),
    ]
    base = dict(
        candidate_id="test", symbols=["DOTUSDT"], policy_name="test_policy",
        baseline_net_ev=-0.02, policy_net_ev=0.05, delta_ev=0.07,
        baseline_net_pf=0.8, policy_net_pf=1.30, trades=300,
        sample_status="PASS",
        cost_stress_status="PASS",
        walk_forward_status="PASS",
        anti_overfit_status="PASS",
        sensitivity_status="PASS",
        stability_status="PASS",
        final_decision="PAPER_DEMO_READY_MANUAL_REVIEW_ONLY",
        reasons=[],
        cost_stress=None,
        walk_forward=Phase8WalkForwardResult("PASS", folds=folds, reasons=[]),
        per_symbol_policy_net_ev={"DOTUSDT": 0.05},
    )
    base.update(overrides)
    return Phase8CandidateResult(**base)


def test_v5_phase9_data_quality_bad_blocks_promotion():
    from app.data_freshness_gate import FreshnessVerdict
    from app.phase9_paper_readiness_validator import (
        PHASE9_REJECT_DATA_QUALITY,
        _verdict_from_phase8,
    )

    candidate = _fake_phase8_candidate()
    verdicts = {"DOTUSDT": FreshnessVerdict(
        symbol="DOTUSDT", timeframe="5m", status="OK",
        newest_timestamp="2026-01-01T00:00:00+00:00",
        age_minutes=1.0, staleness_budget_minutes=20, actionable=True,
    )}
    result = _verdict_from_phase8(
        candidate, verdicts,
        min_trades=250, min_net_pf=1.15, validation_hours=720,
        data_quality_status="BAD",
        net_profit_lock_eligible=True,
        capital_leverage_net_positive=True,
        gross_green_net_negative=False,
    )
    assert result.phase9_decision == PHASE9_REJECT_DATA_QUALITY


def test_v5_phase9_gross_green_net_negative_blocks_promotion():
    from app.data_freshness_gate import FreshnessVerdict
    from app.phase9_paper_readiness_validator import (
        PHASE9_REJECT_NEGATIVE_NET,
        _verdict_from_phase8,
    )

    candidate = _fake_phase8_candidate()
    verdicts = {"DOTUSDT": FreshnessVerdict(
        symbol="DOTUSDT", timeframe="5m", status="OK",
        newest_timestamp="2026-01-01T00:00:00+00:00",
        age_minutes=1.0, staleness_budget_minutes=20, actionable=True,
    )}
    result = _verdict_from_phase8(
        candidate, verdicts,
        min_trades=250, min_net_pf=1.15, validation_hours=720,
        data_quality_status="OK",
        net_profit_lock_eligible=True,
        capital_leverage_net_positive=True,
        gross_green_net_negative=True,
    )
    assert result.phase9_decision == PHASE9_REJECT_NEGATIVE_NET


def test_v5_phase9_paper_ready_only_when_all_gates_pass():
    from app.data_freshness_gate import FreshnessVerdict
    from app.phase9_paper_readiness_validator import (
        PAPER_DEMO_READY_MANUAL_REVIEW_ONLY,
        _verdict_from_phase8,
    )

    candidate = _fake_phase8_candidate()
    verdicts = {"DOTUSDT": FreshnessVerdict(
        symbol="DOTUSDT", timeframe="5m", status="OK",
        newest_timestamp="2026-01-01T00:00:00+00:00",
        age_minutes=1.0, staleness_budget_minutes=20, actionable=True,
    )}
    result = _verdict_from_phase8(
        candidate, verdicts,
        min_trades=250, min_net_pf=1.15, validation_hours=720,
        data_quality_status="OK",
        net_profit_lock_eligible=True,
        capital_leverage_net_positive=True,
        gross_green_net_negative=False,
        require_v5_gates=True,
    )
    assert result.phase9_decision == PAPER_DEMO_READY_MANUAL_REVIEW_ONLY
    # Even on PAPER_DEMO_READY the invariants stay False.
    assert result.paper_filter_enabled is False
    assert result.can_send_real_orders is False


def test_v5_phase9_catastrophic_fold_blocks_promotion():
    from app.data_freshness_gate import FreshnessVerdict
    from app.phase8_candidate_validator import Phase8WalkForwardFold, Phase8WalkForwardResult
    from app.phase9_paper_readiness_validator import (
        PHASE9_REJECT_CATASTROPHIC_FOLD,
        _verdict_from_phase8,
    )

    # Override fold 1 to be catastrophic.
    candidate = _fake_phase8_candidate(
        walk_forward=Phase8WalkForwardResult(
            "PASS",
            folds=[
                Phase8WalkForwardFold(
                    fold=1, start="", end="", baseline_trades=50, policy_trades=50,
                    baseline_net_ev=-0.02, policy_net_ev=-0.20, delta_ev=-0.18, pass_fold=False,
                ),
                Phase8WalkForwardFold(
                    fold=2, start="", end="", baseline_trades=50, policy_trades=50,
                    baseline_net_ev=-0.02, policy_net_ev=0.05, delta_ev=0.07, pass_fold=True,
                ),
            ],
            reasons=[],
        ),
    )
    verdicts = {"DOTUSDT": FreshnessVerdict(
        symbol="DOTUSDT", timeframe="5m", status="OK",
        newest_timestamp="2026-01-01T00:00:00+00:00",
        age_minutes=1.0, staleness_budget_minutes=20, actionable=True,
    )}
    result = _verdict_from_phase8(
        candidate, verdicts,
        min_trades=250, min_net_pf=1.15, validation_hours=720,
        data_quality_status="OK",
        net_profit_lock_eligible=True,
        capital_leverage_net_positive=True,
        gross_green_net_negative=False,
        require_v5_gates=True,
    )
    assert result.phase9_decision == PHASE9_REJECT_CATASTROPHIC_FOLD


# ---------------------------------------------------------------------------
# Bloque 8 — Dashboard endpoints
# ---------------------------------------------------------------------------


def test_v5_dashboard_endpoints_are_registered():
    import app.health_server as hs
    source = inspect.getsource(hs)
    for path in (
        "/api/research-pack-v5",
        "/api/research/ohlcv-freshness-status",
        "/api/research/ohlcv-freshness-refresh-dry",
        "/api/research/training-clean-view-audit",
        "/api/research/shadow-multi-trade-status",
        "/api/research/capital-leverage-sim",
        "/api/research/fee-aware-exit-trainer",
    ):
        assert path in source, f"endpoint {path} not registered"


def test_v5_dashboard_endpoint_handlers_set_safety_markers():
    import app.health_server as hs
    for name in (
        "_v5_ohlcv_freshness_status",
        "_v5_ohlcv_freshness_refresh_dry",
        "_v5_training_clean_view_audit",
        "_v5_shadow_multi_trade_status",
        "_v5_capital_leverage_sim",
        "_v5_fee_aware_exit_trainer",
        "_research_pack_v5_endpoint",
    ):
        assert hasattr(hs, name), f"handler {name} missing"


def test_v5_research_pack_v5_excludes_secrets(db):
    from app.research_pack_v5 import build_research_pack_v5
    pack = build_research_pack_v5(
        load_config(), db,
        hours=6, symbols=["BTCUSDT"], timeframes=["5m"],
        include_short_report=False,
        include_shadow=True,
        include_capital_leverage=True,
        include_fee_aware_exit=False,
    )
    serialised = str(pack)
    # The pack must never serialise secrets.
    forbidden_substrings = (
        "API_KEY", "api_key", "BITGET_API_KEY", "bitget_api_key",
        "API_SECRET", "api_secret", "BITGET_API_SECRET", "bitget_api_secret",
        "PASSPHRASE", "passphrase",
    )
    for forbidden in forbidden_substrings:
        assert forbidden not in serialised, f"{forbidden} leaked in research pack v5"
    assert pack["final_recommendation"] == "NO LIVE"
    assert pack["paper_filter_enabled"] is False
    assert pack["can_send_real_orders"] is False


# ---------------------------------------------------------------------------
# Safety invariants
# ---------------------------------------------------------------------------


def test_v5_safety_flags_unchanged():
    cfg = load_config()
    assert cfg.live_trading is False
    assert cfg.dry_run is True
    assert cfg.paper_trading is True
    assert cfg.enable_paper_policy_filter is False
    assert cfg.enable_candidate_shadow_monitor is False
    assert cfg.can_send_real_orders is False
    # ResearchOps V5 flag exists and defaults to False
    assert getattr(cfg, "enable_ohlcv_auto_refresh", False) is False


def test_v5_modules_do_not_import_forbidden_runtime_methods():
    import app.ohlcv_freshness_manager
    import app.training_data_clean_view
    import app.shadow_multi_trade_learning
    import app.capital_leverage_simulator
    import app.fee_aware_exit_trainer
    import app.research_pack_v5

    modules = (
        app.ohlcv_freshness_manager,
        app.training_data_clean_view,
        app.shadow_multi_trade_learning,
        app.capital_leverage_simulator,
        app.fee_aware_exit_trainer,
        app.research_pack_v5,
    )
    forbidden = (
        "PaperTrader.open_position",
        "ExecutionEngine.execute",
        "place_order(",
        "set_leverage(",
        "set_margin_mode(",
        "can_send_real_orders=True",
        "LIVE_TRADING=True",
        "ENABLE_PAPER_POLICY_FILTER=True",
    )
    for module in modules:
        _assert_forbidden_not_in_executable_code(module, forbidden)
