"""Tests for V8.2.1 — DB readers, ATR fix, same-bar STOP_BEFORE_TP, heavy guard.

Read-only tests. No live, no order paths, no `.env`.
"""

from __future__ import annotations

import ast
import importlib
import pathlib
import tempfile
from datetime import datetime, timedelta, timezone

import pytest


# ---------------------------------------------------------------------------
# DB readers: backwards compatibility + new kwargs
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_fixture(tmp_path):
    """Create a fresh sqlite-backed Database scoped to ``tmp_path``.

    Note: ``Database`` always uses ``PROJECT_ROOT / bot_state.db`` for SQLite,
    so we override ``sqlite_path`` directly to keep the test isolated.
    """
    import logging

    from app.config import BotConfig
    from app.database import Database

    cfg = BotConfig(use_postgres_if_available=False)
    db = Database(cfg, logging.getLogger("v821_test"))
    db.sqlite_path = tmp_path / "v821_isolated.sqlite"
    # Reset WAL flag to force re-PRAGMA on the new file.
    Database._sqlite_wal_initialised = False
    db.initialize()
    yield db


def _insert_observation(db, *, timestamp, symbol, side, score, regime="RANGE",
                       normalized_atr=0.02, spread_pct=0.0010, funding_rate=0.0001):
    # The schema allows direct SQL insert with the supported columns.
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO signal_observations(timestamp, symbol, side, "
            " strategy_type, confidence_score, market_regime, entry_price, "
            " stop_loss, take_profit_1, take_profit_2, risk_reward_ratio, "
            " leverage_recommendation, spread_pct, volume_24h_usdt, "
            " funding_rate, open_interest, timeframe_alignment, "
            " confirmations_json, warnings_json, rsi_14, macd_hist, atr_14, "
            " normalized_atr, volume_relative) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                timestamp, symbol, side, "TEST", score, regime,
                100.0, 99.5, 101.0, 102.0, 1.5, 3,
                spread_pct, 100000000.0, funding_rate, 0.0,
                "neutral", "[]", "[]", 50.0, 0.0, 0.5, normalized_atr, 1.2,
            ),
        )


def test_fetch_signal_observations_legacy_signature_still_works(db_fixture):
    db = db_fixture
    _insert_observation(db, timestamp="2026-06-01T00:00:00+00:00",
                        symbol="BTCUSDT", side="LONG", score=80)
    rows = db.fetch_signal_observations()
    assert len(rows) == 1
    assert rows[0]["symbol"] == "BTCUSDT"


def test_fetch_signal_observations_limit_still_works(db_fixture):
    db = db_fixture
    for i in range(5):
        ts = (datetime(2026, 6, 1, tzinfo=timezone.utc) + timedelta(hours=i)).isoformat()
        _insert_observation(db, timestamp=ts, symbol="BTCUSDT", side="LONG", score=70 + i)
    rows = db.fetch_signal_observations(limit=3)
    assert len(rows) == 3


def test_fetch_signal_observations_hours_filter(db_fixture):
    db = db_fixture
    now = datetime.now(timezone.utc)
    recent_ts = (now - timedelta(hours=1)).isoformat()
    old_ts = (now - timedelta(hours=24 * 30)).isoformat()
    _insert_observation(db, timestamp=recent_ts, symbol="BTCUSDT", side="LONG", score=80)
    _insert_observation(db, timestamp=old_ts, symbol="ETHUSDT", side="LONG", score=80)
    rows = db.fetch_signal_observations(hours=24)
    assert len(rows) == 1
    assert rows[0]["symbol"] == "BTCUSDT"


def test_fetch_signal_observations_side_filter(db_fixture):
    db = db_fixture
    now = datetime.now(timezone.utc)
    ts = (now - timedelta(hours=1)).isoformat()
    _insert_observation(db, timestamp=ts, symbol="BTCUSDT", side="LONG", score=80)
    _insert_observation(db, timestamp=ts, symbol="ETHUSDT", side="SHORT", score=80)
    rows = db.fetch_signal_observations(hours=24, side="SHORT")
    assert len(rows) == 1
    assert rows[0]["side"] == "SHORT"


def test_fetch_router_inputs_returns_structure(db_fixture):
    db = db_fixture
    now = datetime.now(timezone.utc)
    for i in range(10):
        ts = (now - timedelta(hours=i, minutes=10)).isoformat()
        _insert_observation(db, timestamp=ts, symbol="BTCUSDT",
                            side="SHORT" if i % 2 else "LONG",
                            score=75, regime="TREND_DOWN" if i % 2 else "TREND_UP")
    rows = db.fetch_router_inputs(hours=24)
    assert isinstance(rows, list)
    # Each row must look like a RouterInputs dict.
    if rows:
        sample = rows[0]
        for key in ("timestamp", "btc_bias_1h", "pct_universe_up",
                    "pct_universe_down", "regime_current"):
            assert key in sample


