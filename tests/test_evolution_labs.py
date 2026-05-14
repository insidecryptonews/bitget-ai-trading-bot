from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.config import BotConfig, PROJECT_ROOT
from app.database import Database
from app.evolution_score import EvolutionScore
from app.exit_simulation_lab import ExitSimulationLab
from app.mfe_mae_diagnostic import MfeMaeDiagnostic
from app.mfe_mae_smoke_test import MfeMaeSmokeTest
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


def test_mfe_tracker_registers_rejected_signal_with_sufficient_score(tmp_path):
    db = make_db(tmp_path)
    obs_id = db.record_signal_observation({"timestamp": datetime.now(timezone.utc).isoformat(), "symbol": "ETHUSDT", "side": "LONG", "confidence_score": 65, "entry_price": 100.0})
    tracker = MfeMaeTracker(BotConfig(mfe_mae_track_min_score=70, mfe_mae_min_rejected_score=60), db, DummyLogger())
    metric_id = tracker.register_signal(
        observation_id=obs_id,
        signal=make_signal(symbol="ETHUSDT", score=65),
        snapshot=Snapshot(100.0),
        market_regime="CHOPPY_MARKET",
        source="allocator_reject",
        reject_reason="sin slots",
    )
    assert metric_id > 0
    assert tracker.debug_result().candidates_tracked == 1


def test_mfe_tracker_does_not_register_no_trade_score_zero_when_disabled(tmp_path):
    db = make_db(tmp_path)
    obs_id = db.record_signal_observation({"timestamp": datetime.now(timezone.utc).isoformat(), "symbol": "ETHUSDT", "side": "NO_TRADE", "confidence_score": 0, "entry_price": 0.0})
    signal = make_signal(symbol="ETHUSDT", side="NO_TRADE", score=0, entry=0.0)
    tracker = MfeMaeTracker(BotConfig(mfe_mae_track_no_trade=False), db, DummyLogger())
    assert tracker.register_signal(observation_id=obs_id, signal=signal, snapshot=Snapshot(100.0), market_regime="RANGE", source="regime_block") == 0
    assert tracker.debug_result().candidates_tracked == 0


def test_mfe_tracker_registers_high_score_missed_below_track_min(tmp_path):
    db = make_db(tmp_path)
    obs_id = db.record_signal_observation({"timestamp": datetime.now(timezone.utc).isoformat(), "symbol": "SOLUSDT", "side": "LONG", "confidence_score": 65, "entry_price": 100.0})
    tracker = MfeMaeTracker(BotConfig(mfe_mae_track_min_score=70, mfe_mae_min_rejected_score=60), db, DummyLogger())
    assert tracker.register_signal(observation_id=obs_id, signal=make_signal(symbol="SOLUSDT", score=65), snapshot=Snapshot(100.0), market_regime="TREND_DOWN", source="high_score_missed") > 0
    assert tracker.debug_result().by_source["high_score_missed"] == 1


def test_mfe_tracker_registers_edge_guard_block(tmp_path):
    db = make_db(tmp_path)
    obs_id = db.record_signal_observation({"timestamp": datetime.now(timezone.utc).isoformat(), "symbol": "ADAUSDT", "side": "SHORT", "confidence_score": 62, "entry_price": 100.0})
    tracker = MfeMaeTracker(BotConfig(mfe_mae_min_rejected_score=60), db, DummyLogger())
    assert tracker.register_signal(observation_id=obs_id, signal=make_signal(symbol="ADAUSDT", side="SHORT", score=62), snapshot=Snapshot(100.0), market_regime="RISK_OFF", source="edge_guard_block") > 0
    assert tracker.debug_result().by_source["edge_guard_block"] == 1


def test_mfe_tracker_no_duplicates_by_observation_id(tmp_path):
    db = make_db(tmp_path)
    obs_id = db.record_signal_observation({"timestamp": datetime.now(timezone.utc).isoformat(), "symbol": "BTCUSDT", "side": "LONG", "confidence_score": 88, "entry_price": 100.0})
    tracker = MfeMaeTracker(BotConfig(), db, DummyLogger())
    assert tracker.register_signal(observation_id=obs_id, signal=make_signal(), snapshot=Snapshot(100.0), market_regime="TREND_DOWN") > 0
    assert tracker.register_signal(observation_id=obs_id, signal=make_signal(), snapshot=Snapshot(100.0), market_regime="TREND_DOWN") == 0
    assert tracker.debug_result().skipped_duplicate == 1


