from datetime import datetime, timedelta, timezone

from app.config import BotConfig, PROJECT_ROOT
from app.daily_summary import END_MARKER, START_MARKER, DailyResearchSummary
from app.database import Database
from app.research_autopilot import ResearchAutopilot
from app.research_lab import ResearchLab


class DummyLogger:
    def __init__(self):
        self.messages = []

    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        self.messages.append(args[0] % args[1:] if args else "")

    def warning(self, *args, **kwargs):
        self.messages.append(args[0] % args[1:] if args else "")

    def error(self, *args, **kwargs):
        self.messages.append(args[0] % args[1:] if args else "")


def make_db(tmp_path):
    db = Database(BotConfig(), DummyLogger())
    db.sqlite_path = tmp_path / "daily_summary.db"
    db.initialize()
    return db


def insert_labeled(db, *, index=0, label=1, ret=None, symbol="BTCUSDT", strategy="BREAKOUT"):
    timestamp = datetime.now(timezone.utc) - timedelta(hours=1) + timedelta(minutes=index)
    ret = ret if ret is not None else (0.03 if label == 1 else -0.02 if label == -1 else 0.0)
    observation_id = db.record_signal_observation(
        {
            "timestamp": timestamp.isoformat(),
            "symbol": symbol,
            "side": "LONG",
            "strategy_type": strategy,
            "confidence_score": 88,
            "market_regime": "TREND_UP",
            "entry_price": 100.0,
            "stop_loss": 98.0,
            "take_profit_1": 103.0,
            "take_profit_2": 105.0,
            "risk_reward_ratio": 1.5,
            "spread_pct": 0.0005,
            "volume_relative": 1.5,
            "rsi_14": 55,
        }
    )
    db.record_signal_label(
        {
            "timestamp": timestamp.isoformat(),
            "observation_id": observation_id,
            "label": label,
            "first_barrier_hit": "TP1" if label == 1 else "SL" if label == -1 else "TIME",
            "bars_to_outcome": 4,
            "max_favorable_excursion": max(ret, 0.0),
            "max_adverse_excursion": min(ret, 0.0),
            "realized_return_pct": ret,
            "simulated_pnl": ret * 100,
            "would_have_won": int(label == 1),
        }
    )


def insert_stale_paper_trade(db):
    trade_id = db.record_trade(
        mode="paper",
        signal={
            "symbol": "BTCUSDT",
            "strategy_type": "BREAKOUT",
            "side": "LONG",
            "entry_price": 100.0,
            "stop_loss": 98.0,
            "take_profit_1": 103.0,
            "take_profit_2": 105.0,
            "trailing_stop_enabled": False,
            "position_size": 0.1,
            "leverage_recommendation": 3,
            "confidence_score": 88,
            "reason": "test",
        },
        status="PAPER_OPEN",
    )
    stale_time = (datetime.now(timezone.utc) - timedelta(hours=14)).isoformat()
    db._execute_sql("UPDATE trades SET timestamp=? WHERE id=?", (stale_time, trade_id))
    return trade_id


def test_daily_summary_prints_markers(tmp_path):
    db = make_db(tmp_path)
    insert_labeled(db, label=1)
    text = DailyResearchSummary(BotConfig(), db, DummyLogger()).build(hours=24)
    assert START_MARKER in text
    assert END_MARKER in text
    assert "recomendacion final: NO LIVE" in text


def test_daily_summary_does_not_load_full_tables(tmp_path):
    db = make_db(tmp_path)
    insert_labeled(db, label=1)

    def forbidden(*args, **kwargs):
        raise AssertionError("full table fetch should not be used")

    db.fetch_labeled_signal_rows = forbidden
    db.fetch_signal_observations = forbidden
    db.fetch_signal_labels = forbidden
    text = DailyResearchSummary(BotConfig(), db, DummyLogger()).build(hours=24)
    assert START_MARKER in text
    assert "total labels: 1" in text


def test_daily_summary_marks_stale_paper(tmp_path):
    db = make_db(tmp_path)
    insert_stale_paper_trade(db)
    text = DailyResearchSummary(BotConfig(stale_paper_trade_hours=12), db, DummyLogger()).build(hours=24)
    assert "stale paper trades: 1" in text
    assert "PAPER_OPEN" in text


def test_autopilot_saves_run_history(tmp_path):
    db = make_db(tmp_path)
    insert_labeled(db, label=1)
    config = BotConfig(
        enable_virtual_position_research=False,
        research_autopilot_phase2_limit_per_run=1,
        research_autopilot_batch_size=1,
    )
    result = ResearchAutopilot(config, db, DummyLogger()).run_once()
    runs = db.fetch_research_autopilot_runs()
    assert runs
    assert runs[0]["status"] in {"COMPLETED", "FAILED"}
    assert runs[0]["processed"] == result.phase2.processed_labels


def test_research_lab_daily_summary_method(tmp_path):
    db = make_db(tmp_path)
    text = ResearchLab(db, BotConfig(), DummyLogger(), reports_dir=tmp_path / "reports").daily_summary(hours=24)
    assert START_MARKER in text
    assert END_MARKER in text


def test_daily_summary_no_live_coupling():
    for relative in ("app/daily_summary.py", "app/research_autopilot.py"):
        text = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
        assert "ExecutionEngine" not in text
        assert "RiskManager" not in text
        assert "BitgetClient" not in text
        assert "LIVE_TRADING=true" not in text
    risk_text = (PROJECT_ROOT / "app" / "risk_manager.py").read_text(encoding="utf-8")
    execution_text = (PROJECT_ROOT / "app" / "execution_engine.py").read_text(encoding="utf-8")
    assert "DailyResearchSummary" not in risk_text
    assert "DailyResearchSummary" not in execution_text
