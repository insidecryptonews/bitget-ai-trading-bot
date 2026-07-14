"""Dependence-aware statistics and exact paired baselines (research only).

The exact baseline contract is intentionally strict.  A candidate trade is
paired with at most one preregistered baseline trade.  No replication average,
field tolerance or post-result match selection is permitted unless it appears in
the versioned tolerance specification.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from . import event_clock as EC
from . import sim_oms as S


def _acf_neff(xs: list[float]) -> float:
    n = len(xs)
    if n <= 1:
        return float(n)
    mean = sum(xs) / n
    var = sum((x - mean) ** 2 for x in xs) / n
    if var <= 0:
        return 1.0
    acc = 0.0
    for lag in range(1, n // 2):
        cov = sum(
            (xs[index] - mean) * (xs[index - lag] - mean)
            for index in range(lag, n)
        ) / n
        rho = cov / var
        if rho <= 0:
            break
        acc += rho
    return max(1.0, min(float(n), n / (1.0 + 2.0 * acc)))


def _non_overlapping_count(intervals: list[tuple[int, int]]) -> int:
    if not intervals:
        return 0
    count, last_end = 0, -math.inf
    for start, end in sorted(intervals, key=lambda item: item[1]):
        if start > last_end:
            count += 1
            last_end = end
    return count


def n_eff_estimate(trades: list[dict], *, timeframe: str) -> dict:
    n = len(trades)
    if n == 0:
        return {
            "n_raw": 0, "n_executed": 0, "n_overlap": 0, "n_event": 0,
            "n_cluster": 0, "n_session": 0, "n_day": 0, "n_temporal": 0,
            "n_acf": 0.0, "n_eff_final": 0.0,
            "cluster_source": "cluster_id_tf", "fallback_used": False,
            "degenerate_returns": False,
        }
    clusters = {trade["cluster"] for trade in trades}
    sessions = {trade.get("session") for trade in trades}
    days = {trade.get("day") for trade in trades}
    events = {trade["opportunity_bar"] for trade in trades}
    intervals = [(trade["entry_bar"], trade["exit_index"]) for trade in trades]
    n_overlap = _non_overlapping_count(intervals)
    returns = [float(trade["net_eur"]) for trade in trades]
    n_acf = _acf_neff(returns)
    block_ms = EC.cluster_block_ms(timeframe)
    span = max(trade["entry_ts"] for trade in trades) - min(
        trade["entry_ts"] for trade in trades
    )
    n_temporal = max(1, min(n, int(span // block_ms) + 1))
    applicable = [
        len(events), n_overlap, len(clusters), len(sessions), len(days),
        n_temporal, n_acf,
    ]
    n_eff_final = max(1.0, float(min(applicable)))
    return {
        "n_raw": n, "n_executed": n, "n_overlap": n_overlap,
        "n_event": len(events), "n_cluster": len(clusters),
        "n_session": len(sessions), "n_day": len(days),
        "n_temporal": n_temporal, "n_acf": round(n_acf, 4),
        "n_eff_final": round(n_eff_final, 4),
        "cluster_source": "cluster_id_tf", "fallback_used": False,
        "degenerate_returns": len(set(returns)) <= 1,
    }


def block_bootstrap_mean_lb(xs: list[float], *, block: int = 5,
                            reps: int = 2000, alpha: float = 0.05,
                            seed: int = 12345) -> dict:
    n = len(xs)
    if n == 0:
        return {"mean": 0.0, "mean_lb": 0.0, "total_lb": 0.0,
                "reps": 0, "n": 0}
    block = max(1, min(block, n))
    rng = random.Random(seed)
    block_count = math.ceil(n / block)
    means: list[float] = []
    for _ in range(reps):
        sample: list[float] = []
        for _block in range(block_count):
            start = rng.randint(0, n - block)
            sample.extend(xs[start:start + block])
        sample = sample[:n]
        means.append(sum(sample) / len(sample))
    means.sort()
    lower = means[max(0, int(alpha * reps) - 1)]
    mean = sum(xs) / n
    return {
        "mean": round(mean, 8), "mean_lb": round(lower, 8),
        "total_lb": round(lower * n, 8), "reps": reps, "n": n,
    }


def matched_random_null(bars: list[dict], trades: list[dict], *, symbol: str,
                        timeframe: str, exit_params: dict,
                        scenario_money: str = "5eur",
                        scenario_cost: str = "observed", reps: int = 200,
                        seed: int = 20240714) -> dict:
    """Legacy aggregate null retained for diagnostics, never for promotion."""
    if int(reps) < 1:
        raise ValueError("reps must be positive")
    interval_ms = EC.interval_ms_for(timeframe)
    time_exit = int(exit_params.get("time_exit", 20))
    stop_frac = float(exit_params.get("stop_frac", 0.008))
    tp_frac = float(exit_params.get("tp_frac", 0.012))
    trailing_frac = exit_params.get("trailing_frac")
    if not trades:
        return {
            "reps": 0, "strategy_net": 0.0, "null_mean": 0.0,
            "null_p95": 0.0, "p_value": 1.0,
            "beats_matched_random": False, "promotion_eligible": False,
        }
    cluster_bars: dict[str, list[int]] = {}
    for index in range(len(bars) - 2):
        cluster = EC.cluster_id_tf(symbol, int(bars[index]["ts"]), timeframe)
        cluster_bars.setdefault(cluster, []).append(index)
    sides = [trade["side"] for trade in trades]
    strategy_net = float(sum(trade["net_eur"] for trade in trades))

    def simulate(index: int, side: str) -> float:
        entry_bar = bars[index + 1]
        result = S.simulate_trade(
            side=side, entry_bar=entry_bar,
            exit_bars=bars[index + 2:index + 2 + time_exit],
            entry_ts_ms=int(entry_bar["ts"]), stop_frac=stop_frac,
            tp_frac=tp_frac, time_exit=time_exit,
            scenario_money=scenario_money, scenario_cost=scenario_cost,
            trailing_frac=trailing_frac, interval_ms=interval_ms,
        )
        return result["net_pnl_eur"] if result["status"] == "OK" else 0.0

    rng = random.Random(seed)
    totals: list[float] = []
    for _ in range(reps):
        permuted = sides[:]
        rng.shuffle(permuted)
        total = 0.0
        for trade, side in zip(trades, permuted):
            choices = cluster_bars.get(trade["cluster"]) or [trade["opportunity_bar"]]
            total += simulate(choices[rng.randrange(len(choices))], side)
        totals.append(total)
    totals.sort()
    greater_equal = sum(total >= strategy_net for total in totals)
    p_value = (greater_equal + 1) / (len(totals) + 1)
    return {
        "reps": reps, "strategy_net": round(strategy_net, 6),
        "null_mean": round(sum(totals) / len(totals), 6),
        "null_p95": round(totals[min(len(totals) - 1, int(0.95 * len(totals)))], 6),
        "null_min": round(totals[0], 6), "null_max": round(totals[-1], 6),
        "p_value": round(p_value, 8),
        "beats_matched_random": False,
        "promotion_eligible": False,
        "diagnostic_only": True,
    }


BASELINE_MATCH_FIELDS = (
    "symbol", "timeframe", "side", "date", "session", "opportunity_id",
    "cluster_id", "regime_id", "entry_timestamp", "entry_availability",
    "max_holding_bars", "realised_holding_bars", "censoring_type",
    "end_of_dataset_censored", "notional_eur", "exposure_eur",
    "leverage_simulated", "fee_model_id", "spread_model_id",
    "slippage_model_id", "funding_settlements_crossed", "funding_cost_eur",
)

BASELINE_TOLERANCE_SPEC = {
    "schema": "v10_47_21_exact_pair_tolerances",
    "numeric_absolute_tolerance": {
        "notional_eur": 1e-9,
        "exposure_eur": 1e-9,
        "leverage_simulated": 1e-12,
        "funding_cost_eur": 1e-9,
    },
    "all_other_fields": "EXACT",
    "coverage_threshold": 1.0,
}


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _id_problem(value: Any, missing_reason: str, *, require_hash: bool = False) -> str | None:
    if value is None or value == "":
        return missing_reason
    if type(value) is not str:
        return "INVALID_ID_TYPE"
    if value != value.strip() or not value.isprintable():
        return "INVALID_ID_FORMAT"
    if require_hash and (
            len(value) != 64
            or any(char not in "0123456789abcdef" for char in value.lower())):
        return "INVALID_ID_FORMAT"
    return None


def deterministic_pair_id(*, candidate_trade_id: str, baseline_trade_id: str,
                          symbol: str, timeframe: str,
                          matching_spec_hash: str, baseline_spec_hash: str,
                          registry_hash: str) -> str:
    """Hash the complete preregistered identity of an exact pair."""
    payload = {
        "schema": "v10_47_23_deterministic_pair_id",
        "candidate_trade_id": candidate_trade_id,
        "baseline_trade_id": baseline_trade_id,
        "symbol": symbol,
        "timeframe": timeframe,
        "matching_spec_hash": matching_spec_hash,
        "baseline_spec_hash": baseline_spec_hash,
        "registry_hash": registry_hash,
    }
    return _canonical_hash(payload)


@dataclass
class PairingRegistry:
    """Fail-closed identity registry for a single paired evaluation."""

    seen_candidate_ids: set[str] = field(default_factory=set)
    seen_baseline_ids: set[str] = field(default_factory=set)
    seen_pair_ids: set[str] = field(default_factory=set)
    accepted_pairs: list[dict] = field(default_factory=list)
    rejected_pairs: list[dict] = field(default_factory=list)
    rejection_reasons: Counter = field(default_factory=Counter)
    duplicate_pair_ids: int = 0
    last_rejection_reason: str | None = None

    def register_identity(self, *, candidate_id: str, baseline_id: str,
                          pair_id: Any) -> bool:
        problem = _id_problem(pair_id, "MISSING_PAIR_ID", require_hash=True)
        if problem:
            self.last_rejection_reason = problem
            self.rejection_reasons[problem] += 1
            return False
        if candidate_id in self.seen_candidate_ids:
            self.last_rejection_reason = "DUPLICATE_CANDIDATE_TRADE_ID"
            self.rejection_reasons["DUPLICATE_CANDIDATE_TRADE_ID"] += 1
            return False
        if baseline_id in self.seen_baseline_ids:
            self.last_rejection_reason = "DUPLICATE_BASELINE_TRADE_ID"
            self.rejection_reasons["DUPLICATE_BASELINE_TRADE_ID"] += 1
            return False
        if pair_id in self.seen_pair_ids:
            self.duplicate_pair_ids += 1
            self.last_rejection_reason = "DUPLICATE_PAIR_ID"
            self.rejection_reasons["DUPLICATE_PAIR_ID"] += 1
            return False
        self.seen_candidate_ids.add(candidate_id)
        self.seen_baseline_ids.add(baseline_id)
        self.seen_pair_ids.add(pair_id)
        self.last_rejection_reason = None
        return True


def _identity_inventory(rows: list[dict], key: str, missing_reason: str,
                        duplicate_reason: str) -> dict:
    valid_ids: list[str] = []
    reasons: Counter = Counter()
    for row in rows:
        value = row.get(key)
        problem = _id_problem(value, missing_reason)
        if problem:
            reasons[problem] += 1
        else:
            valid_ids.append(value)
    counts = Counter(valid_ids)
    duplicate_count = sum(count - 1 for count in counts.values() if count > 1)
    if duplicate_count:
        reasons[duplicate_reason] += duplicate_count
    return {
        "rows_received": len(rows),
        "unique_ids": len(counts),
        "duplicate_ids": duplicate_count,
        "reasons": reasons,
    }


def _campaign_registry_problems(*, campaign_registry: dict | None,
                                campaign_registry_sha: str | None,
                                m_tournament: int, m_campaign: int | None,
                                correction_method: str, alpha: float) -> Counter:
    reasons: Counter = Counter()
    if not isinstance(campaign_registry, dict):
        reasons["MISSING_CAMPAIGN_REGISTRY"] += 1
        return reasons
    if not campaign_registry_sha:
        reasons["MISSING_CAMPAIGN_REGISTRY_SHA"] += 1
    elif _canonical_hash(campaign_registry) != campaign_registry_sha:
        reasons["CAMPAIGN_REGISTRY_SHA_MISMATCH"] += 1
    multiplicities = {
        "effective": campaign_registry.get("m_campaign_effective_for_gate"),
        "nominal": campaign_registry.get("m_campaign_nominal"),
        "unique_hypotheses": campaign_registry.get("m_campaign_unique_hypotheses"),
        "unique_results": campaign_registry.get("m_campaign_unique_results"),
        "supplied": m_campaign,
    }
    if any(type(value) is not int for value in multiplicities.values()):
        reasons["INVALID_CAMPAIGN_MULTIPLICITY"] += 1
        effective = nominal = supplied = None
        unique_hypotheses = unique_results = None
    else:
        effective = multiplicities["effective"]
        nominal = multiplicities["nominal"]
        unique_hypotheses = multiplicities["unique_hypotheses"]
        unique_results = multiplicities["unique_results"]
        supplied = multiplicities["supplied"]
    if supplied is None or effective is None or nominal is None:
        reasons["INVALID_CAMPAIGN_MULTIPLICITY"] += 1
    else:
        if (
                supplied != effective
                or supplied < int(m_tournament)
                or nominal < int(m_tournament)
                or effective > nominal
                or unique_hypotheses is None
                or unique_results is None
                or not (1 <= unique_hypotheses <= nominal)
                or not (1 <= unique_results <= nominal)):
            reasons["INVALID_CAMPAIGN_MULTIPLICITY"] += 1
        dedup_status = campaign_registry.get("deduplication_status")
        if dedup_status == "AMBIGUOUS_USE_NOMINAL" and effective != nominal:
            reasons["AMBIGUOUS_CAMPAIGN_DEDUP"] += 1
        if effective < nominal:
            if dedup_status != "SEMANTIC_EQUIVALENCE_PROVEN":
                reasons["CAMPAIGN_DEDUP_NOT_PROVEN"] += 1
            proof = campaign_registry.get("semantic_equivalence_proof")
            proof_sha = campaign_registry.get("semantic_equivalence_proof_sha")
            if not isinstance(proof, dict) or _canonical_hash(proof) != proof_sha:
                reasons["CAMPAIGN_DEDUP_PROOF_INVALID"] += 1
            else:
                groups = proof.get("groups")
                members_seen: set[str] = set()
                reduction = 0
                proof_invalid = not isinstance(groups, list) or not groups
                for group in groups if isinstance(groups, list) else []:
                    members = group.get("members") if isinstance(group, dict) else None
                    fingerprint = (
                        group.get("semantic_fingerprint")
                        if isinstance(group, dict) else None
                    )
                    if (
                            not isinstance(members, list)
                            or len(members) < 2
                            or len(set(members)) != len(members)
                            or any(_id_problem(member, "MISSING_HYPOTHESIS_ID")
                                   for member in members)
                            or _id_problem(fingerprint, "MISSING_SEMANTIC_FINGERPRINT",
                                           require_hash=True)):
                        proof_invalid = True
                        continue
                    if members_seen.intersection(members):
                        proof_invalid = True
                    members_seen.update(members)
                    reduction += len(members) - 1
                if proof_invalid or reduction != nominal - effective:
                    reasons["CAMPAIGN_DEDUP_PROOF_INVALID"] += 1
    if campaign_registry.get("correction_method") != correction_method:
        reasons["CAMPAIGN_CORRECTION_MISMATCH"] += 1
    try:
        registered_alpha = float(campaign_registry.get("alpha", -1.0))
    except (TypeError, ValueError):
        registered_alpha = -1.0
    if registered_alpha != float(alpha):
        reasons["CAMPAIGN_ALPHA_MISMATCH"] += 1
    if campaign_registry.get("closed") is not True \
            or campaign_registry.get("closed_before_metrics") is not True:
        reasons["CAMPAIGN_REGISTRY_NOT_PREREGISTERED"] += 1
    return reasons


def _equal(field: str, left: Any, right: Any, tolerances: dict) -> bool:
    numeric = tolerances["numeric_absolute_tolerance"]
    if field in numeric:
        try:
            return math.isclose(float(left), float(right), rel_tol=0.0,
                                abs_tol=float(numeric[field]))
        except (TypeError, ValueError):
            return False
    return left == right


def _one_sided_sign_p_value(deltas: list[float]) -> float:
    nonzero = [value for value in deltas if value != 0]
    n = len(nonzero)
    if n == 0:
        return 1.0
    positive = sum(value > 0 for value in nonzero)
    return min(1.0, sum(math.comb(n, k) for k in range(positive, n + 1)) / (2 ** n))


def _bootstrap_block_from_pairs(pairs: list[dict], timeframe: str) -> int:
    if len(pairs) <= 1:
        return 1
    holds = sorted(int(pair["realised_holding_bars"]) for pair in pairs)
    median = holds[len(holds) // 2]
    return max(1, min(median, len(pairs) // 2))


def matched_random_paired(*, candidate_trades: list[dict],
                          baseline_trades: list[dict], timeframe: str,
                          m_global: int, alpha: float = 0.05,
                          correction_method: str = "bonferroni",
                          tolerance_spec: dict | None = None,
                          m_campaign: int | None = None,
                          campaign_registry: dict | None = None,
                          campaign_registry_sha: str | None = None,
                          baseline_spec_hash: str | None = None,
                          registry_hash: str | None = None) -> dict:
    """Bijective exact-match comparison with campaign-wide FWER correction."""
    if correction_method != "bonferroni":
        raise ValueError("only preregistered bonferroni correction is supported")
    if int(m_global) < 1:
        raise ValueError("m_global must be preregistered and positive")
    tolerances = json.loads(json.dumps(tolerance_spec or BASELINE_TOLERANCE_SPEC))
    matching_spec_hash = _canonical_hash(tolerances)
    candidate_inventory = _identity_inventory(
        candidate_trades, "candidate_trade_id", "MISSING_CANDIDATE_TRADE_ID",
        "DUPLICATE_CANDIDATE_TRADE_ID",
    )
    baseline_inventory = _identity_inventory(
        baseline_trades, "baseline_trade_id", "MISSING_BASELINE_TRADE_ID",
        "DUPLICATE_BASELINE_TRADE_ID",
    )
    preflight_reasons: Counter = Counter(candidate_inventory["reasons"])
    preflight_reasons.update(baseline_inventory["reasons"])
    preflight_reasons.update(_campaign_registry_problems(
        campaign_registry=campaign_registry,
        campaign_registry_sha=campaign_registry_sha,
        m_tournament=int(m_global), m_campaign=m_campaign,
        correction_method=correction_method, alpha=alpha,
    ))
    baseline_hash_problem = _id_problem(
        baseline_spec_hash, "MISSING_BASELINE_SPEC_HASH", require_hash=True
    )
    if baseline_hash_problem:
        preflight_reasons[baseline_hash_problem] += 1
    registry_hash_problem = _id_problem(
        registry_hash, "MISSING_TOURNAMENT_REGISTRY_HASH", require_hash=True
    )
    if registry_hash_problem:
        preflight_reasons[registry_hash_problem] += 1

    def integrity_fields(*, registry: PairingRegistry | None = None,
                         pair_rows_received: int = 0) -> dict:
        pair_registry = registry or PairingRegistry()
        return {
            "candidate_rows_received": candidate_inventory["rows_received"],
            "unique_candidate_ids": candidate_inventory["unique_ids"],
            "duplicate_candidate_ids": candidate_inventory["duplicate_ids"],
            "baseline_rows_received": baseline_inventory["rows_received"],
            "unique_baseline_ids": baseline_inventory["unique_ids"],
            "duplicate_baseline_ids": baseline_inventory["duplicate_ids"],
            "pair_rows_received": pair_rows_received,
            "unique_pair_ids": len({
                pair["pair_id"] for pair in pair_registry.accepted_pairs
            }),
            "duplicate_pair_ids": pair_registry.duplicate_pair_ids,
        }

    def invalid_result(reasons: Counter, *, pairs: list[dict] | None = None,
                       registry: PairingRegistry | None = None,
                       pair_rows_received: int = 0) -> dict:
        pair_rows = []
        for source in pairs or []:
            row = copy_value(source)
            if row.get("match_status") == "OK":
                row["match_status"] = "INVALID"
                row["unmatched_reason"] = "GLOBAL_PAIRING_INTEGRITY_FAILURE"
                row["paired_delta_eur"] = None
            pair_rows.append(row)
        metrics = integrity_fields(
            registry=registry, pair_rows_received=pair_rows_received
        )
        # A globally invalid evaluation accepts no statistical pair, even when
        # an earlier row looked compatible before a later integrity failure.
        metrics["unique_pair_ids"] = 0
        requested = len(candidate_trades)
        return {
            **metrics,
            "pairs_requested": requested,
            "pairs_found": 0,
            "pairs_accepted": 0,
            "pairs_rejected": requested,
            "pairs_impossible": 0,
            "pairs_incompatible": 0,
            "unmatched_candidates": requested,
            "unmatched_baselines": len(baseline_trades),
            "coverage": 0.0,
            "paired_mean_eur": 0.0,
            "paired_median_eur": 0.0,
            "paired_lower_bound_eur": 0.0,
            "paired_deltas_eur": [],
            "block": 1,
            "raw_p_value": None,
            "p_raw": None,
            "p_tournament_corrected": None,
            "p_campaign_corrected": None,
            "corrected_p_value": None,
            "correction_method": correction_method,
            "method": correction_method,
            "m_global": int(m_global),
            "m_tournament": int(m_global),
            "m_campaign": int(m_campaign) if m_campaign is not None else None,
            "m_campaign_nominal": (
                campaign_registry.get("m_campaign_nominal")
                if isinstance(campaign_registry, dict) else None
            ),
            "m_campaign_unique_hypotheses": (
                campaign_registry.get("m_campaign_unique_hypotheses")
                if isinstance(campaign_registry, dict) else None
            ),
            "m_campaign_unique_results": (
                campaign_registry.get("m_campaign_unique_results")
                if isinstance(campaign_registry, dict) else None
            ),
            "campaign_registry_sha": campaign_registry_sha,
            "alpha": float(alpha),
            "match_status": "BASELINE_PAIRING_INVALID",
            "pairing_status": "INVALID",
            "integrity_status": "INVALID",
            "status": "BASELINE_PAIRING_INVALID",
            "unmatched_reason": "PAIRING_INTEGRITY_REQUIRED",
            "rejection_reasons": dict(sorted(reasons.items())),
            "baseline_gate": False,
            "beats_matched_random": False,
            "promotion_allowed": False,
            "pairs": pair_rows,
            "baseline_simulations_per_candidate": 1,
            "tolerance_spec": tolerances,
            "tolerance_spec_hash": matching_spec_hash,
            "matching_spec_hash": matching_spec_hash,
            "baseline_spec_hash": baseline_spec_hash,
            "registry_hash": registry_hash,
            "research_only": True,
            "final_recommendation": "NO LIVE",
        }

    if preflight_reasons:
        return invalid_result(preflight_reasons)

    by_opportunity: dict[Any, list[dict]] = {}
    for baseline in baseline_trades:
        by_opportunity.setdefault(baseline.get("opportunity_id"), []).append(baseline)
    consumed_baselines: set[str] = set()
    pairing_registry = PairingRegistry()
    pairs: list[dict] = []
    compatible_pairs: list[dict] = []
    impossible = incompatible = 0
    pair_rows_received = 0
    rejection_reasons: Counter = Counter()
    for candidate in candidate_trades:
        candidate_id = candidate["candidate_trade_id"]
        options = [
            row for row in by_opportunity.get(candidate.get("opportunity_id"), [])
            if row["baseline_trade_id"] not in consumed_baselines
        ]
        if not options:
            remaining = [
                row for row in baseline_trades
                if row["baseline_trade_id"] not in consumed_baselines
            ]
            if len(remaining) == 1:
                options = remaining
        if len(options) != 1:
            impossible += 1
            rejection_reasons["NO_UNIQUE_BASELINE_FOR_OPPORTUNITY"] += 1
            pairs.append({
                "pair_id": None,
                "candidate_trade_id": candidate_id,
                "baseline_trade_id": None,
                "match_status": "INCOMPATIBLE",
                "unmatched_reason": "NO_UNIQUE_BASELINE_FOR_OPPORTUNITY",
                "paired_delta_eur": None,
            })
            continue
        baseline = options[0]
        baseline_id = baseline["baseline_trade_id"]
        pair_rows_received += 1
        pair_id = deterministic_pair_id(
            candidate_trade_id=candidate_id,
            baseline_trade_id=baseline_id,
            symbol=candidate.get("symbol"),
            timeframe=candidate.get("timeframe"),
            matching_spec_hash=matching_spec_hash,
            baseline_spec_hash=baseline_spec_hash,
            registry_hash=registry_hash,
        )
        pair = {
            "pair_id": pair_id,
            "candidate_trade_id": candidate_id,
            "baseline_trade_id": baseline_id,
            **{field: copy_value(candidate.get(field)) for field in BASELINE_MATCH_FIELDS},
        }
        identity_ok = pairing_registry.register_identity(
            candidate_id=candidate_id, baseline_id=baseline_id, pair_id=pair_id
        )
        if not identity_ok:
            reason = pairing_registry.last_rejection_reason
            pair.update({
                "candidate_net_eur": None,
                "baseline_net_eur": None,
                "paired_delta_eur": None,
                "match_status": "INVALID",
                "unmatched_reason": reason,
            })
            pairing_registry.rejected_pairs.append(copy_value(pair))
            pairs.append(pair)
            continue
        consumed_baselines.add(baseline_id)
        mismatch = next(
            (
                field for field in BASELINE_MATCH_FIELDS
                if field not in candidate
                or field not in baseline
                or not _equal(field, candidate[field], baseline[field], tolerances)
            ),
            None,
        )
        if mismatch:
            incompatible += 1
            rejection_reasons["PAIR_FIELD_INCOMPATIBLE"] += 1
            pair.update({
                "candidate_net_eur": float(candidate.get("candidate_net_eur",
                                                          candidate.get("net_eur", 0.0))),
                "baseline_net_eur": float(baseline.get("baseline_net_eur",
                                                        baseline.get("net_eur", 0.0))),
                "paired_delta_eur": None,
                "match_status": "INCOMPATIBLE",
                "unmatched_reason": f"PAIR_FIELD_INCOMPATIBLE:{mismatch.upper()}",
            })
            pairing_registry.rejected_pairs.append(copy_value(pair))
        else:
            candidate_net = float(candidate.get("candidate_net_eur",
                                                candidate.get("net_eur", 0.0)))
            baseline_net = float(baseline.get("baseline_net_eur",
                                              baseline.get("net_eur", 0.0)))
            pair.update({
                "candidate_net_eur": candidate_net,
                "baseline_net_eur": baseline_net,
                "paired_delta_eur": candidate_net - baseline_net,
                "match_status": "OK",
                "unmatched_reason": None,
            })
            compatible_pairs.append(pair)
            pairing_registry.accepted_pairs.append(copy_value(pair))
        pairs.append(pair)

    if pairing_registry.rejection_reasons:
        invalid_reasons = Counter(pairing_registry.rejection_reasons)
        invalid_reasons.update(rejection_reasons)
        return invalid_result(
            invalid_reasons, pairs=pairs, registry=pairing_registry,
            pair_rows_received=pair_rows_received,
        )

    requested = len(candidate_trades)
    found = len(compatible_pairs)
    coverage = found / requested if requested else 1.0
    deltas = [float(pair["paired_delta_eur"]) for pair in compatible_pairs]
    block = _bootstrap_block_from_pairs(compatible_pairs, timeframe)
    bootstrap = block_bootstrap_mean_lb(deltas, block=block) if deltas else {
        "mean": 0.0, "mean_lb": 0.0, "total_lb": 0.0, "reps": 0, "n": 0,
    }
    ordered = sorted(deltas)
    median = ordered[len(ordered) // 2] if ordered else 0.0
    raw_p = _one_sided_sign_p_value(deltas)
    tournament_corrected = min(1.0, raw_p * int(m_global))
    campaign_corrected = min(1.0, raw_p * int(m_campaign))
    complete = (
        coverage >= float(tolerances["coverage_threshold"])
        and impossible == 0 and incompatible == 0
    )
    beats = bool(
        complete and bootstrap["mean_lb"] > 0 and campaign_corrected < alpha
    )
    rejection_reasons.update(pairing_registry.rejection_reasons)
    metrics = integrity_fields(
        registry=pairing_registry, pair_rows_received=pair_rows_received
    )
    integrity_ok = (
        found <= metrics["unique_candidate_ids"]
        and found <= metrics["unique_baseline_ids"]
        and found == metrics["unique_pair_ids"]
    )
    if not integrity_ok:
        rejection_reasons["PAIRING_CARDINALITY_INVARIANT_FAILED"] += 1
        return invalid_result(
            rejection_reasons, pairs=pairs, registry=pairing_registry,
            pair_rows_received=pair_rows_received,
        )
    return {
        **metrics,
        "pairs_requested": requested,
        "pairs_found": found,
        "pairs_accepted": found,
        "pairs_rejected": requested - found,
        "pairs_impossible": impossible,
        "pairs_incompatible": incompatible,
        "unmatched_candidates": requested - found,
        "unmatched_baselines": len(baseline_trades) - found,
        "coverage": round(coverage, 8),
        "paired_mean_eur": round(float(bootstrap["mean"]), 8),
        "paired_median_eur": round(float(median), 8),
        "paired_lower_bound_eur": round(float(bootstrap["mean_lb"]), 8),
        "paired_deltas_eur": [round(value, 8) for value in deltas],
        "block": block,
        "raw_p_value": round(raw_p, 10),
        "p_raw": round(raw_p, 10),
        "p_tournament_corrected": round(tournament_corrected, 10),
        "p_campaign_corrected": round(campaign_corrected, 10),
        "corrected_p_value": round(campaign_corrected, 10),
        "correction_method": correction_method,
        "method": correction_method,
        "m_global": int(m_global),
        "m_tournament": int(m_global),
        "m_campaign": int(m_campaign),
        "m_campaign_nominal": campaign_registry["m_campaign_nominal"],
        "m_campaign_unique_hypotheses": (
            campaign_registry["m_campaign_unique_hypotheses"]
        ),
        "m_campaign_unique_results": campaign_registry["m_campaign_unique_results"],
        "campaign_registry_sha": campaign_registry_sha,
        "alpha": float(alpha),
        "match_status": "OK" if complete else "BASELINE_MATCH_INCOMPLETE",
        "pairing_status": "VALID",
        "integrity_status": "PASS",
        "status": "BASELINE_PAIRING_VALID",
        "unmatched_reason": None if complete else "EXACT_MATCH_REQUIRED",
        "rejection_reasons": dict(sorted(rejection_reasons.items())),
        "baseline_gate": beats,
        "beats_matched_random": beats,
        "promotion_allowed": beats,
        "pairs": pairs,
        "baseline_simulations_per_candidate": 1,
        "tolerance_spec": tolerances,
        "tolerance_spec_hash": matching_spec_hash,
        "matching_spec_hash": matching_spec_hash,
        "baseline_spec_hash": baseline_spec_hash,
        "registry_hash": registry_hash,
        "research_only": True,
        "final_recommendation": "NO LIVE",
    }


def copy_value(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def paired_delta_vs_zero(trades: list[dict]) -> dict:
    values = [float(trade["net_eur"]) for trade in trades]
    bootstrap = block_bootstrap_mean_lb(values)
    return {
        "n": len(values), "mean_diff_eur": bootstrap["mean"],
        "mean_lb_eur": bootstrap["mean_lb"],
        "total_lb_eur": bootstrap["total_lb"],
        "beats_no_trade": bool(sum(values) > 0 and bootstrap["mean_lb"] > 0),
    }
