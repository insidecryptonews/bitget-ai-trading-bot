from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .config import BotConfig
from .database import Database
from .utils import safe_float, safe_int


START = "TIME DEATH LAB START"
END = "TIME DEATH LAB END"


class TimeDeathLab:
    """Research-only analysis of labels that expire by time instead of reaching TP/SL."""

    def __init__(self, config: BotConfig, db: Database) -> None:
        self.config = config
        self.db = db

    def build(self, *, hours: int = 24) -> dict[str, Any]:
        hours = max(1, int(hours or 24))
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        labels = self.db.fetch_labeled_signal_rows_since(since, limit=50000)
        paths = self.db.fetch_signal_path_metrics_since(since, limit=50000)
        overall = _metrics(labels)
        path_by_obs = {safe_int(row.get("observation_id")): row for row in paths}
        enriched = [_enrich_with_path(row, path_by_obs.get(safe_int(row.get("observation_id")))) for row in labels]
        return {
            "hours": hours,
            "overall": overall,
            "worst_time_groups": _worst_time_groups(enriched),
            "best_fast_move_groups": _best_fast_move_groups(paths),
            "decay_groups": _decay_groups(paths),
            "exit_recommendations": _exit_recommendations(overall, enriched, paths),
            "final_recommendation": "NO LIVE",
        }

    def to_text(self, *, hours: int = 24) -> str:
        payload = self.build(hours=hours)
        overall = payload["overall"]
        lines = [
            START,
            f"hours: {payload['hours']}",
            "overall:",
            f"- TIME%={overall['time_ratio'] * 100:.1f}",
            f"- PF={overall['profit_factor']:.2f}",
            f"- labels={safe_int(overall['total'])} TP={safe_int(overall['tp'])} SL={safe_int(overall['sl'])} TIME={safe_int(overall['time'])}",
            "worst_time_groups:",
            *_group_lines(payload["worst_time_groups"]),
            "best_fast_move_groups:",
            *_path_group_lines(payload["best_fast_move_groups"]),
            "decay_groups:",
            *_path_group_lines(payload["decay_groups"]),
            "exit_recommendations:",
            *[f"- {item}" for item in payload["exit_recommendations"]],
            "final_recommendation: NO LIVE",
            END,
        ]
        return "\n".join(lines)


def _metrics(rows: list[dict[str, Any]]) -> dict[str, float]:
    returns = [safe_float(row.get("realized_return_pct")) for row in rows]
    gains = sum(value for value in returns if value > 0)
    losses = abs(sum(value for value in returns if value < 0))
    total = len(rows)
    tp = sum(1 for row in rows if str(row.get("first_barrier_hit")) in {"TP1", "TP2"})
    sl = sum(1 for row in rows if str(row.get("first_barrier_hit")) == "SL")
    time_count = sum(1 for row in rows if str(row.get("first_barrier_hit")) == "TIME")
    return {
        "total": float(total),
        "tp": float(tp),
        "sl": float(sl),
        "time": float(time_count),
        "profit_factor": gains / losses if losses > 0 else 999.0 if gains > 0 else 0.0,
        "expectancy": sum(returns) / max(total, 1),
        "tp_ratio": tp / max(total, 1),
        "sl_ratio": sl / max(total, 1),
        "time_ratio": time_count / max(total, 1),
    }


def _enrich_with_path(label: dict[str, Any], path: dict[str, Any] | None) -> dict[str, Any]:
    row = dict(label)
    if path:
        for key in ("source", "max_favorable_pct", "max_adverse_pct", "bars_to_mfe", "bars_to_mae", "final_return_pct", "catalyst_active"):
            row[key] = path.get(key)
    row["source"] = row.get("source") or "signal_label"
    row["catalyst_group"] = "with_catalyst" if safe_int(row.get("catalyst_active")) else "without_catalyst"
    return row


