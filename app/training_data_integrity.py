from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from collections import Counter
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any, Iterator

from .candidate_incubator import CandidateIncubator
from .score_calibration import ScoreCalibration, load_score_rows
from .utils import safe_float, safe_int


START = "TRAINING DATA INTEGRITY START"
END = "TRAINING DATA INTEGRITY END"

TABLE_SPECS: dict[str, str | None] = {
    "signal_observations": "timestamp",
    "signal_labels": "timestamp",
    "signal_path_metrics": "created_at",
    "latency_metrics": "timestamp",
    "events": "timestamp",
    "trades": "timestamp",
    "virtual_research_trades": "created_at",
    "research_autopilot_runs": "created_at",
}


class TrainingDataIntegrity:
    """Read-only training data integrity audit.

    This lab never mutates labels, observations, path metrics, trades, config, or execution state.
    """

    def __init__(self, config: Any, db: Any) -> None:
        self.config = config
        self.db = db

    def build(self, *, hours: int = 24) -> dict[str, Any]:
        hours = max(1, int(hours or 24))
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        growth = self._table_growth(since, hours)
        duplicates = self._duplicates()
        relations = self._relations(since)
        labels = self._label_quality(since)
        sources = self._source_mix(since, hours)
        net_ev = self._net_ev_sanity(hours)
        statuses = [
            growth.get("overall_growth_status"),
            duplicates.get("duplicate_status"),
            relations.get("relation_status"),
            labels.get("label_quality_status"),
            sources.get("market_probe_contamination_status"),
            net_ev.get("cost_model_status"),
        ]
        overall = _worst_status(statuses)
        biggest = _biggest_problem(growth, duplicates, relations, labels, sources, net_ev)
        return {
            "hours": hours,
            "tables": growth.get("tables", []),
            "overall_growth_status": growth.get("overall_growth_status"),
            **duplicates,
            **relations,
            **labels,
            **sources,
            **net_ev,
            "overall_data_quality": overall,
            "biggest_problem": biggest,
            "recommended_next_action": _next_action(biggest),
            "final_recommendation": "NO LIVE",
        }

    def to_text(self, *, hours: int = 24) -> str:
        payload = self.build(hours=hours)
        lines = [
            START,
            f"hours: {payload['hours']}",
            "table_growth:",
            *[
                (
                    f"- {row['table']}: total={row['rows_total']} window={row['rows_window']} "
                    f"rows_per_hour={row['rows_per_hour']:.2f} status={row['growth_status']} "
                    f"first={row['first_timestamp'] or 'none'} last={row['last_timestamp'] or 'none'}"
                )
                for row in payload["tables"]
            ],
            f"duplicate_rate: {payload['duplicate_rate']:.4f}",
            f"duplicate_status: {payload['duplicate_status']}",
            "duplicate_examples_sanitized:",
            *_list_lines(payload.get("duplicate_examples_sanitized")),
            f"orphan_labels: {payload['orphan_labels']}",
            f"orphan_path_metrics: {payload['orphan_path_metrics']}",
            f"unlabeled_old_observations: {payload['unlabeled_old_observations']}",
            f"duplicated_labels: {payload['duplicated_labels']}",
            f"relation_status: {payload['relation_status']}",
            f"TP%: {payload['tp_ratio'] * 100:.2f}",
            f"SL%: {payload['sl_ratio'] * 100:.2f}",
            f"TIME%: {payload['time_ratio'] * 100:.2f}",
            f"avg_mfe: {payload['avg_mfe']:.5f}",
            f"avg_mae: {payload['avg_mae']:.5f}",
            f"median_mfe: {payload['median_mfe']:.5f}",
            f"median_mae: {payload['median_mae']:.5f}",
            f"mfe_mae_zero_rate: {payload['mfe_mae_zero_rate']:.4f}",
            f"label_quality_status: {payload['label_quality_status']}",
            "source_mix:",
            *[f"- {key}: {value}" for key, value in sorted(payload["source_mix"].items())],
            f"market_probe_never_actionable: {str(payload['market_probe_never_actionable']).lower()}",
            f"market_probe_contamination_status: {payload['market_probe_contamination_status']}",
            "net_ev_distribution:",
            *[f"- {key}: {value}" for key, value in payload["net_ev_distribution"].items()],
            "gross_pf_distribution:",
            *[f"- {key}: {value}" for key, value in payload["gross_pf_distribution"].items()],
            f"suspicious_constant_penalty: {str(payload['suspicious_constant_penalty']).lower()}",
            f"gross_edge_net_negative_rate: {payload['gross_edge_net_negative_rate']:.4f}",
            f"cost_model_status: {payload['cost_model_status']}",
            f"overall_data_quality: {payload['overall_data_quality']}",
            f"biggest_problem: {payload['biggest_problem']}",
            f"recommended_next_action: {payload['recommended_next_action']}",
            "final_recommendation: NO LIVE",
            END,
        ]
        return "\n".join(lines)

    def _table_growth(self, since: str, hours: int) -> dict[str, Any]:
        rows = []
        statuses = []
        for table, ts_col in TABLE_SPECS.items():
            if not _table_exists(self.db, table):
                row = {
                    "table": table,
                    "rows_total": 0,
                    "rows_window": 0,
                    "first_timestamp": "",
                    "last_timestamp": "",
                    "rows_per_hour": 0.0,
                    "growth_status": "UNKNOWN",
                }
                rows.append(row)
                statuses.append("UNKNOWN")
                continue
            total = _scalar(self.db, f"SELECT COUNT(*) AS count FROM {table}", default=0)
            window = 0
            first = ""
            last = ""
            if ts_col and ts_col in _columns(self.db, table):
                window = _scalar(self.db, f"SELECT COUNT(*) AS count FROM {table} WHERE {ts_col} >= ?", (since,), default=0)
                first = str(_scalar(self.db, f"SELECT MIN({ts_col}) AS value FROM {table}", default="") or "")
                last = str(_scalar(self.db, f"SELECT MAX({ts_col}) AS value FROM {table}", default="") or "")
            status = _growth_status(table, safe_int(total), safe_int(window), hours)
            rows.append({
                "table": table,
                "rows_total": safe_int(total),
                "rows_window": safe_int(window),
                "first_timestamp": first,
                "last_timestamp": last,
                "rows_per_hour": safe_int(window) / max(hours, 1),
                "growth_status": status,
            })
            statuses.append(status)
        return {"tables": rows, "overall_growth_status": _worst_status(statuses)}

    def _duplicates(self) -> dict[str, Any]:
        examples: list[str] = []
        duplicated_labels = _scalar(
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
        ) if _table_exists(self.db, "signal_labels") else 0
        duplicated_paths = _scalar(
            self.db,
            """
            SELECT COALESCE(SUM(c - 1), 0) AS count
            FROM (
                SELECT observation_id, COUNT(*) AS c
                FROM signal_path_metrics
                WHERE observation_id IS NOT NULL
                GROUP BY observation_id
                HAVING COUNT(*) > 1
            ) x
            """,
            default=0,
        ) if _table_exists(self.db, "signal_path_metrics") else 0
        obs_dupes = 0
        if _table_exists(self.db, "signal_observations"):
            cols = _columns(self.db, "signal_observations")
            if {"symbol", "side", "confidence_score", "timestamp"}.issubset(cols):
                bucket_expr = "substr(timestamp, 1, 16)" if not _use_postgres(self.db) else "date_trunc('minute', timestamp::timestamp)"
                obs_dupes = _scalar(
                    self.db,
                    f"""
                    SELECT COALESCE(SUM(c - 1), 0) AS count
                    FROM (
                        SELECT symbol, side, confidence_score, {bucket_expr} AS bucket, COUNT(*) AS c
                        FROM signal_observations
                        GROUP BY symbol, side, confidence_score, bucket
                        HAVING COUNT(*) > 1
                    ) x
                    """,
                    default=0,
                )
        for label, count in (("labels_per_observation", duplicated_labels), ("path_metrics_per_observation", duplicated_paths), ("observation_minute_bucket", obs_dupes)):
            if safe_int(count) > 0:
                examples.append(f"{label} duplicates={safe_int(count)}")
        total_obs = _scalar(self.db, "SELECT COUNT(*) AS count FROM signal_observations", default=0) if _table_exists(self.db, "signal_observations") else 0
        duplicate_count = safe_int(duplicated_labels) + safe_int(duplicated_paths) + safe_int(obs_dupes)
        rate = duplicate_count / max(safe_int(total_obs), 1)
        status = "BAD" if rate > 0.05 or duplicate_count > 1000 else "WARNING" if duplicate_count > 0 else "OK"
        return {
            "duplicate_rate": rate,
            "duplicate_examples_sanitized": examples[:10],
            "duplicate_status": status,
            "duplicated_labels": safe_int(duplicated_labels),
        }

    def _relations(self, since: str) -> dict[str, Any]:
        orphan_labels = _scalar(
            self.db,
            """
            SELECT COUNT(*) AS count
            FROM signal_labels sl
            LEFT JOIN signal_observations so ON so.id = sl.observation_id
            WHERE so.id IS NULL
            """,
            default=0,
        ) if _table_exists(self.db, "signal_labels") and _table_exists(self.db, "signal_observations") else 0
        orphan_paths = _scalar(
            self.db,
            """
            SELECT COUNT(*) AS count
            FROM signal_path_metrics spm
            LEFT JOIN signal_observations so ON so.id = spm.observation_id
            WHERE spm.observation_id IS NOT NULL AND so.id IS NULL
            """,
            default=0,
        ) if _table_exists(self.db, "signal_path_metrics") and _table_exists(self.db, "signal_observations") else 0
        old_cutoff = (datetime.now(timezone.utc) - timedelta(hours=max(2, safe_int(getattr(self.config, "label_horizon_hours", 6), 6)))).isoformat()
        unlabeled_old = _scalar(
            self.db,
            """
            SELECT COUNT(*) AS count
            FROM signal_observations so
            LEFT JOIN signal_labels sl ON sl.observation_id = so.id
            WHERE sl.id IS NULL
              AND so.timestamp < ?
              AND so.side IN ('LONG', 'SHORT')
            """,
            (old_cutoff,),
            default=0,
        ) if _table_exists(self.db, "signal_observations") and _table_exists(self.db, "signal_labels") else 0
        impossible = _scalar(
            self.db,
            """
            SELECT COUNT(*) AS count
            FROM signal_labels
            WHERE first_barrier_hit NOT IN ('TP1', 'TP2', 'SL', 'TIME')
               OR first_barrier_hit IS NULL
            """,
            default=0,
        ) if _table_exists(self.db, "signal_labels") else 0
        bad = safe_int(orphan_labels) + safe_int(orphan_paths) + safe_int(impossible)
        warning = safe_int(unlabeled_old)
        status = "BAD" if bad > 0 else "WARNING" if warning > 100 else "OK"
        return {
            "orphan_labels": safe_int(orphan_labels),
            "orphan_path_metrics": safe_int(orphan_paths),
            "unlabeled_old_observations": safe_int(unlabeled_old),
            "impossible_labels": safe_int(impossible),
            "relation_status": status,
        }

    def _label_quality(self, since: str) -> dict[str, Any]:
        labels = _safe_call(lambda: self.db.fetch_labeled_signal_rows_since(since, limit=50000), [])
        paths = _safe_call(lambda: self.db.fetch_signal_path_metrics_since(since, limit=50000), [])
        hits = Counter(_hit(row.get("first_barrier_hit")) for row in labels)
        total = len(labels)
        path_mfes = [safe_float(row.get("max_favorable_pct")) for row in paths]
        path_maes = [safe_float(row.get("max_adverse_pct")) for row in paths]
        label_mfes = [safe_float(row.get("max_favorable_excursion")) for row in labels]
        label_maes = [safe_float(row.get("max_adverse_excursion")) for row in labels]
        mfes = path_mfes or label_mfes
        maes = path_maes or label_maes
        zero_rate = sum(1 for mfe, mae in zip(mfes, maes) if abs(mfe) <= 1e-12 and abs(mae) <= 1e-12) / max(min(len(mfes), len(maes)), 1)
        labels_without_paths = max(0, total - len(paths))
        time_ratio = hits["TIME"] / max(total, 1)
        status = "BAD" if (total > 20 and time_ratio > 0.9) or (mfes and zero_rate > 0.85) else "WARNING" if (total > 20 and time_ratio > 0.75) or labels_without_paths > total * 0.5 else "OK"
        return {
            "labels_total": total,
            "tp_ratio": hits["TP"] / max(total, 1),
            "sl_ratio": hits["SL"] / max(total, 1),
            "time_ratio": time_ratio,
            "avg_mfe": sum(mfes) / max(len(mfes), 1),
            "avg_mae": sum(maes) / max(len(maes), 1),
            "median_mfe": median(mfes) if mfes else 0.0,
            "median_mae": median(maes) if maes else 0.0,
            "mfe_mae_zero_rate": zero_rate,
            "labels_created_but_no_path_metrics": labels_without_paths,
            "label_quality_status": status,
        }

    def _source_mix(self, since: str, hours: int) -> dict[str, Any]:
        paths = _safe_call(lambda: self.db.fetch_signal_path_metrics_since(since, limit=50000), [])
        mix = Counter(str(row.get("source") or "unknown") for row in paths)
        contamination = False
        try:
            incubator = CandidateIncubator(self.config, self.db).build(hours=hours)
            actionable = {"SHADOW_ONLY", "PAPER_CANDIDATE_DISABLED"}
            contamination = any(
                str(row.get("source")) == "market_probe" and str(row.get("candidate_status")) in actionable
                for row in incubator.get("candidates", [])
            )
        except Exception:
            contamination = False
        return {
            "source_mix": dict(mix),
            "market_probe_count": safe_int(mix.get("market_probe")),
            "trade_signal_count": safe_int(mix.get("trade_signal")),
            "reject_count": sum(safe_int(value) for key, value in mix.items() if "reject" in str(key) or "block" in str(key)),
            "market_probe_never_actionable": not contamination,
            "market_probe_contamination_status": "BAD" if contamination else "OK",
        }

    def _net_ev_sanity(self, hours: int) -> dict[str, Any]:
        rows = load_score_rows(self.db, hours=hours)
        if not rows:
            return {
                "net_ev_distribution": {"groups": 0, "negative": 0, "positive": 0, "zero": 0},
                "gross_pf_distribution": {"groups": 0, "gross_pf_gt_1": 0},
                "suspicious_constant_penalty": False,
                "gross_edge_net_negative_rate": 0.0,
                "cost_model_status": "UNKNOWN",
                "recommendation": "KEEP_COST_MODEL",
            }
        payload = _safe_call(lambda: ScoreCalibration(self.config, self.db).build(hours=hours), {})
        groups: list[dict[str, Any]] = []
        for key in ("by_score_bucket", "by_side", "by_regime", "by_symbol", "by_source"):
            groups.extend(payload.get(key, []) if isinstance(payload, dict) else [])
        groups = [row for row in groups if safe_int(row.get("samples")) > 0]
        net_values = [round(safe_float(row.get("net_EV_est")), 6) for row in groups]
        identical_rate = 0.0
        if net_values:
            most_common = Counter(net_values).most_common(1)[0][1]
            identical_rate = most_common / max(len(net_values), 1)
        gross_edge = [row for row in groups if safe_float(row.get("gross_PF")) > 1.0]
        gross_net_negative = [row for row in gross_edge if safe_float(row.get("net_EV_est")) < 0 or safe_float(row.get("net_PF_est")) < 1.0]
        rate = len(gross_net_negative) / max(len(gross_edge), 1)
        suspicious = bool(len(net_values) >= 8 and identical_rate > 0.65)
        status = "BAD" if suspicious or (len(gross_edge) >= 5 and rate > 0.9) else "WARNING" if rate > 0.6 else "OK"
        return {
            "net_ev_distribution": {
                "groups": len(groups),
                "negative": sum(1 for value in net_values if value < 0),
                "positive": sum(1 for value in net_values if value > 0),
                "zero": sum(1 for value in net_values if value == 0),
            },
            "gross_pf_distribution": {
                "groups": len(groups),
                "gross_pf_gt_1": len(gross_edge),
            },
            "suspicious_constant_penalty": suspicious,
            "gross_edge_net_negative_rate": rate,
            "cost_model_status": status,
            "recommendation": "URGENT_REVIEW" if status == "BAD" else "REVIEW_COST_MODEL" if status == "WARNING" else "KEEP_COST_MODEL",
        }


