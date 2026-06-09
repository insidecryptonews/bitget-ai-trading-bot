"""ResearchOps V10.2.2 — strategy replay backtester contract + stub tests.

All synthetic. No DB, no network, no real data. The stub never simulates a
trade; it only returns guard statuses.
"""

from __future__ import annotations

import pathlib

from app.labs.strategy_replay_backtest_v103_stub import (
    STATUS_MISSING_OI_RISK,
    STATUS_NEED_LONG_HISTORY,
    STATUS_RESEARCH_ONLY,
    STATUS_UNDERCOVERAGE_BLOCK,
    run_replay_backtest_stub,
)

BASE = 1_780_000_000_000
STEP = 3600000


def _market(n_hours):
    return [{"symbol": "ETHUSDT", "timestamp_ms": BASE + i * STEP, "price_close": 100.0 + i,
             "price_high": 101.0 + i, "price_low": 99.0 + i, "funding_rate": 0.0001,
             "oi_usd_close": 9e8} for i in range(n_hours)]


# --- stub guard statuses ---

def test_stub_no_data_need_long_history():
    r = run_replay_backtest_stub([], undercoverage=False, missing_oi_ratio=0.0)
    assert r.status == STATUS_NEED_LONG_HISTORY
    assert r.paper_ready is False and r.live_ready is False
    assert r.final_recommendation == "NO LIVE"


def test_stub_short_history_need_long_history():
    r = run_replay_backtest_stub(_market(45 * 24), undercoverage=False)  # ~45 days
    assert r.status == STATUS_NEED_LONG_HISTORY
    assert r.days_covered < 180


def test_stub_undercoverage_block_takes_precedence():
    # Even with plenty of rows, undercoverage flag blocks first.
    r = run_replay_backtest_stub(_market(200 * 24), undercoverage=True)
    assert r.status == STATUS_UNDERCOVERAGE_BLOCK


def test_stub_missing_oi_risk_when_oi_used():
    r = run_replay_backtest_stub(_market(200 * 24), undercoverage=False,
                                 missing_oi_ratio=0.1522, uses_oi=True)
    assert r.status == STATUS_MISSING_OI_RISK


def test_stub_research_only_when_guards_pass():
    # >=180 days, no undercoverage, no OI dependency => RESEARCH_ONLY (engine NOT built).
    r = run_replay_backtest_stub(_market(181 * 24), undercoverage=False,
                                 missing_oi_ratio=0.1522, uses_oi=False)
    assert r.status == STATUS_RESEARCH_ONLY
    assert r.engine_implemented is False
    assert r.paper_ready is False and r.live_ready is False


def test_stub_invariants_always_no_live():
    for kw in ({}, {"undercoverage": True}, {"missing_oi_ratio": 0.5, "uses_oi": True}):
        r = run_replay_backtest_stub(_market(10), **kw)
        assert r.paper_ready is False
        assert r.live_ready is False
        assert r.can_send_real_orders is False
        assert r.final_recommendation == "NO LIVE"
        assert "PAPER_ELIGIBLE_FUTURE" in r.promotion_ladder


# --- contract doc ---

def _contract_text():
    p = pathlib.Path(__file__).resolve().parents[1] / "docs" / "strategy_replay_backtester_contract_v10_2_2.md"
    assert p.exists(), "contract doc missing"
    return p.read_text(encoding="utf-8")


def test_contract_has_anti_lookahead():
    t = _contract_text().lower()
    assert "anti-lookahead" in t
    assert "next bar" in t
    assert "trailing window" in t


def test_contract_has_same_bar_worst_case():
    t = _contract_text().lower()
    assert "same-bar" in t
    assert "worst case" in t


def test_contract_has_walk_forward():
    t = _contract_text().lower()
    assert "walk-forward" in t
    assert "test window" in t
    assert "in-sample" in t


def test_contract_has_costs():
    t = _contract_text().lower()
    assert "slippage" in t and "funding" in t and ("maker/taker" in t or "maker" in t)


def test_contract_has_blockers_and_no_live():
    t = _contract_text()
    assert "NEED_LONG_HISTORY" in t
    assert "UNDERCOVERAGE_BLOCK" in t
    assert "MISSING_OI_RISK" in t
    assert "NO LIVE" in t


def test_contract_has_readonly_dashboard_relation():
    t = _contract_text().lower()
    assert "dashboard" in t and "read-only" in t
