from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Any

from .utils import safe_float, safe_int


FINAL_LABEL_HITS = {"TP1", "TP2", "SL", "TIME"}


def stable_observation_fingerprint(observation: dict[str, Any]) -> str:
    payload = {
        "timestamp": observation.get("timestamp") or observation.get("created_at"),
        "symbol": str(observation.get("symbol") or "").upper(),
        "side": str(observation.get("side") or "").upper(),
        "score": round(safe_float(observation.get("confidence_score") if observation.get("confidence_score") is not None else observation.get("score")), 6),
        "source": str(observation.get("source") or "").lower(),
        "regime": str(observation.get("market_regime") or "").upper(),
        "reason": str(observation.get("reason") or observation.get("block_reason") or ""),
        "strategy": str(observation.get("strategy_type") or observation.get("strategy") or ""),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def exact_duplicate_observation_detector(observations: list[dict[str, Any]]) -> dict[str, Any]:
    fingerprints = [stable_observation_fingerprint(row) for row in observations]
    counts = Counter(fingerprints)
    duplicates = [fp for fp, count in counts.items() if count > 1]
    examples = []
    for fp in duplicates[:10]:
        first = next((row for row in observations if stable_observation_fingerprint(row) == fp), {})
        examples.append({"fingerprint": fp, "count": counts[fp], "symbol": first.get("symbol"), "side": first.get("side")})
    return {
        "exact_duplicate_count": sum(count - 1 for count in counts.values() if count > 1),
        "exact_duplicate_rate": (sum(count - 1 for count in counts.values() if count > 1) / max(len(observations), 1)),
        "duplicate_examples_sanitized": examples,
        "duplicate_guard_status": "WARNING" if duplicates else "OK",
    }


def duplicate_label_detector(existing_labels: list[dict[str, Any]], observation_id: Any) -> bool:
    obs = safe_int(observation_id)
    return any(safe_int(row.get("observation_id")) == obs and str(row.get("first_barrier_hit") or "").upper() in FINAL_LABEL_HITS for row in existing_labels)


def conflicting_label_detector(existing_labels: list[dict[str, Any]], new_label: dict[str, Any]) -> bool:
    obs = safe_int(new_label.get("observation_id"))
    new_hit = str(new_label.get("first_barrier_hit") or "").upper()
    return any(
        safe_int(row.get("observation_id")) == obs
        and str(row.get("first_barrier_hit") or "").upper() in FINAL_LABEL_HITS
        and str(row.get("first_barrier_hit") or "").upper() != new_hit
        for row in existing_labels
    )


def should_insert_label(existing_labels: list[dict[str, Any]], new_label: dict[str, Any], *, allow_versioned: bool = False) -> tuple[bool, str]:
    if allow_versioned:
        return True, "versioned_shadow_label_allowed"
    if duplicate_label_detector(existing_labels, new_label.get("observation_id")):
        if conflicting_label_detector(existing_labels, new_label):
            return False, "conflicting_final_label_exists"
        return False, "duplicate_final_label_exists"
    return True, "insert_allowed"


def relation_key(observation_id: Any, label_id: Any = None, path_metric_id: Any = None) -> str:
    return f"obs:{safe_int(observation_id)}|label:{safe_int(label_id)}|path:{safe_int(path_metric_id)}"


def classify_label_path_consistency(
    *,
    label_hit: Any,
    mfe_pct: Any,
    mae_pct: Any,
    tp_threshold_pct: Any,
    sl_threshold_pct: Any,
) -> str:
    hit = str(label_hit or "").upper()
    mfe = safe_float(mfe_pct)
    mae = abs(safe_float(mae_pct))
    tp = safe_float(tp_threshold_pct)
    sl = abs(safe_float(sl_threshold_pct))
    if tp <= 0 or sl <= 0:
        return "LABEL_INCOMPLETE_CONTEXT"
    touched_tp = mfe >= tp
    touched_sl = mae >= sl
    if touched_tp and touched_sl:
        return "AMBIGUOUS_BOTH_TOUCHED"
    if touched_tp and not hit.startswith("TP"):
        return "MISSED_TP_POSSIBLE"
    if touched_sl and hit != "SL":
        return "MISSED_SL_POSSIBLE"
    return "OK"


def labeler_guard_smoke_text() -> str:
    existing = [{"observation_id": 1, "first_barrier_hit": "TP1"}]
    checks = {
        "duplicate_label_blocked": should_insert_label(existing, {"observation_id": 1, "first_barrier_hit": "TP1"})[0] is False,
        "conflicting_label_blocked": should_insert_label(existing, {"observation_id": 1, "first_barrier_hit": "SL"})[1] == "conflicting_final_label_exists",
        "versioned_shadow_allowed": should_insert_label(existing, {"observation_id": 1, "first_barrier_hit": "SL"}, allow_versioned=True)[0] is True,
        "missed_tp_detected": classify_label_path_consistency(label_hit="TIME", mfe_pct=0.6, mae_pct=0.1, tp_threshold_pct=0.5, sl_threshold_pct=0.75) == "MISSED_TP_POSSIBLE",
        "missed_sl_detected": classify_label_path_consistency(label_hit="TIME", mfe_pct=0.1, mae_pct=0.8, tp_threshold_pct=0.5, sl_threshold_pct=0.75) == "MISSED_SL_POSSIBLE",
        "both_touched_ambiguous": classify_label_path_consistency(label_hit="TIME", mfe_pct=0.6, mae_pct=0.8, tp_threshold_pct=0.5, sl_threshold_pct=0.75) == "AMBIGUOUS_BOTH_TOUCHED",
    }
    result = "PASS" if all(checks.values()) else "FAIL"
    lines = ["LABELER GUARD SMOKE TEST START"]
    lines.extend(f"{key}: {str(value).lower()}" for key, value in checks.items())
    lines.extend(["final_recommendation: NO LIVE", f"result: {result}", "LABELER GUARD SMOKE TEST END"])
    return "\n".join(lines)


def duplicate_guard_smoke_text() -> str:
    rows = [
        {"timestamp": "2026-05-19T00:00:00+00:00", "symbol": "BTCUSDT", "side": "SHORT", "score": 90, "source": "trade_signal", "market_regime": "RISK_OFF"},
        {"timestamp": "2026-05-19T00:00:00+00:00", "symbol": "BTCUSDT", "side": "SHORT", "score": 90, "source": "trade_signal", "market_regime": "RISK_OFF"},
        {"timestamp": "2026-05-19T00:00:00+00:00", "symbol": "ETHUSDT", "side": "SHORT", "score": 90, "source": "trade_signal", "market_regime": "RISK_OFF"},
    ]
    audit = exact_duplicate_observation_detector(rows)
    checks = {
        "exact_observation_duplicate_detected": audit["exact_duplicate_count"] == 1,
        "benign_other_symbol_not_duplicate": audit["exact_duplicate_rate"] < 1.0,
        "duplicate_guard_status_warning": audit["duplicate_guard_status"] == "WARNING",
    }
    result = "PASS" if all(checks.values()) else "FAIL"
    lines = ["DUPLICATE GUARD SMOKE TEST START"]
    lines.extend(f"{key}: {str(value).lower()}" for key, value in checks.items())
    lines.extend(["historical_data_modified: false", "final_recommendation: NO LIVE", f"result: {result}", "DUPLICATE GUARD SMOKE TEST END"])
    return "\n".join(lines)
