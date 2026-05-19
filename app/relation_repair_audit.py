from __future__ import annotations

import sqlite3
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from .data_pipeline_diagnosis import _fetch_rows
from .training_data_integrity import _columns, _scalar, _table_exists
from .utils import safe_int


START = "RELATION REPAIR AUDIT START"
END = "RELATION REPAIR AUDIT END"


class RelationRepairAudit:
    """Read-only relation audit; proposes safe fixes without mutating DB."""

    def __init__(self, config: Any, db: Any) -> None:
        self.config = config
        self.db = db

    def build(self, *, hours: int = 24) -> dict[str, Any]:
        hours = max(1, int(hours or 24))
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        orphan_paths = self._orphan_path_metrics(since)
        orphan_labels = self._orphan_labels(since)
        duplicated = self._duplicated_labels()
        mismatches = self._mismatches(since)
        old_unlabeled = self._old_unlabeled_observations()
        status = _relation_status(orphan_paths, orphan_labels, duplicated, mismatches, old_unlabeled)
        return {
            "hours": hours,
            "orphan_path_metrics_total": orphan_paths["total"],
            "orphan_classification": orphan_paths["classification"],
            "orphan_examples_sanitized": orphan_paths["examples"],
            "path_metrics_without_label": orphan_paths["without_label"],
            "path_metrics_without_observation": orphan_paths["without_observation"],
            "labels_without_observation": orphan_labels,
            "observations_old_without_label": old_unlabeled,
            "duplicated_labels": duplicated["duplicated_labels"],
            "conflicting_labels": duplicated["conflicting_labels"],
            "timestamp_mismatch_count": mismatches["timestamp_mismatch_count"],
            "source_mismatch_count": mismatches["source_mismatch_count"],
            "labels_with_compatible_path_metric": mismatches["labels_with_compatible_path_metric"],
            "relation_health_status": status,
            "safe_fix_recommendations": _safe_recommendations(status, orphan_paths, duplicated),
            "unsafe_actions_not_taken": [
                "no_delete",
                "no_destructive_migration",
                "no_backfill_written",
                "no_foreign_key_rewrite",
            ],
            "final_recommendation": "NO LIVE",
        }

    def to_text(self, *, hours: int = 24) -> str:
        payload = self.build(hours=hours)
        lines = [
            START,
            f"hours: {payload['hours']}",
            f"orphan_path_metrics_total: {payload['orphan_path_metrics_total']}",
            "orphan_classification:",
            *[f"- {key}: {value}" for key, value in sorted(payload["orphan_classification"].items())],
            f"path_metrics_without_label: {payload['path_metrics_without_label']}",
            f"path_metrics_without_observation: {payload['path_metrics_without_observation']}",
            f"labels_without_observation: {payload['labels_without_observation']}",
            f"observations_old_without_label: {payload['observations_old_without_label']}",
            f"duplicated_labels: {payload['duplicated_labels']}",
            f"conflicting_labels: {payload['conflicting_labels']}",
            f"timestamp_mismatch_count: {payload['timestamp_mismatch_count']}",
            f"source_mismatch_count: {payload['source_mismatch_count']}",
            f"labels_with_compatible_path_metric: {payload['labels_with_compatible_path_metric']}",
            f"relation_health_status: {payload['relation_health_status']}",
            "safe_fix_recommendations:",
            *_line_list(payload["safe_fix_recommendations"]),
            "unsafe_actions_not_taken:",
            *_line_list(payload["unsafe_actions_not_taken"]),
            "final_recommendation: NO LIVE",
            END,
        ]
        return "\n".join(lines)

    def _orphan_path_metrics(self, since: str) -> dict[str, Any]:
        if not (_table_exists(self.db, "signal_path_metrics") and _table_exists(self.db, "signal_observations")):
            return {"total": 0, "classification": {}, "examples": [], "without_label": 0, "without_observation": 0}
        rows = _fetch_rows(
            self.db,
            """
            SELECT spm.observation_id, spm.symbol, spm.side, spm.source, spm.probe_key, spm.created_at, spm.status
            FROM signal_path_metrics spm
            LEFT JOIN signal_observations so ON so.id = spm.observation_id
            WHERE spm.created_at >= ?
              AND spm.observation_id IS NOT NULL
              AND so.id IS NULL
            LIMIT 500
            """,
            (since,),
        )
        classification = Counter(_classify_orphan(row) for row in rows)
        without_label = _scalar(
            self.db,
            """
            SELECT COUNT(*) AS count
            FROM signal_path_metrics spm
            LEFT JOIN signal_labels sl ON sl.observation_id = spm.observation_id
            WHERE spm.created_at >= ?
              AND sl.id IS NULL
            """,
            (since,),
            default=0,
        ) if _table_exists(self.db, "signal_labels") else 0
        examples = [
            f"obs={row.get('observation_id')} source={row.get('source')} class={_classify_orphan(row)} symbol={row.get('symbol')}"
            for row in rows[:10]
        ]
        return {
            "total": len(rows),
            "classification": dict(classification),
            "examples": examples,
            "without_label": safe_int(without_label),
            "without_observation": len(rows),
        }

    def _orphan_labels(self, since: str) -> int:
        if not (_table_exists(self.db, "signal_labels") and _table_exists(self.db, "signal_observations")):
            return 0
        return safe_int(_scalar(
            self.db,
            """
            SELECT COUNT(*) AS count
            FROM signal_labels sl
            LEFT JOIN signal_observations so ON so.id = sl.observation_id
            WHERE sl.timestamp >= ?
              AND so.id IS NULL
            """,
            (since,),
            default=0,
        ))

    def _duplicated_labels(self) -> dict[str, int]:
        if not _table_exists(self.db, "signal_labels"):
            return {"duplicated_labels": 0, "conflicting_labels": 0}
        duplicated = safe_int(_scalar(
            self.db,
            """
            SELECT COALESCE(SUM(c - 1), 0) AS count
            FROM (
                SELECT observation_id, COUNT(*) AS c
                FROM signal_labels
                GROUP BY observation_id
                HAVING COUNT(*) > 1
            ) x
            """,
            default=0,
        ))
        conflicting = safe_int(_scalar(
            self.db,
            """
            SELECT COUNT(*) AS count
            FROM (
                SELECT observation_id, COUNT(DISTINCT COALESCE(first_barrier_hit, '')) AS outcomes
                FROM signal_labels
                GROUP BY observation_id
                HAVING COUNT(*) > 1 AND COUNT(DISTINCT COALESCE(first_barrier_hit, '')) > 1
            ) x
            """,
            default=0,
        ))
        return {"duplicated_labels": duplicated, "conflicting_labels": conflicting}

    def _mismatches(self, since: str) -> dict[str, int]:
        if not (_table_exists(self.db, "signal_labels") and _table_exists(self.db, "signal_path_metrics")):
            return {"timestamp_mismatch_count": 0, "source_mismatch_count": 0, "labels_with_compatible_path_metric": 0}
        compatible = safe_int(_scalar(
            self.db,
            """
            SELECT COUNT(*) AS count
            FROM signal_labels sl
            JOIN signal_path_metrics spm ON spm.observation_id = sl.observation_id
            WHERE sl.timestamp >= ?
            """,
            (since,),
            default=0,
        ))
        timestamp_mismatch = 0
        if {"created_at", "matured_at", "observation_id"}.issubset(_columns(self.db, "signal_path_metrics")):
            timestamp_mismatch = safe_int(_scalar(
                self.db,
                """
                SELECT COUNT(*) AS count
                FROM signal_labels sl
                JOIN signal_path_metrics spm ON spm.observation_id = sl.observation_id
                WHERE sl.timestamp >= ?
                  AND spm.matured_at IS NOT NULL
                  AND substr(sl.timestamp, 1, 10) != substr(spm.matured_at, 1, 10)
                """,
                (since,),
                default=0,
            ))
        source_mismatch = 0
        if "source" in _columns(self.db, "signal_path_metrics"):
            source_mismatch = safe_int(_scalar(
                self.db,
                """
                SELECT COUNT(*) AS count
                FROM signal_labels sl
                JOIN signal_path_metrics spm ON spm.observation_id = sl.observation_id
                WHERE sl.timestamp >= ?
                  AND spm.source = 'market_probe'
                """,
                (since,),
                default=0,
            ))
        return {
            "timestamp_mismatch_count": timestamp_mismatch,
            "source_mismatch_count": source_mismatch,
            "labels_with_compatible_path_metric": compatible,
        }

    def _old_unlabeled_observations(self) -> int:
        if not (_table_exists(self.db, "signal_observations") and _table_exists(self.db, "signal_labels")):
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max(2, safe_int(getattr(self.config, "label_horizon_hours", 6), 6)))).isoformat()
        return safe_int(_scalar(
            self.db,
            """
            SELECT COUNT(*) AS count
            FROM signal_observations so
            LEFT JOIN signal_labels sl ON sl.observation_id = so.id
            WHERE sl.id IS NULL
              AND so.timestamp < ?
              AND so.side IN ('LONG', 'SHORT')
            """,
            (cutoff,),
            default=0,
        ))


