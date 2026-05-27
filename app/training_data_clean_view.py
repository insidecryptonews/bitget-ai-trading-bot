"""ResearchOps V5 — Training data clean view.

Read-only audit that exposes both RAW and CLEAN (de-duplicated) metrics for the
training pipeline. Does NOT delete rows. Does NOT modify the DB. The intent is
to give the policy builder / dashboard a way to compute EV / PF / TP / TIME on
clean samples while leaving the raw tables intact.

Tables consulted (best-effort, all optional):

  - signal_observations
  - signal_labels
  - trades (paper only — `mode='paper'`)
  - shadow_virtual_trades (Phase 9 V5, optional)

Dedup keys:

  - observation_minute_bucket: floor(observation timestamp / 60s) + symbol + side
  - label_per_observation: keep first label per observation_id
  - trade_setup_bucket: symbol + side + setup_key + entry_time minute bucket

Outputs both RAW and CLEAN counts. The dashboard binds:

  - duplicate_rate = (raw - clean) / raw
  - clean_sample_count, raw_sample_count
  - dedupe_ratio = clean / raw

If duplicate_rate > 0.10 the report's overall status is BAD and the readiness
validator blocks promotion via the existing `data_quality_status` gate.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any


FINAL_RECOMMENDATION = "NO LIVE"

STATUS_OK = "OK"
STATUS_WARNING = "WARNING"
STATUS_BAD = "BAD"
STATUS_UNKNOWN = "UNKNOWN"

# Thresholds tuned for the existing repo. >10% duplicates → BAD, >3% → WARNING.
BAD_DUPLICATE_RATE = 0.10
WARN_DUPLICATE_RATE = 0.03


@dataclass
class TableCleanMetrics:
    table: str
    raw_count: int
    clean_count: int
    duplicates: int
    duplicate_rate: float
    dedupe_ratio: float
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TrainingDataCleanReport:
    hours: int
    overall_status: str
    duplicate_rate: float
    raw_sample_count: int
    clean_sample_count: int
    dedupe_ratio: float
    biggest_problem: str
    tables: list[TableCleanMetrics] = field(default_factory=list)
    orphan_metrics: dict[str, int] = field(default_factory=dict)
    relation_status: str = "UNKNOWN"
    recommended_next_action: str = ""
    no_db_writes: bool = True
    research_only: bool = True
    final_recommendation: str = FINAL_RECOMMENDATION

    def as_dict(self) -> dict[str, Any]:
        return {
            "hours": self.hours,
            "overall_status": self.overall_status,
            "duplicate_rate": self.duplicate_rate,
            "raw_sample_count": self.raw_sample_count,
            "clean_sample_count": self.clean_sample_count,
            "dedupe_ratio": self.dedupe_ratio,
            "biggest_problem": self.biggest_problem,
            "tables": [table.as_dict() for table in self.tables],
            "orphan_metrics": dict(self.orphan_metrics),
            "relation_status": self.relation_status,
            "recommended_next_action": self.recommended_next_action,
            "no_db_writes": self.no_db_writes,
            "research_only": self.research_only,
            "final_recommendation": self.final_recommendation,
        }


def _since_iso(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=max(1, int(hours or 1)))).isoformat()


def _run_query_count(db: Any, sql: str, params: tuple) -> int:
    """Execute a single-value count query. Returns 0 on any error."""
    if not db:
        return 0
    try:
        local_sql = sql
        local_params = params
        if bool(getattr(db, "_use_postgres", False)):
            local_sql = local_sql.replace("?", "%s")
        with db._connect() as conn:
            row = conn.execute(local_sql, local_params).fetchone()
        if not row:
            return 0
        return int(db._row_value(row, "cnt", 0, 0) or 0)
    except Exception:
        return 0


def _table_exists(db: Any, name: str) -> bool:
    if not db:
        return False
    try:
        return bool(db.table_exists(name))
    except Exception:
        return False


def _signal_observations_metrics(db: Any, since_iso: str) -> TableCleanMetrics:
    if not _table_exists(db, "signal_observations"):
        return TableCleanMetrics(
            table="signal_observations",
            raw_count=0, clean_count=0, duplicates=0,
            duplicate_rate=0.0, dedupe_ratio=0.0,
            notes=["table_missing"],
        )
    raw = _run_query_count(
        db,
        "SELECT COUNT(*) AS cnt FROM signal_observations WHERE timestamp >= ?",
        (since_iso,),
    )
    # CLEAN: deduplicate by (symbol, side, minute-bucket of timestamp). We
    # approximate the minute bucket by truncating the iso to YYYY-MM-DDTHH:MM.
    clean_sql = (
        "SELECT COUNT(*) AS cnt FROM ("
        "  SELECT MIN(rowid) AS keep_row FROM signal_observations "
        "  WHERE timestamp >= ? "
        "  GROUP BY symbol, side, substr(timestamp, 1, 16)"
        ") AS deduped"
    )
    clean = _run_query_count(db, clean_sql, (since_iso,))
    duplicates = max(0, raw - clean)
    duplicate_rate = (duplicates / raw) if raw > 0 else 0.0
    dedupe_ratio = (clean / raw) if raw > 0 else 0.0
    return TableCleanMetrics(
        table="signal_observations",
        raw_count=raw,
        clean_count=clean,
        duplicates=duplicates,
        duplicate_rate=duplicate_rate,
        dedupe_ratio=dedupe_ratio,
        notes=["bucket=symbol|side|timestamp_minute"],
    )


def _signal_labels_metrics(db: Any, since_iso: str) -> TableCleanMetrics:
    if not _table_exists(db, "signal_labels"):
        return TableCleanMetrics(
            table="signal_labels",
            raw_count=0, clean_count=0, duplicates=0,
            duplicate_rate=0.0, dedupe_ratio=0.0,
            notes=["table_missing"],
        )
    raw = _run_query_count(
        db,
        "SELECT COUNT(*) AS cnt FROM signal_labels WHERE timestamp >= ?",
        (since_iso,),
    )
    clean_sql = (
        "SELECT COUNT(*) AS cnt FROM ("
        "  SELECT MIN(rowid) AS keep_row FROM signal_labels "
        "  WHERE timestamp >= ? "
        "  GROUP BY observation_id"
        ") AS deduped"
    )
    clean = _run_query_count(db, clean_sql, (since_iso,))
    duplicates = max(0, raw - clean)
    duplicate_rate = (duplicates / raw) if raw > 0 else 0.0
    dedupe_ratio = (clean / raw) if raw > 0 else 0.0
    return TableCleanMetrics(
        table="signal_labels",
        raw_count=raw,
        clean_count=clean,
        duplicates=duplicates,
        duplicate_rate=duplicate_rate,
        dedupe_ratio=dedupe_ratio,
        notes=["bucket=observation_id"],
    )


def _trades_metrics(db: Any, since_iso: str) -> TableCleanMetrics:
    if not _table_exists(db, "trades"):
        return TableCleanMetrics(
            table="trades_paper",
            raw_count=0, clean_count=0, duplicates=0,
            duplicate_rate=0.0, dedupe_ratio=0.0,
            notes=["table_missing"],
        )
    raw = _run_query_count(
        db,
        "SELECT COUNT(*) AS cnt FROM trades WHERE timestamp >= ? AND LOWER(mode) = 'paper'",
        (since_iso,),
    )
    clean_sql = (
        "SELECT COUNT(*) AS cnt FROM ("
        "  SELECT MIN(rowid) AS keep_row FROM trades "
        "  WHERE timestamp >= ? AND LOWER(mode) = 'paper' "
        "  GROUP BY symbol, side, substr(timestamp, 1, 16)"
        ") AS deduped"
    )
    clean = _run_query_count(db, clean_sql, (since_iso,))
    duplicates = max(0, raw - clean)
    duplicate_rate = (duplicates / raw) if raw > 0 else 0.0
    dedupe_ratio = (clean / raw) if raw > 0 else 0.0
    return TableCleanMetrics(
        table="trades_paper",
        raw_count=raw,
        clean_count=clean,
        duplicates=duplicates,
        duplicate_rate=duplicate_rate,
        dedupe_ratio=dedupe_ratio,
        notes=["bucket=symbol|side|timestamp_minute"],
    )


def _orphan_metrics(db: Any, since_iso: str) -> dict[str, int]:
    orphans = {}
    if _table_exists(db, "signal_labels") and _table_exists(db, "signal_observations"):
        sql = (
            "SELECT COUNT(*) AS cnt FROM signal_labels l "
            "WHERE l.timestamp >= ? AND NOT EXISTS ("
            "  SELECT 1 FROM signal_observations o WHERE o.id = l.observation_id"
            ")"
        )
        orphans["labels_without_observation"] = _run_query_count(db, sql, (since_iso,))
    return orphans


def _relation_status_from_orphans(orphans: dict[str, int]) -> str:
    if not orphans:
        return "UNKNOWN"
    if any(value > 0 for value in orphans.values()):
        return "WARNING"
    return "OK"


def _aggregate(tables: list[TableCleanMetrics]) -> tuple[int, int, float, float, str, str]:
    raw_total = sum(t.raw_count for t in tables)
    clean_total = sum(t.clean_count for t in tables)
    duplicate_rate = ((raw_total - clean_total) / raw_total) if raw_total > 0 else 0.0
    dedupe_ratio = (clean_total / raw_total) if raw_total > 0 else 0.0
    if raw_total == 0:
        return raw_total, clean_total, duplicate_rate, dedupe_ratio, STATUS_UNKNOWN, "no_data"
    if duplicate_rate >= BAD_DUPLICATE_RATE:
        return raw_total, clean_total, duplicate_rate, dedupe_ratio, STATUS_BAD, "duplicates"
    if duplicate_rate >= WARN_DUPLICATE_RATE:
        return raw_total, clean_total, duplicate_rate, dedupe_ratio, STATUS_WARNING, "duplicates"
    return raw_total, clean_total, duplicate_rate, dedupe_ratio, STATUS_OK, "none"


def run_training_data_clean_view(
    db: Any,
    *,
    hours: int = 24,
) -> TrainingDataCleanReport:
    since_iso = _since_iso(hours)
    tables = [
        _signal_observations_metrics(db, since_iso),
        _signal_labels_metrics(db, since_iso),
        _trades_metrics(db, since_iso),
    ]
    raw_total, clean_total, duplicate_rate, dedupe_ratio, status, biggest = _aggregate(tables)
    orphans = _orphan_metrics(db, since_iso)
    relation_status = _relation_status_from_orphans(orphans)
    recommended = ""
    if status == STATUS_BAD:
        recommended = (
            "duplicate_rate>=10%_block_paper_demo_readiness_and_use_clean_view_for_ev_pf"
        )
    elif status == STATUS_WARNING:
        recommended = "duplicate_rate>=3%_review_dedup_pipeline_metrics"
    elif status == STATUS_OK:
        recommended = "dedup_within_band_continue_research_only"
    else:
        recommended = "no_data_in_window_increase_hours"
    return TrainingDataCleanReport(
        hours=int(hours),
        overall_status=status,
        duplicate_rate=duplicate_rate,
        raw_sample_count=raw_total,
        clean_sample_count=clean_total,
        dedupe_ratio=dedupe_ratio,
        biggest_problem=biggest,
        tables=tables,
        orphan_metrics=orphans,
        relation_status=relation_status,
        recommended_next_action=recommended,
    )


def render_training_data_clean_view_text(report: TrainingDataCleanReport) -> str:
    lines = [
        "TRAINING DATA CLEAN VIEW START",
        f"hours: {report.hours}",
        f"overall_status: {report.overall_status}",
        f"duplicate_rate: {report.duplicate_rate:.4f}",
        f"raw_sample_count: {report.raw_sample_count}",
        f"clean_sample_count: {report.clean_sample_count}",
        f"dedupe_ratio: {report.dedupe_ratio:.4f}",
        f"biggest_problem: {report.biggest_problem}",
        f"relation_status: {report.relation_status}",
        f"recommended_next_action: {report.recommended_next_action}",
        "table | raw | clean | duplicates | dup_rate | dedupe_ratio | notes",
    ]
    for table in report.tables:
        lines.append(
            f"{table.table} | {table.raw_count} | {table.clean_count} | "
            f"{table.duplicates} | {table.duplicate_rate:.4f} | "
            f"{table.dedupe_ratio:.4f} | {','.join(table.notes) or '-'}"
        )
    if report.orphan_metrics:
        lines.append("orphan_metrics:")
        for key, value in report.orphan_metrics.items():
            lines.append(f"- {key}={value}")
    lines.extend([
        "no_db_writes: true",
        "research_only: true",
        "paper_filter_enabled: false",
        "can_send_real_orders: false",
        "final_recommendation: NO LIVE",
        "TRAINING DATA CLEAN VIEW END",
    ])
    return "\n".join(lines)
