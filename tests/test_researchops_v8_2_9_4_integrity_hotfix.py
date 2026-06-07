"""V8.2.9.4 — Sign Integrity Join Fix + Conservative Intrabar Trailing +
SHORT Canonical Replay tests. All synthetic. No DB. No real OHLCV.
"""

from __future__ import annotations

import ast
import importlib
import json
import pathlib
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest


def _row(
    *,
    ts: datetime | None = None,
    symbol: str = "BTCUSDT",
    side: str = "LONG",
    regime: str = "TREND_UP",
    entry: float = 100.0,
    tp: float = 101.0,
    sl: float = 99.0,
    ret_1h: float = 0.5,
    ret_4h: float = 1.0,
    mfe: float = 1.0,
    mae: float = -0.2,
    first_barrier: str = "TP",
    net_pnl: float = 0.50,
    signal_id: Any | None = None,
) -> dict[str, Any]:
    if ts is None:
        ts = datetime(2026, 6, 1, tzinfo=timezone.utc)
    return {
        "signal_id": signal_id if signal_id is not None else id(ts),
        "timestamp": ts.isoformat(),
        "symbol": symbol,
        "side": side,
        "regime": regime,
        "regime_now": regime,
        "candidate_reason": "rebound_long_after_down_regime",
        "entry_price": entry,
        "take_profit_1": tp,
        "stop_loss": sl,
        "tp_price": tp,
        "sl_price": sl,
        "ret_1h_pct": ret_1h,
        "ret_4h_pct": ret_4h,
        "mfe_pct": mfe,
        "mae_pct": mae,
        "first_barrier_hit": first_barrier,
        "tp_before_sl": first_barrier == "TP",
        "sl_before_tp": first_barrier == "SL",
        "baseline_result": first_barrier,
        "baseline_gross_pnl": net_pnl + 0.46,
        "baseline_net_pnl_est": net_pnl,
        "net_pnl_est": net_pnl,
        "training_label": "GOOD_LONG" if net_pnl > 0 else "BAD_LONG",
    }


# ---------------------------------------------------------------------------
# FIX 1 — Sign Integrity join
# ---------------------------------------------------------------------------

def test_join_uses_signal_id_when_available():
    """signal_id takes priority over (symbol, timestamp)."""
    from app.labs.rebound_outcome_sign_integrity_v8_2_9_3 import (
        JOIN_METHOD_SIGNAL_ID,
        _build_join_indexes,
        _join_raw_for_candidate,
    )
    raw = _row(signal_id="SIG-1", symbol="BTCUSDT")
    indexes = _build_join_indexes([raw])
    candidate = _row(signal_id="SIG-1", symbol="BTCUSDT")
    joined, method, ambiguous = _join_raw_for_candidate(candidate, indexes)
    assert joined is raw
    assert method == JOIN_METHOD_SIGNAL_ID
    assert ambiguous is False


def test_join_uses_symbol_timestamp_when_no_signal_id():
    from app.labs.rebound_outcome_sign_integrity_v8_2_9_3 import (
        JOIN_METHOD_SYMBOL_TIMESTAMP,
        _build_join_indexes,
        _join_raw_for_candidate,
    )
    ts = datetime(2026, 6, 1, tzinfo=timezone.utc)
    raw = _row(ts=ts, symbol="ETHUSDT")
    del raw["signal_id"]
    indexes = _build_join_indexes([raw])
    candidate = {"symbol": "ETHUSDT", "timestamp": ts.isoformat()}
    joined, method, ambiguous = _join_raw_for_candidate(candidate, indexes)
    assert joined is raw
    assert method == JOIN_METHOD_SYMBOL_TIMESTAMP


