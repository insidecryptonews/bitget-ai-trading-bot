from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from .cost_model import MARKET_PROBE_ACTIONABILITY, calculate_net_metrics_for_returns, explain_cost_breakdown
from .edge_hardening_utils import cost_config
from .score_calibration import load_score_rows, metric_snapshot
from .utils import safe_float, safe_int


START = "CANDIDATE INCUBATOR START"
END = "CANDIDATE INCUBATOR END"
STATUSES = {"REJECT", "WATCH_ONLY", "NEED_MORE_DATA", "SHADOW_ONLY", "PAPER_CANDIDATE_DISABLED"}
CATEGORIES = {
    "RESEARCH_POCKET",
    "NEED_MORE_DATA_NOT_ACTIONABLE",
    "REJECT_BAD_EDGE",
    "REJECT_DATA_QUALITY",
    "WATCH_ONLY_TRADE_SIGNAL",
    "WATCH_ONLY_MARKET_PROBE",
}
PROMISING_KEYS = {"SHORT", "RISK_OFF", "TREND_DOWN", "ETHUSDT", "DOGEUSDT", "XRPUSDT", "BTCUSDT", "SOLUSDT"}
EXIT_PROFILES = {
    "current_exit": None,
    "tp050_sl075": (0.50, 0.75, 30),
    "tp050_sl050": (0.50, 0.50, 30),
    "tp075_sl075": (0.75, 0.75, 30),
    "tp100_sl050": (1.00, 0.50, 30),
    "hold_shorter": (0.50, 0.75, 10),
    "hold_longer": (0.50, 0.75, 60),
}