def test_fetch_router_inputs_empty_when_no_data(tmp_path):
    """A truly fresh isolated DB with no observations returns an empty list."""
    import logging

    from app.config import BotConfig
    from app.database import Database

    cfg = BotConfig(use_postgres_if_available=False)
    db = Database(cfg, logging.getLogger("v821_empty"))
    db.sqlite_path = tmp_path / "v821_empty_router.sqlite"
    Database._sqlite_wal_initialised = False
    db.initialize()
    rows = db.fetch_router_inputs(hours=24)
    assert rows == []


def test_fetch_campaign_trades_filters_by_side(db_fixture):
    db = db_fixture
    now = datetime.now(timezone.utc)
    ts = (now - timedelta(hours=1)).isoformat()
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO trades(timestamp, mode, symbol, strategy_type, side, "
            " entry, stop_loss, take_profit_1, take_profit_2, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, "paper", "BTCUSDT", "TEST", "LONG", 100.0, 99.0, 101.0, 102.0, "CLOSED_TP"),
        )
        conn.execute(
            "INSERT INTO trades(timestamp, mode, symbol, strategy_type, side, "
            " entry, stop_loss, take_profit_1, take_profit_2, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, "paper", "ETHUSDT", "TEST", "SHORT", 100.0, 101.0, 99.0, 98.0, "CLOSED_SL"),
        )
    # Without OHLCV data, bar_path reconstruction fails → empty list (no crash).
    rows_long = db.fetch_campaign_trades(hours=24, side="LONG")
    rows_short = db.fetch_campaign_trades(hours=24, side="SHORT")
    # Both empty (no OHLCV) — but the method must not raise.
    assert isinstance(rows_long, list)
    assert isinstance(rows_short, list)


def test_fetch_exit_replay_trades_same_shape(db_fixture):
    db = db_fixture
    rows = db.fetch_exit_replay_trades(hours=24)
    assert isinstance(rows, list)


def test_fetch_signal_observations_hours_yields_joined_columns(db_fixture):
    db = db_fixture
    now = datetime.now(timezone.utc)
    ts = (now - timedelta(hours=1)).isoformat()
    _insert_observation(db, timestamp=ts, symbol="BTCUSDT", side="LONG", score=80)
    rows = db.fetch_signal_observations(hours=24)
    assert rows
    # JOIN with signal_path_metrics gives these columns (None when no path row).
    for key in ("mfe_pct", "mae_pct", "realized_pct", "first_barrier_hit", "bars_open"):
        assert key in rows[0]


# ---------------------------------------------------------------------------
# ATR percent → absolute distance fix
# ---------------------------------------------------------------------------

def _bar(o, h, l, c):
    return {"open": o, "high": h, "low": l, "close": c}


def test_campaign_atr_fix_doge_like_price():
    """With entry=0.10 and atr_pct_at_entry=0.50% (=0.0005 absolute), the
    add-distance threshold must be ~0.0005 not 0.5. Otherwise no add can
    ever fire on low-priced symbols.
    """
    from app.labs.trend_campaign_simulator import CampaignTrade, simulate_campaign

    bars = [
        _bar(0.1000, 0.1002, 0.0995, 0.0996),  # base in profit by 0.40%
        _bar(0.0996, 0.0997, 0.0992, 0.0992),  # continuation: 0.40% below last_add
        _bar(0.0992, 0.0993, 0.0988, 0.0988),  # continuation
    ]
    trade = CampaignTrade(
        symbol="DOGEUSDT", side="SHORT", entry=0.1000, stop=0.1010,
        bar_path=bars, atr_pct_at_entry=0.40,
    )
    result = simulate_campaign(trade, max_adds=3, min_profit_for_add_pct=0.3)
    # The fix must allow at least one add despite low absolute prices.
    assert result["adds_executed"] >= 1


def test_campaign_atr_abs_override_used_when_provided():
    """When ``atr_abs_at_entry`` is explicit, the simulator must use it
    instead of converting from percent.
    """
    from app.labs.trend_campaign_simulator import CampaignTrade, simulate_campaign

    bars = [
        _bar(100, 101, 99.5, 99.8),
        _bar(99.8, 100, 99.0, 99.2),
        _bar(99.2, 99.5, 98.5, 98.7),
    ]
    trade = CampaignTrade(
        symbol="X", side="SHORT", entry=100.0, stop=102.0,
        bar_path=bars, atr_pct_at_entry=0.5, atr_abs_at_entry=0.5,
    )
    result = simulate_campaign(trade, max_adds=3, min_profit_for_add_pct=0.3)
    assert isinstance(result["adds_executed"], int)


