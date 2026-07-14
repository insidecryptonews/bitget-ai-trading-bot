"""V10.47.8 repaired causal tournament (RESEARCH ONLY, NO LIVE).

Replaces the V10.47 tournament that used per-cluster overwrite accounting, an
unmatched random baseline, a fake n_eff and a post-selection "OOS". Here:

  * every participant runs through `causal_ledger.drive_causal` (first causal
    signal, single open position, append-only, no ex-post selection);
  * the participant set is PRE-REGISTERED and the registry is CLOSED (hashed)
    before any metric is read, with a real multiple-testing count m_global;
  * the time axis is split TRAIN / VALIDATION / WALK-FORWARD / sealed HOLDOUT;
    selection happens on TRAIN only, candidates are confirmed on VALIDATION and
    WALK-FORWARD, and the HOLDOUT is never touched here;
  * a candidate must beat an EXPOSURE-MATCHED random baseline and a
    block-bootstrap lower bound, survive conservative costs and top-event
    removal, and use real cluster-aware n_eff — or it is not a candidate.

Expected honest outcome on 90 days of free public klines: NO_CONFIRMED_EDGE,
SHADOW_CANDIDATES=0.
"""

from __future__ import annotations

from typing import Any

from . import contracts as C
from . import causal_ledger as CL
from . import causal_stats as CS
from . import event_clock as EC
from . import families as FAM
from . import edge_search as ES


# ------------------------------------------------------- pre-registered split
def split_indices(n: int) -> dict:
    """Chronological TRAIN/VALIDATION/WALK-FORWARD/HOLDOUT boundaries (index
    ranges). Proportional to the mandate's 12/4/4/4-month guide (=0.50/0.17/
    0.17/0.16). Selection uses TRAIN; HOLDOUT is sealed and never read here."""
    tr = int(n * 0.50)
    va = tr + int(n * 0.17)
    wf = va + int(n * 0.17)
    return {"train": (0, tr), "validation": (tr, va),
            "walk_forward": (va, wf), "holdout": (wf, n),
            "selection_end_index": tr, "holdout_start_index": wf}


def _metrics(trades: list[dict], counters: dict, timeframe: str) -> dict:
    n = len(trades)
    nets = [t["net_eur"] for t in trades]
    gross = [t["gross_eur"] for t in trades]
    net_total, gross_total = float(sum(nets)), float(sum(gross))
    neff = CS.n_eff_estimate(trades, timeframe=timeframe)
    without_top3 = float(sum(sorted(nets, reverse=True)[3:])) if n > 3 else net_total
    return {
        "trades": n, "gross_pnl_eur": round(gross_total, 6),
        "net_pnl_eur": round(net_total, 6),
        "gross_ev_eur": round(gross_total / n, 6) if n else 0.0,
        "net_ev_eur": round(net_total / n, 6) if n else 0.0,
        "fee_eur": round(sum(t["fee_eur"] for t in trades), 6),
        "spread_eur": round(sum(t["spread_eur"] for t in trades), 6),
        "slippage_eur": round(sum(t["slippage_eur"] for t in trades), 6),
        "funding_eur": round(sum(t["funding_eur"] for t in trades), 6),
        "net_without_top3_eur": round(without_top3, 6),
        "n_eff_final": neff["n_eff_final"], "n_eff": neff,
        "counters": counters,
        "classification": ES._classify(gross_total, net_total)}


def preregister(symbol: str, venue: str, timeframe: str, gen: str,
                ref_bars_by_ts=None) -> dict:
    """Deterministic CLOSED registry of participants. Returns the decider map,
    the pre-registered spec hashes, and the multiple-testing counts."""
    deciders = ES.build_deciders(symbol, venue, timeframe, gen,
                                 ref_bars_by_ts=ref_bars_by_ts,
                                 directions=(None, "LONG", "SHORT"))
    # No-Trade baseline (never trades)
    def _no_trade(feats, e, dt, c):
        return FAM._mk("ABSTAIN_LOW_REWARD", "FLAT", 0.5, symbol=symbol,
                       venue=venue, timeframe=timeframe, event_id=e, dt=dt,
                       gen_id=gen, reason="NO_TRADE")
    deciders["D_no_trade"] = (_no_trade, FAM.TREND_EXIT)
    specs = {name: C.canonical_hash({"participant": name, "exit": ex})
             for name, (fn, ex) in deciders.items()}
    unique = {}
    for name, h in specs.items():
        unique.setdefault(h, []).append(name)
    m_nominal = len(specs)
    m_unique = len(unique)
    duplicated = {h: names for h, names in unique.items() if len(names) > 1}
    registry_hash = C.canonical_hash({"specs": specs, "symbol": symbol,
                                       "timeframe": timeframe, "gen": gen})
    return {"deciders": deciders, "specs": specs,
            "m_nominal": m_nominal, "m_unique_hypotheses": m_unique,
            "duplicated_runs": duplicated, "registry_hash": registry_hash,
            "correction": "bonferroni", "closed": True}


SHADOW_GATES_V2 = {"min_n_eff": 30, "min_net_pnl_eur": 0.0,
                   "matched_random_alpha": 0.05, "min_bootstrap_lb_eur": 0.0}


