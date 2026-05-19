from __future__ import annotations

import sqlite3
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from .training_data_integrity import _columns, _scalar, _table_exists, _use_postgres
from .utils import safe_float, safe_int


START = "DATA PIPELINE DIAGNOSIS START"
END = "DATA PIPELINE DIAGNOSIS END"


class DataPipelineDiagnosis:
    """Read-only duplicate diagnosis with real/benign/false-positive separation."""

    def __init__(self, config: Any, db: Any) -> None:
        self.config = config
        self.db = db

    def build(self, *, hours: int = 24) -> dict[str, Any]:
        hours = max(1, int(hours or 24))
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        exact = self._exact_duplicates(since)
        benign = self._benign_density(since)
        false_positive = self._false_positive_estimate(exact, benign)
        diagnosis = _diagnosis(exact, benign, false_positive)
        return {
            "hours": hours,
            **exact,
            **benign,
            **false_positive,
            "diagnosis": diagnosis["diagnosis"],
            "recommended_action": diagnosis["recommended_action"],
            "final_recommendation": "NO LIVE",
        }

    def to_text(self, *, hours: int = 24) -> str:
        payload = self.build(hours=hours)
        lines = [
            START,
            f"hours: {payload['hours']}",
            f"exact_duplicate_count: {payload['exact_duplicate_count']}",
            f"exact_duplicate_rate: {payload['exact_duplicate_rate']:.6f}",
            f"observation_id_duplicates: {payload['observation_id_duplicates']}",
            f"label_id_duplicates: {payload['label_id_duplicates']}",
            f"path_metric_id_duplicates: {payload['path_metric_id_duplicates']}",
            f"labels_per_observation_duplicates: {payload['labels_per_observation_duplicates']}",
            f"conflicting_labels: {payload['conflicting_labels']}",
            f"multiple_paper_closes: {payload['multiple_paper_closes']}",
            f"dangerous_duplicate_status: {payload['dangerous_duplicate_status']}",
            f"benign_minute_bucket_density: {payload['benign_minute_bucket_density']:.2f}",
            f"suspicious_minute_bucket_density: {payload['suspicious_minute_bucket_density']:.2f}",
            f"benign_duplicate_status: {payload['benign_duplicate_status']}",
            f"false_positive_duplicate_estimate: {payload['false_positive_duplicate_estimate']:.2f}",
            f"audit_false_positive_status: {payload['audit_false_positive_status']}",
            "top_real_duplicate_examples_sanitized:",
            *_line_list(payload.get("top_real_duplicate_examples_sanitized")),
            "top_benign_density_examples:",
            *_line_list(payload.get("top_benign_density_examples")),
            f"diagnosis: {payload['diagnosis']}",
            f"recommended_action: {payload['recommended_action']}",
            "final_recommendation: NO LIVE",
            END,
        ]
        return "\n".join(lines)

    def _exact_duplicates(self, since: str) -> dict[str, Any]:
        total = _scalar(self.db, "SELECT COUNT(*) AS count FROM signal_observations", default=0) if _table_exists(self.db, "signal_observations") else 0
        observation_id_duplicates = _duplicate_id_count(self.db, "signal_observations")
        label_id_duplicates = _duplicate_id_count(self.db, "signal_labels")
        path_metric_id_duplicates = _duplicate_id_count(self.db, "signal_path_metrics")
        labels_per_observation = _group_duplicate_count(self.db, "signal_labels", "observation_id")
        path_per_observation = _group_duplicate_count(self.db, "signal_path_metrics", "observation_id")
        conflicting_labels = _conflicting_labels(self.db)
        multiple_paper_closes = _multiple_paper_closes(self.db)
        observation_exact = _observation_exact_duplicate_count(self.db, since)
        exact_count = (
            observation_id_duplicates
            + label_id_duplicates
            + path_metric_id_duplicates
            + labels_per_observation
            + path_per_observation
            + conflicting_labels
            + multiple_paper_closes
            + observation_exact
        )
        rate = exact_count / max(safe_int(total), 1)
        examples = []
        for label, count in (
            ("observation_id", observation_id_duplicates),
            ("label_id", label_id_duplicates),
            ("path_metric_id", path_metric_id_duplicates),
            ("labels_per_observation", labels_per_observation),
            ("path_metrics_per_observation", path_per_observation),
            ("conflicting_labels", conflicting_labels),
            ("multiple_paper_closes", multiple_paper_closes),
            ("exact_observation_fingerprint", observation_exact),
        ):
            if count:
                examples.append(f"{label} count={count}")
        status = "BAD" if conflicting_labels or labels_per_observation or rate > 0.02 else "WARNING" if exact_count else "OK"
        return {
            "exact_duplicate_count": safe_int(exact_count),
            "exact_duplicate_rate": rate,
            "observation_id_duplicates": safe_int(observation_id_duplicates),
            "label_id_duplicates": safe_int(label_id_duplicates),
            "path_metric_id_duplicates": safe_int(path_metric_id_duplicates),
            "labels_per_observation_duplicates": safe_int(labels_per_observation),
            "path_metrics_per_observation_duplicates": safe_int(path_per_observation),
            "conflicting_labels": safe_int(conflicting_labels),
            "multiple_paper_closes": safe_int(multiple_paper_closes),
            "dangerous_duplicate_status": status,
            "top_real_duplicate_examples_sanitized": examples[:12],
        }

    def _benign_density(self, since: str) -> dict[str, Any]:
        if not _table_exists(self.db, "signal_observations"):
            return {
                "benign_minute_bucket_density": 0.0,
                "suspicious_minute_bucket_density": 0.0,
                "benign_duplicate_status": "OK",
                "top_benign_density_examples": [],
            }
        rows = _fetch_rows(
            self.db,
            """
            SELECT substr(timestamp, 1, 16) AS bucket,
                   COUNT(*) AS total,
                   COUNT(DISTINCT COALESCE(symbol, '') || '|' || COALESCE(side, '') || '|' || COALESCE(score_bucket, '') || '|' || COALESCE(market_regime, '')) AS distinct_contexts
            FROM signal_observations
            WHERE timestamp >= ?
            GROUP BY bucket
            ORDER BY total DESC
            LIMIT 20
            """,
            (since,),
        )
        densities = [safe_int(row.get("total")) for row in rows]
        benign = sum(1 for row in rows if safe_int(row.get("total")) > 1 and safe_int(row.get("distinct_contexts")) > 1)
        suspicious = sum(1 for row in rows if safe_int(row.get("total")) > 5 and safe_int(row.get("distinct_contexts")) <= 1)
        avg_density = sum(densities) / max(len(densities), 1)
        status = "HIGH_BUT_EXPECTED" if benign and not suspicious else "WARNING" if suspicious else "OK"
        examples = [
            f"bucket={row.get('bucket')} total={safe_int(row.get('total'))} distinct_contexts={safe_int(row.get('distinct_contexts'))}"
            for row in rows[:8]
        ]
        return {
            "benign_minute_bucket_density": avg_density,
            "suspicious_minute_bucket_density": float(suspicious),
            "benign_duplicate_status": status,
            "top_benign_density_examples": examples,
        }

    @staticmethod
    def _false_positive_estimate(exact: dict[str, Any], benign: dict[str, Any]) -> dict[str, Any]:
        benign_density = safe_float(benign.get("benign_minute_bucket_density"))
        exact_rate = safe_float(exact.get("exact_duplicate_rate"))
        estimate = 0.0
        if benign_density > 10 and exact_rate < 0.01:
            estimate = min(1.0, benign_density / 100.0)
        elif benign.get("benign_duplicate_status") == "HIGH_BUT_EXPECTED" and exact.get("dangerous_duplicate_status") == "OK":
            estimate = 0.75
        status = "WARNING" if estimate >= 0.5 else "OK"
        return {"false_positive_duplicate_estimate": estimate, "audit_false_positive_status": status}


