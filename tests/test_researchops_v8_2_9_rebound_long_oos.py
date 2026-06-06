"""V8.2.9 — Rebound LONG Strict OOS + EdgeGuard Repeat Dedup +
Score Gate Sandbox + Exit Monetization + Adversarial Audit tests.

All tests use synthetic dataset rows. No DB. No OHLCV reads.
"""

from __future__ import annotations

import ast
import csv
import importlib
import inspect
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
    regime: str = "RISK_ON",
    score: int = 80,
    strategy: str = "TEST",
    entry: float = 100.0,
    tp: float = 101.0,
    sl: float = 99.0,
    ret_1h: float = 0.5,
    ret_4h: float = 1.0,
    mfe: float = 1.0,
    mae: float = -0.2,
    first_barrier: str = "TP",
    net_pnl: float = 0.50,
    gross: float = 0.96,
    label: str = "GOOD_LONG",
    normalized_atr: float = 0.02,
    edgeguard_reason: str = "",
    source: str = "",
    bars: int = 6,
    tp_pct: float = 1.0,
    sl_pct: float = -1.0,
    closed_by_horizon: bool = False,
) -> dict[str, Any]:
    bucket = (
        "90-100" if score >= 90 else
        "80-89" if score >= 80 else
        "70-79" if score >= 70 else
        "60-69" if score >= 60 else "<60"
    )
    if ts is None:
        ts = datetime(2026, 6, 1, tzinfo=timezone.utc)
    return {
        "signal_id": id(ts),
        "timestamp": ts.isoformat(),
        "symbol": symbol,
        "side": side,
        "regime": regime,
        "score": score,
        "score_bucket": bucket,
        "strategy": strategy,
        "reason": "",
        "blocked_by": "",
        "edgeguard_reason": edgeguard_reason,
        "source": source,
        "candidate_selected": False,
        "risk_approved": False,
        "entry_price": entry,
        "take_profit_1": tp,
        "stop_loss": sl,
        "normalized_atr": normalized_atr,
        "ohlcv_available": True,
        "ret_15m_pct": ret_1h * 0.5,
        "ret_30m_pct": ret_1h * 0.75,
        "ret_1h_pct": ret_1h,
        "ret_4h_pct": ret_4h,
        "ret_24h_pct": ret_4h * 2.0,
        "mfe_pct": mfe,
        "mae_pct": mae,
        "first_barrier_hit": first_barrier,
        "tp_before_sl": first_barrier == "TP",
        "sl_before_tp": first_barrier == "SL",
        "baseline_result": first_barrier,
        "baseline_gross_pnl": gross,
        "baseline_net_pnl_est": net_pnl,
        "trailing_result": "trailing_proxy",
        "trailing_net_pnl_est": net_pnl + 0.1,
        "campaign_result": "1+1_proxy",
        "campaign_net_pnl_est": net_pnl * 1.3,
        "would_have_worked_baseline": net_pnl > 0,
        "would_have_worked_trailing": True,
        "would_have_worked_campaign": net_pnl > 0,
        "data_quality": "OK",
        "label_confidence": 0.8,
        "training_label": label,
        "final_use_for_training": True,
        "research_only": True,
        "final_recommendation": "NO LIVE",
        "bars": bars,
        "tp_pct": tp_pct,
        "sl_pct": sl_pct,
        "closed_by_horizon": closed_by_horizon,
        "exit_reason": "HORIZON_CLOSE" if closed_by_horizon else "",
        "entry_time": ts.isoformat(),
        "exit_time": (ts + timedelta(minutes=bars * 5)).isoformat(),
        "exit_price": entry * (1 + (net_pnl / 100.0)),
        "net_pct": net_pnl,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _detector_body_literals() -> list[str]:
    from app.labs.rebound_long_candidate_extractor_v8_2_9 import (
        _build_prefix_context,
        detect_rebound_long_prefix_only,
    )
    out: list[str] = []
    for fn in (detect_rebound_long_prefix_only, _build_prefix_context):
        tree = ast.parse(inspect.getsource(fn))
        if not tree.body:
            continue
        body = list(tree.body[0].body)
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            body = body[1:]
        for stmt in body:
            for node in ast.walk(stmt):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    out.append(node.value)
    return out


# ---------------------------------------------------------------------------
# Rebound LONG Candidate Extractor
# ---------------------------------------------------------------------------

def test_extractor_does_not_reference_ret_fields_in_code():
    literals = _detector_body_literals()
    bad = [s for s in literals if s.startswith("ret_") and s.endswith("_pct")]
    assert not bad, f"prefix-only detector references ret_*_pct fields: {bad}"


def test_extractor_does_not_reference_mfe_mae_barrier_baseline():
    literals = _detector_body_literals()
    forbidden = {
        "mfe_pct", "mae_pct",
        "first_barrier_hit", "tp_before_sl", "sl_before_tp",
        "baseline_result", "baseline_gross_pnl", "baseline_net_pnl_est",
        "trailing_result", "trailing_net_pnl_est",
        "campaign_result", "campaign_net_pnl_est",
        "training_label",
    }
    bad = [s for s in literals if s in forbidden]
    assert not bad, f"prefix-only detector references forbidden ex-post: {bad}"


def test_extractor_outcome_evaluation_is_separated():
    """``detect_rebound_long_prefix_only`` and ``evaluate_long_outcome``
    are physically separate functions. Detection never invokes outcome
    eval — verified by AST scan."""
    from app.labs.rebound_long_candidate_extractor_v8_2_9 import (
        detect_rebound_long_prefix_only,
    )
    src = inspect.getsource(detect_rebound_long_prefix_only)
    assert "evaluate_long_outcome" not in src
    assert "baseline_net_pnl_est" not in src


def test_extractor_detects_long_rebound_after_down_regime():
    from app.labs.rebound_long_candidate_extractor_v8_2_9 import (
        extract_rebound_long_candidates,
    )
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows: list[dict[str, Any]] = []
    for i in range(10):
        rows.append(_row(
            ts=start + timedelta(hours=i),
            symbol="BTCUSDT", side="LONG", regime="TREND_DOWN",
            entry=100.0 - i * 0.5,
        ))
    rows.append(_row(
        ts=start + timedelta(hours=10),
        symbol="BTCUSDT", side="LONG", regime="TREND_UP",
        entry=98.0, net_pnl=0.40,
    ))
    r = extract_rebound_long_candidates(None, rows=rows)
    assert r.candidates_count >= 1
    assert r.used_future_return_features is False
    first = r.candidates[0]
    assert first["detection_mode"] == "prefix_only"
    assert first["used_future_return_features"] is False
    assert first["outcome_winner_loser"] in {"winner", "loser", "unknown"}


def test_extractor_excludes_market_probe_rows():
    from app.labs.rebound_long_candidate_extractor_v8_2_9 import (
        CANDIDATE_REASON_MARKET_PROBE,
        extract_rebound_long_candidates,
    )
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = []
    for i in range(10):
        rows.append(_row(
            ts=start + timedelta(hours=i),
            symbol="BTCUSDT", side="LONG", regime="TREND_DOWN",
            entry=100.0 - i * 0.4,
        ))
    rows.append(_row(
        ts=start + timedelta(hours=10),
        symbol="BTCUSDT", side="LONG", regime="TREND_UP",
        entry=98.0, source="market_probe",
    ))
    r = extract_rebound_long_candidates(None, rows=rows)
    assert r.by_candidate_reason.get(CANDIDATE_REASON_MARKET_PROBE, 0) >= 1


# ---------------------------------------------------------------------------
# EdgeGuard Repeat Dedup
# ---------------------------------------------------------------------------

def test_dedup_reduces_edgeguard_repeated_blocks():
    from app.labs.edgeguard_repeat_dedup_v8_2_9 import dedup_edgeguard_repeats
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = []
    for _ in range(8):
        rows.append(_row(
            ts=start, symbol="BTCUSDT", side="LONG",
            regime="RISK_ON", strategy="X", entry=100.0,
            edgeguard_reason="edge_guard_watch_only",
        ))
    rows.append(_row(
        ts=start + timedelta(minutes=10), symbol="BTCUSDT",
        side="LONG", regime="RISK_ON", strategy="X", entry=101.0,
        edgeguard_reason="edge_guard_watch_only",
    ))
    dedup, report = dedup_edgeguard_repeats(rows)
    assert report.raw_rows == 9
    assert report.dedup_rows == 2
    assert report.edgeguard_repeat_blocks_removed >= 1
    assert 0.6 < report.duplicate_ratio_before <= 1.0
    assert report.duplicate_ratio_after == 0.0


def test_dedup_keeps_independent_observations():
    from app.labs.edgeguard_repeat_dedup_v8_2_9 import dedup_edgeguard_repeats
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = [
        _row(ts=start + timedelta(hours=0), symbol="BTCUSDT",
             entry=100.0, edgeguard_reason="x"),
        _row(ts=start + timedelta(hours=1), symbol="ETHUSDT",
             entry=200.0, edgeguard_reason="x"),
        _row(ts=start + timedelta(hours=2), symbol="SOLUSDT",
             entry=50.0, edgeguard_reason="y"),
    ]
    dedup, report = dedup_edgeguard_repeats(rows)
    assert report.dedup_rows == 3
    assert all(r.get("edgeguard_repeat_seen_again") is False for r in dedup)


# ---------------------------------------------------------------------------
# Score Gate Sandbox
# ---------------------------------------------------------------------------

def test_score_sandbox_no_gate_increases_samples():
    """no_score_gate variant should include all candidates regardless of
    score, but does NOT activate anything in production."""
    from app.labs.score_gate_sandbox_v8_2_9 import (
        SCORE_GATE_CURRENT_72,
        SCORE_GATE_NO_GATE,
        run_score_gate_sandbox,
    )
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    candidates: list[dict[str, Any]] = []
    for i in range(30):
        # Low score → blocked by current 72 gate, included by no_gate.
        candidates.append({
            "symbol": "BTCUSDT", "side": "LONG",
            "timestamp": (start + timedelta(hours=i)).isoformat(),
            "score": 50, "net_pnl_est": 0.40,
        })
    r = run_score_gate_sandbox(candidates, score_anti_calibrated=True)
    by_variant = {v["variant"]: v for v in r.variants}
    assert by_variant[SCORE_GATE_NO_GATE]["samples"] == 30
    assert by_variant[SCORE_GATE_CURRENT_72]["samples"] == 0
    assert r.score_used_as_positive_gate is False


def test_score_sandbox_does_not_mutate_production_state():
    """Sandbox runs purely on the given candidate list. Imports do not
    pull in any execution / paper / trader modules."""
    from app.labs import score_gate_sandbox_v8_2_9 as mod
    src = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "PaperTrader", "ExecutionEngine", "place_order",
        "private_get", "private_post", "set_leverage", "set_margin_mode",
        "LIVE_TRADING = True",
        "ENABLE_PAPER_POLICY_FILTER = True",
        "can_send_real_orders = True",
        "allow_real_writes = True",
        "apply=True",
    ):
        assert forbidden not in src, (
            f"score gate sandbox references forbidden: {forbidden}"
        )


