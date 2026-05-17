from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from itertools import product
from statistics import median
from typing import Any

from .edge_hardening_utils import cost_config
from .utils import safe_float, safe_int


START = "EXIT LABEL CALIBRATION V2 START"
END = "EXIT LABEL CALIBRATION V2 END"
TP_VALUES = [0.15, 0.20, 0.25, 0.35, 0.50, 0.75, 1.00]
SL_VALUES = [0.25, 0.50, 0.75, 1.00]
HOLD_VALUES = [5, 10, 20, 30, 45, 60]
SOURCE_ORDER = (
    "trade_signal",
    "market_probe",
    "low_score_reject",
    "allocator_reject",
    "edge_guard_block",
    "paper_open_fail",
    "other",
)
ACTIONABLE_GROUPS = {"symbol_side_regime_score_bucket", "symbol_side_regime_strategy"}


class ExitLabelCalibrationV2:
    """Research-only calibration of labels, exits and path metrics.

    It never applies exits, never opens paper trades and never touches live execution.
    """

    def __init__(self, config: Any, db: Any) -> None:
        self.config = config
        self.db = db
        self.costs = cost_config(config)

    def build(self, *, hours: int = 24) -> dict[str, Any]:
        hours = max(1, int(hours or 24))
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        rows = self._rows_since(since)
        matured = [self._normalize_row(row) for row in rows if str(row.get("status") or "").lower() == "matured"]
        current = _current_metrics(matured, self.costs)
        source_comparison = [
            self._group_report(f"source={source}", _filter_source(matured, source), group_key="source", actionable=False)
            for source in SOURCE_ORDER
            if _filter_source(matured, source)
        ]
        grouped_reports = self._group_reports(matured)
        best_trade_signal = [
            row
            for row in grouped_reports
            if row.get("source") == "trade_signal" and row.get("decision") == "SHADOW_EXIT_CANDIDATE"
        ]
        best_trade_signal.sort(key=lambda item: (safe_float(item.get("best_shadow_net_PF")), safe_float(item.get("best_shadow_net_EV"))), reverse=True)
        rejected = [row for row in grouped_reports if row.get("decision") == "REJECT"]
        watch = [row for row in grouped_reports if row.get("decision") in {"WATCH_ONLY", "NEED_MORE_DATA"}]
        diagnosis = _diagnosis(current, source_comparison)
        return {
            "hours": hours,
            "samples": len(matured),
            "global_diagnosis": diagnosis,
            "current": current,
            "source_comparison": source_comparison,
            "best_trade_signal_shadow_exits": best_trade_signal[:10],
            "best_by_symbol": _top_by_group(grouped_reports, "symbol"),
            "best_by_regime": _top_by_group(grouped_reports, "market_regime"),
            "best_by_side": _top_by_group(grouped_reports, "side"),
            "rejected_exit_policies": rejected[:12],
            "watch_only_exit_policies": watch[:12],
            "recommended_shadow_tests": _recommended_shadow_tests(best_trade_signal, diagnosis),
            "do_not_apply": True,
            "final_recommendation": "NO LIVE",
        }

    def to_text(self, *, hours: int = 24) -> str:
        payload = self.build(hours=hours)
        lines = [
            START,
            f"hours: {payload['hours']}",
            f"samples: {payload['samples']}",
            "global_diagnosis:",
            *_bullet_lines(payload["global_diagnosis"]),
            "current:",
            _metrics_line(payload["current"]),
            "source_comparison:",
            *_policy_lines(payload["source_comparison"], limit=10),
            "best_trade_signal_shadow_exits:",
            *_policy_lines(payload["best_trade_signal_shadow_exits"], limit=10),
            "best_by_symbol:",
            *_policy_lines(payload["best_by_symbol"], limit=8),
            "best_by_regime:",
            *_policy_lines(payload["best_by_regime"], limit=8),
            "best_by_side:",
            *_policy_lines(payload["best_by_side"], limit=8),
            "rejected_exit_policies:",
            *_policy_lines(payload["rejected_exit_policies"], limit=8),
            "watch_only_exit_policies:",
            *_policy_lines(payload["watch_only_exit_policies"], limit=8),
            "recommended_shadow_tests:",
            *_bullet_lines(payload["recommended_shadow_tests"]),
            "do_not_apply: true",
            "final_recommendation: NO LIVE",
            END,
        ]
        return "\n".join(lines)

    def _rows_since(self, since: str) -> list[dict[str, Any]]:
        try:
            return self.db.fetch_signal_path_metrics_since(since, limit=50000)
        except Exception:
            return []

    def _normalize_row(self, row: dict[str, Any]) -> dict[str, Any]:
        out = dict(row)
        source = str(out.get("source") or "other").lower()
        if source not in SOURCE_ORDER:
            if "edge_guard" in source:
                source = "edge_guard_block"
            elif "allocator" in source:
                source = "allocator_reject"
            elif "probe" in source:
                source = "market_probe"
            elif "paper" in source and "fail" in source:
                source = "paper_open_fail"
            elif "trade" in source or "signal" in source:
                source = "trade_signal"
            else:
                source = "other"
        out["source"] = source
        out["symbol"] = str(out.get("symbol") or "NA")
        out["side"] = str(out.get("side") or "NA").upper()
        out["market_regime"] = str(out.get("market_regime") or "NA")
        out["score_bucket"] = str(out.get("score_bucket") or _bucket(safe_int(out.get("score"))))
        out["strategy"] = str(out.get("strategy") or out.get("strategy_type") or "NA")
        return out

    def _group_reports(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        specs = [
            ("symbol", lambda row: row["symbol"]),
            ("side", lambda row: row["side"]),
            ("market_regime", lambda row: row["market_regime"]),
            ("score_bucket", lambda row: row["score_bucket"]),
            ("strategy", lambda row: row["strategy"]),
            ("symbol_side_regime_score_bucket", lambda row: f"{row['symbol']}|{row['side']}|{row['market_regime']}|{row['score_bucket']}"),
            ("symbol_side_regime_strategy", lambda row: f"{row['symbol']}|{row['side']}|{row['market_regime']}|{row['strategy']}"),
            ("source_side_regime", lambda row: f"{row['source']}|{row['side']}|{row['market_regime']}"),
        ]
        reports: list[dict[str, Any]] = []
        for group_key, key_fn in specs:
            groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for row in rows:
                groups[str(key_fn(row))].append(row)
            for value, group_rows in groups.items():
                if len(group_rows) < 10 and group_key not in {"symbol_side_regime_score_bucket", "symbol_side_regime_strategy"}:
                    continue
                report = self._group_report(value, group_rows, group_key=group_key, actionable=group_key in ACTIONABLE_GROUPS)
                reports.append(report)
        reports.sort(key=lambda item: (safe_float(item.get("best_shadow_net_PF")), safe_float(item.get("best_shadow_net_EV"))), reverse=True)
        return reports

    def _group_report(self, group_value: str, rows: list[dict[str, Any]], *, group_key: str, actionable: bool) -> dict[str, Any]:
        current = _current_metrics(rows, self.costs)
        best = self._best_exit(rows)
        source = _dominant(rows, "source")
        decision, reason = _decision(
            group_key=group_key,
            source=source,
            samples=len(rows),
            current=current,
            best=best,
            costs=self.costs,
            actionable=actionable,
        )
        report = {
            "group_key": group_key,
            "group": group_value,
            "source": source,
            "symbol": _dominant(rows, "symbol"),
            "side": _dominant(rows, "side"),
            "market_regime": _dominant(rows, "market_regime"),
            "score_bucket": _dominant(rows, "score_bucket"),
            "strategy": _dominant(rows, "strategy"),
            "samples": len(rows),
            "current_TIME": current["time_ratio"],
            "current_TP": current["tp_ratio"],
            "current_SL": current["sl_ratio"],
            "current_gross_PF": current["gross_PF"],
            "current_net_PF": current["net_PF"],
            "current_net_EV": current["net_EV"],
            "avg_MFE": current["avg_MFE"],
            "median_MFE": current["median_MFE"],
            "avg_MAE": current["avg_MAE"],
            "median_MAE": current["median_MAE"],
            "bars_to_MFE": current["bars_to_MFE"],
            "bars_to_MAE": current["bars_to_MAE"],
            "best_shadow_exit": best["name"],
            "best_shadow_gross_PF": best["gross_PF"],
            "best_shadow_net_PF": best["net_PF"],
            "best_shadow_net_EV": best["net_EV"],
            "best_shadow_TP": best["tp_ratio"],
            "best_shadow_SL": best["sl_ratio"],
            "best_shadow_TIME": best["time_ratio"],
            "improvement_vs_current": best["net_EV"] - current["net_EV"],
            "drawdown_proxy": best["drawdown_proxy"],
            "confidence": _confidence(len(rows)),
            "deterioration_recent": False,
            "walk_forward_stability": "not_checked",
            "decision": decision,
            "reason": reason,
        }
        return report

    def _best_exit(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        if not rows:
            return _empty_exit()
        combos = [_simulate(rows, tp, sl, hold, self.costs) for tp, sl, hold in product(TP_VALUES, SL_VALUES, HOLD_VALUES)]
        combos.sort(key=lambda item: (safe_float(item.get("net_PF")), safe_float(item.get("net_EV"))), reverse=True)
        return combos[0] if combos else _empty_exit()


def _simulate(rows: list[dict[str, Any]], tp_pct: float, sl_pct: float, holding: int, costs: Any) -> dict[str, Any]:
    returns: list[float] = []
    tp_count = sl_count = time_count = 0
    for row in rows:
        mfe = safe_float(row.get("max_favorable_pct"))
        mae = safe_float(row.get("max_adverse_pct"))
        bars = safe_int(row.get("bars_tracked"))
        final_return = safe_float(row.get("final_return_pct"))
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
    metrics = _metrics_from_returns(returns, tp_count, sl_count, time_count, costs, holding=holding)
    metrics.update({"tp_pct": tp_pct, "sl_pct": sl_pct, "holding_bars": holding, "name": f"TP={tp_pct:.2f}% SL={sl_pct:.2f}% HOLD={holding}"})
    return metrics


def _current_metrics(rows: list[dict[str, Any]], costs: Any) -> dict[str, Any]:
    if not rows:
        out = _empty_exit()
        out.update({"avg_MFE": 0.0, "median_MFE": 0.0, "avg_MAE": 0.0, "median_MAE": 0.0, "bars_to_MFE": 0.0, "bars_to_MAE": 0.0})
        return out
    returns = [safe_float(row.get("final_return_pct")) for row in rows]
    tp_count = sl_count = time_count = 0
    for row in rows:
        hit = str(row.get("first_barrier_hit") or "").upper()
        if hit.startswith("TP"):
            tp_count += 1
        elif hit == "SL":
            sl_count += 1
        else:
            time_count += 1
    metrics = _metrics_from_returns(returns, tp_count, sl_count, time_count, costs, holding=30)
    mfe = [safe_float(row.get("max_favorable_pct")) for row in rows]
    mae = [safe_float(row.get("max_adverse_pct")) for row in rows]
    metrics.update(
        {
            "avg_MFE": sum(mfe) / max(len(mfe), 1),
            "median_MFE": median(mfe) if mfe else 0.0,
            "avg_MAE": sum(mae) / max(len(mae), 1),
            "median_MAE": median(mae) if mae else 0.0,
            "bars_to_MFE": sum(safe_float(row.get("bars_to_mfe")) for row in rows) / max(len(rows), 1),
            "bars_to_MAE": sum(safe_float(row.get("bars_to_mae")) for row in rows) / max(len(rows), 1),
        }
    )
    return metrics


def _metrics_from_returns(returns: list[float], tp_count: int, sl_count: int, time_count: int, costs: Any, *, holding: int) -> dict[str, Any]:
    samples = len(returns)
    gross_gains = sum(value for value in returns if value > 0)
    gross_losses = abs(sum(value for value in returns if value < 0))
    fee_pct = (2.0 * safe_float(costs.taker_fee_bps)) / 100.0
    slippage_pct = (2.0 * safe_float(costs.slippage_bps)) / 100.0
    funding_pct = max(0.0, (safe_float(holding) * 5.0 / 480.0) * safe_float(costs.funding_bps_per_8h) / 100.0)
    total_cost = fee_pct + slippage_pct + funding_pct
    net_returns = [value - total_cost for value in returns]
    net_gains = sum(value for value in net_returns if value > 0)
    net_losses = abs(sum(value for value in net_returns if value < 0))
    return {
        "samples": samples,
        "gross_PF": gross_gains / gross_losses if gross_losses > 0 else 999.0 if gross_gains > 0 else 0.0,
        "net_PF": net_gains / net_losses if net_losses > 0 else 999.0 if net_gains > 0 else 0.0,
        "gross_EV": sum(returns) / max(samples, 1),
        "net_EV": sum(net_returns) / max(samples, 1),
        "tp_ratio": tp_count / max(samples, 1),
        "sl_ratio": sl_count / max(samples, 1),
        "time_ratio": time_count / max(samples, 1),
        "drawdown_proxy": abs(min(net_returns)) if net_returns else 0.0,
        "estimated_fee_cost": fee_pct,
        "estimated_slippage_cost": slippage_pct,
        "estimated_funding_cost": funding_pct,
    }


def _decision(*, group_key: str, source: str, samples: int, current: dict[str, Any], best: dict[str, Any], costs: Any, actionable: bool) -> tuple[str, str]:
    if source == "market_probe":
        return "DO_NOT_USE_PROBES_FOR_POLICY", "market_probe_research_only"
    if samples < safe_int(costs.min_samples):
        return "NEED_MORE_DATA", "sample_too_small"
    if not actionable:
        return "WATCH_ONLY", "generic_group_not_actionable"
    if safe_float(best.get("net_EV")) <= 0:
        return "REJECT", "net_ev_not_positive"
    if safe_float(best.get("net_PF")) < safe_float(costs.min_net_pf):
        return "REJECT", "net_pf_below_min"
    if safe_float(best.get("time_ratio")) > safe_float(costs.max_time_ratio):
        return "REJECT", "time_still_too_high"
    if safe_float(best.get("tp_ratio")) <= safe_float(current.get("tp_ratio")) and safe_float(best.get("net_EV")) <= safe_float(current.get("net_EV")):
        return "REJECT", "no_real_improvement"
    if safe_float(best.get("drawdown_proxy")) > max(1.5, safe_float(current.get("drawdown_proxy")) * 1.5):
        return "REJECT", "drawdown_proxy_worse"
    if group_key in ACTIONABLE_GROUPS:
        return "SHADOW_EXIT_CANDIDATE", "trade_signal_shadow_exit_requires_validation"
    return "WATCH_ONLY", "research_only"


def _diagnosis(current: dict[str, Any], source_rows: list[dict[str, Any]]) -> list[str]:
    notes: list[str] = []
    if safe_float(current.get("time_ratio")) > 0.80:
        notes.append("TIME death alto: revisar TP/SL/HOLD por fuente antes de aplicar filtros")
    if safe_float(current.get("tp_ratio")) < 0.05:
        notes.append("TP bajo: el objetivo actual no se confirma en suficientes rutas")
    trade = next((row for row in source_rows if row.get("source") == "trade_signal"), None)
    probe = next((row for row in source_rows if row.get("source") == "market_probe"), None)
    if trade and probe and safe_float(trade.get("best_shadow_net_EV")) > safe_float(probe.get("best_shadow_net_EV")):
        notes.append("trade_signal mejor que probes: posible edge de entrada, aun requiere validacion neta")
    if not notes:
        notes.append("sin evidencia suficiente para cambiar exits")
    notes.append("Research only. No exits applied.")
    return notes


def _recommended_shadow_tests(rows: list[dict[str, Any]], diagnosis: list[str]) -> list[str]:
    if not rows:
        return ["- mantener observacion; no hay shadow exit candidate validado"]
    lines = []
    for row in rows[:5]:
        lines.append(f"- {row.get('group')} {row.get('best_shadow_exit')} net_EV={safe_float(row.get('best_shadow_net_EV')):.4f} decision={row.get('decision')}")
    if any("TIME death" in item for item in diagnosis):
        lines.append("- priorizar variantes que bajen TIME sin empeorar net_EV")
    return lines


def _top_by_group(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    filtered = [row for row in rows if row.get(key) not in {"", "NA", None}]
    filtered.sort(key=lambda item: (safe_float(item.get("best_shadow_net_PF")), safe_float(item.get("best_shadow_net_EV"))), reverse=True)
    seen = set()
    out = []
    for row in filtered:
        value = row.get(key)
        if value in seen:
            continue
        seen.add(value)
        out.append(row)
        if len(out) >= 8:
            break
    return out


def _filter_source(rows: list[dict[str, Any]], source: str) -> list[dict[str, Any]]:
    return [row for row in rows if row.get("source") == source]


def _dominant(rows: list[dict[str, Any]], key: str) -> str:
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        counts[str(row.get(key) or "NA")] += 1
    if not counts:
        return "NA"
    return sorted(counts.items(), key=lambda item: item[1], reverse=True)[0][0]


def _confidence(samples: int) -> str:
    if samples >= 1000:
        return "HIGH"
    if samples >= 500:
        return "MEDIUM"
    return "LOW"


def _bucket(score: int) -> str:
    if score >= 95:
        return "95-100"
    if score >= 90:
        return "90-94"
    if score >= 80:
        return "80-89"
    if score >= 70:
        return "70-79"
    if score >= 60:
        return "60-69"
    if score > 0:
        return "<60"
    return "PROBE"


def _empty_exit() -> dict[str, Any]:
    return {
        "samples": 0,
        "gross_PF": 0.0,
        "net_PF": 0.0,
        "gross_EV": 0.0,
        "net_EV": 0.0,
        "tp_ratio": 0.0,
        "sl_ratio": 0.0,
        "time_ratio": 0.0,
        "drawdown_proxy": 0.0,
        "name": "none",
    }


def _metrics_line(row: dict[str, Any]) -> str:
    return (
        f"- samples={safe_int(row.get('samples'))} gross_PF={safe_float(row.get('gross_PF')):.2f} "
        f"net_PF={safe_float(row.get('net_PF')):.2f} net_EV={safe_float(row.get('net_EV')):.4f} "
        f"TP%={safe_float(row.get('tp_ratio')) * 100:.1f} SL%={safe_float(row.get('sl_ratio')) * 100:.1f} "
        f"TIME%={safe_float(row.get('time_ratio')) * 100:.1f}"
    )


def _policy_lines(rows: list[dict[str, Any]], *, limit: int) -> list[str]:
    if not rows:
        return ["- none"]
    out = []
    for row in rows[:limit]:
        out.append(
            "- "
            f"group={row.get('group')} source={row.get('source')} samples={safe_int(row.get('samples'))} "
            f"current_TIME={safe_float(row.get('current_TIME')) * 100:.1f}% "
            f"best={row.get('best_shadow_exit')} net_PF={safe_float(row.get('best_shadow_net_PF')):.2f} "
            f"net_EV={safe_float(row.get('best_shadow_net_EV')):.4f} "
            f"shadow_TP={safe_float(row.get('best_shadow_TP')) * 100:.1f}% "
            f"shadow_SL={safe_float(row.get('best_shadow_SL')) * 100:.1f}% "
            f"shadow_TIME={safe_float(row.get('best_shadow_TIME')) * 100:.1f}% "
            f"decision={row.get('decision')} reason={row.get('reason')}"
        )
    return out


def _bullet_lines(rows: list[str]) -> list[str]:
    if not rows:
        return ["- none"]
    return [row if str(row).startswith("-") else f"- {row}" for row in rows]