class DataPipelineDiagnosisSmokeTest:
    def __init__(self, config: Any, db: Any | None = None, logger: Any | None = None) -> None:
        self.config = config

    def to_text(self) -> str:
        db = _PipelineSmokeDb()
        db.initialize()
        payload = DataPipelineDiagnosis(self.config, db).build(hours=24)
        passed = (
            payload["dangerous_duplicate_status"] in {"WARNING", "BAD"}
            and payload["benign_duplicate_status"] in {"OK", "HIGH_BUT_EXPECTED", "WARNING"}
            and payload["audit_false_positive_status"] in {"OK", "WARNING"}
            and payload["conflicting_labels"] > 0
            and payload["final_recommendation"] == "NO LIVE"
        )
        lines = [
            "DATA PIPELINE DIAGNOSIS SMOKE TEST START",
            f"real_duplicate_detected: {str(payload['dangerous_duplicate_status'] in {'WARNING', 'BAD'}).lower()}",
            f"benign_density_detected: {str(payload['benign_minute_bucket_density'] > 0).lower()}",
            f"false_positive_bucket_checked: {str(bool(payload['audit_false_positive_status'])).lower()}",
            f"conflicting_labels_detected: {str(payload['conflicting_labels'] > 0).lower()}",
            "LIVE_TRADING=false",
            "DRY_RUN=true",
            "PAPER_TRADING=true",
            f"result: {'PASS' if passed else 'FAIL'}",
            "final_recommendation: NO LIVE",
            "DATA PIPELINE DIAGNOSIS SMOKE TEST END",
        ]
        return "\n".join(lines)


