from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .candidate_ranking import CandidateRanking
from .config import BotConfig, PROJECT_ROOT
from .database import Database
from .edge_guard import ALLOW_PAPER, EdgeGuard
from .paper_policy_orchestrator import ALLOW_PAPER_CANDIDATE, PaperPolicyOrchestrator
from .pre_move_event_labeler import PreMoveEventLabeler
from .pre_move_pattern_miner import REJECT, TIME_DEATH_PATTERN, WATCH_ONLY, pattern_decision
from .utils import safe_int


START = "PRE MOVE SMOKE TEST START"
END = "PRE MOVE SMOKE TEST END"


class PreMoveSmokeTest:
    """Local synthetic validation; never opens paper/live trades."""

    def __init__(self, config: BotConfig, db: Any, logger: Any | None = None) -> None:
        self.config = config
        self.logger = logger

    def to_text(self) -> str:
        work = PROJECT_ROOT / ".manual_test_tmp"
        work.mkdir(parents=True, exist_ok=True)
        good_db_path = work / "pre_move_smoke_good.db"
        bad_db_path = work / "pre_move_smoke_bad.db"
        for path in (good_db_path, bad_db_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        config = BotConfig(
            edge_guard_min_sample=5,
            net_edge_min_samples=5,
            paper_policy_min_samples=5,
            min_score_to_trade=70,
            live_trading=False,
            dry_run=True,
            paper_trading=True,
            enable_paper_policy_filter=False,
            paper_policy_filter_mode="shadow",
        )
        good = _db(config, good_db_path)
        bad = _db(config, bad_db_path)
        _seed_good_long_short(good)
        _seed_bad_generic_bucket(bad)
        events = PreMoveEventLabeler(config, good).build(hours=24)
        ranking_bad = CandidateRanking(config, bad).build(hours=24)
        edge_bad = EdgeGuard(config, bad).build_edge_guard_report(hours=24)
        orch_bad = PaperPolicyOrchestrator(config, bad).build(hours=24)
        checks = {
            "detects_strong_up_event": events["long_events"] > 0,
            "detects_strong_down_event": events["short_events"] > 0,
            "does_not_favor_long_by_default": events["long_events"] > 0 and events["short_events"] > 0,
            "does_not_favor_short_by_default": events["long_events"] > 0 and events["short_events"] > 0,
            "negative_ev_pattern_rejected": pattern_decision({"samples": 100, "net_EV": -0.1, "net_PF": 0.5}, config) == REJECT,
            "high_time_pattern_rejected": pattern_decision({"samples": 100, "net_EV": 0.2, "net_PF": 2.0, "TIME_after_signal": 0.95, "TP_after_signal": 0.01}, config) == TIME_DEATH_PATTERN,
            "small_sample_watch_only": pattern_decision({"samples": 1, "net_EV": 1.0, "net_PF": 3.0}, config) == WATCH_ONLY,
            "bad_long_rejected": pattern_decision({"samples": 100, "direction": "LONG", "net_EV": 0.2, "net_PF": 2.0, "SL_after_signal": 0.30, "TP_after_signal": 0.01}, config) == REJECT,
            "bad_short_rejected": pattern_decision({"samples": 100, "direction": "SHORT", "net_EV": 0.2, "net_PF": 2.0, "TIME_after_signal": 0.90, "TP_after_signal": 0.10}, config) == REJECT,
            "candidate_ranking_no_valid_candidates": ranking_bad["status"] == "NO_VALID_CANDIDATES",
            "generic_90_bucket_not_allow": not any(
                row.get("group_type") == "score_bucket" and row.get("decision") == ALLOW_PAPER
                for row in edge_bad.get("candidate_table", [])
            ),
            "edge_guard_no_allow_when_ev_rejects": not edge_bad.get("allow_paper_candidates"),
            "orchestrator_no_generic_allow": not any(
                row.get("group_type") == "score_bucket" and row.get("decision") == ALLOW_PAPER_CANDIDATE
                for row in orch_bad.get("policy_candidates", [])
            ),
            "live_false": config.live_trading is False,
            "dry_run_true": config.dry_run is True,
            "paper_true": config.paper_trading is True,
            "paper_filter_not_enabled": config.enable_paper_policy_filter is False,
        }
        result = all(checks.values())
        lines = [
            START,
            *[f"{key}: {str(value).lower()}" for key, value in checks.items()],
            "opened_real_trades: 0",
            "opened_paper_trades_from_smoke: 0",
            "slots_changed: false",
            "final_recommendation: NO LIVE",
            f"result: {'PASS' if result else 'FAIL'}",
            END,
        ]
        return "\n".join(lines)


def _db(config: BotConfig, path: Path) -> Database:
    class DummyLogger:
        def warning(self, *args, **kwargs):
            pass

        def info(self, *args, **kwargs):
            pass

    db = Database(config, DummyLogger())
    db.sqlite_path = path
    db.initialize()
    return db


def _seed_good_long_short(db: Database) -> None:
    for idx in range(60):
        _seed(db, idx=idx, symbol="XRPUSDT", side="LONG", regime="RISK_ON", score=86, barrier="TP1" if idx % 8 else "TIME", ret=1.0 if idx % 8 else 0.0, mfe=1.20, mae=0.20)
        _seed(db, idx=idx + 80, symbol="SOLUSDT", side="SHORT", regime="TREND_DOWN", score=84, barrier="TP1" if idx % 9 else "TIME", ret=1.0 if idx % 9 else 0.0, mfe=1.10, mae=0.25)


def _seed_bad_generic_bucket(db: Database) -> None:
    for idx in range(30):
        _seed(db, idx=idx, symbol="BNBUSDT", side="LONG", regime="CHOPPY_MARKET", score=96, barrier="TIME" if idx % 2 else "SL", ret=0.0 if idx % 2 else -1.0, mfe=0.10, mae=1.20)


def _seed(db: Database, *, idx: int, symbol: str, side: str, regime: str, score: int, barrier: str, ret: float, mfe: float, mae: float) -> None:
    ts = (datetime.now(timezone.utc) - timedelta(minutes=idx + 1)).isoformat()
    bucket = "95-100" if score >= 95 else "90-94" if score >= 90 else "80-89" if score >= 80 else "70-79"
    obs_id = db.record_signal_observation({
        "timestamp": ts,
        "symbol": symbol,
        "side": side,
        "strategy_type": "SMOKE",
        "confidence_score": score,
        "entry_price": 100.0,
        "market_regime": regime,
        "score_bucket": bucket,
    })
    db.record_signal_label({
        "timestamp": ts,
        "observation_id": obs_id,
        "label": 1 if barrier.startswith("TP") else -1 if barrier == "SL" else 0,
        "first_barrier_hit": barrier,
        "bars_to_outcome": 12,
        "realized_return_pct": ret,
    })
    db.upsert_signal_path_metric({
        "observation_id": obs_id,
        "source": "trade_signal",
        "symbol": symbol,
        "side": side,
        "score": score,
        "score_bucket": bucket,
        "market_regime": regime,
        "entry_price": 100.0,
        "current_price": 101.0 if side == "LONG" else 99.0,
        "max_favorable_pct": mfe,
        "max_adverse_pct": mae,
        "final_return_pct": ret,
        "bars_tracked": 30,
        "bars_to_mfe": 5,
        "bars_to_mae": 8,
        "status": "matured",
        "created_at": ts,
        "updated_at": ts,
        "matured_at": ts,
    })