def _worst_time_groups(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for key in ("symbol", "market_regime", "side", "score_bucket", "source", "catalyst_group"):
        buckets: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            buckets.setdefault(str(row.get(key) or "NA"), []).append(row)
        for value, group_rows in buckets.items():
            metrics = _metrics(group_rows)
            if metrics["total"] >= 10:
                groups.append({"group_key": key, "group_value": value, **metrics})
    groups.sort(key=lambda row: (safe_float(row.get("time_ratio")), safe_float(row.get("total"))), reverse=True)
    return groups[:10]


def _best_fast_move_groups(paths: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return _path_groups(paths, predicate=lambda row: safe_float(row.get("max_favorable_pct")) >= 0.50 and safe_int(row.get("bars_to_mfe")) <= 5)


def _decay_groups(paths: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return _path_groups(paths, predicate=lambda row: safe_float(row.get("max_favorable_pct")) >= 0.50 and safe_float(row.get("final_return_pct")) <= 0)


def _path_groups(paths: list[dict[str, Any]], predicate) -> list[dict[str, Any]]:
    rows = [row for row in paths if str(row.get("status")) == "matured"]
    out: list[dict[str, Any]] = []
    for key in ("symbol", "market_regime", "side", "score_bucket", "source"):
        buckets: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            buckets.setdefault(str(row.get(key) or "NA"), []).append(row)
        for value, group_rows in buckets.items():
            if len(group_rows) < 10:
                continue
            hits = [row for row in group_rows if predicate(row)]
            out.append({
                "group_key": key,
                "group_value": value,
                "samples": len(group_rows),
                "hit_ratio": len(hits) / max(len(group_rows), 1),
                "avg_mfe": sum(safe_float(row.get("max_favorable_pct")) for row in group_rows) / max(len(group_rows), 1),
                "avg_bars_to_mfe": sum(safe_float(row.get("bars_to_mfe")) for row in group_rows) / max(len(group_rows), 1),
            })
    out.sort(key=lambda row: (safe_float(row.get("hit_ratio")), safe_float(row.get("samples"))), reverse=True)
    return out[:10]


def _exit_recommendations(overall: dict[str, float], rows: list[dict[str, Any]], paths: list[dict[str, Any]]) -> list[str]:
    recommendations = ["NO LIVE"]
    if overall["time_ratio"] > 0.60:
        recommendations.append("shorten_hold_for=groups_with_TIME_above_60pct")
        recommendations.append("early_exit_if_no_mfe_after_bars=5_to_10")
    if overall["tp_ratio"] < 0.05:
        recommendations.append("review_tp_distance=TP parece demasiado lejos o la entrada no genera seguimiento")
    decay = [row for row in paths if safe_float(row.get("max_favorable_pct")) >= 0.50 and safe_float(row.get("final_return_pct")) <= 0]
    if decay:
        recommendations.append("decay_exit_if_mfe_reverts=probar profit-lock research-only")
    fast = [row for row in paths if safe_float(row.get("max_favorable_pct")) >= 0.50 and safe_int(row.get("bars_to_mfe")) <= 5]
    if fast:
        recommendations.append("profit_lock=probar cierre parcial si MFE temprano se evapora")
    if any(safe_int(row.get("catalyst_active")) for row in rows):
        recommendations.append("catalyst_expiry_exit=evitar mantener despues de la ventana catalyst")
    return recommendations


def _group_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- none"]
    return [
        (
            f"- {row.get('group_key')}={row.get('group_value')} labels={safe_int(row.get('total'))} "
            f"TIME%={safe_float(row.get('time_ratio')) * 100:.1f} PF={safe_float(row.get('profit_factor')):.2f}"
        )
        for row in rows[:8]
    ]


def _path_group_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- none"]
    return [
        (
            f"- {row.get('group_key')}={row.get('group_value')} samples={safe_int(row.get('samples'))} "
            f"ratio={safe_float(row.get('hit_ratio')) * 100:.1f} avg_mfe={safe_float(row.get('avg_mfe')):.2f}% "
            f"bars_to_mfe={safe_float(row.get('avg_bars_to_mfe')):.1f}"
        )
        for row in rows[:8]
    ]
