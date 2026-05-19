from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any, Iterator

from .data_pipeline_diagnosis import _fetch_rows
from .training_data_integrity import _scalar, _table_exists
from .utils import safe_float, safe_int


START = "LABEL QUALITY V2 START"
END = "LABEL QUALITY V2 END"


class LabelQualityV2:
    """Read-only audit of labels versus compact MFE/MAE path metrics."""

    def __init__(self, config: Any, db: Any) -> None:
        self.config = config
        self.db = db

    def build(self, *, hours: int = 24) -> dict[str, Any]:
        hours = max(1, int(hours or 24))
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        rows = self._rows(since)
        summary = _label_summary(rows)
        mismatches = _label_path_mismatches(rows)
        groups = _group_summaries(rows)
        diagnosis = _diagnosis(summary, mismatches)
        return {
            "hours": hours,
            "samples": len(rows),
            **summary,
            **mismatches,
            "by_side": groups["side"],
            "by_symbol": groups["symbol"][:20],
            "by_regime": groups["market_regime"][:20],
            "by_source": groups["source"],
            "by_score_bucket": groups["score_bucket"],
            "label_quality_status": diagnosis["label_quality_status"],
            "time_label_consistency": diagnosis["time_label_consistency"],
            "horizon_diagnosis": diagnosis["horizon_diagnosis"],
            "tp_sl_threshold_diagnosis": diagnosis["tp_sl_threshold_diagnosis"],
            "recommended_action": diagnosis["recommended_action"],
            "final_recommendation": "NO LIVE",
        }

    def to_text(self, *, hours: int = 24) -> str:
        payload = self.build(hours=hours)
        lines = [
            START,
            f"hours: {payload['hours']}",
            f"samples: {payload['samples']}",
            f"label_quality_status: {payload['label_quality_status']}",
            f"TP%: {payload['tp_ratio'] * 100:.2f}",
            f"SL%: {payload['sl_ratio'] * 100:.2f}",
            f"TIME%: {payload['time_ratio'] * 100:.2f}",
            f"avg_mfe: {payload['avg_mfe']:.5f}",
            f"median_mfe: {payload['median_mfe']:.5f}",
            f"avg_mae: {payload['avg_mae']:.5f}",
            f"median_mae: {payload['median_mae']:.5f}",
            f"avg_bars_to_outcome: {payload['avg_bars_to_outcome']:.2f}",
            f"max_holding_bars: {payload['max_holding_bars']}",
            f"missed_tp_labels: {payload['missed_tp_labels']}",
            f"missed_sl_labels: {payload['missed_sl_labels']}",
            f"inconsistent_time_labels: {payload['inconsistent_time_labels']}",
            f"path_metric_label_mismatch: {payload['path_metric_label_mismatch']}",
            f"both_tp_sl_touched_count: {payload['both_tp_sl_touched_count']}",
            f"horizon_too_short: {str(payload['horizon_too_short']).lower()}",
            f"tp_too_far: {str(payload['tp_too_far']).lower()}",
            f"sl_too_tight: {str(payload['sl_too_tight']).lower()}",
            f"low_vol_no_movement_real: {str(payload['low_vol_no_movement_real']).lower()}",
            f"stale_labeling: {str(payload['stale_labeling']).lower()}",
            f"time_label_consistency: {payload['time_label_consistency']}",
            f"horizon_diagnosis: {payload['horizon_diagnosis']}",
            f"tp_sl_threshold_diagnosis: {payload['tp_sl_threshold_diagnosis']}",
            "by_source:",
            *_group_lines(payload["by_source"]),
            "by_side:",
            *_group_lines(payload["by_side"]),
            "by_score_bucket:",
            *_group_lines(payload["by_score_bucket"]),
            f"recommended_action: {payload['recommended_action']}",
            "final_recommendation: NO LIVE",
            END,
        ]
        return "\n".join(lines)

    def _rows(self, since: str) -> list[dict[str, Any]]:
        if not (_table_exists(self.db, "signal_labels") and _table_exists(self.db, "signal_observations")):
            return []
        return _fetch_rows(
            self.db,
            """
            SELECT
                sl.id AS label_id,
                sl.timestamp AS label_timestamp,
                sl.observation_id,
                sl.first_barrier_hit AS label_hit,
                sl.bars_to_outcome,
                sl.max_favorable_excursion,
                sl.max_adverse_excursion,
                sl.realized_return_pct,
                so.symbol,
                so.side,
                so.market_regime,
                so.confidence_score,
                so.score_bucket,
                so.strategy_type,
                so.entry_price,
                so.stop_loss,
                so.take_profit_1,
                spm.source,
                spm.max_favorable_pct,
                spm.max_adverse_pct,
                spm.bars_tracked,
                spm.first_barrier_hit AS path_hit,
                spm.would_hit_tp_025,
                spm.would_hit_tp_050,
                spm.would_hit_tp_075,
                spm.would_hit_tp_100,
                spm.would_hit_sl_025,
                spm.would_hit_sl_050,
                spm.would_hit_sl_075,
                spm.would_hit_sl_100
            FROM signal_labels sl
            JOIN signal_observations so ON so.id = sl.observation_id
            LEFT JOIN signal_path_metrics spm ON spm.observation_id = sl.observation_id
            WHERE sl.timestamp >= ?
            ORDER BY sl.timestamp ASC
            LIMIT 50000
            """,
            (since,),
        )


