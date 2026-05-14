from __future__ import annotations

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


class MfeMaeTracker:
    """Tracks compact path metrics for research without storing candle arrays."""

    def __init__(self, config: BotConfig, db: Database, logger=None) -> None:
        self.config = config
        self.db = db
        self.logger = logger

    def register_signal(
        self,
        *,
        observation_id: int | None,
        signal: Signal,
        snapshot: MarketSnapshot | None,
        market_regime: str,
    ) -> int:
        if not self.config.enable_mfe_mae_capture or not observation_id:
            return 0
        score = safe_int(getattr(signal, "confidence_score", 0))
        side = str(getattr(signal, "side", "") or "").upper()
        if score < self.config.mfe_mae_track_min_score:
            return 0
        if side == "NO_TRADE" and not self.config.mfe_mae_track_no_trade:
            return 0
        if side not in {"LONG", "SHORT"}:
            return 0
        entry = safe_float(getattr(signal, "entry_price", 0.0))
        current = safe_float(getattr(snapshot, "current_price", 0.0) if snapshot else 0.0, entry)
        if entry <= 0:
            return 0
        payload = {
            "observation_id": int(observation_id),
            "symbol": getattr(signal, "symbol", ""),
            "side": side,
            "score": score,
            "score_bucket": score_bucket(score),
            "market_regime": str(market_regime or ""),
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
            return self.db.upsert_signal_path_metric(payload)
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
        total_done = matured + insufficient + active
        coverage = (matured + active) / max(total_done, 1) if total_done else 0.0
        return MfeMaeUpdateResult(active=active, matured=matured, insufficient=insufficient, coverage_pct=coverage)

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
