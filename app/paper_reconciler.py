from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .config import BotConfig
from .database import Database
from .utils import safe_float, safe_int, timeframe_to_seconds


@dataclass
class PaperReconcileResult:
    paper_open_before: int = 0
    stale_paper_trades_found: int = 0
    paper_trades_closed_by_label: int = 0
    paper_trades_closed_by_time: int = 0
    paper_trades_left_open: int = 0
    paper_open_after: int = 0
    errors: int = 0

    def to_text(self) -> str:
        return "\n".join(
            [
                "PAPER RECONCILE START",
                f"paper open before: {self.paper_open_before}",
                f"stale paper trades found: {self.stale_paper_trades_found}",
                f"paper trades closed by label: {self.paper_trades_closed_by_label}",
                f"paper trades closed by time: {self.paper_trades_closed_by_time}",
                f"paper trades left open: {self.paper_trades_left_open}",
                f"paper open after: {self.paper_open_after}",
                f"errors: {self.errors}",
                "PAPER RECONCILE END",
            ]
        )


class PaperReconciler:
    """Repairs PAPER-only trade state. It never touches live trades or exchange APIs."""

    def __init__(self, config: BotConfig, db: Database, logger=None) -> None:
        self.config = config
        self.db = db
        self.logger = logger

    def reconcile(self) -> PaperReconcileResult:
        result = PaperReconcileResult()
        open_trades = self._open_paper_trades()
        result.paper_open_before = len(open_trades)
        for trade in open_trades:
            try:
                label = self.db.find_label_for_paper_trade(trade)
                if label:
                    self._close_by_label(trade, label)
                    result.paper_trades_closed_by_label += 1
                    continue
                if self._is_stale(trade):
                    result.stale_paper_trades_found += 1
                    self._close_by_time(trade)
                    result.paper_trades_closed_by_time += 1
                    continue
                result.paper_trades_left_open += 1
            except Exception as exc:
                result.errors += 1
                self._warn("Paper reconcile fallo trade_id=%s: %s", trade.get("id"), exc)
        result.paper_open_after = len(self._open_paper_trades())
        return result

    def _open_paper_trades(self) -> list[dict[str, Any]]:
        try:
            return self.db.fetch_open_paper_trades()
        except Exception as exc:
            self._warn("Paper reconcile no pudo leer trades PAPER_OPEN: %s", exc)
            return []

    def _close_by_label(self, trade: dict[str, Any], label: dict[str, Any]) -> None:
        status = _status_from_barrier(str(label.get("first_barrier_hit") or "TIME"))
        realized = _pnl_from_label(trade, label)
        self.db.update_trade_status(
            safe_int(trade.get("id")),
            status,
            realized_pnl=realized,
            unrealized_pnl=0.0,
            error_message=f"paper_reconcile_label:{safe_int(label.get('label_id'))}",
        )

    def _close_by_time(self, trade: dict[str, Any]) -> None:
        self.db.update_trade_status(
            safe_int(trade.get("id")),
            "TIME_EXIT",
            realized_pnl=safe_float(trade.get("unrealized_pnl")),
            unrealized_pnl=0.0,
            error_message="paper_reconcile_stale_time",
        )

    def _is_stale(self, trade: dict[str, Any]) -> bool:
        timestamp = _parse_timestamp(trade.get("timestamp"))
        if timestamp is None:
            return True
        max_age_seconds = max(1, self.config.max_holding_bars) * timeframe_to_seconds(self.config.main_timeframe)
        age_seconds = (datetime.now(timezone.utc) - timestamp).total_seconds()
        return age_seconds >= max_age_seconds

    def _warn(self, message: str, *args: Any) -> None:
        if self.logger:
            self.logger.warning(message, *args)


def _status_from_barrier(barrier: str) -> str:
    barrier = barrier.upper()
    if barrier == "SL":
        return "STOP_LOSS"
    if barrier == "TP2":
        return "TAKE_PROFIT_2"
    if barrier == "TP1":
        return "TAKE_PROFIT_1"
    return "TIME_EXIT"


def _pnl_from_label(trade: dict[str, Any], label: dict[str, Any]) -> float:
    simulated = label.get("simulated_pnl")
    if simulated is not None:
        return safe_float(simulated)
    entry = safe_float(trade.get("entry"))
    size = safe_float(trade.get("size"))
    notional = abs(entry * size)
    return safe_float(label.get("realized_return_pct")) * notional


def _parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None