def test_join_timestamp_unique_fallback_only_when_no_symbol():
    """If the candidate has NO symbol AND the timestamp is unique, the
    fallback to timestamp-only join is allowed. Otherwise reject."""
    from app.labs.rebound_outcome_sign_integrity_v8_2_9_3 import (
        JOIN_METHOD_MISSING_OR_AMBIGUOUS,
        JOIN_METHOD_TIMESTAMP_UNIQUE_FALLBACK,
        _build_join_indexes,
        _join_raw_for_candidate,
    )
    ts = datetime(2026, 6, 1, tzinfo=timezone.utc)
    raw = _row(ts=ts, symbol="DOTUSDT")
    del raw["signal_id"]
    indexes = _build_join_indexes([raw])
    candidate_no_symbol = {"timestamp": ts.isoformat()}
    joined, method, _ = _join_raw_for_candidate(candidate_no_symbol, indexes)
    assert joined is raw
    assert method == JOIN_METHOD_TIMESTAMP_UNIQUE_FALLBACK
    # Candidate with a symbol must not fallback — symbol mismatch.
    candidate_with_symbol = {
        "symbol": "DOGEUSDT", "timestamp": ts.isoformat(),
    }
    joined2, method2, ambiguous2 = _join_raw_for_candidate(
        candidate_with_symbol, indexes,
    )
    assert joined2 is None
    assert method2 == JOIN_METHOD_MISSING_OR_AMBIGUOUS
    assert ambiguous2 is True


def test_join_refuses_duplicate_timestamp_without_symbol():
    """Two raw rows at the same timestamp + candidate without symbol/id
    → MISSING_OR_AMBIGUOUS_JOIN (no false fallback)."""
    from app.labs.rebound_outcome_sign_integrity_v8_2_9_3 import (
        JOIN_METHOD_MISSING_OR_AMBIGUOUS,
        _build_join_indexes,
        _join_raw_for_candidate,
    )
    ts = datetime(2026, 6, 1, tzinfo=timezone.utc)
    raw1 = _row(ts=ts, symbol="BTCUSDT")
    del raw1["signal_id"]
    raw2 = _row(ts=ts, symbol="DOTUSDT")
    del raw2["signal_id"]
    indexes = _build_join_indexes([raw1, raw2])
    candidate = {"timestamp": ts.isoformat()}
    joined, method, ambiguous = _join_raw_for_candidate(candidate, indexes)
    assert joined is None
    assert method == JOIN_METHOD_MISSING_OR_AMBIGUOUS
    assert ambiguous is True


def test_sign_integrity_does_not_cross_join_symbols_at_same_timestamp():
    """DOT candidate must NOT be audited against BTC raw row at the same
    timestamp. Without a (DOTUSDT, ts) match the result must be
    MISSING_OR_AMBIGUOUS_JOIN, never a false BASELINE_FIELD_MISMATCH."""
    from app.labs.rebound_outcome_sign_integrity_v8_2_9_3 import (
        BASELINE_FIELD_MISMATCH,
        MISSING_OR_AMBIGUOUS_JOIN,
        audit_sign_integrity,
    )
    ts = datetime(2026, 6, 1, tzinfo=timezone.utc)
    candidate_dot = _row(ts=ts, symbol="DOTUSDT", net_pnl=0.30)
    raw_btc = _row(ts=ts, symbol="BTCUSDT", net_pnl=0.99)
    # Strip signal_id so the only signal would be timestamp.
    del candidate_dot["signal_id"]
    del raw_btc["signal_id"]
    r = audit_sign_integrity([candidate_dot], dataset_rows=[raw_btc])
    assert MISSING_OR_AMBIGUOUS_JOIN in r.by_mismatch_type
    assert BASELINE_FIELD_MISMATCH not in r.by_mismatch_type
    assert r.ambiguous_join_count == 1
    assert r.sign_bug_count == 0