# ---------------------------------------------------------------------------
# Strict OOS Rebound
# ---------------------------------------------------------------------------

def test_strict_oos_need_more_data_with_small_sample():
    from app.labs.rebound_long_strict_oos_v8_2_9 import (
        STATUS_NEED_MORE_DATA,
        run_strict_oos_rebound,
    )
    candidates = []
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    for i in range(10):
        candidates.append({
            "symbol": "BTCUSDT",
            "timestamp": (start + timedelta(hours=i)).isoformat(),
            "regime_before": "TREND_DOWN", "regime_now": "TREND_UP",
            "volatility_bucket": "normal",
            "trend_recovering_prefix": True,
            "net_pnl_est": 0.40,
        })
    r = run_strict_oos_rebound(candidates)
    assert r.final_status_top_level == STATUS_NEED_MORE_DATA


def test_strict_oos_rejects_when_test_negative():
    """Train positive, test negative → REJECT."""
    from app.labs.rebound_long_strict_oos_v8_2_9 import (
        STATUS_REJECT,
        run_strict_oos_rebound,
    )
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    candidates = []
    # 60 train, 20 val, 20 test = 100 rows
    for i in range(60):
        candidates.append({
            "symbol": "BTCUSDT",
            "timestamp": (start + timedelta(hours=i)).isoformat(),
            "regime_before": "TREND_DOWN", "regime_now": "TREND_UP",
            "volatility_bucket": "normal",
            "trend_recovering_prefix": True,
            "net_pnl_est": 0.80,
        })
    for i in range(20):
        candidates.append({
            "symbol": "BTCUSDT",
            "timestamp": (start + timedelta(hours=60 + i)).isoformat(),
            "regime_before": "TREND_DOWN", "regime_now": "TREND_UP",
            "volatility_bucket": "normal",
            "trend_recovering_prefix": True,
            "net_pnl_est": 0.70,
        })
    for i in range(20):
        candidates.append({
            "symbol": "BTCUSDT",
            "timestamp": (start + timedelta(hours=80 + i)).isoformat(),
            "regime_before": "TREND_DOWN", "regime_now": "TREND_UP",
            "volatility_bucket": "normal",
            "trend_recovering_prefix": True,
            "net_pnl_est": -0.30,
        })
    r = run_strict_oos_rebound(candidates)
    assert r.final_status_top_level == STATUS_REJECT