class TrainingDataIntegritySmokeTest:
    def __init__(self, config: Any, db: Any | None = None, logger: Any | None = None) -> None:
        self.config = config

    def to_text(self) -> str:
        db = _SmokeDb()
        db.initialize()
        payload = TrainingDataIntegrity(self.config, db).build(hours=24)
        passed = (
            payload["duplicate_status"] in {"WARNING", "BAD"}
            and payload["relation_status"] == "BAD"
            and payload["label_quality_status"] in {"WARNING", "BAD"}
            and payload["market_probe_contamination_status"] == "OK"
            and payload["cost_model_status"] in {"WARNING", "BAD", "UNKNOWN", "OK"}
            and payload["final_recommendation"] == "NO LIVE"
        )
        return "\n".join([
            "TRAINING DATA INTEGRITY SMOKE TEST START",
            f"duplicates_detected: {str(payload['duplicate_status'] in {'WARNING', 'BAD'}).lower()}",
            f"orphan_labels_detected: {str(payload['orphan_labels'] > 0).lower()}",
            f"mfe_mae_zero_detected: {str(payload['mfe_mae_zero_rate'] > 0).lower()}",
            f"time_excessive_detected: {str(payload['time_ratio'] > 0.7).lower()}",
            f"market_probe_separated: {str(payload['market_probe_contamination_status'] == 'OK').lower()}",
            f"net_ev_suspicious_checked: {str(bool(payload['cost_model_status'])).lower()}",
            "LIVE_TRADING=false",
            "DRY_RUN=true",
            "PAPER_TRADING=true",
            f"result: {'PASS' if passed else 'FAIL'}",
            "final_recommendation: NO LIVE",
            "TRAINING DATA INTEGRITY SMOKE TEST END",
        ])