def test_sign_integrity_join_method_top_reported():
    from app.labs.rebound_outcome_sign_integrity_v8_2_9_3 import (
        JOIN_METHOD_SIGNAL_ID,
        audit_sign_integrity,
    )
    rows = []
    for i in range(5):
        ts = datetime(2026, 6, 1, tzinfo=timezone.utc) + timedelta(hours=i)
        rows.append(_row(ts=ts, signal_id=f"SIG-{i}"))
    r = audit_sign_integrity(rows, dataset_rows=rows)
    assert r.join_method_top == JOIN_METHOD_SIGNAL_ID
    assert r.by_join_method.get(JOIN_METHOD_SIGNAL_ID, 0) == 5


# ---------------------------------------------------------------------------
# FIX 2 — Conservative intrabar trailing
# ---------------------------------------------------------------------------

def test_trailing_does_not_use_current_bar_high_to_create_stop():
    """V8.2.9.4: single-bar (entry=100, high=110, low=99, close=105)
    under trailing_atr_soft must NOT exit at +9.45% (a trailing stop
    that the bar's own high would create). The trailing for bar 0 must
    use prior-bar info only (running_high_prev = entry)."""
    from app.labs.exit_bar_by_bar_replay_v8_2_9_3 import (
        POLICY_TRAILING_ATR_SOFT,
        replay_long_policy,
    )
    single_bar = [{"open": 100.0, "high": 110.0, "low": 99.0, "close": 105.0}]
    result = replay_long_policy(
        100.0, 120.0, 99.0, single_bar, POLICY_TRAILING_ATR_SOFT,
    )
    # Must NOT be the optimistic +9.45% (109.45 trailing from current high).
    assert result["net_pct"] is None or result["net_pct"] < 1.0
    # Specifically, the trailing should fall back to initial SL (99) on
    # bar 0, so the bar's low (99) triggers SL → net ≈ -1%.
    if result["net_pct"] is not None:
        assert result["net_pct"] == pytest.approx(-1.0, abs=0.05)
    assert result["exit_reason"] != "TRAILING_SL_AMBIGUOUS" or (
        result["net_pct"] is not None and result["net_pct"] < 0
    )


def test_trailing_can_execute_stop_set_by_previous_bar():
    """If a PRIOR bar established a high, trailing ratchet activates
    from the next bar. Bar 1's low can hit a stop computed from bar 0's
    high."""
    from app.labs.exit_bar_by_bar_replay_v8_2_9_3 import (
        POLICY_TRAILING_ATR_SOFT,
        replay_long_policy,
    )
    # Bar 0 makes a high of 110, closes at 109.
    # Bar 1: trailing should activate at 110 * 0.995 = 109.45.
    path = [
        {"open": 100, "high": 110, "low": 100, "close": 109},
        {"open": 109, "high": 109.5, "low": 108, "close": 108.5},
    ]
    result = replay_long_policy(
        100.0, 120.0, 99.0, path, POLICY_TRAILING_ATR_SOFT,
    )
    assert result["exit_bar_index"] == 1
    assert result["exit_reason"] == "TRAILING_SL"
    assert result["net_pct"] == pytest.approx(9.45, abs=0.05)


def test_trailing_same_bar_tp_and_trailing_uses_stop_before_tp():
    """If a prior bar set a trailing stop and the current bar's high
    also reaches TP, ambiguity resolves as STOP_BEFORE_TP — SL/trailing
    wins."""
    from app.labs.exit_bar_by_bar_replay_v8_2_9_3 import (
        POLICY_TRAILING_ATR_SOFT,
        replay_long_policy,
    )
    # Bar 0: high=110 establishes running_high_prev = 110.
    # Bar 1: high=120 hits TP=115 AND low=108 hits trailing=109.45.
    path = [
        {"open": 100, "high": 110, "low": 100, "close": 109},
        {"open": 109, "high": 120, "low": 108, "close": 115},
    ]
    result = replay_long_policy(
        100.0, 115.0, 99.0, path, POLICY_TRAILING_ATR_SOFT,
    )
    # STOP_BEFORE_TP → trailing wins.
    assert result["same_bar_ambiguous"] is True
    assert result["exit_reason"] == "TRAILING_SL_AMBIGUOUS"
    assert result["net_pct"] == pytest.approx(9.45, abs=0.05)


