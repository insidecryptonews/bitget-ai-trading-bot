"""V8.2.8 — Duplicate Source Root Cause Audit (research-only).

Explains where dataset duplicates come from. Categories tested:

- ``repeated_cycle_logging`` — same exact bucket logged across multiple
  worker cycles.
- ``same_bar_resampling`` — multiple signals on the same OHLCV candle.
- ``signal_observation_spam`` — high-density burst per (symbol, side).
- ``market_probe_repeats`` — same probe key over and over.
- ``edgeguard_repeat_blocks`` — repeated EdgeGuard-blocked observations.
- ``unknown`` — fallback.

Proposes ``research-only`` fixes (idempotency key, cooldown, per-candle
dedup). Does not enforce production changes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from . import FINAL_RECOMMENDATION_NO_LIVE, STATUS_NEED_DATA, STATUS_OK
from .counterfactual_dedup_audit import _is_evaluable, fingerprint
from .counterfactual_training_dataset import build_dataset


ROOT_CAUSE_REPEATED_CYCLE = "repeated_cycle_logging"
ROOT_CAUSE_SAME_BAR_RESAMPLING = "same_bar_resampling"
ROOT_CAUSE_SIGNAL_OBSERVATION_SPAM = "signal_observation_spam"
ROOT_CAUSE_MARKET_PROBE_REPEATS = "market_probe_repeats"
ROOT_CAUSE_EDGEGUARD_REPEAT_BLOCKS = "edgeguard_repeat_blocks"
ROOT_CAUSE_UNKNOWN = "unknown"

PROPOSED_FIX_IDEMPOTENCY_KEY = "add_idempotency_key_to_signal_observations"
PROPOSED_FIX_COOLDOWN = "candidate_fingerprint_cooldown_per_candle"
PROPOSED_FIX_PER_CANDLE_DEDUP = "no_duplicate_write_per_candle"
PROPOSED_FIX_SEPARATE_REOBS = "separate_signal_seen_again_from_new_observation"


@dataclass
class DuplicateGroup:
    fingerprint: str
    count: int
    symbol: str
    side: str
    regime: str
    timestamp_bucket: str
    probable_root_cause: str
    sample_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DuplicateRootCauseReport:
    hours: int
    generated_at: str
    evaluable_rows: int = 0
    duplicate_rows: int = 0
    duplicate_ratio: float = 0.0
    duplicate_ratio_by_symbol: dict[str, float] = field(default_factory=dict)
    duplicate_ratio_by_side: dict[str, float] = field(default_factory=dict)
    duplicate_ratio_by_regime: dict[str, float] = field(default_factory=dict)
    duplicate_ratio_by_strategy: dict[str, float] = field(default_factory=dict)
    duplicate_ratio_by_source: dict[str, float] = field(default_factory=dict)
    by_root_cause: dict[str, int] = field(default_factory=dict)
    top_duplicate_fingerprints: list[dict[str, Any]] = field(default_factory=list)
    proposed_fixes: list[str] = field(default_factory=list)
    status: str = STATUS_NEED_DATA
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _classify_root_cause(rows: list[dict[str, Any]]) -> tuple[str, str]:
    """Look at the rows sharing a fingerprint and guess where they came from."""
    if not rows:
        return ROOT_CAUSE_UNKNOWN, ""
    # Inspect reason / blocked_by markers.
    reasons = {str(r.get("reason") or "").lower() for r in rows}
    blocked_by = {str(r.get("blocked_by") or "").lower() for r in rows}
    sources = {str(r.get("source") or "").lower() for r in rows}
    # Common case: market_probe.
    if any("market_probe" in s for s in sources):
        return ROOT_CAUSE_MARKET_PROBE_REPEATS, "source_contains_market_probe"
    # EdgeGuard repeats.
    if any("edge_guard" in r or "watch_only" in r for r in reasons | blocked_by):
        return ROOT_CAUSE_EDGEGUARD_REPEAT_BLOCKS, "edgeguard_block_repeated"
    timestamps = {str(r.get("timestamp", ""))[:16] for r in rows}
    ingested_at = {
        str(r.get("ingested_at") or r.get("received_at") or "")
        for r in rows
    }
    ingested_at.discard("")
    # Repeated cycle logging is MORE SPECIFIC than same-bar resampling — the
    # same row gets re-ingested by multiple worker cycles. Check it first.
    if len(timestamps) == 1 and len(ingested_at) > 1:
        return ROOT_CAUSE_REPEATED_CYCLE, "same_timestamp_multiple_ingest_cycles"
    # Same OHLCV candle (same minute-bucket timestamp) — no separating
    # ingest cycle, just resampling within the bar.
    if len(timestamps) == 1 and len(rows) >= 3:
        return ROOT_CAUSE_SAME_BAR_RESAMPLING, "all_rows_share_same_minute_bucket"
    # Burst per symbol+side within a small window → spam.
    if len(rows) >= 10:
        return ROOT_CAUSE_SIGNAL_OBSERVATION_SPAM, "10_or_more_identical_observations"
    return ROOT_CAUSE_UNKNOWN, ""


def _by_attr_ratio(
    all_rows: list[dict[str, Any]],
    fp_groups: dict[str, list[dict[str, Any]]],
    attr: str,
) -> dict[str, float]:
    """Per-attribute duplicate ratio. Duplicate rows are
    ``sum(len(group) - 1 for group)``.
    """
    if not all_rows:
        return {}
    by_attr: dict[str, list[dict[str, Any]]] = {}
    for r in all_rows:
        key = str(r.get(attr, "UNKNOWN")).upper()
        by_attr.setdefault(key, []).append(r)
    out: dict[str, float] = {}
    for attr_val, rs in by_attr.items():
        local_fps: dict[str, list[dict[str, Any]]] = {}
        for r in rs:
            fp = fingerprint(r)
            local_fps.setdefault(fp, []).append(r)
        dup_count = sum(len(g) - 1 for g in local_fps.values() if len(g) > 1)
        out[attr_val] = dup_count / max(len(rs), 1)
    return out


def audit_duplicate_root_cause(
    db: Any = None,
    *,
    hours: int = 168,
    limit: int = 50000,
    rows: Iterable[dict[str, Any]] | None = None,
) -> DuplicateRootCauseReport:
    """Build the V8.2.8 duplicate root cause report."""
    report = DuplicateRootCauseReport(
        hours=int(hours),
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
    if rows is None:
        dataset, _ = build_dataset(db, hours=int(hours), limit=int(limit))
    else:
        dataset = list(rows)
    evaluable = [r for r in dataset if _is_evaluable(r)]
    report.evaluable_rows = len(evaluable)
    if not evaluable:
        return report

    fp_groups: dict[str, list[dict[str, Any]]] = {}
    for r in evaluable:
        fp = fingerprint(r)
        fp_groups.setdefault(fp, []).append(r)

    dup_keys = [(fp, rs) for fp, rs in fp_groups.items() if len(rs) > 1]
    duplicate_rows = sum(len(rs) - 1 for _, rs in dup_keys)
    report.duplicate_rows = duplicate_rows
    report.duplicate_ratio = duplicate_rows / max(len(evaluable), 1)

    report.duplicate_ratio_by_symbol = _by_attr_ratio(evaluable, fp_groups, "symbol")
    report.duplicate_ratio_by_side = _by_attr_ratio(evaluable, fp_groups, "side")
    report.duplicate_ratio_by_regime = _by_attr_ratio(evaluable, fp_groups, "regime")
    report.duplicate_ratio_by_strategy = _by_attr_ratio(evaluable, fp_groups, "strategy")
    report.duplicate_ratio_by_source = _by_attr_ratio(evaluable, fp_groups, "source")

    dup_keys.sort(key=lambda x: len(x[1]), reverse=True)
    for fp, rs in dup_keys[:20]:
        cause, reason = _classify_root_cause(rs)
        report.by_root_cause[cause] = report.by_root_cause.get(cause, 0) + 1
        sample = rs[0]
        report.top_duplicate_fingerprints.append(DuplicateGroup(
            fingerprint=fp,
            count=len(rs),
            symbol=str(sample.get("symbol") or ""),
            side=str(sample.get("side") or ""),
            regime=str(sample.get("regime") or sample.get("market_regime") or ""),
            timestamp_bucket=str(sample.get("timestamp", ""))[:16],
            probable_root_cause=cause,
            sample_reason=reason,
        ).as_dict())
    # Aggregate by_root_cause over ALL duplicate groups (not just top 20).
    full_by_cause: dict[str, int] = {}
    for fp, rs in dup_keys:
        cause, _ = _classify_root_cause(rs)
        full_by_cause[cause] = full_by_cause.get(cause, 0) + 1
    report.by_root_cause = full_by_cause

    proposed = set()
    if full_by_cause.get(ROOT_CAUSE_REPEATED_CYCLE, 0) > 0:
        proposed.add(PROPOSED_FIX_IDEMPOTENCY_KEY)
        proposed.add(PROPOSED_FIX_COOLDOWN)
    if full_by_cause.get(ROOT_CAUSE_SAME_BAR_RESAMPLING, 0) > 0:
        proposed.add(PROPOSED_FIX_PER_CANDLE_DEDUP)
    if full_by_cause.get(ROOT_CAUSE_SIGNAL_OBSERVATION_SPAM, 0) > 0:
        proposed.add(PROPOSED_FIX_SEPARATE_REOBS)
    if full_by_cause.get(ROOT_CAUSE_EDGEGUARD_REPEAT_BLOCKS, 0) > 0:
        proposed.add(PROPOSED_FIX_SEPARATE_REOBS)
    if full_by_cause.get(ROOT_CAUSE_MARKET_PROBE_REPEATS, 0) > 0:
        proposed.add(PROPOSED_FIX_PER_CANDLE_DEDUP)
    report.proposed_fixes = sorted(proposed)
    report.status = STATUS_OK
    return report
