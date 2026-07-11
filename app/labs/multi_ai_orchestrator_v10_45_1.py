"""ResearchOps V10.45.1 - Multi-AI research orchestrator (research only, NO LIVE).

Runs a team of AI research roles across the REAL providers (Ollama local,
Gemini, Groq when reachable) plus a large deterministic procedural universe:

  roles: HYPOTHESIS_GENERATOR, TECHNICAL_STRATEGIST, MICROSTRUCTURE_RESEARCHER,
  DERIVATIVES_RESEARCHER, CROSS_VENUE_RESEARCHER, NEWS_SENTIMENT_RESEARCHER,
  EXIT_ENGINEER, SKEPTICAL_CRITIC, OVERFIT_AUDITOR, FINAL_EVIDENCE_JUDGE.

Discipline:
  * models output STRICT JSON strategies for the V10.45.1 compiler; garbage is
    rejected and logged, never repaired silently;
  * criticism is CROSS-MODEL: a strategy is critiqued by a different provider
    than the one that generated it; no model approves its own idea;
  * model votes never promote anything — the FINAL_EVIDENCE_JUDGE is the
    deterministic replay funnel, not an LLM;
  * request budgets per provider with quota reserve; prompts are cached;
  * context sent to models contains only public research summaries, never keys.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from . import FINAL_RECOMMENDATION_NO_LIVE
from . import ai_providers_v10_45_1 as PROV
from . import edge_discovery_engine_v10_45_1 as ENG

TOOL_VERSION = "v10.45.3"

ROLES_GENERATION = (
    ("HYPOTHESIS_GENERATOR",
     "Invent 4 UNCONVENTIONAL but mechanically testable intraday strategies "
     "(novel feature combinations, session effects, volatility transitions)."),
    ("TECHNICAL_STRATEGIST",
     "Design 4 strategies from classic technical analysis done rigorously "
     "(RSI/MACD/ADX/Bollinger/Donchian/EMA structures, breakouts, pullbacks, "
     "mean reversion, squeeze expansion)."),
    ("MICROSTRUCTURE_RESEARCHER",
     "Design 3 strategies using volume/candle microstructure proxies available "
     "in the catalog (vol_z_30, body_pct, wicks, compression, flow_imbalance "
     "when present): bursts, absorption, exhaustion, liquidity vacuum proxies."),
    ("DERIVATIVES_RESEARCHER",
     "Design 3 strategies around derivatives session mechanics observable in "
     "OHLCV: funding-hour behaviour (is_funding_hour), volatility around "
     "0/8/16 UTC, post-spike reversion, crowding proxies via consecutive runs."),
    ("CROSS_VENUE_RESEARCHER",
     "Design 3 strategies using cross-venue features xv_ret_gap and "
     "xv_dislocation (reference venue leading the target venue)."),
    ("EXIT_ENGINEER",
     "Design 3 strategies where the EDGE IS THE EXIT: asymmetric TP/SL, "
     "partial TP1 with break-even, ATR trailing, tight time stops on momentum "
     "decay. Entries may be simple."),
)

CRITIC_ROLES = ("SKEPTICAL_CRITIC", "OVERFIT_AUDITOR")

SCHEMA_INSTRUCTIONS = """
Return STRICT JSON: {"strategies": [ ... ]} where each strategy is:
{
 "strategy_id": "short_snake_case_id",
 "hypothesis": "one sentence",
 "economic_rationale": "why this could persist after costs",
 "side": "LONG" or "SHORT",
 "regime_filter": one of ["TREND_UP","TREND_DOWN","RANGE","HIGH_VOLATILITY","LOW_VOLATILITY","ASIA","EU","US","ANY"],
 "entry_conditions": [ {"feature": "<from catalog>", "op": ">"|"<"|">="|"<=", "value": <number>} ]  (1-5 conditions),
 "stop_policy": {"type": "fixed"|"atr", "value": <0.001-0.03 if fixed, 0.5-4.0 if atr>},
 "take_profit_policy": {"type": "fixed"|"atr"|"rr", "value": <number>, "partial": {"tp1_frac": 0.5, "tp1_value": <number>, "move_stop_to_be": true} (optional)},
 "trailing_policy": {"type": "none"|"fixed"|"atr", "value": <number>},
 "time_exit": <bars 5-240>,
 "cooldown": <bars 1-60>,
 "expected_failure_modes": ["..."],
 "falsification_test": "what result would disprove this"
}
FEATURE CATALOG (the ONLY allowed features): %s
Numbers must be realistic for 1-minute BTC bars (fixed stops 0.002-0.02).
This is SIMULATION-ONLY research. Never mention real orders. No other text.
""" % ", ".join(ENG.FEATURE_REGISTRY)


def _safety() -> dict[str, Any]:
    return {"research_only": True, "simulation_only": True,
            "can_send_real_orders": False, "no_orders": True,
            "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}


def _extract_json(text: str) -> dict | None:
    """Parse model output as JSON; tolerate markdown fences; never repair."""
    t = text.strip()
    t = re.sub(r"^```(json)?", "", t).strip()
    t = re.sub(r"```$", "", t).strip()
    try:
        return json.loads(t)
    except Exception:
        m = re.search(r"\{.*\}", t, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
    return None


def _market_context(data_note: str) -> str:
    return PROV.sanitize_error("")[:0] + (
        f"Context: {data_note}. Costs per side ~9bps (fee+spread/2+slippage); "
        "round trip ~18bps — strategies must clear that. 1m bars, BTCUSDT "
        "perpetual, target venue Bitget. RESEARCH ONLY.")


def generate_hypotheses(providers: dict[str, PROV.BaseProvider],
                        data_note: str, log=print) -> dict[str, Any]:
    """Ask each available REAL provider to fill several generation roles."""
    import hashlib as _hl
    raw_ideas: list[dict] = []
    calls: list[dict] = []
    role_meta: dict[str, dict] = {}
    order = [p for p in ("ollama", "gemini", "groq") if p in providers]
    if not order:
        return {"ideas": [], "calls": [], "role_meta": {},
                "note": "no real providers available"}
    role_idx = 0
    for role, brief in ROLES_GENERATION:
        prov = providers[order[role_idx % len(order)]]
        role_idx += 1
        prompt = (f"ROLE: {role}. {brief}\n{_market_context(data_note)}\n"
                  + SCHEMA_INSTRUCTIONS)
        prompt_sha = _hl.sha256(prompt.encode("utf-8")).hexdigest()[:16]
        r = prov.generate(prompt, temperature=0.8)
        role_meta[role] = {"provider": prov.name,
                           "model": getattr(prov, "model", None),
                           "prompt_sha": prompt_sha}
        calls.append({"role": role, "provider": prov.name,
                      "model": getattr(prov, "model", None),
                      "prompt_sha": prompt_sha,
                      "ok": bool(r.get("ok")), "cached": r.get("cached"),
                      "latency_s": r.get("latency_s"),
                      "error": r.get("error")})
        if not r.get("ok"):
            log(f"  {role} via {prov.name}: FAILED {r.get('error')}")
            continue
        obj = _extract_json(r["text"])
        strategies = (obj or {}).get("strategies") or []
        if not isinstance(strategies, list):
            strategies = []
        for s in strategies:
            if isinstance(s, dict):
                s["origin"] = f"ai:{prov.name}:{role}"
                raw_ideas.append(s)
        took = "cache" if r.get("cached") else f"{r.get('latency_s')}s"
        log(f"  {role} via {prov.name} ({getattr(prov, 'model', None)}): "
            f"{len(strategies)} strategies ({took})")
    return {"ideas": raw_ideas, "calls": calls, "role_meta": role_meta}


def cross_critique(providers: dict[str, PROV.BaseProvider],
                   compiled: list[dict], log=print,
                   max_batch: int = 12) -> list[dict]:
    """Cross-model criticism: strategies from provider X are critiqued by a
    DIFFERENT provider. Output annotates; it never approves or promotes."""
    real = [p for p in ("gemini", "ollama", "groq") if p in providers]
    if len(real) == 0 or not compiled:
        return []
    notes = []
    ai_strats = [s for s in compiled if str(s.get("origin", "")).startswith("ai:")]
    batch = ai_strats[:max_batch]
    if not batch:
        return []
    by_provider: dict[str, list[dict]] = {}
    for s in batch:
        gen_prov = s["origin"].split(":")[1]
        critics = [p for p in real if p != gen_prov] or real
        by_provider.setdefault(critics[0], []).append(s)
    for critic_name, strats in by_provider.items():
        prov = providers[critic_name]
        listing = json.dumps([{ "strategy_id": s["strategy_id"],
                                "hypothesis": s["hypothesis"],
                                "side": s["side"],
                                "conditions": [f"{a}{b}{c}" for a, b, c in s["conditions"]],
                                "stop": s["stop"], "tp": s["tp"],
                                "time_exit": s["time_exit"]}
                              for s in strats], default=str)
        prompt = (
            "ROLE: SKEPTICAL_CRITIC + OVERFIT_AUDITOR. You did NOT create these "
            "strategies; attack them. For each, list the most likely reason it "
            "fails after 18bps round-trip costs, any overfit/leakage smell, and "
            "a falsification check. STRICT JSON only: {\"critiques\": "
            "[{\"strategy_id\": str, \"kill_reasons\": [str], "
            "\"overfit_risk\": \"LOW|MEDIUM|HIGH\", \"note\": str}]}. "
            f"STRATEGIES: {listing}")
        r = prov.generate(prompt, temperature=0.4)
        if not r.get("ok"):
            log(f"  critic {critic_name}: FAILED {r.get('error')}")
            continue
        obj = _extract_json(r["text"]) or {}
        for cnote in (obj.get("critiques") or []):
            if isinstance(cnote, dict) and cnote.get("strategy_id"):
                cnote["critic_provider"] = critic_name
                notes.append(cnote)
        log(f"  critic {critic_name} ({getattr(prov,'model',None)}): "
            f"{len(obj.get('critiques') or [])} critiques")
    return notes


# ==========================================================================
# PROCEDURAL UNIVERSE (deterministic, wide, reproducible)
# ==========================================================================

def procedural_universe() -> list[dict]:
    """A few hundred transparent strategies across families: indicator
    thresholds x sides x regimes x exit shapes. Deterministic and cheap."""
    out: list[dict] = []

    def add(sid, side, conds, sl, tp, te, regime="ANY", tp_type="fixed",
            sl_type="fixed", trail=("none", 0.0), partial=None, cooldown=5,
            hypothesis=""):
        out.append({
            "strategy_id": sid, "origin": "procedural", "side": side,
            "hypothesis": hypothesis or sid,
            "economic_rationale": "systematic family scan",
            "regime_filter": regime, "entry_conditions": conds,
            "stop_policy": {"type": sl_type, "value": sl},
            "take_profit_policy": {"type": tp_type, "value": tp,
                                   **({"partial": partial} if partial else {})},
            "trailing_policy": {"type": trail[0], "value": trail[1]},
            "time_exit": te, "cooldown": cooldown})

    rr_grid = ((0.004, 0.004, 30), (0.004, 0.006, 45), (0.004, 0.008, 60),
               (0.006, 0.009, 60), (0.005, 0.010, 90), (0.006, 0.015, 120))
    # RSI mean reversion / momentum
    for lo in (20, 25, 30):
        for sl, tp, te in rr_grid[:3]:
            add(f"rsi{lo}_mr_long_{int(tp*1e4)}", "LONG",
                [{"feature": "rsi_14", "op": "<", "value": float(lo)}], sl, tp, te)
            add(f"rsi{100-lo}_mr_short_{int(tp*1e4)}", "SHORT",
                [{"feature": "rsi_14", "op": ">", "value": float(100 - lo)}], sl, tp, te)
    for hi in (60, 65):
        add(f"rsi{hi}_mom_long", "LONG",
            [{"feature": "rsi_14", "op": ">", "value": float(hi)},
             {"feature": "adx_14", "op": ">", "value": 20.0}], 0.005, 0.01, 60,
            regime="TREND_UP")
    # Donchian / breakout
    for bfeat, side in (("donchian_break_up", "LONG"), ("donchian_break_down", "SHORT"),
                        ("donchian_break_up_55", "LONG"), ("donchian_break_down_55", "SHORT")):
        for sl, tp, te in rr_grid[2:5]:
            add(f"{bfeat}_{side.lower()}_{int(tp*1e4)}", side,
                [{"feature": bfeat, "op": ">", "value": 0.5},
                 {"feature": "vol_z_30", "op": ">", "value": 0.5}], sl, tp, te)
        add(f"{bfeat}_{side.lower()}_trail", side,
            [{"feature": bfeat, "op": ">", "value": 0.5}], 0.005, 0.02, 120,
            trail=("atr", 2.0))
    # failed breakout fade
    add("failed_break_up_fade_short", "SHORT",
        [{"feature": "donchian_break_up", "op": ">", "value": 0.5},
         {"feature": "body_pct", "op": "<", "value": 0.0},
         {"feature": "upper_wick", "op": ">", "value": 0.001}], 0.005, 0.0075, 45)
    add("failed_break_down_fade_long", "LONG",
        [{"feature": "donchian_break_down", "op": ">", "value": 0.5},
         {"feature": "body_pct", "op": ">", "value": 0.0},
         {"feature": "lower_wick", "op": ">", "value": 0.001}], 0.005, 0.0075, 45)
    # Bollinger touches
    for side, op, val in (("LONG", "<", 0.02), ("SHORT", ">", 0.98)):
        for sl, tp, te in rr_grid[:3]:
            add(f"bb_touch_{side.lower()}_{int(tp*1e4)}", side,
                [{"feature": "bb_pos", "op": op, "value": val}], sl, tp, te,
                regime="RANGE")
    # squeeze expansion
    for side, mfeat in (("LONG", "macd_cross_up"), ("SHORT", "macd_cross_down")):
        add(f"squeeze_expand_{side.lower()}", side,
            [{"feature": "squeeze_on", "op": ">", "value": 0.5},
             {"feature": mfeat, "op": ">", "value": 0.5}], 0.005, 0.012, 90,
            trail=("atr", 1.5))
    # MACD cross with trend filters
    for regime in ("ANY", "TREND_UP"):
        add(f"macd_cross_long_{regime.lower()}", "LONG",
            [{"feature": "macd_cross_up", "op": ">", "value": 0.5}], 0.005, 0.0075, 60,
            regime=regime)
    for regime in ("ANY", "TREND_DOWN"):
        add(f"macd_cross_short_{regime.lower()}", "SHORT",
            [{"feature": "macd_cross_down", "op": ">", "value": 0.5}], 0.005, 0.0075, 60,
            regime=regime)
    # EMA pullback in trend
    for side, align, rfeat, rop in (("LONG", "ema_align_up", "ret_5", "<"),
                                    ("SHORT", "ema_align_down", "ret_5", ">")):
        for sl, tp, te in rr_grid[1:4]:
            add(f"ema_pullback_{side.lower()}_{int(tp*1e4)}", side,
                [{"feature": align, "op": ">", "value": 0.5},
                 {"feature": rfeat, "op": rop, "value": 0.0}], sl, tp, te)
    # VWAP reversion / continuation
    for side, op, val in (("LONG", "<", -0.004), ("SHORT", ">", 0.004)):
        add(f"vwap_revert_{side.lower()}", side,
            [{"feature": "vwap_dist", "op": op, "value": val}], 0.005, 0.006, 45)
    for side, op, val in (("LONG", ">", 0.002), ("SHORT", "<", -0.002)):
        add(f"vwap_trend_{side.lower()}", side,
            [{"feature": "vwap_dist", "op": op, "value": val},
             {"feature": "adx_14", "op": ">", "value": 25.0}], 0.005, 0.01, 90)
    # volatility percentile transitions
    add("lowvol_breakout_long", "LONG",
        [{"feature": "atr_percentile_240", "op": "<", "value": 0.2},
         {"feature": "donchian_break_up", "op": ">", "value": 0.5}], 0.004, 0.012, 120)
    add("highvol_exhaustion_short", "SHORT",
        [{"feature": "atr_percentile_240", "op": ">", "value": 0.85},
         {"feature": "ret_15", "op": ">", "value": 0.01},
         {"feature": "upper_wick", "op": ">", "value": 0.0015}], 0.006, 0.009, 45)
    add("capitulation_rebound_long", "LONG",
        [{"feature": "ret_15", "op": "<", "value": -0.01},
         {"feature": "lower_wick", "op": ">", "value": 0.0015},
         {"feature": "vol_z_30", "op": ">", "value": 1.5}], 0.006, 0.009, 45)
    # momentum burst continuation / decay
    for side, op in (("LONG", ">"), ("SHORT", "<")):
        sgn = 1 if side == "LONG" else -1
        add(f"burst_cont_{side.lower()}", side,
            [{"feature": "ret_5", "op": op, "value": sgn * 0.004},
             {"feature": "vol_z_30", "op": ">", "value": 2.0}], 0.005, 0.0075, 30)
        add(f"burst_fade_{('short' if side=='LONG' else 'long')}",
            "SHORT" if side == "LONG" else "LONG",
            [{"feature": "ret_5", "op": op, "value": sgn * 0.006},
             {"feature": "vol_z_30", "op": ">", "value": 2.5}], 0.006, 0.006, 30)
    # sessions / time-of-day
    for sess in ("ASIA", "EU", "US"):
        add(f"open_break_long_{sess.lower()}", "LONG",
            [{"feature": "donchian_break_up", "op": ">", "value": 0.5}],
            0.005, 0.01, 60, regime=sess)
    add("funding_hour_fade_short", "SHORT",
        [{"feature": "is_funding_hour", "op": ">", "value": 0.5},
         {"feature": "ret_15", "op": ">", "value": 0.005}], 0.005, 0.0075, 45)
    add("funding_hour_fade_long", "LONG",
        [{"feature": "is_funding_hour", "op": ">", "value": 0.5},
         {"feature": "ret_15", "op": "<", "value": -0.005}], 0.005, 0.0075, 45)
    # cross-venue lead-lag
    for side, op, val in (("LONG", ">", 0.0008), ("SHORT", "<", -0.0008)):
        for te in (15, 30):
            add(f"xv_leadlag_{side.lower()}_{te}", side,
                [{"feature": "xv_ret_gap", "op": op, "value": val}], 0.004, 0.006, te,
                cooldown=10)
    for side, op, val in (("SHORT", ">", 0.0008), ("LONG", "<", -0.0008)):
        add(f"xv_dislocation_revert_{side.lower()}", side,
            [{"feature": "xv_dislocation", "op": op, "value": val}], 0.004, 0.005, 20,
            cooldown=10)
    # exit-shape variants on one robust entry (exit engineering family)
    base_conds = [{"feature": "ema_align_up", "op": ">", "value": 0.5},
                  {"feature": "ret_5", "op": "<", "value": 0.0},
                  {"feature": "rsi_14", "op": "<", "value": 45.0}]
    add("exit_rr1", "LONG", base_conds, 0.005, 1.0, 60, tp_type="rr")
    add("exit_rr15", "LONG", base_conds, 0.005, 1.5, 60, tp_type="rr")
    add("exit_rr2", "LONG", base_conds, 0.005, 2.0, 90, tp_type="rr")
    add("exit_rr3", "LONG", base_conds, 0.005, 3.0, 120, tp_type="rr")
    add("exit_atr_stops", "LONG", base_conds, 1.5, 3.0, 90, tp_type="atr",
        sl_type="atr")
    add("exit_partial_be", "LONG", base_conds, 0.005, 0.012, 90,
        partial={"tp1_frac": 0.5, "tp1_value": 0.005, "move_stop_to_be": True})
    add("exit_trail_atr", "LONG", base_conds, 0.005, 0.03, 120, trail=("atr", 2.0))
    add("exit_trail_tight", "LONG", base_conds, 0.005, 0.03, 120, trail=("fixed", 0.004))
    add("exit_time_short", "LONG", base_conds, 0.005, 0.02, 10)
    return out


# ==========================================================================
# FULL RUN: data -> AI hypotheses -> compile -> funnel -> reports
# ==========================================================================

def run_edge_discovery(symbol: str = "BTCUSDT", use_ai: bool = True,
                       write_reports: bool = True, timeframe: str = "1m",
                       n_trials_total: int | None = None,
                       run_id: str | None = None,
                       iteration: int = 1, parent_experiment: str | None = None,
                       log=print) -> dict[str, Any]:
    import csv as _csv
    import os as _os
    import uuid
    from . import public_data_backfill_v10_45_1 as BF
    started = datetime.now(timezone.utc)
    run_id = run_id or f"edr_{uuid.uuid4().hex[:12]}"
    # ---------- data (Bitget = target venue; Bybit = cross-venue reference) --
    bars = BF.load_klines("bitget", symbol)
    ref = BF.load_klines("bybit", symbol)
    manifest = BF.load_manifest("bitget", symbol) or {}
    factor = {"1m": 1, "5m": 5, "15m": 15, "1h": 60}.get(timeframe, 1)
    if factor > 1:
        bars = ENG.resample_bars(bars, factor)
        ref = ENG.resample_bars(ref, factor)
    if len(bars) < 5000:
        return {"status": "NEED_MORE_DATA",
                "detail": f"{len(bars)} {timeframe} bars", **_safety()}
    # zero-gap promotion rule: if the full dataset has real gaps (exchange
    # maintenance), run on the longest strictly-contiguous segment instead —
    # a TIME-based subset chosen before any strategy runs (not outcome-biased)
    dq_full = ENG.dataset_quality(bars)
    segment_note = "full dataset"
    if not dq_full.get("quality_pass"):
        bar_ms = dq_full.get("bar_ms") or 60_000
        seg_bars = ENG.longest_contiguous_segment(bars, bar_ms=bar_ms)
        if len(seg_bars) >= 5000:
            segment_note = (f"longest contiguous segment {len(seg_bars)}/"
                            f"{len(bars)} bars (full data had "
                            f"{dq_full.get('gap_count')} gaps)")
            bars = seg_bars
            ref_ts = {b["ts"] for b in bars}
            ref = [r for r in ref if r["ts"] in ref_ts] if ref else ref
    data_note = (f"{len(bars)} {timeframe} bars of {symbol} on bitget "
                 f"(+{len(ref)} bybit reference bars) [{segment_note}]")
    if len(bars) < 5000:
        return {"status": "NEED_MORE_DATA", "detail": data_note, **_safety()}
    log(f"data: {data_note}")
    dq = ENG.dataset_quality(bars)
    seg = ENG.split_indices(len(bars))
    prov = ENG.code_tree_hash()
    ENG.set_run_context(
        run_id=run_id, iteration=iteration, parent_experiment=parent_experiment,
        repo_commit=BF._repo_commit(), dataset_sha256=manifest.get("sha256"),
        downloader_version=manifest.get("downloader_version"),
        code_tree_hash=prov["code_tree_hash"],
        runner_version=prov["runner_version"],
        dirty_worktree=prov["dirty_worktree"],
        download_complete=manifest.get("download_complete"),
        symbol=symbol, timeframe=timeframe, venue="bitget",
        splits={k: [bars[max(v[0], 0)]["ts"], bars[min(v[1], len(bars) - 1)]["ts"]]
                for k, v in seg.items()},
        cost_config=dict(ENG.DEFAULT_COSTS),
        data_quality={k: dq.get(k) for k in ("quality_pass", "gap_count",
                                             "duplicates", "out_of_order",
                                             "irregular_deltas", "invalid_ohlc",
                                             "coverage")})
    feats = ENG.build_features(bars, ref_bars=ref)
    log(f"features built: {len(feats)} rows x {len(ENG.FEATURE_REGISTRY)} catalog "
        f"| quality_pass={dq.get('quality_pass')} gaps={dq.get('gap_count')}")
    # ---------- universe: procedural + AI ----------
    raw: list[dict] = procedural_universe()
    n_procedural = len(raw)
    providers: dict[str, PROV.BaseProvider] = {}
    ai_meta: dict[str, Any] = {"calls": [], "ideas": 0}
    if use_ai:
        providers = PROV.build_providers()
        real = [k for k in providers if k != "mock"]
        log(f"providers available: {real or 'NONE (procedural only)'}")
        if real:
            gen = generate_hypotheses(providers, data_note, log=log)
            ai_meta["calls"] = gen["calls"]
            ai_meta["ideas"] = len(gen["ideas"])
            ai_meta["role_meta"] = gen.get("role_meta", {})
            raw.extend(gen["ideas"])
    # ---------- compile ----------
    import hashlib as _hashlib
    seen: set[str] = set()
    compiled: list[dict] = []
    n_invalid = n_dup = 0
    for s in raw:
        state, spec = ENG.compile_strategy(s, seen, symbol=symbol,
                                           timeframe=timeframe)
        prov_meta = {}
        origin = str(s.get("origin", ""))
        if origin.startswith("ai:"):
            parts = origin.split(":")
            role_name = parts[2] if len(parts) > 2 else None
            rm = (ai_meta.get("role_meta") or {}).get(role_name, {})
            prov_meta = {"provider": parts[1] if len(parts) > 1 else None,
                         "role": role_name,
                         "model": rm.get("model"),
                         "prompt_sha": rm.get("prompt_sha")}
        if state == "OK":
            compiled.append(spec)
            ENG.ledger_append({"phase": "compile", "state": "OK",
                               "strategy_id": spec["strategy_id"],
                               "signature": spec["signature"],
                               "origin": origin, **prov_meta,
                               "strategy_raw": s, "strategy_compiled": spec})
        elif state == "DUPLICATE":
            n_dup += 1
            ENG.ledger_append({"phase": "compile", "state": "DUPLICATE",
                               "origin": origin, **prov_meta,
                               "strategy_id": s.get("strategy_id"),
                               "strategy_raw": s})
        else:
            n_invalid += 1
            ENG.ledger_append({"phase": "compile", "state": "INVALID",
                               "origin": origin, **prov_meta,
                               "strategy_id": str(s.get("strategy_id"))[:60],
                               "strategy_raw": s})
    log(f"universe: {len(raw)} raw ({n_procedural} procedural + {ai_meta['ideas']} AI) "
        f"-> {len(compiled)} compiled, {n_dup} dup, {n_invalid} invalid")
    # ---------- cross-model criticism (annotation only) ----------
    critiques = []
    if use_ai and providers:
        critiques = cross_critique(providers, compiled, log=log)
        for cnote in critiques:
            ENG.ledger_append({"phase": "critique", **cnote})
    # ---------- deterministic funnel (the only judge) ----------
    funnel = ENG.run_funnel(
        bars, feats, compiled, n_trials_total=n_trials_total,
        promotion_allowed=bool(manifest.get("download_complete", True)),
        log=log)
    finals = funnel["finalists"]
    by_state: dict[str, int] = {}
    for e in funnel["results"]:
        by_state[e["state"]] = by_state.get(e["state"], 0) + 1
    top = sorted(finals, key=lambda e: (e["holdout_metrics"].get("net_EV") or -9),
                 reverse=True)
    summary = {
        "tool_version": TOOL_VERSION, "ran_at": started.isoformat(),
        "run_id": run_id, "iteration": iteration,
        "runtime_s": round((datetime.now(timezone.utc) - started).total_seconds(), 1),
        "symbol": symbol, "timeframe": timeframe,
        "target_venue": "bitget", "reference_venue": "bybit",
        "n_bars": len(bars), "data_note": data_note,
        "dataset_sha256": manifest.get("sha256"),
        "data_quality": funnel.get("data_quality"),
        "hypotheses_total": len(raw), "procedural": n_procedural,
        "ai_generated": ai_meta["ideas"], "ai_calls": ai_meta["calls"],
        "duplicates": n_dup, "invalid": n_invalid,
        "executed": len(compiled),
        "funnel": {k: funnel[k] for k in ("universe", "discovery_survivors",
                                          "screening_survivors",
                                          "validation_survivors")},
        "n_trials_total": funnel.get("n_trials_total"),
        "m_raw": funnel.get("m_raw"),
        "m_effective": funnel.get("m_effective"),
        "replays_run": funnel.get("replays_run"),
        "expected_random_survivors_at_5pct":
            funnel.get("expected_random_survivors_at_5pct"),
        "multiple_testing_sensitivity": funnel.get("multiple_testing_sensitivity"),
        "cost_attribution_best": funnel.get("cost_attribution_best"),
        "baseline_best_lb": funnel.get("baseline_best_lb"),
        "execution_proxies": funnel.get("execution_proxies"),
        "proxy_note": ENG.PROXY_NOTE,
        "state_counts": by_state,
        "finalists": len(finals),
        "top_candidates": top[:10],
        "critiques": critiques[:20],
        "baselines": funnel["baselines"],
        "splits": funnel["splits"],
        "judge": "deterministic replay funnel (LLM votes never promote)",
        **_safety()}
    if write_reports:
        _write_reports(summary, funnel, log=log)
    return summary


def _write_reports(summary: dict, funnel: dict, log=print) -> None:
    import csv as _csv
    import os as _os
    out = ENG._out()
    tf = summary.get("timeframe", "1m")
    sfx = "" if tf == "1m" else f"_{tf}"
    tmp = out / f"edge_discovery_summary_v10_45_3{sfx}.json.tmp"
    tmp.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    _os.replace(tmp, out / f"edge_discovery_summary_v10_45_3{sfx}.json")
    # strategy scoreboard (every phase result)
    with open(out / f"strategy_scoreboard_v10_45_3{sfx}.csv", "w", newline="",
              encoding="utf-8") as f:
        w = _csv.writer(f)
        w.writerow(["phase", "strategy_id", "origin", "state", "n_trades",
                    "net_EV", "net_EV_lower_bound", "profit_factor", "win_rate",
                    "max_drawdown"])
        for e in funnel["results"]:
            m = e.get("metrics") or e.get("validation_metrics") or {}
            w.writerow([e.get("phase"), e.get("strategy_id"), e.get("origin"),
                        e.get("state"), m.get("n_trades"), m.get("net_EV"),
                        m.get("net_EV_lower_bound"), m.get("profit_factor"),
                        m.get("win_rate"), m.get("max_drawdown")])
    # cost stress scoreboard (finalists)
    with open(out / f"cost_stress_scoreboard_v10_45_3{sfx}.csv", "w", newline="",
              encoding="utf-8") as f:
        w = _csv.writer(f)
        w.writerow(["strategy_id", "state", "val_net_EV", "holdout_net_EV",
                    "cost_plus_25", "cost_plus_50", "spread_x2", "slip_x2",
                    "nonfill10_latency", "stress_ok"])
        for e in funnel["finalists"]:
            cs = e.get("cost_stress") or {}
            w.writerow([e["strategy_id"], e["state"],
                        (e.get("validation_metrics") or {}).get("net_EV"),
                        (e.get("holdout_metrics") or {}).get("net_EV"),
                        cs.get("cost_plus_25"), cs.get("cost_plus_50"),
                        cs.get("spread_x2"), cs.get("slip_x2"),
                        cs.get("nonfill10_latency"), e.get("stress_ok")])
    md = _memo(summary)
    (out / f"edge_discovery_report_v10_45_3{sfx}.md").write_text(md, encoding="utf-8")
    log(f"reports -> {out}")


def _memo(s: dict) -> str:
    ca = s.get("cost_attribution_best") or {}
    mt = s.get("multiple_testing_sensitivity") or {}
    lines = [
        "# V10.45.3 Multi-AI Edge Discovery — RESEARCH ONLY, NO LIVE", "",
        f"- ran_at: {s['ran_at']} · runtime: {s['runtime_s']}s · run_id: {s.get('run_id')}",
        f"- data: {s['data_note']} · sha256: {str(s.get('dataset_sha256'))[:16]} · "
        f"quality: {s.get('data_quality')}",
        f"- hypotheses: {s['hypotheses_total']} total "
        f"({s['procedural']} procedural + {s['ai_generated']} AI) · "
        f"dup={s['duplicates']} invalid={s['invalid']} executed={s['executed']}",
        f"- multiple testing: m={s.get('n_trials_total')} trials · "
        f"replays_run={s.get('replays_run')} · expected random survivors @5%: "
        f"{s.get('expected_random_survivors_at_5pct')} · sensitivity: {mt}",
        f"- funnel: {s['funnel']}",
        f"- state_counts: {s['state_counts']}", "",
        "## Cost attribution (best candidate, validation slice)", "",
        f"- strategy: {ca.get('strategy_id')}",
        f"- gross_EV: {ca.get('gross_EV')} · fee_impact: {ca.get('fee_impact')} · "
        f"spread_impact: {ca.get('spread_impact')} · slippage_impact: "
        f"{ca.get('slippage_impact')} · funding_impact: {ca.get('funding_impact')} "
        f"· net_EV: {ca.get('net_EV')}", "",
        f"- execution proxies (cap promotion at SHADOW): {s.get('execution_proxies')}", "",
        "## Top candidates (holdout-ranked)", ""]
    for e in s.get("top_candidates") or []:
        vm = e.get("validation_metrics") or {}
        hm = e.get("holdout_metrics") or {}
        lines.append(
            f"- **{e['strategy_id']}** [{e['state']}] origin={e.get('origin')} · "
            f"val EV={vm.get('net_EV')} lb={vm.get('net_EV_lower_bound')} "
            f"n={vm.get('n_trades')} · holdout EV={hm.get('net_EV')} "
            f"lb={hm.get('net_EV_lower_bound')} n={hm.get('n_trades')} · "
            f"stress_ok={e.get('stress_ok')}")
    if not s.get("top_candidates"):
        lines.append("- NONE (no strategy survived validation)")
    lines += ["", "## Baselines (validation slice, same replay/costs)", ""]
    for k, v in (s.get("baselines") or {}).items():
        if isinstance(v, dict):
            lines.append(f"- {k}: EV={v.get('net_EV')} n={v.get('n_trades')} "
                         f"PF={v.get('profit_factor')}")
        else:
            lines.append(f"- {k}: {v}")
    lines += ["", "Model votes never promote. The judge is the deterministic "
              "replay funnel. Max state: PAPER_CANDIDATE_RESEARCH_ONLY. "
              "**FINAL_RECOMMENDATION=NO LIVE.**"]
    return "\n".join(lines) + "\n"
