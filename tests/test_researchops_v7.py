"""ResearchOps V7 tests.

Cubre:
  - Data pipeline root cause: clasifica duplicados / market_probe / orphans.
  - Duplicate guard: huellas deterministas; conserva setups distintos.
  - Clean strategy lab: usa CLEAN; LONG vs SHORT; market_probe nunca accionable.
  - Capital scaling: net_EV ≤ 0 → DO_NOT_SCALE.
  - Research pack V7: incluye root-cause y clean lab.
  - Endpoints registrados.
  - Safety scan (AST strip).
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


def _strip(source: str) -> str:
    tree = ast.parse(source)
    spans: list[tuple[int, int, int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            spans.append((node.lineno, node.col_offset, node.end_lineno, node.end_col_offset))
    lines = source.splitlines(keepends=True)
    chars = [list(line) for line in lines]
    for sl, sc, el, ec in sorted(spans):
        if sl == el:
            row = chars[sl - 1]
            for c in range(sc, min(ec, len(row))):
                row[c] = " "
        else:
            for li in range(sl, el + 1):
                if 1 <= li <= len(chars):
                    row = chars[li - 1]
                    if li == sl:
                        for c in range(sc, len(row)): row[c] = " "
                    elif li == el:
                        for c in range(0, min(ec, len(row))): row[c] = " "
                    else:
                        for c in range(len(row)):
                            if row[c] != "\n": row[c] = " "
    return re.sub(r"(?m)#.*$", "", "".join("".join(row) for row in chars))


def _assert_no_tokens(module, tokens: tuple[str, ...]) -> None:
    src = inspect.getsource(module)
    cleaned = _strip(src)
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


# Parte 1 — Data pipeline root cause -----------------------------------------


def test_v7_root_cause_module_exists():
    import app.data_pipeline_root_cause as mod
    assert hasattr(mod, "run_data_pipeline_root_cause")
    assert hasattr(mod, "DataPipelineRootCauseReport")


def test_v7_root_cause_on_empty_db_does_not_crash(db):
    from app.data_pipeline_root_cause import (
        render_data_pipeline_root_cause_text,
        run_data_pipeline_root_cause,
    )
    report = run_data_pipeline_root_cause(db, hours=24)
    assert report.research_only is True
    assert report.no_db_writes is True
    assert report.final_recommendation == "NO LIVE"
    assert report.biggest_problem in {"no_data", "clean_enough_for_research"}
    text = render_data_pipeline_root_cause_text(report)
    assert "DATA PIPELINE ROOT CAUSE START" in text
    assert "no_db_writes: true" in text
    assert "no_private_endpoints_used: true" in text


def test_v7_root_cause_does_not_write_db():
    import app.data_pipeline_root_cause as mod
    src = _strip(inspect.getsource(mod))
    for forbidden in ("INSERT INTO ", "DELETE FROM ", "UPDATE ", "DROP "):
        assert forbidden.upper() not in src.upper(), f"{forbidden} found"


# Parte 2 — Duplicate guard ---------------------------------------------------


def test_v7_duplicate_guard_fingerprint_is_deterministic():
    from app.duplicate_guard import fingerprint

    obs = {
        "source": "trade_signal",
        "strategy_type": "smc_short",
        "symbol": "DOTUSDT",
        "timeframe": "5m",
        "side": "SHORT",
        "market_regime": "RISK_OFF",
        "confidence_score": 88,
        "timestamp": "2026-05-29T12:34:00+00:00",
        "entry_price": 5.012345,
    }
    fp1 = fingerprint(obs)
    fp2 = fingerprint(dict(obs))
    assert fp1 == fp2 and len(fp1) == 40


def test_v7_duplicate_guard_preserves_distinct_setups():
    from app.duplicate_guard import fingerprint

    base = {
        "symbol": "DOTUSDT", "side": "SHORT", "timeframe": "5m",
        "timestamp": "2026-05-29T12:34:00+00:00",
        "source": "trade_signal", "strategy_type": "smc_short",
    }
    other = dict(base, market_regime="RISK_OFF")
    different_setup = dict(base, strategy_type="ema200_breakout")
    different_regime = dict(other, market_regime="TREND_UP")
    assert fingerprint(other) != fingerprint(different_setup)
    assert fingerprint(other) != fingerprint(different_regime)


def test_v7_duplicate_guard_classifies_repeats():
    from app.duplicate_guard import classify_duplicate

    obs1 = {
        "symbol": "DOTUSDT", "side": "SHORT", "timeframe": "5m",
        "timestamp": "2026-05-29T12:34:00+00:00",
        "strategy_type": "smc_short", "market_regime": "RISK_OFF",
    }
    obs2 = dict(obs1)
    assert classify_duplicate(obs1, obs2) == "EXACT_DUPLICATE"
    obs3 = dict(obs1, timestamp="2026-05-29T12:35:00+00:00")
    assert classify_duplicate(obs1, obs3) in {"SEMANTIC_DUPLICATE", "EXACT_DUPLICATE"}


def test_v7_duplicate_guard_market_probe_never_actionable():
    from app.duplicate_guard import evaluate, is_market_probe, is_trade_signal

    probe = {"source": "market_probe", "strategy_type": "market_probe",
             "symbol": "BTCUSDT", "side": "SHORT", "timestamp": "2026-05-29T00:00:00+00:00"}
    assert is_market_probe(probe) is True
    assert is_trade_signal(probe) is False
    verdict = evaluate(probe)
    assert verdict.is_market_probe is True
    assert verdict.actionable is False
    assert verdict.can_send_real_orders is False


def test_v7_duplicate_guard_dedupe_in_memory_preserves_distinct():
    from app.duplicate_guard import deduplicate

    rows = [
        {"symbol": "DOTUSDT", "side": "SHORT", "timeframe": "5m",
         "timestamp": "2026-05-29T12:34:00+00:00",
         "strategy_type": "smc_short", "market_regime": "RISK_OFF"},
        {"symbol": "DOTUSDT", "side": "SHORT", "timeframe": "5m",
         "timestamp": "2026-05-29T12:34:00+00:00",
         "strategy_type": "smc_short", "market_regime": "RISK_OFF"},
        {"symbol": "DOTUSDT", "side": "SHORT", "timeframe": "5m",
         "timestamp": "2026-05-29T12:34:00+00:00",
         "strategy_type": "ema200_breakdown", "market_regime": "RISK_OFF"},
    ]
    cleaned = deduplicate(rows)
    # 2 setups distintos preservados (smc_short vs ema200_breakdown).
    assert len(cleaned) == 2


def test_v7_duplicate_guard_safety_scan():
    import app.duplicate_guard as mod
    _assert_no_tokens(mod, (
        "PaperTrader.open_position", "ExecutionEngine.execute",
        "place_order(", "set_leverage(", "set_margin_mode(",
        "private_get(", "private_post(",
        "can_send_real_orders=True", "LIVE_TRADING=True",
        "ENABLE_PAPER_POLICY_FILTER=True",
    ))


# Parte 5 — Clean strategy lab ----------------------------------------------


def test_v7_clean_strategy_lab_module_exists():
    import app.clean_strategy_lab as mod
    assert hasattr(mod, "run_clean_strategy_lab")
    assert hasattr(mod, "STRATEGY_FAMILIES")


def test_v7_clean_strategy_lab_returns_no_live(db):
    from app.clean_strategy_lab import (
        render_clean_strategy_lab_text,
        run_clean_strategy_lab,
    )
    report = run_clean_strategy_lab(load_config(), db, hours=6, symbols=["BTCUSDT"])
    assert report.research_only is True
    assert report.can_send_real_orders is False
    assert report.paper_filter_enabled is False
    text = render_clean_strategy_lab_text(report)
    assert "CLEAN STRATEGY LAB START" in text
    assert "do_not_promote_raw: true" in text
    assert "final_recommendation: NO LIVE" in text


def test_v7_clean_strategy_lab_short_family_long_blocked(db):
    from app.clean_strategy_lab import run_clean_strategy_lab

    report = run_clean_strategy_lab(load_config(), db, hours=6, symbols=["BTCUSDT"])
    short_family = next((f for f in report.families if f.strategy_family == "A_short_trend_continuation"), None)
    assert short_family is not None
    # En DB vacía o casi vacía, la familia debe reportar NEED_MORE_DATA o REJECT.
    assert short_family.decision in {"NEED_MORE_DATA", "REJECT", "WATCH_ONLY"}


def test_v7_clean_strategy_lab_safety():
    import app.clean_strategy_lab as mod
    _assert_no_tokens(mod, (
        "PaperTrader.open_position", "ExecutionEngine.execute",
        "place_order(", "set_leverage(", "set_margin_mode(",
        "can_send_real_orders=True", "LIVE_TRADING=True",
        "ENABLE_PAPER_POLICY_FILTER=True",
    ))


# Parte 10 — Capital scaling --------------------------------------------------


def test_v7_capital_scaling_negative_ev_blocks():
    from app.capital_scaling_simulator import run_capital_scaling_simulator

    report = run_capital_scaling_simulator(
        base_clean_net_ev_pct=-0.10,
        base_clean_pf=0.7,
        trades_per_window=100,
        data_quality_status="OK",
        ohlcv_actionable=True,
    )
    for scenario in report.scenarios:
        assert scenario.scale_up_eligible is False
        assert "do_not_scale" in scenario.do_not_scale_reason


def test_v7_capital_scaling_data_bad_blocks_even_with_positive_ev():
    from app.capital_scaling_simulator import run_capital_scaling_simulator

    report = run_capital_scaling_simulator(
        base_clean_net_ev_pct=0.20,
        base_clean_pf=1.5,
        trades_per_window=100,
        data_quality_status="BAD",
        ohlcv_actionable=True,
    )
    for scenario in report.scenarios:
        assert scenario.scale_up_eligible is False


def test_v7_capital_scaling_safety_scan():
    import app.capital_scaling_simulator as mod
    _assert_no_tokens(mod, (
        "set_leverage(", "set_margin_mode(", "place_order(",
        "PaperTrader.open_position", "ExecutionEngine.execute",
    ))


# Parte 9 — Research pack V7 -------------------------------------------------


def test_v7_research_pack_excludes_secrets(db):
    from app.research_pack_v7 import build_research_pack_v7

    pack = build_research_pack_v7(
        load_config(), db,
        hours=6, symbols=["BTCUSDT"], timeframes=["5m"],
        include_strategy_lab=True,
        include_capital_scaling=True,
    )
    serialised = str(pack)
    for forbidden in ("API_KEY", "api_key", "API_SECRET", "api_secret", "PASSPHRASE"):
        assert forbidden not in serialised, f"{forbidden} leaked"
    assert pack["pack_version"] == "v7"
    assert pack["final_recommendation"] == "NO LIVE"
    assert pack["can_send_real_orders"] is False


# Endpoints + CLI ------------------------------------------------------------


def test_v7_health_server_endpoints_registered():
    import app.health_server as hs
    source = inspect.getsource(hs)
    for path in (
        "/api/research/data-pipeline-root-cause",
        "/api/research/clean-strategy-lab",
        "/api/research/capital-scaling-simulator",
        "/api/research-pack-v7",
    ):
        assert path in source, f"{path} missing"


def test_v7_research_lab_has_commands():
    import app.research_lab as rl
    source = inspect.getsource(rl)
    for command in (
        "data-pipeline-root-cause",
        "clean-strategy-lab",
        "capital-scaling-simulator",
        "research-pack-v7",
    ):
        assert f'"{command}"' in source, f"{command} not registered"


def test_v7_dashboard_html_has_operator_loop():
    html_path = Path(__file__).resolve().parent.parent / "app" / "static" / "dashboard.html"
    body = html_path.read_text(encoding="utf-8", errors="ignore")
    assert "v7-operator-loop" in body
    assert "Operator Loop V7" in body
    assert "SCAN → DETECT → VALIDATE → SIZE_SIM → MANAGE_SIM → SETTLE → LEARN" in body


# Safety invariants ---------------------------------------------------------


def test_v7_safety_flags_unchanged():
    cfg = load_config()
    assert cfg.live_trading is False
    assert cfg.dry_run is True
    assert cfg.paper_trading is True
    assert cfg.enable_paper_policy_filter is False
    assert cfg.enable_candidate_shadow_monitor is False
    assert cfg.can_send_real_orders is False
    assert getattr(cfg, "enable_ohlcv_auto_refresh", False) is False


def test_v7_all_modules_safety_scan():
    import app.data_pipeline_root_cause
    import app.duplicate_guard
    import app.clean_strategy_lab
    import app.capital_scaling_simulator
    import app.research_pack_v7

    forbidden = (
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
    )
    for module in (
        app.data_pipeline_root_cause,
        app.duplicate_guard,
        app.clean_strategy_lab,
        app.capital_scaling_simulator,
        app.research_pack_v7,
    ):
        _assert_no_tokens(module, forbidden)
