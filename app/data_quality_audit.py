"""Data Quality Audit — READ-ONLY diagnosis of DB integrity issues.

Separates real problems (exact duplicates, true orphans, conflicting labels)
from benign density (e.g. many signal_observations within the same minute
because the bot scans every 30s and 10 symbols are observed in parallel).

THIS MODULE NEVER DELETES OR MODIFIES DATA. Even when called with `--apply`,
the repair path is dry-run by default and writes an audit log of intended
changes only. A separate confirmation flag would be needed for actual
mutation, and that path is NOT implemented in this module — left as
'PROPOSED_BUT_NOT_IMPLEMENTED' on purpose.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .utils import iso_utc, safe_float, safe_int


FINAL_RECOMMENDATION = "NO LIVE"


@dataclass
class TableDiagnosis:
    table: str
    total_rows: int
    exact_duplicate_count: int
    exact_duplicate_rate: float
    minute_bucket_density_peak: int
    minute_bucket_density_avg: float
    minute_bucket_density_benign_threshold: int
    duplicate_classification: str   # 'EXACT_DUPLICATE', 'BENIGN_DENSITY', 'MIXED', 'CLEAN'
    sample_duplicate_fingerprints: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RelationDiagnosis:
    orphan_path_metrics_count: int
    labels_without_observation_count: int
    multiple_labels_per_observation_count: int
    conflicting_labels_count: int
    observations_with_label_mismatched_path_count: int
    overall_status: str           # 'OK', 'WARNING', 'BAD'
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DataQualityReport:
    generated_at: str
    hours: int
    tables: list[TableDiagnosis]
    relations: RelationDiagnosis
    overall_status: str
    recommended_action: str
    notes: list[str] = field(default_factory=list)
    final_recommendation: str = FINAL_RECOMMENDATION
    research_only: bool = True
    can_delete: bool = False

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        return payload


TABLES_TO_AUDIT: tuple[str, ...] = (
    "signal_observations",
    "signal_labels",
    "signal_path_metrics",
    "events",
    "trades",
    "latency_metrics",
)


# Density thresholds tuned to the bot's known scan cadence:
# - 10 symbols × ~2 scans/min × LONG/SHORT shadow variants = ~40 rows/min benign
# - We mark anything above 60 as suspicious, above 120 as likely artefact.
BENIGN_MINUTE_DENSITY = {
    "signal_observations": 80,
    "signal_labels": 80,
    "signal_path_metrics": 120,
    "events": 40,
    "trades": 5,
    "latency_metrics": 200,
}


class DataQualityAudit:
    """Read-only audit of DB integrity."""

    def __init__(self, db: Any, logger: Any | None = None) -> None:
        self.db = db
        self.logger = logger

    def build(self, *, hours: int = 24) -> DataQualityReport:
        since_iso = (datetime.now(timezone.utc) - timedelta(hours=max(1, int(hours or 24)))).isoformat()
        diagnoses: list[TableDiagnosis] = []
        for table in TABLES_TO_AUDIT:
            try:
                diagnoses.append(self._diagnose_table(table, since_iso=since_iso))
            except Exception as exc:
                diagnoses.append(TableDiagnosis(
                    table=table, total_rows=0, exact_duplicate_count=0,
                    exact_duplicate_rate=0.0, minute_bucket_density_peak=0,
                    minute_bucket_density_avg=0.0,
                    minute_bucket_density_benign_threshold=BENIGN_MINUTE_DENSITY.get(table, 100),
                    duplicate_classification="ERROR",
                    notes=[f"audit_failed:{exc}"],
                ))
        relations = self._diagnose_relations(since_iso=since_iso)
        # Overall status: aggregate worst signal.
        overall = self._overall_status(diagnoses, relations)
        recommended = self._recommend(overall, diagnoses, relations)
        notes: list[str] = [
            "READ_ONLY_AUDIT_NO_DELETE",
            "REPAIR_NOT_IMPLEMENTED_IN_THIS_MODULE",
        ]
        return DataQualityReport(
            generated_at=iso_utc(),
            hours=hours,
            tables=diagnoses,
            relations=relations,
            overall_status=overall,
            recommended_action=recommended,
            notes=notes,
        )

    def _diagnose_table(self, table: str, *, since_iso: str) -> TableDiagnosis:
        total = self._count_rows(table, since_iso=since_iso)
        threshold = BENIGN_MINUTE_DENSITY.get(table, 100)
        dup_count, samples = self._exact_duplicates(table, since_iso=since_iso)
        dup_rate = dup_count / max(total, 1)
        peak, avg = self._minute_density(table, since_iso=since_iso)
        classification = self._classify_duplicates(dup_rate, peak, threshold)
        notes: list[str] = []
        if classification == "BENIGN_DENSITY":
            notes.append("peak_density_above_threshold_but_no_exact_duplicates")
        elif classification == "EXACT_DUPLICATE":
            notes.append("exact_duplicate_fingerprints_present")
        elif classification == "MIXED":
            notes.append("both_density_and_exact_duplicates_present")
        return TableDiagnosis(
            table=table,
            total_rows=total,
            exact_duplicate_count=dup_count,
            exact_duplicate_rate=dup_rate,
            minute_bucket_density_peak=peak,
            minute_bucket_density_avg=avg,
            minute_bucket_density_benign_threshold=threshold,
            duplicate_classification=classification,
            sample_duplicate_fingerprints=samples[:5],
            notes=notes,
        )

    def _count_rows(self, table: str, *, since_iso: str) -> int:
        if not self._table_exists(table):
            return 0
        column = self._timestamp_column(table)
        if column:
            sql = f"SELECT COUNT(*) AS count FROM {table} WHERE {column} >= ?"
            params: tuple[Any, ...] = (since_iso,)
        else:
            sql = f"SELECT COUNT(*) AS count FROM {table}"
            params = ()
        if self._use_postgres():
            sql = sql.replace("?", "%s")
        return safe_int(self._scalar(sql, params))

    def _exact_duplicates(self, table: str, *, since_iso: str) -> tuple[int, list[dict[str, Any]]]:
        """Detect exact duplicates by a small fingerprint.

        Fingerprint columns per table — chosen to NOT false-positive on benign
        density: two rows with the same fingerprint are very likely the same
        logical record persisted twice.
        """
        fingerprint = {
            "signal_observations": ("timestamp", "symbol", "side", "strategy_type", "confidence_score", "entry_price"),
            "signal_labels": ("observation_id", "first_barrier_hit", "bars_to_outcome"),
            "signal_path_metrics": ("observation_id", "source", "probe_key"),
            "events": ("timestamp", "event_type", "message"),
            "trades": ("timestamp", "symbol", "side", "entry_price"),
            "latency_metrics": ("timestamp", "metric_name", "duration_ms"),
        }.get(table)
        if not fingerprint or not self._table_exists(table):
            return 0, []
        existing_columns = set(self._table_columns(table))
        fingerprint = tuple(c for c in fingerprint if c in existing_columns)
        if not fingerprint:
            return 0, []
        column = self._timestamp_column(table)
        where = f"WHERE {column} >= ?" if column else ""
        cols_csv = ", ".join(fingerprint)
        sql = (
            f"SELECT {cols_csv}, COUNT(*) AS dup_count FROM {table} {where} "
            f"GROUP BY {cols_csv} HAVING COUNT(*) > 1 ORDER BY dup_count DESC LIMIT 200"
        )
        params: tuple[Any, ...] = (since_iso,) if column else ()
        if self._use_postgres():
            sql = sql.replace("?", "%s")
        rows = self._fetch(sql, params)
        total_dup = 0
        samples: list[dict[str, Any]] = []
        for row in rows:
            dup_n = safe_int(row.get("dup_count"))
            if dup_n <= 1:
                continue
            # Count surplus rows (n - 1 is the duplication beyond the first)
            total_dup += dup_n - 1
            samples.append({key: row.get(key) for key in fingerprint} | {"dup_count": dup_n})
        return total_dup, samples

    def _minute_density(self, table: str, *, since_iso: str) -> tuple[int, float]:
        if not self._table_exists(table):
            return 0, 0.0
        column = self._timestamp_column(table)
        if not column:
            return 0, 0.0
        # SQLite/Postgres minute bucket via substr of ISO timestamp.
        sql = (
            f"SELECT substr({column}, 1, 16) AS minute_bucket, COUNT(*) AS cnt "
            f"FROM {table} WHERE {column} >= ? GROUP BY minute_bucket ORDER BY cnt DESC LIMIT 1000"
        )
        params: tuple[Any, ...] = (since_iso,)
        if self._use_postgres():
            sql = sql.replace("?", "%s")
        rows = self._fetch(sql, params)
        counts = [safe_int(row.get("cnt")) for row in rows]
        if not counts:
            return 0, 0.0
        return max(counts), sum(counts) / len(counts)

    def _diagnose_relations(self, *, since_iso: str) -> RelationDiagnosis:
        orphan = self._scalar_safe(
            "SELECT COUNT(*) FROM signal_path_metrics spm LEFT JOIN signal_observations so "
            "ON so.id = spm.observation_id WHERE so.id IS NULL"
        )
        labels_no_obs = self._scalar_safe(
            "SELECT COUNT(*) FROM signal_labels sl LEFT JOIN signal_observations so "
            "ON so.id = sl.observation_id WHERE so.id IS NULL"
        )
        multi_labels = self._scalar_safe(
            "SELECT COUNT(*) FROM (SELECT observation_id, COUNT(*) AS n FROM signal_labels "
            "GROUP BY observation_id HAVING COUNT(*) > 1) AS multi"
        )
        # Conflicting labels: same observation_id has both label=1 and label=-1.
        conflicting = self._scalar_safe(
            "SELECT COUNT(*) FROM (SELECT observation_id FROM signal_labels GROUP BY observation_id "
            "HAVING SUM(CASE WHEN label=1 THEN 1 ELSE 0 END) > 0 "
            "AND SUM(CASE WHEN label=-1 THEN 1 ELSE 0 END) > 0) AS conflicting"
        )
        # path_metric vs label mismatch: a row in signal_path_metrics whose
        # observation_id corresponds to a label with a clearly conflicting
        # first_barrier_hit value (heuristic check).
        path_mismatch = self._scalar_safe(
            "SELECT COUNT(*) FROM signal_path_metrics spm JOIN signal_labels sl "
            "ON sl.observation_id = spm.observation_id "
            "WHERE (spm.status IN ('TP_HIT','SL_HIT') AND sl.first_barrier_hit='TIME')"
        )
        worst = max(orphan, labels_no_obs, multi_labels, conflicting, path_mismatch)
        if conflicting > 0 or labels_no_obs > 0:
            overall = "BAD"
        elif worst > 0:
            overall = "WARNING"
        else:
            overall = "OK"
        notes: list[str] = []
        if orphan > 0:
            notes.append("orphan_path_metrics_likely_observation_purged_or_inserted_async")
        if multi_labels > 0:
            notes.append("multi_labels_per_observation_check_labeler_idempotency")
        return RelationDiagnosis(
            orphan_path_metrics_count=orphan,
            labels_without_observation_count=labels_no_obs,
            multiple_labels_per_observation_count=multi_labels,
            conflicting_labels_count=conflicting,
            observations_with_label_mismatched_path_count=path_mismatch,
            overall_status=overall,
            notes=notes,
        )

    def _overall_status(self, diagnoses: list[TableDiagnosis], relations: RelationDiagnosis) -> str:
        if any(d.duplicate_classification in {"EXACT_DUPLICATE", "MIXED"} for d in diagnoses):
            return "BAD"
        if relations.overall_status == "BAD":
            return "BAD"
        if any(d.duplicate_classification == "BENIGN_DENSITY" for d in diagnoses):
            return "WARNING"
        if relations.overall_status == "WARNING":
            return "WARNING"
        return "OK"

    def _recommend(self, overall: str, diagnoses: list[TableDiagnosis], relations: RelationDiagnosis) -> str:
        if overall == "BAD":
            return "FIX_DATA_PIPELINE_AUDIT_FIRST_NO_DELETE_BLIND"
        if overall == "WARNING":
            return "MONITOR_DENSITY_AND_ORPHANS_NO_ACTION_REQUIRED"
        return "NO_ACTION"

    # SQL helpers ---------------------------------------------------------

    def _use_postgres(self) -> bool:
        return bool(getattr(self.db, "_use_postgres", False))

    def _table_exists(self, table: str) -> bool:
        try:
            return bool(self.db.table_exists(table))
        except Exception:
            return False

    def _table_columns(self, table: str) -> list[str]:
        try:
            return list(self.db.get_table_columns(table))
        except Exception:
            return []

    def _timestamp_column(self, table: str) -> str:
        columns = set(self._table_columns(table))
        for candidate in ("timestamp", "created_at", "signal_timestamp"):
            if candidate in columns:
                return candidate
        return ""

    def _scalar(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        try:
            with self.db._connect() as conn:
                row = conn.execute(sql, params).fetchone()
                if row is None:
                    return 0
                if isinstance(row, dict):
                    return next(iter(row.values()), 0)
                try:
                    return row[0]
                except Exception:
                    return 0
        except Exception:
            return 0

    def _scalar_safe(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        if self._use_postgres():
            sql = sql.replace("?", "%s")
        try:
            return safe_int(self._scalar(sql, params))
        except Exception:
            return 0

    def _fetch(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        try:
            with self.db._connect() as conn:
                cursor = conn.execute(sql, params)
                if hasattr(self.db, "_fetchall_dicts"):
                    return self.db._fetchall_dicts(cursor)
                return [dict(row) for row in cursor.fetchall()]
        except Exception:
            return []

    @staticmethod
    def _classify_duplicates(dup_rate: float, peak_density: int, benign_threshold: int) -> str:
        has_exact = dup_rate > 0.0001  # 0.01% threshold for "non-trivial"
        high_density = peak_density > benign_threshold
        if has_exact and high_density:
            return "MIXED"
        if has_exact:
            return "EXACT_DUPLICATE"
        if high_density:
            return "BENIGN_DENSITY"
        return "CLEAN"


def render_report_text(report: DataQualityReport) -> str:
    lines = [
        "DATA QUALITY AUDIT START",
        f"generated_at: {report.generated_at}",
        f"hours: {report.hours}",
        f"overall_status: {report.overall_status}",
        f"recommended_action: {report.recommended_action}",
        "tables:",
    ]
    for t in report.tables:
        lines.append(
            f"- table={t.table} total={t.total_rows} dup_count={t.exact_duplicate_count} "
            f"dup_rate={t.exact_duplicate_rate:.6f} minute_peak={t.minute_bucket_density_peak} "
            f"minute_avg={t.minute_bucket_density_avg:.2f} threshold={t.minute_bucket_density_benign_threshold} "
            f"classification={t.duplicate_classification} notes={','.join(t.notes) if t.notes else 'none'}"
        )
    lines.append("relations:")
    r = report.relations
    lines.append(
        f"- orphan_path_metrics={r.orphan_path_metrics_count} labels_without_obs={r.labels_without_observation_count} "
        f"multi_labels={r.multiple_labels_per_observation_count} conflicting_labels={r.conflicting_labels_count} "
        f"path_mismatch={r.observations_with_label_mismatched_path_count} status={r.overall_status}"
    )
    lines.append("can_delete: false")
    lines.append("research_only: true")
    lines.append("repair_implemented: false")
    lines.append(f"final_recommendation: {report.final_recommendation}")
    lines.append("DATA QUALITY AUDIT END")
    return "\n".join(lines)