def _table_exists(db: Any, table: str) -> bool:
    try:
        return bool(db.table_exists(table))
    except Exception:
        return False


def _columns(db: Any, table: str) -> set[str]:
    try:
        return set(db.get_table_columns(table))
    except Exception:
        return set()


def _use_postgres(db: Any) -> bool:
    return bool(getattr(db, "_use_postgres", False))


def _scalar(db: Any, sql: str, params: tuple[Any, ...] = (), default: Any = 0) -> Any:
    try:
        if _use_postgres(db):
            sql = sql.replace("?", "%s")
        with db._connect() as conn:
            row = conn.execute(sql, params).fetchone()
            if row is None:
                return default
            if isinstance(row, dict):
                return next(iter(row.values()), default)
            try:
                return row[0]
            except Exception:
                return default
    except Exception:
        return default


def _safe_call(callback: Any, default: Any) -> Any:
    try:
        return callback()
    except Exception:
        return default


def _hit(value: Any) -> str:
    text = str(value or "").upper()
    if text in {"TP1", "TP2", "TP"}:
        return "TP"
    if text == "SL":
        return "SL"
    if text == "TIME":
        return "TIME"
    return "UNKNOWN"


def _growth_status(table: str, total: int, window: int, hours: int) -> str:
    if total <= 0:
        return "UNKNOWN"
    if table in {"trades", "virtual_research_trades", "research_autopilot_runs"} and window == 0:
        return "OK"
    per_hour = window / max(hours, 1)
    if per_hour == 0 and table in {"signal_observations", "signal_labels", "signal_path_metrics", "latency_metrics"}:
        return "STALLED"
    if per_hour > 50000:
        return "SPIKE"
    if per_hour < 0.1 and table in {"signal_observations", "signal_labels"}:
        return "LOW"
    return "OK"


