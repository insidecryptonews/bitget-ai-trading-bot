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

import copy
import hashlib
from collections.abc import Callable
from typing import Any

from . import contracts as C
from . import causal_ledger as CL
from . import causal_stats as CS
from . import event_clock as EC
from . import families as FAM
from . import edge_search as ES
from .discovery_dataset import DiscoveryPartitions


RANDOM_BASELINE_SPEC = {
    "policy_id": "PREREGISTERED_RANDOM_BASELINE_V10_47_21",
    "seed_prefix": "v10.47.21",
    "trade_probability_numerator": 64,
    "trade_probability_denominator": 256,
    "side_rule": "sha256_byte_1_lt_128_long_else_short",
    "simulations_per_candidate": 1,
    "match_contract": "V10_47_21_EXACT_ONE_TO_ONE",
}


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
    # nominal spec hash (registry closure) — includes the name
    specs = {name: C.canonical_hash({"participant": name, "exit": ex})
             for name, (fn, ex) in deciders.items()}
    # SEMANTIC dedup (Work audit P2.3): fingerprint each policy by the sequence of
    # (action, side) decisions it emits over a FIXED synthetic fixture. Two
    # operationally-identical policies collide regardless of their names.
    fps = _behavioral_fingerprints(deciders, symbol, venue, timeframe, gen)
    by_fp: dict[str, list[str]] = {}
    for name, fp in fps.items():
        by_fp.setdefault(fp, []).append(name)
    duplicated = {fp: sorted(names) for fp, names in by_fp.items() if len(names) > 1}
    m_nominal = len(specs)
    m_unique_hypotheses = len(specs)
    m_unique_results = len(by_fp)
    registry_contract = {
        "specs": specs,
        "fingerprints": fps,
        "symbol": symbol,
        "venue": venue,
        "timeframe": timeframe,
        "gen": gen,
        "m_nominal": m_nominal,
        "m_unique_hypotheses": m_unique_hypotheses,
        "m_unique_results": m_unique_results,
        "m_global": m_unique_hypotheses,
        "correction": "bonferroni",
        "alpha": 0.05,
        "baseline_policy_spec": RANDOM_BASELINE_SPEC,
        "baseline_tolerance_spec": CS.BASELINE_TOLERANCE_SPEC,
        "closed_before_metrics": True,
    }
    registry_hash = C.canonical_hash(registry_contract)
    return {"deciders": deciders, "specs": specs, "fingerprints": fps,
            "m_nominal": m_nominal,
            "m_unique_hypotheses": m_unique_hypotheses,
            "m_unique_results": m_unique_results,
            "m_global": m_unique_hypotheses,
            "duplicated_runs": duplicated,
            "registry_hash": registry_hash,
            "registry_contract": registry_contract,
            "specs_hash": C.canonical_hash(specs),
            "baseline_policy_spec": copy.deepcopy(RANDOM_BASELINE_SPEC),
            "baseline_policy_spec_hash": C.canonical_hash(RANDOM_BASELINE_SPEC),
            "correction": "bonferroni",
            "alpha": 0.05, "baseline_tolerance_spec_hash": C.canonical_hash(
                CS.BASELINE_TOLERANCE_SPEC),
            "closed": True, "closed_before_metrics": True}