def _diagnosis(exact: dict[str, Any], benign: dict[str, Any], false_positive: dict[str, Any]) -> dict[str, str]:
    dangerous = str(exact.get("dangerous_duplicate_status"))
    if dangerous == "BAD":
        return {"diagnosis": "dangerous_duplicates_need_review", "recommended_action": "FIX_DATA_PIPELINE"}
    if false_positive.get("audit_false_positive_status") == "WARNING":
        return {"diagnosis": "previous_duplicate_audit_likely_had_false_positives", "recommended_action": "REFINE_AUDIT_BUCKETS"}
    if benign.get("benign_duplicate_status") == "HIGH_BUT_EXPECTED":
        return {"diagnosis": "normal_multi_symbol_cycle_density", "recommended_action": "KEEP_RESEARCH"}
    if dangerous == "WARNING":
        return {"diagnosis": "minor_duplicate_risk", "recommended_action": "REVIEW_DATA_PIPELINE"}
    return {"diagnosis": "no_dangerous_duplicates_detected", "recommended_action": "KEEP_RESEARCH"}


def _duplicate_id_count(db: Any, table: str) -> int:
    if not _table_exists(db, table) or "id" not in _columns(db, table):
        return 0
    return safe_int(_scalar(
        db,
        f"""
        SELECT COALESCE(SUM(c - 1), 0) AS count
        FROM (
            SELECT id, COUNT(*) AS c
            FROM {table}
            GROUP BY id
            HAVING COUNT(*) > 1
        ) x
        """,
        default=0,
    ))


def _group_duplicate_count(db: Any, table: str, column: str) -> int:
    if not _table_exists(db, table) or column not in _columns(db, table):
        return 0
    return safe_int(_scalar(
        db,
        f"""
        SELECT COALESCE(SUM(c - 1), 0) AS count
        FROM (
            SELECT {column}, COUNT(*) AS c
            FROM {table}
            WHERE {column} IS NOT NULL
            GROUP BY {column}
            HAVING COUNT(*) > 1
        ) x
        """,
        default=0,
    ))