def test_strict_oos_can_mark_paper_sandbox_candidate():
    """Clean mock that passes all gates → PAPER_SANDBOX_CANDIDATE.

    Uses a mix of wins and losses so PF is well-defined (75% wins of
    +0.80, 25% losses of -0.30 → PF≈8). 300 rows × 3 symbols → per-rule
    train=60 / val=20 / test=20, all above gate minima."""
    from app.labs.rebound_long_strict_oos_v8_2_9 import (
        STATUS_PAPER_SANDBOX_CANDIDATE,
        run_strict_oos_rebound,
    )
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    candidates = []
    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    for i in range(300):
        sym = symbols[i % 3]
        ts = start + timedelta(hours=i, minutes=(i * 7) % 60)
        # 75% wins, 25% losses.
        net = 0.80 if (i % 4 != 0) else -0.30
        candidates.append({
            "symbol": sym,
            "timestamp": ts.isoformat(),
            "regime_before": "TREND_DOWN", "regime_now": "TREND_UP",
            "volatility_bucket": "normal",
            "trend_recovering_prefix": True,
            "net_pnl_est": net,
        })
    r = run_strict_oos_rebound(candidates)
    assert r.final_status_top_level == STATUS_PAPER_SANDBOX_CANDIDATE
    assert r.score_used_as_gate is False
    assert r.research_only is True