class RelationRepairAuditSmokeTest:
    def __init__(self, config: Any, db: Any | None = None, logger: Any | None = None) -> None:
        self.config = config

    def to_text(self) -> str:
        db = _RelationSmokeDb()
        db.initialize()
        payload = RelationRepairAudit(self.config, db).build(hours=24)
        passed = (
            payload["orphan_path_metrics_total"] > 0
            and payload["conflicting_labels"] > 0
            and payload["relation_health_status"] in {"WARNING", "BAD"}
            and payload["unsafe_actions_not_taken"]
            and payload["final_recommendation"] == "NO LIVE"
        )
        return "\n".join([
            "RELATION REPAIR AUDIT SMOKE TEST START",
            f"orphan_path_metric_detected: {str(payload['orphan_path_metrics_total'] > 0).lower()}",
            f"conflicting_label_detected: {str(payload['conflicting_labels'] > 0).lower()}",
            f"safe_fix_recommendations_present: {str(bool(payload['safe_fix_recommendations'])).lower()}",
            "unsafe_actions_not_taken: true",
            "LIVE_TRADING=false",
            "DRY_RUN=true",
            "PAPER_TRADING=true",
            f"result: {'PASS' if passed else 'FAIL'}",
            "final_recommendation: NO LIVE",
            "RELATION REPAIR AUDIT SMOKE TEST END",
        ])


