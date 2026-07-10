"""ResearchOps V10.45 - AI Simulated Trader SANDBOX (simulation only, NO LIVE).

Lets an AI provider (default: deterministic local mock) take PAPER decisions
bar-by-bar over the research dataset, inside a fully isolated ledger:

  * the model sees ONLY information available up to the current bar (ex-ante
    features from the V10.44 feature builder — prefix-only, no future);
  * entries execute at NEXT bar open; SL beats TP on the same bar; round-trip
    costs are charged; gaps block entries and force STALE exits;
  * decisions are validated against the strict V10.45 schema — garbage is
    REJECTED_AI_OUTPUT, order-like language is REJECTED_DANGEROUS_AI_OUTPUT and
    both count as NO_TRADE;
  * everything lands in a separate CSV ledger + JSON report. There is no path
    from here to any exchange, key, order or live flag;
  * the AI competes against a same-frequency random baseline and buy-and-hold;
    verdicts are gated exactly like every other research candidate. The best
    possible outcome is AI_SIM_PROMISING_RESEARCH_ONLY. Never live.
"""

from __future__ import annotations

import csv
import json
import math
import os
import random
import statistics as st
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import FINAL_RECOMMENDATION_NO_LIVE
from . import ai_research_copilot_v10_45 as COP
from . import alpha_factory_v10_44 as AF
from . import autonomous_strategy_lab_v10_43b as LAB
from . import shadow_simulation_tournament_v10_40 as SH
from . import continuous_edge_factory_v10_38 as CE

TOOL_VERSION = "v10.45"
OUTPUT_SUBDIR = ("reports", "research", "v10_45_ai_copilot")
WARMUP_BARS = 60
GAP_MS = 2 * 60_000
MIN_TRADES_FOR_CLAIM = 30
VERDICTS = ("AI_SIM_REJECTED", "AI_SIM_NEEDS_MORE_DATA",
            "AI_SIM_WATCHLIST_RESEARCH_ONLY", "AI_SIM_PROMISING_RESEARCH_ONLY")


