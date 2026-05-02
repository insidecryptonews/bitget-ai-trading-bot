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

    def execute(self, sql, params=()):
        self.sql_seen.append((sql, params))
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
            return FakeCursor(rows=[{"id": 1, "symbol": "BTCUSDT", "side": "LONG", "operated": 0}])
        if "FROM trades" in sql and "COUNT" in sql:
            return FakeCursor(row={"total": 2, "open_count": 1, "closed_count": 1})
        if "FROM trades" in sql:
            return FakeCursor(rows=[{"id": 9, "symbol": "BTCUSDT", "status": "PAPER_OPEN"}])
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