# ---------------------------------------------------------------------------
# Same-bar STOP_BEFORE_TP for dynamic policies
# ---------------------------------------------------------------------------

def test_profit_lock_same_bar_stop_before_tp_long():
    """LONG entry 100, stop 99, tp2 102, lock_r=1.0. Bar touches both 99
    (current stop) and 102 (tp2). Conservative same-bar rule → stop wins.
    """
    from app.labs.profit_lock_simulator import (
        ExitTrade,
        POLICY_PROFIT_LOCK_1R,
        _simulate_policy,
    )

    bars = [_bar(100, 102.0, 99.0, 101.0)]
    trade = ExitTrade(
        symbol="X", side="LONG", entry=100.0, stop=99.0,
        tp1=101.0, tp2=102.0, bar_path=bars, fees_pct=0.0,
    )
    r = _simulate_policy(POLICY_PROFIT_LOCK_1R, trade)
    assert r["exit_reason"] == "STOP_LOSS"
    assert r["realized_net_pct"] == pytest.approx(-1.0)


def test_profit_lock_same_bar_stop_before_tp_short():
    """SHORT entry 100, stop 101, tp2 98. Bar touches both 101 (stop) and
    98 (tp2). Conservative same-bar rule → stop wins.
    """
    from app.labs.profit_lock_simulator import (
        ExitTrade,
        POLICY_PROFIT_LOCK_1R,
        _simulate_policy,
    )

    bars = [_bar(100, 101.0, 98.0, 99.0)]
    trade = ExitTrade(
        symbol="X", side="SHORT", entry=100.0, stop=101.0,
        tp1=99.0, tp2=98.0, bar_path=bars, fees_pct=0.0,
    )
    r = _simulate_policy(POLICY_PROFIT_LOCK_1R, trade)
    assert r["exit_reason"] == "STOP_LOSS"
    assert r["realized_net_pct"] == pytest.approx(-1.0)


def test_trailing_atr_same_bar_does_not_raise_within_bar():
    """LONG entry 100 with initial stop 99. A bar that touches 99 must hit
    the **original** stop, not a freshly raised trailing stop derived from
    the same bar's high.
    """
    from app.labs.profit_lock_simulator import (
        ExitTrade,
        POLICY_TRAILING_ATR,
        _simulate_policy,
    )

    bars = [_bar(100, 103.0, 99.0, 102.0)]
    trade = ExitTrade(
        symbol="X", side="LONG", entry=100.0, stop=99.0,
        tp1=101.0, tp2=102.0, bar_path=bars, fees_pct=0.0,
    )
    r = _simulate_policy(POLICY_TRAILING_ATR, trade)
    assert r["exit_reason"] == "TRAILING_STOP"
    # Stop at 99 → -1.0%
    assert r["realized_net_pct"] == pytest.approx(-1.0)


def test_partial_trailing_same_bar_stop_before_tp1_long():
    """LONG entry 100 with stop 99, tp1 101. Bar touches both → stop wins.

    V8.2.2 fix: when stop fires before TP1 has been taken (including the
    same-bar STOP_BEFORE_TP case), the loss applies to the **full**
    position, not 50%. Previously this test asserted -0.5 which reflected
    the legacy bug.
    """
    from app.labs.profit_lock_simulator import (
        ExitTrade,
        POLICY_PARTIAL_50_TRAILING,
        _simulate_policy,
    )

    bars = [_bar(100, 101.0, 99.0, 100.0)]
    trade = ExitTrade(
        symbol="X", side="LONG", entry=100.0, stop=99.0,
        tp1=101.0, tp2=102.0, bar_path=bars, fees_pct=0.0,
    )
    r = _simulate_policy(POLICY_PARTIAL_50_TRAILING, trade)
    assert r["exit_reason"] == "STOP_LOSS"
    # Full-position loss: (99-100)/100 = -1.0%
    assert r["realized_net_pct"] == pytest.approx(-1.0)


# ---------------------------------------------------------------------------
# Heavy guard
# ---------------------------------------------------------------------------

def test_endpoint_bidirectional_funnel_skips_heavy_without_flag():
    from app.health_server import _v82_bidirectional_funnel

    out = _v82_bidirectional_funnel(None, None, {"hours": ["720"]})
    assert out["status"] == "SKIPPED_HEAVY"
    assert out["research_only"] is True
    assert out["final_recommendation"] == "NO LIVE"