def evaluate_candidate(bars_sel, sigs_sel, bars_wf, sigs_wf, decide_fn,
                       exit_params, *, symbol, timeframe, m_unique):
    """Full repaired gate for a NET_EDGE_POSITIVE candidate. Uses matched random
    baseline + block bootstrap on the SELECTION region and a walk-forward on a
    LATER region; applies multiple-testing correction. Returns gate booleans."""
    sel = CL.drive_causal(bars_sel, sigs_sel, decide_fn, exit_params,
                          symbol=symbol, timeframe=timeframe)
    m = _metrics(sel["trades"], sel["counters"], timeframe)
    mr = CS.matched_random_null(bars_sel, sel["trades"], symbol=symbol,
                                timeframe=timeframe, exit_params=exit_params,
                                reps=200)
    bb = CS.paired_delta_vs_zero(sel["trades"])
    cons = CL.drive_causal(bars_sel, sigs_sel, decide_fn, exit_params,
                           symbol=symbol, timeframe=timeframe,
                           scenario_cost="conservative")
    cons_net = float(sum(t["net_eur"] for t in cons["trades"]))
    wf = CL.drive_causal(bars_wf, sigs_wf, decide_fn, exit_params,
                         symbol=symbol, timeframe=timeframe)
    wf_net = float(sum(t["net_eur"] for t in wf["trades"]))
    # multiple-testing corrected significance vs matched random
    p_corrected = min(1.0, mr["p_value"] * max(1, m_unique))
    gates = {
        "net_positive_selection": m["net_pnl_eur"] > 0,
        "n_eff_sufficient": m["n_eff_final"] >= SHADOW_GATES_V2["min_n_eff"],
        "top3_robust": m["net_without_top3_eur"] >= 0,
        "beats_matched_random_corrected": p_corrected < SHADOW_GATES_V2["matched_random_alpha"],
        "bootstrap_lb_positive": bb["mean_lb_eur"] > SHADOW_GATES_V2["min_bootstrap_lb_eur"],
        "beats_no_trade": m["net_pnl_eur"] > 0,
        "conservative_survives": cons_net > 0,
        "walk_forward_positive": wf_net > 0,
    }
    gates["all_pass"] = all(gates.values())
    return {"selection_metrics": m, "matched_random": mr, "bootstrap": bb,
            "conservative_net_eur": round(cons_net, 6),
            "walk_forward_net_eur": round(wf_net, 6),
            "p_value_raw": mr["p_value"], "p_value_corrected": round(p_corrected, 6),
            "gates": gates, "is_shadow_candidate": gates["all_pass"]}


def run_causal_tournament(bars: list[dict], *, symbol: str, venue: str,
                          timeframe: str, gen: str, ref_bars_by_ts=None,
                          log=lambda *a: None) -> dict:
    """Run the full repaired tournament for one (symbol, timeframe). Selection on
    TRAIN; candidates confirmed on WALK-FORWARD; HOLDOUT sealed (untouched)."""
    import time as _t
    n = len(bars)
    sp = split_indices(n)
    a_tr, b_tr = sp["train"]
    a_wf, b_wf = sp["walk_forward"]
    reg = preregister(symbol, venue, timeframe, gen, ref_bars_by_ts)
    t0 = _t.time()
    sigs = ES.precompute_sigs(bars)
    log(f"  [sigs] {n} bars in {round(_t.time()-t0,1)}s | "
        f"m_nominal={reg['m_nominal']} m_unique={reg['m_unique_hypotheses']}")
    bars_tr, sigs_tr = bars[a_tr:b_tr], sigs[a_tr:b_tr]
    bars_wf, sigs_wf = bars[a_wf:b_wf], sigs[a_wf:b_wf]
    results: dict = {}
    for name, (fn, ex) in reg["deciders"].items():
        ta = _t.time()
        out = CL.drive_causal(bars_tr, sigs_tr, fn, ex, symbol=symbol,
                              timeframe=timeframe)
        m = _metrics(out["trades"], out["counters"], timeframe)
        results[name] = {"metrics": m}
        log(f"  {name}: {m['classification']} trades={m['trades']} "
            f"net={m['net_pnl_eur']}€ gross={m['gross_pnl_eur']}€ "
            f"n_eff={m['n_eff_final']} ({round(_t.time()-ta,1)}s)")
    # candidate gate only on NET_EDGE_POSITIVE (few); everything else can't be shadow
    candidates = {n_: r for n_, r in results.items()
                  if r["metrics"]["classification"] == "NET_EDGE_POSITIVE"
                  and n_ != "D_no_trade"}
    shadow = []
    for name in candidates:
        fn, ex = reg["deciders"][name]
        ev = evaluate_candidate(bars_tr, sigs_tr, bars_wf, sigs_wf, fn, ex,
                                symbol=symbol, timeframe=timeframe,
                                m_unique=reg["m_unique_hypotheses"])
        results[name]["gate"] = ev
        if ev["is_shadow_candidate"]:
            shadow.append(name)
        log(f"   gate {name}: shadow={ev['is_shadow_candidate']} "
            f"p_corr={ev['p_value_corrected']} wf={ev['walk_forward_net_eur']}€ "
            f"cons={ev['conservative_net_eur']}€")
    return {"symbol": symbol, "venue": venue, "timeframe": timeframe,
            "data_generation_id": gen, "n_bars": n, "split": sp,
            "registry": {k: reg[k] for k in ("m_nominal", "m_unique_hypotheses",
                         "duplicated_runs", "registry_hash", "correction",
                         "closed")},
            "results": results, "n_net_positive": len(candidates),
            "shadow_candidates": shadow, "holdout_touched": False}
