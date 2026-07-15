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
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from . import event_clock as EC
from . import sim_oms as S
from . import campaign_authority as CA


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
            "n_acf": 0.0, "n_dependency": 0, "n_underlying": 0,
            "n_eff_final": 0.0,
            "cluster_source": "cluster_id_tf", "fallback_used": False,
            "degenerate_returns": False,
        }
    required = (
        "entry_ts", "cluster", "session", "day", "opportunity_bar",
        "entry_bar", "exit_index", "net_eur",
    )
    invalid_reason = None
    for trade in trades:
        if not isinstance(trade, dict) or any(key not in trade for key in required):
            invalid_reason = "MISSING_REQUIRED_N_EFF_FIELD"
            break
        if any(
                type(trade[key]) is not str or not trade[key]
                for key in ("cluster", "session", "day")):
            invalid_reason = "INVALID_N_EFF_GROUP_ID"
            break
        if any(
                type(trade[key]) is not int
                for key in ("entry_ts", "opportunity_bar", "entry_bar", "exit_index")):
            invalid_reason = "INVALID_N_EFF_INDEX"
            break
        if trade["entry_ts"] < 0 or trade["entry_bar"] > trade["exit_index"]:
            invalid_reason = "INVALID_N_EFF_INTERVAL"
            break
        try:
            outcome = float(trade["net_eur"])
        except (TypeError, ValueError):
            invalid_reason = "INVALID_N_EFF_RETURN"
            break
        if not math.isfinite(outcome):
            invalid_reason = "NONFINITE_RETURN"
            break
    if invalid_reason:
        return {
            "n_raw": n, "n_executed": n, "n_overlap": 0,
            "n_event": 0, "n_cluster": 0, "n_session": 0, "n_day": 0,
            "n_temporal": 0, "n_acf": 0.0, "n_dependency": 0,
            "n_underlying": 0, "n_eff_final": 0.0,
            "cluster_source": "dependency_cluster_id+underlying_trade_id",
            "fallback_used": False, "dependency_ids_complete": False,
            "underlying_ids_complete": False, "degenerate_returns": False,
            "input_valid": False, "invalid_reason": invalid_reason,
        }
    ordered = sorted(
        trades,
        key=lambda trade: (
            int(trade["entry_ts"]), str(trade.get("underlying_trade_id", "")),
        ),
    )
    clusters = {trade["cluster"] for trade in ordered}
    sessions = {trade.get("session") for trade in ordered}
    days = {trade.get("day") for trade in ordered}
    events = {trade["opportunity_bar"] for trade in ordered}
    intervals = [(trade["entry_bar"], trade["exit_index"]) for trade in ordered]
    n_overlap = _non_overlapping_count(intervals)
    returns = [float(trade["net_eur"]) for trade in ordered]
    if any(not math.isfinite(value) for value in returns):
        return {
            "n_raw": n, "n_executed": n, "n_overlap": n_overlap,
            "n_event": len(events), "n_cluster": len(clusters),
            "n_session": len(sessions), "n_day": len(days),
            "n_temporal": 0, "n_acf": 0.0, "n_dependency": 0,
            "n_underlying": 0, "n_eff_final": 0.0,
            "cluster_source": "dependency_cluster_id+underlying_trade_id",
            "fallback_used": False, "dependency_ids_complete": False,
            "underlying_ids_complete": False, "degenerate_returns": False,
            "invalid_reason": "NONFINITE_RETURN",
        }
    n_acf = _acf_neff(returns)
    try:
        block_ms = EC.cluster_block_ms(timeframe)
    except ValueError:
        return {
            "n_raw": n, "n_executed": n, "n_overlap": n_overlap,
            "n_event": len(events), "n_cluster": len(clusters),
            "n_session": len(sessions), "n_day": len(days),
            "n_temporal": 0, "n_acf": round(n_acf, 4), "n_dependency": 0,
            "n_underlying": 0, "n_eff_final": 0.0,
            "cluster_source": "dependency_cluster_id+underlying_trade_id",
            "fallback_used": False, "dependency_ids_complete": False,
            "underlying_ids_complete": False,
            "degenerate_returns": len(set(returns)) <= 1,
            "input_valid": False, "invalid_reason": "UNKNOWN_TIMEFRAME",
        }
    span = max(trade["entry_ts"] for trade in ordered) - min(
        trade["entry_ts"] for trade in ordered
    )
    n_temporal = max(1, min(n, int(span // block_ms) + 1))
    dependency_values = [trade.get("dependency_cluster_id") for trade in ordered]
    underlying_values = [trade.get("underlying_trade_id") for trade in ordered]
    dependency_complete = all(
        type(value) is str and value for value in dependency_values
    )
    underlying_complete = all(
        type(value) is str and value for value in underlying_values
    )
    n_dependency = len(set(dependency_values)) if dependency_complete else 0
    n_underlying = len(set(underlying_values)) if underlying_complete else 0
    applicable = [
        len(events), n_overlap, len(clusters), len(sessions), len(days),
        n_temporal, n_acf, n_dependency, n_underlying,
    ]
    n_eff_final = max(1.0, float(min(applicable))) \
        if dependency_complete and underlying_complete else 0.0
    return {
        "n_raw": n, "n_executed": n, "n_overlap": n_overlap,
        "n_event": len(events), "n_cluster": len(clusters),
        "n_session": len(sessions), "n_day": len(days),
        "n_temporal": n_temporal, "n_acf": round(n_acf, 4),
        "n_dependency": n_dependency, "n_underlying": n_underlying,
        "n_eff_final": round(n_eff_final, 4),
        "cluster_source": "dependency_cluster_id+underlying_trade_id",
        "fallback_used": False,
        "dependency_ids_complete": dependency_complete,
        "underlying_ids_complete": underlying_complete,
        "degenerate_returns": len(set(returns)) <= 1,
        "input_valid": True,
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
    "global_event_id", "cluster_id", "dependency_cluster_id", "regime_id",
    "entry_timestamp", "entry_availability",
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


_CANONICAL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._/-]{0,255}$")


def _id_problem(value: Any, missing_reason: str, *, require_hash: bool = False) -> str | None:
    if value is None or value == "":
        return missing_reason
    if type(value) is not str:
        return "INVALID_ID_TYPE"
    if value != value.strip() or not value.isascii() or not value.isprintable():
        return "INVALID_ID_FORMAT"
    if require_hash and (
            len(value) != 64
            or any(char not in "0123456789abcdef" for char in value.lower())):
        return "INVALID_ID_FORMAT"
    if not require_hash and _CANONICAL_ID.fullmatch(value) is None:
        return "INVALID_ID_FORMAT"
    return None


def deterministic_pair_id(*, candidate_trade_id: str, baseline_trade_id: str,
                          symbol: str, timeframe: str,
                          matching_spec_hash: str, baseline_spec_hash: str,
                          registry_hash: str, campaign_authority_root: str,
                          tournament_spec_hash: str) -> str:
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
        "campaign_authority_root": campaign_authority_root,
        "tournament_spec_hash": tournament_spec_hash,
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
        if not isinstance(row, dict):
            reasons["INVALID_PAIR_ROW"] += 1
            continue
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


_PAIR_NUMERIC_FIELDS = {
    "entry_timestamp", "entry_availability", "max_holding_bars",
    "realised_holding_bars", "notional_eur", "exposure_eur",
    "leverage_simulated", "funding_settlements_crossed", "funding_cost_eur",
}
_PAIR_BOOLEAN_FIELDS = {"end_of_dataset_censored"}
_PAIR_TEXT_FIELDS = set(BASELINE_MATCH_FIELDS) - _PAIR_NUMERIC_FIELDS - _PAIR_BOOLEAN_FIELDS


def _pair_evidence_problems(rows: list[dict], *, candidate: bool,
                            symbol: str, timeframe: str,
                            participant_ids: set[str]) -> Counter:
    reasons: Counter = Counter()
    outcome_key = "candidate_net_eur" if candidate else "baseline_net_eur"
    trade_key = "candidate_trade_id" if candidate else "baseline_trade_id"
    identity_fields = (
        trade_key, "global_event_id", "dependency_cluster_id",
        "underlying_trade_id", "hypothesis_id",
    )
    for row in rows:
        if not isinstance(row, dict):
            reasons["INVALID_PAIR_ROW"] += 1
            continue
        for field in identity_fields:
            problem = _id_problem(row.get(field), f"MISSING_{field.upper()}")
            if problem:
                reasons[problem] += 1
        if row.get("symbol") != symbol or row.get("timeframe") != timeframe:
            reasons["PAIR_SCOPE_MISMATCH"] += 1
        if row.get("side") not in ("LONG", "SHORT"):
            reasons["INVALID_PAIR_SIDE"] += 1
        hypothesis = row.get("hypothesis_id")
        if candidate and hypothesis not in participant_ids:
            reasons["UNAUTHORIZED_HYPOTHESIS_ID"] += 1
        if not candidate and hypothesis != "PREREGISTERED_RANDOM_BASELINE_V10_47_23":
            reasons["UNAUTHORIZED_BASELINE_HYPOTHESIS"] += 1
        for field in BASELINE_MATCH_FIELDS:
            if field not in row or row[field] is None or row[field] == "":
                reasons["MISSING_REQUIRED_PAIR_FIELD"] += 1
                continue
            value = row[field]
            if field in _PAIR_BOOLEAN_FIELDS and type(value) is not bool:
                reasons["INVALID_REQUIRED_PAIR_FIELD"] += 1
            if field in _PAIR_TEXT_FIELDS and (
                    type(value) is not str or value != value.strip()
                    or not value.isascii() or not value.isprintable()):
                reasons["INVALID_REQUIRED_PAIR_FIELD"] += 1
            if field in _PAIR_NUMERIC_FIELDS:
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    reasons["INVALID_REQUIRED_PAIR_FIELD"] += 1
                else:
                    if not math.isfinite(number):
                        reasons["NONFINITE_PAIR_FIELD"] += 1
        numeric_ranges = {
            "entry_timestamp": (0.0, None),
            "entry_availability": (0.0, None),
            "max_holding_bars": (1.0, None),
            "realised_holding_bars": (1.0, None),
            "notional_eur": (1e-12, None),
            "exposure_eur": (1e-12, None),
            "leverage_simulated": (1.0, 1.0),
            "funding_settlements_crossed": (0.0, None),
        }
        for field, (minimum, maximum) in numeric_ranges.items():
            try:
                number = float(row[field])
            except (KeyError, TypeError, ValueError):
                continue
            if not math.isfinite(number) or number < minimum \
                    or (maximum is not None and number > maximum):
                reasons["PAIR_FIELD_OUT_OF_RANGE"] += 1
        try:
            realised = float(row["realised_holding_bars"])
            maximum_hold = float(row["max_holding_bars"])
        except (KeyError, TypeError, ValueError):
            pass
        else:
            if math.isfinite(realised) and math.isfinite(maximum_hold) \
                    and realised > maximum_hold:
                reasons["PAIR_FIELD_OUT_OF_RANGE"] += 1
        for field in (
                "entry_timestamp", "entry_availability", "max_holding_bars",
                "realised_holding_bars", "funding_settlements_crossed"):
            if field in row and type(row[field]) is not int:
                reasons["INVALID_REQUIRED_PAIR_FIELD"] += 1
        try:
            if int(row["entry_availability"]) > int(row["entry_timestamp"]):
                reasons["ENTRY_AVAILABLE_AFTER_ENTRY"] += 1
            if not math.isclose(
                    float(row["exposure_eur"]), float(row["notional_eur"]),
                    rel_tol=0.0, abs_tol=1e-9):
                reasons["EXPOSURE_NOTIONAL_MISMATCH"] += 1
        except (KeyError, TypeError, ValueError):
            pass
        if outcome_key not in row:
            reasons["MISSING_CANONICAL_PAIR_OUTCOME"] += 1
            continue
        try:
            outcome = float(row[outcome_key])
        except (TypeError, ValueError):
            reasons["INVALID_PAIR_OUTCOME"] += 1
        else:
            if not math.isfinite(outcome):
                reasons["NONFINITE_PAIR_OUTCOME"] += 1
    # A dependency cluster is intentionally allowed to repeat: n_eff uses that
    # repetition to cap pseudo-replication. Event and underlying trade identity
    # remain one-to-one within each candidate/baseline stream.
    for key, reason in (
        ("global_event_id", "DUPLICATE_GLOBAL_EVENT_ID"),
        ("underlying_trade_id", "DUPLICATE_UNDERLYING_TRADE_ID"),
    ):
        values = [row.get(key) for row in rows if isinstance(row, dict)]
        valid = [value for value in values if _id_problem(value, "MISSING") is None]
        duplicates = sum(count - 1 for count in Counter(valid).values() if count > 1)
        if duplicates:
            reasons[reason] += duplicates
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
                          baseline_trades: list[dict], campaign_id: str,
                          symbol: str, timeframe: str) -> dict:
    """Order-independent exact pairing under the tracked campaign authority.

    Multiplicity, alpha, correction, tolerances and identity hashes are loaded
    internally. They are deliberately absent from this caller-facing API.
    """
    authority_reasons: Counter = Counter()
    try:
        context = CA.authorize_pairing(
            campaign_id=campaign_id, symbol=symbol, timeframe=timeframe,
        )
        authority = CA.load_campaign_authority(campaign_id)
    except CA.CampaignAuthorityError as exc:
        context, authority = None, None
        authority_reasons[str(exc)] += 1
    m_tournament = context.m_tournament if context else 0
    m_campaign = context.m_campaign if context else 0
    correction_method = context.correction_method if context else "bonferroni"
    alpha = context.alpha if context else 0.05
    entry = context.entry if context else {}
    tolerances = json.loads(json.dumps(BASELINE_TOLERANCE_SPEC))
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
    preflight_reasons.update(authority_reasons)
    participant_ids = set(
        authority.get("participant_spec_hashes", {}) if authority else {}
    )
    preflight_reasons.update(_pair_evidence_problems(
        candidate_trades, candidate=True, symbol=symbol, timeframe=timeframe,
        participant_ids=participant_ids,
    ))
    preflight_reasons.update(_pair_evidence_problems(
        baseline_trades, candidate=False, symbol=symbol, timeframe=timeframe,
        participant_ids=participant_ids,
    ))
    if context and matching_spec_hash != entry.get("matching_spec_hash"):
        preflight_reasons["CANONICAL_MATCHING_SPEC_MISMATCH"] += 1

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
            "m_global": m_tournament or None,
            "m_tournament": m_tournament or None,
            "m_campaign": m_campaign or None,
            "m_campaign_nominal": m_campaign or None,
            "m_campaign_unique_hypotheses": m_campaign or None,
            "m_campaign_unique_results": m_campaign or None,
            "campaign_registry_sha": (
                context.root_anchor_sha256 if context else None
            ),
            "campaign_authority_root": (
                context.root_anchor_sha256 if context else None
            ),
            "campaign_id": campaign_id,
            "authority_status": (
                "CANONICAL_AUTHORITY_VALID" if context else "AUTHORITY_INVALID"
            ),
            "tournament_spec_hash": entry.get("tournament_spec_hash"),
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
            "baseline_spec_hash": entry.get("baseline_spec_hash"),
            "registry_hash": entry.get("tournament_registry_hash"),
            "research_only": True,
            "final_recommendation": "NO LIVE",
        }

    if preflight_reasons:
        return invalid_result(preflight_reasons)

    by_opportunity: dict[Any, list[dict]] = {}
    for baseline in baseline_trades:
        by_opportunity.setdefault(baseline.get("opportunity_id"), []).append(baseline)
    candidates_by_opportunity: dict[Any, list[dict]] = {}
    for candidate in candidate_trades:
        candidates_by_opportunity.setdefault(
            candidate.get("opportunity_id"), []
        ).append(candidate)
    pairing_registry = PairingRegistry()
    pairs: list[dict] = []
    compatible_pairs: list[dict] = []
    impossible = incompatible = 0
    pair_rows_received = 0
    rejection_reasons: Counter = Counter()
    for candidate in sorted(candidate_trades, key=lambda row: row["candidate_trade_id"]):
        candidate_id = candidate["candidate_trade_id"]
        opportunity_id = candidate.get("opportunity_id")
        options = by_opportunity.get(opportunity_id, [])
        candidate_group = candidates_by_opportunity.get(opportunity_id, [])
        if len(options) != 1 or len(candidate_group) != 1:
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
            baseline_spec_hash=entry["baseline_spec_hash"],
            registry_hash=entry["tournament_registry_hash"],
            campaign_authority_root=context.root_anchor_sha256,
            tournament_spec_hash=entry["tournament_spec_hash"],
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
                "candidate_net_eur": float(candidate["candidate_net_eur"]),
                "baseline_net_eur": float(baseline["baseline_net_eur"]),
                "paired_delta_eur": None,
                "match_status": "INCOMPATIBLE",
                "unmatched_reason": f"PAIR_FIELD_INCOMPATIBLE:{mismatch.upper()}",
            })
            pairing_registry.rejected_pairs.append(copy_value(pair))
        else:
            candidate_net = float(candidate["candidate_net_eur"])
            baseline_net = float(baseline["baseline_net_eur"])
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
    statistical_pairs = sorted(
        compatible_pairs,
        key=lambda pair: (pair["entry_timestamp"], pair["pair_id"]),
    )
    deltas = [float(pair["paired_delta_eur"]) for pair in statistical_pairs]
    block = _bootstrap_block_from_pairs(statistical_pairs, timeframe)
    bootstrap = block_bootstrap_mean_lb(
        deltas, block=block, alpha=float(alpha)
    ) if deltas else {
        "mean": 0.0, "mean_lb": 0.0, "total_lb": 0.0, "reps": 0, "n": 0,
    }
    ordered = sorted(deltas)
    median = ordered[len(ordered) // 2] if ordered else 0.0
    raw_p = _one_sided_sign_p_value(deltas)
    tournament_corrected = min(1.0, raw_p * m_tournament)
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
        "m_global": m_tournament,
        "m_tournament": m_tournament,
        "m_campaign": int(m_campaign),
        "m_campaign_nominal": m_campaign,
        "m_campaign_unique_hypotheses": m_campaign,
        "m_campaign_unique_results": m_campaign,
        "campaign_registry_sha": context.root_anchor_sha256,
        "campaign_authority_root": context.root_anchor_sha256,
        "campaign_id": campaign_id,
        "authority_status": "CANONICAL_AUTHORITY_VALID",
        "tournament_spec_hash": entry["tournament_spec_hash"],
        "alpha": float(alpha),
        "match_status": "OK" if complete else "BASELINE_MATCH_INCOMPLETE",
        "pairing_status": "VALID",
        "integrity_status": "PASS",
        "status": "BASELINE_PAIRING_VALID",
        "unmatched_reason": None if complete else "EXACT_MATCH_REQUIRED",
        "rejection_reasons": dict(sorted(rejection_reasons.items())),
        "baseline_gate": beats,
        "beats_matched_random": beats,
        "promotion_allowed": False,
        "promotion_scope": "BASELINE_COMPONENT_GATE_ONLY",
        "pairs": pairs,
        "baseline_simulations_per_candidate": 1,
        "tolerance_spec": tolerances,
        "tolerance_spec_hash": matching_spec_hash,
        "matching_spec_hash": matching_spec_hash,
        "baseline_spec_hash": entry["baseline_spec_hash"],
        "registry_hash": entry["tournament_registry_hash"],
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