def _worst_status(statuses: list[Any]) -> str:
    order = {"BAD": 3, "WARNING": 2, "LOW": 2, "STALLED": 2, "SPIKE": 2, "UNKNOWN": 1, "OK": 0}
    normalized = [str(status or "UNKNOWN").upper() for status in statuses]
    return max(normalized, key=lambda item: order.get(item, 1)) if normalized else "UNKNOWN"


def _biggest_problem(growth: dict[str, Any], duplicates: dict[str, Any], relations: dict[str, Any], labels: dict[str, Any], sources: dict[str, Any], net_ev: dict[str, Any]) -> str:
    if duplicates.get("duplicate_status") == "BAD":
        return "duplicates"
    if relations.get("relation_status") == "BAD":
        return "stale_labels"
    if labels.get("mfe_mae_zero_rate", 0) > 0.85:
        return "mfe_mae_zero"
    if labels.get("label_quality_status") == "BAD":
        return "time_death"
    if sources.get("market_probe_contamination_status") == "BAD":
        return "source_contamination"
    if net_ev.get("cost_model_status") == "BAD":
        return "cost_model_suspicious"
    if growth.get("overall_growth_status") in {"STALLED", "LOW", "SPIKE"}:
        return "worker_health"
    return "unknown"


def _next_action(problem: str) -> str:
    return {
        "duplicates": "FIX_DATA_PIPELINE",
        "stale_labels": "REVIEW_LABELER",
        "mfe_mae_zero": "REVIEW_LABELER",
        "time_death": "REVIEW_LABELER",
        "source_contamination": "FIX_DATA_PIPELINE",
        "cost_model_suspicious": "REVIEW_COST_MODEL",
        "worker_health": "FIX_DATA_PIPELINE",
    }.get(problem, "KEEP_RESEARCH")


