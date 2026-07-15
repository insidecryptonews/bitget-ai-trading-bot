"""V10.47.8 repaired causal tournament (RESEARCH ONLY, NO LIVE).

Replaces the V10.47 tournament that used per-cluster overwrite accounting, an
unmatched random baseline, a fake n_eff and a post-selection "OOS". Here:

  * every participant runs through `causal_ledger.drive_causal` (first causal
    signal, single open position, append-only, no ex-post selection);
  * the participant set and the complete 12-tournament campaign are
    PRE-REGISTERED and CLOSED (hashed) before any metric is read;
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
import marshal
import math
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import contracts as C
from . import causal_ledger as CL
from . import causal_stats as CS
from . import campaign_authority as CA
from . import event_clock as EC
from . import families as FAM
from . import edge_search as ES
from .discovery_dataset import (
    DiscoveryPartitions,
    load_verified_holdout_commitment,
    load_verified_reference,
    verify_discovery_partitions,
)


RANDOM_BASELINE_SPEC = {
    "policy_id": "PREREGISTERED_RANDOM_BASELINE_V10_47_23",
    "seed_prefix": "v10.47.21",
    "trade_probability_numerator": 64,
    "trade_probability_denominator": 256,
    "side_rule": "sha256_byte_1_lt_128_long_else_short",
    "simulations_per_candidate": 1,
    "match_contract": "V10_47_23_EXACT_BIJECTIVE_ONE_TO_ONE",
}

CAMPAIGN_SYMBOLS = CA.EXPECTED_SYMBOLS
CAMPAIGN_TIMEFRAMES = CA.EXPECTED_TIMEFRAMES
CAMPAIGN_CORRECTION_METHOD = "bonferroni"
CAMPAIGN_ALPHA = 0.05


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
    money_fields = (
        "net_eur", "gross_eur", "fee_eur", "spread_eur",
        "slippage_eur", "funding_eur",
    )
    try:
        values = {
            field: [float(trade[field]) for trade in trades]
            for field in money_fields
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid trade metrics") from exc
    if any(
            not math.isfinite(value)
            for field_values in values.values() for value in field_values):
        raise ValueError("non-finite trade metrics")
    nets, gross = values["net_eur"], values["gross_eur"]
    net_total, gross_total = float(sum(nets)), float(sum(gross))
    neff = CS.n_eff_estimate(trades, timeframe=timeframe)
    without_top3 = float(sum(sorted(nets, reverse=True)[3:])) if n > 3 else net_total
    return {
        "trades": n, "gross_pnl_eur": round(gross_total, 6),
        "net_pnl_eur": round(net_total, 6),
        "gross_ev_eur": round(gross_total / n, 6) if n else 0.0,
        "net_ev_eur": round(net_total / n, 6) if n else 0.0,
        "fee_eur": round(sum(values["fee_eur"]), 6),
        "spread_eur": round(sum(values["spread_eur"]), 6),
        "slippage_eur": round(sum(values["slippage_eur"]), 6),
        "funding_eur": round(sum(values["funding_eur"]), 6),
        "net_without_top3_eur": round(without_top3, 6),
        "n_eff_final": neff["n_eff_final"], "n_eff": neff,
        "counters": counters,
        "classification": ES._classify(gross_total, net_total),
        "metrics_valid": True}


def _metrics_are_finite(metrics: dict | None) -> bool:
    if not isinstance(metrics, dict) or metrics.get("metrics_valid") is not True:
        return False
    for key in (
        "gross_pnl_eur", "net_pnl_eur", "gross_ev_eur", "net_ev_eur",
        "net_without_top3_eur", "n_eff_final",
    ):
        value = metrics.get(key)
        if type(value) not in (int, float) or not math.isfinite(float(value)):
            return False
    return True


def _safe_metrics(trades: list[dict], counters: dict, timeframe: str) -> dict:
    try:
        return _metrics(trades, counters, timeframe)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        return {
            "trades": len(trades), "gross_pnl_eur": 0.0,
            "net_pnl_eur": 0.0, "gross_ev_eur": 0.0, "net_ev_eur": 0.0,
            "fee_eur": 0.0, "spread_eur": 0.0, "slippage_eur": 0.0,
            "funding_eur": 0.0, "net_without_top3_eur": 0.0,
            "n_eff_final": 0.0, "n_eff": {}, "counters": counters,
            "classification": "INVALID_METRICS", "metrics_valid": False,
            "metrics_error": type(exc).__name__,
        }


def _ledger_integrity(ledger: CL.ImmutableLedger, trades: list[dict]) -> dict:
    records = ledger.records()
    kinds: dict[str, int] = {}
    for record in records:
        kinds[record["kind"]] = kinds.get(record["kind"], 0) + 1
    sequence_ok = [record["seq"] for record in records] == list(range(len(records)))
    required_atr_records = {
        kind: kinds.get(kind, 0) for kind in ("SIGNAL", "ENTRY", "POSITION", "CLOSE")
    }
    trade_ids = {trade.get("trade_id") for trade in trades}
    return {
        "schema": "v10_47_22_append_only_ledger_index",
        "records": len(records),
        "record_kinds": kinds,
        "sequence_contiguous": sequence_ok,
        "ledger_sha256": C.canonical_hash(records),
        "executed_trades": len(trades),
        "unique_trade_ids": len(trade_ids - {None}),
        "required_atr_record_counts": required_atr_records,
        "close_matches_trade_count": kinds.get("CLOSE", 0) == len(trades),
        "append_only_defensive_copy": True,
        "research_only": True,
        "final_recommendation": "NO LIVE",
    }


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


def preregister_campaign() -> dict:
    """Public read-only view of the tracked canonical campaign authority."""
    return CA.public_campaign_contract()


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


def _structural_value(value: Any, seen: set[int] | None = None) -> Any:
    """Canonicalize a policy closure without executing it.

    The identity is used only to compare a supplied participant with the
    canonical participant rebuilt in this process. It is not caller authority.
    """
    if seen is None:
        seen = set()
    if value is None or type(value) in (bool, int, str):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise CA.CampaignAuthorityError("POLICY_CALLABLE_NONFINITE_STATE")
        return {"float_hex": value.hex()}
    if isinstance(value, bytes):
        return {"bytes_sha256": hashlib.sha256(value).hexdigest()}
    identity = id(value)
    if identity in seen:
        return {"cycle": type(value).__qualname__}
    seen.add(identity)
    try:
        if isinstance(value, types.FunctionType):
            closure = []
            for cell in value.__closure__ or ():
                try:
                    cell_value = cell.cell_contents
                except ValueError:
                    cell_value = {"empty_cell": True}
                closure.append(_structural_value(cell_value, seen))
            return {
                "kind": "function",
                "module": value.__module__,
                "qualname": value.__qualname__,
                "code_sha256": hashlib.sha256(
                    marshal.dumps(value.__code__)
                ).hexdigest(),
                "defaults": _structural_value(value.__defaults__, seen),
                "kwdefaults": _structural_value(value.__kwdefaults__, seen),
                "closure": closure,
            }
        if isinstance(value, dict):
            if len(value) > 1_000 and all(
                    type(key) is int
                    and type(item) in (int, float)
                    and not isinstance(item, bool)
                    and math.isfinite(float(item))
                    for key, item in value.items()):
                digest = hashlib.sha256()
                for key, item in sorted(value.items()):
                    digest.update(f"{key}:{float(item).hex()}\n".encode("ascii"))
                return {"numeric_mapping_sha256": digest.hexdigest(), "size": len(value)}
            rows = [
                (_structural_value(key, seen), _structural_value(item, seen))
                for key, item in value.items()
            ]
            rows.sort(key=lambda row: C.canonical_hash(row[0]))
            return {"mapping": rows}
        if isinstance(value, (list, tuple)):
            return {
                "sequence_type": type(value).__name__,
                "items": [_structural_value(item, seen) for item in value],
            }
        if isinstance(value, (set, frozenset)):
            items = [_structural_value(item, seen) for item in value]
            items.sort(key=C.canonical_hash)
            return {"set_type": type(value).__name__, "items": items}
    finally:
        seen.discard(identity)
    raise CA.CampaignAuthorityError(
        f"POLICY_CALLABLE_UNSUPPORTED_STATE:{type(value).__qualname__}"
    )


def _callable_fingerprint(decide_fn: Callable) -> str:
    if not isinstance(decide_fn, types.FunctionType):
        raise CA.CampaignAuthorityError("POLICY_CALLABLE_TYPE_INVALID")
    return C.canonical_hash(_structural_value(decide_fn))


SHADOW_GATES_V2 = {"min_n_eff": 30, "min_net_pnl_eur": 0.0,
                   "matched_random_alpha": 0.05, "min_bootstrap_lb_eur": 0.0}


def _authorize_candidate_policy(*, decide_fn, exit_params: dict,
                                hypothesis_id: str,
                                authorization: CA.TournamentAuthorization,
                                verified_reference=None) -> dict:
    """Derive policy identity from canonical code, never caller hashes."""
    context = CA.validate_full_authorization(authorization)
    authority = CA.load_campaign_authority(context.campaign_id)
    symbol, timeframe = context.symbol, context.timeframe
    entry = context.entry
    canonical = preregister(
        symbol, entry["venue"], timeframe, entry["dataset_source_generation_id"],
        verified_reference,
    )
    expected_registry = {
        "registry_hash": entry["tournament_registry_hash"],
        "specs_hash": entry["participant_specs_hash"],
        "baseline_policy_spec_hash": entry["baseline_spec_hash"],
        "baseline_tolerance_spec_hash": entry["tolerance_spec_hash"],
    }
    if any(canonical.get(key) != value for key, value in expected_registry.items()):
        raise CA.CampaignAuthorityError("POLICY_REGISTRY_AUTHORITY_MISMATCH")
    if hypothesis_id not in canonical["deciders"] \
            or hypothesis_id not in authority["participant_spec_hashes"]:
        raise CA.CampaignAuthorityError("UNAUTHORIZED_HYPOTHESIS_ID")
    expected_exit = canonical["deciders"][hypothesis_id][1]
    if exit_params != expected_exit:
        raise CA.CampaignAuthorityError("POLICY_EXIT_SPEC_AUTHORITY_MISMATCH")
    supplied_callable = _callable_fingerprint(decide_fn)
    expected_callable = _callable_fingerprint(
        canonical["deciders"][hypothesis_id][0]
    )
    if supplied_callable != expected_callable:
        raise CA.CampaignAuthorityError("POLICY_CALLABLE_AUTHORITY_MISMATCH")
    supplied_behavior = _behavioral_fingerprints(
        {hypothesis_id: (decide_fn, exit_params)}, symbol, entry["venue"],
        timeframe, entry["dataset_source_generation_id"],
    )[hypothesis_id]
    expected_behavior = canonical["fingerprints"][hypothesis_id]
    if supplied_behavior != expected_behavior:
        raise CA.CampaignAuthorityError("POLICY_BEHAVIOR_AUTHORITY_MISMATCH")
    expected_spec = authority["participant_spec_hashes"][hypothesis_id]
    if canonical["specs"][hypothesis_id] != expected_spec:
        raise CA.CampaignAuthorityError("POLICY_SPEC_AUTHORITY_MISMATCH")
    return {
        "participant_spec_hash": expected_spec,
        "behavior_fingerprint": expected_behavior,
        "callable_fingerprint": expected_callable,
        "registry_hash": canonical["registry_hash"],
    }


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
                       sigs_wf, decide_fn, exit_params, *,
                       authorization: CA.TournamentAuthorization,
                       hypothesis_id, verified_reference=None):
    """Evaluate TRAIN and VALIDATION before any WALK_FORWARD computation.

    ``sigs_wf`` may be a lazy zero-argument supplier.  It is called only after
    VALIDATION admission.  The holdout is neither an argument nor an import.
    """
    context = CA.validate_full_authorization(authorization)
    campaign_id = context.campaign_id
    symbol, timeframe = context.symbol, context.timeframe
    authority = CA.load_campaign_authority(campaign_id)
    if hypothesis_id not in authority["participant_spec_hashes"]:
        raise CA.CampaignAuthorityError("UNAUTHORIZED_HYPOTHESIS_ID")
    policy_contract = _authorize_candidate_policy(
        decide_fn=decide_fn, exit_params=exit_params,
        hypothesis_id=hypothesis_id, authorization=context,
        verified_reference=verified_reference,
    )
    original_params = copy.deepcopy(exit_params)
    original_decider = decide_fn
    stage_parameter_integrity: dict[str, bool] = {}

    def drive(stage: str, bars, sigs, fn, *, scenario_cost="observed",
              stage_hypothesis=hypothesis_id):
        params = copy.deepcopy(original_params)
        result = CL.drive_causal(
            bars, sigs, fn, params, symbol=symbol, timeframe=timeframe,
            scenario_cost=scenario_cost, hypothesis_id=stage_hypothesis,
        )
        stage_parameter_integrity[stage] = params == original_params
        return result

    policy_identity = {
        "decider_fingerprint": policy_contract["behavior_fingerprint"],
        "callable_fingerprint": policy_contract["callable_fingerprint"],
        "participant_spec_hash": policy_contract["participant_spec_hash"],
        "registry_hash": policy_contract["registry_hash"],
        "callable_unchanged": True,
        "parameters_before": copy.deepcopy(original_params),
        "hypothesis_id": hypothesis_id,
        "campaign_id": campaign_id,
    }
    sel = drive("selection", bars_tr, sigs_tr, decide_fn)
    m = _safe_metrics(sel["trades"], sel["counters"], timeframe)
    baseline = drive(
        "baseline", bars_tr, sigs_tr,
        _random_baseline_decider(
            symbol=symbol, venue="research_baseline", timeframe=timeframe,
            gen="v10_47_23",
        ),
        stage_hypothesis="PREREGISTERED_RANDOM_BASELINE_V10_47_23",
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
        campaign_id=campaign_id, symbol=symbol, timeframe=timeframe,
    )
    if paired["pairing_status"] == "INVALID":
        gates = {
            "net_positive_selection": m["net_pnl_eur"] > 0,
            "n_eff_sufficient": (
                m["n_eff_final"] >= SHADOW_GATES_V2["min_n_eff"]
            ),
            "top3_robust": m["net_without_top3_eur"] >= 0,
            "baseline_match_complete": False,
            "beats_matched_random_paired": False,
            "conservative_survives": False,
            "validation_positive": False,
            "validation_n_eff_sufficient": False,
            "walk_forward_positive": False,
            "all_pass": False,
        }
        return {
            "selection_metrics": m,
            "matched_random_paired": paired,
            "conservative_net_eur": None,
            "validation_net_eur": None,
            "validation_trades": 0,
            "validation_metrics": None,
            "validation_gate": False,
            "validation_rejection_reason": "BASELINE_PAIRING_INVALID",
            "walk_forward_called": False,
            "walk_forward_metrics": None,
            "walk_forward_net_eur": None,
            "status": "BASELINE_PAIRING_INVALID",
            "next_stage": "NONE",
            "paired_lower_bound_eur": 0.0,
            "baseline_coverage": 0.0,
            "policy_identity": policy_identity,
            "campaign_authority": {
                "status": paired.get("authority_status"),
                "root": context.root_anchor_sha256,
            },
            "gates": gates,
            "is_shadow_candidate": False,
        }
    cons = drive(
        "conservative", bars_tr, sigs_tr, decide_fn,
        scenario_cost="conservative",
    )
    cons_values = [float(trade.get("net_eur", math.nan)) for trade in cons["trades"]]
    cons_net = sum(cons_values) if all(math.isfinite(value) for value in cons_values) \
        else math.nan
    val = drive("validation", bars_validation, sigs_validation, decide_fn)
    val_metrics = _safe_metrics(val["trades"], val["counters"], timeframe)
    val_net = float(val_metrics["net_pnl_eur"])
    validation_reason = None
    identity_unchanged = (
        decide_fn is original_decider
        and all(stage_parameter_integrity.values())
        and exit_params == original_params
    )
    if not _metrics_are_finite(m) or not math.isfinite(cons_net):
        validation_reason = "SELECTION_OR_COST_METRICS_INVALID"
    elif not val["trades"]:
        validation_reason = "NO_VALIDATION_TRADES"
    elif not _metrics_are_finite(val_metrics):
        validation_reason = "VALIDATION_METRICS_INVALID"
    elif val_net <= 0:
        validation_reason = "VALIDATION_NET_NOT_POSITIVE"
    elif val_metrics["n_eff_final"] < SHADOW_GATES_V2["min_n_eff"]:
        validation_reason = "VALIDATION_N_EFF_INSUFFICIENT"
    elif not identity_unchanged:
        validation_reason = "POLICY_IDENTITY_CHANGED"
    elif not isinstance(sigs_wf, Callable):
        validation_reason = "WALK_FORWARD_SUPPLIER_NOT_LAZY"
    base_gates = {
        "net_positive_selection": m["net_pnl_eur"] > 0,
        "n_eff_sufficient": m["n_eff_final"] >= SHADOW_GATES_V2["min_n_eff"],
        "top3_robust": m["net_without_top3_eur"] >= 0,
        "baseline_match_complete": (
            paired["match_status"] == "OK"
            and paired["pairing_status"] == "VALID"
            and paired["integrity_status"] == "PASS"
        ),
        "beats_matched_random_paired": paired["beats_matched_random"],
        "conservative_survives": math.isfinite(cons_net) and cons_net > 0,
        "validation_positive": _metrics_are_finite(val_metrics) and val_net > 0,
        "validation_n_eff_sufficient": (
            val_metrics["n_eff_final"] >= SHADOW_GATES_V2["min_n_eff"]
        ),
        "policy_identity_unchanged": identity_unchanged,
        "campaign_authority_valid": paired.get("authority_status") == (
            "CANONICAL_AUTHORITY_VALID"
        ),
    }
    failed_pre_walk_forward = [
        name for name, passed in base_gates.items() if not passed
    ]
    if validation_reason is None and failed_pre_walk_forward:
        validation_reason = "PRE_WALK_FORWARD_GATES_FAILED:" + ",".join(
            failed_pre_walk_forward
        )
    validation_gate = validation_reason is None
    policy_identity["parameters_after_validation"] = copy.deepcopy(exit_params)
    policy_identity["stage_parameter_integrity"] = copy.deepcopy(
        stage_parameter_integrity
    )
    policy_identity["parameters_unchanged"] = identity_unchanged
    policy_identity["callable_unchanged"] = decide_fn is original_decider
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
            "campaign_authority": {
                "status": paired.get("authority_status"),
                "root": context.root_anchor_sha256,
            },
            "gates": gates,
            "is_shadow_candidate": False,
        }
    wf_signals = sigs_wf()
    wf = drive("walk_forward", bars_wf, wf_signals, decide_fn)
    wf_metrics = _safe_metrics(wf["trades"], wf["counters"], timeframe)
    wf_net = float(wf_metrics["net_pnl_eur"])
    identity_after_wf = (
        decide_fn is original_decider
        and all(stage_parameter_integrity.values())
        and exit_params == original_params
    )
    gates = {
        **base_gates,
        "walk_forward_positive": _metrics_are_finite(wf_metrics) and wf_net > 0,
        "policy_identity_unchanged": identity_after_wf,
    }
    gates["all_pass"] = all(gates.values())
    policy_identity["parameters_after_walk_forward"] = copy.deepcopy(exit_params)
    policy_identity["stage_parameter_integrity"] = copy.deepcopy(
        stage_parameter_integrity
    )
    policy_identity["parameters_unchanged"] = identity_after_wf
    policy_identity["callable_unchanged"] = decide_fn is original_decider
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
            "campaign_authority": {
                "status": paired.get("authority_status"),
                "root": context.root_anchor_sha256,
            },
            "gates": gates, "is_shadow_candidate": gates["all_pass"]}


def run_causal_tournament(discovery_partitions: DiscoveryPartitions, *,
                          symbol: str, venue: str, timeframe: str, gen: str,
                          log=lambda *a: None) -> dict:
    """Run discovery without ever receiving or loading holdout observations.

    VALIDATION admission is the boundary that makes WALK_FORWARD computation
    reachable.  Only commitment metadata is present in this process.
    """
    import time as _t
    if not isinstance(discovery_partitions, DiscoveryPartitions):
        raise TypeError("discovery_partitions must come from DiscoveryDatasetLoader")
    bars_tr, bars_va, bars_wf = discovery_partitions.as_mutable()
    # Close the full family before any real-market signal or metric is read.
    campaign = preregister_campaign()
    dataset_manifest_path = (
        Path(discovery_partitions.source_root).resolve(strict=True).parent
        / "dataset_manifest.json"
    )
    dataset_evidence = verify_discovery_partitions(
        discovery_partitions, dataset_manifest_path
    )
    verified_reference, reference_evidence = load_verified_reference(
        discovery_partitions.source_root, dataset_manifest_path,
    )
    holdout_commitment, holdout_evidence = load_verified_holdout_commitment(
        discovery_partitions.source_root, dataset_manifest_path,
    )
    n_discovery = len(bars_tr) + len(bars_va) + len(bars_wf)
    n_holdout = holdout_commitment["n_bars"]
    n = n_discovery + n_holdout
    tr_end = len(bars_tr)
    val_end = tr_end + len(bars_va)
    wf_end = val_end + len(bars_wf)
    sp = {
        "train": (0, tr_end),
        "validation": (tr_end, val_end),
        "walk_forward": (val_end, wf_end),
        "holdout": tuple(holdout_commitment["index_range"]),
        "selection_end_index": tr_end,
        "holdout_start_index": wf_end,
    }
    if sp["holdout"][0] != wf_end or sp["holdout"][1] != n:
        raise ValueError("holdout commitment does not continue discovery partitions")
    reg = preregister(symbol, venue, timeframe, gen, verified_reference)
    authorization = CA.validate_full_authorization(
        CA.authorize_tournament(
            campaign_id=CA.CAMPAIGN_ID, symbol=symbol, timeframe=timeframe,
            venue=venue, registry=reg,
            dataset_manifest_path=dataset_manifest_path,
            source_generation_id=gen,
            holdout_commitment_sha256=holdout_commitment["commitment_sha256"],
        )
    )

    # PHYSICAL SEAL: holdout bars never enter this process or object graph.
    # Features are computed only from the three discovery partitions.
    t0 = _t.time()
    sigs_tr = ES.precompute_sigs(bars_tr)
    train_validation = bars_tr + bars_va
    sigs_train_validation = ES.precompute_sigs(train_validation)
    sigs_va = sigs_train_validation[len(bars_tr):]
    log(
        f"  [sigs] {n_discovery}/{n} discovery bars "
        f"(holdout {n_holdout} PHYSICALLY ABSENT) in {round(_t.time()-t0, 1)}s "
        f"| m_nominal={reg['m_nominal']} "
        f"m_unique_results={reg['m_unique_results']} "
        f"m_campaign={campaign['m_campaign_effective_for_gate']} "
        f"holdout_state=SEALED"
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
        out = CL.drive_causal(
            bars_tr, sigs_tr, fn, ex, symbol=symbol, timeframe=timeframe,
            hypothesis_id=name,
        )
        m = _metrics(out["trades"], out["counters"], timeframe)
        results[name] = {
            "metrics": m,
            "ledger_integrity": _ledger_integrity(out["ledger"], out["trades"]),
        }
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
                                authorization=authorization,
                                hypothesis_id=name,
                                verified_reference=verified_reference,
                                )
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
            "campaign_registry": campaign,
            "campaign_authority": {
                "campaign_id": authorization.campaign_id,
                "campaign_version": authorization.campaign_version,
                "root_anchor_sha256": authorization.root_anchor_sha256,
                "tournament_spec_hash": authorization.entry[
                    "tournament_spec_hash"
                ],
                "canonical_entry_match": authorization.full_context_verified,
                "m_campaign": authorization.m_campaign,
                "alpha": authorization.alpha,
                "correction_method": authorization.correction_method,
                "research_only": True,
                "final_recommendation": "NO LIVE",
            },
            "discovery_dataset_evidence": dataset_evidence,
            "reference_dataset_evidence": reference_evidence,
            "holdout_commitment_evidence": holdout_evidence,
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
            "holdout_touched": hasattr(discovery_partitions, "holdout"),
            "holdout_access_evidence": {
                "input_type": type(discovery_partitions).__name__,
                "input_fields": list(discovery_partitions.__dataclass_fields__),
                "holdout_field_present": hasattr(discovery_partitions, "holdout"),
                "holdout_bytes_received": False,
                "capability_present": False,
            }}
