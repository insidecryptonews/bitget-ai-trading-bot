"""ResearchOps V7.5 — Tests integrados.

Cubre:
  - Duplicate Guard Hook (audit / enforce / disabled).
  - Funding cost model (signo, NEED_DATA, sin endpoints privados).
  - Walk-Forward V2 (folds, bootstrap CI, decisión).
  - Liquidation model Bitget (tiers, fallback, blocks_scale_up).
  - Pack V7.5 (incluye nuevas secciones, sin secretos).
  - Endpoints registrados.
  - CLI registrados.
  - FeatureLogger integra el hook sin romper escritura.
  - Safety scan AST.
  - Flags de config v7.5 arrancan False.
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


def _strip(src: str) -> str:
    tree = ast.parse(src)
    spans: list[tuple[int, int, int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            spans.append((node.lineno, node.col_offset, node.end_lineno, node.end_col_offset))
    lines = src.splitlines(keepends=True)
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
                        for c in range(sc, len(row)): row[c] = " "
                    elif li == el:
                        for c in range(0, min(ec, len(row))): row[c] = " "
                    else:
                        for c in range(len(row)):
                            if row[c] != "\n": row[c] = " "
    return re.sub(r"(?m)#.*$", "", "".join("".join(row) for row in chars))


def _no_forbidden(module, tokens) -> None:
    src = _strip(inspect.getsource(module))
    for token in tokens:
        assert token not in src, f"{token} found in {module.__name__} executable code"


FORBIDDEN_V75 = (
    "PaperTrader.open_position", "ExecutionEngine.execute",
    "place_order(", "set_leverage(", "set_margin_mode(",
    "private_get(", "private_post(",
    "can_send_real_orders=True", "LIVE_TRADING=True",
    "ENABLE_PAPER_POLICY_FILTER=True", "ENABLE_OHLCV_AUTO_REFRESH=True",
    "allow_real_writes=True",
)


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
# Config flags
# ---------------------------------------------------------------------------


def test_v75_config_flags_default_false():
    cfg = load_config()
    assert cfg.enable_duplicate_guard_hook is False
    assert cfg.duplicate_guard_hook_mode == "audit"
    assert cfg.enable_funding_cost_model is False
    assert cfg.enable_liquidation_model_bitget is False


# ---------------------------------------------------------------------------
# Duplicate Guard Hook
# ---------------------------------------------------------------------------


def test_v75_duplicate_guard_hook_disabled_by_default_allows_all():
    from app.duplicate_guard_hook import DuplicateGuardHook

    hook = DuplicateGuardHook(enabled=False, mode="audit")
    decision = hook.decide({"symbol": "DOTUSDT", "side": "SHORT", "timestamp": "2026-05-29T12:34:00+00:00", "strategy_type": "smc_short", "market_regime": "RISK_OFF"})
    assert decision.allow_write is True
    assert decision.would_block is False
    assert decision.actual_block is False
    stats = hook.stats()
    assert stats.seen_count == 0


def test_v75_duplicate_guard_hook_audit_does_not_block_but_counts():
    from app.duplicate_guard_hook import DuplicateGuardHook

    hook = DuplicateGuardHook(enabled=True, mode="audit")
    obs = {"symbol": "DOTUSDT", "side": "SHORT", "timestamp": "2026-05-29T12:34:00+00:00", "strategy_type": "smc_short", "market_regime": "RISK_OFF"}
    d1 = hook.decide(obs)
    d2 = hook.decide(dict(obs))
    assert d1.allow_write is True and d1.duplicate_class == "NEW"
    assert d2.allow_write is True and d2.would_block is True and d2.actual_block is False
    stats = hook.stats()
    assert stats.would_block_count == 1
    assert stats.actual_block_count == 0


def test_v75_duplicate_guard_hook_enforce_blocks_duplicates():
    from app.duplicate_guard_hook import DuplicateGuardHook

    hook = DuplicateGuardHook(enabled=True, mode="enforce")
    obs = {"symbol": "DOTUSDT", "side": "SHORT", "timestamp": "2026-05-29T12:34:00+00:00", "strategy_type": "smc_short", "market_regime": "RISK_OFF"}
    d1 = hook.decide(obs)
    d2 = hook.decide(dict(obs))
    assert d1.allow_write is True
    assert d2.allow_write is False
    assert d2.actual_block is True
    stats = hook.stats()
    assert stats.actual_block_count == 1


def test_v75_duplicate_guard_hook_preserves_distinct_setups():
    from app.duplicate_guard_hook import DuplicateGuardHook

    hook = DuplicateGuardHook(enabled=True, mode="enforce")
    base = {"symbol": "DOTUSDT", "side": "SHORT", "timestamp": "2026-05-29T12:34:00+00:00", "market_regime": "RISK_OFF"}
    d1 = hook.decide(dict(base, strategy_type="smc_short"))
    d2 = hook.decide(dict(base, strategy_type="ema200_breakdown"))
    assert d1.allow_write and d2.allow_write
    assert d1.fingerprint != d2.fingerprint


def test_v75_feature_logger_integrates_hook_without_breaking(monkeypatch):
    """FeatureLogger.record_observation no debe romperse aunque el hook esté
    en modo enforce y bloquee."""
    from app.duplicate_guard_hook import configure_global_hook
    from app.feature_logger import FeatureLogger

    written: list[dict] = []

    class _FakeDB:
        def record_signal_observation(self, obs):
            written.append(obs)
            return len(written)

    logger = logging.getLogger("test")
    fl = FeatureLogger(_FakeDB(), logger)
    configure_global_hook(enabled=True, mode="enforce")
    try:
        obs = {"symbol": "DOTUSDT", "side": "SHORT", "timestamp": "2026-05-29T12:34:00+00:00", "strategy_type": "smc_short", "market_regime": "RISK_OFF"}
        id1 = fl.record_observation(dict(obs))
        id2 = fl.record_observation(dict(obs))
        assert id1 == 1
        assert id2 == 0  # bloqueado
        assert len(written) == 1
    finally:
        configure_global_hook(enabled=False, mode="audit")


def test_v75_feature_logger_disabled_hook_writes_all(monkeypatch):
    from app.duplicate_guard_hook import configure_global_hook
    from app.feature_logger import FeatureLogger

    written: list[dict] = []

    class _FakeDB:
        def record_signal_observation(self, obs):
            written.append(obs)
            return len(written)

    fl = FeatureLogger(_FakeDB(), logging.getLogger("test"))
    configure_global_hook(enabled=False, mode="audit")
    obs = {"symbol": "DOTUSDT", "side": "SHORT", "timestamp": "2026-05-29T12:34:00+00:00", "strategy_type": "smc_short", "market_regime": "RISK_OFF"}
    fl.record_observation(dict(obs))
    fl.record_observation(dict(obs))
    fl.record_observation(dict(obs))
    assert len(written) == 3


# ---------------------------------------------------------------------------
# Funding cost model
# ---------------------------------------------------------------------------


def test_v75_funding_no_table_returns_need_data(db):
    from app.funding_cost_model import apply_funding_to_trade

    v = apply_funding_to_trade(
        db,
        symbol="DOTUSDT", side="SHORT",
        entry_time="2026-05-29T07:30:00+00:00",
        exit_time="2026-05-29T09:30:00+00:00",
    )
    assert v.funding_data_status == "NEED_DATA"
    assert v.net_adjustment_pct == 0.0


def test_v75_funding_no_crossing_returns_ok_zero(db, monkeypatch):
    """Cuando hay tabla pero el trade no cruza, el ajuste es 0 y status OK."""
    from app import funding_cost_model as fcm

    monkeypatch.setattr(fcm, "_funding_table_exists", lambda _db: True)
    monkeypatch.setattr(fcm, "_fetch_funding_rates", lambda _db, _s, _c: [])
    v = fcm.apply_funding_to_trade(
        db,
        symbol="DOTUSDT", side="SHORT",
        entry_time="2026-05-29T09:00:00+00:00",
        exit_time="2026-05-29T15:00:00+00:00",
    )
    assert v.crossings == 0
    assert v.funding_data_status == "OK"
    assert v.net_adjustment_pct == 0.0


def test_v75_funding_signs_long_pays_when_positive_rate(db, monkeypatch):
    from app import funding_cost_model as fcm

    monkeypatch.setattr(fcm, "_funding_table_exists", lambda _db: True)
    # +0.01% por cruce.
    monkeypatch.setattr(fcm, "_fetch_funding_rates", lambda _db, _s, _c: [0.0001])
    v = fcm.apply_funding_to_trade(
        db,
        symbol="DOTUSDT", side="LONG",
        entry_time="2026-05-29T07:00:00+00:00",
        exit_time="2026-05-29T09:00:00+00:00",
    )
    # LONG paga si rate > 0 → ajuste negativo.
    assert v.crossings >= 1
    assert v.net_adjustment_pct < 0


def test_v75_funding_signs_short_receives_when_positive_rate(db, monkeypatch):
    from app import funding_cost_model as fcm

    monkeypatch.setattr(fcm, "_funding_table_exists", lambda _db: True)
    monkeypatch.setattr(fcm, "_fetch_funding_rates", lambda _db, _s, _c: [0.0001])
    v = fcm.apply_funding_to_trade(
        db,
        symbol="DOTUSDT", side="SHORT",
        entry_time="2026-05-29T07:00:00+00:00",
        exit_time="2026-05-29T09:00:00+00:00",
    )
    assert v.crossings >= 1
    assert v.net_adjustment_pct > 0


def test_v75_funding_module_no_private_endpoints():
    import app.funding_cost_model as mod
    _no_forbidden(mod, FORBIDDEN_V75)


# ---------------------------------------------------------------------------
# Walk-forward V2
# ---------------------------------------------------------------------------


def _make_trades(n: int, *, start: datetime, days_between: float, net_ev: float = 0.05) -> list[dict]:
    out: list[dict] = []
    for i in range(n):
        dt = start + timedelta(days=i * days_between)
        out.append({"entry_time": dt.isoformat(), "net_return_pct": net_ev if (i % 5) != 0 else -net_ev})
    return out


def test_v75_wf2_need_more_data_when_empty():
    from app.walk_forward_runner_v2 import run_walk_forward_v2

    report = run_walk_forward_v2(trades=[])
    assert report.n_folds == 0
    assert report.decision == "WF2_NEED_MORE_DATA"


def test_v75_wf2_produces_folds_and_bootstrap_when_enough_data():
    from app.walk_forward_runner_v2 import run_walk_forward_v2

    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    trades = _make_trades(900, start=start, days_between=0.1, net_ev=0.05)
    report = run_walk_forward_v2(trades=trades, train_days=10, test_days=3, step_days=3, n_bootstrap=200)
    assert report.n_folds >= 4
    assert report.bootstrap_net_ev is not None
    ci = report.bootstrap_net_ev
    eps = 1e-9
    assert ci.low - eps <= ci.mean + eps
    assert ci.mean - eps <= ci.high + eps


def test_v75_wf2_safety_research_only():
    import app.walk_forward_runner_v2 as mod
    _no_forbidden(mod, FORBIDDEN_V75)


# ---------------------------------------------------------------------------
# Liquidation model Bitget
# ---------------------------------------------------------------------------


def test_v75_liquidation_known_symbol():
    from app.liquidation_model_bitget import evaluate_liquidation

    v = evaluate_liquidation(symbol="BTCUSDT", leverage=5, capital_usdt=40.0, margin_per_trade_usdt=5.0)
    assert v.tier_source == "local_table_v1"
    assert v.liquidation_distance_pct > 0
    assert v.research_only is True
    assert v.can_send_real_orders is False


def test_v75_liquidation_unknown_symbol_uses_fallback():
    from app.liquidation_model_bitget import evaluate_liquidation

    v = evaluate_liquidation(symbol="XYZUSDT", leverage=5, capital_usdt=40.0)
    assert v.tier_source == "fallback_conservative"


def test_v75_liquidation_high_leverage_blocks_scale_up():
    from app.liquidation_model_bitget import evaluate_liquidation

    v = evaluate_liquidation(symbol="DOTUSDT", leverage=50, capital_usdt=40.0, margin_per_trade_usdt=5.0)
    assert v.liquidation_risk in {"HIGH", "CRITICAL"}
    assert v.blocks_scale_up is True


def test_v75_liquidation_module_safety():
    import app.liquidation_model_bitget as mod
    _no_forbidden(mod, FORBIDDEN_V75)


# ---------------------------------------------------------------------------
# Pack V7.5
# ---------------------------------------------------------------------------


def test_v75_pack_includes_new_sections(db):
    from app.research_pack_v7_5 import build_research_pack_v7_5

    pack = build_research_pack_v7_5(
        load_config(), db, hours=6, symbols=["BTCUSDT"], timeframes=["5m"],
    )
    # ``pack_version`` was ``v7_5``; with V8/V9 foundation it becomes
    # ``v7_5_v8v9_foundation``. Accept both to keep the test future-proof.
    assert pack["pack_version"] in {"v7_5", "v7_5_v8v9_foundation"}
    assert "duplicate_guard_hook_stats" in pack
    assert "funding_cost_model" in pack
    assert "liquidation_model_sample" in pack
    # V8/V9 sections must be present in the new pack version.
    if pack["pack_version"] == "v7_5_v8v9_foundation":
        for key in (
            "auto_data_enrichment",
            "exit_intelligence",
            "strategy_experiment_registry",
            "shadow_candidate_lifecycle",
            "validation_gates_v9",
        ):
            assert key in pack, f"missing V8/V9 section {key}"
    assert pack["final_recommendation"] == "NO LIVE"
    serialised = str(pack)
    for forbidden in ("API_KEY", "api_key", "API_SECRET", "PASSPHRASE"):
        assert forbidden not in serialised


# ---------------------------------------------------------------------------
# Endpoints + CLI
# ---------------------------------------------------------------------------


def test_v75_endpoints_registered():
    import app.health_server as hs
    src = inspect.getsource(hs)
    for path in (
        "/api/research/duplicate-guard-hook-status",
        "/api/research/funding-cost-model",
        "/api/research/liquidation-model-bitget",
        "/api/research/walk-forward-v2",
        "/api/research-pack-v7-5",
    ):
        assert path in src, f"{path} missing"


def test_v75_research_lab_has_commands():
    import app.research_lab as rl
    src = inspect.getsource(rl)
    for cmd in (
        "duplicate-guard-hook-status",
        "funding-cost-model",
        "liquidation-model-bitget",
        "walk-forward-v2",
        "research-pack-v7-5",
    ):
        assert f'"{cmd}"' in src, f"command {cmd} not registered"


def test_v75_dashboard_has_strip():
    html = (Path(__file__).resolve().parent.parent / "app" / "static" / "dashboard.html").read_text(encoding="utf-8")
    assert "v75-strip" in html
    assert "ResearchOps V7.5" in html


# ---------------------------------------------------------------------------
# Safety global
# ---------------------------------------------------------------------------


def test_v75_safety_flags_unchanged():
    cfg = load_config()
    assert cfg.live_trading is False
    assert cfg.dry_run is True
    assert cfg.paper_trading is True
    assert cfg.enable_paper_policy_filter is False
    assert cfg.enable_candidate_shadow_monitor is False
    assert cfg.can_send_real_orders is False
    assert cfg.enable_ohlcv_auto_refresh is False
    assert cfg.enable_duplicate_guard_hook is False
    assert cfg.enable_funding_cost_model is False
    assert cfg.enable_liquidation_model_bitget is False


def test_v75_all_new_modules_safety_scan():
    import app.duplicate_guard_hook
    import app.funding_cost_model
    import app.liquidation_model_bitget
    import app.bitget_liquidation_tiers
    import app.walk_forward_runner_v2
    import app.research_pack_v7_5
    for module in (
        app.duplicate_guard_hook,
        app.funding_cost_model,
        app.liquidation_model_bitget,
        app.bitget_liquidation_tiers,
        app.walk_forward_runner_v2,
        app.research_pack_v7_5,
    ):
        _no_forbidden(module, FORBIDDEN_V75)