class CandidateIncubator:
    """Concrete setup incubator for research/shadow only.

    It never enables paper filters and never opens or closes orders.
    """

    def __init__(self, config: Any, db: Any) -> None:
        self.config = config
        self.db = db
        self.costs = cost_config(config)

    def build(self, *, hours: int = 24) -> dict[str, Any]:
        rows = load_score_rows(self.db, hours=hours)
        groups = _candidate_groups(rows)
        candidates = [self._candidate(group_rows) for group_rows in groups.values()]
        candidates.sort(key=lambda row: (status_rank(row.get("candidate_status")), safe_float(row.get("net_EV_est")), safe_float(row.get("net_PF_est"))), reverse=True)
        reject = [row for row in candidates if row.get("candidate_status") == "REJECT"]
        watch = [row for row in candidates if row.get("candidate_status") == "WATCH_ONLY"]
        more = [row for row in candidates if row.get("candidate_status") == "NEED_MORE_DATA"]
        shadow = [row for row in candidates if row.get("candidate_status") == "SHADOW_ONLY"]
        disabled = [row for row in candidates if row.get("candidate_status") == "PAPER_CANDIDATE_DISABLED"]
        category_counts = dict(Counter(str(row.get("candidate_category")) for row in candidates))
        return {
            "hours": max(1, int(hours or 24)),
            "total_candidates": len(candidates),
            "candidate_status_counts": dict(Counter(str(row.get("candidate_status")) for row in candidates)),
            "candidate_category_counts": category_counts,
            "candidates": candidates[:80],
            "top_reject": reject[:15],
            "top_watch_only": watch[:15],
            "top_need_more_data": more[:15],
            "top_shadow_only": shadow[:15],
            "disabled_paper_candidates": disabled[:10],
            "actionable_candidates": [],
            "promising_not_actionable": [row for row in candidates if row.get("candidate_category") in {"RESEARCH_POCKET", "NEED_MORE_DATA_NOT_ACTIONABLE"}][:20],
            "market_probe_only": [row for row in candidates if row.get("actionability") == MARKET_PROBE_ACTIONABILITY][:20],
            "reject_bad_edge": [row for row in candidates if row.get("candidate_category") == "REJECT_BAD_EDGE"][:20],
            "promising_raw_pockets": _promising_raw_pockets(candidates),
            "paper_filter_enabled": False,
            "do_not_apply": True,
            "final_recommendation": "NO LIVE",
        }

    def to_text(self, *, hours: int = 24) -> str:
        payload = self.build(hours=hours)
        lines = [
            START,
            f"hours: {payload['hours']}",
            f"total_candidates: {payload['total_candidates']}",
            "candidate_status_counts:",
            *_count_lines(payload["candidate_status_counts"]),
            "candidate_category_counts:",
            *_count_lines(payload["candidate_category_counts"]),
            "disabled_paper_candidates:",
            *_candidate_lines(payload["disabled_paper_candidates"]),
            "promising_not_actionable:",
            *_candidate_lines(payload["promising_not_actionable"]),
            "market_probe_only:",
            *_candidate_lines(payload["market_probe_only"]),
            "top_shadow_only:",
            *_candidate_lines(payload["top_shadow_only"]),
            "top_watch_only:",
            *_candidate_lines(payload["top_watch_only"]),
            "top_need_more_data:",
            *_candidate_lines(payload["top_need_more_data"]),
            "top_reject:",
            *_candidate_lines(payload["top_reject"]),
            "PROMISING RAW POCKETS",
            *_pocket_lines(payload["promising_raw_pockets"]),
            "paper_filter_enabled: false",
            "do_not_apply: true",
            "final_recommendation: NO LIVE",
            END,
        ]
        return "\n".join(lines)

    def _candidate(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        current = metric_snapshot(rows, self.config)
        exit_reports = [_exit_report(name, rows, self.config) for name in EXIT_PROFILES]
        exit_reports.sort(key=lambda row: (safe_float(row.get("net_EV")), safe_float(row.get("net_PF"))), reverse=True)
        best = exit_reports[0] if exit_reports else _empty_exit("current_exit")
        worst = exit_reports[-1] if exit_reports else _empty_exit("current_exit")
        symbol = _dominant(rows, "symbol")
        side = _dominant(rows, "side")
        regime = _dominant(rows, "market_regime")
        bucket = _dominant(rows, "score_bucket")
        source = _dominant(rows, "source")
        status, reason, actionability, category = _candidate_status(current, best, source=source, side=side, config=self.config)
        return {
            "candidate_id": f"{symbol}_{side}_{regime}_{bucket}_{best.get('profile')}",
            "symbol": symbol,
            "side": side,
            "market_regime": regime,
            "score_bucket": bucket,
            "source": source,
            "suggested_exit_profile": best.get("profile"),
            "samples": current["samples"],
            "TP": current["tp_ratio"],
            "SL": current["sl_ratio"],
            "TIME": current["time_ratio"],
            "gross_PF": current["gross_PF"],
            "net_EV_est": current["net_EV_est"],
            "net_PF_est": current["net_PF_est"],
            "stability_score": _stability_score(current),
            "sample_score": min(1.0, safe_int(current.get("samples")) / 750.0),
            "drawdown_proxy": current["drawdown_proxy"],
            "best_exit_profile": best.get("profile"),
            "best_exit_net_EV": best.get("net_EV"),
            "best_exit_net_PF": best.get("net_PF"),
            "best_exit_TIME": best.get("TIME"),
            "best_exit_SL": best.get("SL"),
            "best_exit_TP": best.get("TP"),
            "worst_exit_profile": worst.get("profile"),
            "false_score_risk": _false_score_risk(current, bucket),
            "overfit_risk": _overfit_risk(current),
            "exit_decision": _exit_decision(best, current),
            "candidate_status": status,
            "reason": reason,
            "actionability": actionability,
            "candidate_category": category,
        }


def status_rank(status: Any) -> int:
    return {
        "PAPER_CANDIDATE_DISABLED": 5,
        "SHADOW_ONLY": 4,
        "NEED_MORE_DATA": 3,
        "WATCH_ONLY": 2,
        "REJECT": 1,
    }.get(str(status), 0)


def _candidate_groups(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = "|".join([
            str(row.get("symbol") or "NA").upper(),
            str(row.get("side") or "NA").upper(),
            str(row.get("market_regime") or "unknown").upper(),
            str(row.get("score_bucket") or "0-49"),
        ])
        groups[key].append(row)
    return groups


def _candidate_status(current: dict[str, Any], best: dict[str, Any], *, source: str, side: str, config: Any) -> tuple[str, str, str, str]:
    samples = safe_int(current.get("samples"))
    net_ev = safe_float(current.get("net_EV_est"))
    net_pf = safe_float(current.get("net_PF_est"))
    gross_pf = safe_float(current.get("gross_PF"))
    sl = safe_float(current.get("sl_ratio"))
    time_ratio = safe_float(current.get("time_ratio"))
    tp = safe_float(current.get("tp_ratio"))
    max_sl = safe_float(getattr(config, "candidate_incubator_max_sl_ratio", 0.25))
    max_time = safe_float(getattr(config, "candidate_incubator_max_time_ratio", 0.80))
    min_net_pf = safe_float(getattr(config, "candidate_incubator_min_net_pf", 1.15))
    soft = safe_int(getattr(config, "candidate_incubator_min_samples_soft", 250))
    hard = safe_int(getattr(config, "candidate_incubator_min_samples_hard", 750))
    source_text = str(source or "").lower()
    if source_text == "market_probe":
        if samples < soft and (gross_pf > 1.2 or net_ev > 0 or tp >= 0.30):
            return "NEED_MORE_DATA", "sample_too_small + market_probe_not_actionable", MARKET_PROBE_ACTIONABILITY, "NEED_MORE_DATA_NOT_ACTIONABLE"
        if gross_pf > 1.2 or net_ev > 0:
            return "WATCH_ONLY", "market_probe_not_actionable", MARKET_PROBE_ACTIONABILITY, "WATCH_ONLY_MARKET_PROBE"
        return "REJECT", "market_probe_not_actionable_bad_edge", MARKET_PROBE_ACTIONABILITY, "REJECT_DATA_QUALITY"
    if gross_pf < 1.0:
        return "REJECT", "gross_pf_below_1", "ACTIONABLE_RESEARCH_SIGNAL", "REJECT_BAD_EDGE"
    if net_ev < -0.05:
        return "REJECT", "net_ev_negative_strong", "ACTIONABLE_RESEARCH_SIGNAL", "REJECT_BAD_EDGE"
    if sl > max_sl:
        return "REJECT", "sl_ratio_too_high", "ACTIONABLE_RESEARCH_SIGNAL", "REJECT_BAD_EDGE"
    if time_ratio > 0.92:
        return "REJECT", "time_extreme", "ACTIONABLE_RESEARCH_SIGNAL", "REJECT_BAD_EDGE"
    if side == "LONG" and (tp < 0.03 or sl > tp):
        return "REJECT", "long_bad_side", "ACTIONABLE_RESEARCH_SIGNAL", "REJECT_BAD_EDGE"
    if samples < soft:
        if gross_pf > 1.2 or net_ev > 0:
            return "NEED_MORE_DATA", "sample_too_small_promising", "ACTIONABLE_RESEARCH_SIGNAL", "RESEARCH_POCKET"
        return "REJECT", "sample_too_small", "ACTIONABLE_RESEARCH_SIGNAL", "REJECT_DATA_QUALITY"
    if net_ev <= 0 or net_pf < 1.0:
        return "WATCH_ONLY", "gross_edge_but_net_uncertain_or_negative", "ACTIONABLE_RESEARCH_SIGNAL", "WATCH_ONLY_TRADE_SIGNAL"
    if time_ratio > max_time:
        return "WATCH_ONLY", "time_ratio_high", "ACTIONABLE_RESEARCH_SIGNAL", "WATCH_ONLY_TRADE_SIGNAL"
    if safe_float(best.get("net_EV")) <= 0 or safe_float(best.get("net_PF")) < min_net_pf:
        return "WATCH_ONLY", "exit_variant_not_net_confirmed", "ACTIONABLE_RESEARCH_SIGNAL", "WATCH_ONLY_TRADE_SIGNAL"
    if samples < hard:
        return "SHADOW_ONLY", "positive_preliminary_needs_walkforward", "ACTIONABLE_RESEARCH_SIGNAL", "RESEARCH_POCKET"
    if net_pf >= min_net_pf and net_ev > 0 and _stability_score(current) >= 0.55:
        return "PAPER_CANDIDATE_DISABLED", "strong_thresholds_but_disabled_until_phase_6", "ACTIONABLE_RESEARCH_SIGNAL", "RESEARCH_POCKET"
    return "SHADOW_ONLY", "positive_but_not_strong_enough", "ACTIONABLE_RESEARCH_SIGNAL", "RESEARCH_POCKET"


def _exit_report(profile: str, rows: list[dict[str, Any]], config: Any) -> dict[str, Any]:
    spec = EXIT_PROFILES[profile]
    if spec is None:
        metrics = metric_snapshot(rows, config)
        return {
            "profile": profile,
            "net_EV": metrics["net_EV_est"],
            "net_PF": metrics["net_PF_est"],
            "TP": metrics["tp_ratio"],
            "SL": metrics["sl_ratio"],
            "TIME": metrics["time_ratio"],
        }
    tp_pct, sl_pct, holding = spec
    returns: list[float] = []
    tp_count = sl_count = time_count = 0
    for row in rows:
        mfe = safe_float(row.get("mfe"))
        mae = safe_float(row.get("mae"))
        bars = safe_float(row.get("bars"))
        final_return = safe_float(row.get("return_pct"))
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
    costs = cost_config(config)
    breakdowns = [
        explain_cost_breakdown(
            source=str(row.get("source") or "trade_signal"),
            side=str(row.get("side") or ""),
            entry_type="taker",
            exit_type="taker",
            slippage_bps=costs.slippage_bps,
            entry_time=row.get("timestamp"),
            holding_bars=holding if spec is not None else row.get("bars"),
            funding_rate=row.get("funding_rate") if safe_float(row.get("funding_rate")) else None,
            outcome=str(row.get("first_barrier_hit") or ""),
        )
        for row in rows
    ]
    net = calculate_net_metrics_for_returns(returns, breakdowns)
    samples = len(rows)
    return {
        "profile": profile,
        "net_EV": net["net_EV"],
        "net_PF": net["net_PF"],
        "TP": tp_count / max(samples, 1),
        "SL": sl_count / max(samples, 1),
        "TIME": time_count / max(samples, 1),
    }


def _exit_decision(best: dict[str, Any], current: dict[str, Any]) -> str:
    if safe_float(best.get("net_EV")) <= 0:
        return "REJECT_EXIT"
    if safe_int(current.get("samples")) < 250:
        return "NEED_MORE_DATA"
    if str(best.get("profile")) == "current_exit":
        return "KEEP_CURRENT"
    if str(best.get("profile")) in {"tp050_sl075", "tp050_sl050"}:
        return "WATCH_TP050_SL075"
    return "NEED_MORE_DATA"


def _empty_exit(profile: str) -> dict[str, Any]:
    return {"profile": profile, "net_EV": 0.0, "net_PF": 0.0, "TP": 0.0, "SL": 0.0, "TIME": 0.0}


def _stability_score(metrics: dict[str, Any]) -> float:
    sample = min(1.0, safe_int(metrics.get("samples")) / 750.0)
    net = max(0.0, min(1.0, safe_float(metrics.get("net_EV_est")) + 0.5))
    penalty = safe_float(metrics.get("time_ratio")) * 0.25 + safe_float(metrics.get("sl_ratio")) * 0.35
    return max(0.0, min(1.0, sample * 0.45 + net * 0.55 - penalty))


def _false_score_risk(metrics: dict[str, Any], bucket: str) -> float:
    high = bucket in {"85-89", "90-94", "95-100"}
    risk = 0.0
    if high and safe_float(metrics.get("net_EV_est")) <= 0:
        risk += 0.45
    if high and safe_float(metrics.get("sl_ratio")) > 0.20:
        risk += 0.25
    if high and safe_float(metrics.get("time_ratio")) > 0.80:
        risk += 0.20
    return min(1.0, risk)


def _overfit_risk(metrics: dict[str, Any]) -> float:
    risk = 0.0
    if safe_int(metrics.get("samples")) < 250:
        risk += 0.50
    if safe_float(metrics.get("gross_PF")) > 3.0 and safe_int(metrics.get("samples")) < 750:
        risk += 0.30
    if safe_float(metrics.get("drawdown_proxy")) > 1.0:
        risk += 0.20
    return min(1.0, risk)


def _promising_raw_pockets(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pockets = []
    for row in candidates:
        tokens = {str(row.get("symbol")), str(row.get("side")), str(row.get("market_regime"))}
        if not (tokens & PROMISING_KEYS):
            continue
        pockets.append({
            "candidate_id": row.get("candidate_id"),
            "symbol": row.get("symbol"),
            "side": row.get("side"),
            "market_regime": row.get("market_regime"),
            "score_bucket": row.get("score_bucket"),
            "status": row.get("candidate_status"),
            "reason": row.get("reason"),
            "net_EV_est": row.get("net_EV_est"),
            "net_PF_est": row.get("net_PF_est"),
            "actionability": row.get("actionability"),
            "candidate_category": row.get("candidate_category"),
        })
    return pockets[:20]


def _dominant(rows: list[dict[str, Any]], key: str) -> str:
    counts = Counter(str(row.get(key) or "NA") for row in rows)
    return counts.most_common(1)[0][0] if counts else "NA"


def _count_lines(counts: dict[str, Any]) -> list[str]:
    if not counts:
        return ["- none"]
    return [f"- {key}: {value}" for key, value in sorted(counts.items())]


def _candidate_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- none"]
    out = []
    for row in rows[:15]:
        out.append(
            f"- {row.get('candidate_id')} samples={row.get('samples')} "
            f"TP%={safe_float(row.get('TP')) * 100:.1f} SL%={safe_float(row.get('SL')) * 100:.1f} "
            f"TIME%={safe_float(row.get('TIME')) * 100:.1f} gross_PF={safe_float(row.get('gross_PF')):.2f} "
            f"net_PF={safe_float(row.get('net_PF_est')):.2f} net_EV={safe_float(row.get('net_EV_est')):.4f} "
            f"best_exit={row.get('best_exit_profile')} exit_decision={row.get('exit_decision')} "
            f"status={row.get('candidate_status')} category={row.get('candidate_category')} "
            f"actionability={row.get('actionability')} reason={row.get('reason')}"
        )
    return out


def _pocket_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- none"]
    return [
        (
            f"- {row.get('symbol')} {row.get('side')} {row.get('market_regime')} {row.get('score_bucket')}: "
            f"{row.get('status')}, reason={row.get('reason')}, net_EV={safe_float(row.get('net_EV_est')):.4f}"
        )
        for row in rows[:20]
    ]
