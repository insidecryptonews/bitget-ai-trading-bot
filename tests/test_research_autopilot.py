from datetime import datetime, timedelta, timezone

from app.config import BotConfig, PROJECT_ROOT
from app.database import Database
from app.phase2_persist import Phase2Persister
from app.research_autopilot import ResearchAutopilot
from app.virtual_portfolio import VirtualPortfolioResearch


class DummyLogger:
    def __init__(self):
        self.messages = []

    def info(self, *args, **kwargs):
        self.messages.append(args[0] % args[1:] if args else "")

    def warning(self, *args, **kwargs):
        self.messages.append(args[0] % args[1:] if args else "")

    def debug(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


def make_db(tmp_path):
    db = Database(BotConfig(), DummyLogger())
    db.sqlite_path = tmp_path / "research_autopilot.db"
    db.initialize()
    return db


def insert_labeled(db, index=0, label=1, symbol="BTCUSDT", strategy="BREAKOUT"):
    timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=index)
    ret = 0.03 if label == 1 else -0.02 if label == -1 else 0.0
    obs_id = db.record_signal_observation(
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
            "rsi_14": 55,
            "volume_relative": 1.8,
            "distance_to_ema_21": 0.01,
            "spread_pct": 0.0005,
            "normalized_atr": 0.01,
            "momentum_5": 0.01,
            "momentum_15": 0.02,
            "btc_momentum_5": 0.01,
            "btc_momentum_15": 0.02,
            "eth_momentum_5": 0.01,
            "market_risk_on": 1,
            "market_risk_off": 0,
            "number_of_symbols_bullish": 7,
            "number_of_symbols_bearish": 3,
        }
    )
    db.record_signal_label(
        {
            "observation_id": obs_id,
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


def test_research_autopilot_disabled_by_default():
    assert BotConfig().enable_research_autopilot is False


def test_autopilot_once_does_not_activate_live(tmp_path):
    db = make_db(tmp_path)
    insert_labeled(db, label=1)
    config = BotConfig()
    text = ResearchAutopilot(config, db, DummyLogger()).run_once().to_text()
    assert config.live_trading is False
    assert config.dry_run is True
    assert "RESEARCH AUTOPILOT START" in text
    assert "final recommendation: NO LIVE" in text


def test_virtual_portfolio_does_not_use_execution_engine():
    text = (PROJECT_ROOT / "app" / "virtual_portfolio.py").read_text(encoding="utf-8")
    assert "ExecutionEngine" not in text
    assert "RiskManager" not in text
    assert "BitgetClient" not in text


def test_virtual_portfolio_allows_many_virtual_positions(tmp_path):
    db = make_db(tmp_path)
    for index in range(20):
        insert_labeled(db, index=index, label=1 if index % 2 == 0 else -1, symbol="BTCUSDT" if index % 2 == 0 else "ETHUSDT")
    result = VirtualPortfolioResearch(db, DummyLogger()).simulate(limit=20, max_concurrent=1000)
    assert result.labels_loaded == 20
    assert result.virtual_trades_simulated > 20
    assert result.virtual_trades_created > 20
    assert db.fetch_trades() == []


def test_phase2_persist_remains_idempotent_under_autopilot_stack(tmp_path):
    db = make_db(tmp_path)
    insert_labeled(db, index=1, label=1)
    persister = Phase2Persister(db, DummyLogger())
    first = persister.persist(limit=1, batch_size=1, progress=None)
    second = persister.persist(limit=1, batch_size=1, progress=None)
    assert first.explanations_created == 1
    assert second.processed_labels == 0
    assert len(db.fetch_signal_explanations()) == 1


class BrokenDb:
    def count_phase2_pending_labels(self):
        raise RuntimeError("boom")

    def fetch_phase2_labeled_rows(self, *args, **kwargs):
        raise RuntimeError("boom")


def test_autopilot_failures_do_not_raise():
    result = ResearchAutopilot(BotConfig(), BrokenDb(), DummyLogger()).run_once()
    assert result.errors >= 1
    assert "final recommendation: NO LIVE" in result.to_text()


def test_autopilot_recommended_output_stays_no_live(tmp_path):
    db = make_db(tmp_path)
    insert_labeled(db, label=1)
    output = ResearchAutopilot(BotConfig(), db, DummyLogger()).run_once().to_text()
    assert "final recommendation: NO LIVE" in output
    assert "LIVE_TRADING=true" not in output