def test_strict_oos_high_duplicate_ratio_blocks_paper_sandbox():
    """duplicate_ratio_after > 0.30 must block PAPER_SANDBOX."""
    from app.labs.rebound_long_strict_oos_v8_2_9 import (
        STATUS_REJECT,
        run_strict_oos_rebound,
    )
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    candidates = []
    for i in range(200):
        ts = start + timedelta(hours=i, minutes=(i * 7) % 60)
        candidates.append({
            "symbol": "BTCUSDT",
            "timestamp": ts.isoformat(),
            "regime_before": "TREND_DOWN", "regime_now": "TREND_UP",
            "volatility_bucket": "normal",
            "trend_recovering_prefix": True,
            "net_pnl_est": 0.80,
        })
    r = run_strict_oos_rebound(candidates, duplicate_ratio_after=0.85)
    assert r.final_status_top_level == STATUS_REJECT


def test_strict_oos_single_symbol_when_symbol_excluded_marks_research_only():
    """If symbol is NOT in the grouping features and a single symbol
    dominates the test slice → SINGLE_SYMBOL_RESEARCH_ONLY (downgraded
    to research_candidate). Uses mixed wins/losses so PF is computable."""
    from app.labs.rebound_long_strict_oos_v8_2_9 import (
        STATUS_PAPER_SANDBOX_CANDIDATE,
        STATUS_RESEARCH_CANDIDATE,
        STATUS_SINGLE_SYMBOL_RESEARCH_ONLY,
        run_strict_oos_rebound,
    )
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    candidates = []
    for i in range(200):
        ts = start + timedelta(hours=i, minutes=(i * 7) % 60)
        net = 0.80 if (i % 4 != 0) else -0.30
        candidates.append({
            "symbol": "BTCUSDT",
            "timestamp": ts.isoformat(),
            "regime_before": "TREND_DOWN", "regime_now": "TREND_UP",
            "volatility_bucket": "normal",
            "trend_recovering_prefix": True,
            "net_pnl_est": net,
        })
    grouping = ("regime_before", "regime_now", "volatility_bucket",
                "trend_recovering_prefix")
    r = run_strict_oos_rebound(candidates, grouping_features=grouping)
    # Either marked SINGLE_SYMBOL or as a research_candidate (downgraded).
    assert r.final_status_top_level in {
        STATUS_RESEARCH_CANDIDATE,
        STATUS_SINGLE_SYMBOL_RESEARCH_ONLY,
    }
    # And NOT promoted directly to PAPER_SANDBOX in this case.
    assert STATUS_PAPER_SANDBOX_CANDIDATE != r.final_status_top_level


def test_strict_oos_validates_features_forbids_score():
    from app.labs.rebound_long_strict_oos_v8_2_9 import run_strict_oos_rebound
    with pytest.raises(ValueError):
        run_strict_oos_rebound(
            [], grouping_features=("symbol", "score"),
        )


# ---------------------------------------------------------------------------
# Exit Monetization Audit
# ---------------------------------------------------------------------------

def test_exit_audit_marks_horizon_close_high_mfe_as_missed_profit():
    from app.labs.exit_monetization_audit_v8_2_9 import build_exit_audit_row
    row = _row(
        side="LONG", net_pnl=0.10, mfe=1.50, mae=-0.10,
        closed_by_horizon=True,
    )
    ar = build_exit_audit_row(row)
    assert ar.closed_by_horizon is True
    assert ar.is_missed_profit_candidate is True
    # Profit captured a small fraction of MFE.
    assert ar.profit_capture_ratio is not None
    assert ar.profit_capture_ratio < 0.20
    assert ar.missed_profit_pct is not None
    assert ar.missed_profit_pct > 1.0


def test_exit_audit_same_bar_ambiguity_resolved_conservatively():
    from app.labs.exit_monetization_audit_v8_2_9 import (
        SAME_BAR_AMBIGUITY_RULE,
        build_exit_audit_row,
    )
    row = _row(side="LONG", net_pnl=0.0, mfe=0.5, mae=-0.5)
    row["tp_before_sl"] = True
    row["sl_before_tp"] = True
    ar = build_exit_audit_row(row)
    assert ar.same_bar_ambiguous is True
    assert ar.same_bar_resolution == SAME_BAR_AMBIGUITY_RULE
    assert SAME_BAR_AMBIGUITY_RULE == "STOP_BEFORE_TP"


