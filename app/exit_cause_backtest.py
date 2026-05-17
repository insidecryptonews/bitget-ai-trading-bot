from __future__ import annotations

from typing import Any

from .edge_hardening_utils import FINAL_NO_LIVE, cost_config, format_num, format_pct, since_iso
from .utils import safe_float, safe_int


START = "EXIT CAUSE BACKTEST START"
END = "EXIT CAUSE BACKTEST END"


class ExitCauseBacktest:
    """Research-only exit variant testing focused on TIME death causes."""

    def __init__(self, config: Any, db: Any) -> None:
        self.config = config
        self.db = db

    def build(self, *, hours: int = 24) -> dict[str, Any]:
        since = since_iso(hours)
        paths = _safe(lambda: self.db.fetch_signal_path_metrics_since(since, limit=50000), [])
        variants = [
            _current(paths, self.config),
            _variant(paths, "lower_TP_only_if_MFE_supports", tp=0.25, sl=0.75, max_bars=safe_int(getattr(self.config, "max_holding_bars", 30))),
            _variant(paths, "early_exit_if_no_MFE_after_5_bars", tp=0.50, sl=0.75, max_bars=5, min_mfe=0.10),
            _variant(paths, "early_exit_if_no_MFE_after_10_bars", tp=0.50, sl=0.75, max_bars=10, min_mfe=0.15),
            _variant(paths, "profit_lock_after_MFE_0.25", tp=0.25, sl=0.50, max_bars=20, profit_lock=0.25),
            _variant(paths, "profit_lock_after_MFE_0.50", tp=0.50, sl=0.50, max_bars=20, profit_lock=0.50),
            _variant(paths, "max_hold_shortened", tp=0.50, sl=0.75, max_bars=15),
            _variant(paths, "max_hold_extended", tp=0.75, sl=0.75, max_bars=60),
            _variant(paths, "group_specific_exit", tp=0.50, sl=0.75, max_bars=20),
        ]
        costs = cost_config(self.config)
        for row in variants:
            _apply_costs(row, costs)
            row["decision"] = _decision(row, costs)
        variants.sort(key=lambda item: (safe_float(item.get("net_PF")), safe_float(item.get("net_EV"))), reverse=True)
        return {"hours": max(1, int(hours or 24)), "variants": variants, "final_recommendation": FINAL_NO_LIVE}

    def to_text(self, *, hours: int = 24) -> str:
        payload = self.build(hours=hours)
        return "\n".join([
            START,
            f"hours: {payload['hours']}",
            "variants:",
            *_variant_lines(payload["variants"]),
            "final_recommendation: NO LIVE",
            END,
        ])


def _current(paths: list[dict[str, Any]], config: Any) -> dict[str, Any]:
    returns = [safe_float(row.get("final_return_pct")) for row in paths if str(row.get("status")) in {"matured", "expired"}]
    return _metrics("current_exit", returns)


def _variant(
    paths: list[dict[str, Any]],
    name: str,
    *,
    tp: float,
    sl: float,
    max_bars: int,
    min_mfe: float = 0.0,
    profit_lock: float = 0.0,
) -> dict[str, Any]:
    returns = []
    for row in paths:
        if str(row.get("status")) not in {"matured", "expired"}:
            continue
        mfe = safe_float(row.get("max_favorable_pct"))
        mae = safe_float(row.get("max_adverse_pct"))
        bars = safe_int(row.get("bars_tracked"))
        final = safe_float(row.get("final_return_pct"))
        if profit_lock and mfe >= profit_lock and final < profit_lock:
            returns.append(profit_lock * 0.50)
        elif mfe >= tp:
            returns.append(tp)
        elif mae >= sl:
            returns.append(-sl)
        elif min_mfe and bars >= max_bars and mfe < min_mfe:
            returns.append(min(0.0, final))
        elif bars >= max_bars:
            returns.append(max(min(final, tp), -sl))
        else:
            returns.append(final)
    row = _metrics(name, returns)
    row["tp_pct"] = tp
    row["sl_pct"] = sl
    row["max_bars"] = max_bars
    return row


def _metrics(name: str, returns: list[float]) -> dict[str, Any]:
    total = len(returns)
    gains = sum(value for value in returns if value > 0)
    losses = abs(sum(value for value in returns if value < 0))
    tp = sum(1 for value in returns if value > 0)
    sl = sum(1 for value in returns if value < 0)
    time_count = sum(1 for value in returns if value == 0)
    return {
        "name": name,
        "samples": total,
        "gross_PF": gains / losses if losses > 0 else 999.0 if gains > 0 else 0.0,
        "gross_expectancy": sum(returns) / max(total, 1),
        "TP_ratio": tp / max(total, 1),
        "SL_ratio": sl / max(total, 1),
        "TIME_ratio": time_count / max(total, 1),
        "drawdown_proxy": _drawdown(returns),
    }


def _apply_costs(row: dict[str, Any], costs: Any) -> None:
    cost = (2 * costs.taker_fee_bps + 2 * costs.slippage_bps + costs.funding_bps_per_8h) / 100.0
    row["net_EV"] = safe_float(row.get("gross_expectancy")) - cost
    row["net_PF"] = max(0.0, safe_float(row.get("gross_PF")) - cost)


def _decision(row: dict[str, Any], costs: Any) -> str:
    if safe_int(row.get("samples")) < costs.min_samples:
        return "WATCH_ONLY"
    if safe_float(row.get("net_EV")) <= 0:
        return "REJECT"
    if safe_float(row.get("TIME_ratio")) > costs.max_time_ratio and safe_float(row.get("TP_ratio")) < costs.min_tp_ratio:
        return "REJECT"
    if safe_float(row.get("net_PF")) >= costs.min_net_pf:
        return "SHADOW_EXIT_TEST"
    return "WATCH_ONLY"


def _drawdown(values: list[float]) -> float:
    equity = peak = drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = min(drawdown, equity - peak)
    return abs(drawdown)


def _safe(fn, fallback):
    try:
        return fn()
    except Exception:
        return fallback


def _variant_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- none"]
    return [
        (
            f"- name={row.get('name')} samples={row.get('samples')} gross_PF={format_num(row.get('gross_PF'))} "
            f"net_PF={format_num(row.get('net_PF'))} net_EV={format_num(row.get('net_EV'), 4)} "
            f"TP={format_pct(row.get('TP_ratio'))} SL={format_pct(row.get('SL_ratio'))} TIME={format_pct(row.get('TIME_ratio'))} "
            f"drawdown_proxy={format_num(row.get('drawdown_proxy'))} decision={row.get('decision')}"
        )
        for row in rows[:12]
    ]
