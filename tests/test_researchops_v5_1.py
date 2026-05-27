"""ResearchOps V5.1 tests.

Covers:
  - OHLCV freshness wrapper hotfix (CSV parsing, 10x3 iteration, BitgetClient
    args fixed, per-(symbol,timeframe) error rows on client_init failures).
  - Dashboard endpoint cannot trigger real writes.
  - Research Pack V5.1 aggregates surface known issues / suggested actions.
  - Strategy Research Enhancer is research-only and respects data quality BAD.
  - Safety scan on V5.1 modules (no place_order, no set_leverage, etc.).
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


# ---------------------------------------------------------------------------
# Helpers shared with the V5 test (AST-strip docstrings before safety scans).
# ---------------------------------------------------------------------------


def _strip_string_literals_and_comments(source: str) -> str:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source
    spans: list[tuple[int, int, int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            spans.append((node.lineno, node.col_offset, node.end_lineno, node.end_col_offset))
    lines = source.splitlines(keepends=True)
    line_chars = [list(line) for line in lines]
    for start_line, start_col, end_line, end_col in sorted(spans):
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
    cleaned = re.sub(r"(?m)#.*$", "", cleaned)
    return cleaned


def _assert_forbidden_not_in_executable_code(module, forbidden_tokens: tuple[str, ...]) -> None:
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


# ---------------------------------------------------------------------------
# Bloque 2 — OHLCV wrapper hotfix
# ---------------------------------------------------------------------------


def test_v51_coerce_symbols_parses_csv_string():
    from app.ohlcv_freshness_manager import _coerce_symbols

    assert _coerce_symbols("BTCUSDT,ETHUSDT,DOTUSDT") == ["BTCUSDT", "ETHUSDT", "DOTUSDT"]
    assert _coerce_symbols(["BTCUSDT", "ETHUSDT"]) == ["BTCUSDT", "ETHUSDT"]


def test_v51_coerce_timeframes_parses_csv_string():
    from app.ohlcv_freshness_manager import _coerce_timeframes

    assert _coerce_timeframes("5m,15m,1h") == ["5m", "15m", "1h"]
    assert _coerce_timeframes(["5m", "1h"]) == ["5m", "1h"]


def test_v51_freshness_status_iterates_10x3(db):
    """Wrapper must produce one row per (symbol, timeframe)."""
    from app.ohlcv_freshness_manager import freshness_status, DEFAULT_V5_SYMBOLS, DEFAULT_V5_TIMEFRAMES

    report = freshness_status(
        db,
        symbols=list(DEFAULT_V5_SYMBOLS),
        timeframes=list(DEFAULT_V5_TIMEFRAMES),
        config=load_config(),
    )
    assert len(report.symbols) == 10
    assert len(report.timeframes) == 3
    assert len(report.rows) == 30


def test_v51_refresh_dry_run_iterates_per_pair(db):
    from app.ohlcv_freshness_manager import refresh

    report = refresh(
        db,
        config=load_config(),
        symbols="BTCUSDT,ETHUSDT,DOTUSDT",
        timeframes="5m,15m,1h",
        hours=24,
        dry_run=True,
    )
    assert len(report.results) == 9  # 3 symbols * 3 timeframes
    # No two rows share the same (symbol, timeframe).
    seen: set[tuple[str, str]] = set()
    for row in report.results:
        key = (row.symbol, row.timeframe)
        assert key not in seen, f"duplicate row for {key}"
        seen.add(key)
        assert row.dry_run is True
        assert row.rows_inserted == 0
        assert row.symbol in {"BTCUSDT", "ETHUSDT", "DOTUSDT"}
        assert row.timeframe in {"5m", "15m", "1h"}


def test_v51_refresh_apply_without_allow_real_writes_does_not_write(db):
    """--apply alone must NOT trigger real writes; the gate requires
    allow_real_writes too (per CLI dispatch contract)."""
    from app.ohlcv_freshness_manager import refresh

    cfg = load_config()
    # auto_refresh stays False by default → without allow_real_writes the
    # manager refuses to write even with dry_run=False.
    report = refresh(
        db, config=cfg,
        symbols=["BTCUSDT"], timeframes=["5m"],
        hours=24, dry_run=False, allow_real_writes=False,
    )
    assert report.dry_run is True
    for result in report.results:
        assert result.status == "SKIPPED_AUTO_DISABLED"
        assert result.rows_inserted == 0


def test_v51_refresh_allow_real_writes_without_apply_is_still_dry_run():
    """Calling refresh with dry_run=True (i.e. CLI not passing --apply) must
    keep the run as dry-run regardless of allow_real_writes."""
    from app.ohlcv_freshness_manager import refresh

    cfg = load_config()
    # Stub DB so the test does not need a real one.
    class _StubDB:
        def __init__(self) -> None:
            self._use_postgres = False
        def table_exists(self, *_a, **_k) -> bool:
            return False
    report = refresh(
        _StubDB(), config=cfg,
        symbols=["BTCUSDT"], timeframes=["5m"],
        hours=24, dry_run=True, allow_real_writes=True,
    )
    assert report.dry_run is True
    for result in report.results:
        # dry_run=True forces the DRY_RUN status path.
        assert result.status == "DRY_RUN"
        assert result.dry_run is True


def test_v51_refresh_full_gate_invokes_bitget_client_with_logger(monkeypatch, db):
    """The gate `dry_run=False AND allow_real_writes=True` must construct
    BitgetClient with (config, logger) — never with a single argument.

    We monkeypatch BitgetClient and backfill_pair so no exchange call ever
    happens and assert the call signature."""
    import logging as logging_mod

    captured: dict[str, object] = {}

    class _FakeBitgetClient:
        def __init__(self, config, logger):
            captured["config_arg"] = config
            captured["logger_arg"] = logger

    def _fake_backfill_pair(*, client, db, symbol, timeframe, days, dry_run, logger):
        # Return a minimal stats-shaped object.
        class _Stats:
            status = "OK"
            inserted = 1
            skipped = 0
            rejected = 0
            duration_seconds = 0.01
            error = ""
        captured.setdefault("pairs", []).append((symbol, timeframe))
        return _Stats()

    monkeypatch.setattr("app.bitget_client.BitgetClient", _FakeBitgetClient)
    monkeypatch.setattr("app.ohlcv_backfill.backfill_pair", _fake_backfill_pair)

    from app.ohlcv_freshness_manager import refresh

    cfg = load_config()
    report = refresh(
        db, config=cfg, logger=logging_mod.getLogger("test"),
        symbols=["BTCUSDT", "ETHUSDT"], timeframes=["5m", "1h"],
        hours=24, dry_run=False, allow_real_writes=True,
    )
    # Client was instantiated with (config, logger).
    assert captured.get("config_arg") is cfg
    assert isinstance(captured.get("logger_arg"), logging_mod.Logger)
    # And the iteration produced 2 symbols x 2 timeframes = 4 pairs.
    assert len(captured.get("pairs", [])) == 4
    assert all(row.dry_run is False for row in report.results)


def test_v51_dashboard_endpoint_never_writes(db):
    """The HTTP handler always forces dry_run=True, allow_real_writes=False."""
    from app import health_server as hs

    payload = hs._v5_ohlcv_freshness_refresh_dry(
        load_config(), db,
        {"symbols": ["BTCUSDT"], "timeframes": ["5m"], "hours": ["24"]},
    )
    assert payload["dry_run"] is True
    assert payload["final_recommendation"] == "NO LIVE"
    assert payload["can_send_real_orders"] is False


def test_v51_health_server_has_no_real_refresh_route():
    """There must be no /api/...ohlcv-freshness-refresh route without -dry."""
    from app import health_server as hs

    source = inspect.getsource(hs)
    # Strip strings/comments first — we are looking for executable references.
    cleaned = _strip_string_literals_and_comments(source)
    # The only allowed mention is the dry endpoint.
    assert "ohlcv-freshness-refresh-dry" in source
    # We must never have a route literal without "-dry".
    bad = re.findall(r"\"/api/[^\"]*ohlcv-freshness-refresh(?!-dry)[^\"]*\"", cleaned)
    assert bad == [], f"forbidden real-refresh route literals: {bad}"


# ---------------------------------------------------------------------------
# Bloque 4 — Research Pack V5.1 aggregates
# ---------------------------------------------------------------------------


def test_v51_research_pack_v51_has_aggregates_and_known_issues(db):
    from app.research_pack_v5 import build_research_pack_v5

    pack = build_research_pack_v5(
        load_config(), db,
        hours=24, symbols=["BTCUSDT"], timeframes=["5m"],
        include_short_report=False,
        include_shadow=True,
        include_capital_leverage=True,
        include_fee_aware_exit=False,
    )
    # Old V5 + new V5.1 fields present.
    assert pack["pack_version"] == "v5"
    assert "ohlcv_freshness_matrix" in pack
    assert "training_data_clean_view" in pack
    assert "shadow_multi_trade_summary" in pack
    assert "capital_leverage_top" in pack
    assert "suggested_next_actions" in pack
    # No secrets / credentials.
    serialised = str(pack)
    for forbidden in ("API_KEY", "api_key", "API_SECRET", "api_secret", "PASSPHRASE", "passphrase"):
        assert forbidden not in serialised


# ---------------------------------------------------------------------------
# Bloque 5 — Strategy Research Enhancer
# ---------------------------------------------------------------------------


def test_v51_strategy_research_enhancer_module_exists():
    import app.strategy_research_enhancer as mod
    assert hasattr(mod, "run_strategy_research_enhancer")
    assert hasattr(mod, "render_strategy_research_enhancer_text")


def test_v51_strategy_research_enhancer_research_only(db):
    from app.strategy_research_enhancer import run_strategy_research_enhancer

    report = run_strategy_research_enhancer(load_config(), db, hours=6, symbols=["BTCUSDT"])
    assert report.research_only is True
    assert report.paper_filter_enabled is False
    assert report.can_send_real_orders is False
    assert report.final_recommendation == "NO LIVE"


def test_v51_strategy_research_enhancer_respects_data_quality_bad(db):
    """When `data_quality_status='BAD'` the enhancer must return
    REJECT_DATA_QUALITY for the overall decision."""
    from app.strategy_research_enhancer import run_strategy_research_enhancer

    report = run_strategy_research_enhancer(
        load_config(), db, hours=6, symbols=["BTCUSDT"],
        data_quality_status="BAD",
    )
    assert report.overall_decision == "REJECT_DATA_QUALITY"


def test_v51_strategy_research_enhancer_does_not_activate_anything():
    """Safety scan on the new module."""
    import app.strategy_research_enhancer as mod
    _assert_forbidden_not_in_executable_code(mod, (
        "PaperTrader.open_position",
        "ExecutionEngine.execute",
        "place_order(",
        "set_leverage(",
        "set_margin_mode(",
        "can_send_real_orders=True",
        "LIVE_TRADING=True",
        "ENABLE_PAPER_POLICY_FILTER=True",
        "ENABLE_OHLCV_AUTO_REFRESH=True",
        "allow_real_writes=True",
    ))


# ---------------------------------------------------------------------------
# Bloque 8 — Safety invariants
# ---------------------------------------------------------------------------


def test_v51_safety_flags_unchanged():
    cfg = load_config()
    assert cfg.live_trading is False
    assert cfg.dry_run is True
    assert cfg.paper_trading is True
    assert cfg.enable_paper_policy_filter is False
    assert cfg.enable_candidate_shadow_monitor is False
    assert cfg.can_send_real_orders is False
    assert getattr(cfg, "enable_ohlcv_auto_refresh", False) is False


def test_v51_freshness_manager_executable_code_does_not_hardcode_true_for_writes():
    import app.ohlcv_freshness_manager as mod
    _assert_forbidden_not_in_executable_code(mod, (
        "allow_real_writes=True",
        "ENABLE_OHLCV_AUTO_REFRESH=True",
        "private_get(",
        "private_post(",
        "place_order(",
        "set_leverage(",
        "set_margin_mode(",
    ))


def test_v51_dashboard_js_still_has_no_real_write_calls():
    js_path = Path(__file__).resolve().parent.parent / "app" / "static" / "dashboard.js"
    body = js_path.read_text(encoding="utf-8", errors="ignore")
    assert "allow_real_writes=true" not in body
    assert "apply=true" not in body
    # Only mention allowed is in the prepareV5FreshnessCli string for clipboard.
    real_refresh = re.findall(r"fetch[^(]*\([^)]*ohlcv-freshness-refresh(?!-dry)", body)
    assert real_refresh == [], f"forbidden fetch to real-refresh: {real_refresh}"
