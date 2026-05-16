from __future__ import annotations

from typing import Any

from .edge_hardening_utils import FINAL_NO_LIVE, apply_net_costs, cost_config, fetch_group_metrics, format_num, format_pct, since_iso
from .utils import safe_float, safe_int


START = "POLICY STABILITY MATRIX START"
END = "POLICY STABILITY MATRIX END"


class PolicyStabilityMatrix:
    WINDOWS = (1, 3, 6, 12, 24)

    def __init__(self, config: Any, db: Any) -> None:
        self.config = config
        self.db = db

    def build(self, *, hours: int = 24) -> dict[str, Any]:
        costs = cost_config(self.config)
        window_maps: dict[int, dict[str, dict[str, Any]]] = {}
        for window in self.WINDOWS:
            if window > max(24, int(hours or 24)):
                continue
            rows = [
                apply_net_costs(row, costs)
                for row in fetch_group_metrics(self.db, since=since_iso(window), group_key="policy_id", limit=80, min_samples=1)
            ]
            window_maps[window] = {str(row.get("group_value") or ""): row for row in rows}
        policy_ids = sorted({policy for rows in window_maps.values() for policy in rows})
        matrix = []
        for policy_id in policy_ids[:80]:
            item: dict[str, Any] = {"policy_id": policy_id}
            pfs = []
            samples = []
            for window in self.WINDOWS:
                row = window_maps.get(window, {}).get(policy_id, {})
                item[f"pf_{window}h"] = safe_float(row.get("gross_PF"))
                item[f"net_pf_{window}h"] = safe_float(row.get("net_PF"))
                item[f"samples_{window}h"] = safe_int(row.get("samples"))
                if row:
                    item[f"tp_ratio_{window}h"] = safe_float(row.get("tp_ratio"))
                    item[f"sl_ratio_{window}h"] = safe_float(row.get("sl_ratio"))
                    item[f"time_ratio_{window}h"] = safe_float(row.get("time_ratio"))
                pfs.append(item[f"net_pf_{window}h"])
                samples.append(item[f"samples_{window}h"])
            item["trend_status"] = _trend_status(pfs, samples)
            item["final_decision"] = _decision(item, costs)
            matrix.append(item)
        matrix.sort(key=lambda item: (item["final_decision"] == "PAPER_CANDIDATE", safe_float(item.get("net_pf_24h"))), reverse=True)
        return {"hours": max(1, int(hours or 24)), "matrix": matrix[:50], "final_recommendation": FINAL_NO_LIVE}

    def to_text(self, *, hours: int = 24) -> str:
        payload = self.build(hours=hours)
        return "\n".join([
            START,
            f"hours: {payload['hours']}",
            "policies:",
            *_lines(payload["matrix"]),
            "final_recommendation: NO LIVE",
            END,
        ])


def _trend_status(pfs: list[float], samples: list[int]) -> str:
    valid = [(pf, sample) for pf, sample in zip(pfs, samples) if sample > 0]
    if len(valid) < 3:
        return "insufficient"
    first = valid[0][0]
    last = valid[-1][0]
    if last > first * 1.15:
        return "improving"
    if last < first * 0.75:
        return "deteriorating"
    return "stable"


def _decision(item: dict[str, Any], costs: Any) -> str:
    status = item.get("trend_status")
    net_24 = safe_float(item.get("net_pf_24h"))
    samples_24 = safe_int(item.get("samples_24h"))
    if samples_24 < costs.min_samples:
        return "WATCH_ONLY"
    if status == "deteriorating" or net_24 < costs.min_net_pf:
        return "REJECT"
    if status == "stable" and net_24 >= costs.min_net_pf:
        return "PAPER_CANDIDATE"
    return "SHADOW_CANDIDATE"


def _lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- none"]
    out = []
    for row in rows[:20]:
        out.append(
            f"- policy_id={row.get('policy_id')} "
            f"PF_1h={format_num(row.get('pf_1h'))} PF_3h={format_num(row.get('pf_3h'))} "
            f"PF_6h={format_num(row.get('pf_6h'))} PF_12h={format_num(row.get('pf_12h'))} PF_24h={format_num(row.get('pf_24h'))} "
            f"net_PF_24h={format_num(row.get('net_pf_24h'))} TP_24h={format_pct(row.get('tp_ratio_24h'))} "
            f"SL_24h={format_pct(row.get('sl_ratio_24h'))} TIME_24h={format_pct(row.get('time_ratio_24h'))} "
            f"samples_24h={row.get('samples_24h')} trend_status={row.get('trend_status')} decision={row.get('final_decision')}"
        )
    return out