def test_exit_audit_policy_train_wins_test_loses_is_failed():
    """Exit policy that gains on train but loses on test → best policy
    test status FAIL (or NEED_MORE_DATA when test too small). Never
    PASS."""
    from app.labs.exit_monetization_audit_v8_2_9 import (
        POLICY_BASELINE_ACTUAL,
        POLICY_PROFIT_LOCK_MFE_THRESHOLD,
        run_exit_monetization_audit,
    )
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = []
    # Train slice: closed-by-horizon with high MFE → profit_lock wins.
    for i in range(60):
        rows.append(_row(
            ts=start + timedelta(hours=i),
            net_pnl=0.05, mfe=1.5, mae=-0.10,
            closed_by_horizon=True,
        ))
    # Validation slice: still positive.
    for i in range(20):
        rows.append(_row(
            ts=start + timedelta(hours=60 + i),
            net_pnl=0.10, mfe=1.0, mae=-0.10,
            closed_by_horizon=True,
        ))
    # Test slice: heavy losers — profit_lock cannot rescue.
    for i in range(20):
        rows.append(_row(
            ts=start + timedelta(hours=80 + i),
            net_pnl=-1.5, mfe=0.0, mae=-2.0,
            first_barrier="SL", closed_by_horizon=False,
        ))
    r = run_exit_monetization_audit(None, rows=rows)
    assert r.best_policy_test_status in {"FAIL", "NEED_MORE_DATA"}


def test_exit_audit_detects_horizon_problem():
    """Many horizon closes with high MFE → horizon_close_problem_detected."""
    from app.labs.exit_monetization_audit_v8_2_9 import (
        run_exit_monetization_audit,
    )
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = []
    for i in range(40):
        rows.append(_row(
            ts=start + timedelta(hours=i),
            net_pnl=0.10, mfe=1.5, mae=-0.10,
            closed_by_horizon=True,
        ))
    r = run_exit_monetization_audit(None, rows=rows)
    assert r.horizon_close_problem_detected is True
    assert r.avg_missed_profit_pct > 0.5


def test_exit_audit_policy_does_not_read_mfe_as_input_for_entry():
    """The exit-policy helpers may inspect MFE/MAE post-hoc to score a
    realised bar path, but the prefix-only ENTRY detector must NOT
    reference them. Verified via the rebound extractor literals."""
    literals = _detector_body_literals()
    assert "mfe_pct" not in literals
    assert "mae_pct" not in literals


# ---------------------------------------------------------------------------
# Adversarial Audit
# ---------------------------------------------------------------------------

def test_adversarial_audit_passes_on_clean_inputs():
    from app.labs.adversarial_research_audit_v8_2_9 import (
        AUDIT_PASS,
        audit_v829,
    )
    r = audit_v829(
        score_anti_calibrated=True,
        score_used_as_gate=False,
        duplicate_ratio_after=0.10,
        paper_filter_enabled=False,
        can_send_real_orders=False,
        live_trading=False,
        paper_sandbox_candidates_count=0,
    )
    assert r.audit_status == AUDIT_PASS
    assert r.blockers == []


def test_adversarial_audit_detects_high_duplicate_ratio():
    from app.labs.adversarial_research_audit_v8_2_9 import (
        AUDIT_FAIL_DUPLICATES,
        audit_v829,
    )
    r = audit_v829(duplicate_ratio_after=0.85)
    assert AUDIT_FAIL_DUPLICATES in r.blockers


def test_adversarial_audit_detects_score_misuse():
    from app.labs.adversarial_research_audit_v8_2_9 import (
        AUDIT_FAIL_SCORE_MISUSE,
        audit_v829,
    )
    r = audit_v829(score_anti_calibrated=True, score_used_as_gate=True)
    assert AUDIT_FAIL_SCORE_MISUSE in r.blockers


def test_adversarial_audit_detects_exit_lookahead():
    from app.labs.adversarial_research_audit_v8_2_9 import (
        AUDIT_FAIL_EXIT_LOOKAHEAD,
        audit_v829,
    )
    r = audit_v829(exit_policy_used_future_returns=True)
    assert AUDIT_FAIL_EXIT_LOOKAHEAD in r.blockers
    r2 = audit_v829(exit_policy_selected_on_test=True)
    assert AUDIT_FAIL_EXIT_LOOKAHEAD in r2.blockers
    r3 = audit_v829(same_bar_resolution_conservative=False)
    assert AUDIT_FAIL_EXIT_LOOKAHEAD in r3.blockers


def test_adversarial_audit_detects_safety_violation():
    from app.labs.adversarial_research_audit_v8_2_9 import (
        AUDIT_FAIL_SAFETY,
        audit_v829,
    )
    r = audit_v829(paper_filter_enabled=True)
    assert AUDIT_FAIL_SAFETY in r.blockers
    r2 = audit_v829(can_send_real_orders=True)
    assert AUDIT_FAIL_SAFETY in r2.blockers
    r3 = audit_v829(live_trading=True)
    assert AUDIT_FAIL_SAFETY in r3.blockers


