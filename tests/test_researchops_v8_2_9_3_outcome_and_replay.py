"""V8.2.9.3 — Sign Integrity + Canonical Outcome + Bar-by-Bar Replay +
Strict OOS Canonical tests. All synthetic. No DB. No real OHLCV.
"""

from __future__ import annotations

import ast
import csv
import importlib
import inspect
import json
import pathlib
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest


def _ohlcv_path_long_winner(entry: float = 100.0, tp: float = 101.0,
                            sl: float = 99.0, bars: int = 6) -> list[dict]:
    """Path that hits TP on the second bar."""
    path = []
    last_close = entry
    for i in range(bars):
        high = entry * (1 + 0.002 * i)
        low = entry * (1 - 0.001 * i)
        close = entry * (1 + 0.001 * i)
        if i == 1:
            high = tp + 0.05
            close = tp
        path.append({"open": last_close, "high": high, "low": low, "close": close})
        last_close = close
    return path


def _ohlcv_path_long_loser(entry: float = 100.0, tp: float = 101.0,
                           sl: float = 99.0, bars: int = 6) -> list[dict]:
    """Path that hits SL on the second bar."""
    path = []
    last_close = entry
    for i in range(bars):
        high = entry * (1 + 0.0005 * i)
        low = entry * (1 - 0.001 * i)
        close = entry * (1 - 0.0008 * i)
        if i == 1:
            low = sl - 0.05
            close = sl
        path.append({"open": last_close, "high": high, "low": low, "close": close})
        last_close = close
    return path


def _ohlcv_path_long_horizon(entry: float = 100.0, bars: int = 6) -> list[dict]:
    """Path that drifts mildly and hits horizon close above entry."""
    path = []
    for i in range(bars):
        path.append({
            "open": entry + 0.05 * i,
            "high": entry + 0.10 * i,
            "low": entry - 0.05 * i,
            "close": entry + 0.05 * (i + 1),
        })
    return path


def _ohlcv_path_same_bar_ambiguous(entry: float = 100.0, tp: float = 101.0,
                                   sl: float = 99.0) -> list[dict]:
    """First bar reaches both TP and SL high/low. SL must win
    (STOP_BEFORE_TP)."""
    return [
        {"open": entry, "high": tp + 0.05, "low": sl - 0.05, "close": entry},
        {"open": entry, "high": entry, "low": entry, "close": entry},
    ]


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
    ohlcv_path: list[dict] | None = None,
) -> dict[str, Any]:
    if ts is None:
        ts = datetime(2026, 6, 1, tzinfo=timezone.utc)
    row = {
        "signal_id": id(ts),
        "timestamp": ts.isoformat(),
        "symbol": symbol,
        "side": side,
        "regime": regime,
        "regime_now": regime,
        "score": 80,
        "score_bucket": "80-89",
        "strategy": "TEST",
        "candidate_reason": "rebound_long_after_down_regime",
        "entry_price": entry,
        "take_profit_1": tp,
        "stop_loss": sl,
        "tp_price": tp,
        "sl_price": sl,
        "ohlcv_available": True,
        "ret_1h_pct": ret_1h,
        "ret_4h_pct": ret_4h,
        "ret_24h_pct": ret_4h * 2.0,
        "mfe_pct": mfe,
        "mae_pct": mae,
        "first_barrier_hit": first_barrier,
        "tp_before_sl": first_barrier == "TP",
        "sl_before_tp": first_barrier == "SL",
        "baseline_result": first_barrier,
        "baseline_gross_pnl": net_pnl + 0.46,
        "baseline_net_pnl_est": net_pnl,
        "net_pnl_est": net_pnl,
        "trailing_result": "trailing_proxy",
        "trailing_net_pnl_est": net_pnl + 0.1,
        "campaign_result": "1+1_proxy",
        "campaign_net_pnl_est": net_pnl * 1.3,
        "data_quality": "OK",
        "training_label": "GOOD_LONG" if net_pnl > 0 else "BAD_LONG",
        "final_use_for_training": True,
        "research_only": True,
        "final_recommendation": "NO LIVE",
    }
    if ohlcv_path is not None:
        row["ohlcv_path"] = ohlcv_path
    return row


# ---------------------------------------------------------------------------
# Sign Integrity
# ---------------------------------------------------------------------------

