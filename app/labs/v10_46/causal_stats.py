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
                          tolerance_spec: dict | None = None) -> dict:
    """One-to-one exact-match comparison against a preregistered baseline ledger."""
    if correction_method != "bonferroni":
        raise ValueError("only preregistered bonferroni correction is supported")
    if int(m_global) < 1:
        raise ValueError("m_global must be preregistered and positive")
    tolerances = json.loads(json.dumps(tolerance_spec or BASELINE_TOLERANCE_SPEC))
    by_opportunity: dict[Any, list[dict]] = {}
    for baseline in baseline_trades:
        by_opportunity.setdefault(baseline.get("opportunity_id"), []).append(baseline)
    consumed: set[str] = set()
    pairs: list[dict] = []
    compatible_pairs: list[dict] = []
    impossible = incompatible = 0
    for index, candidate in enumerate(candidate_trades):
        raw_candidate_id = candidate.get("candidate_trade_id")
        candidate_id = str(raw_candidate_id) if raw_candidate_id else ""
        options = [
            row for row in by_opportunity.get(candidate.get("opportunity_id"), [])
            if str(row.get("baseline_trade_id") or row.get("trade_id")) not in consumed
        ]
        if not options:
            remaining = [
                row for row in baseline_trades
                if str(row.get("baseline_trade_id") or row.get("trade_id")) not in consumed
            ]
            if len(remaining) == 1:
                options = remaining
        if len(options) != 1:
            impossible += 1
            pairs.append({
                "pair_id": _canonical_hash([candidate_id, "UNMATCHED"]),
                "candidate_trade_id": candidate_id,
                "baseline_trade_id": None,
                "match_status": "INCOMPATIBLE",
                "unmatched_reason": "NO_UNIQUE_BASELINE_FOR_OPPORTUNITY",
                "paired_delta_eur": None,
            })
            continue
        baseline = options[0]
        raw_baseline_id = baseline.get("baseline_trade_id")
        baseline_id = str(raw_baseline_id) if raw_baseline_id else ""
        mismatch = None
        if not candidate_id:
            mismatch = "CANDIDATE_TRADE_ID"
        elif not baseline_id:
            mismatch = "BASELINE_TRADE_ID"
        else:
            mismatch = next(
                (
                    field for field in BASELINE_MATCH_FIELDS
                    if field not in candidate
                    or field not in baseline
                    or not _equal(
                        field, candidate[field], baseline[field], tolerances
                    )
                ),
                None,
            )
        pair = {
            "pair_id": _canonical_hash([candidate_id, baseline_id]),
            "candidate_trade_id": candidate_id,
            "baseline_trade_id": baseline_id,
            **{field: copy_value(candidate.get(field)) for field in BASELINE_MATCH_FIELDS},
        }
        consumed.add(baseline_id)
        if mismatch:
            incompatible += 1
            pair.update({
                "candidate_net_eur": float(candidate.get("candidate_net_eur",
                                                          candidate.get("net_eur", 0.0))),
                "baseline_net_eur": float(baseline.get("baseline_net_eur",
                                                        baseline.get("net_eur", 0.0))),
                "paired_delta_eur": None,
                "match_status": "INCOMPATIBLE",
                "unmatched_reason": mismatch.upper(),
            })
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
        pairs.append(pair)
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
    corrected_p = min(1.0, raw_p * int(m_global))
    complete = (
        coverage >= float(tolerances["coverage_threshold"])
        and impossible == 0 and incompatible == 0
    )
    beats = bool(
        complete and bootstrap["mean_lb"] > 0 and corrected_p < alpha
    )
    return {
        "pairs_requested": requested,
        "pairs_found": found,
        "pairs_impossible": impossible,
        "pairs_incompatible": incompatible,
        "coverage": round(coverage, 8),
        "paired_mean_eur": round(float(bootstrap["mean"]), 8),
        "paired_median_eur": round(float(median), 8),
        "paired_lower_bound_eur": round(float(bootstrap["mean_lb"]), 8),
        "paired_deltas_eur": [round(value, 8) for value in deltas],
        "block": block,
        "raw_p_value": round(raw_p, 10),
        "corrected_p_value": round(corrected_p, 10),
        "correction_method": correction_method,
        "m_global": int(m_global),
        "alpha": float(alpha),
        "match_status": "OK" if complete else "BASELINE_MATCH_INCOMPLETE",
        "unmatched_reason": None if complete else "EXACT_MATCH_REQUIRED",
        "beats_matched_random": beats,
        "pairs": pairs,
        "baseline_simulations_per_candidate": 1,
        "tolerance_spec": tolerances,
        "tolerance_spec_hash": _canonical_hash(tolerances),
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