class LabelQualityV2SmokeTest:
    def __init__(self, config: Any, db: Any | None = None, logger: Any | None = None) -> None:
        self.config = config

    def to_text(self) -> str:
        db = _LabelQualitySmokeDb()
        db.initialize()
        payload = LabelQualityV2(self.config, db).build(hours=24)
        passed = (
            payload["missed_tp_labels"] > 0
            and payload["missed_sl_labels"] > 0
            and payload["inconsistent_time_labels"] > 0
            and payload["label_quality_status"] in {"WARNING", "BAD"}
            and payload["final_recommendation"] == "NO LIVE"
        )
        return "\n".join([
            "LABEL QUALITY V2 SMOKE TEST START",
            f"missed_tp_detected: {str(payload['missed_tp_labels'] > 0).lower()}",
            f"missed_sl_detected: {str(payload['missed_sl_labels'] > 0).lower()}",
            f"time_consistency_checked: {str(bool(payload['time_label_consistency'])).lower()}",
            f"path_metric_mismatch_detected: {str(payload['path_metric_label_mismatch'] > 0).lower()}",
            "LIVE_TRADING=false",
            "DRY_RUN=true",
            "PAPER_TRADING=true",
            f"result: {'PASS' if passed else 'FAIL'}",
            "final_recommendation: NO LIVE",
            "LABEL QUALITY V2 SMOKE TEST END",
        ])


def _label_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    samples = len(rows)
    hits = Counter(_hit(row.get("label_hit")) for row in rows)
    mfes = [_mfe_pct(row) for row in rows]
    maes = [_mae_pct(row) for row in rows]
    bars = [safe_float(row.get("bars_to_outcome") if row.get("bars_to_outcome") is not None else row.get("bars_tracked")) for row in rows]
    max_hold = max([safe_int(value) for value in bars] or [0])
    return {
        "tp_count": hits["TP"],
        "sl_count": hits["SL"],
        "time_count": hits["TIME"],
        "tp_ratio": hits["TP"] / max(samples, 1),
        "sl_ratio": hits["SL"] / max(samples, 1),
        "time_ratio": hits["TIME"] / max(samples, 1),
        "avg_mfe": sum(mfes) / max(len(mfes), 1),
        "median_mfe": median(mfes) if mfes else 0.0,
        "avg_mae": sum(maes) / max(len(maes), 1),
        "median_mae": median(maes) if maes else 0.0,
        "avg_bars_to_outcome": sum(bars) / max(len(bars), 1),
        "max_holding_bars": max_hold,
    }