def test_sign_integrity_detects_future_return_disagrees_with_net_pnl():
    from app.labs.rebound_outcome_sign_integrity_v8_2_9_3 import (
        FUTURE_RETURN_DISAGREES_WITH_NET_PNL,
        audit_sign_integrity,
    )
    # LONG with ret_4h positive but baseline net negative → sign bug.
    candidate = _row(
        side="LONG", ret_1h=0.8, ret_4h=1.5, net_pnl=-0.80,
        first_barrier="SL", mfe=1.5, mae=-0.10,
    )
    r = audit_sign_integrity([candidate], dataset_rows=[candidate])
    assert FUTURE_RETURN_DISAGREES_WITH_NET_PNL in r.by_mismatch_type
    assert r.sign_bug_count >= 1


def test_sign_integrity_detects_baseline_field_mismatch():
    from app.labs.rebound_outcome_sign_integrity_v8_2_9_3 import (
        BASELINE_FIELD_MISMATCH,
        audit_sign_integrity,
    )
    raw = _row(net_pnl=0.50)
    candidate = dict(raw)
    candidate["net_pnl_est"] = 0.30  # Diverges from raw baseline.
    r = audit_sign_integrity([candidate], dataset_rows=[raw])
    assert BASELINE_FIELD_MISMATCH in r.by_mismatch_type


def test_sign_integrity_detects_net_pnl_sign_inverted():
    """LONG barrier=TP with strongly positive MFE but baseline net very
    negative → classifier triggers either NET_PNL_SIGN_INVERTED or
    BARRIER_DISAGREES_WITH_NET_PNL. The exact label depends on cascade
    priority but BOTH count as sign_bug. We keep ret_4h below the
    +0.50 threshold so the future-return rule does not fire first."""
    from app.labs.rebound_outcome_sign_integrity_v8_2_9_3 import (
        BARRIER_DISAGREES_WITH_NET_PNL,
        NET_PNL_SIGN_INVERTED,
        audit_sign_integrity,
    )
    candidate = _row(
        side="LONG", net_pnl=-0.80, first_barrier="TP",
        mfe=2.0, mae=-0.20, ret_1h=0.0, ret_4h=0.30,
    )
    r = audit_sign_integrity([candidate], dataset_rows=[candidate])
    assert (
        NET_PNL_SIGN_INVERTED in r.by_mismatch_type
        or BARRIER_DISAGREES_WITH_NET_PNL in r.by_mismatch_type
    )
    assert r.sign_bug_count >= 1


def test_sign_integrity_sign_ok_for_consistent_long():
    from app.labs.rebound_outcome_sign_integrity_v8_2_9_3 import (
        SIGN_OK,
        audit_sign_integrity,
    )
    candidate = _row(side="LONG", net_pnl=0.50, first_barrier="TP",
                     mfe=1.2, mae=-0.10, ret_4h=1.0)
    r = audit_sign_integrity([candidate], dataset_rows=[candidate])
    assert SIGN_OK in r.by_mismatch_type
    assert r.sign_bug_count == 0


# ---------------------------------------------------------------------------
# Canonical Outcome
# ---------------------------------------------------------------------------

def test_canonical_prefers_ohlcv_replay_over_baseline_net_pnl():
    from app.labs.outcome_field_canonicalizer_v8_2_9_3 import (
        CANONICAL_SOURCE_OHLCV,
        CANONICAL_STATUS_OK,
        canonicalize_row,
    )
    # Path produces ~+1% (TP). Baseline says +0.5%. OHLCV must win.
    row = _row(ohlcv_path=_ohlcv_path_long_winner(), net_pnl=0.50)
    c = canonicalize_row(row)
    assert c.canonical_source == CANONICAL_SOURCE_OHLCV
    assert c.canonical_outcome_status == CANONICAL_STATUS_OK
    assert c.canonical_net_pnl_est is not None
    # Replay path hits TP at price 101 → ~+1%, not 0.5%.
    assert c.canonical_net_pnl_est > 0.5


