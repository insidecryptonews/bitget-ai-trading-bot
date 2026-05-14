from __future__ import annotations

from datetime import datetime, timedelta, timezone
from itertools import product
from typing import Any

from .config import BotConfig
from .database import Database
from .utils import safe_float, safe_int


START = "EXIT SIMULATION START"
END = "EXIT SIMULATION END"
TP_VALUES = [0.25, 0.50, 0.75, 1.00, 1.50]
SL_VALUES = [0.25, 0.50, 0.75, 1.00]
HOLDING_VALUES = [5, 10, 20, 30]


class ExitSimulationLab:
    """Research-only exit simulation using compact MFE/MAE metrics."""

    def __init__(self, config: BotConfig, db: Database) -> None:
        self.config = config
        self.db = db

    def build(self, *, hours: int = 24) -> dict[str, Any]:
        hours = max(1, int(hours or 24))
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        try:
            summary = self.db.get_signal_path_metrics_summary_since(since)
            rows = self.db.fetch_signal_path_metrics_since(since, limit=50000)
        except Exception:
            summary = {"total": 0, "active_count": 0, "matured_count": 0, "insufficient_count": 0, "coverage_pct": 0.0}
            rows = []
            status = "no_mfe_mae_table"
        else:
            status = _data_status(summary, rows)
        usable = [row for row in rows if str(row.get("status") or "") == "matured"]
        if status != "ok" or len(usable) < 25:
            if status == "ok":
                status = "insufficient_matured_samples"
            return {
                "hours": hours,
                "samples": len(usable),
                "coverage": summary,
                "status": status,
                "current": _empty_metrics(),
                "best_exit_candidates": [],
                "worst_exit_candidates": [],
                "by_symbol_best": [],
                "by_regime_best": [],
                "score_bucket_best": [],
                "suggested_shadow_exit": "collect_mfe_mae_data",
                "final_recommendation": "NO LIVE",
            }
        candidates = [_simulate_combo(usable, tp, sl, holding) for tp, sl, holding in product(TP_VALUES, SL_VALUES, HOLDING_VALUES)]
        candidates.sort(key=lambda item: (safe_float(item.get("profit_factor")), safe_float(item.get("expectancy"))), reverse=True)
        best = candidates[:8]
        worst = list(reversed(candidates[-8:]))
        return {
            "hours": hours,
            "samples": len(usable),
            "coverage": summary,
            "status": "ok",
            "current": _current_from_rows(usable),
            "best_exit_candidates": best,
            "worst_exit_candidates": worst,
            "by_symbol_best": _best_by(usable, "symbol"),
            "by_regime_best": _best_by(usable, "market_regime"),
            "score_bucket_best": _best_by(usable, "score_bucket"),
            "suggested_shadow_exit": _candidate_name(best[0]) if best else "none",
            "final_recommendation": "NO LIVE",
        }

    def to_text(self, *, hours: int = 24) -> str:
        payload = self.build(hours=hours)
        current = payload["current"]
        lines = [
            START,
            f"hours: {payload['hours']}",
            f"samples: {payload['samples']}",
            f"status: {payload['status']}",
            "coverage:",
            (
                f"- path_metrics={safe_int(payload['coverage'].get('total'))} "
                f"active={safe_int(payload['coverage'].get('active_count'))} "
                f"matured={safe_int(payload['coverage'].get('matured_count'))} "
                f"insufficient={safe_int(payload['coverage'].get('insufficient_count'))} "
                f"coverage={safe_float(payload['coverage'].get('coverage_pct')) * 100:.1f}%"
            ),
            "current:",
            (
                f"- PF={current['profit_factor']:.2f} "
                f"TP%={current['tp_ratio'] * 100:.1f} "
                f"SL%={current['sl_ratio'] * 100:.1f} "
                f"TIME%={current['time_ratio'] * 100:.1f}"
            ),
        ]
        if payload["status"] != "ok":
            lines.extend([
                "recommendation:",
                *_recommendation_for_status(payload["status"]),
                "final_recommendation: NO LIVE",
                END,
            ])
            return "\n".join(lines)
        lines.extend([
            "best_exit_candidates:",
            *_candidate_lines(payload["best_exit_candidates"]),
            "worst_exit_candidates:",
            *_candidate_lines(payload["worst_exit_candidates"]),
            "by_symbol_best:",
            *_candidate_lines(payload["by_symbol_best"]),
            "by_regime_best:",
            *_candidate_lines(payload["by_regime_best"]),
            "score_bucket_best:",
            *_candidate_lines(payload["score_bucket_best"]),
            "recommendation:",
            f"- suggested_shadow_exit={payload['suggested_shadow_exit']}",
            "- NO LIVE",
            END,
        ])
        return "\n".join(lines)


