from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.config import BotConfig, PROJECT_ROOT
from app.database import Database
from app.evolution_score import EvolutionScore
from app.exit_simulation_lab import ExitSimulationLab
from app.mfe_mae_tracker import MfeMaeTracker
from app.score_calibration_lab import ScoreCalibrationLab
from app.signal_engine import Signal


class DummyLogger:
    def warning(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass


class Snapshot:
    def __init__(self, price: float):
        self.current_price = price


def make_db(tmp_path):
    db = Database(BotConfig(), DummyLogger())
    db.sqlite_path = tmp_path / "evolution.db"
    db.initialize()
    return db


def make_signal(symbol="BTCUSDT", side="LONG", score=88, entry=100.0):
    return Signal(
        symbol=symbol,
        side=side,
        strategy_type="TREND_FOLLOWING",
        confidence_score=score,
        entry_price=entry,
        stop_loss=99.0,
        take_profit_1=101.0,
        take_profit_2=102.0,
        trailing_stop_enabled=False,
        trailing_stop_rule="",
        risk_reward_ratio=1.5,
        leverage_recommendation=3,
        position_size=0.0,
        reason="test",
    )


def record_labeled_observation(db, score: int, barrier: str, return_pct: float, timestamp: str | None = None):
    ts = timestamp or datetime.now(timezone.utc).isoformat()
    obs_id = db.record_signal_observation(
        {
            "timestamp": ts,
            "symbol": "BTCUSDT",
            "side": "LONG",
            "strategy_type": "TREND_FOLLOWING",
            "confidence_score": score,
            "market_regime": "TREND_DOWN",
            "entry_price": 100.0,
            "stop_loss": 99.0,
            "take_profit_1": 101.0,
            "take_profit_2": 102.0,
        }
    )
    db.record_signal_label(
        {
            "timestamp": ts,
            "observation_id": obs_id,
            "label": 1 if barrier.startswith("TP") else -1 if barrier == "SL" else 0,
            "first_barrier_hit": barrier,
            "bars_to_outcome": 10,
            "realized_return_pct": return_pct,
        }
    )
    return obs_id


def test_mfe_mae_tracker_creates_compact_metrics(tmp_path):
    db = make_db(tmp_path)
    signal = make_signal(entry=100.0)
    obs_id = db.record_signal_observation(
        {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": "BTCUSDT",
            "side": "LONG",
            "confidence_score": 88,
            "entry_price": 100.0,
        }
    )
    tracker = MfeMaeTracker(BotConfig(mfe_mae_max_bars=2), db, DummyLogger())
    assert tracker.register_signal(observation_id=obs_id, signal=signal, snapshot=Snapshot(100.0), market_regime="TREND_DOWN") > 0
    tracker.update_active({"BTCUSDT": Snapshot(101.0)})
    tracker.update_active({"BTCUSDT": Snapshot(98.0)})
    rows = db.fetch_signal_path_metrics_since((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat())
    assert len(rows) == 1
    row = rows[0]
    assert row["max_favorable_pct"] > 0
    assert row["max_adverse_pct"] > 0
    assert "candles" not in row
    assert "forecast_json" not in row


def test_exit_simulation_cli_exists_and_handles_insufficient_data(tmp_path):
    db = make_db(tmp_path)
    text = ExitSimulationLab(BotConfig(), db).to_text(hours=24)
    assert "EXIT SIMULATION START" in text
    assert "insufficient_mfe_mae_data" in text
    assert "EXIT SIMULATION END" in text
    assert '"exit-simulation"' in (PROJECT_ROOT / "app" / "research_lab.py").read_text(encoding="utf-8")


def test_score_calibration_detects_non_monotonic(tmp_path):
    db = make_db(tmp_path)
    now = datetime.now(timezone.utc).isoformat()
    for _ in range(4):
        record_labeled_observation(db, 85, "TP1", 1.0, now)
    for _ in range(4):
        record_labeled_observation(db, 97, "SL", -1.0, now)
    report = ScoreCalibrationLab(BotConfig(), db).build(hours=24)
    assert report["diagnosis"]["score_not_monotonic"] is True
    text = ScoreCalibrationLab(BotConfig(), db).to_text(hours=24)
    assert "SCORE CALIBRATION START" in text
    assert "SCORE CALIBRATION END" in text


def test_evolution_score_returns_quality_sections(tmp_path):
    db = make_db(tmp_path)
    record_labeled_observation(db, 85, "SL", -1.0)
    payload = EvolutionScore(BotConfig(), db).build(hours=24)
    for key in ("data_quality", "edge_quality", "stability", "safety", "final_status"):
        assert key in payload
    assert payload["final_recommendation"] == "NO LIVE"


def test_new_research_cli_commands_exist():
    text = (PROJECT_ROOT / "app" / "research_lab.py").read_text(encoding="utf-8")
    for command in ("exit-simulation", "score-calibration", "shadow-experiments", "evolution-score"):
        assert f'"{command}"' in text
