"""Strategy Tournament RC1 tests (research-only). All synthetic."""

from __future__ import annotations

import ast
import importlib
import pathlib
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest


def _cand(
    *,
    ts: datetime,
    symbol: str = "BTCUSDT",
    side: str = "LONG",
    regime_now: str = "TREND_UP",
    regime_before: str = "TREND_DOWN",
    net: float = 0.50,
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "timestamp": ts.isoformat(),
        "side": side,
        "regime_now": regime_now,
        "regime_before": regime_before,
        "candidate_reason": "rebound_long_after_down_regime",
        "net_pnl_est": net,
    }


# ---------------------------------------------------------------------------
# Forbidden entry-feature guard
# ---------------------------------------------------------------------------

def test_strategy_rejects_forbidden_ex_post_entry_feature():
    from app.labs.strategy_tournament_rc1 import StrategySpec, run_tournament
    bad = StrategySpec(
        name="leaky",
        side="LONG",
        logic="uses ex-post outcome as entry feature",
        entry_features=("net_pnl_est",),  # forbidden
        predicate=lambda r: True,
    )
    with pytest.raises(ValueError):
        run_tournament([], [bad])


def test_strategy_rejects_ret_field_as_entry_feature():
    from app.labs.strategy_tournament_rc1 import StrategySpec, run_tournament
    bad = StrategySpec(
        name="leaky_ret",
        side="LONG",
        logic="uses future return",
        entry_features=("ret_4h_pct",),
        predicate=lambda r: True,
    )
    with pytest.raises(ValueError):
        run_tournament([], [bad])


def test_strategy_accepts_ex_ante_features():
    from app.labs.strategy_tournament_rc1 import StrategySpec
    ok = StrategySpec(
        name="clean",
        side="LONG",
        logic="regime based",
        entry_features=("side", "regime_now"),
        predicate=lambda r: True,
    )
    ok.validate()  # must not raise


# ---------------------------------------------------------------------------
# Gate behaviour
# ---------------------------------------------------------------------------

def test_negative_ev_strategy_is_rejected():
    """A losing cohort (20% winrate, 1:1 R:R) → REJECT."""
    from app.labs.strategy_tournament_rc1 import (
        STATUS_REJECT,
        StrategySpec,
        run_tournament,
    )
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = []
    for i in range(100):
        net = 0.81 if i % 5 == 0 else -0.75  # 20% winners
        rows.append(_cand(ts=start + timedelta(hours=i), net=net))
    spec = StrategySpec(
        name="loser", side="LONG", logic="losing cohort",
        entry_features=("side",), predicate=lambda r: True,
    )
    report = run_tournament(rows, [spec])
    res = report.results[0]
    assert res["status"] == STATUS_REJECT


def test_small_sample_returns_need_more_data():
    from app.labs.strategy_tournament_rc1 import (
        STATUS_NEED_MORE_DATA,
        StrategySpec,
        run_tournament,
    )
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = [_cand(ts=start + timedelta(hours=i), net=0.8) for i in range(10)]
    spec = StrategySpec(
        name="tiny", side="LONG", logic="too few samples",
        entry_features=("side",), predicate=lambda r: True,
    )
    report = run_tournament(rows, [spec])
    assert report.results[0]["status"] == STATUS_NEED_MORE_DATA


def test_sign_bug_ratio_above_threshold_rejects():
    """Even a profitable-looking cohort is rejected if its outcome labels
    are flagged untrustworthy (sign_bug_ratio > 0.05)."""
    from app.labs.strategy_tournament_rc1 import (
        STATUS_REJECT,
        StrategySpec,
        run_tournament,
    )
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]
    rows = []
    for i in range(300):
        net = 0.81 if i % 4 != 0 else -0.30  # 75% winners → healthy EV
        rows.append(_cand(
            ts=start + timedelta(hours=i, minutes=(i * 7) % 60),
            symbol=symbols[i % 4], net=net,
        ))
    spec = StrategySpec(
        name="suspect_labels", side="LONG", logic="good EV but bad labels",
        entry_features=("side",), predicate=lambda r: True,
    )
    report = run_tournament(
        rows, [spec], sign_bug_ratio_by_strategy={"suspect_labels": 0.20},
    )
    assert report.results[0]["status"] == STATUS_REJECT
    assert "sign_bug_ratio" in report.results[0]["reason"]