def _safety() -> dict[str, Any]:
    return {"research_only": True, "shadow_only": True, "simulation_only": True,
            "sandboxed_ledger": True, "can_send_real_orders": False,
            "paper_filter_enabled": False, "edge_validated": False,
            "not_actionable": True, "no_orders": True, "no_broker": True,
            "no_private_endpoints": True, "changes_sizing": False,
            "changes_leverage": False,
            "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _out() -> Path:
    return CE._repo_root().joinpath(*OUTPUT_SUBDIR)


# ==========================================================================
# Sandbox replay engine
# ==========================================================================

def _context_for(feats: list[dict], i: int, position: dict | None) -> dict:
    f = feats[i]
    keep = ("ret_1m_prefix", "ret_3m_prefix", "ret_5m_prefix", "ret_15m_prefix",
            "trend_score", "flow_imbalance_10", "volume_z", "range_position_20",
            "realized_volatility_30", "compression", "body_pct", "symbol_regime")
    return {"bar_index": i, "ts": f.get("ts"),
            "features": {k: f.get(k) for k in keep},
            "position_state": ("FLAT" if position is None else position["side"]),
            "bars_held": 0 if position is None else i - position["entry_i"],
            "rules": ["simulation only", "no real orders ever",
                      "define tp/sl/max_hold for any entry",
                      "NO_TRADE is always acceptable"]}


def _close_position(position: dict, exit_px: float, exit_reason: str, i: int,
                    rt: float) -> dict:
    entry = position["entry_px"]
    side = position["side"]
    gross = (exit_px / entry - 1.0) if side == "LONG_SIM" else (entry - exit_px) / entry
    return {"entry_i": position["entry_i"], "exit_i": i, "side": side,
            "entry_px": entry, "exit_px": exit_px, "exit_reason": exit_reason,
            "bars_held": i - position["entry_i"],
            "gross_return": round(gross, 8), "net_return": round(gross - rt, 8),
            "confidence": position.get("confidence"),
            "entry_reason": position.get("entry_reason", "")}


def run_ai_simulated_trader(symbol: str = "BTCUSDT", provider: str = "mock",
                            data_source: str = "ws_persistent",
                            max_bars: int = 300,
                            write_reports: bool = True,
                            decide_fn=None) -> dict[str, Any]:
    """Replay the last `max_bars` bars; the provider decides, the sandbox
    executes in its isolated ledger. decide_fn injects a decision function for
    tests (overrides the provider)."""
    pstat = COP.provider_status(provider)
    summary: dict[str, Any] = {"tool_version": TOOL_VERSION, "ran_at": _now(),
                               "symbol": symbol, "provider": pstat["provider"],
                               "provider_status": pstat["status"],
                               "data_source": data_source, **_safety()}
    if decide_fn is None:
        prov = COP.get_provider(provider)
        if prov is None:
            summary["verdict"] = pstat["status"]      # MISSING_API_KEY etc.
            summary["note"] = "fail-closed: no provider available; use mock"
            if write_reports:
                _write(summary, [])
            return summary
        decide_fn = lambda ctx: prov.decide(ctx)      # noqa: E731
    bars, eff_source, _meta = LAB._load_bars(symbol, data_source)
    summary["effective_source"] = eff_source
    if len(bars) < WARMUP_BARS + 40:
        summary["verdict"] = "AI_SIM_NEEDS_MORE_DATA"
        summary["n_bars"] = len(bars)
        if write_reports:
            _write(summary, [])
        return summary
    bars = bars[-(max_bars + WARMUP_BARS):] if max_bars else bars
    feats = AF.build_alpha_features(bars)
    rt = SH._round_trip(None)
    ledger: list[dict] = []
    trades: list[dict] = []
    position: dict | None = None
    n_decisions = n_rejected = n_dangerous = 0
    for i in range(WARMUP_BARS, len(bars) - 1):
        f, nxt = bars[i], bars[i + 1]
        gap = (nxt["ts"] - f["ts"]) > GAP_MS
        # manage open position on THIS bar first (mechanical, from the AI's plan)
        if position is not None:
            hi, lo = float(f["high"]), float(f["low"])
            side = position["side"]
            exit_row = None
            if (f["ts"] - bars[position["entry_i"]]["ts"]) > GAP_MS * 40:
                exit_row = _close_position(position, float(f["close"]), "STALE_EXIT", i, rt)
            elif side == "LONG_SIM" and lo <= position["sl_px"]:
                exit_row = _close_position(position, position["sl_px"], "SL", i, rt)
            elif side == "SHORT_SIM" and hi >= position["sl_px"]:
                exit_row = _close_position(position, position["sl_px"], "SL", i, rt)
            elif side == "LONG_SIM" and hi >= position["tp_px"]:
                exit_row = _close_position(position, position["tp_px"], "TP", i, rt)
            elif side == "SHORT_SIM" and lo <= position["tp_px"]:
                exit_row = _close_position(position, position["tp_px"], "TP", i, rt)
            elif i - position["entry_i"] >= position["max_hold"]:
                exit_row = _close_position(position, float(f["close"]), "TIME", i, rt)
            if exit_row is not None:
                trades.append(exit_row)
                position = None
        # ask the AI (bounded context; ex-ante only)
        ctx = _context_for(feats, i, position)
        n_decisions += 1
        try:
            raw = decide_fn(ctx)
        except Exception:
            raw = None
        obj = None
        if raw is not None:
            try:
                obj = json.loads(raw) if isinstance(raw, str) else raw
            except Exception:
                obj = None
        verdict, dec = COP.validate_decision(obj) if obj is not None \
            else ("REJECTED_AI_OUTPUT", None)
        if verdict == "REJECTED_DANGEROUS_AI_OUTPUT":
            n_dangerous += 1
            dec = None
        elif verdict != "OK":
            n_rejected += 1
            dec = None
        decision = dec["decision"] if dec else "NO_TRADE"
        ledger.append({"bar_i": i, "ts": f.get("ts"), "decision": decision,
                       "validation": verdict,
                       "position_state": "FLAT" if position is None else position["side"],
                       "reason": (dec or {}).get("entry_reason", "")[:120]})
        if dec is None:
            continue
        if decision == "CLOSE_SIM" and position is not None and not gap:
            trades.append(_close_position(position, float(nxt["open"]),
                                          "AI_CLOSE", i + 1, rt))
            position = None
            continue
        if decision in ("LONG_SIM", "SHORT_SIM") and position is None:
            if gap:
                ledger[-1]["decision"] = "ENTRY_BLOCKED_DATA_GAP"
                continue
            entry_px = float(nxt["open"])
            plan = dec["exit_plan"]
            tp = plan["tp_bps"] / 10_000.0
            sl = plan["sl_bps"] / 10_000.0
            position = {"side": decision, "entry_i": i + 1, "entry_px": entry_px,
                        "tp_px": entry_px * (1 + tp) if decision == "LONG_SIM"
                        else entry_px * (1 - tp),
                        "sl_px": entry_px * (1 - sl) if decision == "LONG_SIM"
                        else entry_px * (1 + sl),
                        "max_hold": plan["max_hold_bars"],
                        "confidence": dec["confidence_bucket"],
                        "entry_reason": dec.get("entry_reason", "")}
    if position is not None:                      # close remainder at last bar
        trades.append(_close_position(position, float(bars[-1]["close"]),
                                      "END_OF_REPLAY", len(bars) - 1, rt))
    metrics = _metrics(trades)
    baselines = _baselines(bars, feats, trades, rt)
    verdict = _verdict(metrics, baselines)
    summary.update({
        "n_bars_replayed": len(bars) - WARMUP_BARS,
        "n_decisions": n_decisions,
        "n_rejected_outputs": n_rejected,
        "n_dangerous_outputs": n_dangerous,
        "n_trades": metrics["n_trades"],
        "metrics": metrics, "baselines": baselines,
        "beats_random": baselines.get("beats_random"),
        "beats_buy_hold": baselines.get("beats_buy_hold"),
        "verdict": verdict,
        "ai_role": "simulated decisions in an isolated ledger; nothing executable",
    })
    if write_reports:
        _write(summary, ledger)
    return summary


def _metrics(trades: list[dict]) -> dict[str, Any]:
    xs = [t["net_return"] for t in trades]
    wins = [x for x in xs if x > 0]
    losses = [x for x in xs if x < 0]
    return {"n_trades": len(xs),
            "net_EV": round(st.mean(xs), 8) if xs else None,
            "net_EV_lower_bound": AF._round(AF._lower_bound(xs, tests=1)),
            "profit_factor": AF._round(AF._pf(xs), 4),
            "win_rate": round(len(wins) / len(xs), 4) if xs else None,
            "payoff_ratio": AF._round((st.mean(wins) / abs(st.mean(losses)))
                                      if wins and losses else 0.0, 4),
            "max_drawdown": AF._round(AF._dd(xs)),
            "avg_hold_bars": round(st.mean([t["bars_held"] for t in trades]), 2)
            if trades else None,
            "total_net_return": round(sum(xs), 8) if xs else 0.0,
            "by_side": {s: sum(1 for t in trades if t["side"] == s)
                        for s in ("LONG_SIM", "SHORT_SIM")},
            "by_exit": {r: sum(1 for t in trades if t["exit_reason"] == r)
                        for r in ("TP", "SL", "TIME", "AI_CLOSE",
                                  "STALE_EXIT", "END_OF_REPLAY")}}


def _baselines(bars: list[dict], feats: list[dict], trades: list[dict],
               rt: float) -> dict[str, Any]:
    n_tr = len(trades)
    xs_ai = [t["net_return"] for t in trades]
    ai_ev = st.mean(xs_ai) if xs_ai else None
    # buy & hold over the same window, charged one round-trip
    try:
        bh = bars[-1]["close"] / bars[WARMUP_BARS]["open"] - 1.0 - rt
    except Exception:
        bh = None
    # random baseline: same trade count, same average hold, seeded
    rng = random.Random(1044)
    rand_rets: list[float] = []
    hold = max(1, int(st.mean([t["bars_held"] for t in trades])) if trades else 15)
    for _ in range(max(n_tr, 20)):
        i = rng.randrange(WARMUP_BARS, max(WARMUP_BARS + 1, len(bars) - hold - 2))
        side = rng.choice(("L", "S"))
        e = float(bars[i + 1]["open"])
        x = float(bars[min(i + 1 + hold, len(bars) - 1)]["close"])
        g = (x / e - 1.0) if side == "L" else (e - x) / e
        rand_rets.append(g - rt)
    rand_ev = st.mean(rand_rets) if rand_rets else None
    # alpha factory best (test EV), if a report exists
    alpha_best = None
    try:
        rep = json.loads((_out().parent / "v10_44_alpha_sprint" /
                          "alpha_factory_v10_44.json").read_text(encoding="utf-8"))
        alpha_best = ((rep.get("best_candidate") or {}).get("metrics_test")
                      or {}).get("net_EV")
    except Exception:
        pass
    return {"buy_hold_net": AF._round(bh),
            "random_same_freq_net_EV": AF._round(rand_ev),
            "alpha_factory_best_test_net_EV": alpha_best,
            "beats_random": (ai_ev is not None and rand_ev is not None
                             and ai_ev > rand_ev),
            "beats_buy_hold": (ai_ev is not None and bh is not None and ai_ev > bh)}


def _verdict(m: dict, b: dict) -> str:
    n = m["n_trades"]
    ev = m["net_EV"]
    lb = m["net_EV_lower_bound"]
    if n == 0:
        return "AI_SIM_NEEDS_MORE_DATA"
    if n < MIN_TRADES_FOR_CLAIM:
        return "AI_SIM_NEEDS_MORE_DATA" if (ev or 0) > 0 else "AI_SIM_REJECTED"
    if ev is None or ev <= 0:
        return "AI_SIM_REJECTED"
    if lb is None or lb <= 0 or not b.get("beats_random"):
        return "AI_SIM_WATCHLIST_RESEARCH_ONLY"
    if not b.get("beats_buy_hold"):
        return "AI_SIM_WATCHLIST_RESEARCH_ONLY"
    return "AI_SIM_PROMISING_RESEARCH_ONLY"


# ==========================================================================
# Reports
# ==========================================================================

def _write(summary: dict[str, Any], ledger: list[dict]) -> None:
    out = _out()
    out.mkdir(parents=True, exist_ok=True)
    tmp = out / "ai_simulated_trader_v10_45.json.tmp"
    tmp.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, out / "ai_simulated_trader_v10_45.json")
    if ledger:
        with open(out / "ai_decision_ledger_v10_45.csv", "w", newline="",
                  encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=["bar_i", "ts", "decision",
                                               "validation", "position_state",
                                               "reason"])
            w.writeheader()
            for r in ledger:
                w.writerow(r)
    m = summary.get("metrics") or {}
    b = summary.get("baselines") or {}
    md = ["# V10.45 AI Simulated Trader (SANDBOX — research only, NO LIVE)", "",
          f"- ran_at: {summary.get('ran_at')}",
          f"- provider: {summary.get('provider')} ({summary.get('provider_status')})",
          f"- source: {summary.get('effective_source')} · bars replayed: "
          f"{summary.get('n_bars_replayed')}",
          f"- decisions: {summary.get('n_decisions')} · rejected_outputs: "
          f"{summary.get('n_rejected_outputs')} · dangerous_outputs: "
          f"{summary.get('n_dangerous_outputs')}",
          f"- trades (SIM): {m.get('n_trades')} · net_EV: {m.get('net_EV')} · "
          f"lb: {m.get('net_EV_lower_bound')} · PF: {m.get('profit_factor')} · "
          f"win_rate: {m.get('win_rate')} · maxDD: {m.get('max_drawdown')}",
          f"- baselines: random={b.get('random_same_freq_net_EV')} · "
          f"buy&hold={b.get('buy_hold_net')} · beats_random={b.get('beats_random')} · "
          f"beats_buy_hold={b.get('beats_buy_hold')}",
          f"- **verdict: {summary.get('verdict')}**", "",
          "Every decision lived only in the isolated ledger. No orders, no keys, "
          "no exchange, no live flags. **FINAL_RECOMMENDATION=NO LIVE.**"]
    (out / "ai_simulated_trader_v10_45.md").write_text("\n".join(md) + "\n",
                                                       encoding="utf-8")