def test_canonical_falls_back_to_baseline_when_no_path():
    from app.labs.outcome_field_canonicalizer_v8_2_9_3 import (
        CANONICAL_SOURCE_BASELINE,
        CANONICAL_STATUS_OK,
        canonicalize_row,
    )
    row = _row(net_pnl=0.40)
    row.pop("ohlcv_path", None)
    c = canonicalize_row(row)
    assert c.canonical_source == CANONICAL_SOURCE_BASELINE
    assert c.canonical_outcome_status == CANONICAL_STATUS_OK
    assert c.canonical_net_pnl_est == pytest.approx(0.40)


def test_canonical_need_data_when_no_path_and_no_baseline():
    from app.labs.outcome_field_canonicalizer_v8_2_9_3 import (
        CANONICAL_SOURCE_NEED_DATA,
        CANONICAL_STATUS_NEED_DATA,
        canonicalize_row,
    )
    row = {
        "symbol": "BTCUSDT", "timestamp": "2026-06-01T00:00:00",
        "side": "LONG",
    }
    c = canonicalize_row(row)
    assert c.canonical_outcome_status == CANONICAL_STATUS_NEED_DATA
    assert c.canonical_source == CANONICAL_SOURCE_NEED_DATA
    assert c.canonical_net_pnl_est is None


def test_canonical_does_not_use_future_returns_as_source():
    from app.labs.outcome_field_canonicalizer_v8_2_9_3 import (
        CANONICAL_SOURCE_BASELINE,
        CANONICAL_SOURCE_FUTURE_RETURN_DIAGNOSTIC,
        CANONICAL_SOURCE_OHLCV,
        canonicalize_row,
    )
    # With baseline present, future returns must NOT take over.
    row = _row(net_pnl=0.40, ret_4h=2.0)
    row.pop("ohlcv_path", None)
    c = canonicalize_row(row)
    assert c.canonical_source == CANONICAL_SOURCE_BASELINE
    # Only when there is no baseline AND no path is the future-return
    # diagnostic source used — and even then the canonical net is None.
    row2 = {
        "symbol": "BTCUSDT", "timestamp": "2026-06-01T00:00:00",
        "side": "LONG", "ret_4h_pct": 2.0,
    }
    c2 = canonicalize_row(row2)
    assert c2.canonical_source == CANONICAL_SOURCE_FUTURE_RETURN_DIAGNOSTIC
    assert c2.canonical_net_pnl_est is None


def test_canonical_field_mismatch_when_candidate_disagrees_with_baseline():
    from app.labs.outcome_field_canonicalizer_v8_2_9_3 import (
        CANONICAL_STATUS_FIELD_MISMATCH,
        canonicalize_row,
    )
    row = _row(net_pnl=0.50)
    row.pop("ohlcv_path", None)
    row["net_pnl_est"] = 0.10  # Mismatches baseline.
    c = canonicalize_row(row)
    assert c.canonical_outcome_status == CANONICAL_STATUS_FIELD_MISMATCH


# ---------------------------------------------------------------------------
# Bar-by-Bar Exit Replay
# ---------------------------------------------------------------------------

def test_bar_by_bar_does_not_use_mfe_mae_as_input():
    """The replay engine's policy functions only read ``entry``, ``tp``,
    ``sl``, and the OHLCV path. MFE / MAE columns are never referenced
    as inputs."""
    import inspect as _inspect
    from app.labs.exit_bar_by_bar_replay_v8_2_9_3 import replay_long_policy
    src = _inspect.getsource(replay_long_policy)
    tree = ast.parse(src)
    body = list(tree.body[0].body)
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    for stmt in body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert node.value not in {"mfe_pct", "mae_pct"}, (
                    f"replay_long_policy reads forbidden field {node.value!r}"
                )


def test_bar_by_bar_same_bar_ambiguity_resolves_as_stop_before_tp():
    from app.labs.exit_bar_by_bar_replay_v8_2_9_3 import (
        SAME_BAR_AMBIGUITY_RULE,
        replay_long_baseline,
    )
    assert SAME_BAR_AMBIGUITY_RULE == "STOP_BEFORE_TP"
    path = _ohlcv_path_same_bar_ambiguous()
    result = replay_long_baseline(entry=100.0, tp=101.0, sl=99.0, path=path)
    assert result["exit_reason"] == "SL"
    assert result["same_bar_ambiguous"] is True
    assert result["net_pct"] < 0