def _conflicting_labels(db: Any) -> int:
    if not _table_exists(db, "signal_labels") or "observation_id" not in _columns(db, "signal_labels"):
        return 0
    return safe_int(_scalar(
        db,
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


def _multiple_paper_closes(db: Any) -> int:
    if not _table_exists(db, "trades") or "trade_id" not in _columns(db, "trades"):
        return 0
    status_col = "status" if "status" in _columns(db, "trades") else ""
    if not status_col:
        return 0
    return safe_int(_scalar(
        db,
        """
        SELECT COALESCE(SUM(c - 1), 0) AS count
        FROM (
            SELECT trade_id, COUNT(*) AS c
            FROM trades
            WHERE trade_id IS NOT NULL
              AND status NOT IN ('PAPER_OPEN', 'OPEN', 'PAPER_READY')
            GROUP BY trade_id
            HAVING COUNT(*) > 1
        ) x
        """,
        default=0,
    ))


def _observation_exact_duplicate_count(db: Any, since: str) -> int:
    if not _table_exists(db, "signal_observations"):
        return 0
    cols = _columns(db, "signal_observations")
    required = {"timestamp", "symbol", "side", "confidence_score", "market_regime"}
    if not required.issubset(cols):
        return 0
    optional = []
    for column in ("score_bucket", "block_reason", "strategy_type"):
        optional.append(f"COALESCE({column}, '')" if column in cols else "''")
    exprs = ["timestamp", "COALESCE(symbol, '')", "COALESCE(side, '')", "COALESCE(confidence_score, 0)", "COALESCE(market_regime, '')", *optional]
    group = ", ".join(exprs)
    return safe_int(_scalar(
        db,
        f"""
        SELECT COALESCE(SUM(c - 1), 0) AS count
        FROM (
            SELECT {group}, COUNT(*) AS c
            FROM signal_observations
            WHERE timestamp >= ?
            GROUP BY {group}
            HAVING COUNT(*) > 1
        ) x
        """,
        (since,),
        default=0,
    ))


def _fetch_rows(db: Any, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    try:
        if _use_postgres(db):
            sql = sql.replace("?", "%s")
        with db._connect() as conn:
            cur = conn.execute(sql, params)
            if hasattr(db, "_fetchall_dicts"):
                return db._fetchall_dicts(cur)
            return [dict(row) for row in cur.fetchall()]
    except Exception:
        return []


def _line_list(items: Any) -> list[str]:
    rows = list(items or [])
    return [f"- {item}" for item in rows] if rows else ["- none"]


class _PipelineSmokeDb:
    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self._use_postgres = False

    @contextmanager
    def _connect(self) -> Iterator[Any]:
        yield self.conn
        self.conn.commit()

    def initialize(self) -> None:
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0).isoformat()
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE signal_observations(id INTEGER, timestamp TEXT, symbol TEXT, side TEXT, confidence_score INTEGER, market_regime TEXT, score_bucket TEXT, block_reason TEXT, strategy_type TEXT);
                CREATE TABLE signal_labels(id INTEGER, timestamp TEXT, observation_id INTEGER, label INTEGER, first_barrier_hit TEXT, realized_return_pct REAL);
                CREATE TABLE signal_path_metrics(id INTEGER, observation_id INTEGER, source TEXT, created_at TEXT);
                CREATE TABLE trades(id INTEGER, trade_id TEXT, status TEXT);
                """
            )
            for i in range(12):
                conn.execute("INSERT INTO signal_observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (i + 1, now, f"SYM{i % 4}", "SHORT", 90, "RISK_OFF", "90-94", "", "trend"))
            conn.execute("INSERT INTO signal_observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (100, now, "ETHUSDT", "SHORT", 90, "RISK_OFF", "90-94", "", "trend"))
            conn.execute("INSERT INTO signal_observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (101, now, "ETHUSDT", "SHORT", 90, "RISK_OFF", "90-94", "", "trend"))
            conn.execute("INSERT INTO signal_labels VALUES (?, ?, ?, ?, ?, ?)", (1, now, 100, 1, "TP1", 0.5))
            conn.execute("INSERT INTO signal_labels VALUES (?, ?, ?, ?, ?, ?)", (2, now, 100, -1, "SL", -0.5))
            conn.execute("INSERT INTO signal_path_metrics VALUES (?, ?, ?, ?)", (1, 100, "trade_signal", now))
            conn.execute("INSERT INTO signal_path_metrics VALUES (?, ?, ?, ?)", (2, 100, "trade_signal", now))
            conn.execute("INSERT INTO trades VALUES (?, ?, ?)", (1, "paper-1", "PAPER_CLOSED_TP"))
            conn.execute("INSERT INTO trades VALUES (?, ?, ?)", (2, "paper-1", "PAPER_CLOSED_SL"))

    def _fetchall_dicts(self, cursor: Any) -> list[dict[str, Any]]:
        return [dict(row) for row in cursor.fetchall()]

    def table_exists(self, table: str) -> bool:
        return self.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None

    def get_table_columns(self, table: str) -> list[str]:
        return [row[1] for row in self.conn.execute(f"PRAGMA table_info({table})").fetchall()]
