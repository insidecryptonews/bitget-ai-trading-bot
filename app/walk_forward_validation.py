from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .catalyst_registry import CatalystRegistry, edge_metrics, match_catalysts
from .config import BotConfig
from .database import Database
from .news_risk_gate import NewsRiskGate
from .paper_policy_lab import PaperPolicyLab
from .utils import safe_float, safe_int


START = "WALK FORWARD VALIDATION START"
END = "WALK FORWARD VALIDATION END"


class WalkForwardValidation:
    """Temporal 70/30 validation for research-only policy candidates."""

    def __init__(self, config: BotConfig, db: Database) -> None:
        self.config = config
        self.db = db

    def build(self, *, hours: int = 24) -> dict[str, Any]:
        hours = max(1, int(hours or 24))
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        rows = self.db.fetch_labeled_signal_rows_since(since, limit=50000) if hasattr(self.db, "fetch_labeled_signal_rows_since") else []
        rows.sort(key=lambda row: str(row.get("label_timestamp") or row.get("timestamp") or ""))
        split = max(1, int(len(rows) * 0.70)) if rows else 0
        train = rows[:split]
        validation = rows[split:]
        policies = PaperPolicyLab(self.config, self.db).build(hours=hours).get("candidate_policies", [])
        catalysts = self.db.fetch_market_catalysts(since_iso=since, until_iso=datetime.now(timezone.utc).isoformat(), limit=500)
        news = NewsRiskGate(self.config, self.db).build(hours=hours)
        results = [self._validate_policy(policy, train, validation, catalysts, news) for policy in policies]
        if not results and rows:
            results.append(self._validate_policy({"policy_id": "baseline_all", "decision": "WATCH_ONLY"}, train, validation, catalysts, news))
        return {"hours": hours, "policies": results, "final_recommendation": "NO LIVE"}

    def to_text(self, *, hours: int = 24) -> str:
        payload = self.build(hours=hours)
        lines = [
            START,
            f"hours: {payload['hours']}",
            "policies:",
            *_policy_lines(payload["policies"]),
            "final_recommendation: NO LIVE",
            END,
        ]
        return "\n".join(lines)

    def _validate_policy(
        self,
        policy: dict[str, Any],
        train: list[dict[str, Any]],
        validation: list[dict[str, Any]],
        catalysts: list[dict[str, Any]],
        news: dict[str, Any],
    ) -> dict[str, Any]:
        train_rows = _filter_policy(train, policy)
        validation_rows = _filter_policy(validation, policy)
        train_metrics = edge_metrics(train_rows)
        validation_metrics = edge_metrics(validation_rows)
        catalyst_ratio = _catalyst_ratio(validation_rows, catalysts)
        news_conflict = _news_conflict(policy, news)
        decision = "SHADOW_VALIDATE"
        reason = "needs_more_validation"
        if safe_int(validation_metrics.get("samples")) < 100:
            decision, reason = "REJECT", "validation_sample_too_small"
        elif news_conflict:
            decision, reason = "REJECT", "news_risk_conflict"
        elif catalyst_ratio > 0.70:
            decision, reason = "WATCH_ONLY", "catalyst_dependent"
        elif safe_float(validation_metrics.get("profit_factor")) >= 1.20:
            decision, reason = "PAPER_CANDIDATE", "stable_candidate"
        if safe_float(train_metrics.get("profit_factor")) - safe_float(validation_metrics.get("profit_factor")) > 0.60:
            decision, reason = "WATCH_ONLY", "recent_deterioration"
        return {
            "policy_id": policy.get("policy_id", "unknown"),
            "train_pf": train_metrics["profit_factor"],
            "validation_pf": validation_metrics["profit_factor"],
            "train_samples": train_metrics["samples"],
            "validation_samples": validation_metrics["samples"],
            "stability": _stability(train_metrics, validation_metrics),
            "catalyst_dependency": catalyst_ratio,
            "news_risk": "conflict" if news_conflict else "none",
            "decision": decision,
            "reason": reason,
        }


def _filter_policy(rows: list[dict[str, Any]], policy: dict[str, Any]) -> list[dict[str, Any]]:
    symbol = str(policy.get("symbol_allowlist") or "").upper()
    side = str(policy.get("side_allowlist") or "").upper()
    regime = str(policy.get("regime_allowlist") or "").upper()
    bucket = str(policy.get("score_bucket_allowlist") or "").upper()
    selected = []
    for row in rows:
        row_bucket = str(row.get("score_bucket") or _score_bucket(safe_int(row.get("confidence_score")))).upper()
        if symbol and str(row.get("symbol") or "").upper() != symbol:
            continue
        if side and str(row.get("side") or "").upper() != side:
            continue
        if regime and str(row.get("market_regime") or "").upper() != regime:
            continue
        if bucket and row_bucket != bucket:
            continue
        selected.append(row)
    return selected


def _catalyst_ratio(rows: list[dict[str, Any]], catalysts: list[dict[str, Any]]) -> float:
    if not rows:
        return 0.0
    matched = 0
    for row in rows:
        if match_catalysts(catalysts, str(row.get("symbol") or ""), str(row.get("label_timestamp") or row.get("timestamp") or "")):
            matched += 1
    return matched / max(len(rows), 1)


def _news_conflict(policy: dict[str, Any], news: dict[str, Any]) -> bool:
    symbol = str(policy.get("symbol_allowlist") or "").upper()
    for row in news.get("blocked", []):
        blocked_symbol = str(row.get("symbol") or "").upper()
        if blocked_symbol in {"GLOBAL", symbol}:
            return True
    return False


def _stability(train: dict[str, Any], validation: dict[str, Any]) -> float:
    train_pf = safe_float(train.get("profit_factor"))
    validation_pf = safe_float(validation.get("profit_factor"))
    if train_pf <= 0 or validation_pf <= 0:
        return 0.0
    return max(0.0, min(1.0, 1.0 - abs(train_pf - validation_pf) / max(train_pf, 1.0)))


def _score_bucket(score: int) -> str:
    if score >= 90:
        return "90-100"
    if score >= 80:
        return "80-89"
    if score >= 70:
        return "70-79"
    return "<70"


def _policy_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- none"]
    return [
        (
            f"- policy_id={row.get('policy_id')} train_pf={safe_float(row.get('train_pf')):.2f} "
            f"validation_pf={safe_float(row.get('validation_pf')):.2f} stability={safe_float(row.get('stability')):.2f} "
            f"catalyst_dependency={safe_float(row.get('catalyst_dependency')):.2f} news_risk={row.get('news_risk')} "
            f"decision={row.get('decision')} reason={row.get('reason')}"
        )
        for row in rows[:10]
    ]