def test_profit_lock_does_not_activate_in_first_bar():
    """profit_lock_after_mfe activates only when a PRIOR bar's high
    crossed +0.50%. A single-bar replay with high=110 cannot activate
    profit lock in that same bar."""
    from app.labs.exit_bar_by_bar_replay_v8_2_9_3 import (
        POLICY_PROFIT_LOCK_MFE,
        replay_long_policy,
    )
    single_bar = [{"open": 100, "high": 110, "low": 99, "close": 105}]
    result = replay_long_policy(
        100.0, 120.0, 99.0, single_bar, POLICY_PROFIT_LOCK_MFE,
    )
    # Trailing stays at initial SL → low=99 hits SL → -1%.
    assert result["net_pct"] == pytest.approx(-1.0, abs=0.05)


# ---------------------------------------------------------------------------
# FIX 3 — SHORT canonical OHLCV replay
# ---------------------------------------------------------------------------

def test_short_baseline_tp_winner():
    """SHORT: low <= tp triggers TP, PnL is positive."""
    from app.labs.exit_bar_by_bar_replay_v8_2_9_3 import replay_short_baseline
    path = [
        {"open": 100, "high": 100.5, "low": 100, "close": 100},
        {"open": 100, "high": 100, "low": 98.5, "close": 99},
    ]
    result = replay_short_baseline(100.0, 99.0, 101.0, path)
    assert result["exit_reason"] == "TP"
    assert result["net_pct"] == pytest.approx(1.0, abs=0.05)
    assert result["net_pct"] > 0


def test_short_baseline_sl_loser():
    """SHORT: high >= sl triggers SL, PnL is negative."""
    from app.labs.exit_bar_by_bar_replay_v8_2_9_3 import replay_short_baseline
    path = [
        {"open": 100, "high": 101.5, "low": 100, "close": 101},
    ]
    result = replay_short_baseline(100.0, 99.0, 101.0, path)
    assert result["exit_reason"] == "SL"
    assert result["net_pct"] == pytest.approx(-1.0, abs=0.05)
    assert result["net_pct"] < 0


def test_short_same_bar_stop_before_tp():
    """SHORT same-bar ambiguity: high >= sl AND low <= tp → SL wins."""
    from app.labs.exit_bar_by_bar_replay_v8_2_9_3 import replay_short_baseline
    path = [
        {"open": 100, "high": 101.5, "low": 98.5, "close": 100},
    ]
    result = replay_short_baseline(100.0, 99.0, 101.0, path)
    assert result["exit_reason"] == "SL"
    assert result["same_bar_ambiguous"] is True
    assert result["net_pct"] < 0


def test_canonical_short_uses_ohlcv_replay():
    from app.labs.outcome_field_canonicalizer_v8_2_9_3 import (
        CANONICAL_SOURCE_OHLCV,
        CANONICAL_STATUS_OK,
        canonicalize_row,
    )
    short_path = [
        {"open": 100, "high": 100, "low": 98.5, "close": 99},
    ]
    row = {
        "symbol": "BTCUSDT",
        "timestamp": "2026-06-01T00:00:00",
        "side": "SHORT",
        "entry_price": 100.0,
        "tp_price": 99.0,
        "sl_price": 101.0,
        "ohlcv_path": short_path,
    }
    c = canonicalize_row(row)
    assert c.canonical_source == CANONICAL_SOURCE_OHLCV
    assert c.canonical_outcome_status == CANONICAL_STATUS_OK
    assert c.canonical_win is True
    assert c.canonical_net_pnl_est is not None
    assert c.canonical_net_pnl_est > 0


