from contextlib import contextmanager

from app.config import BotConfig
from app.database import Database
from app.main import _emit_research_auto_report_if_due
from app.research_engine import ResearchEngine


class DummyLogger:
    def __init__(self):
        self.messages = []

    def warning(self, *args, **kwargs):
        self.messages.append(args[0] % args[1:] if args else "")

    def info(self, *args, **kwargs):
        self.messages.append(args[0] % args[1:] if args else "")


class FakeCursor:
    def __init__(self, rows=None, row=None):
        self._rows = rows or []
        self._row = row

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._row


class FakePostgresConnection:
    def __init__(self):
        self.sql_seen = []
        self.signal_observations = [{"id": 1, "symbol": "BTCUSDT", "side": "LONG", "operated": 0}]
        self.trades = [
            {"id": 9, "symbol": "BTCUSDT", "status": "PAPER_OPEN", "mode": "paper"},
            {"id": 10, "symbol": "ETHUSDT", "status": "STOP_LOSS", "mode": "paper"},
        ]
        self.signal_labels = []
        self.signal_updates = []
        self.trade_updates = []

    def execute(self, sql, params=()):
        self.sql_seen.append((sql, params))
        clean_sql = " ".join(sql.split())
        if clean_sql.startswith("INSERT INTO signal_observations"):
            assert "RETURNING id" in clean_sql
            self.signal_observations.append({"id": 101, "symbol": "BTCUSDT", "side": "LONG", "operated": 0})
            return FakeCursor(row={"id": 101})
        if clean_sql.startswith("UPDATE signal_observations"):
            self.signal_updates.append((sql, params))
            observation_id = params[-1]
            for row in self.signal_observations:
                if row["id"] == observation_id:
                    if "operated" in sql:
                        row["operated"] = params[0]
                    if "selected_by_allocator" in sql:
                        row["selected_by_allocator"] = params[1 if "operated" in sql else 0]
                    if "risk_manager_approved" in sql:
                        row["risk_manager_approved"] = params[-2]
            return FakeCursor()
        if clean_sql.startswith("INSERT INTO trades"):
            assert "RETURNING id" in clean_sql
            self.trades.append({"id": 202, "symbol": "BTCUSDT", "status": "PAPER_OPEN", "mode": "paper"})
            return FakeCursor(row={"id": 202})
        if clean_sql.startswith("UPDATE trades"):
            self.trade_updates.append((sql, params))
            trade_id = params[-1]
            for row in self.trades:
                if row["id"] == trade_id:
                    row["status"] = params[0]
            return FakeCursor()
        if clean_sql.startswith("INSERT INTO signal_labels"):
            assert "RETURNING id" in clean_sql
            self.signal_labels.append({"id": 303, "observation_id": params[1], "label": params[2]})
            return FakeCursor(row={"id": 303})
        if "LEFT JOIN signal_labels" in sql:
            return FakeCursor(rows=[{"id": 3, "symbol": "SOLUSDT", "side": "LONG"}])
        if "JOIN signal_labels" in sql:
            return FakeCursor(
                rows=[
                    {
                        "id": 2,
                        "symbol": "ETHUSDT",
                        "strategy_type": "PULLBACK",
                        "label": 1,
                        "realized_return_pct": 0.02,
                    }
                ]
            )
        if "FROM signal_observations" in sql:
            return FakeCursor(rows=self.signal_observations)
        if "FROM trades" in sql and "COUNT" in sql:
            open_count = sum(1 for row in self.trades if row.get("status") in {"PAPER_OPEN", "OPEN"})
            closed_count = sum(1 for row in self.trades if row.get("status") not in {"PAPER_OPEN", "OPEN", "PAPER_READY"})
            return FakeCursor(row={"total": len(self.trades), "open_count": open_count, "closed_count": closed_count})
        if "FROM trades" in sql:
            return FakeCursor(rows=[row for row in self.trades if row.get("status") in {"OPEN", "PAPER_OPEN", "LIVE_OPEN"}])
        return FakeCursor(rows=[])


class FakePostgresModule:
    def __init__(self):
        self.connect_calls = []
        self.connection = FakePostgresConnection()

    @contextmanager
    def connect(self, url, **kwargs):
        self.connect_calls.append((url, kwargs))
        yield self.connection


def postgres_mock_db():
    db = Database(BotConfig(database_url="", use_postgres_if_available=False), DummyLogger())
    db.config = BotConfig(database_url="postgres://test", use_postgres_if_available=True)
    db._use_postgres = True
    db._postgres = FakePostgresModule()
    db._postgres_dict_row = object()
    return db


