from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

from .config import BotConfig
from .database import Database
from .market_data import MarketSnapshot
from .signal_engine import Signal
from .utils import iso_utc, safe_float, safe_int


TP_THRESHOLDS = {
    "would_hit_tp_025": 0.25,
    "would_hit_tp_050": 0.50,
    "would_hit_tp_075": 0.75,
    "would_hit_tp_100": 1.00,
    "would_hit_tp_150": 1.50,
}
SL_THRESHOLDS = {
    "would_hit_sl_025": 0.25,
    "would_hit_sl_050": 0.50,
    "would_hit_sl_075": 0.75,
    "would_hit_sl_100": 1.00,
}


@dataclass(frozen=True)
class MfeMaeUpdateResult:
    active: int = 0
    matured: int = 0
    insufficient: int = 0
    created: int = 0
    coverage_pct: float = 0.0
    candidates_seen: int = 0
    candidates_tracked: int = 0
    skipped_low_score: int = 0
    skipped_no_price: int = 0
    skipped_duplicate: int = 0
    skipped_max_active: int = 0
    by_source: dict[str, int] | None = None


class MfeMaeTracker:
    """Tracks compact path metrics for research without storing candle arrays."""

    def __init__(self, config: BotConfig, db: Database, logger=None) -> None:
        self.config = config
        self.db = db
        self.logger = logger
        self.candidates_seen = 0
        self.candidates_tracked = 0
        self.skipped_low_score = 0
        self.skipped_no_price = 0
        self.skipped_duplicate = 0
        self.skipped_max_active = 0
        self.by_source: Counter[str] = Counter()

    def register_signal(
        self,
        *,
        observation_id: int | None,
        signal: Signal,
        snapshot: MarketSnapshot | None,
        market_regime: str,
        source: str = "trade_signal",
        reject_reason: str = "",
        force: bool = False,
    ) -> int:
        if not self.config.enable_mfe_mae_capture or not observation_id:
            return 0
        source = _source(source)
        if not self._source_enabled(source):
            return 0
        self.candidates_seen += 1
        score = safe_int(getattr(signal, "confidence_score", 0))
        side = str(getattr(signal, "side", "") or "").upper()
        threshold = self._threshold_for_source(source)
        if not force and score < threshold:
            self.skipped_low_score += 1
            return 0
        if side == "NO_TRADE" and not self.config.mfe_mae_track_no_trade:
            self.skipped_low_score += 1
            return 0
        if side not in {"LONG", "SHORT"}:
            self.skipped_low_score += 1
            return 0
        try:
            if self.db.signal_path_metric_exists(int(observation_id)):
                self.skipped_duplicate += 1
                return 0
            if self.db.count_active_signal_path_metrics() >= self.config.mfe_mae_max_active:
                self.skipped_max_active += 1
                return 0
        except Exception as exc:
            if self.logger:
                self.logger.warning("MFE/MAE precheck fallo sin detener worker: %s", exc)
        entry = safe_float(getattr(signal, "entry_price", 0.0))
        current = safe_float(getattr(snapshot, "current_price", 0.0) if snapshot else 0.0, entry)
        if entry <= 0 and current > 0:
            entry = current
        if entry <= 0:
            self.skipped_no_price += 1
            return 0
        payload = {
            "observation_id": int(observation_id),
            "symbol": getattr(signal, "symbol", ""),
            "side": side,
            "score": score,
            "score_bucket": score_bucket(score),
            "market_regime": str(market_regime or ""),
            "source": source,
            "reject_reason": str(reject_reason or "")[:300],
            "priority": _priority(source),
            "entry_price": entry,
            "current_price": current or entry,
            "max_favorable_pct": 0.0,
            "max_adverse_pct": 0.0,
            "final_return_pct": 0.0,
            "bars_tracked": 0,
            "bars_to_mfe": 0,
            "bars_to_mae": 0,
            "first_barrier_hit": "",
            "status": "active",
            "created_at": iso_utc(),
            "updated_at": iso_utc(),
        }
        try:
            metric_id = self.db.upsert_signal_path_metric(payload)
            if metric_id:
                self.candidates_tracked += 1
                self.by_source[source] += 1
            return metric_id
        except Exception as exc:
            if self.logger:
                self.logger.warning("MFE/MAE register fallo sin detener worker: %s", exc)
            return 0

    def update_active(self, snapshots: dict[str, MarketSnapshot]) -> MfeMaeUpdateResult:
        if not self.config.enable_mfe_mae_capture:
            return MfeMaeUpdateResult()
        try:
            rows = self.db.fetch_active_signal_path_metrics(limit=self.config.mfe_mae_batch_size)
        except Exception as exc:
            if self.logger:
                self.logger.warning("MFE/MAE fetch active fallo sin detener worker: %s", exc)
            return MfeMaeUpdateResult()
        matured = 0
        insufficient = 0
        active = 0
        for row in rows:
            observation_id = safe_int(row.get("observation_id"))
            symbol = str(row.get("symbol") or "")
            snapshot = snapshots.get(symbol)
            current = safe_float(getattr(snapshot, "current_price", 0.0) if snapshot else 0.0)
            if current <= 0:
                bars = safe_int(row.get("bars_tracked")) + 1
                if bars >= self.config.mfe_mae_max_bars:
                    self._safe_update(
                        observation_id,
                        status="insufficient_price_path_data",
                        bars_tracked=bars,
                        matured_at=iso_utc(),
                    )
                    insufficient += 1
                else:
                    self._safe_update(observation_id, bars_tracked=bars)
                    active += 1
                continue
            update = self._build_update(row, current)
            if update["status"] == "matured":
                matured += 1
            else:
                active += 1
            self._safe_update(observation_id, **update)
        return self.debug_result()

    def debug_result(self) -> MfeMaeUpdateResult:
        try:
            summary = self.db.get_signal_path_metrics_summary_since("1970-01-01T00:00:00+00:00")
        except Exception:
            summary = {}
        return MfeMaeUpdateResult(
            active=safe_int(summary.get("active_count")),
            matured=safe_int(summary.get("matured_count")),
            insufficient=safe_int(summary.get("insufficient_count")),
            created=0,
            coverage_pct=safe_float(summary.get("coverage_pct")),
            candidates_seen=self.candidates_seen,
            candidates_tracked=self.candidates_tracked,
            skipped_low_score=self.skipped_low_score,
            skipped_no_price=self.skipped_no_price,
            skipped_duplicate=self.skipped_duplicate,
            skipped_max_active=self.skipped_max_active,
            by_source=dict(self.by_source),
        )

    def debug_text(self) -> str:
        result = self.debug_result()
        by_source = result.by_source or {}
        source_text = ", ".join(f"{key}={value}" for key, value in sorted(by_source.items())) or "none"
        return "\n".join([
            "MFE_MAE DEBUG",
            f"- enabled={self.config.enable_mfe_mae_capture}",
            f"- active={result.active}",
            f"- matured={result.matured}",
            f"- insufficient={result.insufficient}",
            f"- coverage_pct={result.coverage_pct * 100:.1f}",
            f"- candidates_seen={result.candidates_seen}",
            f"- candidates_tracked={result.candidates_tracked}",
            f"- skipped_low_score={result.skipped_low_score}",
            f"- skipped_no_price={result.skipped_no_price}",
            f"- skipped_duplicate={result.skipped_duplicate}",
            f"- skipped_max_active={result.skipped_max_active}",
            f"- by_source: {source_text}",
        ])

    def _threshold_for_source(self, source: str) -> int:
        if source == "trade_signal":
            return int(self.config.mfe_mae_track_min_score)
        return int(self.config.mfe_mae_min_rejected_score)

    def _source_enabled(self, source: str) -> bool:
        if source == "trade_signal":
            return True
        if source == "edge_guard_block":
            return bool(self.config.mfe_mae_track_edge_guard_blocks)
        if source == "high_score_missed":
            return bool(self.config.mfe_mae_track_high_score_missed)
        if source == "regime_block":
            return bool(self.config.mfe_mae_track_regime_blocks)
        return bool(self.config.mfe_mae_track_rejected_signals)

    def _build_update(self, row: dict[str, Any], current_price: float) -> dict[str, Any]:
        entry = safe_float(row.get("entry_price"))
        side = str(row.get("side") or "").upper()
        bars = safe_int(row.get("bars_tracked")) + 1
        if entry <= 0:
            return {
                "current_price": current_price,
                "bars_tracked": bars,
                "status": "insufficient_price_path_data",
                "matured_at": iso_utc(),
            }
        raw_return = ((current_price - entry) / entry) * 100.0
        directional_return = raw_return if side == "LONG" else -raw_return
        old_mfe = safe_float(row.get("max_favorable_pct"))
        old_mae = safe_float(row.get("max_adverse_pct"))
        favorable = max(0.0, directional_return)
        adverse = max(0.0, -directional_return)
        max_favorable = max(old_mfe, favorable)
        max_adverse = max(old_mae, adverse)
        first_hit = str(row.get("first_barrier_hit") or "")
        if not first_hit:
            first_hit = _first_hit(max_favorable, max_adverse)
        status = "matured" if bars >= self.config.mfe_mae_max_bars else "active"
        update: dict[str, Any] = {
            "current_price": current_price,
            "max_favorable_pct": max_favorable,
            "max_adverse_pct": max_adverse,
            "final_return_pct": directional_return,
            "bars_tracked": bars,
            "bars_to_mfe": bars if max_favorable > old_mfe else safe_int(row.get("bars_to_mfe")),
            "bars_to_mae": bars if max_adverse > old_mae else safe_int(row.get("bars_to_mae")),
            "first_barrier_hit": first_hit,
            "status": status,
            "updated_at": iso_utc(),
        }
        if status == "matured":
            update["matured_at"] = iso_utc()
            if not update["first_barrier_hit"]:
                update["first_barrier_hit"] = "TIME"
        for column, threshold in TP_THRESHOLDS.items():
            update[column] = int(max_favorable >= threshold)
        for column, threshold in SL_THRESHOLDS.items():
            update[column] = int(max_adverse >= threshold)
        return update

    def _safe_update(self, observation_id: int, **updates: Any) -> None:
        if not observation_id:
            return
        try:
            self.db.update_signal_path_metric(observation_id, **updates)
        except Exception as exc:
            if self.logger:
                self.logger.warning("MFE/MAE update fallo obs=%s: %s", observation_id, exc)


def score_bucket(score: int) -> str:
    if score >= 95:
        return "95-100"
    if score >= 90:
        return "90-94"
    if score >= 80:
        return "80-89"
    if score >= 70:
        return "70-79"
    return "<70"


def _first_hit(max_favorable: float, max_adverse: float) -> str:
    if max_favorable >= 0.25 and max_adverse >= 0.25:
        return "TP_025" if max_favorable >= max_adverse else "SL_025"
    if max_favorable >= 0.25:
        return "TP_025"
    if max_adverse >= 0.25:
        return "SL_025"
    return ""


def _source(source: str) -> str:
    text = str(source or "trade_signal").strip().lower()
    return text or "trade_signal"


def _priority(source: str) -> int:
    return {
        "high_score_missed": 100,
        "edge_guard_block": 90,
        "allocator_reject": 80,
        "regime_block": 70,
        "risk_block": 60,
        "paper_open_fail": 50,
        "trade_signal": 10,
    }.get(source, 20)