def test_adversarial_audit_lookahead_simulation_via_monkeypatch(monkeypatch):
    """Inject a ``ret_*`` literal into the detector source and confirm
    the audit catches it."""
    from app.labs import adversarial_research_audit_v8_2_9 as audit_mod

    def fake_strings(fn):
        return ["ret_1h_pct", "ret_4h_pct"]

    monkeypatch.setattr(audit_mod, "_strings_in_function_body", fake_strings)
    r = audit_mod.audit_v829()
    assert "FAIL_LOOKAHEAD" in r.blockers


# ---------------------------------------------------------------------------
# Export V8.2.9
# ---------------------------------------------------------------------------

def test_export_v829_zip_only_csv_txt_json(tmp_path):
    from app.labs.research_export_v8_2_9 import export_research_v829
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = [_row(ts=start + timedelta(hours=i)) for i in range(40)]
    base = tmp_path / "v829_zip"
    export_research_v829(None, rows=rows, base_dir=base)
    zip_path = base / "research_v8_2_9_exports.zip"
    assert zip_path.exists()
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            assert name.endswith((".csv", ".txt", ".json"))


def test_export_v829_summary_includes_required_keys(tmp_path):
    from app.labs.research_export_v8_2_9 import export_research_v829
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = [_row(ts=start + timedelta(hours=i)) for i in range(40)]
    base = tmp_path / "v829_summary"
    export_research_v829(None, rows=rows, base_dir=base)
    summary_txt = (base / "research_v8_2_9_summary.txt").read_text(
        encoding="utf-8"
    )
    for key in (
        "raw_rebound_candidates",
        "dedup_rebound_candidates",
        "duplicate_ratio_before",
        "duplicate_ratio_after",
        "score_gate_best_variant",
        "score_used_as_gate",
        "strict_oos_status",
        "paper_sandbox_candidates",
        "best_exit_policy",
        "exit_oos_status",
        "horizon_close_problem_detected",
        "avg_profit_capture_ratio",
        "avg_missed_profit_pct",
        "adversarial_audit_status",
        "blockers",
        "final_recommendation: NO LIVE",
    ):
        assert key in summary_txt


def test_export_v829_rebound_csv_marks_no_future_features(tmp_path):
    from app.labs.research_export_v8_2_9 import export_research_v829
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows: list[dict[str, Any]] = []
    for i in range(10):
        rows.append(_row(
            ts=start + timedelta(hours=i),
            symbol="BTCUSDT", side="LONG", regime="TREND_DOWN",
            entry=100.0 - i * 0.5,
        ))
    for i in range(20):
        rows.append(_row(
            ts=start + timedelta(hours=10 + i),
            symbol="BTCUSDT", side="LONG", regime="TREND_UP",
            entry=98.0 + i * 0.2, net_pnl=0.30,
        ))
    base = tmp_path / "v829_rebound_csv"
    export_research_v829(None, rows=rows, base_dir=base)
    rebound_csv = base / "rebound_long_candidates_v1.csv"
    text = rebound_csv.read_text(encoding="utf-8")
    assert "detection_mode" in text
    assert "used_future_return_features" in text
    with rebound_csv.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            flag = str(row.get("used_future_return_features", "")).strip().lower()
            assert flag in {"false", "0", ""}


# ---------------------------------------------------------------------------
# CLI parser regression
# ---------------------------------------------------------------------------

def test_v829_cli_commands_parse():
    from app.research_lab import build_argument_parser
    parser = build_argument_parser()
    for argv in [
        ["rebound-long-candidates-v829", "--hours", "168"],
        ["edgeguard-repeat-dedup-v829", "--hours", "168"],
        ["score-gate-sandbox-v829", "--hours", "168"],
        ["exit-monetization-audit-v829", "--hours", "168"],
        ["rebound-long-strict-oos-v829", "--hours", "168"],
        ["adversarial-research-audit-v829", "--hours", "168"],
        ["export-research-v829", "--hours", "168"],
        ["research-pack-v829", "--hours", "168"],
    ]:
        ns = parser.parse_args(argv)
        assert ns.command == argv[0]


def test_v829_parser_has_no_duplicate_option_strings():
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

V829_MODULES = [
    "app.labs.rebound_long_candidate_extractor_v8_2_9",
    "app.labs.edgeguard_repeat_dedup_v8_2_9",
    "app.labs.score_gate_sandbox_v8_2_9",
    "app.labs.exit_monetization_audit_v8_2_9",
    "app.labs.rebound_long_strict_oos_v8_2_9",
    "app.labs.adversarial_research_audit_v8_2_9",
    "app.labs.research_export_v8_2_9",
]


def test_v829_modules_have_no_forbidden_calls():
    for mod in V829_MODULES:
        path = pathlib.Path(importlib.import_module(mod).__file__)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = getattr(fn, "attr", None) or getattr(fn, "id", None)
                assert name not in FORBIDDEN_CALL_NAMES, (
                    f"{mod} calls {name}"
                )


def test_v829_modules_have_no_forbidden_literal_true_assigns():
    for mod in V829_MODULES:
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