def test_postgres_connect_uses_dict_row_factory():
    db = postgres_mock_db()
    db.fetch_signal_observations()
    url, kwargs = db._postgres.connect_calls[0]
    assert url == "postgres://test"
    assert kwargs["row_factory"] is db._postgres_dict_row


def test_postgres_fetch_functions_return_dict_rows():
    db = postgres_mock_db()
    assert db.fetch_signal_observations()[0]["symbol"] == "BTCUSDT"
    assert db.fetch_labeled_signal_rows()[0]["label"] == 1
    assert db.fetch_unlabeled_signal_observations()[0]["symbol"] == "SOLUSDT"
    assert db.list_open_trades()[0]["status"] == "PAPER_OPEN"
    assert db.get_paper_trade_summary() == {"total": 2, "open": 1, "closed": 1}


def test_tuple_rows_fail_with_clear_postgres_row_factory_message():
    db = postgres_mock_db()
    try:
        db._row_to_dict(("BTCUSDT", "LONG"))
    except TypeError as exc:
        assert "dict_row" in str(exc)
        return
    raise AssertionError("Tuple rows should fail with a clear dict_row message")


def test_research_auto_report_with_postgres_mock_logs_markers():
    db = postgres_mock_db()
    logger = DummyLogger()
    config = BotConfig(enable_research_auto_report=True, research_report_interval_minutes=60)
    last = _emit_research_auto_report_if_due(config, ResearchEngine(db, logger), logger, 0.0, 100.0)
    assert last == 100.0
    joined = "\n".join(logger.messages)
    assert "===== RESEARCH REPORT START =====" in joined
    assert "===== RESEARCH REPORT END =====" in joined
    assert "total senales: 1" in joined


def test_postgres_record_signal_observation_returns_real_id_and_update_uses_it():
    db = postgres_mock_db()
    observation_id = db.record_signal_observation({"symbol": "BTCUSDT", "side": "LONG", "operated": 0})
    db.update_signal_observation(
        observation_id,
        operated=1,
        selected_by_allocator=1,
        risk_manager_approved=1,
        meta_decision="TRADE",
    )

    assert observation_id == 101
    update_sql, update_params = db._postgres.connection.signal_updates[-1]
    assert "WHERE id=%s" in update_sql
    assert update_params[-1] == 101
    assert db._postgres.connection.signal_observations[-1]["operated"] == 1


def test_postgres_record_trade_returns_real_id_and_update_status_targets_trade():
    db = postgres_mock_db()
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
            "position_size": 0.3,
            "leverage_recommendation": 3,
            "confidence_score": 88,
            "reason": "test",
        },
        status="PAPER_OPEN",
    )
    db.update_trade_status(trade_id, "STOP_LOSS", realized_pnl=-1.0)

    assert trade_id == 202
    update_sql, update_params = db._postgres.connection.trade_updates[-1]
    assert "WHERE id=%s" in update_sql
    assert update_params[-1] == 202
    assert db._postgres.connection.trades[-1]["status"] == "STOP_LOSS"


def test_postgres_record_signal_label_returns_real_id():
    db = postgres_mock_db()
    label_id = db.record_signal_label(
        {
            "observation_id": 101,
            "label": 1,
            "first_barrier_hit": "TP1",
            "bars_to_outcome": 3,
            "max_favorable_excursion": 0.02,
            "max_adverse_excursion": -0.005,
            "realized_return_pct": 0.015,
            "simulated_pnl": 0.5,
            "would_have_won": 1,
        }
    )
    assert label_id == 303
    assert db._postgres.connection.signal_labels[-1]["observation_id"] == 101


def test_research_report_coherent_when_postgres_signal_update_marks_operated():
    db = postgres_mock_db()
    observation_id = db.record_signal_observation({"symbol": "BTCUSDT", "side": "LONG", "operated": 0})
    db.update_signal_observation(observation_id, operated=1, selected_by_allocator=1, risk_manager_approved=1)
    db.record_trade(
        mode="paper",
        signal={
            "symbol": "BTCUSDT",
            "strategy_type": "BREAKOUT",
            "side": "LONG",
            "entry_price": 100.0,
            "stop_loss": 98.0,
            "take_profit_1": 103.0,
            "take_profit_2": 105.0,
            "position_size": 0.3,
            "leverage_recommendation": 3,
            "confidence_score": 88,
            "reason": "test",
        },
        status="PAPER_OPEN",
    )

    report = ResearchEngine(db, DummyLogger()).build_report()
    assert "senales operadas: 1" in report
    assert "operaciones paper abiertas: 2" in report
