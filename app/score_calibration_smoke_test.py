from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .score_calibration import ScoreCalibration


class _FakeScoreDb:
    def __init__(self) -> None:
        self.rows = _synthetic_rows()

    def fetch_labeled_signal_rows_since(self, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return list(self.rows)

    def fetch_signal_path_metrics_since(self, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return []


def score_calibration_smoke_text(config: Any) -> str:
    lab = ScoreCalibration(config, _FakeScoreDb())
    payload = lab.build(hours=24)
    flags = payload.get("flags", {})
    high_failures = payload.get("high_score_failures", [])
    by_side = {str(row.get("group_value")): row for row in payload.get("by_side", [])}
    checks = {
        "score_high_fails": bool(high_failures),
        "score_medium_wins": any(str(row.get("group_value")) == "80-84" and float(row.get("net_EV_est") or 0) > 0 for row in payload.get("by_score_bucket", [])),
        "score_not_monotonic": bool(flags.get("score_not_monotonic")),
        "long_bad_side": bool(flags.get("long_bad_side")),
        "short_promising": float(by_side.get("SHORT", {}).get("gross_PF") or 0) > 1.0,
        "negative_net_ev_detected": bool(flags.get("high_score_negative_net_EV")),
        "low_sample_fake_edge": bool(flags.get("low_sample_fake_edge")),
        "final_recommendation_no_live": payload.get("final_recommendation") == "NO LIVE",
    }
    result = "PASS" if all(checks.values()) else "FAIL"
    lines = ["SCORE CALIBRATION SMOKE TEST START"]
    lines.extend(f"{key}: {str(value).lower()}" for key, value in checks.items())
    lines.extend([
        f"overall_score_quality: {payload.get('overall_score_quality')}",
        f"biggest_problem: {payload.get('biggest_problem')}",
        "opened_real_trades: 0",
        "opened_paper_trades_from_smoke: 0",
        "paper_filter_enabled: false",
        "final_recommendation: NO LIVE",
        f"result: {result}",
        "SCORE CALIBRATION SMOKE TEST END",
    ])
    return "\n".join(lines)


def _synthetic_rows() -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc).isoformat()
    rows: list[dict[str, Any]] = []
    obs = 1

    def add_many(count: int, *, symbol: str, side: str, regime: str, score: int, hit: str, ret: float, mfe: float, mae: float) -> None:
        nonlocal obs
        for _ in range(count):
            rows.append({
                "id": obs,
                "observation_id": obs,
                "timestamp": now,
                "label_timestamp": now,
                "symbol": symbol,
                "side": side,
                "market_regime": regime,
                "confidence_score": score,
                "strategy_type": "smoke",
                "first_barrier_hit": hit,
                "realized_return_pct": ret,
                "max_favorable_excursion": mfe,
                "max_adverse_excursion": mae,
                "bars_to_outcome": 10,
            })
            obs += 1

    add_many(140, symbol="ETHUSDT", side="SHORT", regime="TREND_DOWN", score=82, hit="TP1", ret=0.55, mfe=0.70, mae=0.10)
    add_many(40, symbol="ETHUSDT", side="SHORT", regime="TREND_DOWN", score=82, hit="SL", ret=-0.25, mfe=0.10, mae=0.35)
    add_many(20, symbol="ETHUSDT", side="SHORT", regime="TREND_DOWN", score=82, hit="TIME", ret=0.05, mfe=0.20, mae=0.10)

    add_many(25, symbol="SOLUSDT", side="SHORT", regime="CHOPPY_MARKET", score=92, hit="TP1", ret=0.20, mfe=0.30, mae=0.20)
    add_many(95, symbol="SOLUSDT", side="SHORT", regime="CHOPPY_MARKET", score=92, hit="SL", ret=-0.45, mfe=0.10, mae=0.55)
    add_many(40, symbol="SOLUSDT", side="SHORT", regime="CHOPPY_MARKET", score=92, hit="TIME", ret=-0.05, mfe=0.15, mae=0.20)

    add_many(10, symbol="XRPUSDT", side="SHORT", regime="RISK_OFF", score=75, hit="TP1", ret=1.0, mfe=1.2, mae=0.05)
    add_many(90, symbol="BNBUSDT", side="LONG", regime="RISK_OFF", score=88, hit="SL", ret=-0.60, mfe=0.02, mae=0.70)
    return rows