def test_endpoint_bidirectional_funnel_allows_heavy_with_flag():
    from app.health_server import _v82_bidirectional_funnel

    out = _v82_bidirectional_funnel(None, None, {"hours": ["720"], "allow_heavy": ["true"]})
    assert out.get("status") != "SKIPPED_HEAVY"


def test_endpoint_score_asymmetry_skips_heavy_without_flag():
    from app.health_server import _v82_score_asymmetry

    out = _v82_score_asymmetry(None, None, {"hours": ["720"]})
    assert out["status"] == "SKIPPED_HEAVY"
    assert out["final_recommendation"] == "NO LIVE"


def test_cli_warns_on_heavy_window():
    from app.research_lab import ResearchLab

    class _NoopDB:
        pass

    lab = ResearchLab(config=None, db=_NoopDB())
    out = lab.bidirectional_funnel(hours=720)
    assert "heavy_window_warning" in out


# ---------------------------------------------------------------------------
# CLI reporting — every V8.2 command must print all four safety lines
# ---------------------------------------------------------------------------

V82_CLI_METHODS = [
    ("bidirectional_funnel", {"hours": 168}),
    ("missed_opportunities_cli", {"hours": 168, "side": "SHORT"}),
    ("blocked_counterfactual_cli", {"hours": 168, "side": "SHORT"}),
    ("failed_executed_cli", {"hours": 168, "side": "SHORT"}),
    ("good_not_monetized_cli", {"hours": 168, "side": "SHORT"}),
    ("score_asymmetry_audit_cli", {"hours": 168}),
    ("score_symmetric_simulation_cli", {"hours": 168}),
    ("score_atr_softened_simulation_cli", {"hours": 168}),
    ("score_high_vol_directional_simulation_cli", {"hours": 168}),
    ("regime_router_simulation_cli", {"hours": 168}),
    ("trend_campaign_sim_cli", {"hours": 168, "side": "SHORT", "max_adds": 3}),
    ("profit_lock_sim_cli", {"hours": 168, "side": "SHORT", "policy": "all"}),
]


def test_all_cli_commands_print_full_safety_footer():
    from app.research_lab import ResearchLab

    class _NoopDB:
        pass

    lab = ResearchLab(config=None, db=_NoopDB())
    for method_name, kwargs in V82_CLI_METHODS:
        fn = getattr(lab, method_name)
        out = fn(**kwargs)
        assert "research_only: true" in out, f"{method_name} missing research_only"
        assert "paper_filter_enabled: false" in out, f"{method_name} missing paper_filter"
        assert "can_send_real_orders: false" in out, f"{method_name} missing can_send_real_orders"
        assert "final_recommendation: NO LIVE" in out, f"{method_name} missing NO LIVE"


# ---------------------------------------------------------------------------
# Safety AST scan over the touched modules
# ---------------------------------------------------------------------------

FORBIDDEN_CALL_NAMES = {
    "place_order", "set_leverage", "set_margin_mode",
    "private_get", "private_post",
}

FORBIDDEN_ASSIGN_LITERALS = {
    "LIVE_TRADING", "ENABLE_PAPER_POLICY_FILTER",
    "can_send_real_orders", "allow_real_writes",
}

V82_1_MODULES = [
    "app.labs.bidirectional_forensic_lab",
    "app.labs.score_asymmetry_audit",
    "app.labs.regime_router_simulator",
    "app.labs.trend_campaign_simulator",
    "app.labs.profit_lock_simulator",
    "app.labs.research_pack_bidirectional_v1",
]


def _module_path(modname: str) -> pathlib.Path:
    return pathlib.Path(importlib.import_module(modname).__file__)


def test_v82_1_modules_have_no_forbidden_calls():
    for mod in V82_1_MODULES:
        tree = ast.parse(_module_path(mod).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = getattr(fn, "attr", None) or getattr(fn, "id", None)
                assert name not in FORBIDDEN_CALL_NAMES, (
                    f"{mod} must not call {name}"
                )


def test_v82_1_modules_have_no_forbidden_literal_true_assigns():
    for mod in V82_1_MODULES:
        tree = ast.parse(_module_path(mod).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    name = getattr(tgt, "id", None) or getattr(tgt, "attr", None)
                    if name in FORBIDDEN_ASSIGN_LITERALS and isinstance(node.value, ast.Constant):
                        assert node.value.value is not True, (
                            f"{mod} contains forbidden {name}=True"
                        )