def test_mfe_tracker_respects_max_active(tmp_path):
    db = make_db(tmp_path)
    obs_id = db.record_signal_observation({"timestamp": datetime.now(timezone.utc).isoformat(), "symbol": "BTCUSDT", "side": "LONG", "confidence_score": 88, "entry_price": 100.0})
    tracker = MfeMaeTracker(BotConfig(mfe_mae_max_active=0), db, DummyLogger())
    assert tracker.register_signal(observation_id=obs_id, signal=make_signal(), snapshot=Snapshot(100.0), market_regime="TREND_DOWN") == 0
    assert tracker.debug_result().skipped_max_active == 1


def test_market_probes_create_long_short_without_paper(tmp_path):
    db = make_db(tmp_path)
    tracker = MfeMaeTracker(BotConfig(mfe_mae_probe_every_n_cycles=1, mfe_mae_probe_top_n_symbols=1), db, DummyLogger())
    result = tracker.register_market_probes(
        snapshots={"BTCUSDT": Snapshot(100.0)},
        market_regime="CHOPPY_MARKET",
        cycle_count=1,
    )
    rows = db.fetch_signal_path_metrics_since((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat())
    assert result.market_probes_created == 2
    assert {row["side"] for row in rows} == {"LONG", "SHORT"}
    assert {row["source"] for row in rows} == {"market_probe"}
    assert db.get_paper_trade_summary()["open"] == 0


def test_market_probes_respect_max_per_cycle_and_duplicates(tmp_path):
    db = make_db(tmp_path)
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    tracker = MfeMaeTracker(BotConfig(mfe_mae_probe_every_n_cycles=1, mfe_mae_probe_top_n_symbols=3, mfe_mae_probe_max_per_cycle=2), db, DummyLogger())
    tracker.register_market_probes(
        snapshots={"BTCUSDT": Snapshot(100.0), "ETHUSDT": Snapshot(200.0), "SOLUSDT": Snapshot(10.0)},
        market_regime="RANGE",
        cycle_count=1,
        now=now,
    )
    tracker.register_market_probes(
        snapshots={"BTCUSDT": Snapshot(100.0), "ETHUSDT": Snapshot(200.0), "SOLUSDT": Snapshot(10.0)},
        market_regime="RANGE",
        cycle_count=1,
        now=now,
    )
    rows = db.fetch_signal_path_metrics_since((datetime.now(timezone.utc) - timedelta(days=365)).isoformat())
    assert len(rows) == 2
    assert tracker.debug_result().skipped_duplicate >= 1


def test_market_probes_respect_max_active(tmp_path):
    db = make_db(tmp_path)
    tracker = MfeMaeTracker(BotConfig(mfe_mae_probe_every_n_cycles=1, mfe_mae_max_active=0), db, DummyLogger())
    result = tracker.register_market_probes(
        snapshots={"BTCUSDT": Snapshot(100.0)},
        market_regime="RANGE",
        cycle_count=1,
    )
    assert result.market_probes_created == 0
    assert result.skipped_max_active >= 1


def test_low_score_sampling_tracks_controlled_sample(tmp_path):
    db = make_db(tmp_path)
    obs_id = db.record_signal_observation({"timestamp": datetime.now(timezone.utc).isoformat(), "symbol": "BTCUSDT", "side": "LONG", "confidence_score": 30, "entry_price": 100.0})
    tracker = MfeMaeTracker(BotConfig(mfe_mae_low_score_sample_rate=1.0, mfe_mae_low_score_max_per_cycle=2), db, DummyLogger())
    result = tracker.register_low_score_samples(
        signals=[make_signal(score=30)],
        snapshots={"BTCUSDT": Snapshot(100.0)},
        observation_ids={"BTCUSDT": obs_id},
        market_regime="CHOPPY_MARKET",
    )
    assert result.low_score_samples_tracked == 1
    assert db.fetch_signal_path_metrics_since((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat())[0]["source"] == "low_score_reject"


def test_low_score_sampling_skips_no_trade_score_zero(tmp_path):
    db = make_db(tmp_path)
    tracker = MfeMaeTracker(BotConfig(mfe_mae_low_score_sample_rate=1.0), db, DummyLogger())
    result = tracker.register_low_score_samples(
        signals=[make_signal(side="NO_TRADE", score=0, entry=0.0)],
        snapshots={"BTCUSDT": Snapshot(100.0)},
        observation_ids={},
        market_regime="RANGE",
    )
    assert result.low_score_samples_tracked == 0


def test_active_metrics_update_and_mature(tmp_path):
    db = make_db(tmp_path)
    obs_id = db.record_signal_observation({"timestamp": datetime.now(timezone.utc).isoformat(), "symbol": "BTCUSDT", "side": "LONG", "confidence_score": 88, "entry_price": 100.0})
    tracker = MfeMaeTracker(BotConfig(mfe_mae_max_bars=1), db, DummyLogger())
    tracker.register_signal(observation_id=obs_id, signal=make_signal(), snapshot=Snapshot(100.0), market_regime="TREND_DOWN")
    result = tracker.update_active({"BTCUSDT": Snapshot(101.0)})
    rows = db.fetch_signal_path_metrics_since((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat())
    assert result.matured == 1
    assert rows[0]["status"] == "matured"
    assert rows[0]["max_favorable_pct"] > 0


def test_exit_simulation_cli_exists_and_handles_insufficient_data(tmp_path):
    db = make_db(tmp_path)
    text = ExitSimulationLab(BotConfig(), db).to_text(hours=24)
    assert "EXIT SIMULATION START" in text
    assert "table_exists_but_empty" in text
    assert "EXIT SIMULATION END" in text
    assert '"exit-simulation"' in (PROJECT_ROOT / "app" / "research_lab.py").read_text(encoding="utf-8")


def test_exit_simulation_distinguishes_active_not_matured(tmp_path):
    db = make_db(tmp_path)
    obs_id = db.record_signal_observation({"timestamp": datetime.now(timezone.utc).isoformat(), "symbol": "BTCUSDT", "side": "LONG", "confidence_score": 88, "entry_price": 100.0})
    tracker = MfeMaeTracker(BotConfig(mfe_mae_max_bars=30), db, DummyLogger())
    tracker.register_signal(observation_id=obs_id, signal=make_signal(), snapshot=Snapshot(100.0), market_regime="TREND_DOWN")
    payload = ExitSimulationLab(BotConfig(), db).build(hours=24)
    assert payload["status"] == "only_active_not_matured"


def test_exit_simulation_marks_probe_data_only(tmp_path):
    db = make_db(tmp_path)
    tracker = MfeMaeTracker(BotConfig(mfe_mae_probe_every_n_cycles=1, mfe_mae_max_bars=1), db, DummyLogger())
    for index in range(30):
        symbol = f"PROBE{index}"
        tracker.register_market_probes(
            snapshots={symbol: Snapshot(100.0 + index)},
            market_regime="RANGE",
            cycle_count=1,
            now=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc) + timedelta(minutes=5 * index),
        )
    active_rows = db.fetch_active_signal_path_metrics(limit=100)
    tracker.update_active({row["symbol"]: Snapshot(float(row["entry_price"]) + 0.5) for row in active_rows})
    payload = ExitSimulationLab(BotConfig(), db).build(hours=24)
    assert payload["status"] == "probe_data_only_not_signal_edge"
    assert payload["by_source_best"]


def test_mfe_mae_diagnostic_prints_markers(tmp_path):
    db = make_db(tmp_path)
    text = MfeMaeDiagnostic(BotConfig(), db).to_text(hours=24)
    assert "MFE MAE DIAGNOSTIC START" in text
    assert "MFE MAE DIAGNOSTIC END" in text


def test_mfe_mae_smoke_test_creates_rows_without_paper_or_live(tmp_path):
    db = make_db(tmp_path)
    text = MfeMaeSmokeTest(BotConfig(), db, DummyLogger()).to_text()
    assert "MFE MAE SMOKE TEST START" in text
    assert "result: PASS" in text
    assert "opened_paper_trades=0" in text
    assert db.get_signal_path_metrics_summary_since("1970-01-01T00:00:00+00:00")["total"] > 0


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


def test_evolution_score_separates_probe_coverage_and_gates(tmp_path):
    db = make_db(tmp_path)
    tracker = MfeMaeTracker(BotConfig(mfe_mae_probe_every_n_cycles=1, mfe_mae_max_bars=1), db, DummyLogger())
    tracker.register_market_probes(snapshots={"BTCUSDT": Snapshot(100.0)}, market_regime="RANGE", cycle_count=1)
    tracker.update_active({"BTCUSDT": Snapshot(100.5)})
    payload = EvolutionScore(BotConfig(), db).build(hours=24)
    assert payload["matured_probe_samples"] > 0
    assert payload["go_live_gates"]["live_allowed"] is False


def test_new_research_cli_commands_exist():
    text = (PROJECT_ROOT / "app" / "research_lab.py").read_text(encoding="utf-8")
    for command in ("exit-simulation", "score-calibration", "shadow-experiments", "evolution-score", "mfe-mae-diagnostic", "mfe-mae-smoke-test"):
        assert f'"{command}"' in text
