"""V8.2.5 — Counterfactual Dataset Dedup Audit (research-only).

Detects duplicate outcome rows in the V8.2.4 dataset by fingerprinting each
evaluable observation, then recomputes RAW vs DEDUP metrics so the operator
can see whether the apparent edge survives deduplication.

Hard contract:
- read-only.
- never writes to DB.
- never opens orders.
- ``final_recommendation: NO LIVE``.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from . import (
    FINAL_RECOMMENDATION_NO_LIVE,
    STATUS_NEED_DATA,
    STATUS_OK,
)
from .counterfactual_training_dataset import build_dataset


DEFAULT_BUCKET_MINUTES = 5
DEFAULT_INFLATION_RAW_NET_EV_THRESHOLD = 0.20  # %
DEFAULT_INFLATION_DEDUP_NET_EV_CAP = 0.05      # %


# ---- Public helpers (also re-used by score / cost-stress audits) -----------

def _is_evaluable(row: dict[str, Any]) -> bool:
    """Row is usable for outcome-based analysis."""
    label = str(row.get("training_label", ""))
    if label in {"NEED_DATA", "UNCERTAIN"}:
        return False
    return row.get("baseline_net_pnl_est") is not None


def _round(value: Any, digits: int) -> Any:
    if value is None:
        return None
    try:
        return round(float(value), digits)
    except Exception:
        return None


def _ts_bucket(ts: str, minutes: int = DEFAULT_BUCKET_MINUTES) -> str:
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        floored = (dt.minute // max(minutes, 1)) * max(minutes, 1)
        return dt.replace(minute=floored, second=0, microsecond=0).isoformat()
    except Exception:
        return str(ts)[:16]


def fingerprint(row: dict[str, Any], *, bucket_minutes: int = DEFAULT_BUCKET_MINUTES) -> str:
    """Outcome fingerprint of an evaluable row. Same fingerprint → duplicate."""
    parts = [
        _ts_bucket(str(row.get("timestamp", "")), bucket_minutes),
        str(row.get("symbol", "")).upper(),
        str(row.get("side", "")).upper(),
        str(row.get("regime", "")).upper(),
        str(row.get("strategy", "")),
        str(_round(row.get("entry_price"), 4)),
        str(_round(row.get("ret_1h_pct"), 3)),
        str(_round(row.get("ret_4h_pct"), 3)),
        str(_round(row.get("mfe_pct"), 3)),
        str(_round(row.get("mae_pct"), 3)),
        str(row.get("first_barrier_hit", "")),
        str(row.get("baseline_result", "")),
    ]
    raw = "|".join(parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def dedup_rows(
    rows: Iterable[dict[str, Any]],
    *,
    bucket_minutes: int = DEFAULT_BUCKET_MINUTES,
) -> list[dict[str, Any]]:
    """Return one row per unique outcome fingerprint, in first-seen order."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for r in rows:
        fp = fingerprint(r, bucket_minutes=bucket_minutes)
        if fp in seen:
            continue
        seen.add(fp)
        out.append(r)
    return out


# ---- Report ---------------------------------------------------------------

@dataclass
class DedupAuditReport:
    hours: int
    generated_at: str
    total_rows: int = 0
    evaluable_rows: int = 0
    duplicate_rows: int = 0
    unique_outcomes: int = 0
    duplicate_ratio: float = 0.0
    raw_metrics: dict[str, Any] = field(default_factory=dict)
    dedup_metrics: dict[str, Any] = field(default_factory=dict)
    top_duplicate_fingerprints: list[dict[str, Any]] = field(default_factory=list)
    inflated_symbols: list[dict[str, Any]] = field(default_factory=list)
    raw_vs_dedup_by_symbol: list[dict[str, Any]] = field(default_factory=list)
    raw_vs_dedup_by_side: list[dict[str, Any]] = field(default_factory=list)
    raw_vs_dedup_by_regime: list[dict[str, Any]] = field(default_factory=list)
    raw_vs_dedup_label_counts: dict[str, Any] = field(default_factory=dict)
    status: str = STATUS_NEED_DATA
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---- Aggregations ---------------------------------------------------------

def _group_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"count": 0, "winrate": 0.0, "net_ev_avg_pct": 0.0, "pf": 0.0}
    nets: list[float] = []
    for r in rows:
        try:
            nets.append(float(r.get("baseline_net_pnl_est") or 0))
        except Exception:
            continue
    if not nets:
        return {"count": 0, "winrate": 0.0, "net_ev_avg_pct": 0.0, "pf": 0.0}
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n < 0]
    loss_sum = abs(sum(losses))
    pf = (sum(wins) / loss_sum) if loss_sum > 0 else 0.0
    return {
        "count": len(nets),
        "winrate": len(wins) / max(len(nets), 1),
        "net_ev_avg_pct": sum(nets) / max(len(nets), 1),
        "pf": pf,
    }


def _by_attr(rows: list[dict[str, Any]], attr: str) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        key = str(r.get(attr, "UNKNOWN")).upper()
        out.setdefault(key, []).append(r)
    return out


def _label_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        label = str(r.get("training_label", "UNKNOWN"))
        out[label] = out.get(label, 0) + 1
    return out


# ---- Main entry point -----------------------------------------------------

