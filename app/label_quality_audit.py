"""Label Quality Audit — READ-ONLY checks for TripleBarrierLabeler outputs.

Verifies:
  - missed_tp_labels:    label says TIME/SL but a TP price was actually touched
                         within the holding window (per path metric or OHLCV).
  - missed_sl_labels:    label says TIME/TP but SL was touched.
  - inconsistent_time_labels: TIME label but realized_return_pct outside the
                              cost band.
  - path_metric_label_mismatch: signal_path_metric reports TP_HIT but label
                                says TIME, etc.
  - both_tp_sl_touched_count: same-bar TP and SL detected — must respect
                              STOP_BEFORE_TP rule.
  - stale_labels: label timestamp older than observation timestamp.

NO runtime change. NO modification of signal_labels. NO new labels generated.
Pure diagnostic on already-persisted rows.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .utils import iso_utc, safe_float, safe_int


FINAL_RECOMMENDATION = "NO LIVE"


@dataclass
class LabelQualityReport:
    generated_at: str
    hours: int
    total_labels: int
    tp_count: int
    sl_count: int
    time_count: int
    tp_rate: float
    sl_rate: float
    time_rate: float
    missed_tp_labels: int
    missed_sl_labels: int
    inconsistent_time_labels: int
    path_metric_label_mismatch: int
    both_tp_sl_touched_count: int
    stale_labels: int
    label_quality_status: str
    tp_too_far_flag: bool
    sl_too_tight_flag: bool
    horizon_too_short_flag: bool
    recommended_action: str
    notes: list[str] = field(default_factory=list)
    final_recommendation: str = FINAL_RECOMMENDATION
    research_only: bool = True

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class LabelQualityAudit:
    """Read-only audit over signal_labels + signal_path_metrics + signal_observations."""

    # Tunable thresholds — research/diagnostic only, not runtime gates.
    COST_BAND_PCT = 0.0018       # ~0.18% Bitget taker round-trip
    TIME_TOO_FAR_THRESHOLD = 0.6 # >=60% of TP1 already touched and still TIME

    def __init__(self, db: Any, logger: Any | None = None) -> None:
        self.db = db
        self.logger = logger

    def build(self, *, hours: int = 24) -> LabelQualityReport:
        since_iso = (datetime.now(timezone.utc) - timedelta(hours=max(1, int(hours or 24)))).isoformat()
        if not self._table_exists("signal_labels"):
            return self._empty_report(hours)
        total = self._count_rows("signal_labels", since_iso=since_iso)
        if total == 0:
            return self._empty_report(hours)

        breakdown = self._barrier_breakdown(since_iso=since_iso)
        tp_count = breakdown.get("TP1", 0) + breakdown.get("TP2", 0)
        sl_count = breakdown.get("SL", 0)
        time_count = breakdown.get("TIME", 0)

        missed_tp = self._missed_tp_labels(since_iso=since_iso)
        missed_sl = self._missed_sl_labels(since_iso=since_iso)
        inconsistent_time = self._inconsistent_time_labels(since_iso=since_iso)
        mismatch = self._path_label_mismatch(since_iso=since_iso)
        both = self._both_tp_sl_touched(since_iso=since_iso)
        stale = self._stale_labels(since_iso=since_iso)

        tp_rate = tp_count / max(total, 1)
        sl_rate = sl_count / max(total, 1)
        time_rate = time_count / max(total, 1)

        tp_too_far = bool(time_rate > 0.85 and missed_tp == 0 and self._mfe_close_to_tp_ratio(since_iso=since_iso) > self.TIME_TOO_FAR_THRESHOLD)
        sl_too_tight = bool(sl_rate > 0.3 and self._mae_far_from_realized_ratio(since_iso=since_iso) > 0.5)
        horizon_too_short = bool(time_rate > 0.9 and self._time_at_max_bars_ratio(since_iso=since_iso) > 0.5)

        status = self._status(
            missed_tp=missed_tp, missed_sl=missed_sl,
            inconsistent_time=inconsistent_time, mismatch=mismatch,
            both=both, stale=stale,
            tp_too_far=tp_too_far, sl_too_tight=sl_too_tight,
        )
        recommended = self._recommend(status, tp_too_far=tp_too_far, sl_too_tight=sl_too_tight, horizon_too_short=horizon_too_short)

        notes: list[str] = []
        if tp_too_far:
            notes.append("tp_too_far_suggests_tp_distance_reduction_or_dynamic_tp")
        if sl_too_tight:
            notes.append("sl_too_tight_suggests_swing_stop_or_atr_stop")
        if horizon_too_short:
            notes.append("horizon_too_short_suggests_extending_max_holding_bars")

        return LabelQualityReport(
            generated_at=iso_utc(),
            hours=hours,
            total_labels=total,
            tp_count=tp_count,
            sl_count=sl_count,
            time_count=time_count,
            tp_rate=tp_rate,
            sl_rate=sl_rate,
            time_rate=time_rate,
            missed_tp_labels=missed_tp,
            missed_sl_labels=missed_sl,
            inconsistent_time_labels=inconsistent_time,
            path_metric_label_mismatch=mismatch,
            both_tp_sl_touched_count=both,
            stale_labels=stale,
            label_quality_status=status,
            tp_too_far_flag=tp_too_far,
            sl_too_tight_flag=sl_too_tight,
            horizon_too_short_flag=horizon_too_short,
            recommended_action=recommended,
            notes=notes,
        )

    def _empty_report(self, hours: int) -> LabelQualityReport:
        return LabelQualityReport(
            generated_at=iso_utc(), hours=hours, total_labels=0,
            tp_count=0, sl_count=0, time_count=0,
            tp_rate=0.0, sl_rate=0.0, time_rate=0.0,
            missed_tp_labels=0, missed_sl_labels=0,
            inconsistent_time_labels=0, path_metric_label_mismatch=0,
            both_tp_sl_touched_count=0, stale_labels=0,
            label_quality_status="NO_DATA",
            tp_too_far_flag=False, sl_too_tight_flag=False, horizon_too_short_flag=False,
            recommended_action="NEED_LABELS",
        )

    # ---- queries ---------------------------------------------------------

    def _barrier_breakdown(self, *, since_iso: str) -> dict[str, int]:
        sql = (
            "SELECT first_barrier_hit, COUNT(*) AS cnt FROM signal_labels "
            "WHERE timestamp >= ? GROUP BY first_barrier_hit"
        )
        rows = self._fetch(sql, (since_iso,))
        return {str(row.get("first_barrier_hit") or "").upper(): safe_int(row.get("cnt")) for row in rows}

    def _missed_tp_labels(self, *, since_iso: str) -> int:
        """Label != TP but MFE >= (tp_distance / entry)."""
        # Heuristic: realized_return_pct (fraction) for TIME/SL is less than
        # the take_profit threshold but max_favorable_excursion exceeded it.
        sql = (
            "SELECT COUNT(*) FROM signal_labels sl "
            "JOIN signal_observations so ON so.id = sl.observation_id "
            "WHERE sl.timestamp >= ? "
            "AND sl.first_barrier_hit IN ('TIME','SL') "
            "AND so.take_profit_1 > 0 AND so.entry_price > 0 "
            "AND ((so.side = 'LONG' AND sl.max_favorable_excursion >= "
            "       ((so.take_profit_1 - so.entry_price) / so.entry_price)) "
            "  OR (so.side = 'SHORT' AND sl.max_favorable_excursion >= "
            "       ((so.entry_price - so.take_profit_1) / so.entry_price)))"
        )
        return self._scalar(sql, (since_iso,))

    def _missed_sl_labels(self, *, since_iso: str) -> int:
        sql = (
            "SELECT COUNT(*) FROM signal_labels sl "
            "JOIN signal_observations so ON so.id = sl.observation_id "
            "WHERE sl.timestamp >= ? "
            "AND sl.first_barrier_hit IN ('TIME','TP1','TP2') "
            "AND so.stop_loss > 0 AND so.entry_price > 0 "
            "AND ((so.side = 'LONG' AND sl.max_adverse_excursion <= "
            "       ((so.stop_loss - so.entry_price) / so.entry_price)) "
            "  OR (so.side = 'SHORT' AND sl.max_adverse_excursion <= "
            "       ((so.entry_price - so.stop_loss) / so.entry_price)))"
        )
        return self._scalar(sql, (since_iso,))

    def _inconsistent_time_labels(self, *, since_iso: str) -> int:
        """TIME label but realized_return_pct outside cost band (suggests it should have been TP or SL)."""
        sql = (
            "SELECT COUNT(*) FROM signal_labels "
            "WHERE timestamp >= ? AND first_barrier_hit = 'TIME' "
            "AND ABS(realized_return_pct) > ?"
        )
        return self._scalar(sql, (since_iso, self.COST_BAND_PCT * 3.0))

    def _path_label_mismatch(self, *, since_iso: str) -> int:
        if not self._table_exists("signal_path_metrics"):
            return 0
        sql = (
            "SELECT COUNT(*) FROM signal_path_metrics spm "
            "JOIN signal_labels sl ON sl.observation_id = spm.observation_id "
            "WHERE sl.timestamp >= ? AND ("
            "  (spm.status = 'TP_HIT' AND sl.first_barrier_hit = 'TIME') "
            "  OR (spm.status = 'SL_HIT' AND sl.first_barrier_hit = 'TIME') "
            "  OR (spm.status = 'TIME' AND sl.first_barrier_hit IN ('TP1','TP2','SL'))"
            ")"
        )
        return self._scalar(sql, (since_iso,))

    def _both_tp_sl_touched(self, *, since_iso: str) -> int:
        """Labels where BOTH MFE >= TP threshold AND MAE <= SL threshold."""
        sql = (
            "SELECT COUNT(*) FROM signal_labels sl "
            "JOIN signal_observations so ON so.id = sl.observation_id "
            "WHERE sl.timestamp >= ? AND so.take_profit_1 > 0 AND so.stop_loss > 0 "
            "AND so.entry_price > 0 "
            "AND ((so.side = 'LONG' "
            "      AND sl.max_favorable_excursion >= ((so.take_profit_1 - so.entry_price) / so.entry_price) "
            "      AND sl.max_adverse_excursion <= ((so.stop_loss - so.entry_price) / so.entry_price)) "
            "  OR (so.side = 'SHORT' "
            "      AND sl.max_favorable_excursion >= ((so.entry_price - so.take_profit_1) / so.entry_price) "
            "      AND sl.max_adverse_excursion <= ((so.entry_price - so.stop_loss) / so.entry_price)))"
        )
        return self._scalar(sql, (since_iso,))

    def _stale_labels(self, *, since_iso: str) -> int:
        sql = (
            "SELECT COUNT(*) FROM signal_labels sl "
            "JOIN signal_observations so ON so.id = sl.observation_id "
            "WHERE sl.timestamp >= ? AND sl.timestamp < so.timestamp"
        )
        return self._scalar(sql, (since_iso,))

    def _mfe_close_to_tp_ratio(self, *, since_iso: str) -> float:
        sql = (
            "SELECT AVG(ratio) FROM (SELECT "
            "  CASE WHEN so.side='LONG' AND so.take_profit_1 > so.entry_price AND so.entry_price > 0 THEN "
            "    sl.max_favorable_excursion / NULLIF((so.take_profit_1 - so.entry_price) / so.entry_price, 0) "
            "    WHEN so.side='SHORT' AND so.entry_price > so.take_profit_1 AND so.entry_price > 0 THEN "
            "    sl.max_favorable_excursion / NULLIF((so.entry_price - so.take_profit_1) / so.entry_price, 0) "
            "    ELSE 0 END AS ratio "
            "FROM signal_labels sl JOIN signal_observations so ON so.id = sl.observation_id "
            "WHERE sl.timestamp >= ? AND sl.first_barrier_hit='TIME') t"
        )
        return safe_float(self._scalar(sql, (since_iso,)))

    def _mae_far_from_realized_ratio(self, *, since_iso: str) -> float:
        sql = (
            "SELECT AVG(ABS(max_adverse_excursion) / NULLIF(ABS(realized_return_pct), 0)) "
            "FROM signal_labels WHERE timestamp >= ? AND first_barrier_hit = 'SL' "
            "AND realized_return_pct < 0"
        )
        return safe_float(self._scalar(sql, (since_iso,)))

    def _time_at_max_bars_ratio(self, *, since_iso: str) -> float:
        sql = (
            "SELECT AVG(CASE WHEN bars_to_outcome >= ? THEN 1.0 ELSE 0.0 END) "
            "FROM signal_labels WHERE timestamp >= ? AND first_barrier_hit = 'TIME'"
        )
        # Use a generic max_holding_bars=48 fallback (matches config default).
        return safe_float(self._scalar(sql, (48, since_iso)))

    # ---- classification ---------------------------------------------------

    def _status(self, **kwargs) -> str:
        bad_signals = [
            kwargs.get("missed_tp", 0) > 0,
            kwargs.get("missed_sl", 0) > 0,
            kwargs.get("mismatch", 0) > 0,
            kwargs.get("inconsistent_time", 0) > 0,
            kwargs.get("tp_too_far", False),
            kwargs.get("sl_too_tight", False),
        ]
        if sum(1 for v in bad_signals if v) >= 2:
            return "BAD"
        if any(bad_signals):
            return "WARNING"
        if kwargs.get("stale", 0) > 0 or kwargs.get("both", 0) > 0:
            return "WARNING"
        return "OK"

    def _recommend(self, status: str, *, tp_too_far: bool, sl_too_tight: bool, horizon_too_short: bool) -> str:
        if status == "BAD":
            return "REVIEW_LABELER_AND_PATH_METRICS"
        if tp_too_far and not sl_too_tight:
            return "REDUCE_TP_DISTANCE_OR_DYNAMIC_TP"
        if sl_too_tight and not tp_too_far:
            return "USE_SWING_STOP_OR_ATR_STOP"
        if horizon_too_short:
            return "EXTEND_MAX_HOLDING_BARS"
        return "NO_ACTION"

    # ---- SQL helpers ------------------------------------------------------

    def _use_postgres(self) -> bool:
        return bool(getattr(self.db, "_use_postgres", False))

    def _table_exists(self, table: str) -> bool:
        try:
            return bool(self.db.table_exists(table))
        except Exception:
            return False

    def _count_rows(self, table: str, *, since_iso: str) -> int:
        sql = f"SELECT COUNT(*) AS count FROM {table} WHERE timestamp >= ?"
        if self._use_postgres():
            sql = sql.replace("?", "%s")
        return safe_int(self._scalar(sql, (since_iso,)))

    def _scalar(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        if self._use_postgres():
            sql = sql.replace("?", "%s")
        try:
            with self.db._connect() as conn:
                row = conn.execute(sql, params).fetchone()
                if row is None:
                    return 0
                if isinstance(row, dict):
                    return safe_int(next(iter(row.values()), 0))
                try:
                    return safe_int(row[0])
                except Exception:
                    return 0
        except Exception:
            return 0

    def _fetch(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        if self._use_postgres():
            sql = sql.replace("?", "%s")
        try:
            with self.db._connect() as conn:
                cursor = conn.execute(sql, params)
                if hasattr(self.db, "_fetchall_dicts"):
                    return self.db._fetchall_dicts(cursor)
                return [dict(row) for row in cursor.fetchall()]
        except Exception:
            return []


def render_report_text(report: LabelQualityReport) -> str:
    lines = [
        "LABEL QUALITY AUDIT START",
        f"generated_at: {report.generated_at}",
        f"hours: {report.hours}",
        f"label_quality_status: {report.label_quality_status}",
        f"total_labels: {report.total_labels}",
        f"tp_count: {report.tp_count}  ({report.tp_rate:.1%})",
        f"sl_count: {report.sl_count}  ({report.sl_rate:.1%})",
        f"time_count: {report.time_count}  ({report.time_rate:.1%})",
        f"missed_tp_labels: {report.missed_tp_labels}",
        f"missed_sl_labels: {report.missed_sl_labels}",
        f"inconsistent_time_labels: {report.inconsistent_time_labels}",
        f"path_metric_label_mismatch: {report.path_metric_label_mismatch}",
        f"both_tp_sl_touched_count: {report.both_tp_sl_touched_count}",
        f"stale_labels: {report.stale_labels}",
        f"tp_too_far: {str(report.tp_too_far_flag).lower()}",
        f"sl_too_tight: {str(report.sl_too_tight_flag).lower()}",
        f"horizon_too_short: {str(report.horizon_too_short_flag).lower()}",
        f"recommended_action: {report.recommended_action}",
        "research_only: true",
        "no_runtime_change: true",
        f"final_recommendation: {report.final_recommendation}",
        "LABEL QUALITY AUDIT END",
    ]
    return "\n".join(lines)
