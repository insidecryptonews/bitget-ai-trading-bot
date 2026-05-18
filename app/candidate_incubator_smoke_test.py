from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .candidate_incubator import CandidateIncubator


class _FakeIncubatorDb:
    def __init__(self) -> None:
        self.labels: list[dict[str, Any]] = []
        self.paths: list[dict[str, Any]] = []
        self._build()

    def fetch_labeled_signal_rows_since(self, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return list(self.labels)

    def fetch_signal_path_metrics_since(self, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return list(self.paths)

    def _build(self) -> None:
        obs = 1

        def add_labels(count: int, *, symbol: str, side: str, regime: str, score: int, hit: str, ret: float, mfe: float, mae: float) -> None:
            nonlocal obs
            now = datetime.now(timezone.utc).isoformat()
            for _ in range(count):
                self.labels.append({
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
                    "bars_to_outcome": 20,
                })
                obs += 1

        def add_probe(count: int) -> None:
            nonlocal obs
            now = datetime.now(timezone.utc).isoformat()
            for _ in range(count):
                self.paths.append({
                    "id": obs,
                    "observation_id": obs,
                    "created_at": now,
                    "updated_at": now,
                    "status": "matured",
                    "source": "market_probe",
                    "symbol": "AVAXUSDT",
                    "side": "SHORT",
                    "market_regime": "TREND_DOWN",
                    "score": 0,
                    "score_bucket": "PROBE",
                    "first_barrier_hit": "TP1",
                    "final_return_pct": 0.8,
                    "max_favorable_pct": 0.9,
                    "max_adverse_pct": 0.05,
                    "bars_tracked": 20,
                })
                obs += 1

        add_labels(300, symbol="SOLUSDT", side="SHORT", regime="CHOPPY_MARKET", score=90, hit="SL", ret=-0.7, mfe=0.05, mae=0.8)
        add_labels(240, symbol="LINKUSDT", side="SHORT", regime="TREND_DOWN", score=86, hit="TP1", ret=0.22, mfe=0.30, mae=0.05)
        add_labels(60, symbol="LINKUSDT", side="SHORT", regime="TREND_DOWN", score=86, hit="SL", ret=-0.10, mfe=0.05, mae=0.20)
        add_labels(200, symbol="BTCUSDT", side="SHORT", regime="RISK_OFF", score=86, hit="TP1", ret=0.28, mfe=0.35, mae=0.06)
        add_labels(100, symbol="BTCUSDT", side="SHORT", regime="RISK_OFF", score=86, hit="SL", ret=-0.10, mfe=0.04, mae=0.20)
        add_labels(50, symbol="DOGEUSDT", side="SHORT", regime="RISK_OFF", score=76, hit="TP1", ret=0.9, mfe=1.0, mae=0.05)
        add_labels(250, symbol="ETHUSDT", side="SHORT", regime="TREND_DOWN", score=84, hit="TP1", ret=0.70, mfe=0.85, mae=0.10)
        add_labels(70, symbol="ETHUSDT", side="SHORT", regime="TREND_DOWN", score=84, hit="SL", ret=-0.25, mfe=0.10, mae=0.35)
        add_labels(650, symbol="XRPUSDT", side="SHORT", regime="TREND_DOWN", score=88, hit="TP1", ret=0.70, mfe=0.85, mae=0.10)
        add_labels(150, symbol="XRPUSDT", side="SHORT", regime="TREND_DOWN", score=88, hit="SL", ret=-0.25, mfe=0.10, mae=0.35)
        add_probe(900)


def candidate_incubator_smoke_text(config: Any) -> str:
    payload = CandidateIncubator(config, _FakeIncubatorDb()).build(hours=24)
    candidates = payload.get("candidates", [])
    statuses = {str(row.get("candidate_status")) for row in candidates}
    market_probe_rows = [row for row in candidates if row.get("source") == "market_probe"]
    checks = {
        "reject_net_ev_negative": any(row.get("candidate_status") == "REJECT" and row.get("reason") in {"net_ev_negative_strong", "gross_pf_below_1"} for row in candidates),
        "watch_only_gross_edge_net_bad": any(row.get("candidate_status") == "WATCH_ONLY" for row in candidates),
        "need_more_data_small_sample": "NEED_MORE_DATA" in statuses,
        "shadow_only_candidate": "SHADOW_ONLY" in statuses,
        "paper_candidate_disabled_no_activation": "PAPER_CANDIDATE_DISABLED" in statuses and not payload.get("paper_filter_enabled"),
        "market_probe_never_actionable": bool(market_probe_rows) and all(row.get("candidate_status") == "REJECT" for row in market_probe_rows),
        "final_recommendation_no_live": payload.get("final_recommendation") == "NO LIVE",
    }
    result = "PASS" if all(checks.values()) else "FAIL"
    lines = ["CANDIDATE INCUBATOR SMOKE TEST START"]
    lines.extend(f"{key}: {str(value).lower()}" for key, value in checks.items())
    lines.extend([
        f"candidate_statuses: {','.join(sorted(statuses))}",
        "opened_real_trades: 0",
        "opened_paper_trades_from_smoke: 0",
        "paper_filter_enabled: false",
        "final_recommendation: NO LIVE",
        f"result: {result}",
        "CANDIDATE INCUBATOR SMOKE TEST END",
    ])
    return "\n".join(lines)