def _simulate_combo(rows: list[dict[str, Any]], tp_pct: float, sl_pct: float, holding: int) -> dict[str, Any]:
    returns: list[float] = []
    tp_count = sl_count = time_count = 0
    for row in rows:
        mfe = safe_float(row.get("max_favorable_pct"))
        mae = safe_float(row.get("max_adverse_pct"))
        bars = safe_int(row.get("bars_tracked"))
        final_return = safe_float(row.get("final_return_pct"))
        # Conservative ordering: if both barriers were reachable, count SL first.
        if mae >= sl_pct:
            returns.append(-sl_pct)
            sl_count += 1
        elif mfe >= tp_pct:
            returns.append(tp_pct)
            tp_count += 1
        elif bars >= holding:
            returns.append(final_return)
            time_count += 1
        else:
            returns.append(final_return)
            time_count += 1
    metrics = _metrics_from_returns(returns, tp_count, sl_count, time_count)
    metrics.update({"tp_pct": tp_pct, "sl_pct": sl_pct, "holding_bars": holding, "name": f"TP={tp_pct:.2f}% SL={sl_pct:.2f}% HOLD={holding}"})
    return metrics


def _data_status(summary: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    total = safe_int(summary.get("total"))
    active = safe_int(summary.get("active_count"))
    matured = safe_int(summary.get("matured_count"))
    insufficient = safe_int(summary.get("insufficient_count"))
    if total <= 0:
        return "table_exists_but_empty"
    if active > 0 and matured <= 0:
        return "only_active_not_matured"
    if insufficient >= total and total > 0:
        return "no_price_path_metrics"
    if matured < 25:
        return "insufficient_matured_samples"
    if not rows:
        return "no_price_path_metrics"
    return "ok"


def _recommendation_for_status(status: str) -> list[str]:
    if status == "only_active_not_matured":
        return ["- seguir capturando hasta que maduren las ventanas MFE/MAE"]
    if status == "table_exists_but_empty":
        return ["- no hubo señales elegibles desde el deploy o todas fueron score bajo"]
    if status == "no_price_path_metrics":
        return ["- revisar que llegan precios por simbolo para actualizar MFE/MAE"]
    if status == "insufficient_matured_samples":
        return ["- esperar mas muestras maduras antes de optimizar salidas"]
    if status == "no_mfe_mae_table":
        return ["- inicializar base de datos con la tabla signal_path_metrics"]
    return ["- seguir capturando MFE/MAE antes de ajustar TP/SL"]


def _best_by(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(str(row.get(key) or "NA"), []).append(row)
    best: list[dict[str, Any]] = []
    for value, group_rows in groups.items():
        if len(group_rows) < 25:
            continue
        candidates = [_simulate_combo(group_rows, tp, sl, 20) for tp, sl in product(TP_VALUES, SL_VALUES)]
        candidates.sort(key=lambda item: safe_float(item.get("profit_factor")), reverse=True)
        top = dict(candidates[0])
        top["group_value"] = value
        top["group_key"] = key
        best.append(top)
    best.sort(key=lambda item: (safe_float(item.get("profit_factor")), safe_int(item.get("samples"))), reverse=True)
    return best[:8]


def _current_from_rows(rows: list[dict[str, Any]]) -> dict[str, float]:
    returns = [safe_float(row.get("final_return_pct")) for row in rows]
    tp_count = sum(1 for row in rows if safe_float(row.get("max_favorable_pct")) >= 1.0)
    sl_count = sum(1 for row in rows if safe_float(row.get("max_adverse_pct")) >= 1.0)
    time_count = max(0, len(rows) - tp_count - sl_count)
    return _metrics_from_returns(returns, tp_count, sl_count, time_count)


def _metrics_from_returns(returns: list[float], tp_count: int, sl_count: int, time_count: int) -> dict[str, float]:
    gains = sum(value for value in returns if value > 0)
    losses = abs(sum(value for value in returns if value < 0))
    total = len(returns)
    return {
        "samples": float(total),
        "tp_count": float(tp_count),
        "sl_count": float(sl_count),
        "time_count": float(time_count),
        "profit_factor": gains / losses if losses > 0 else 999.0 if gains > 0 else 0.0,
        "expectancy": sum(returns) / max(total, 1),
        "tp_ratio": tp_count / max(total, 1),
        "sl_ratio": sl_count / max(total, 1),
        "time_ratio": time_count / max(total, 1),
    }


def _empty_metrics() -> dict[str, float]:
    return {"samples": 0.0, "profit_factor": 0.0, "expectancy": 0.0, "tp_ratio": 0.0, "sl_ratio": 0.0, "time_ratio": 0.0}


def _candidate_name(row: dict[str, Any]) -> str:
    return f"TP={safe_float(row.get('tp_pct')):.2f}_SL={safe_float(row.get('sl_pct')):.2f}_H={safe_int(row.get('holding_bars'))}"


def _candidate_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- none"]
    lines: list[str] = []
    for row in rows[:8]:
        prefix = f"{row.get('group_key')}={row.get('group_value')} " if row.get("group_key") else ""
        lines.append(
            f"- {prefix}{row.get('name')} samples={safe_int(row.get('samples'))} "
            f"PF={safe_float(row.get('profit_factor')):.2f} TP%={safe_float(row.get('tp_ratio')) * 100:.1f} "
            f"SL%={safe_float(row.get('sl_ratio')) * 100:.1f} TIME%={safe_float(row.get('time_ratio')) * 100:.1f}"
        )
    return lines