def test_bar_by_bar_need_data_when_no_path():
    from app.labs.exit_bar_by_bar_replay_v8_2_9_3 import run_bar_by_bar_replay
    row = _row()
    row.pop("ohlcv_path", None)
    r = run_bar_by_bar_replay([row])
    assert r.replay_rows == 0
    assert r.need_data_rows == 1
    assert r.bar_by_bar_replay_available is False
    assert r.best_policy_bar_by_bar_status == "NEED_DATA"


def test_bar_by_bar_uses_path_winner_and_loser():
    from app.labs.exit_bar_by_bar_replay_v8_2_9_3 import (
        POLICY_BASELINE_ACTUAL,
        replay_long_baseline,
    )
    win = replay_long_baseline(
        100.0, 101.0, 99.0, _ohlcv_path_long_winner(),
    )
    lose = replay_long_baseline(
        100.0, 101.0, 99.0, _ohlcv_path_long_loser(),
    )
    assert win["exit_reason"] == "TP"
    assert win["net_pct"] > 0
    assert lose["exit_reason"] == "SL"
    assert lose["net_pct"] < 0


def test_bar_by_bar_policy_promotion_uses_train_only():
    """Train-only promotion: the best policy is chosen by the train
    slice's net_ev_after_cost. Test is single-shot and may FAIL even if
    train passes."""
    from app.labs.exit_bar_by_bar_replay_v8_2_9_3 import run_bar_by_bar_replay
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = []
    # 24 winners in train, 16 in val, then 20 losers in test.
    for i in range(40):
        rows.append(_row(
            ts=start + timedelta(hours=i),
            ohlcv_path=_ohlcv_path_long_winner(),
        ))
    for i in range(20):
        rows.append(_row(
            ts=start + timedelta(hours=40 + i),
            ohlcv_path=_ohlcv_path_long_loser(),
        ))
    r = run_bar_by_bar_replay(rows)
    assert r.replay_rows == 60
    assert r.bar_by_bar_replay_available is True
    # Best policy chosen on train: train slice was all winners.
    # Test slice has losers → status REJECT or WATCH_ONLY, NEVER an
    # automatic paper-sandbox promotion (the constant is
    # ``PAPER_SANDBOX_CANDIDATE_ONLY_IF_ALL_GATES_PASS`` which the gates
    # would only emit if everything passed).
    assert r.best_policy_bar_by_bar_status in {
        "REJECT", "WATCH_ONLY", "NEED_DATA",
    }


# ---------------------------------------------------------------------------
# Strict OOS Canonical
# ---------------------------------------------------------------------------

def test_strict_oos_canonical_blocks_when_sign_bug_ratio_exceeds_threshold():
    from app.labs.rebound_long_strict_oos_canonical_v8_2_9_3 import (
        MAX_SIGN_BUG_RATIO,
        run_strict_oos_canonical,
    )
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    # Mostly sign-suspect rows (LONG, ret_4h strongly positive, baseline
    # strongly negative) → canonical SIGN_SUSPECT triggers.
    rows = []
    for i in range(100):
        rows.append(_row(
            ts=start + timedelta(hours=i),
            side="LONG",
            ret_4h=2.0, net_pnl=-1.5,
            first_barrier="SL", mfe=0.2, mae=-2.0,
            regime="TREND_UP",
        ))
    r = run_strict_oos_canonical(rows)
    assert r.sign_bug_ratio > MAX_SIGN_BUG_RATIO
    assert r.final_status_top_level == "REJECT"
    assert r.rejected_for_sign_bug is True


def test_strict_oos_canonical_need_more_data_when_canonical_insufficient():
    """Rows without baseline or path → canonical NEED_DATA → low OK
    ratio → NEED_MORE_DATA top-level."""
    from app.labs.rebound_long_strict_oos_canonical_v8_2_9_3 import (
        run_strict_oos_canonical,
    )
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = []
    for i in range(60):
        rows.append({
            "symbol": "BTCUSDT", "side": "LONG",
            "timestamp": (start + timedelta(hours=i)).isoformat(),
            # No baseline, no path → NEED_DATA.
        })
    r = run_strict_oos_canonical(rows)
    assert r.canonical_ok_ratio == 0.0
    assert r.final_status_top_level == "NEED_MORE_DATA"
    assert r.rejected_for_canonical_insufficient is True