def render_cli(summary: dict[str, Any]) -> str:
    m = summary.get("metrics") or {}
    b = summary.get("baselines") or {}
    lines = ["AI SIMULATED TRADER V10.45 START",
             f"provider: {summary.get('provider')} ({summary.get('provider_status')})",
             f"source: {summary.get('effective_source')}",
             f"bars_replayed: {summary.get('n_bars_replayed')}",
             f"decisions: {summary.get('n_decisions')}  "
             f"rejected: {summary.get('n_rejected_outputs')}  "
             f"dangerous: {summary.get('n_dangerous_outputs')}",
             f"sim_trades: {m.get('n_trades')}",
             f"net_EV: {m.get('net_EV')}  lower_bound: {m.get('net_EV_lower_bound')}",
             f"profit_factor: {m.get('profit_factor')}  win_rate: {m.get('win_rate')}  "
             f"max_drawdown: {m.get('max_drawdown')}",
             f"baseline_random: {b.get('random_same_freq_net_EV')}  "
             f"buy_hold: {b.get('buy_hold_net')}",
             f"beats_random: {b.get('beats_random')}  beats_buy_hold: {b.get('beats_buy_hold')}",
             f"verdict: {summary.get('verdict')}",
             "ledger: isolated (reports/research/v10_45_ai_copilot/ai_decision_ledger_v10_45.csv)",
             "no_orders: true", "no_broker: true", "sandboxed_ledger: true",
             "can_send_real_orders: false",
             "final_recommendation: NO LIVE",
             "AI SIMULATED TRADER V10.45 END"]
    if summary.get("note"):
        lines.insert(2, f"note: {summary['note']}")
    return "\n".join(lines)
