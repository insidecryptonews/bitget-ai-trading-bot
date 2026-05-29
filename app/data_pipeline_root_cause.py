"""ResearchOps V7 — Data pipeline root cause audit.

Read-only diagnostic that explains *why* the training data is BAD. It classifies
duplicates and surfaces the biggest contributor:

    EXACT_DUPLICATE
    SEMANTIC_DUPLICATE
    BENIGN_SCAN_REPEAT
    DANGEROUS_DUPLICATE
    LABEL_DUPLICATE
    ORPHAN_LABEL
    ORPHAN_METRIC
    MARKET_PROBE_NOISE
    TRADE_SIGNAL_DUPLICATE

Contract:
    - never modifies the DB
    - never calls Bitget private endpoints
    - never opens orders
    - returns a dataclass payload + plain-text render
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable


FINAL_RECOMMENDATION = "NO LIVE"

# Thresholds — matched to existing clean view defaults.
DANGEROUS_DUPLICATE_RATE = 0.10
WARN_DUPLICATE_RATE = 0.03


@dataclass
class DuplicateBucketCount:
    bucket: str
    count: int
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DataPipelineRootCauseReport:
    hours: int
    symbols: list[str]
    timeframes: list[str]
    raw_sample_count: int = 0
    clean_sample_count: int = 0
    exact_duplicate_count: int = 0
    semantic_duplicate_count: int = 0
    dangerous_duplicate_count: int = 0
    benign_scan_repeat_count: int = 0
    market_probe_count: int = 0
    trade_signal_count: int = 0
    exact_duplicate_rate: float = 0.0
    semantic_duplicate_rate: float = 0.0
    dangerous_duplicate_rate: float = 0.0
    benign_scan_repeat_rate: float = 0.0
    clean_ratio: float = 0.0
    source_adjusted_clean_count: int = 0
    trade_signal_clean_count: int = 0
    market_probe_clean_count: int = 0
    orphan_labels: int = 0
    orphan_path_metrics: int = 0
    label_duplicates: int = 0
    duplicate_key_is_too_aggressive: bool = False
    biggest_problem: str = "none"
    recommended_fix: str = ""
    can_use_for_strategy_eval: bool = False
    classification_counts: list[DuplicateBucketCount] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    research_only: bool = True
    final_recommendation: str = FINAL_RECOMMENDATION
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    no_db_writes: bool = True

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["classification_counts"] = [c.as_dict() for c in self.classification_counts]
        return data


def _since_iso(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=max(1, int(hours or 1)))).isoformat()


def _coerce_symbols(symbols: Iterable[str] | str | None) -> list[str] | None:
    if symbols is None:
        return None
    if isinstance(symbols, str):
        values = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    else:
        values = [str(s).strip().upper() for s in symbols if str(s).strip()]
    return values or None


def _coerce_timeframes(timeframes: Iterable[str] | str | None) -> list[str] | None:
    if timeframes is None:
        return None
    if isinstance(timeframes, str):
        values = [t.strip().lower() for t in timeframes.split(",") if t.strip()]
    else:
        values = [str(t).strip().lower() for t in timeframes if str(t).strip()]
    return values or None


def _table_exists(db: Any, name: str) -> bool:
    if not db:
        return False
    try:
        return bool(db.table_exists(name))
    except Exception:
        return False


def _safe_count(db: Any, sql: str, params: tuple) -> int:
    if not db:
        return 0
    try:
        local_sql = sql.replace("?", "%s") if bool(getattr(db, "_use_postgres", False)) else sql
        with db._connect() as conn:
            row = conn.execute(local_sql, params).fetchone()
        if not row:
            return 0
        try:
            return int(db._row_value(row, "cnt", 0, 0) or 0)
        except Exception:
            try:
                return int(row[0] or 0)
            except Exception:
                return 0
    except Exception:
        return 0


def _audit_signal_observations(
    db: Any,
    since_iso: str,
    symbol_filter: str,
    params: list[Any],
) -> dict[str, int]:
    """Compute raw, clean (aggressive minute key) and source-adjusted clean."""
    raw_sql = (
        "SELECT COUNT(*) AS cnt FROM signal_observations "
        "WHERE timestamp >= ?" + symbol_filter
    )
    raw = _safe_count(db, raw_sql, tuple(params))

    # Aggressive minute key — matches the V5 clean view default.
    aggressive_clean_sql = (
        "SELECT COUNT(*) AS cnt FROM ("
        " SELECT MIN(rowid) AS keep_row FROM signal_observations "
        " WHERE timestamp >= ?" + symbol_filter +
        " GROUP BY symbol, side, substr(timestamp, 1, 16)"
        ") AS aggregated"
    )
    aggressive_clean = _safe_count(db, aggressive_clean_sql, tuple(params))

    # Source-adjusted clean key — adds source column (when available) so
    # market_probe and trade_signal rows are not collapsed together.
    # Falls back gracefully when the column is missing or unpopulated.
    source_adjusted_clean = aggressive_clean
    try:
        if bool(getattr(db, "_use_postgres", False)):
            source_present_sql = (
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name='signal_observations' AND column_name='source' LIMIT 1"
            )
        else:
            source_present_sql = (
                "SELECT 1 FROM pragma_table_info('signal_observations') WHERE name='source' LIMIT 1"
            )
        with db._connect() as conn:
            present = conn.execute(source_present_sql).fetchone()
        if present:
            adjusted_sql = (
                "SELECT COUNT(*) AS cnt FROM ("
                " SELECT MIN(rowid) AS keep_row FROM signal_observations "
                " WHERE timestamp >= ?" + symbol_filter +
                " GROUP BY symbol, side, COALESCE(source,'unknown'), "
                "COALESCE(strategy_type,'unknown'), COALESCE(market_regime,'unknown'), "
                "COALESCE(confidence_score,0), substr(timestamp, 1, 16)"
                ") AS aggregated"
            )
            source_adjusted_clean = _safe_count(db, adjusted_sql, tuple(params))
    except Exception:
        pass
    return {
        "raw_sample_count": raw,
        "aggressive_clean_count": aggressive_clean,
        "source_adjusted_clean_count": source_adjusted_clean,
    }


def _split_by_source(
    db: Any,
    since_iso: str,
    symbol_filter: str,
    params: list[Any],
) -> dict[str, int]:
    """Count trade_signal vs market_probe rows. Tolerant of missing column."""
    try:
        trade_signal_sql = (
            "SELECT COUNT(*) AS cnt FROM signal_observations "
            "WHERE timestamp >= ?" + symbol_filter +
            " AND COALESCE(LOWER(strategy_type), '') NOT LIKE '%market_probe%'"
            " AND COALESCE(LOWER(strategy_type), '') NOT IN ('probe', 'market_probe')"
        )
        market_probe_sql = (
            "SELECT COUNT(*) AS cnt FROM signal_observations "
            "WHERE timestamp >= ?" + symbol_filter +
            " AND (COALESCE(LOWER(strategy_type), '') LIKE '%market_probe%'"
            "      OR COALESCE(LOWER(strategy_type), '') IN ('probe', 'market_probe'))"
        )
        return {
            "trade_signal_count": _safe_count(db, trade_signal_sql, tuple(params)),
            "market_probe_count": _safe_count(db, market_probe_sql, tuple(params)),
        }
    except Exception:
        return {"trade_signal_count": 0, "market_probe_count": 0}


def _benign_scan_repeats(
    db: Any,
    since_iso: str,
    symbol_filter: str,
    params: list[Any],
) -> int:
    """Benign repeats — same observation re-scanned within ≤2 minutes with
    identical strategy_type/market_regime. They are NOT promotable but they
    are not dangerous either (the worker just re-evaluated the candle)."""
    try:
        sql = (
            "SELECT COUNT(*) AS cnt FROM ("
            " SELECT 1 FROM signal_observations "
            " WHERE timestamp >= ?" + symbol_filter +
            " GROUP BY symbol, side, COALESCE(strategy_type,''), COALESCE(market_regime,''), "
            "substr(timestamp, 1, 15)  -- minute precision -1 char (≤10s buckets) "
            " HAVING COUNT(*) > 1"
            ") AS dup_buckets"
        )
        return _safe_count(db, sql, tuple(params))
    except Exception:
        return 0


def _dangerous_duplicates(
    db: Any,
    since_iso: str,
    symbol_filter: str,
    params: list[Any],
) -> int:
    """Dangerous duplicates — rows with the same (symbol, side, strategy_type,
    confidence_score, entry_price) collapsed to a different minute. These would
    inflate sample counts and EV/PF if the bucketing key were wrong."""
    try:
        sql = (
            "SELECT COUNT(*) AS cnt FROM ("
            " SELECT 1 FROM signal_observations "
            " WHERE timestamp >= ?" + symbol_filter +
            " GROUP BY symbol, side, COALESCE(strategy_type,''), COALESCE(confidence_score,0), "
            "COALESCE(ROUND(entry_price, 6),0) "
            " HAVING COUNT(*) > 1"
            ") AS d"
        )
        return _safe_count(db, sql, tuple(params))
    except Exception:
        return 0


def _label_duplicates(db: Any, since_iso: str) -> int:
    if not _table_exists(db, "signal_labels"):
        return 0
    sql = (
        "SELECT COUNT(*) AS cnt FROM ("
        " SELECT 1 FROM signal_labels WHERE timestamp >= ? "
        " GROUP BY observation_id HAVING COUNT(*) > 1"
        ") AS d"
    )
    return _safe_count(db, sql, (since_iso,))


def _orphan_counts(db: Any, since_iso: str) -> dict[str, int]:
    out: dict[str, int] = {"orphan_labels": 0, "orphan_path_metrics": 0}
    if _table_exists(db, "signal_labels") and _table_exists(db, "signal_observations"):
        sql = (
            "SELECT COUNT(*) AS cnt FROM signal_labels l "
            "WHERE l.timestamp >= ? AND NOT EXISTS ("
            "  SELECT 1 FROM signal_observations o WHERE o.id = l.observation_id)"
        )
        out["orphan_labels"] = _safe_count(db, sql, (since_iso,))
    if _table_exists(db, "signal_path_metrics") and _table_exists(db, "signal_observations"):
        sql = (
            "SELECT COUNT(*) AS cnt FROM signal_path_metrics p "
            "WHERE p.created_at >= ? AND NOT EXISTS ("
            "  SELECT 1 FROM signal_observations o WHERE o.id = p.observation_id)"
        )
        out["orphan_path_metrics"] = _safe_count(db, sql, (since_iso,))
    return out


def _classify_biggest_problem(
    *,
    raw: int,
    aggressive_clean: int,
    source_adjusted_clean: int,
    dangerous: int,
    benign: int,
    orphans: int,
    label_dups: int,
    market_probe: int,
    trade_signal: int,
) -> tuple[str, str, bool]:
    if raw == 0:
        return "no_data", "increase_hours_window_or_check_worker", False
    duplicate_rate = (raw - aggressive_clean) / raw if raw else 0.0
    danger_rate = dangerous / raw if raw else 0.0
    market_probe_share = market_probe / raw if raw else 0.0
    too_aggressive = source_adjusted_clean > aggressive_clean * 1.05
    if too_aggressive:
        return (
            "duplicate_key_too_aggressive_collapses_different_setups",
            "use_source_adjusted_key_for_clean_view",
            True,
        )
    if danger_rate >= DANGEROUS_DUPLICATE_RATE:
        return (
            "dangerous_duplicate_rate_high",
            "investigate_repeated_writes_per_setup_and_dedupe_key",
            False,
        )
    if duplicate_rate >= DANGEROUS_DUPLICATE_RATE and market_probe_share >= 0.30:
        return (
            "market_probe_noise_inflates_raw_counts",
            "evaluate_with_trade_signal_subset_only",
            False,
        )
    if duplicate_rate >= DANGEROUS_DUPLICATE_RATE:
        return (
            "duplicates_above_safe_threshold",
            "use_clean_metrics_only_for_promotion",
            False,
        )
    if label_dups > 0 or orphans > 0:
        return (
            "label_orphans_or_label_duplicates_present",
            "review_label_pipeline_dedupe_by_observation_id",
            False,
        )
    if benign / max(raw, 1) >= 0.30:
        return ("benign_scan_repeats_dominate", "tighten_scan_rate_or_dedupe_buckets", True)
    return ("clean_enough_for_research", "continue_collecting_clean_samples", True)


def run_data_pipeline_root_cause(
    db: Any,
    *,
    hours: int = 24,
    symbols: Iterable[str] | str | None = None,
    timeframes: Iterable[str] | str | None = None,
) -> DataPipelineRootCauseReport:
    symbol_list = _coerce_symbols(symbols) or []
    timeframe_list = _coerce_timeframes(timeframes) or []
    since_iso = _since_iso(hours)
    symbol_filter = ""
    params: list[Any] = [since_iso]
    if symbol_list:
        placeholders = ",".join("?" for _ in symbol_list)
        symbol_filter = f" AND UPPER(symbol) IN ({placeholders})"
        params.extend(symbol_list)

    if not _table_exists(db, "signal_observations"):
        return DataPipelineRootCauseReport(
            hours=int(hours),
            symbols=symbol_list,
            timeframes=timeframe_list,
            biggest_problem="signal_observations_table_missing",
            recommended_fix="ensure_database_initialised",
            classification_counts=[],
            notes=["table_missing_signal_observations"],
        )

    counts = _audit_signal_observations(db, since_iso, symbol_filter, params)
    sources = _split_by_source(db, since_iso, symbol_filter, params)
    benign = _benign_scan_repeats(db, since_iso, symbol_filter, params)
    danger = _dangerous_duplicates(db, since_iso, symbol_filter, params)
    label_dups = _label_duplicates(db, since_iso)
    orphans = _orphan_counts(db, since_iso)

    raw = counts["raw_sample_count"]
    agg_clean = counts["aggressive_clean_count"]
    src_clean = counts["source_adjusted_clean_count"]
    exact_dup = max(0, raw - src_clean)
    semantic_dup = max(0, src_clean - agg_clean)

    duplicate_rate = (raw - agg_clean) / raw if raw else 0.0
    src_adj_rate = (raw - src_clean) / raw if raw else 0.0
    benign_rate = benign / raw if raw else 0.0
    dangerous_rate = danger / raw if raw else 0.0
    clean_ratio = (agg_clean / raw) if raw else 0.0

    market_probe = sources["market_probe_count"]
    trade_signal = sources["trade_signal_count"]
    market_probe_clean = int(market_probe * clean_ratio)
    trade_signal_clean = int(trade_signal * clean_ratio)

    biggest, recommended, can_use = _classify_biggest_problem(
        raw=raw,
        aggressive_clean=agg_clean,
        source_adjusted_clean=src_clean,
        dangerous=danger,
        benign=benign,
        orphans=orphans["orphan_labels"] + orphans["orphan_path_metrics"],
        label_dups=label_dups,
        market_probe=market_probe,
        trade_signal=trade_signal,
    )

    classification = [
        DuplicateBucketCount("EXACT_DUPLICATE", exact_dup,
                             notes=["aggressive_minute_key_collapsed_rows"]),
        DuplicateBucketCount("SEMANTIC_DUPLICATE", semantic_dup,
                             notes=["source_or_setup_distinct_rows_collapsed_by_minute_key"]),
        DuplicateBucketCount("BENIGN_SCAN_REPEAT", benign,
                             notes=["worker_re-scanned_same_candle"]),
        DuplicateBucketCount("DANGEROUS_DUPLICATE", danger,
                             notes=["multi-write_with_identical_setup_and_price"]),
        DuplicateBucketCount("LABEL_DUPLICATE", label_dups,
                             notes=["multiple_labels_per_observation_id"]),
        DuplicateBucketCount("ORPHAN_LABEL", orphans["orphan_labels"]),
        DuplicateBucketCount("ORPHAN_METRIC", orphans["orphan_path_metrics"]),
        DuplicateBucketCount("MARKET_PROBE_NOISE", market_probe,
                             notes=["never_actionable_research_only"]),
        DuplicateBucketCount("TRADE_SIGNAL_DUPLICATE",
                             max(0, trade_signal - trade_signal_clean),
                             notes=["dedupe_in_view_only_not_destructive"]),
    ]

    notes: list[str] = []
    if market_probe > 0:
        notes.append("market_probe_never_actionable_always_research")
    if trade_signal == 0 and market_probe > 0:
        notes.append("only_market_probe_rows_present_no_promotion_possible")
    notes.append("no_db_writes_performed")

    return DataPipelineRootCauseReport(
        hours=int(hours),
        symbols=symbol_list,
        timeframes=timeframe_list,
        raw_sample_count=raw,
        clean_sample_count=agg_clean,
        exact_duplicate_count=exact_dup,
        semantic_duplicate_count=semantic_dup,
        dangerous_duplicate_count=danger,
        benign_scan_repeat_count=benign,
        market_probe_count=market_probe,
        trade_signal_count=trade_signal,
        exact_duplicate_rate=src_adj_rate,
        semantic_duplicate_rate=(semantic_dup / raw) if raw else 0.0,
        dangerous_duplicate_rate=dangerous_rate,
        benign_scan_repeat_rate=benign_rate,
        clean_ratio=clean_ratio,
        source_adjusted_clean_count=src_clean,
        trade_signal_clean_count=trade_signal_clean,
        market_probe_clean_count=market_probe_clean,
        orphan_labels=orphans["orphan_labels"],
        orphan_path_metrics=orphans["orphan_path_metrics"],
        label_duplicates=label_dups,
        duplicate_key_is_too_aggressive=(src_clean > agg_clean * 1.05),
        biggest_problem=biggest,
        recommended_fix=recommended,
        can_use_for_strategy_eval=can_use and trade_signal_clean >= 50,
        classification_counts=classification,
        notes=notes,
    )


def render_data_pipeline_root_cause_text(report: DataPipelineRootCauseReport) -> str:
    lines = [
        "DATA PIPELINE ROOT CAUSE START",
        f"hours: {report.hours}",
        f"symbols: {','.join(report.symbols) if report.symbols else 'ALL'}",
        f"timeframes: {','.join(report.timeframes) if report.timeframes else 'ALL'}",
        f"raw_sample_count: {report.raw_sample_count}",
        f"clean_sample_count: {report.clean_sample_count}",
        f"source_adjusted_clean_count: {report.source_adjusted_clean_count}",
        f"exact_duplicate_count: {report.exact_duplicate_count}",
        f"semantic_duplicate_count: {report.semantic_duplicate_count}",
        f"dangerous_duplicate_count: {report.dangerous_duplicate_count}",
        f"benign_scan_repeat_count: {report.benign_scan_repeat_count}",
        f"market_probe_count: {report.market_probe_count}",
        f"trade_signal_count: {report.trade_signal_count}",
        f"trade_signal_clean_count: {report.trade_signal_clean_count}",
        f"orphan_labels: {report.orphan_labels}",
        f"orphan_path_metrics: {report.orphan_path_metrics}",
        f"label_duplicates: {report.label_duplicates}",
        f"clean_ratio: {report.clean_ratio:.4f}",
        f"duplicate_key_is_too_aggressive: {str(report.duplicate_key_is_too_aggressive).lower()}",
        f"biggest_problem: {report.biggest_problem}",
        f"recommended_fix: {report.recommended_fix}",
        f"can_use_for_strategy_eval: {str(report.can_use_for_strategy_eval).lower()}",
        "classification_counts:",
    ]
    for bucket in report.classification_counts:
        notes = ",".join(bucket.notes) if bucket.notes else "-"
        lines.append(f"- {bucket.bucket}: count={bucket.count} notes={notes}")
    if report.notes:
        lines.append("notes:")
        for note in report.notes:
            lines.append(f"- {note}")
    lines.extend([
        "no_db_writes: true",
        "research_only: true",
        "paper_filter_enabled: false",
        "can_send_real_orders: false",
        "no_private_endpoints_used: true",
        "final_recommendation: NO LIVE",
        "DATA PIPELINE ROOT CAUSE END",
    ])
    return "\n".join(lines)