def test_canonical_short_loser():
    from app.labs.outcome_field_canonicalizer_v8_2_9_3 import (
        CANONICAL_SOURCE_OHLCV,
        canonicalize_row,
    )
    short_path = [
        {"open": 100, "high": 101.5, "low": 100, "close": 101},
    ]
    row = {
        "symbol": "BTCUSDT",
        "timestamp": "2026-06-01T00:00:00",
        "side": "SHORT",
        "entry_price": 100.0,
        "tp_price": 99.0,
        "sl_price": 101.0,
        "ohlcv_path": short_path,
    }
    c = canonicalize_row(row)
    assert c.canonical_source == CANONICAL_SOURCE_OHLCV
    assert c.canonical_win is False
    assert c.canonical_net_pnl_est < 0


def test_canonical_short_inverted_barriers_field_mismatch():
    """SHORT barriers inverted (tp > entry > sl) → FIELD_MISMATCH."""
    from app.labs.outcome_field_canonicalizer_v8_2_9_3 import (
        CANONICAL_STATUS_FIELD_MISMATCH,
        canonicalize_row,
    )
    row = {
        "symbol": "BTCUSDT",
        "timestamp": "2026-06-01T00:00:00",
        "side": "SHORT",
        "entry_price": 100.0,
        # SHORT requires tp < entry < sl. These are inverted (LONG-like).
        "tp_price": 101.0,
        "sl_price": 99.0,
        "ohlcv_path": [
            {"open": 100, "high": 101.5, "low": 98.5, "close": 100},
        ],
    }
    c = canonicalize_row(row)
    assert c.canonical_outcome_status == CANONICAL_STATUS_FIELD_MISMATCH


def test_canonical_long_behaviour_preserved():
    """Existing LONG behaviour must still work after V8.2.9.4."""
    from app.labs.outcome_field_canonicalizer_v8_2_9_3 import (
        CANONICAL_SOURCE_OHLCV,
        CANONICAL_STATUS_OK,
        canonicalize_row,
    )
    long_path = [
        {"open": 100, "high": 100, "low": 100, "close": 100},
        {"open": 100, "high": 101.05, "low": 99.9, "close": 101},
    ]
    row = {
        "symbol": "BTCUSDT",
        "timestamp": "2026-06-01T00:00:00",
        "side": "LONG",
        "entry_price": 100.0,
        "tp_price": 101.0,
        "sl_price": 99.0,
        "ohlcv_path": long_path,
    }
    c = canonicalize_row(row)
    assert c.canonical_source == CANONICAL_SOURCE_OHLCV
    assert c.canonical_outcome_status == CANONICAL_STATUS_OK
    assert c.canonical_win is True


def test_canonical_short_future_returns_never_canonical_when_path_present():
    """Even with ret_4h strongly negative, with a valid OHLCV path the
    canonical source must be OHLCV — never FUTURE_RETURN_DIAGNOSTIC."""
    from app.labs.outcome_field_canonicalizer_v8_2_9_3 import (
        CANONICAL_SOURCE_OHLCV,
        canonicalize_row,
    )
    short_path = [
        {"open": 100, "high": 100, "low": 98.5, "close": 99},
    ]
    row = {
        "symbol": "BTCUSDT",
        "timestamp": "2026-06-01T00:00:00",
        "side": "SHORT",
        "entry_price": 100.0,
        "tp_price": 99.0,
        "sl_price": 101.0,
        "ohlcv_path": short_path,
        "ret_4h_pct": -2.0,
    }
    c = canonicalize_row(row)
    assert c.canonical_source == CANONICAL_SOURCE_OHLCV


# ---------------------------------------------------------------------------
# FIX 4 — Export trazability + manifest bump
# ---------------------------------------------------------------------------