def test_strict_oos_canonical_passes_with_clean_mock():
    from app.labs.rebound_long_strict_oos_canonical_v8_2_9_3 import (
        run_strict_oos_canonical,
    )
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    rows = []
    for i in range(300):
        sym = symbols[i % 3]
        ts = start + timedelta(hours=i, minutes=(i * 7) % 60)
        # 75/25 wins/losses, baseline present, no ret_4h disagreement.
        net = 0.80 if (i % 4 != 0) else -0.30
        rows.append({
            "symbol": sym,
            "timestamp": ts.isoformat(),
            "side": "LONG",
            "regime": "TREND_UP", "regime_now": "TREND_UP",
            "regime_before": "TREND_DOWN",
            "volatility_bucket": "normal",
            "trend_recovering_prefix": True,
            "baseline_net_pnl_est": net,
            "net_pnl_est": net,
            "ret_4h_pct": net,  # Matches sign.
        })
    r = run_strict_oos_canonical(rows)
    assert r.canonical_ok_ratio > 0.3
    assert r.sign_bug_ratio == 0.0
    assert r.final_status_top_level == "PAPER_SANDBOX_CANDIDATE"


# ---------------------------------------------------------------------------
# Export V8.2.9.3
# ---------------------------------------------------------------------------

def test_export_v8293_emits_new_csvs(tmp_path):
    from app.labs.research_export_v8_2_9 import export_research_v829
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = []
    for i in range(60):
        rows.append(_row(
            ts=start + timedelta(hours=i),
            ohlcv_path=_ohlcv_path_long_winner(),
        ))
    base = tmp_path / "v8293_csvs"
    export_research_v829(None, rows=rows, base_dir=base)
    for name in (
        "rebound_outcome_sign_integrity_v1.csv",
        "canonical_outcome_v1.csv",
        "exit_bar_by_bar_replay_v1.csv",
        "rebound_long_strict_oos_canonical_v1.csv",
    ):
        assert (base / name).exists(), f"missing {name}"


def test_export_v8293_zip_only_allowed_extensions(tmp_path):
    from app.labs.research_export_v8_2_9 import export_research_v829
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = [_row(ts=start + timedelta(hours=i)) for i in range(40)]
    base = tmp_path / "v8293_zip"
    export_research_v829(None, rows=rows, base_dir=base)
    zip_path = base / "research_v8_2_9_exports.zip"
    assert zip_path.exists()
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
    for name in names:
        assert name.endswith((".csv", ".txt", ".json"))
    assert "rebound_outcome_sign_integrity_v1.csv" in names
    assert "canonical_outcome_v1.csv" in names
    assert "exit_bar_by_bar_replay_v1.csv" in names
    assert "rebound_long_strict_oos_canonical_v1.csv" in names


def test_export_v8293_summary_includes_new_keys(tmp_path):
    from app.labs.research_export_v8_2_9 import export_research_v829
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = [_row(ts=start + timedelta(hours=i)) for i in range(40)]
    base = tmp_path / "v8293_summary"
    export_research_v829(None, rows=rows, base_dir=base)
    summary = (base / "research_v8_2_9_summary.txt").read_text(encoding="utf-8")
    for key in (
        "sign_integrity_status",
        "sign_bug_ratio",
        "outcome_field_mismatch_ratio",
        "canonical_outcome_ok_ratio",
        "canonical_outcome_source_top",
        "bar_by_bar_replay_available",
        "best_policy_bar_by_bar",
        "best_policy_bar_by_bar_status",
        "strict_oos_canonical_status",
        "paper_sandbox_candidates_canonical",
        "final_recommendation: NO LIVE",
    ):
        assert key in summary, f"summary missing {key}"