def audit_dedup(
    db: Any = None,
    *,
    hours: int = 168,
    limit: int = 50000,
    bucket_minutes: int = DEFAULT_BUCKET_MINUTES,
    rows: Iterable[dict[str, Any]] | None = None,
) -> DedupAuditReport:
    report = DedupAuditReport(
        hours=int(hours),
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
    if rows is None:
        dataset, _ = build_dataset(db, hours=int(hours), limit=int(limit))
    else:
        dataset = list(rows)
    report.total_rows = len(dataset)
    if not dataset:
        return report
    evaluable = [r for r in dataset if _is_evaluable(r)]
    report.evaluable_rows = len(evaluable)
    if not evaluable:
        report.status = STATUS_NEED_DATA
        return report

    # Fingerprint each evaluable row.
    fp_groups: dict[str, list[dict[str, Any]]] = {}
    for r in evaluable:
        fp = fingerprint(r, bucket_minutes=bucket_minutes)
        fp_groups.setdefault(fp, []).append(r)

    unique_keys = len(fp_groups)
    dup_keys = [(fp, rs) for fp, rs in fp_groups.items() if len(rs) > 1]
    duplicate_rows = sum(len(rs) - 1 for _, rs in dup_keys)

    report.duplicate_rows = duplicate_rows
    report.unique_outcomes = unique_keys
    report.duplicate_ratio = duplicate_rows / max(len(evaluable), 1)

    dedup = [rs[0] for _, rs in fp_groups.items()]

    report.raw_metrics = _group_metrics(evaluable)
    report.dedup_metrics = _group_metrics(dedup)

    # By symbol with inflation detection.
    raw_by_sym = _by_attr(evaluable, "symbol")
    dedup_by_sym = _by_attr(dedup, "symbol")
    for symbol in sorted(set(raw_by_sym.keys()) | set(dedup_by_sym.keys())):
        raw_m = _group_metrics(raw_by_sym.get(symbol, []))
        dd_m = _group_metrics(dedup_by_sym.get(symbol, []))
        entry = {
            "symbol": symbol,
            "raw_count": raw_m["count"],
            "raw_winrate": raw_m["winrate"],
            "raw_net_ev": raw_m["net_ev_avg_pct"],
            "dedup_count": dd_m["count"],
            "dedup_winrate": dd_m["winrate"],
            "dedup_net_ev": dd_m["net_ev_avg_pct"],
            "inflation_factor": raw_m["count"] / max(dd_m["count"], 1),
            "winrate_drop": raw_m["winrate"] - dd_m["winrate"],
            "net_ev_drop": raw_m["net_ev_avg_pct"] - dd_m["net_ev_avg_pct"],
        }
        report.raw_vs_dedup_by_symbol.append(entry)

    inflated: list[dict[str, Any]] = []
    for entry in report.raw_vs_dedup_by_symbol:
        if (
            entry["raw_net_ev"] > DEFAULT_INFLATION_RAW_NET_EV_THRESHOLD
            and entry["dedup_net_ev"] < DEFAULT_INFLATION_DEDUP_NET_EV_CAP
        ):
            inflated.append({
                "symbol": entry["symbol"],
                "raw_net_ev": entry["raw_net_ev"],
                "dedup_net_ev": entry["dedup_net_ev"],
                "inflation_factor": entry["inflation_factor"],
                "winrate_drop": entry["winrate_drop"],
            })
    inflated.sort(key=lambda x: x["winrate_drop"], reverse=True)
    report.inflated_symbols = inflated

    # By side.
    raw_by_side = _by_attr(evaluable, "side")
    dedup_by_side = _by_attr(dedup, "side")
    for side in sorted(set(raw_by_side.keys()) | set(dedup_by_side.keys())):
        report.raw_vs_dedup_by_side.append({
            "side": side,
            "raw_count": _group_metrics(raw_by_side.get(side, []))["count"],
            "raw_net_ev": _group_metrics(raw_by_side.get(side, []))["net_ev_avg_pct"],
            "dedup_count": _group_metrics(dedup_by_side.get(side, []))["count"],
            "dedup_net_ev": _group_metrics(dedup_by_side.get(side, []))["net_ev_avg_pct"],
        })

    raw_by_regime = _by_attr(evaluable, "regime")
    dedup_by_regime = _by_attr(dedup, "regime")
    for regime in sorted(set(raw_by_regime.keys()) | set(dedup_by_regime.keys())):
        report.raw_vs_dedup_by_regime.append({
            "regime": regime,
            "raw_count": _group_metrics(raw_by_regime.get(regime, []))["count"],
            "raw_net_ev": _group_metrics(raw_by_regime.get(regime, []))["net_ev_avg_pct"],
            "dedup_count": _group_metrics(dedup_by_regime.get(regime, []))["count"],
            "dedup_net_ev": _group_metrics(dedup_by_regime.get(regime, []))["net_ev_avg_pct"],
        })

    report.raw_vs_dedup_label_counts = {
        "raw": _label_counts(evaluable),
        "dedup": _label_counts(dedup),
    }

    dup_keys.sort(key=lambda x: len(x[1]), reverse=True)
    for fp, rs in dup_keys[:20]:
        sample = rs[0]
        report.top_duplicate_fingerprints.append({
            "fingerprint": fp,
            "count": len(rs),
            "symbol": sample.get("symbol"),
            "side": sample.get("side"),
            "regime": sample.get("regime"),
            "first_barrier_hit": sample.get("first_barrier_hit"),
            "baseline_net_pnl_est": sample.get("baseline_net_pnl_est"),
        })

    report.status = STATUS_OK
    return report