def _classify_orphan(row: dict[str, Any]) -> str:
    source = str(row.get("source") or "").lower()
    obs = safe_int(row.get("observation_id"))
    if source == "market_probe" or str(row.get("probe_key") or "").startswith("market_probe"):
        return "EXPECTED_RESEARCH_METRIC"
    if source in {"low_score_reject", "edge_guard_block", "edge_guard_shadow", "edge_guard_watch"}:
        return "EXPECTED_RESEARCH_METRIC"
    if obs >= 1_000_000_000:
        return "MISSING_FOREIGN_KEY_ONLY"
    created = str(row.get("created_at") or "")
    if created and created[:4].isdigit() and int(created[:4]) < 2026:
        return "LEGACY_DATA"
    return "REAL_ORPHAN_DANGEROUS"


def _relation_status(orphan_paths: dict[str, Any], orphan_labels: int, duplicated: dict[str, int], mismatches: dict[str, int], old_unlabeled: int) -> str:
    dangerous = safe_int(orphan_paths.get("classification", {}).get("REAL_ORPHAN_DANGEROUS")) + safe_int(orphan_labels) + safe_int(duplicated.get("conflicting_labels"))
    if dangerous > 0:
        return "BAD"
    if safe_int(orphan_paths.get("total")) or safe_int(duplicated.get("duplicated_labels")) or safe_int(old_unlabeled) or safe_int(mismatches.get("timestamp_mismatch_count")):
        return "WARNING"
    return "OK"


def _safe_recommendations(status: str, orphan_paths: dict[str, Any], duplicated: dict[str, int]) -> list[str]:
    recs = []
    if safe_int(orphan_paths.get("total")):
        recs.append("build_logical_relation_resolver_read_only")
        recs.append("classify_legacy_and_research_metrics_before_cleanup")
    if safe_int(duplicated.get("duplicated_labels")):
        recs.append("add_duplicate_label_detector_before_insert")
    if status != "OK":
        recs.append("do_not_delete_or_backfill_without_manual_review")
    return recs or ["keep_monitoring"]


def _line_list(items: Any) -> list[str]:
    rows = list(items or [])
    return [f"- {item}" for item in rows] if rows else ["- none"]


class _RelationSmokeDb:
    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self._use_postgres = False

    @contextmanager
    def _connect(self) -> Iterator[Any]:
        yield self.conn
        self.conn.commit()

    def initialize(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE signal_observations(id INTEGER PRIMARY KEY, timestamp TEXT, symbol TEXT, side TEXT);
                CREATE TABLE signal_labels(id INTEGER PRIMARY KEY, timestamp TEXT, observation_id INTEGER, first_barrier_hit TEXT);
                CREATE TABLE signal_path_metrics(id INTEGER PRIMARY KEY, observation_id INTEGER, symbol TEXT, side TEXT, source TEXT, probe_key TEXT, status TEXT, created_at TEXT, matured_at TEXT);
                """
            )
            conn.execute("INSERT INTO signal_observations VALUES (?, ?, ?, ?)", (1, now, "ETHUSDT", "SHORT"))
            conn.execute("INSERT INTO signal_labels VALUES (?, ?, ?, ?)", (1, now, 1, "TP1"))
            conn.execute("INSERT INTO signal_labels VALUES (?, ?, ?, ?)", (2, now, 1, "SL"))
            conn.execute("INSERT INTO signal_path_metrics VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (1, 1, "ETHUSDT", "SHORT", "trade_signal", "", "matured", now, now))
            conn.execute("INSERT INTO signal_path_metrics VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (2, 999999, "BTCUSDT", "LONG", "trade_signal", "", "matured", now, now))
            conn.execute("INSERT INTO signal_path_metrics VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (3, 1000000001, "SOLUSDT", "LONG", "market_probe", "market_probe:SOLUSDT", "matured", now, now))

    def _fetchall_dicts(self, cursor: Any) -> list[dict[str, Any]]:
        return [dict(row) for row in cursor.fetchall()]

    def table_exists(self, table: str) -> bool:
        return self.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None

    def get_table_columns(self, table: str) -> list[str]:
        return [row[1] for row in self.conn.execute(f"PRAGMA table_info({table})").fetchall()]