def _behavioral_fingerprints(deciders: dict, symbol, venue, timeframe, gen) -> dict:
    """Behavioural fingerprint per participant: hash of the (action, side)
    decision sequence over a fixed synthetic fixture. Semantic, name-independent."""
    import random as _r
    rng = _r.Random(20240714)
    price, fx = 100.0, []
    for i in range(400):
        ph = (i // 80) % 3
        drift = 0.001 if ph == 0 else (-0.001 if ph == 2 else 0.0)
        new = price * (1 + drift + rng.uniform(-0.0008, 0.0008))
        fx.append({"ts": i * 60_000, "open": price, "high": max(price, new) * 1.0006,
                   "low": min(price, new) * 0.9994, "close": new, "volume": 10.0})
        price = new
    sigs = ES.precompute_sigs(fx)
    out = {}
    for name, (fn, ex) in deciders.items():
        seq = []
        traded = False
        for i in range(CL.WARMUP, len(fx) - 1):
            d = fn({"_sig": sigs[i], "ts": int(fx[i]["ts"])}, f"{symbol}:{fx[i]['ts']}",
                   int(fx[i]["ts"]) + 60_000, "c")
            act, side = d.get("decision_action"), d.get("side")
            if act == "TRADE":
                traded = True
            seq.append((act, side))
        # policies that never fire on the fixture are NOT collapsed together
        # (that would understate the hypothesis count); they keep a distinct id.
        out[name] = (C.canonical_hash({"seq": seq}) if traded
                     else C.canonical_hash({"nofire": name, "exit": ex}))
    return out


SHADOW_GATES_V2 = {"min_n_eff": 30, "min_net_pnl_eur": 0.0,
                   "matched_random_alpha": 0.05, "min_bootstrap_lb_eur": 0.0}


def _random_baseline_decider(*, symbol: str, venue: str, timeframe: str,
                             gen: str):
    """Single preregistered deterministic random-policy realization.

    It is run once.  Exact pairing later decides whether any baseline trade is
    compatible; no repeated simulations or outcome-conditioned selection occur.
    """
    def decide(feats, event_id, dt, cluster):
        digest = hashlib.sha256(
            f"{RANDOM_BASELINE_SPEC['seed_prefix']}|{event_id}".encode(
                "utf-8"
            )
        ).digest()
        if digest[0] < 64:
            side = "LONG" if digest[1] < 128 else "SHORT"
            return FAM._mk(
                "TRADE", side, 0.5, symbol=symbol, venue=venue,
                timeframe=timeframe, event_id=event_id, dt=dt, gen_id=gen,
                reason="PREREGISTERED_RANDOM_BASELINE",
            )
        return FAM._mk(
            "ABSTAIN_LOW_REWARD", "FLAT", 0.5, symbol=symbol, venue=venue,
            timeframe=timeframe, event_id=event_id, dt=dt, gen_id=gen,
            reason="PREREGISTERED_RANDOM_BASELINE_NO_TRADE",
        )
    return decide


def evaluate_candidate(bars_tr, sigs_tr, bars_validation, sigs_validation, bars_wf,
                       sigs_wf, decide_fn, exit_params, *, symbol, timeframe,
                       m_unique):
    """Evaluate TRAIN and VALIDATION before any WALK_FORWARD computation.

    ``sigs_wf`` may be a lazy zero-argument supplier.  It is called only after
    VALIDATION admission.  The holdout is neither an argument nor an import.
    """
    original_params = copy.deepcopy(exit_params)
    policy_identity = {
        "decider_object_id": id(decide_fn),
        "parameters_before": copy.deepcopy(original_params),
    }
    sel = CL.drive_causal(bars_tr, sigs_tr, decide_fn, exit_params,
                          symbol=symbol, timeframe=timeframe)
    m = _metrics(sel["trades"], sel["counters"], timeframe)
    baseline = CL.drive_causal(
        bars_tr, sigs_tr,
        _random_baseline_decider(
            symbol=symbol, venue="research_baseline", timeframe=timeframe,
            gen="v10_47_21",
        ),
        exit_params, symbol=symbol, timeframe=timeframe,
    )
    baseline_trades = []
    for index, trade in enumerate(baseline["trades"]):
        row = copy.deepcopy(trade)
        row["baseline_trade_id"] = row.pop(
            "candidate_trade_id", row.get("trade_id", f"baseline-{index}")
        )
        row["baseline_net_eur"] = row["net_eur"]
        baseline_trades.append(row)
    paired = CS.matched_random_paired(
        candidate_trades=sel["trades"], baseline_trades=baseline_trades,
        timeframe=timeframe, m_global=m_unique,
        alpha=SHADOW_GATES_V2["matched_random_alpha"],
    )
    cons = CL.drive_causal(bars_tr, sigs_tr, decide_fn, exit_params,
                           symbol=symbol, timeframe=timeframe,
                           scenario_cost="conservative")
    cons_net = float(sum(t["net_eur"] for t in cons["trades"]))
    val = CL.drive_causal(bars_validation, sigs_validation, decide_fn, exit_params,
                          symbol=symbol, timeframe=timeframe)
    val_metrics = _metrics(val["trades"], val["counters"], timeframe)
    val_net = float(val_metrics["net_pnl_eur"])
    validation_reason = None
    if not val["trades"]:
        validation_reason = "NO_VALIDATION_TRADES"
    elif val_net <= 0:
        validation_reason = "VALIDATION_NET_NOT_POSITIVE"
    elif val_metrics["n_eff_final"] < SHADOW_GATES_V2["min_n_eff"]:
        validation_reason = "VALIDATION_N_EFF_INSUFFICIENT"
    validation_gate = validation_reason is None
    base_gates = {
        "net_positive_selection": m["net_pnl_eur"] > 0,
        "n_eff_sufficient": m["n_eff_final"] >= SHADOW_GATES_V2["min_n_eff"],
        "top3_robust": m["net_without_top3_eur"] >= 0,
        "baseline_match_complete": paired["match_status"] == "OK",
        "beats_matched_random_paired": paired["beats_matched_random"],
        "conservative_survives": cons_net > 0,
        "validation_positive": val_net > 0,
        "validation_n_eff_sufficient": (
            val_metrics["n_eff_final"] >= SHADOW_GATES_V2["min_n_eff"]
        ),
    }
    policy_identity["parameters_after_validation"] = copy.deepcopy(exit_params)
    policy_identity["parameters_unchanged"] = exit_params == original_params
    if not validation_gate:
        gates = {**base_gates, "walk_forward_positive": False, "all_pass": False}
        return {
            "selection_metrics": m,
            "matched_random_paired": paired,
            "conservative_net_eur": round(cons_net, 6),
            "validation_net_eur": round(val_net, 6),
            "validation_trades": len(val["trades"]),
            "validation_metrics": val_metrics,
            "validation_gate": False,
            "validation_rejection_reason": validation_reason,
            "walk_forward_called": False,
            "walk_forward_metrics": None,
            "walk_forward_net_eur": None,
            "status": "REJECTED_AT_VALIDATION",
            "next_stage": "NONE",
            "paired_lower_bound_eur": paired["paired_lower_bound_eur"],
            "baseline_coverage": paired["coverage"],
            "policy_identity": policy_identity,
            "gates": gates,
            "is_shadow_candidate": False,
        }
    wf_signals = sigs_wf() if isinstance(sigs_wf, Callable) else sigs_wf
    wf = CL.drive_causal(bars_wf, wf_signals, decide_fn, exit_params,
                         symbol=symbol, timeframe=timeframe)
    wf_metrics = _metrics(wf["trades"], wf["counters"], timeframe)
    wf_net = float(wf_metrics["net_pnl_eur"])
    gates = {**base_gates, "walk_forward_positive": wf_net > 0}
    gates["all_pass"] = all(gates.values())
    policy_identity["parameters_after_walk_forward"] = copy.deepcopy(exit_params)
    policy_identity["parameters_unchanged"] = exit_params == original_params
    return {"selection_metrics": m, "matched_random_paired": paired,
            "conservative_net_eur": round(cons_net, 6),
            "validation_net_eur": round(val_net, 6),
            "validation_trades": len(val["trades"]),
            "validation_metrics": val_metrics,
            "validation_gate": True,
            "validation_rejection_reason": None,
            "walk_forward_called": True,
            "walk_forward_metrics": wf_metrics,
            "walk_forward_net_eur": round(wf_net, 6),
            "status": "WALK_FORWARD_EVALUATED",
            "next_stage": "SHADOW_GATE" if gates["all_pass"] else "NONE",
            "paired_lower_bound_eur": paired["paired_lower_bound_eur"],
            "baseline_coverage": paired["coverage"],
            "policy_identity": policy_identity,
            "gates": gates, "is_shadow_candidate": gates["all_pass"]}


def run_causal_tournament(discovery_partitions: DiscoveryPartitions, *,
                          symbol: str, venue: str, timeframe: str, gen: str,
                          holdout_commitment: dict,
                          ref_bars_by_ts=None, log=lambda *a: None) -> dict:
    """Run discovery without ever receiving or loading holdout observations.

    VALIDATION admission is the boundary that makes WALK_FORWARD computation
    reachable.  Only commitment metadata is present in this process.
    """
    import time as _t
    if not isinstance(discovery_partitions, DiscoveryPartitions):
        raise TypeError("discovery_partitions must come from DiscoveryDatasetLoader")
    if holdout_commitment.get("state") != "SEALED" \
            or len(str(holdout_commitment.get("commitment_sha256", ""))) != 64:
        raise ValueError("a valid SEALED holdout commitment is required")
    bars_tr, bars_va, bars_wf = discovery_partitions.as_mutable()
    n_discovery = len(bars_tr) + len(bars_va) + len(bars_wf)
    n_holdout = int(holdout_commitment.get("n_bars", 0))
    n = n_discovery + n_holdout
    tr_end = len(bars_tr)
    val_end = tr_end + len(bars_va)
    wf_end = val_end + len(bars_wf)
    sp = {
        "train": (0, tr_end),
        "validation": (tr_end, val_end),
        "walk_forward": (val_end, wf_end),
        "holdout": tuple(holdout_commitment.get("index_range", (wf_end, n))),
        "selection_end_index": tr_end,
        "holdout_start_index": wf_end,
    }
    reg = preregister(symbol, venue, timeframe, gen, ref_bars_by_ts)

    # PHYSICAL SEAL: the holdout bars go into a guarded object, never loaded here.
    # features computed ONLY over [0, hstart) — the holdout range is not touched.
    t0 = _t.time()
    sigs_tr = ES.precompute_sigs(bars_tr)
    train_validation = bars_tr + bars_va
    sigs_train_validation = ES.precompute_sigs(train_validation)
    sigs_va = sigs_train_validation[len(bars_tr):]
    log(
        f"  [sigs] {n_discovery}/{n} discovery bars "
        f"(holdout {n_holdout} PHYSICALLY ABSENT) in {round(_t.time()-t0, 1)}s "
        f"| m_nominal={reg['m_nominal']} "
        f"m_unique_results={reg['m_unique_results']} holdout_state=SEALED"
    )
    wf_signal_cache: list | None = None

    def _walk_forward_signals():
        nonlocal wf_signal_cache
        if wf_signal_cache is None:
            all_discovery = bars_tr + bars_va + bars_wf
            all_signals = ES.precompute_sigs(all_discovery)
            wf_signal_cache = all_signals[len(bars_tr) + len(bars_va):]
        return wf_signal_cache
    results: dict = {}
    for name, (fn, ex) in reg["deciders"].items():
        out = CL.drive_causal(bars_tr, sigs_tr, fn, ex, symbol=symbol,
                              timeframe=timeframe)
        m = _metrics(out["trades"], out["counters"], timeframe)
        results[name] = {"metrics": m}
    candidates = {n_: r for n_, r in results.items()
                  if r["metrics"]["classification"] == "NET_EDGE_POSITIVE"
                  and n_ != "D_no_trade"}
    shadow = []
    validation_admitted_candidates: list[str] = []
    validation_rejected_candidates: list[str] = []
    for name in candidates:
        fn, ex = reg["deciders"][name]
        ev = evaluate_candidate(bars_tr, sigs_tr, bars_va, sigs_va, bars_wf,
                                _walk_forward_signals, fn, ex,
                                symbol=symbol, timeframe=timeframe,
                                m_unique=reg["m_global"])
        results[name]["gate"] = ev
        if ev["validation_gate"]:
            validation_admitted_candidates.append(name)
        else:
            validation_rejected_candidates.append(name)
        if ev["is_shadow_candidate"]:
            shadow.append(name)
        log(f"   gate {name}: shadow={ev['is_shadow_candidate']} "
            f"val={ev['validation_net_eur']}€ wf={ev['walk_forward_net_eur']}€ "
            f"paired_lb={ev['paired_lower_bound_eur']}€ "
            f"cov={ev['baseline_coverage']} match={ev['gates']['baseline_match_complete']}")
    return {"symbol": symbol, "venue": venue, "timeframe": timeframe,
            "data_generation_id": gen, "n_bars": n, "split": sp,
            "registry": {k: reg[k] for k in ("m_nominal", "m_unique_hypotheses",
                         "m_unique_results", "m_global", "duplicated_runs",
                         "registry_hash", "registry_contract",
                         "specs_hash", "baseline_policy_spec",
                         "baseline_policy_spec_hash", "correction", "alpha",
                         "baseline_tolerance_spec_hash",
                         "closed", "closed_before_metrics")},
            "holdout": {"state": "SEALED",
                        "commitment_sha256": holdout_commitment["commitment_sha256"],
                        "index_range": list(sp["holdout"]), "n_bars": n_holdout,
                        "access_log": [], "physically_loaded": False,
                        "capability_present": False},
            "results": results, "n_net_positive": len(candidates),
            "validation_admitted_candidates": validation_admitted_candidates,
            "validation_rejected_candidates": validation_rejected_candidates,
            "shadow_candidates": shadow,
            "walk_forward_precomputed": wf_signal_cache is not None,
            "holdout_touched": False}
