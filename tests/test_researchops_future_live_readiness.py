"""ResearchOps V10.33 - Future Live Readiness Scaffold tests (FAIL-CLOSED).

Nothing in this scaffold may ever enable live: the audit reports gates, the
preflight is read-only, the simulators are pure functions. Every path must
return live_ready=False and can_send_real_orders=False today.
"""

from __future__ import annotations

import sys
from pathlib import Path

from app import research_lab
from app.labs import future_live_readiness_v10_33 as FL


# ---- readiness audit: false by default and for every missing gate -----------

def test_live_readiness_false_by_default():
    rep = FL.readiness_audit()
    assert rep["LIVE_READY"] is False and len(rep["unmet_gates"]) == len(FL.HARD_GATES)
    assert rep["live_ready"] is False and rep["can_send_real_orders"] is False
    assert rep["edge_validated"] is False
    assert rep["final_recommendation"] == "NO LIVE"


def test_live_readiness_false_when_any_single_gate_missing():
    for missing, _req in FL.HARD_GATES:
        ev = {name: True for name, _ in FL.HARD_GATES}
        ev[missing] = False
        rep = FL.readiness_audit(ev)
        assert rep["LIVE_READY"] is False, missing
        assert missing in rep["unmet_gates"]


def test_even_full_evidence_never_flips_safety_flags():
    # the CHECKLIST can be satisfied, but the module safety flags cannot flip:
    # promotion with real money is a human decision outside code
    rep = FL.readiness_audit({name: True for name, _ in FL.HARD_GATES})
    assert rep["LIVE_READY"] is True                # checklist verdict only
    assert rep["live_ready"] is False               # hard safety, never flips
    assert rep["can_send_real_orders"] is False
    assert rep["final_recommendation"] == "NO LIVE"


def test_promotion_ladder_has_no_automatic_live():
    rep = FL.readiness_audit()
    assert rep["promotion_ladder"] == ["research", "shadow", "paper",
                                       "micro_live", "limited_live"]
    assert "human approval" in rep["promotion_rule"]
    assert "NEVER automatic" in rep["promotion_rule"]


# ---- preflight: read-only, no env, blocks today ------------------------------

def test_preflight_blocks_today_and_reads_no_env():
    rep = FL.preflight_dry_run()
    assert rep["preflight_would_allow_live"] is False
    assert rep["can_send_real_orders"] is False
    names = {c["check"]: c["ok"] for c in rep["checks"]}
    assert names["hard_gates_all_met"] is False       # honest: gates unmet
    assert names["can_send_real_orders_is_false"] is True
    assert names["preflight_reads_no_env"] is True    # structural self-check
    src = Path(FL.__file__).read_text(encoding="utf-8")
    assert "urllib" not in src and "websocket" not in src and "requests" not in src
    assert "private_get" not in src and "api_key" not in src


# ---- order-path simulator: pure, at-most-once, no network --------------------

def test_order_simulator_idempotency_and_scenarios():
    orders = [
        {"client_order_id": "A1", "qty": 1, "price": 100, "scenario": "fill"},
        {"client_order_id": "A1", "qty": 1, "price": 100, "scenario": "fill"},   # dup
        {"client_order_id": "A2", "qty": 2, "price": 100, "scenario": "partial"},
        {"client_order_id": "A3", "qty": 1, "price": 100, "scenario": "reject"},
        {"client_order_id": "A4", "qty": 1, "price": 100, "scenario": "timeout_then_retry"},
        {"client_order_id": "A5", "qty": 1, "price": 100, "scenario": "cancel"},
        {"client_order_id": "", "qty": 1, "price": 100},                          # no id
        {"client_order_id": "A6", "qty": 0, "price": 100},                        # bad qty
    ]
    rep = FL.simulate_order_path(orders)
    st = {r["client_order_id"]: r["status"] for r in rep["results"]}
    assert st["A2"] == "PARTIALLY_FILLED" and st["A3"] == "REJECTED"
    assert st["A4"] == "FILLED_AFTER_RETRY" and st["A5"] == "CANCELLED"
    assert st["A6"] == "REJECTED" and st[""] == "REJECTED"
    dup = [r for r in rep["results"] if r["client_order_id"] == "A1"]
    assert dup[0]["status"] == "FILLED" and dup[1]["status"] == "DUPLICATE_IGNORED"
    retry = next(r for r in rep["results"] if r["client_order_id"] == "A4")
    assert retry["at_most_once"] is True              # safe retry = same id, once
    fill = next(r for r in rep["results"] if r["client_order_id"] == "A1")
    assert fill["fill_price"] > 100 and fill["fee"] > 0   # slippage + fees modelled
    assert rep["would_send_real_order"] is False
    assert rep["uses_network"] is False and rep["uses_keys"] is False


# ---- circuit breakers: fail-closed on every breach ---------------------------

def test_circuit_breakers_halt_fail_closed():
    r = FL.simulate_circuit_breakers([{"type": "pnl", "pct": -1.2},
                                      {"type": "pnl", "pct": -0.9},
                                      {"type": "order"}])
    assert r["halted"] is True and "DAILY_LOSS_LIMIT" in r["halt_reasons"]
    assert r["state"]["orders_today"] == 0            # nothing runs after halt
    r = FL.simulate_circuit_breakers([{"type": "pnl", "pct": -0.1}] * 5)
    assert "MAX_CONSECUTIVE_LOSSES" in r["halt_reasons"]
    r = FL.simulate_circuit_breakers([{"type": "order"}] * 51)
    assert "MAX_ORDERS_PER_DAY" in r["halt_reasons"]
    r = FL.simulate_circuit_breakers([{"type": "data_stale", "seconds": 999}])
    assert "DATA_STALE" in r["halt_reasons"]
    r = FL.simulate_circuit_breakers([{"type": "kill_switch"}])
    assert "KILL_SWITCH" in r["halt_reasons"]
    r = FL.simulate_circuit_breakers([], limits={"kill_switch_engaged": True})
    assert "KILL_SWITCH" in r["halt_reasons"]         # engaged switch halts all
    r = FL.simulate_circuit_breakers([{"type": "mystery_event"}])
    assert r["halted"] is True                        # unknown event => halt


def test_breaker_report_keeps_no_live_flags():
    r = FL.simulate_circuit_breakers([])
    assert r["live_ready"] is False and r["can_send_real_orders"] is False
    assert r["final_recommendation"] == "NO LIVE"


# ---- CLI wiring + isolation ---------------------------------------------------

def _run_main(argv):
    old = sys.argv
    sys.argv = ["prog"] + argv
    try:
        research_lab.main()
    finally:
        sys.argv = old


def test_cli_allowlisted_isolated_and_no_live(monkeypatch, capsys):
    for c in ("future-live-readiness-audit", "future-live-preflight-dry-run"):
        assert c in research_lab.PUBLIC_RESEARCH_ONLY_COMMANDS
    monkeypatch.setattr(research_lab, "load_config",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no config")))
    _run_main(["future-live-readiness-audit"])
    out = capsys.readouterr().out
    assert "LIVE_READY: False" in out and "NO LIVE" in out
    _run_main(["future-live-preflight-dry-run"])
    out = capsys.readouterr().out
    assert "preflight_would_allow_live: False" in out and "NO LIVE" in out
