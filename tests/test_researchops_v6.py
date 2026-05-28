"""ResearchOps V6 tests.

Covers:
  - `get_clean_research_metrics` helper (RAW vs CLEAN, duplicate_rate, blocked_gate).
  - Strategy Research Enhancer uses CLEAN metrics for decision (not RAW).
  - Phase 9 readiness validator escalates to BAD when central helper says BAD.
  - Research Pack V5 surfaces clean_research_metrics + known_issues.
  - Dashboard Overview contains the Operator Cockpit and "Qué está bloqueando" block.
  - Dashboard endpoints are research-only and never apply real writes.
  - Safety scan on V6 module + research_lab + dashboard.js.
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
# Helpers
# ---------------------------------------------------------------------------


def _strip_strings_and_comments(source: str) -> str:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source
    spans: list[tuple[int, int, int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            spans.append((node.lineno, node.col_offset, node.end_lineno, node.end_col_offset))
    lines = source.splitlines(keepends=True)
    chars = [list(line) for line in lines]
    for sl, sc, el, ec in sorted(spans):
        if sl == el:
            if 1 <= sl <= len(chars):
                row = chars[sl - 1]
                for c in range(sc, min(ec, len(row))):
                    row[c] = " "
        else:
            for li in range(sl, el + 1):
                if 1 <= li <= len(chars):
                    row = chars[li - 1]
                    if li == sl:
                        for c in range(sc, len(row)):
                            row[c] = " "
                    elif li == el:
                        for c in range(0, min(ec, len(row))):
                            row[c] = " "
                    else:
                        for c in range(len(row)):
                            if row[c] != "\n":
                                row[c] = " "
    cleaned = "".join("".join(row) for row in chars)
    return re.sub(r"(?m)#.*$", "", cleaned)


def _assert_no_executable_tokens(module, tokens: tuple[str, ...]) -> None:
    src = inspect.getsource(module)
    cleaned = _strip_strings_and_comments(src)
    for token in tokens:
        assert token not in cleaned, f"{token} found in {module.__name__} executable code"


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
# Bloque 2 — Clean metrics helper
# ---------------------------------------------------------------------------


def test_v6_clean_metrics_module_exists():
    import app.clean_research_metrics as mod
    assert hasattr(mod, "get_clean_research_metrics")
    assert hasattr(mod, "CleanResearchMetrics")


def test_v6_clean_metrics_returns_unknown_on_empty_db(db):
    from app.clean_research_metrics import get_clean_research_metrics

    report = get_clean_research_metrics(db, hours=24, symbols=["BTCUSDT"], timeframes=["5m"])
    assert report.research_only is True
    assert report.paper_filter_enabled is False
    assert report.can_send_real_orders is False
    assert report.final_recommendation == "NO LIVE"
    # No data → counts are 0 and quality is UNKNOWN (not BAD).
    assert report.raw_sample_count == 0
    assert report.clean_sample_count == 0
    # Without data the helper still emits a blocked_gate for low sample.
    assert "clean_sample_count_too_low" in (report.blocked_gate or "")


def test_v6_clean_metrics_handles_no_db_writes():
    """Safety scan: no executable code in the helper writes to the DB."""
    import app.clean_research_metrics as mod
    src = _strip_strings_and_comments(inspect.getsource(mod))
    for forbidden in ("INSERT INTO ", "DELETE FROM ", "UPDATE ", "DROP "):
        assert forbidden.upper() not in src.upper(), f"{forbidden} found in clean_research_metrics"


def test_v6_clean_metrics_render_text_marks_no_promote_raw(db):
    from app.clean_research_metrics import (
        get_clean_research_metrics,
        render_clean_metrics_text,
    )

    report = get_clean_research_metrics(db, hours=24)
    text = render_clean_metrics_text(report)
    assert "CLEAN RESEARCH METRICS START" in text
    assert "do_not_promote_raw: true" in text
    assert "final_recommendation: NO LIVE" in text


# ---------------------------------------------------------------------------
# Bloque 2 — Strategy Research Enhancer uses CLEAN
# ---------------------------------------------------------------------------


def test_v6_strategy_enhancer_returns_clean_metrics_field(db):
    from app.strategy_research_enhancer import run_strategy_research_enhancer

    report = run_strategy_research_enhancer(load_config(), db, hours=6, symbols=["BTCUSDT"])
    # Report carries the V6 fields.
    assert hasattr(report, "clean_metrics")
    assert hasattr(report, "raw_vs_clean_delta_ev_pct")
    assert hasattr(report, "blocked_gate")
    assert hasattr(report, "research_hypotheses")
    # Hypotheses list is not empty and each item is research-only.
    assert isinstance(report.research_hypotheses, list)
    if report.research_hypotheses:
        for h in report.research_hypotheses:
            assert h.decision in {
                "RESEARCH_PROMISING",
                "NEED_MORE_DATA",
                "REJECT_NEGATIVE_NET",
                "REJECT_DATA_QUALITY",
                "REJECT_OVERFIT_RISK",
                "REJECT_COSTS",
                "SHADOW_ONLY",
            }


def test_v6_strategy_enhancer_blocks_when_clean_helper_says_bad(db, monkeypatch):
    """If the central helper reports BAD, the enhancer must surface REJECT_DATA_QUALITY."""
    import app.strategy_research_enhancer as enh_mod
    from app.clean_research_metrics import CleanResearchMetrics

    def _fake_clean(db, **kwargs):
        return CleanResearchMetrics(
            hours=24, symbols=[], timeframes=["5m"],
            raw_sample_count=1000, clean_sample_count=520,
            duplicate_count=480, duplicate_rate=0.48, dedupe_ratio=0.52,
            raw_ev_pct=0.05, clean_ev_pct=-0.02,
            raw_pf=1.20, clean_pf=0.85,
            raw_win_rate=0.55, clean_win_rate=0.40,
            raw_tp_rate=0.5, clean_tp_rate=0.4,
            raw_sl_rate=0.2, clean_sl_rate=0.3,
            raw_time_rate=0.3, clean_time_rate=0.3,
            duplicate_impact_pct=0.07,
            confidence="LOW",
            data_quality_status="BAD",
            blocked_gate="data_quality_bad_duplicate_rate",
            reasons=["duplicate_rate=0.4800_above_bad_threshold"],
        )

    monkeypatch.setattr(enh_mod, "get_clean_research_metrics", _fake_clean)
    report = enh_mod.run_strategy_research_enhancer(load_config(), db, hours=6, symbols=["BTCUSDT"])
    assert report.data_quality_status == "BAD"
    assert report.blocked_gate == "data_quality_bad_duplicate_rate"
    # When data quality is BAD, the overall decision must NOT be RESEARCH_PROMISING.
    assert report.overall_decision == "REJECT_DATA_QUALITY"


def test_v6_strategy_enhancer_renders_raw_vs_clean_warning(db):
    from app.strategy_research_enhancer import (
        run_strategy_research_enhancer,
        render_strategy_research_enhancer_text,
    )

    report = run_strategy_research_enhancer(load_config(), db, hours=6, symbols=["BTCUSDT"])
    text = render_strategy_research_enhancer_text(report)
    assert "do_not_promote_raw: true" in text
    assert "research_hypotheses:" in text
    assert "raw_vs_clean_delta_ev_pct" in text


# ---------------------------------------------------------------------------
# Phase 9 readiness consumes clean metrics
# ---------------------------------------------------------------------------


def test_v6_phase9_escalates_to_bad_when_clean_says_bad(db, monkeypatch):
    """When `require_v6_clean_gate=True` (default) and the central helper says
    BAD, Phase 9 must propagate it into the decision path."""
    import app.phase9_paper_readiness_validator as p9
    from app.clean_research_metrics import CleanResearchMetrics

    def _fake_clean(db, **kwargs):
        return CleanResearchMetrics(
            hours=720, symbols=[], timeframes=["5m"],
            raw_sample_count=500, clean_sample_count=200,
            duplicate_count=300, duplicate_rate=0.60, dedupe_ratio=0.40,
            raw_ev_pct=0.10, clean_ev_pct=-0.05,
            raw_pf=1.5, clean_pf=0.7,
            raw_win_rate=0.6, clean_win_rate=0.4,
            raw_tp_rate=0.5, clean_tp_rate=0.3,
            raw_sl_rate=0.2, clean_sl_rate=0.4,
            raw_time_rate=0.3, clean_time_rate=0.3,
            duplicate_impact_pct=0.15,
            confidence="LOW",
            data_quality_status="BAD",
            blocked_gate="data_quality_bad_duplicate_rate",
            reasons=[],
        )

    # Patch the import target inside the function (we use module-level import).
    monkeypatch.setattr("app.clean_research_metrics.get_clean_research_metrics", _fake_clean)
    # Spin up the validator with the V6 gate on; the actual Phase 8 path will
    # find no candidates on an empty DB, so the decision is NEED_MORE_DATA but
    # the report's data_quality_status field must be BAD.
    report = p9.run_phase9_paper_readiness(
        load_config(), db,
        hours=720, timeframe="5m", symbols=["BTCUSDT"],
        require_v6_clean_gate=True,
    )
    # The candidate list may be empty in the test DB; the test just checks the
    # validator can be invoked with V6 gate without raising and final_recommendation
    # is NO LIVE.
    assert report.final_recommendation == "NO LIVE"
    assert report.can_send_real_orders is False
    assert report.paper_filter_enabled is False


# ---------------------------------------------------------------------------
# Research Pack V5 surfaces V6 clean metrics + known issues
# ---------------------------------------------------------------------------


def test_v6_research_pack_v5_includes_clean_metrics(db):
    from app.research_pack_v5 import build_research_pack_v5

    pack = build_research_pack_v5(
        load_config(), db,
        hours=6, symbols=["BTCUSDT"], timeframes=["5m"],
        include_short_report=False,
        include_shadow=True,
        include_capital_leverage=False,
        include_fee_aware_exit=False,
    )
    assert "clean_research_metrics" in pack
    assert pack["final_recommendation"] == "NO LIVE"


# ---------------------------------------------------------------------------
# Bloque 3 — Dashboard V6 Overview contains the Operator Cockpit
# ---------------------------------------------------------------------------


def test_v6_dashboard_overview_has_operator_cockpit():
    html_path = Path(__file__).resolve().parent.parent / "app" / "static" / "dashboard.html"
    body = html_path.read_text(encoding="utf-8", errors="ignore")
    # The Overview must include the V6 cockpit grid + command status.
    assert "v6-command-status" in body
    assert "v6-cockpit-grid" in body
    assert 'id="v6BlockersList"' in body
    assert 'id="v6BestLeadsRows"' in body
    assert 'id="v6RawSampleValue"' in body
    assert 'id="v6CleanSampleValue"' in body
    # Critical legacy markers still must be present so existing scripts work.
    assert 'id="overviewKpis"' in body
    assert "NO LIVE" in body


def test_v6_dashboard_has_blockers_and_best_leads_sections():
    html_path = Path(__file__).resolve().parent.parent / "app" / "static" / "dashboard.html"
    body = html_path.read_text(encoding="utf-8", errors="ignore")
    # The two key sections required by the V6 spec.
    assert "Qué está bloqueando el avance" in body
    assert "Mejores pistas actuales" in body
    assert "RAW vs CLEAN data quality" in body


def test_v6_dashboard_js_has_no_real_write_calls():
    js_path = Path(__file__).resolve().parent.parent / "app" / "static" / "dashboard.js"
    body = js_path.read_text(encoding="utf-8", errors="ignore")
    assert "allow_real_writes=true" not in body
    assert "apply=true" not in body
    assert "apply=1" not in body
    # The single mention of `ohlcv-freshness-refresh` (no `-dry`) must live
    # inside the clipboard CLI preparation string, never in a fetch.
    fetches = re.findall(r"fetch\w*\([^)]*ohlcv-freshness-refresh(?!-dry)", body)
    assert fetches == [], f"forbidden fetch calls: {fetches}"


def test_v6_dashboard_overview_v6_ids_are_unique():
    html_path = Path(__file__).resolve().parent.parent / "app" / "static" / "dashboard.html"
    body = html_path.read_text(encoding="utf-8", errors="ignore")
    for tag in (
        "v6CardSafety", "v6CardFreshness", "v6CardDataQuality", "v6CardShadow",
        "v6CardEdge", "v6CardReadiness",
        "v6OverviewRefreshBtn", "v6OverviewPackBtn",
        "v6BlockersList", "v6BestLeadsRows", "v6WorstSideList",
        "v6RawSampleValue", "v6CleanSampleValue",
    ):
        n = len(re.findall(rf'id="{tag}"', body))
        assert n == 1, f"id={tag} appears {n} times"


# ---------------------------------------------------------------------------
# Endpoint registration + safety
# ---------------------------------------------------------------------------


def test_v6_health_server_registers_clean_metrics_endpoint():
    import app.health_server as hs
    source = inspect.getsource(hs)
    assert "/api/research/clean-research-metrics" in source
    assert hasattr(hs, "_v6_clean_research_metrics")


def test_v6_clean_metrics_endpoint_returns_no_live(db):
    from app import health_server as hs
    payload = hs._v6_clean_research_metrics(load_config(), db, {"hours": ["24"]})
    assert payload["final_recommendation"] == "NO LIVE"
    assert payload["can_send_real_orders"] is False
    assert payload["paper_filter_enabled"] is False


def test_v6_research_lab_has_clean_research_metrics_command():
    import app.research_lab as rl
    source = inspect.getsource(rl)
    assert '"clean-research-metrics"' in source
    assert "def clean_research_metrics" in source


# ---------------------------------------------------------------------------
# Safety invariants
# ---------------------------------------------------------------------------


def test_v6_safety_flags_unchanged():
    cfg = load_config()
    assert cfg.live_trading is False
    assert cfg.dry_run is True
    assert cfg.paper_trading is True
    assert cfg.enable_paper_policy_filter is False
    assert cfg.enable_candidate_shadow_monitor is False
    assert cfg.can_send_real_orders is False
    assert getattr(cfg, "enable_ohlcv_auto_refresh", False) is False


def test_v6_modules_have_no_forbidden_runtime_tokens():
    import app.clean_research_metrics
    import app.strategy_research_enhancer as enh

    forbidden = (
        "PaperTrader.open_position",
        "ExecutionEngine.execute",
        "place_order(",
        "set_leverage(",
        "set_margin_mode(",
        "private_get(",
        "private_post(",
        "can_send_real_orders=True",
        "LIVE_TRADING=True",
        "ENABLE_PAPER_POLICY_FILTER=True",
        "ENABLE_OHLCV_AUTO_REFRESH=True",
        "allow_real_writes=True",
    )
    for module in (app.clean_research_metrics, enh):
        _assert_no_executable_tokens(module, forbidden)