def test_export_v8294_summary_contains_new_flags(tmp_path):
    from app.labs.research_export_v8_2_9 import export_research_v829
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = [_row(ts=start + timedelta(hours=i)) for i in range(40)]
    base = tmp_path / "v8294_summary"
    export_research_v829(None, rows=rows, base_dir=base)
    summary = (base / "research_v8_2_9_summary.txt").read_text(
        encoding="utf-8",
    )
    for key in (
        "sign_integrity_join_method_top:",
        "sign_integrity_ambiguous_join_count:",
        "bar_replay_intrabar_rule: STOP_BEFORE_TP",
        "bar_replay_trailing_uses_previous_bar_only: true",
        "canonical_supports_short_ohlcv_replay: true",
    ):
        assert key in summary, f"summary missing {key}"


def test_export_v8294_manifest_v4_bump(tmp_path):
    from app.labs.research_export_v8_2_9 import export_research_v829
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = [_row(ts=start + timedelta(hours=i)) for i in range(40)]
    base = tmp_path / "v8294_manifest"
    export_research_v829(None, rows=rows, base_dir=base)
    manifest = json.loads(
        (base / "manifest_v1.json").read_text(encoding="utf-8"),
    )
    assert manifest["version"] in {"v8.2.9.v4", "v8.2.9.v5", "v8.2.9.v6"}
    assert manifest["bar_replay_intrabar_rule"] == "STOP_BEFORE_TP"
    assert manifest["bar_replay_trailing_uses_previous_bar_only"] is True
    assert manifest["canonical_supports_short_ohlcv_replay"] is True
    assert "sign_integrity_join_method_top" in manifest
    assert "sign_integrity_ambiguous_join_count" in manifest


def test_export_v8294_zip_only_allowed_extensions(tmp_path):
    from app.labs.research_export_v8_2_9 import export_research_v829
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = [_row(ts=start + timedelta(hours=i)) for i in range(40)]
    base = tmp_path / "v8294_zip"
    export_research_v829(None, rows=rows, base_dir=base)
    zip_path = base / "research_v8_2_9_exports.zip"
    assert zip_path.exists()
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            assert name.endswith((".csv", ".txt", ".json"))


# ---------------------------------------------------------------------------
# Safety / regression
# ---------------------------------------------------------------------------

def test_v8294_parser_no_duplicate_option_strings():
    from app.research_lab import build_argument_parser
    parser = build_argument_parser()
    counts: dict[str, int] = {}
    for action in parser._actions:
        for opt in action.option_strings or []:
            counts[opt] = counts.get(opt, 0) + 1
    duplicates = [opt for opt, c in counts.items() if c > 1]
    assert not duplicates, f"Duplicate option strings: {duplicates}"


V8294_TOUCHED_MODULES = [
    "app.labs.exit_bar_by_bar_replay_v8_2_9_3",
    "app.labs.outcome_field_canonicalizer_v8_2_9_3",
    "app.labs.rebound_outcome_sign_integrity_v8_2_9_3",
    "app.labs.research_export_v8_2_9",
]


def test_v8294_modules_have_no_forbidden_calls():
    forbidden = {
        "place_order", "set_leverage", "set_margin_mode",
        "private_get", "private_post",
    }
    for mod in V8294_TOUCHED_MODULES:
        path = pathlib.Path(importlib.import_module(mod).__file__)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = getattr(fn, "attr", None) or getattr(fn, "id", None)
                assert name not in forbidden, f"{mod} calls {name}"


def test_v8294_modules_have_no_forbidden_literal_true_assigns():
    forbidden = {
        "LIVE_TRADING", "ENABLE_PAPER_POLICY_FILTER",
        "can_send_real_orders", "allow_real_writes",
    }
    for mod in V8294_TOUCHED_MODULES:
        path = pathlib.Path(importlib.import_module(mod).__file__)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    name = getattr(tgt, "id", None) or getattr(tgt, "attr", None)
                    if (
                        name in forbidden
                        and isinstance(node.value, ast.Constant)
                        and node.value.value is True
                    ):
                        raise AssertionError(f"{mod} {name}=True")
