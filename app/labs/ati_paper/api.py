"""Read-only presentation API for the ATI paper simulation ledger."""

from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from . import ACCOUNT_ID, POLICY_VERSION, safety_envelope
from .config import load_config
from .executor import read_executor_status
from .ledger import AtiPaperLedger


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _parse_payload(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    if "payload_json" in result:
        try:
            result["payload"] = json.loads(str(result.pop("payload_json") or "{}"))
        except json.JSONDecodeError:
            result["payload"] = {"status": "INVALID_STORED_PAYLOAD"}
    return result


def _safe_base() -> dict[str, Any]:
    return {"schema": "ati_paper_api.v1", **safety_envelope()}


def account_payload(ledger: AtiPaperLedger | None = None) -> dict[str, Any]:
    ledger = ledger or AtiPaperLedger()
    account = ledger.account()
    config = load_config()
    if not account:
        return {**_safe_base(), "status": "NO_LEDGER", "account": None,
                "sizing": {"method": config.sizing_method,
                           "configured_position_fraction": config.position_fraction}}
    trades = ledger.rows("trades", limit=5000)
    today = datetime.now(timezone.utc).date()
    daily = sum(
        _finite(row.get("net_pnl")) for row in trades
        if str(row.get("exit_ts") or "")[:10] == today.isoformat()
    )
    exposure = sum(_finite(row.get("notional")) for row in ledger.open_positions())
    cumulative = (_finite(account["total_equity"]) / _finite(account["initial_balance"], 1.0) - 1.0) * 100.0
    return {
        **_safe_base(), "status": "OK", "account": account,
        "daily_pnl": daily, "cumulative_return_pct": cumulative,
        "open_exposure": exposure,
        "sizing": {
            "method": config.sizing_method,
            "configured_position_fraction": config.position_fraction,
            "compound_from": "realized_equity_before_entry",
            "uses_unrealized_pnl": False,
            "notional_multiplier": 1.0,
        },
    }


def positions_payload(ledger: AtiPaperLedger | None = None) -> dict[str, Any]:
    ledger = ledger or AtiPaperLedger()
    config = load_config()
    rows: list[dict[str, Any]] = []
    for position in ledger.open_positions():
        row = dict(position)
        signal = ledger.signal(str(row.get("signal_id") or "")) or {}
        last_price = _finite(row.get("last_price"))
        quantity = _finite(row.get("quantity"))
        estimated_exit_slippage = last_price * quantity * config.adverse_slippage_fraction
        adverse_exit = last_price * (
            1.0 - config.adverse_slippage_fraction
            if row.get("direction") == "LONG" else 1.0 + config.adverse_slippage_fraction
        )
        estimated_exit_fee = adverse_exit * quantity * config.exit_fee_fraction
        row.update({
            "ati_score": signal.get("ati_score"),
            "score_components_json": signal.get("score_components_json"),
            "support": signal.get("support"), "resistance": signal.get("resistance"),
            "decision_ts": signal.get("decision_ts"),
            "entry_reason": "ATI_V2_EXACT_TRIGGER_FORWARD_OBSERVED",
            "estimated_exit_fee": estimated_exit_fee,
            "estimated_exit_slippage": estimated_exit_slippage,
            "estimated_net_pnl": (
                _finite(row.get("unrealized_pnl")) - _finite(row.get("entry_fee"))
                - _finite(row.get("entry_slippage")) - estimated_exit_fee
                - estimated_exit_slippage
            ),
            "funding_status": config.funding_mode,
        })
        rows.append(row)
    return {**_safe_base(), "status": "OK", "positions": rows}


def trades_payload(ledger: AtiPaperLedger | None = None, *, limit: int = 500) -> dict[str, Any]:
    ledger = ledger or AtiPaperLedger()
    rows: list[dict[str, Any]] = []
    for trade in ledger.rows("trades", limit=limit):
        row = dict(trade)
        signal = ledger.signal(str(row.get("signal_id") or "")) or {}
        row.update({
            "ati_score": signal.get("ati_score"),
            "score_components_json": signal.get("score_components_json"),
            "support": signal.get("support"), "resistance": signal.get("resistance"),
            "decision_ts": signal.get("decision_ts"),
        })
        rows.append(row)
    return {**_safe_base(), "status": "OK", "trades": rows}


def equity_payload(ledger: AtiPaperLedger | None = None, *, limit: int = 2000) -> dict[str, Any]:
    ledger = ledger or AtiPaperLedger()
    rows = list(reversed(ledger.rows("equity_curve", limit=limit)))
    return {**_safe_base(), "status": "OK", "equity": rows}


def events_payload(ledger: AtiPaperLedger | None = None, *, limit: int = 300) -> dict[str, Any]:
    ledger = ledger or AtiPaperLedger()
    return {**_safe_base(), "status": "OK",
            "events": [_parse_payload(row) for row in ledger.rows("events", limit=limit)]}


def signals_payload(ledger: AtiPaperLedger | None = None, *, limit: int = 500) -> dict[str, Any]:
    ledger = ledger or AtiPaperLedger()
    return {**_safe_base(), "status": "OK", "signals": ledger.rows("signals", limit=limit)}


def performance_payload(ledger: AtiPaperLedger | None = None) -> dict[str, Any]:
    ledger = ledger or AtiPaperLedger()
    trades = list(reversed(ledger.rows("trades", limit=5000)))
    nets = [_finite(row.get("net_pnl")) for row in trades]
    wins = [value for value in nets if value > 0]
    losses = [value for value in nets if value < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    win_rate = len(wins) / len(nets) if nets else None
    avg_win = sum(wins) / len(wins) if wins else None
    avg_loss = sum(losses) / len(losses) if losses else None
    payoff = avg_win / abs(avg_loss) if avg_win is not None and avg_loss not in {None, 0} else None
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else None
    expectancy = sum(nets) / len(nets) if nets else None
    returns = [_finite(row.get("return_pct")) for row in trades]
    net_ev_pct = sum(returns) / len(returns) if returns else None
    mean = expectancy or 0.0
    if len(nets) >= 30:
        variance = sum((value - mean) ** 2 for value in nets) / (len(nets) - 1)
        half = 1.96 * math.sqrt(variance / len(nets))
        confidence = {"available": True, "mean_net_pnl_95pct": [mean - half, mean + half]}
    else:
        confidence = {"available": False, "reason": "SAMPLE_BELOW_30"}
    max_wins = max_losses = current_wins = current_losses = 0
    daily: dict[str, float] = defaultdict(float)
    for row, net in zip(trades, nets):
        if net > 0:
            current_wins += 1
            current_losses = 0
        elif net < 0:
            current_losses += 1
            current_wins = 0
        else:
            current_wins = current_losses = 0
        max_wins, max_losses = max(max_wins, current_wins), max(max_losses, current_losses)
        daily[str(row.get("exit_ts") or "")[:10]] += net
    account = ledger.account() or {}
    return {
        **_safe_base(), "status": "OK",
        "sample_size": len(trades), "sample_size_warning": len(trades) < 30,
        "total_trades": len(trades), "wins": len(wins), "losses": len(losses),
        "win_rate": win_rate, "profit_factor": profit_factor,
        "net_ev_pct": net_ev_pct, "expectancy_usdt": expectancy,
        "average_win": avg_win, "average_loss": avg_loss, "payoff_ratio": payoff,
        "max_consecutive_wins": max_wins, "max_consecutive_losses": max_losses,
        "fees": _finite(account.get("fees_total")),
        "slippage": _finite(account.get("slippage_total")),
        "funding": _finite(account.get("funding_total")),
        "max_drawdown_pct": _finite(account.get("max_drawdown_pct")) * 100.0,
        "average_holding_seconds": (
            sum(_finite(row.get("holding_seconds")) for row in trades) / len(trades)
            if trades else None
        ),
        "confidence": confidence,
        "daily_results": [{"date": key, "net_pnl": daily[key]} for key in sorted(daily)],
    }


def chart_payload(ledger: AtiPaperLedger | None = None, *, symbol: str = "BTCUSDT") -> dict[str, Any]:
    ledger = ledger or AtiPaperLedger()
    symbol = str(symbol).upper()
    bars = list(reversed(ledger.rows("market_bars", limit=500, symbol=symbol)))
    positions = [row for row in positions_payload(ledger)["positions"] if row.get("symbol") == symbol]
    trades = list(reversed(ledger.rows("trades", limit=200, symbol=symbol)))
    signals = list(reversed(ledger.rows("signals", limit=200, symbol=symbol)))
    return {**_safe_base(), "status": "OK", "symbol": symbol,
            "bars": bars, "positions": positions, "trades": trades, "signals": signals}


def health_payload(ledger: AtiPaperLedger | None = None) -> dict[str, Any]:
    ledger = ledger or AtiPaperLedger()
    status = read_executor_status()
    reconcile = ledger.reconcile()
    return {**_safe_base(), **status, "reconciliation": reconcile,
            "ledger_status": "READY" if ledger.account() else "NO_LEDGER"}


def dashboard_snapshot(ledger: AtiPaperLedger | None = None) -> dict[str, Any]:
    ledger = ledger or AtiPaperLedger()
    return {
        "account": account_payload(ledger), "positions": positions_payload(ledger),
        "trades": trades_payload(ledger, limit=100), "equity": equity_payload(ledger, limit=500),
        "events": events_payload(ledger, limit=100), "signals": signals_payload(ledger, limit=100),
        "health": health_payload(ledger), "performance": performance_payload(ledger),
        **_safe_base(),
    }


API_READERS = {
    "/api/ati-paper/account": account_payload,
    "/api/ati-paper/positions": positions_payload,
    "/api/ati-paper/trades": trades_payload,
    "/api/ati-paper/equity": equity_payload,
    "/api/ati-paper/events": events_payload,
    "/api/ati-paper/signals": signals_payload,
    "/api/ati-paper/health": health_payload,
    "/api/ati-paper/chart": chart_payload,
    "/api/ati-paper/performance": performance_payload,
}


def api_payload(path: str, query: dict[str, list[str]] | None = None) -> tuple[dict[str, Any], int]:
    reader = API_READERS.get(path)
    if reader is None:
        return {**_safe_base(), "error": "NOT_FOUND"}, 404
    try:
        if path == "/api/ati-paper/chart":
            symbol = ((query or {}).get("symbol") or ["BTCUSDT"])[0]
            return reader(symbol=symbol), 200
        return reader(), 200
    except Exception as exc:
        return {**_safe_base(), "status": "ERROR",
                "error": f"{type(exc).__name__}:{str(exc)[:240]}"}, 500
