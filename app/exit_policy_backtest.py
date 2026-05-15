from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .catalyst_registry import edge_metrics
from .config import BotConfig
from .database import Database
from .utils import safe_float, safe_int


START = "EXIT POLICY BACKTEST START"
END = "EXIT POLICY BACKTEST END"


class ExitPolicyBacktest:
    """Research-only comparison of exit variants using compact labels and MFE/MAE metrics."""

    def __init__(self, config: BotConfig, db: Database) -> None:
        self.config = config
        self.db = db

    def build(self, *, hours: int = 24) -> dict[str, Any]:
        hours = max(1, int(hours or 24))
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        labels = self.db.fetch_labeled_signal_rows_since(since, limit=50000) if hasattr(self.db, "fetch_labeled_signal_rows_since") else []
        paths = self.db.fetch_signal_path_metrics_since(since, limit=50000) if hasattr(self.db, "fetch_signal_path_metrics_since") else []
        baseline = edge_metrics(labels)
        variants = [
            self._variant_from_labels("current_exit", labels),
            self._variant_from_paths("early_exit_no_mfe_5", paths, tp_pct=0.25, sl_pct=0.75, max_bars=5),
            self._variant_from_paths("early_exit_no_mfe_10", paths, tp_pct=0.25, sl_pct=0.75, max_bars=10),
            self._variant_from_paths("profit_lock_after_mfe_050", paths, tp_pct=0.50, sl_pct=0.50, max_bars=20),
            self._variant_from_paths("shorten_hold_time_death", paths, tp_pct=0.50, sl_pct=0.75, max_bars=20),
            self._variant_from_paths("tp_050_sl_075", paths, tp_pct=0.50, sl_pct=0.75, max_bars=30),
            self._variant_from_paths("tp_075_sl_075", paths, tp_pct=0.75, sl_pct=0.75, max_bars=30),
        ]
        variants = [row for row in variants if safe_int(row.get("samples")) > 0]
        variants.sort(key=lambda row: (safe_float(row.get("profit_factor")), safe_float(row.get("tp_ratio"))), reverse=True)
        return {
            "hours": hours,
            "baseline": baseline,
            "variants": variants,
            "best_by_group": _best_by_group(paths),
            "recommendation": ["research_only", "no live"],
            "final_recommendation": "NO LIVE",
        }

    def to_text(self, *, hours: int = 24) -> str:
        payload = self.build(hours=hours)
        return "\n".join([
            START,
            f"hours: {payload['hours']}",
            "baseline:",
            _metrics_line(payload["baseline"]),
            "variants:",
            *_variant_lines(payload["variants"]),
            "best_by_group:",
            *_group_lines(payload["best_by_group"]),
            "recommendation:",
            "- research_only",
            "- no live",
            END,
        ])

    @staticmethod
    def _variant_from_labels(name: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
        metrics = edge_metrics(rows)
        return {"name": name, **metrics, "max_drawdown_proxy": _drawdown_proxy(rows), "accepted_count": safe_int(metrics.get("samples"))}

    @staticmethod
    def _variant_from_paths(name: str, rows: list[dict[str, Any]], *, tp_pct: float, sl_pct: float, max_bars: int) -> dict[str, Any]:
        returns: list[float] = []
        for row in rows:
            if str(row.get("status") or "") not in {"matured", "expired"}:
                continue
            bars = safe_int(row.get("bars_tracked"))
            mfe = safe_float(row.get("max_favorable_pct"))
            mae = safe_float(row.get("max_adverse_pct"))
            final_return = safe_float(row.get("final_return_pct"))
            if mfe >= tp_pct:
                returns.append(tp_pct)
            elif mae >= sl_pct:
                returns.append(-sl_pct)
            elif bars >= max_bars:
                returns.append(max(min(final_return, tp_pct), -sl_pct))
            else:
                returns.append(final_return)
        return _metrics_from_returns(name, returns)


def _metrics_from_returns(name: str, returns: list[float]) -> dict[str, Any]:
    gains = sum(value for value in returns if value > 0)
    losses = abs(sum(value for value in returns if value < 0))
    tp = sum(1 for value in returns if value > 0)
    sl = sum(1 for value in returns if value < 0)
    time_count = sum(1 for value in returns if value == 0)
    total = len(returns)
    return {
        "name": name,
        "samples": total,
        "profit_factor": gains / losses if losses > 0 else 999.0 if gains > 0 else 0.0,
        "expectancy": sum(returns) / max(total, 1),
        "tp_ratio": tp / max(total, 1),
        "sl_ratio": sl / max(total, 1),
        "time_ratio": time_count / max(total, 1),
        "max_drawdown_proxy": _drawdown_returns(returns),
        "accepted_count": total,
    }


def _drawdown_proxy(rows: list[dict[str, Any]]) -> float:
    return _drawdown_returns([safe_float(row.get("realized_return_pct")) for row in rows])


def _drawdown_returns(returns: list[float]) -> float:
    equity = peak = drawdown = 0.0
    for value in returns:
        equity += value
        peak = max(peak, equity)
        drawdown = min(drawdown, equity - peak)
    return abs(drawdown)


def _best_by_group(paths: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for key in ("symbol", "market_regime", "side"):
        buckets: dict[str, list[dict[str, Any]]] = {}
        for row in paths:
            buckets.setdefault(str(row.get(key) or "NA"), []).append(row)
        for value, rows in buckets.items():
            if len(rows) < 5:
                continue
            best = ExitPolicyBacktest._variant_from_paths(f"{key}_{value}_tp050_sl075", rows, tp_pct=0.50, sl_pct=0.75, max_bars=20)
            groups.append({"group_key": key, "group_value": value, **best})
    groups.sort(key=lambda row: safe_float(row.get("profit_factor")), reverse=True)
    return groups[:10]


def _metrics_line(metrics: dict[str, Any]) -> str:
    return (
        f"- samples={safe_int(metrics.get('samples'))} PF={safe_float(metrics.get('profit_factor')):.2f} "
        f"TP%={safe_float(metrics.get('tp_ratio')) * 100:.1f} SL%={safe_float(metrics.get('sl_ratio')) * 100:.1f} "
        f"TIME%={safe_float(metrics.get('time_ratio')) * 100:.1f}"
    )


def _variant_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- none"]
    return [
        (
            f"- name={row.get('name')} samples={safe_int(row.get('samples'))} PF={safe_float(row.get('profit_factor')):.2f} "
            f"TP%={safe_float(row.get('tp_ratio')) * 100:.1f} SL%={safe_float(row.get('sl_ratio')) * 100:.1f} "
            f"TIME%={safe_float(row.get('time_ratio')) * 100:.1f} max_drawdown_proxy={safe_float(row.get('max_drawdown_proxy')):.2f}"
        )
        for row in rows[:10]
    ]


def _group_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- none"]
    return [
        (
            f"- {row.get('group_key')}={row.get('group_value')} best={row.get('name')} "
            f"PF={safe_float(row.get('profit_factor')):.2f} samples={safe_int(row.get('samples'))}"
        )
        for row in rows[:10]
    ]