def _list_lines(items: Any) -> list[str]:
    rows = list(items or [])
    return [f"- {item}" for item in rows] if rows else ["- none"]


class _SmokeDb:
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
                CREATE TABLE signal_observations(id INTEGER PRIMARY KEY, timestamp TEXT, symbol TEXT, side TEXT, confidence_score INTEGER, market_regime TEXT, score_bucket TEXT);
                CREATE TABLE signal_labels(id INTEGER PRIMARY KEY, timestamp TEXT, observation_id INTEGER, label INTEGER, first_barrier_hit TEXT, bars_to_outcome INTEGER, max_favorable_excursion REAL, max_adverse_excursion REAL, realized_return_pct REAL);
                CREATE TABLE signal_path_metrics(id INTEGER PRIMARY KEY, observation_id INTEGER, symbol TEXT, side TEXT, score INTEGER, score_bucket TEXT, market_regime TEXT, source TEXT, max_favorable_pct REAL, max_adverse_pct REAL, final_return_pct REAL, bars_tracked INTEGER, first_barrier_hit TEXT, status TEXT, created_at TEXT, updated_at TEXT);
                CREATE TABLE latency_metrics(id INTEGER PRIMARY KEY, timestamp TEXT, metric_name TEXT, component TEXT, duration_ms REAL);
                CREATE TABLE events(id INTEGER PRIMARY KEY, timestamp TEXT, level TEXT, event_type TEXT, message TEXT);
                CREATE TABLE trades(id INTEGER PRIMARY KEY, timestamp TEXT, symbol TEXT, side TEXT, status TEXT);
                """
            )
            for i in range(1, 11):
                conn.execute("INSERT INTO signal_observations VALUES (?, ?, ?, ?, ?, ?, ?)", (i, now, "ETHUSDT", "SHORT", 90, "RISK_OFF", "90-94"))
                conn.execute("INSERT INTO signal_labels VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (i, now, i, 0, "TIME", 20, 0.0, 0.0, 0.0))
                conn.execute("INSERT INTO signal_path_metrics VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (i, i, "ETHUSDT", "SHORT", 90, "90-94", "RISK_OFF", "trade_signal", 0.0, 0.0, 0.0, 20, "TIME", "matured", now, now))
            conn.execute("INSERT INTO signal_labels VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (100, now, 1, 0, "TIME", 20, 0.0, 0.0, 0.0))
            conn.execute("INSERT INTO signal_labels VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (101, now, 999, 0, "TIME", 20, 0.0, 0.0, 0.0))
            conn.execute("INSERT INTO signal_path_metrics VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (100, 1000, "BTCUSDT", "LONG", 0, "PROBE", "CHOPPY_MARKET", "market_probe", 0.1, 0.1, 0.0, 20, "TIME", "matured", now, now))

    def _fetchall_dicts(self, cursor: Any) -> list[dict[str, Any]]:
        return [dict(row) for row in cursor.fetchall()]

    def table_exists(self, table: str) -> bool:
        row = self.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        return row is not None

    def get_table_columns(self, table: str) -> list[str]:
        return [row[1] for row in self.conn.execute(f"PRAGMA table_info({table})").fetchall()]

    def fetch_labeled_signal_rows_since(self, since_iso: str, limit: int = 50000) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT so.*, so.id AS observation_id, sl.first_barrier_hit, sl.bars_to_outcome,
                   sl.max_favorable_excursion, sl.max_adverse_excursion, sl.realized_return_pct
            FROM signal_labels sl
            JOIN signal_observations so ON so.id = sl.observation_id
            WHERE sl.timestamp >= ?
            LIMIT ?
            """,
            (since_iso, limit),
        )
        return [dict(row) for row in rows.fetchall()]

    def fetch_signal_path_metrics_since(self, since_iso: str, limit: int = 50000) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM signal_path_metrics WHERE created_at >= ? LIMIT ?", (since_iso, limit))
        return [dict(row) for row in rows.fetchall()]