def test_single_time_window_cohort_is_watch_only_or_worse():
    """A profitable cohort concentrated in one hour bucket must NOT be
    promoted; time-cluster gate keeps it WATCH_ONLY."""
    from app.labs.strategy_tournament_rc1 import (
        STATUS_WATCH_ONLY,
        StrategySpec,
        run_tournament,
    )
    base = datetime(2026, 6, 1, 16, 0, tzinfo=timezone.utc)
    rows = []
    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]
    # 100 rows, ALL within the same hour 16:00–16:59 → time cluster ~1.0.
    for i in range(100):
        net = 0.81 if i % 4 != 0 else -0.30
        rows.append(_cand(
            ts=base + timedelta(seconds=i * 30),
            symbol=symbols[i % 4], net=net,
        ))
    spec = StrategySpec(
        name="one_window", side="LONG", logic="single window",
        entry_features=("side",), predicate=lambda r: True,
    )
    report = run_tournament(rows, [spec])
    assert report.results[0]["status"] == STATUS_WATCH_ONLY
    assert "time_cluster_share" in report.results[0]["reason"]


def test_clean_diversified_cohort_can_reach_shadow_or_paper_research():
    """A profitable, diversified, multi-window cohort with trustworthy
    labels reaches at most a research sandbox label (never live)."""
    from app.labs.strategy_tournament_rc1 import (
        STATUS_PAPER_SANDBOX_RESEARCH_ONLY,
        STATUS_SHADOW_SANDBOX_CANDIDATE,
        StrategySpec,
        run_tournament,
    )
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT"]
    rows = []
    # 300 rows spread across many days/hours and 5 symbols, 70% winners
    # with healthy +0.81 / -0.30 payoff.
    for i in range(300):
        net = 0.81 if i % 10 < 7 else -0.30
        rows.append(_cand(
            ts=start + timedelta(hours=i * 3, minutes=(i * 13) % 60),
            symbol=symbols[i % 5], net=net,
        ))
    spec = StrategySpec(
        name="clean_div", side="LONG", logic="diversified winner",
        entry_features=("side", "regime_now"), predicate=lambda r: True,
    )
    report = run_tournament(rows, [spec])
    status = report.results[0]["status"]
    assert status in {
        STATUS_SHADOW_SANDBOX_CANDIDATE,
        STATUS_PAPER_SANDBOX_RESEARCH_ONLY,
    }
    # Hard safety: research-only flags always present.
    assert report.results[0]["research_only"] is True
    assert report.results[0]["can_send_real_orders"] is False
    assert report.results[0]["final_recommendation"] == "NO LIVE"


def test_default_suite_validates_and_runs():
    from app.labs.strategy_tournament_rc1 import (
        default_strategy_suite,
        run_tournament,
    )
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = []
    for i in range(120):
        regime = "TREND_DOWN" if i % 2 == 0 else "RISK_OFF"
        net = -0.75 if regime == "TREND_DOWN" else (0.81 if i % 3 else -0.75)
        rows.append(_cand(
            ts=start + timedelta(hours=i), regime_now=regime, net=net,
        ))
    report = run_tournament(rows, default_strategy_suite())
    assert report.strategies_evaluated == 4
    assert report.status == "OK"
    # Falling-knife cohort must never be promoted.
    by_name = {r["name"]: r for r in report.results}
    assert by_name["avoid_long_while_trend_down"]["status"] in {
        "REJECT", "NEED_MORE_DATA", "WATCH_ONLY",
    }


def test_no_trade_down_cohort_is_documented_loser():
    """The TREND_DOWN cohort (falling knife) must score as a clear loser,
    justifying the avoid rule."""
    from app.labs.strategy_tournament_rc1 import (
        default_strategy_suite,
        run_tournament,
    )
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = [
        _cand(ts=start + timedelta(hours=i), regime_now="TREND_DOWN", net=-0.75)
        for i in range(120)
    ]
    report = run_tournament(rows, default_strategy_suite())
    by_name = {r["name"]: r for r in report.results}
    avoid = by_name["avoid_long_while_trend_down"]
    assert avoid["winrate"] == 0.0
    assert avoid["status"] == "REJECT"


# ---------------------------------------------------------------------------
# Safety AST scan
# ---------------------------------------------------------------------------

def test_tournament_module_has_no_forbidden_calls():
    forbidden = {
        "place_order", "set_leverage", "set_margin_mode",
        "private_get", "private_post",
    }
    path = pathlib.Path(
        importlib.import_module("app.labs.strategy_tournament_rc1").__file__
    )
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = getattr(fn, "attr", None) or getattr(fn, "id", None)
            assert name not in forbidden, f"tournament calls {name}"


def test_tournament_module_no_forbidden_true_assigns():
    forbidden = {
        "LIVE_TRADING", "ENABLE_PAPER_POLICY_FILTER",
        "can_send_real_orders", "allow_real_writes",
    }
    path = pathlib.Path(
        importlib.import_module("app.labs.strategy_tournament_rc1").__file__
    )
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
                    raise AssertionError(f"{name}=True in tournament")


def test_tournament_report_carries_no_live():
    from app.labs.strategy_tournament_rc1 import TournamentReport
    inst = TournamentReport(hours=1, generated_at="t")
    assert inst.research_only is True
    assert inst.paper_filter_enabled is False
    assert inst.can_send_real_orders is False
    assert inst.final_recommendation == "NO LIVE"
