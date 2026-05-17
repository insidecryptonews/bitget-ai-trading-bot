from __future__ import annotations

from typing import Any

from .edge_hardening_utils import FINAL_NO_LIVE
from .time_death_autopsy import TimeDeathAutopsyLab
from .utils import safe_float, safe_int


START = "TIME DEATH FILTER PROPOSAL START"
END = "TIME DEATH FILTER PROPOSAL END"


class TimeDeathFilterProposal:
    """Research-only filter proposal; it never changes config or execution."""

    def __init__(self, config: Any, db: Any) -> None:
        self.config = config
        self.db = db

    def build(self, *, hours: int = 24) -> dict[str, Any]:
        autopsy = TimeDeathAutopsyLab(self.config, self.db).build(hours=hours)
        groups = autopsy.get("groups", [])
        proposal = {
            "hours": max(1, int(hours or 24)),
            "block_symbols": [],
            "watch_symbols": [],
            "preferred_symbols": [],
            "block_regimes": [],
            "preferred_regimes": [],
            "block_score_buckets": [],
            "preferred_score_buckets": [],
            "block_sides": [],
            "preferred_sides": [],
            "suggested_exit_tests": [],
            "apply_automatically": False,
            "final_recommendation": FINAL_NO_LIVE,
        }
        for row in groups:
            key = str(row.get("group_key") or "")
            value = str(row.get("group_value") or "")
            decision = str(row.get("decision") or "")
            time_ratio = safe_float(row.get("time_ratio"))
            tp_ratio = safe_float(row.get("tp_ratio"))
            net_ev = safe_float(row.get("net_EV"))
            if decision == "REJECT":
                _add_block(proposal, key, value)
            elif decision == "WATCH_ONLY":
                _add_watch(proposal, key, value)
            elif decision == "SHADOW_EXIT_TEST":
                proposal["suggested_exit_tests"].append(_exit_test(row))
            elif net_ev > 0 and time_ratio < 0.70 and tp_ratio >= 0.05:
                _add_preferred(proposal, key, value)
        _dedupe_all(proposal)
        return proposal

    def to_text(self, *, hours: int = 24) -> str:
        payload = self.build(hours=hours)
        return "\n".join([
            START,
            f"hours: {payload['hours']}",
            "block_symbols:",
            *_item_lines(payload["block_symbols"]),
            "watch_symbols:",
            *_item_lines(payload["watch_symbols"]),
            "preferred_symbols:",
            *_item_lines(payload["preferred_symbols"]),
            "block_regimes:",
            *_item_lines(payload["block_regimes"]),
            "preferred_regimes:",
            *_item_lines(payload["preferred_regimes"]),
            "block_score_buckets:",
            *_item_lines(payload["block_score_buckets"]),
            "preferred_score_buckets:",
            *_item_lines(payload["preferred_score_buckets"]),
            "block_sides:",
            *_item_lines(payload["block_sides"]),
            "preferred_sides:",
            *_item_lines(payload["preferred_sides"]),
            "suggested_exit_tests:",
            *_item_lines(payload["suggested_exit_tests"]),
            "apply_automatically=false",
            "final_recommendation: NO LIVE",
            END,
        ])


def _add_block(payload: dict[str, Any], key: str, value: str) -> None:
    mapping = {"symbol": "block_symbols", "market_regime": "block_regimes", "score_bucket": "block_score_buckets", "side": "block_sides"}
    target = mapping.get(key)
    if target and value:
        payload[target].append(value)


def _add_watch(payload: dict[str, Any], key: str, value: str) -> None:
    if key == "symbol" and value:
        payload["watch_symbols"].append(value)


def _add_preferred(payload: dict[str, Any], key: str, value: str) -> None:
    mapping = {"symbol": "preferred_symbols", "market_regime": "preferred_regimes", "score_bucket": "preferred_score_buckets", "side": "preferred_sides"}
    target = mapping.get(key)
    if target and value:
        payload[target].append(value)


def _exit_test(row: dict[str, Any]) -> str:
    cause = str(row.get("likely_cause") or "UNKNOWN")
    group = f"{row.get('group_key')}={row.get('group_value')}"
    if cause == "HOLD_TOO_LONG_DECAY":
        return f"{group}: profit_lock_after_MFE"
    if safe_float(row.get("avg_MFE")) >= 0.25:
        return f"{group}: lower_TP_or_profit_lock_shadow"
    return f"{group}: observe_only"


def _dedupe_all(payload: dict[str, Any]) -> None:
    for key, value in list(payload.items()):
        if isinstance(value, list):
            seen = []
            for item in value:
                if item and item not in seen:
                    seen.append(item)
            payload[key] = seen[:20]


def _item_lines(items: list[Any]) -> list[str]:
    if not items:
        return ["- none"]
    return [f"- {item}" for item in items[:20]]
