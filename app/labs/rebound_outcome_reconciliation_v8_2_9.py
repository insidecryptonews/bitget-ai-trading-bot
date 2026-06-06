"""V8.2.9.2 — Rebound Outcome Reconciliation (research-only).

Explains why V8.2.8 reported ``rebound_good_count=119/121`` while V8.2.9
sees winrate ≈ 0.10 over 219 candidates on the same VPS dataset. Builds
three candidate sets, computes their winrates raw and after realistic
cost, and classifies the most likely reason for the gap.

Hard contract:

- research-only;
- detection of the V8.2.8-like candidate set uses ONLY prefix-only
  features (strictest variant of the V8.2.9 extractor) — no ``ret_*``
  is read as a detection input;
- outcome reading (`baseline_net_pnl_est`, `mfe_pct`, `mae_pct`) only
  AFTER detection — same separation rule as the extractor.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from . import FINAL_RECOMMENDATION_NO_LIVE, STATUS_NEED_DATA, STATUS_OK
from .edgeguard_repeat_dedup_v8_2_9 import dedup_edgeguard_repeats
from .rebound_long_candidate_extractor_v8_2_9 import (
    extract_rebound_long_candidates,
)


REASON_DIFFERENT_UNIVERSE = "different_candidate_universe"
REASON_COST_FLIPS = "cost_adjustment_flips_outcome"
REASON_DEDUP_REMOVED_WINNERS = "dedup_removed_winners"
REASON_OUTCOME_FIELD_MISMATCH = "outcome_field_mismatch"
REASON_SIGN_BUG = "sign_bug"
REASON_UNKNOWN = "unknown"

COST_REALISTIC_PCT = 0.25
COST_NORMAL_PCT = 0.18
COST_STRESS_PCT = 0.35

# Heuristic thresholds for the cascade classifier.
SIGN_BUG_RATIO_TRIGGER = 0.10
OUTCOME_FIELD_MISMATCH_RATIO_TRIGGER = 0.05
DEDUP_DROPS_WINRATE_TRIGGER = 0.10
DIFFERENT_UNIVERSE_COUNT_DELTA = 0.30


@dataclass
class ReconciliationReport:
    hours: int
    generated_at: str
    candidates_v828_like: int = 0
    candidates_v829_raw: int = 0
    candidates_v829_dedup: int = 0
    winrate_v828_like: float = 0.0
    winrate_v829_raw: float = 0.0
    winrate_v829_dedup: float = 0.0
    net_ev_before_cost: float = 0.0
    net_ev_after_cost_0_18: float = 0.0
    net_ev_after_cost_0_25: float = 0.0
    net_ev_after_cost_0_35: float = 0.0
    sign_bug_count: int = 0
    outcome_field_mismatch_count: int = 0
    reason_for_gap: str = REASON_UNKNOWN
    notes: list[str] = field(default_factory=list)
    examples_top_100: list[dict[str, Any]] = field(default_factory=list)
    used_future_return_features: bool = False
    status: str = STATUS_NEED_DATA
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _winrate(candidates: list[dict[str, Any]]) -> float:
    if not candidates:
        return 0.0
    wins = sum(1 for c in candidates if (c.get("net_pnl_est") or 0) > 0)
    return wins / len(candidates)


def _net_ev(candidates: list[dict[str, Any]], cost: float = 0.0) -> float:
    if not candidates:
        return 0.0
    nets = [
        float(c["net_pnl_est"]) for c in candidates
        if isinstance(c.get("net_pnl_est"), (int, float))
    ]
    if not nets:
        return 0.0
    return (sum(nets) / len(nets)) - cost


def _sign_bug_count(candidates: list[dict[str, Any]], dataset_by_ts: dict[str, dict[str, Any]]) -> int:
    """Count rows whose net_pnl_est sign disagrees with ret_4h_pct sign
    in a way that suggests a side/sign bug.

    For LONG candidates: if ret_4h_pct > +0.50% (clear up move) AND
    net_pnl_est < -0.30% (clear loss), flag as suspicious. ``ret_4h_pct``
    is read ONLY here, on the already-emitted candidate, to detect
    inverted signs — it is NEVER fed back into detection.
    """
    bug = 0
    for c in candidates:
        net = c.get("net_pnl_est")
        if not isinstance(net, (int, float)):
            continue
        ts = str(c.get("timestamp") or "")
        raw = dataset_by_ts.get(ts) or {}
        ret_4h = raw.get("ret_4h_pct")
        if not isinstance(ret_4h, (int, float)):
            continue
        if float(ret_4h) > 0.50 and float(net) < -0.30:
            bug += 1
    return bug


def _outcome_field_mismatch_count(
    candidates: list[dict[str, Any]],
    dataset_by_ts: dict[str, dict[str, Any]],
) -> int:
    """Count rows where the candidate's net_pnl_est disagrees with the
    dataset's baseline_net_pnl_est. Both should be the same value; a
    mismatch points to a serialisation / read bug."""
    mismatch = 0
    for c in candidates:
        net_candidate = c.get("net_pnl_est")
        if not isinstance(net_candidate, (int, float)):
            continue
        ts = str(c.get("timestamp") or "")
        raw = dataset_by_ts.get(ts) or {}
        baseline = raw.get("baseline_net_pnl_est")
        if not isinstance(baseline, (int, float)):
            continue
        if abs(float(net_candidate) - float(baseline)) > 0.01:
            mismatch += 1
    return mismatch


def _classify_reason(
    *,
    raw_count: int,
    dedup_count: int,
    v828_like_count: int,
    winrate_raw: float,
    winrate_dedup: float,
    winrate_v828_like: float,
    net_ev_before_cost: float,
    net_ev_after_cost: float,
    sign_bug_count: int,
    outcome_field_mismatch_count: int,
) -> tuple[str, list[str]]:
    """Cascade classifier — returns ``(reason, notes)``."""
    notes: list[str] = []
    # Highest priority — sign bug.
    if raw_count > 0 and sign_bug_count >= SIGN_BUG_RATIO_TRIGGER * raw_count:
        notes.append(
            f"sign_bug_ratio={sign_bug_count}/{raw_count} "
            f">= trigger {SIGN_BUG_RATIO_TRIGGER}"
        )
        return REASON_SIGN_BUG, notes
    # Outcome serialisation mismatch.
    if raw_count > 0 and outcome_field_mismatch_count >= OUTCOME_FIELD_MISMATCH_RATIO_TRIGGER * raw_count:
        notes.append(
            f"outcome_field_mismatch_ratio={outcome_field_mismatch_count}/{raw_count}"
        )
        return REASON_OUTCOME_FIELD_MISMATCH, notes
    # Cost flips outcome from positive to negative.
    if net_ev_before_cost > 0 and net_ev_after_cost <= 0:
        notes.append(
            f"net_ev_before_cost={net_ev_before_cost:.4f} > 0 but "
            f"after_cost={net_ev_after_cost:.4f} <= 0"
        )
        return REASON_COST_FLIPS, notes
    # Dedup made winrate materially worse — dedup removed winners.
    if dedup_count < raw_count and winrate_dedup < (winrate_raw - DEDUP_DROPS_WINRATE_TRIGGER):
        notes.append(
            f"winrate_raw={winrate_raw:.4f} winrate_dedup={winrate_dedup:.4f} "
            f"(dropped by >= {DEDUP_DROPS_WINRATE_TRIGGER})"
        )
        return REASON_DEDUP_REMOVED_WINNERS, notes
    # V8.2.8-like (strict prefix-only) subset gives a materially better
    # winrate vs raw → different universe.
    if (
        v828_like_count > 0
        and raw_count > 0
        and winrate_v828_like > winrate_raw + 0.20
        and abs(v828_like_count - raw_count) / max(raw_count, 1) > DIFFERENT_UNIVERSE_COUNT_DELTA
    ):
        notes.append(
            f"v828_like_count={v828_like_count} raw={raw_count} "
            f"winrate_v828_like={winrate_v828_like:.4f} > raw={winrate_raw:.4f}"
        )
        return REASON_DIFFERENT_UNIVERSE, notes
    notes.append("no_single_heuristic_triggered")
    return REASON_UNKNOWN, notes


def reconcile_rebound_outcome(
    db: Any = None,
    *,
    hours: int = 168,
    limit: int = 50000,
    rows: Iterable[dict[str, Any]] | None = None,
) -> ReconciliationReport:
    """Run the V8.2.9.2 rebound outcome reconciliation."""
    report = ReconciliationReport(
        hours=int(hours),
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
    if rows is None:
        from .counterfactual_training_dataset import build_dataset
        dataset, _ = build_dataset(db, hours=int(hours), limit=int(limit))
    else:
        dataset = list(rows)
    if not dataset:
        return report

    # V8.2.9 extractor on the dataset (prefix-only).
    extractor = extract_rebound_long_candidates(
        db, hours=hours, limit=limit, rows=dataset,
    )
    raw_candidates = list(extractor.candidates)
    report.candidates_v829_raw = len(raw_candidates)

    # Dedup pass (prefix-only fingerprint).
    deduped, _ = dedup_edgeguard_repeats(raw_candidates, hours=hours)
    report.candidates_v829_dedup = len(deduped)

    # V8.2.8-like subset — same prefix-only extractor, but stricter:
    # require ``bounce_confirmation_prefix=True`` AND
    # ``higher_lows_prefix=True`` AND ``trend_recovering_prefix=True``.
    # Approximates the tighter V8.2.8 detector that filtered to the
    # high-winrate slice. ``ret_*`` is never read for this filtering.
    v828_like = [
        c for c in raw_candidates
        if c.get("bounce_confirmation_prefix") is True
        and c.get("higher_lows_prefix") is True
        and c.get("trend_recovering_prefix") is True
    ]
    report.candidates_v828_like = len(v828_like)

    report.winrate_v829_raw = _winrate(raw_candidates)
    report.winrate_v829_dedup = _winrate(deduped)
    report.winrate_v828_like = _winrate(v828_like)
    report.net_ev_before_cost = _net_ev(raw_candidates, cost=0.0)
    report.net_ev_after_cost_0_18 = _net_ev(raw_candidates, cost=COST_NORMAL_PCT)
    report.net_ev_after_cost_0_25 = _net_ev(raw_candidates, cost=COST_REALISTIC_PCT)
    report.net_ev_after_cost_0_35 = _net_ev(raw_candidates, cost=COST_STRESS_PCT)

    # Sign-bug + outcome-field-mismatch counters — read ``ret_4h_pct`` /
    # ``baseline_net_pnl_est`` ONLY on the already-emitted candidates,
    # never as detection inputs.
    dataset_by_ts: dict[str, dict[str, Any]] = {}
    for r in dataset:
        ts = str(r.get("timestamp") or "")
        if ts:
            dataset_by_ts.setdefault(ts, r)
    report.sign_bug_count = _sign_bug_count(raw_candidates, dataset_by_ts)
    report.outcome_field_mismatch_count = _outcome_field_mismatch_count(
        raw_candidates, dataset_by_ts,
    )

    reason, notes = _classify_reason(
        raw_count=report.candidates_v829_raw,
        dedup_count=report.candidates_v829_dedup,
        v828_like_count=report.candidates_v828_like,
        winrate_raw=report.winrate_v829_raw,
        winrate_dedup=report.winrate_v829_dedup,
        winrate_v828_like=report.winrate_v828_like,
        net_ev_before_cost=report.net_ev_before_cost,
        net_ev_after_cost=report.net_ev_after_cost_0_25,
        sign_bug_count=report.sign_bug_count,
        outcome_field_mismatch_count=report.outcome_field_mismatch_count,
    )
    report.reason_for_gap = reason
    report.notes = notes
    report.examples_top_100 = raw_candidates[:100]
    report.used_future_return_features = False
    report.status = STATUS_OK
    return report
