from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.config import BotConfig, PROJECT_ROOT
from app.database import Database
from app.paper_reconciler import PaperReconciler
from app.paper_trader import PaperTrader
from tests.helpers import DummyLogger, signal


class DummyTelegram:
    def trade_opened(self, *args, **kwargs):
        pass

    def send(self, *args, **kwargs):
        pass


def make_db(tmp_path):
    db = Database(BotConfig(), DummyLogger())
    db.sqlite_path = tmp_path / "paper_reconcile.db"
    db.initialize()
    return db


def open_trade(db, *, mode="paper", status="PAPER_OPEN", symbol="BTCUSDT", side="LONG", entry=100.0):
    trade_id = db.record_trade(
        mode=mode,
        signal={
            "symbol": symbol,
            "side": side,
            "strategy_type": "BREAKOUT",
            "entry_price": entry,
            "stop_loss": 98.0 if side == "LONG" else 102.0,
            "take_profit_1": 103.0 if side == "LONG" else 97.0,
            "take_profit_2": 105.0 if side == "LONG" else 95.0,
            "position_size": 1.0,
            "leverage_recommendation": 3,
            "confidence_score": 88,
            "reason": "test",
        },
        status=status,
    )
    return trade_id


def set_trade_timestamp(db, trade_id, timestamp):
    db._execute_sql("UPDATE trades SET timestamp=? WHERE id=?", (timestamp.isoformat(), trade_id))


def add_matching_label(db, *, symbol="BTCUSDT", side="LONG", entry=100.0, barrier="SL", label=-1):
    observation_id = db.record_signal_observation(
        {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "side": side,
            "strategy_type": "BREAKOUT",
            "entry_price": entry,
            "stop_loss": 98.0 if side == "LONG" else 102.0,
            "take_profit_1": 103.0 if side == "LONG" else 97.0,
            "take_profit_2": 105.0 if side == "LONG" else 95.0,
            "operated": 1,
        }
    )
    db.record_signal_label(
        {
            "observation_id": observation_id,
            "label": label,
            "first_barrier_hit": barrier,
            "bars_to_outcome": 4,
            "realized_return_pct": -0.02 if label == -1 else 0.03,
            "simulated_pnl": -0.2 if label == -1 else 0.3,
            "would_have_won": int(label == 1),
        }
    )


def test_reconcile_detects_old_paper_open_and_closes_by_time(tmp_path):
    db = make_db(tmp_path)
    trade_id = open_trade(db)
    set_trade_timestamp(db, trade_id, datetime.now(timezone.utc) - timedelta(days=2))
    result = PaperReconciler(BotConfig(max_holding_bars=1), db, DummyLogger()).reconcile()
    trade = db.fetch_trades()[0]
    assert result.paper_open_before == 1
    assert result.stale_paper_trades_found == 1
    assert result.paper_trades_closed_by_time == 1
    assert result.paper_open_after == 0
    assert trade["status"] == "TIME_EXIT"


def test_reconcile_closes_paper_open_by_resolved_label(tmp_path):
    db = make_db(tmp_path)
    open_trade(db, symbol="ETHUSDT", side="LONG", entry=2300.0)
    add_matching_label(db, symbol="ETHUSDT", side="LONG", entry=2300.0, barrier="SL", label=-1)
    result = PaperReconciler(BotConfig(), db, DummyLogger()).reconcile()
    trade = db.fetch_trades()[0]
    assert result.paper_trades_closed_by_label == 1
    assert trade["status"] == "STOP_LOSS"
    assert trade["realized_pnl"] == -0.2


def test_reconcile_does_not_touch_live_trades(tmp_path):
    db = make_db(tmp_path)
    trade_id = open_trade(db, mode="live", status="LIVE_OPEN")
    set_trade_timestamp(db, trade_id, datetime.now(timezone.utc) - timedelta(days=2))
    result = PaperReconciler(BotConfig(max_holding_bars=1), db, DummyLogger()).reconcile()
    trade = db.fetch_trades()[0]
    assert result.paper_open_before == 0
    assert trade["status"] == "LIVE_OPEN"


def test_reconcile_does_not_duplicate_closures_or_reclose_closed_trades(tmp_path):
    db = make_db(tmp_path)
    trade_id = open_trade(db)
    db.update_trade_status(trade_id, "STOP_LOSS", realized_pnl=-1.0)
    first = PaperReconciler(BotConfig(), db, DummyLogger()).reconcile()
    second = PaperReconciler(BotConfig(), db, DummyLogger()).reconcile()
    trade = db.fetch_trades()[0]
    assert first.paper_open_before == 0
    assert second.paper_open_before == 0
    assert trade["status"] == "STOP_LOSS"
    assert trade["realized_pnl"] == -1.0


def test_paper_trader_loads_db_open_positions_for_consistent_count(tmp_path):
    db = make_db(tmp_path)
    open_trade(db, symbol="DOGEUSDT", side="SHORT", entry=0.2)
    trader = PaperTrader(BotConfig(), db, DummyTelegram(), DummyLogger())
    loaded = trader.load_open_positions_from_db()
    assert loaded == 1
    assert len(trader.open_positions()) == db.get_paper_trade_summary()["open"] == 1
    assert trader.open_positions()[0]["symbol"] == "DOGEUSDT"


def test_reconcile_command_markers_present(tmp_path):
    db = make_db(tmp_path)
    open_trade(db)
    text = PaperReconciler(BotConfig(), db, DummyLogger()).reconcile().to_text()
    assert "PAPER RECONCILE START" in text
    assert "paper open before" in text
    assert "PAPER RECONCILE END" in text


def test_paper_reconcile_not_coupled_to_live_risk_or_execution():
    text = (PROJECT_ROOT / "app" / "paper_reconciler.py").read_text(encoding="utf-8")
    assert "ExecutionEngine" not in text
    assert "RiskManager" not in text
    assert "private_" not in text
    assert "BitgetClient" not in text
    assert BotConfig().enable_paper_reconcile_on_start is False

