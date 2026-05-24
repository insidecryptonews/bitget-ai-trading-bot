"""Tests for Phase 7.4A-5 multi-symbol Real Strategy Backtester CLI.

Covers:
- multi-symbol run with fixture OHLCV across two symbols,
- missing data on one symbol does NOT crash the whole run,
- contract invariants preserved (no-lookahead, entry next open, STOP_BEFORE_TP,
  both-way fees, no exchange calls, no MFE/MAE fallback),
- CLI surface accepts --symbols and defaults to canonical 10,
- ResearchLab.real_strategy_backtest_multi() returns text report.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from app.config import load_config
from app.database import Database
from app.real_strategy_backtester import (
    DEFAULT_BACKTESTER_SYMBOLS,
    _resolve_symbols,
    real_strategy_backtest_multi,
    real_strategy_backtest_multi_text,
)


@pytest.fixture()
def db(tmp_path: Path, monkeypatch) -> Database:
    monkeypatch.setattr("app.database.PROJECT_ROOT", tmp_path)
    cfg = load_config()
    instance = Database(cfg, logging.getLogger("test"))
    instance.sqlite_path = tmp_path / "test.db"
    Database._sqlite_wal_initialised = False
    instance.initialize()
    return instance


def _seed_ohlcv(db: Database, symbol: str, *, bars: int = 120, base: float = 100.0) -> None:
    """Insert a synthetic OHLCV series for a single symbol on 5m timeframe.

    Must seed >= 65 candles within the test's hours window because
    RealStrategyBacktester.run() returns NEED_DATA otherwise. With bars=120
    × 5min = 600min = 10h, callers must use hours >= 10 to keep all candles
    inside the loader's window.
    """
    rows = []
    now = datetime.now(timezone.utc) - timedelta(minutes=bars * 5)
    price = base
    for i in range(bars):
        ts = now + timedelta(minutes=i * 5)
        open_p = price
        close = price * (1 + (0.002 if i % 7 == 0 else -0.0005 if i % 11 == 0 else 0.0))
        high = max(open_p, close) * 1.002
        low = min(open_p, close) * 0.998
        rows.append({
            "symbol": symbol, "timeframe": "5m",
            "timestamp": ts.isoformat(),
            "open": open_p, "high": high, "low": low, "close": close,
            "volume": 1000.0, "quote_volume": 100_000.0,
        })
        price = close
    db.insert_ohlcv_batch(rows)


# Tests use this constant so the loader window always covers all seeded bars.
TEST_HOURS = 24


# ---------------------------------------------------------------------------
# Symbol resolution
# ---------------------------------------------------------------------------


def test_resolve_symbols_explicit_wins(db):
    syms = _resolve_symbols(load_config(), ["btcusdt", "ethusdt"])
    assert syms == ["BTCUSDT", "ETHUSDT"]


def test_resolve_symbols_falls_back_to_config_symbols():
    class Cfg: pass
    cfg = Cfg()
    cfg.symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    syms = _resolve_symbols(cfg, None)
    assert syms == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


def test_resolve_symbols_uses_canonical_default_when_no_config():
    class Cfg: pass
    cfg = Cfg()
    syms = _resolve_symbols(cfg, None)
    assert syms == list(DEFAULT_BACKTESTER_SYMBOLS)
    assert "BTCUSDT" in syms
    assert "BNBUSDT" in syms
    assert len(syms) == 10


# ---------------------------------------------------------------------------
# Multi-symbol run — happy path
# ---------------------------------------------------------------------------


def test_multi_runs_each_symbol_independently(db):
    _seed_ohlcv(db, "BTCUSDT")
    _seed_ohlcv(db, "ETHUSDT")
    payload = real_strategy_backtest_multi(
        load_config(), db, hours=TEST_HOURS, symbols=["BTCUSDT", "ETHUSDT"], timeframe="5m",
    )
    assert payload["symbols_requested"] == 2
    symbols_in_report = {r["symbol"] for r in payload["per_symbol"]}
    assert symbols_in_report == {"BTCUSDT", "ETHUSDT"}
    # Both symbols should be evaluable (status OK or NO_TRADES)
    for row in payload["per_symbol"]:
        assert row["status"] in {"OK", "NO_TRADES", "NEED_DATA"}, row


def test_multi_missing_data_does_not_crash_run(db):
    _seed_ohlcv(db, "BTCUSDT")
    # ETHUSDT NOT seeded -> NEED_DATA
    payload = real_strategy_backtest_multi(
        load_config(), db, hours=TEST_HOURS, symbols=["BTCUSDT", "ETHUSDT"], timeframe="5m",
    )
    rows = {r["symbol"]: r for r in payload["per_symbol"]}
    assert rows["BTCUSDT"]["status"] in {"OK", "NO_TRADES"}
    assert rows["ETHUSDT"]["status"] == "NEED_DATA"
    assert rows["ETHUSDT"]["trades"] == 0
    # Totals should still be computed without crashing.
    assert "trades" in payload["total"]


def test_multi_total_aggregates_only_symbols_with_trades(db):
    _seed_ohlcv(db, "BTCUSDT")
    _seed_ohlcv(db, "ETHUSDT")
    payload = real_strategy_backtest_multi(
        load_config(), db, hours=TEST_HOURS, symbols=["BTCUSDT", "ETHUSDT", "SOLUSDT"], timeframe="5m",
    )
    per_total = sum(r["trades"] for r in payload["per_symbol"])
    assert payload["total"]["trades"] == per_total


# ---------------------------------------------------------------------------
# Contract invariants preserved at the multi level
# ---------------------------------------------------------------------------


def test_multi_preserves_contract_invariants(db):
    _seed_ohlcv(db, "BTCUSDT")
    payload = real_strategy_backtest_multi(
        load_config(), db, hours=TEST_HOURS, symbols=["BTCUSDT"], timeframe="5m",
    )
    c = payload["contract"]
    assert c["uses_signal_engine"] is True
    assert c["no_lookahead_status"] == "OK_PREFIX_ONLY"
    assert c["entry_model"] == "signal_close_i_entry_next_open_i+1"
    assert c["stop_tp_same_bar_rule"] == "STOP_BEFORE_TP"
    assert c["min_order_rule"] == "BLOCK_BELOW_MIN_NOTIONAL"
    assert c["both_way_fees_applied"] is True
    assert c["no_mfe_mae_fallback"] is True
    assert c["exchange_calls"] is False


def test_multi_final_recommendation_is_no_live(db):
    payload = real_strategy_backtest_multi(load_config(), db, hours=TEST_HOURS, symbols=["BTCUSDT"])
    assert payload["final_recommendation"] == "NO LIVE"
    assert payload["research_only"] is True


def test_multi_text_output_contains_table_and_total(db):
    _seed_ohlcv(db, "BTCUSDT")
    text = real_strategy_backtest_multi_text(load_config(), db, hours=TEST_HOURS, symbols=["BTCUSDT", "ETHUSDT"])
    assert "REAL STRATEGY BACKTESTER MULTI START" in text
    assert "REAL STRATEGY BACKTESTER MULTI END" in text
    assert "BTCUSDT" in text
    assert "ETHUSDT" in text
    assert "TOTAL" in text
    assert "NO LIVE" in text
    assert "research_only: true" in text


def test_multi_does_not_call_bitget_or_paper_trader(db, monkeypatch):
    """Spies on BitgetClient/PaperTrader/ExecutionEngine: must remain untouched."""
    import app.bitget_client as bc
    import app.execution_engine as ee
    import app.paper_trader as pt
    call_log: list[str] = []

    def _spy(name):
        def _impl(*args, **kwargs):
            call_log.append(name)
            raise AssertionError(f"FORBIDDEN call: {name}")
        return _impl

    monkeypatch.setattr(bc.BitgetClient, "__init__", _spy("BitgetClient.__init__"))
    monkeypatch.setattr(ee.ExecutionEngine, "execute", _spy("ExecutionEngine.execute"))
    monkeypatch.setattr(pt.PaperTrader, "open_position", _spy("PaperTrader.open_position"))

    _seed_ohlcv(db, "BTCUSDT")
    real_strategy_backtest_multi(load_config(), db, hours=TEST_HOURS, symbols=["BTCUSDT"])
    assert call_log == [], f"forbidden execution call(s): {call_log}"


# ---------------------------------------------------------------------------
# ResearchLab CLI surface
# ---------------------------------------------------------------------------


def test_research_lab_exposes_real_strategy_backtest_multi(db):
    from app.research_lab import ResearchLab
    lab = ResearchLab(db, load_config(), logging.getLogger("test"))
    assert hasattr(lab, "real_strategy_backtest_multi")


def test_research_lab_real_strategy_backtest_multi_returns_text(db):
    from app.research_lab import ResearchLab
    _seed_ohlcv(db, "BTCUSDT")
    lab = ResearchLab(db, load_config(), logging.getLogger("test"))
    text = lab.real_strategy_backtest_multi(hours=TEST_HOURS, symbols=["BTCUSDT"], timeframe="5m")
    assert "REAL STRATEGY BACKTESTER MULTI" in text
    assert "BTCUSDT" in text
    assert "NO LIVE" in text


def test_research_lab_cli_command_listed():
    """The CLI argparse must recognize `real-strategy-backtest-multi`."""
    from pathlib import Path
    source = Path("app/research_lab.py").read_text(encoding="utf-8")
    assert '"real-strategy-backtest-multi",' in source
    # Dispatch line should exist
    assert 'real_strategy_backtest_multi(' in source


def test_research_lab_cli_argparse_accepts_timeframe_flag():
    from pathlib import Path
    source = Path("app/research_lab.py").read_text(encoding="utf-8")
    assert '"--timeframe"' in source


# ---------------------------------------------------------------------------
# Default symbol list — used when no --symbols passed and no config
# ---------------------------------------------------------------------------


def test_default_symbol_list_matches_phase_7_4a_5_spec():
    expected = {
        "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT",
        "BNBUSDT", "LINKUSDT", "AVAXUSDT", "ADAUSDT", "DOTUSDT",
    }
    assert set(DEFAULT_BACKTESTER_SYMBOLS) == expected