def _label_path_mismatches(rows: list[dict[str, Any]]) -> dict[str, Any]:
    missed_tp = missed_sl = inconsistent_time = mismatch = both = 0
    stale = 0
    for row in rows:
        label_hit = _hit(row.get("label_hit"))
        path_hit = _path_hit(row)
        tp_distance = _tp_distance_pct(row)
        sl_distance = _sl_distance_pct(row)
        mfe = _mfe_pct(row)
        mae = _mae_pct(row)
        hit_tp_by_distance = tp_distance > 0 and mfe >= tp_distance
        hit_sl_by_distance = sl_distance > 0 and mae >= sl_distance
        hit_tp_by_path = bool(safe_int(row.get("would_hit_tp_025")) or str(path_hit).startswith("TP"))
        hit_sl_by_path = bool(safe_int(row.get("would_hit_sl_025")) or str(path_hit).startswith("SL"))
        if label_hit != "TP" and (hit_tp_by_distance or hit_tp_by_path):
            missed_tp += 1
        if label_hit != "SL" and (hit_sl_by_distance or hit_sl_by_path):
            missed_sl += 1
        if label_hit == "TIME" and (hit_tp_by_distance or hit_sl_by_distance or hit_tp_by_path or hit_sl_by_path):
            inconsistent_time += 1
        if path_hit and label_hit != _hit(path_hit):
            mismatch += 1
        if (hit_tp_by_distance or hit_tp_by_path) and (hit_sl_by_distance or hit_sl_by_path):
            both += 1
        if not row.get("path_hit") and row.get("source") and safe_int(row.get("bars_tracked")) <= 0:
            stale += 1
    total = max(len(rows), 1)
    return {
        "missed_tp_labels": missed_tp,
        "missed_sl_labels": missed_sl,
        "inconsistent_time_labels": inconsistent_time,
        "path_metric_label_mismatch": mismatch,
        "both_tp_sl_touched_count": both,
        "horizon_too_short": inconsistent_time / total > 0.05 and missed_tp > missed_sl,
        "tp_too_far": _label_summary(rows)["time_ratio"] > 0.6 and _label_summary(rows)["avg_mfe"] > 0.20,
        "sl_too_tight": _label_summary(rows)["sl_ratio"] > 0.5,
        "low_vol_no_movement_real": _label_summary(rows)["time_ratio"] > 0.6 and _label_summary(rows)["avg_mfe"] < 0.10 and _label_summary(rows)["avg_mae"] < 0.10,
        "stale_labeling": stale / total > 0.20,
    }


