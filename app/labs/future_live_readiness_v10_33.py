"""ResearchOps V10.33 - Future Live Readiness Scaffold (FAIL-CLOSED, research only).

PURPOSE: when (and only when) a validated edge ever exists, going live must not
fail on basics. This module PREPARES that day without enabling anything:
- a formal live-readiness AUDIT (hard gates; returns LIVE_READY=False today);
- a fail-closed PREFLIGHT dry-run (read-only checks, no keys, no network);
- a pure ORDER-PATH SIMULATOR (idempotency, duplicates, partial fills,
  rejects, timeout-safe retry, cancel -- zero exchange, zero keys);
- a pure CIRCUIT-BREAKER SIMULATOR (daily loss, consecutive losses, order
  budget, stale data, kill switch -- all halt fail-closed).

NOTHING here can send an order or read a secret: no network imports, no env
reads, no config loads. Every public function returns live_ready=False and
can_send_real_orders=False until every hard gate is genuinely met -- and the
gates themselves (validated edge, walk-forward, net EV, anti-overfit, paper
consistency) are facts that do not exist yet. FINAL_RECOMMENDATION: NO LIVE.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import FINAL_RECOMMENDATION_NO_LIVE

TOOL_VERSION = "v10.33"

# Hard gates: ALL must be True before live can even be DISCUSSED. They are
# evaluated from evidence, never assumed. Today every evidence-gate is False.
HARD_GATES = (
    ("edge_validated", "a strategy with positive net EV after costs, validated "
                       "out-of-sample AND in forward-shadow"),
    ("walk_forward_passed", "rolling walk-forward with stable OOS performance"),
    ("anti_overfit_passed", "beats random/permutation baselines with margin"),
    ("net_ev_after_costs_positive", "fees+slippage-adjusted EV > 0 with real cost model"),
    ("paper_shadow_consistent", ">=30d paper/shadow tracking matching research metrics"),
    ("kill_switch_implemented", "manual+automatic kill switch tested"),
    ("circuit_breakers_tested", "loss/drawdown/order-budget/stale-data halts tested"),
    ("reconciliation_designed", "position/order/balance reconciliation with alerts"),
    ("runbook_written", "operational runbook: start/stop/revert/emergencies"),
    ("human_approval", "explicit human sign-off per promotion stage"),
)

PROMOTION_LADDER = ("research", "shadow", "paper", "micro_live", "limited_live")


def _safety() -> dict[str, Any]:
    return {"research_only": True, "shadow_only": True, "paper_ready": False,
            "live_ready": False, "can_send_real_orders": False,
            "paper_filter_enabled": False, "edge_validated": False,
            "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------
# 1) Formal live readiness audit (evidence-based; fail-closed)
# --------------------------------------------------------------------------

def readiness_audit(evidence: dict[str, bool] | None = None) -> dict[str, Any]:
    """LIVE_READY only if EVERY hard gate has evidence=True. No evidence dict
    (the real situation today) => everything False => LIVE_READY=False."""
    ev = dict(evidence or {})
    gates = []
    for name, requirement in HARD_GATES:
        gates.append({"gate": name, "requirement": requirement,
                      "met": bool(ev.get(name, False))})
    unmet = [g["gate"] for g in gates if not g["met"]]
    live_ready = not unmet
    rep = {"tool_version": TOOL_VERSION, "checked_at": _now_iso(),
           "gates": gates, "unmet_gates": unmet,
           # V10.36 semantics fix (Codex): NEVER an ambiguous LIVE_READY=True.
           # checklist_complete = the synthetic checklist verdict only;
           # actual_live_ready = the real system state, hardcoded False.
           "checklist_complete": live_ready,
           "simulated_live_readiness": live_ready,
           "actual_live_ready": False,
           "ACTUAL_LIVE_READY": False,
           "note": ("human promotion required, real live remains blocked -- a "
                    "complete checklist NEVER enables anything"),
           "promotion_ladder": list(PROMOTION_LADDER),
           "promotion_rule": ("each step requires ALL gates for that stage plus "
                              "explicit human approval; NEVER automatic with real money"),
           **_safety()}
    # fail-closed belt-and-braces: the module safety flags can never flip;
    # promotion with real money is a human decision outside code
    rep["live_ready"] = False
    rep["can_send_real_orders"] = False
    return rep


# --------------------------------------------------------------------------
# 2) Preflight dry-run (read-only; no env, no keys, no network)
# --------------------------------------------------------------------------

def preflight_dry_run() -> dict[str, Any]:
    """Simulated future-live preflight. Each check is read-only. ANY failure
    (or inability to verify) blocks. Today it must PASS as a blocker-report:
    everything verifies that live is OFF."""
    repo = Path(__file__).resolve().parents[2]
    checks: list[dict[str, Any]] = []

    def add(name, ok, detail):
        checks.append({"check": name, "ok": bool(ok), "detail": str(detail)[:120]})

    aud = readiness_audit()
    add("hard_gates_all_met", aud["checklist_complete"],
        f"{len(aud['unmet_gates'])} unmet: {aud['unmet_gates'][:3]}")
    add("can_send_real_orders_is_false", aud["can_send_real_orders"] is False,
        "module safety flags")
    add("no_pending_orders", True, "research system places no orders by design")
    add("no_open_positions", True, "research system opens no positions by design")
    dash = repo / "reports" / "research" / "v10_29" / "status.html"
    if dash.is_file():
        html = dash.read_text(encoding="utf-8", errors="ignore")
        add("dashboard_says_no_live", "NO LIVE" in html, "status.html scanned")
        needle_key = "api_" + "key"      # split: never match this line itself
        add("dashboard_no_secrets", ".env" not in html and needle_key not in html,
            "status.html scanned")
    else:
        add("dashboard_says_no_live", False, "dashboard missing -> fail closed")
    # the preflight itself must never read env/config: structural self-check
    # (needles split so this check's own source line never matches itself)
    src = Path(__file__).read_text(encoding="utf-8")
    needle_env = "os." + "environ"
    needle_dotenv = "load_" + "dotenv"
    add("preflight_reads_no_env",
        needle_env not in src and needle_dotenv not in src,
        "module source scanned")
    all_ok = all(c["ok"] for c in checks)
    return {"tool_version": TOOL_VERSION, "checked_at": _now_iso(),
            "checks": checks, "preflight_would_allow_live": False,
            "reason": ("hard gates unmet" if not all_ok else
                       "gates unmet by definition today; promotion is human-only"),
            **_safety()}


# --------------------------------------------------------------------------
# 3) Order-path simulator (pure; no exchange, no keys, no network)
# --------------------------------------------------------------------------

def simulate_order_path(orders: list[dict], fee_bps: float = 6.0,
                        slippage_bps: float = 4.0) -> dict[str, Any]:
    """Simulate the future order pipeline on SYNTHETIC orders. Verifies the
    behaviours that burn money when wrong: idempotency by client_order_id,
    duplicate protection, partial fills, rejects, timeout-safe retry, cancel."""
    seen_ids: set[str] = set()
    results = []
    for o in orders:
        cid = str(o.get("client_order_id") or "")
        if not cid:
            results.append({"client_order_id": cid, "status": "REJECTED",
                            "reason": "missing_client_order_id"})
            continue
        if cid in seen_ids:
            results.append({"client_order_id": cid, "status": "DUPLICATE_IGNORED",
                            "reason": "idempotency_key_already_processed"})
            continue
        seen_ids.add(cid)
        action = str(o.get("scenario") or "fill")
        qty = float(o.get("qty") or 0)
        px = float(o.get("price") or 0)
        if qty <= 0 or px <= 0:
            results.append({"client_order_id": cid, "status": "REJECTED",
                            "reason": "non_positive_qty_or_price"})
            continue
        if action == "reject":
            results.append({"client_order_id": cid, "status": "REJECTED",
                            "reason": "exchange_reject_simulated"})
        elif action == "timeout_then_retry":
            # SAFE retry = same client_order_id resubmitted; the idempotency
            # key guarantees at-most-once execution
            results.append({"client_order_id": cid, "status": "FILLED_AFTER_RETRY",
                            "retries": 1, "at_most_once": True,
                            "fill_qty": qty,
                            "fill_price": round(px * (1 + slippage_bps / 10000), 8),
                            "fee": round(qty * px * fee_bps / 10000, 8)})
        elif action == "partial":
            filled = round(qty * 0.4, 8)
            results.append({"client_order_id": cid, "status": "PARTIALLY_FILLED",
                            "fill_qty": filled, "remaining": round(qty - filled, 8),
                            "fill_price": round(px * (1 + slippage_bps / 10000), 8),
                            "fee": round(filled * px * fee_bps / 10000, 8)})
        elif action == "cancel":
            results.append({"client_order_id": cid, "status": "CANCELLED",
                            "fill_qty": 0})
        else:
            results.append({"client_order_id": cid, "status": "FILLED",
                            "fill_qty": qty,
                            "fill_price": round(px * (1 + slippage_bps / 10000), 8),
                            "fee": round(qty * px * fee_bps / 10000, 8)})
    return {"tool_version": TOOL_VERSION, "simulated": True, "orders_in": len(orders),
            "results": results, "uses_network": False, "uses_keys": False,
            "would_send_real_order": False, **_safety()}


# --------------------------------------------------------------------------
# 4) Circuit-breaker simulator (pure; every breach halts fail-closed)
# --------------------------------------------------------------------------

DEFAULT_BREAKERS = {"daily_loss_limit_pct": 2.0, "max_consecutive_losses": 5,
                    "max_orders_per_day": 50, "max_data_stale_seconds": 120,
                    "kill_switch_engaged": False}


def simulate_circuit_breakers(events: list[dict],
                              limits: dict | None = None) -> dict[str, Any]:
    lim = {**DEFAULT_BREAKERS, **(limits or {})}
    state = {"daily_pnl_pct": 0.0, "consecutive_losses": 0, "orders_today": 0,
             "halted": False, "halt_reasons": []}

    def halt(reason):
        if not state["halted"]:
            state["halted"] = True
        if reason not in state["halt_reasons"]:
            state["halt_reasons"].append(reason)

    if lim.get("kill_switch_engaged"):
        halt("KILL_SWITCH")
    for ev in events:
        if state["halted"]:
            break                                  # halted = NOTHING else runs
        kind = str(ev.get("type") or "")
        if kind == "order":
            state["orders_today"] += 1
            if state["orders_today"] > lim["max_orders_per_day"]:
                halt("MAX_ORDERS_PER_DAY")
        elif kind == "pnl":
            pnl = float(ev.get("pct") or 0)
            state["daily_pnl_pct"] += pnl
            state["consecutive_losses"] = (state["consecutive_losses"] + 1
                                           if pnl < 0 else 0)
            if state["daily_pnl_pct"] <= -abs(lim["daily_loss_limit_pct"]):
                halt("DAILY_LOSS_LIMIT")
            if state["consecutive_losses"] >= lim["max_consecutive_losses"]:
                halt("MAX_CONSECUTIVE_LOSSES")
        elif kind == "data_stale":
            if float(ev.get("seconds") or 0) > lim["max_data_stale_seconds"]:
                halt("DATA_STALE")
        elif kind == "kill_switch":
            halt("KILL_SWITCH")
        else:
            halt(f"UNKNOWN_EVENT_FAIL_CLOSED:{kind}")   # unknown => halt
    return {"tool_version": TOOL_VERSION, "simulated": True, "limits": lim,
            "state": state, "halted": state["halted"],
            "halt_reasons": state["halt_reasons"], **_safety()}
