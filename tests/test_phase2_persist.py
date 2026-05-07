from datetime import datetime, timedelta, timezone

from app.config import BotConfig, PROJECT_ROOT
from app.database import Database
from app.phase2_persist import Phase2Persister


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
    db.sqlite_path = tmp_path / "phase2_persist.db"
    db.initialize()
    return db


def insert_labeled(db, index=0, label=1, side="LONG", barrier=None, ret=None):
    barrier = barrier or ("TP1" if label == 1 else "SL" if label == -1 else "TIME")
    ret = ret if ret is not None else (0.03 if label == 1 else -0.02 if label == -1 else 0.0)
    timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=index)
    observation_id = db.record_signal_observation(
        {
            "timestamp": timestamp.isoformat(),
            "symbol": "BTCUSDT",
            "side": side,
            "strategy_type": "BREAKOUT",
            "confidence_score": 88,
            "market_regime": "TREND_UP",
            "entry_price": 100.0,
            "stop_loss": 98.0 if side == "LONG" else 102.0,
            "take_profit_1": 103.0 if side == "LONG" else 97.0,
            "take_profit_2": 105.0 if side == "LONG" else 95.0,
            "risk_reward_ratio": 1.5,
            "volume_relative": 1.5,
            "spread_pct": 0.0005,
            "rsi_14": 55,
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
    label_id = db.record_signal_label(
        {
            "observation_id": observation_id,
            "label": label,
            "first_barrier_hit": barrier,
            "bars_to_outcome": 4,
            "max_favorable_excursion": max(ret, 0.0),
            "max_adverse_excursion": min(ret, 0.0),
            "realized_return_pct": ret,
            "simulated_pnl": ret * 100,
            "would_have_won": int(label == 1),
        }
    )
    return observation_id, label_id


def test_phase2_persist_creates_signal_explanations(tmp_path):
    db = make_db(tmp_path)
    insert_labeled(db, label=-1)
    result = Phase2Persister(db, DummyLogger()).persist(limit=1, batch_size=1, progress=None)
    assert result.explanations_created == 1
    assert len(db.fetch_signal_explanations()) == 1


def test_phase2_persist_creates_counterfactuals(tmp_path):
    db = make_db(tmp_path)
    insert_labeled(db, label=-1)
    result = Phase2Persister(db, DummyLogger()).persist(limit=1, batch_size=1, progress=None)
    assert result.counterfactuals_created > 0
    assert len(db.fetch_signal_counterfactuals()) == result.counterfactuals_created


def test_phase2_persist_creates_win_clusters(tmp_path):
    db = make_db(tmp_path)
    insert_labeled(db, index=1, label=1)
    insert_labeled(db, index=2, label=-1)
    Phase2Persister(db, DummyLogger()).persist(limit=2, batch_size=1, progress=None)
    assert len(db.fetch_win_clusters()) >= 1


def test_phase2_persist_creates_stop_loss_clusters(tmp_path):
    db = make_db(tmp_path)
    insert_labeled(db, index=1, label=-1)
    Phase2Persister(db, DummyLogger()).persist(limit=1, batch_size=1, progress=None)
    assert len(db.fetch_stop_loss_failure_clusters()) >= 1


def test_phase2_persist_creates_research_rules(tmp_path):
    db = make_db(tmp_path)
    insert_labeled(db, index=1, label=1)
    insert_labeled(db, index=2, label=-1)
    Phase2Persister(db, DummyLogger()).persist(limit=2, batch_size=2, progress=None)
    assert len(db.fetch_research_rules()) >= 1


def test_phase2_persist_is_idempotent(tmp_path):
    db = make_db(tmp_path)
    insert_labeled(db, index=1, label=1)
    insert_labeled(db, index=2, label=-1)
    persister = Phase2Persister(db, DummyLogger())
    first = persister.persist(limit=2, batch_size=1, progress=None)
    counts_after_first = {
        "explanations": len(db.fetch_signal_explanations()),
        "paths": len(db.fetch_signal_price_paths()),
        "counterfactuals": len(db.fetch_signal_counterfactuals()),
        "wins": len(db.fetch_win_clusters()),
        "rules": len(db.fetch_research_rules()),
    }
    second = persister.persist(limit=2, batch_size=1, progress=None)
    assert second.processed_labels == 0
    assert first.explanations_created == 2
    assert counts_after_first == {
        "explanations": len(db.fetch_signal_explanations()),
        "paths": len(db.fetch_signal_price_paths()),
        "counterfactuals": len(db.fetch_signal_counterfactuals()),
        "wins": len(db.fetch_win_clusters()),
        "rules": len(db.fetch_research_rules()),
    }


def test_phase2_persist_respects_limit_and_batch_size(tmp_path):
    db = make_db(tmp_path)
    for index in range(5):
        insert_labeled(db, index=index, label=1 if index % 2 == 0 else -1)
    result = Phase2Persister(db, DummyLogger()).persist(limit=3, batch_size=2, progress=None)
    assert result.target_labels == 3
    assert result.processed_labels == 3
    assert len(db.fetch_signal_explanations()) == 3


def test_phase2_persist_limit_zero_processes_nothing(tmp_path):
    db = make_db(tmp_path)
    insert_labeled(db, index=1, label=1)
    result = Phase2Persister(db, DummyLogger()).persist(limit=0, batch_size=1, progress=None)
    assert result.target_labels == 0
    assert result.processed_labels == 0
    assert db.fetch_signal_explanations() == []


def test_phase2_persist_does_not_activate_live():
    config = BotConfig()
    assert config.live_trading is False
    assert config.dry_run is True
    assert config.enable_phase2_persist is False


def test_phase2_persist_not_coupled_to_risk_manager_or_execution_engine():
    risk_text = (PROJECT_ROOT / "app" / "risk_manager.py").read_text(encoding="utf-8")
    execution_text = (PROJECT_ROOT / "app" / "execution_engine.py").read_text(encoding="utf-8")
    assert "Phase2Persister" not in risk_text
    assert "phase2_persist" not in risk_text
    assert "Phase2Persister" not in execution_text
    assert "phase2_persist" not in execution_text