def _group_summaries(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for key in ("side", "symbol", "market_regime", "source", "score_bucket"):
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            value = row.get(key) if row.get(key) not in {None, ""} else _score_bucket(row) if key == "score_bucket" else "unknown"
            groups[str(value).upper()].append(row)
        metrics = []
        for value, group in groups.items():
            snap = _label_summary(group)
            metrics.append({"group": value, "samples": len(group), **snap})
        metrics.sort(key=lambda item: safe_int(item.get("samples")), reverse=True)
        result[key] = metrics
    return result


def _diagnosis(summary: dict[str, Any], mismatches: dict[str, Any]) -> dict[str, str]:
    mismatch_count = safe_int(mismatches.get("missed_tp_labels")) + safe_int(mismatches.get("missed_sl_labels")) + safe_int(mismatches.get("inconsistent_time_labels"))
    samples = max(1, safe_int(summary.get("tp_count")) + safe_int(summary.get("sl_count")) + safe_int(summary.get("time_count")))
    if mismatch_count / samples > 0.05:
        status = "BAD"
        action = "REVIEW_LABELER"
    elif mismatch_count:
        status = "WARNING"
        action = "REVIEW_LABELER"
    elif safe_float(summary.get("time_ratio")) > 0.80:
        status = "WARNING"
        action = "REVIEW_TP_SL_HORIZON"
    else:
        status = "OK"
        action = "KEEP_RESEARCH"
    if mismatches.get("inconsistent_time_labels"):
        consistency = "WARNING"
    else:
        consistency = "OK"
    if mismatches.get("horizon_too_short"):
        horizon = "horizon_may_be_too_short"
    elif mismatches.get("low_vol_no_movement_real"):
        horizon = "low_vol_no_movement_real"
    else:
        horizon = "no_horizon_issue_detected"
    if mismatches.get("tp_too_far"):
        tp_sl = "tp_may_be_too_far"
    elif mismatches.get("sl_too_tight"):
        tp_sl = "sl_may_be_too_tight"
    else:
        tp_sl = "thresholds_not_proven_bad"
    return {
        "label_quality_status": status,
        "time_label_consistency": consistency,
        "horizon_diagnosis": horizon,
        "tp_sl_threshold_diagnosis": tp_sl,
        "recommended_action": action,
    }


def _hit(value: Any) -> str:
    text = str(value or "").upper()
    if text.startswith("TP"):
        return "TP"
    if text == "SL":
        return "SL"
    if text == "TIME":
        return "TIME"
    return "UNKNOWN"


def _path_hit(row: dict[str, Any]) -> str:
    text = str(row.get("path_hit") or "").upper()
    if text.startswith("TP"):
        return "TP"
    if text.startswith("SL"):
        return "SL"
    return ""


def _mfe_pct(row: dict[str, Any]) -> float:
    value = row.get("max_favorable_pct")
    if value is not None:
        return safe_float(value)
    label_value = safe_float(row.get("max_favorable_excursion"))
    return label_value * 100.0 if abs(label_value) <= 5 else label_value


def _mae_pct(row: dict[str, Any]) -> float:
    value = row.get("max_adverse_pct")
    if value is not None:
        return abs(safe_float(value))
    label_value = abs(safe_float(row.get("max_adverse_excursion")))
    return label_value * 100.0 if abs(label_value) <= 5 else label_value


def _tp_distance_pct(row: dict[str, Any]) -> float:
    entry = safe_float(row.get("entry_price"))
    tp = safe_float(row.get("take_profit_1"))
    if entry <= 0 or tp <= 0:
        return 0.0
    return abs(tp - entry) / entry * 100.0


def _sl_distance_pct(row: dict[str, Any]) -> float:
    entry = safe_float(row.get("entry_price"))
    stop = safe_float(row.get("stop_loss"))
    if entry <= 0 or stop <= 0:
        return 0.0
    return abs(entry - stop) / entry * 100.0


def _score_bucket(row: dict[str, Any]) -> str:
    score = safe_float(row.get("confidence_score"))
    if score >= 95:
        return "95-100"
    if score >= 90:
        return "90-94"
    if score >= 80:
        return "80-89"
    if score >= 70:
        return "70-79"
    return "<70"


def _group_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- none"]
    return [
        f"- {row.get('group')} samples={row.get('samples')} TP%={safe_float(row.get('tp_ratio')) * 100:.1f} SL%={safe_float(row.get('sl_ratio')) * 100:.1f} TIME%={safe_float(row.get('time_ratio')) * 100:.1f}"
        for row in rows[:10]
    ]


class _LabelQualitySmokeDb:
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
                CREATE TABLE signal_observations(id INTEGER PRIMARY KEY, timestamp TEXT, symbol TEXT, side TEXT, market_regime TEXT, confidence_score INTEGER, score_bucket TEXT, strategy_type TEXT, entry_price REAL, stop_loss REAL, take_profit_1 REAL);
                CREATE TABLE signal_labels(id INTEGER PRIMARY KEY, timestamp TEXT, observation_id INTEGER, first_barrier_hit TEXT, bars_to_outcome INTEGER, max_favorable_excursion REAL, max_adverse_excursion REAL, realized_return_pct REAL);
                CREATE TABLE signal_path_metrics(id INTEGER PRIMARY KEY, observation_id INTEGER, source TEXT, max_favorable_pct REAL, max_adverse_pct REAL, bars_tracked INTEGER, first_barrier_hit TEXT, would_hit_tp_025 INTEGER, would_hit_tp_050 INTEGER, would_hit_tp_075 INTEGER, would_hit_tp_100 INTEGER, would_hit_sl_025 INTEGER, would_hit_sl_050 INTEGER, would_hit_sl_075 INTEGER, would_hit_sl_100 INTEGER);
                """
            )
            rows = [
                (1, "TIME", 0.60, 0.10, 0.0, "TP_025", 1, 0),
                (2, "TIME", 0.05, 0.80, 0.0, "SL_025", 0, 1),
                (3, "TP1", 0.70, 0.20, 0.5, "TP_025", 1, 0),
                (4, "SL", 0.10, 0.90, -0.5, "SL_025", 0, 1),
            ]
            for obs_id, hit, mfe, mae, ret, path_hit, tp_flag, sl_flag in rows:
                conn.execute("INSERT INTO signal_observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (obs_id, now, "ETHUSDT", "SHORT", "RISK_OFF", 90, "90-94", "trend", 100.0, 101.0, 99.5))
                conn.execute("INSERT INTO signal_labels VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (obs_id, now, obs_id, hit, 20, mfe / 100.0, -mae / 100.0, ret / 100.0))
                conn.execute("INSERT INTO signal_path_metrics VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (obs_id, obs_id, "trade_signal", mfe, mae, 20, path_hit, tp_flag, tp_flag, 0, 0, sl_flag, sl_flag, 0, 0))

    def _fetchall_dicts(self, cursor: Any) -> list[dict[str, Any]]:
        return [dict(row) for row in cursor.fetchall()]

    def table_exists(self, table: str) -> bool:
        return self.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None

    def get_table_columns(self, table: str) -> list[str]:
        return [row[1] for row in self.conn.execute(f"PRAGMA table_info({table})").fetchall()]