def test_v829_reports_carry_no_live():
    from app.labs.adversarial_research_audit_v8_2_9 import (
        AdversarialAuditReport,
    )
    from app.labs.edgeguard_repeat_dedup_v8_2_9 import DedupReport
    from app.labs.exit_monetization_audit_v8_2_9 import ExitMonetizationReport
    from app.labs.rebound_long_candidate_extractor_v8_2_9 import (
        ReboundLongExtractorReport,
    )
    from app.labs.rebound_long_strict_oos_v8_2_9 import StrictOosReboundReport
    from app.labs.score_gate_sandbox_v8_2_9 import ScoreGateSandboxReport
    for inst in [
        ReboundLongExtractorReport(hours=1, generated_at="t"),
        DedupReport(hours=1, generated_at="t"),
        ScoreGateSandboxReport(hours=1, generated_at="t"),
        ExitMonetizationReport(hours=1, generated_at="t"),
        StrictOosReboundReport(hours=1, generated_at="t"),
        AdversarialAuditReport(hours=1, generated_at="t"),
    ]:
        assert inst.research_only is True
        assert inst.paper_filter_enabled is False
        assert inst.can_send_real_orders is False
        assert inst.final_recommendation == "NO LIVE"


# ---------------------------------------------------------------------------
# V8.2.9.1 — Profit Factor all-wins fix
# ---------------------------------------------------------------------------

PF_MODULES = (
    "app.labs.rebound_long_strict_oos_v8_2_9",
    "app.labs.score_gate_sandbox_v8_2_9",
    "app.labs.exit_monetization_audit_v8_2_9",
)


def test_v8291_profit_factor_helper_rules():
    """All three V8.2.9 modules expose the canonical ``_profit_factor``
    with the same rule: all-wins → 999.0, all-zero → 0.0, mixed →
    gross_profit / abs(gross_loss)."""
    for modname in PF_MODULES:
        mod = importlib.import_module(modname)
        assert hasattr(mod, "_profit_factor"), f"{modname} missing _profit_factor"
        assert hasattr(mod, "PF_SENTINEL_NO_LOSSES"), (
            f"{modname} missing PF_SENTINEL_NO_LOSSES"
        )
        assert mod.PF_SENTINEL_NO_LOSSES == 999.0
        # all wins → sentinel
        assert mod._profit_factor(50.0, 0.0) == 999.0
        # no profit, no loss → 0.0
        assert mod._profit_factor(0.0, 0.0) == 0.0
        # negative profit, no loss → 0.0
        assert mod._profit_factor(-3.0, 0.0) == 0.0
        # mixed → canonical ratio (accepts gross_loss as negative sum)
        assert mod._profit_factor(60.0, -20.0) == pytest.approx(3.0)
        assert mod._profit_factor(60.0, 20.0) == pytest.approx(3.0)


def test_v8291_strict_oos_all_wins_pf_is_sentinel_not_zero():
    """100% wins → PF = 999.0 in strict OOS metrics, NEVER 0.0 → rule
    must not be rejected as ``test_pf=0.00_below_1.15``."""
    from app.labs.rebound_long_strict_oos_v8_2_9 import (
        PF_SENTINEL_NO_LOSSES,
        STATUS_PAPER_SANDBOX_CANDIDATE,
        STATUS_REJECT,
        _metrics,
        run_strict_oos_rebound,
    )
    rows_all_wins = [
        {"net_pnl_est": 0.80} for _ in range(50)
    ]
    m = _metrics(rows_all_wins)
    assert m["pf"] == PF_SENTINEL_NO_LOSSES
    # And the end-to-end strict OOS path with 300 all-win rows + 3 symbols
    # → PAPER_SANDBOX_CANDIDATE (was REJECT before the fix).
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    candidates = []
    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    for i in range(300):
        ts = start + timedelta(hours=i, minutes=(i * 7) % 60)
        candidates.append({
            "symbol": symbols[i % 3],
            "timestamp": ts.isoformat(),
            "regime_before": "TREND_DOWN", "regime_now": "TREND_UP",
            "volatility_bucket": "normal",
            "trend_recovering_prefix": True,
            "net_pnl_est": 0.80,
        })
    r = run_strict_oos_rebound(candidates)
    assert r.final_status_top_level == STATUS_PAPER_SANDBOX_CANDIDATE
    # No rejection mentions test_pf=0.00.
    for rule in r.rejected:
        assert "test_pf=0.00" not in rule.get("reject_reason", "")


def test_v8291_strict_oos_no_profit_no_loss_pf_is_zero():
    """All zero outcomes → gross_profit == 0 AND gross_loss == 0 → PF = 0.0."""
    from app.labs.rebound_long_strict_oos_v8_2_9 import _metrics
    rows = [{"net_pnl_est": 0.0} for _ in range(20)]
    m = _metrics(rows)
    assert m["pf"] == 0.0