def test_export_v8293_manifest_v3_bump(tmp_path):
    from app.labs.research_export_v8_2_9 import export_research_v829
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = [_row(ts=start + timedelta(hours=i)) for i in range(40)]
    base = tmp_path / "v8293_manifest"
    export_research_v829(None, rows=rows, base_dir=base)
    manifest = json.loads((base / "manifest_v1.json").read_text(encoding="utf-8"))
    # V8.2.9.3 introduced the v3 manifest; V8.2.9.4+ bumps further but
    # the V8.2.9.3-required keys must stay.
    assert manifest["version"] in {"v8.2.9.v3", "v8.2.9.v4", "v8.2.9.v5", "v8.2.9.v6"}
    for key in (
        "sign_integrity_status",
        "sign_bug_ratio",
        "outcome_field_mismatch_ratio",
        "canonical_outcome_ok_ratio",
        "canonical_outcome_source_top",
        "bar_by_bar_replay_available",
        "best_policy_bar_by_bar",
        "best_policy_bar_by_bar_status",
        "strict_oos_canonical_status",
        "paper_sandbox_candidates_canonical",
    ):
        assert key in manifest, f"manifest missing {key}"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_v8293_cli_commands_parse():
    from app.research_lab import build_argument_parser
    parser = build_argument_parser()
    for argv in [
        ["rebound-sign-integrity-v8293", "--hours", "168"],
        ["canonical-outcome-v8293", "--hours", "168"],
        ["exit-bar-replay-v8293", "--hours", "168"],
        ["rebound-strict-oos-canonical-v8293", "--hours", "168"],
    ]:
        ns = parser.parse_args(argv)
        assert ns.command == argv[0]


def test_v8293_parser_no_duplicate_option_strings():
    from app.research_lab import build_argument_parser
    parser = build_argument_parser()
    counts: dict[str, int] = {}
    for action in parser._actions:
        for opt in action.option_strings or []:
            counts[opt] = counts.get(opt, 0) + 1
    duplicates = [opt for opt, c in counts.items() if c > 1]
    assert not duplicates, f"Duplicate option strings: {duplicates}"


# ---------------------------------------------------------------------------
# Safety AST scan
# ---------------------------------------------------------------------------

FORBIDDEN_CALL_NAMES = {
    "place_order", "set_leverage", "set_margin_mode",
    "private_get", "private_post",
}
FORBIDDEN_ASSIGN_LITERALS = {
    "LIVE_TRADING", "ENABLE_PAPER_POLICY_FILTER",
    "can_send_real_orders", "allow_real_writes",
}

V8293_MODULES = [
    "app.labs.exit_bar_by_bar_replay_v8_2_9_3",
    "app.labs.outcome_field_canonicalizer_v8_2_9_3",
    "app.labs.rebound_outcome_sign_integrity_v8_2_9_3",
    "app.labs.rebound_long_strict_oos_canonical_v8_2_9_3",
]


def test_v8293_modules_have_no_forbidden_calls():
    for mod in V8293_MODULES:
        path = pathlib.Path(importlib.import_module(mod).__file__)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = getattr(fn, "attr", None) or getattr(fn, "id", None)
                assert name not in FORBIDDEN_CALL_NAMES, (
                    f"{mod} calls {name}"
                )


def test_v8293_modules_have_no_forbidden_literal_true_assigns():
    for mod in V8293_MODULES:
        path = pathlib.Path(importlib.import_module(mod).__file__)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    name = getattr(tgt, "id", None) or getattr(tgt, "attr", None)
                    if (
                        name in FORBIDDEN_ASSIGN_LITERALS
                        and isinstance(node.value, ast.Constant)
                        and node.value.value is True
                    ):
                        raise AssertionError(f"{mod} {name}=True")


def test_v8293_reports_carry_no_live():
    from app.labs.exit_bar_by_bar_replay_v8_2_9_3 import BarByBarReplayReport
    from app.labs.outcome_field_canonicalizer_v8_2_9_3 import CanonicalReport
    from app.labs.rebound_long_strict_oos_canonical_v8_2_9_3 import (
        StrictOosCanonicalReport,
    )
    from app.labs.rebound_outcome_sign_integrity_v8_2_9_3 import (
        SignIntegrityReport,
    )
    for inst in [
        BarByBarReplayReport(hours=1, generated_at="t"),
        CanonicalReport(hours=1, generated_at="t"),
        SignIntegrityReport(hours=1, generated_at="t"),
        StrictOosCanonicalReport(hours=1, generated_at="t"),
    ]:
        assert inst.research_only is True
        assert inst.paper_filter_enabled is False
        assert inst.can_send_real_orders is False
        assert inst.final_recommendation == "NO LIVE"
