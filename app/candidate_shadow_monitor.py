"""Candidate Shadow Monitor — register candidate setups in shadow.

NO order placement.
NO paper filter activation.
NO live activation.

Initial rule (configurable): register `BNBUSDT LONG RISK_ON score>=80` signals
in the `shadow_candidates` table. Later a separate process evaluates the
hypothetical outcome using OHLCV (via outcome_engine).

Disabled-by-default at config level: `enable_candidate_shadow_monitor=False`.
When enabled, the monitor only WRITES rows; it never reads orders, never
calls Bitget, and never changes anything in the trading runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from .outcome_engine import simulate_outcome_ohlcv
from .setup_key import build_setup_key
from .utils import iso_utc, safe_float, safe_int


FINAL_RECOMMENDATION = "NO LIVE"


@dataclass(frozen=True)
class CandidateRule:
    name: str
    symbol: str
    side: str
    regime: str
    min_score: int
    timeframe: str = "5m"

    def matches(self, *, symbol: str, side: str, regime: str, score: int) -> bool:
        return (
            str(symbol or "").upper() == self.symbol
            and str(side or "").upper() == self.side
            and str(regime or "").upper() == self.regime
            and int(score or 0) >= int(self.min_score or 0)
        )


# The single canonical rule we want to monitor right now.
DEFAULT_RULES: tuple[CandidateRule, ...] = (
    CandidateRule(
        name="bnb_long_risk_on_score80",
        symbol="BNBUSDT",
        side="LONG",
        regime="RISK_ON",
        min_score=80,
        timeframe="5m",
    ),
)


class CandidateShadowMonitor:
    """Append-only writer for shadow candidate observations."""

    def __init__(
        self,
        config: Any,
        db: Any,
        logger=None,
        rules: tuple[CandidateRule, ...] = DEFAULT_RULES,
    ) -> None:
        self.config = config
        self.db = db
        self.logger = logger
        self.rules = rules

    @property
    def enabled(self) -> bool:
        # Disabled by default. Activate by setting `enable_candidate_shadow_monitor=True`
        # in config (no impact on trading runtime: only writes shadow rows).
        return bool(getattr(self.config, "enable_candidate_shadow_monitor", False))

    def matching_rule(self, *, symbol: str, side: str, regime: str, score: int) -> CandidateRule | None:
        for rule in self.rules:
            if rule.matches(symbol=symbol, side=side, regime=regime, score=score):
                return rule
        return None

    def register_signal(
        self,
        *,
        observation_id: int | None,
        symbol: str,
        side: str,
        regime: str,
        score: int,
        timeframe: str,
        strategy: str,
        entry_price: float,
        stop_loss: float,
        take_profit_1: float,
        take_profit_2: float,
        signal_timestamp: Any | None = None,
        source: str = "trade_signal",
        cost_assumption_bps: float = 18.0,
    ) -> int:
        """If signal matches a rule, write it to shadow_candidates. Returns row id or 0."""
        if not self.enabled:
            return 0
        rule = self.matching_rule(symbol=symbol, side=side, regime=regime, score=score)
        if rule is None:
            return 0

        setup = build_setup_key(
            symbol=symbol,
            side=side,
            regime=regime,
            score=score,
            timeframe=timeframe or rule.timeframe,
            strategy=strategy,
            exit_policy="current_exit",
            source=source,
        )

        entry = safe_float(entry_price)
        tp1 = safe_float(take_profit_1)
        side_upper = setup.side
        if entry <= 0 or tp1 <= 0:
            expected_move_pct = 0.0
        elif side_upper == "LONG":
            expected_move_pct = (tp1 - entry) / entry * 100.0
        else:
            expected_move_pct = (entry - tp1) / entry * 100.0
        cost_pct = cost_assumption_bps / 100.0
        ratio = expected_move_pct / cost_pct if cost_pct > 0 else 0.0

        payload = {
            "created_at": iso_utc(),
            "signal_timestamp": str(signal_timestamp or iso_utc()),
            "observation_id": int(observation_id or 0),
            "symbol": setup.symbol,
            "side": setup.side,
            "regime": setup.regime,
            "score": int(score or 0),
            "score_bucket": setup.score_bucket,
            "timeframe": setup.timeframe,
            "strategy": setup.strategy,
            "source": setup.source,
            "setup_key": setup.as_string(),
            "entry_price": entry,
            "stop_loss": safe_float(stop_loss),
            "take_profit_1": tp1,
            "take_profit_2": safe_float(take_profit_2),
            "expected_move_pct": expected_move_pct,
            "expected_move_to_cost_ratio": ratio,
            "status": "PENDING",
        }
        new_id = self.db.record_shadow_candidate(payload)
        if self.logger:
            self.logger.info(
                "Candidate Shadow Monitor registered %s id=%s rule=%s score=%s",
                setup.as_string(),
                new_id,
                rule.name,
                score,
            )
        return int(new_id or 0)

    def evaluate_pending(
        self,
        *,
        ohlcv_loader,
        max_holding_bars: int = 30,
        slippage_bps: float = 3.0,
        cost_assumption_bps: float = 18.0,
    ) -> dict[str, Any]:
        """For each PENDING candidate, simulate hypothetical outcome using OHLCV.

        Pure research evaluation: writes outcome back to the shadow_candidates row.
        Does NOT execute orders. Does NOT touch paper_trader.
        """
        evaluated = 0
        skipped = 0
        errors: list[str] = []
        pending = self.db.fetch_shadow_candidates(status="PENDING", limit=2000)
        for row in pending:
            try:
                symbol = str(row.get("symbol"))
                timeframe = str(row.get("timeframe") or "5m")
                signal_ts = row.get("signal_timestamp")
                since = datetime.fromisoformat(str(signal_ts).replace("Z", "+00:00"))
                if since.tzinfo is None:
                    since = since.replace(tzinfo=timezone.utc)
                until = since + timedelta(hours=max(1, int(max_holding_bars or 30)) * _timeframe_hours(timeframe))
                load_result = ohlcv_loader.load_ohlcv(
                    symbols=[symbol],
                    timeframe=timeframe,
                    since=since,
                    until=until,
                )
                if load_result.status != "OK" or symbol not in load_result.frames_by_symbol:
                    skipped += 1
                    continue
                frame = load_result.frames_by_symbol[symbol]
                if "timestamp" in frame.columns:
                    future = frame[pd.to_datetime(frame["timestamp"], utc=True, errors="coerce") > pd.to_datetime(since, utc=True)]
                else:
                    future = frame
                if future.empty:
                    skipped += 1
                    continue
                outcome = simulate_outcome_ohlcv(
                    side=str(row.get("side")),
                    entry_price=safe_float(row.get("entry_price")),
                    stop_loss=safe_float(row.get("stop_loss")),
                    take_profit=safe_float(row.get("take_profit_1")),
                    candles=future.reset_index(drop=True),
                    max_holding_bars=max_holding_bars,
                    slippage_bps=slippage_bps,
                    entry_timestamp=signal_ts,
                )
                self.db.update_shadow_candidate_outcome(
                    safe_int(row.get("id")),
                    {
                        "outcome": "EVALUATED",
                        "exit_reason": outcome.exit_reason,
                        "gross_return_pct": outcome.gross_return_pct,
                        "net_return_pct": outcome.net_return_pct,
                        "total_cost_bps": outcome.total_cost_bps,
                        "bars_to_outcome": outcome.bars_to_outcome,
                        "mfe": outcome.mfe,
                        "mae": outcome.mae,
                        "evaluated_at": iso_utc(),
                        "status": "EVALUATED",
                        "notes": ";".join(outcome.notes) if outcome.notes else "",
                    },
                )
                evaluated += 1
            except Exception as exc:
                errors.append(f"id={row.get('id')}:{exc}")
                if self.logger:
                    self.logger.warning("Shadow eval failed for id=%s: %s", row.get("id"), exc)
        return {
            "pending_seen": len(pending),
            "evaluated": evaluated,
            "skipped": skipped,
            "errors": errors[:10],
            "final_recommendation": FINAL_RECOMMENDATION,
        }

    def summary(self, *, hours: int = 720) -> dict[str, Any]:
        """Aggregate report — totals, per-window samples, win/loss split."""
        since = (datetime.now(timezone.utc) - timedelta(hours=max(1, int(hours or 720)))).isoformat()
        rows = self.db.fetch_shadow_candidates(since_iso=since, limit=5000)
        total = len(rows)
        evaluated = [r for r in rows if str(r.get("status") or "").upper() == "EVALUATED"]
        pending = [r for r in rows if str(r.get("status") or "").upper() == "PENDING"]
        wins = [r for r in evaluated if safe_float(r.get("net_return_pct")) > 0]
        losses = [r for r in evaluated if safe_float(r.get("net_return_pct")) < 0]
        tp = sum(1 for r in evaluated if str(r.get("exit_reason") or "").upper() == "TAKE_PROFIT")
        sl = sum(1 for r in evaluated if str(r.get("exit_reason") or "").upper() == "STOP_LOSS")
        tm = sum(1 for r in evaluated if str(r.get("exit_reason") or "").upper() == "HORIZON_CLOSE")
        gross_sum = sum(safe_float(r.get("gross_return_pct")) for r in evaluated)
        net_sum = sum(safe_float(r.get("net_return_pct")) for r in evaluated)
        gains = sum(safe_float(r.get("net_return_pct")) for r in wins)
        losses_total = abs(sum(safe_float(r.get("net_return_pct")) for r in losses))

        return {
            "hours": int(hours),
            "total_candidates": total,
            "pending": len(pending),
            "evaluated": len(evaluated),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(evaluated) if evaluated else 0.0,
            "tp_count": tp,
            "sl_count": sl,
            "time_count": tm,
            "gross_ev_pct": gross_sum / len(evaluated) if evaluated else 0.0,
            "net_ev_pct": net_sum / len(evaluated) if evaluated else 0.0,
            "net_pf": gains / losses_total if losses_total > 0 else (999.0 if gains > 0 else 0.0),
            "last_candidate_timestamp": rows[0].get("signal_timestamp") if rows else "",
            "paper_filter_enabled": False,
            "live_enabled": False,
            "research_only": True,
            "final_recommendation": FINAL_RECOMMENDATION,
        }


def _timeframe_hours(timeframe: str) -> float:
    text = str(timeframe or "").lower().strip()
    if text.endswith("m"):
        try:
            return int(text[:-1]) / 60.0
        except ValueError:
            return 1.0
    if text.endswith("h"):
        try:
            return float(int(text[:-1]))
        except ValueError:
            return 1.0
    if text.endswith("d"):
        return 24.0
    return 1.0


def render_summary_text(summary: dict[str, Any]) -> str:
    lines = ["CANDIDATE SHADOW MONITOR SUMMARY START"]
    lines.append(f"hours: {summary.get('hours', 0)}")
    lines.append(f"total_candidates: {summary.get('total_candidates', 0)}")
    lines.append(f"pending: {summary.get('pending', 0)}")
    lines.append(f"evaluated: {summary.get('evaluated', 0)}")
    lines.append(f"wins: {summary.get('wins', 0)}")
    lines.append(f"losses: {summary.get('losses', 0)}")
    lines.append(f"win_rate: {summary.get('win_rate', 0):.4f}")
    lines.append(f"tp_count: {summary.get('tp_count', 0)}")
    lines.append(f"sl_count: {summary.get('sl_count', 0)}")
    lines.append(f"time_count: {summary.get('time_count', 0)}")
    lines.append(f"gross_ev_pct: {summary.get('gross_ev_pct', 0):.4f}")
    lines.append(f"net_ev_pct: {summary.get('net_ev_pct', 0):.4f}")
    lines.append(f"net_pf: {summary.get('net_pf', 0):.4f}")
    lines.append(f"last_candidate_timestamp: {summary.get('last_candidate_timestamp', '')}")
    lines.append("paper_filter_enabled: false")
    lines.append("live_enabled: false")
    lines.append("research_only: true")
    lines.append(f"final_recommendation: {summary.get('final_recommendation', FINAL_RECOMMENDATION)}")
    lines.append("CANDIDATE SHADOW MONITOR SUMMARY END")
    return "\n".join(lines)