def test_v8291_score_sandbox_all_wins_pf_sentinel():
    """A score variant with 100% wins must not get oos_status FAIL
    because of PF=0."""
    from app.labs.score_gate_sandbox_v8_2_9 import (
        PF_SENTINEL_NO_LOSSES,
        SCORE_GATE_NO_GATE,
        _metrics,
        run_score_gate_sandbox,
    )
    rows = [{"net_pnl_est": 0.80} for _ in range(40)]
    m = _metrics(rows)
    assert m["pf"] == PF_SENTINEL_NO_LOSSES
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    candidates = [
        {
            "symbol": "BTCUSDT", "side": "LONG",
            "timestamp": (start + timedelta(hours=i)).isoformat(),
            "score": 80, "net_pnl_est": 0.80,
        }
        for i in range(60)
    ]
    r = run_score_gate_sandbox(candidates, score_anti_calibrated=True)
    by_variant = {v["variant"]: v for v in r.variants}
    no_gate = by_variant[SCORE_GATE_NO_GATE]
    assert no_gate["test_pf"] == PF_SENTINEL_NO_LOSSES
    # OOS must be PASS (or NEED_MORE_DATA if too few test samples) — NOT FAIL
    # caused by PF=0.
    assert no_gate["oos_status"] in {"PASS", "NEED_MORE_DATA"}


def test_v8291_exit_monetization_all_wins_pf_sentinel():
    """An exit policy with 100% wins must report PF = 999.0 instead of
    0.0 and must not be FAILed by the PF gate."""
    from app.labs.exit_monetization_audit_v8_2_9 import (
        PF_SENTINEL_NO_LOSSES,
        POLICY_BASELINE_ACTUAL,
        _metrics_for_policy,
        build_exit_audit_row,
        run_exit_monetization_audit,
    )
    rows = []
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    for i in range(40):
        rows.append(_row(
            ts=start + timedelta(hours=i),
            net_pnl=0.80, mfe=1.2, mae=-0.05,
            first_barrier="TP", closed_by_horizon=False,
        ))
    audit_rows = [build_exit_audit_row(r) for r in rows]
    m = _metrics_for_policy(audit_rows, POLICY_BASELINE_ACTUAL)
    assert m["pf"] == PF_SENTINEL_NO_LOSSES
    # End-to-end audit: with 40 all-wins rows the train/val/test split
    # has ~24/8/8 — test slice too small (15+) → NEED_MORE_DATA, not FAIL
    # caused by PF=0.
    r = run_exit_monetization_audit(None, rows=rows)
    assert r.best_policy_test_status in {"PASS", "NEED_MORE_DATA"}


def test_v8291_strict_oos_pf_high_with_too_few_samples_returns_need_more_data():
    """High PF must NOT auto-promote to PAPER_SANDBOX when the train /
    validation / test minima are not satisfied."""
    from app.labs.rebound_long_strict_oos_v8_2_9 import (
        STATUS_NEED_MORE_DATA,
        run_strict_oos_rebound,
    )
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    candidates = []
    for i in range(12):  # Far below MIN_TRAIN_SAMPLES.
        candidates.append({
            "symbol": "BTCUSDT",
            "timestamp": (start + timedelta(hours=i)).isoformat(),
            "regime_before": "TREND_DOWN", "regime_now": "TREND_UP",
            "volatility_bucket": "normal",
            "trend_recovering_prefix": True,
            "net_pnl_est": 0.80,
        })
    r = run_strict_oos_rebound(candidates)
    assert r.final_status_top_level == STATUS_NEED_MORE_DATA


def test_v8291_export_v829_pf_sentinel_serialises_as_number(tmp_path):
    """When the PF sentinel ends up in a CSV / JSON / ZIP, it must be a
    plain number (999.0), not ``inf`` / ``nan``."""
    import json
    from app.labs.research_export_v8_2_9 import export_research_v829
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = []
    # All-win dataset → strict OOS, score sandbox, exit audit metrics
    # will all see PF sentinel.
    for i in range(60):
        rows.append(_row(
            ts=start + timedelta(hours=i),
            net_pnl=0.80, mfe=1.2, mae=-0.05,
            first_barrier="TP", closed_by_horizon=False,
        ))
    base = tmp_path / "v8291_pf"
    manifest = export_research_v829(None, rows=rows, base_dir=base)
    # Manifest serialisable as JSON without trouble.
    manifest_text = (base / "manifest_v1.json").read_text(encoding="utf-8")
    parsed = json.loads(manifest_text)
    assert "inf" not in manifest_text.lower()
    assert "nan" not in manifest_text.lower()
    # PF columns appear in strict OOS / score sandbox CSVs as numeric
    # strings.
    score_csv = base / "score_gate_sandbox_v1.csv"
    text = score_csv.read_text(encoding="utf-8")
    assert "inf" not in text.lower()
    assert "nan" not in text.lower()
    # Manifest contains adversarial audit status (smoke check the file
    # was written end-to-end).
    assert "adversarial_audit_status" in parsed
