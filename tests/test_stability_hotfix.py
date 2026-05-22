"""Tests for the Phase 7.3 stability hotfix.

Covers:
- main.py: used_margin must be defined for paper/dry path (no UnboundLocalError).
- database.py: SQLite connection uses timeout, WAL journal mode, and busy_timeout
  so concurrent readers/writers do not raise 'database is locked'.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from app.config import load_config
from app.database import Database


# ---------------------------------------------------------------------------
# Task A — used_margin default-safe in paper/dry
# ---------------------------------------------------------------------------


def test_main_used_margin_initialized_before_balance_state(monkeypatch):
    """Reads main.py source and confirms that used_margin is initialized
    BEFORE the `if config.can_send_real_orders:` block, so paper/dry path
    cannot raise UnboundLocalError when referencing used_margin later.

    This is a structural test on the source, not an end-to-end run of main(),
    because main()'s full execution path requires Bitget API, market data,
    and DB simultaneously — heavy. The structural check is sufficient to
    prevent regression of the exact bug seen in VPS.
    """
    main_source = Path("app/main.py").read_text(encoding="utf-8")

    # Find the slice between the `for selected_signal in selected:` loop and
    # the first reference to `used_margin=` inside `build_effective_balance_for_risk(`.
    for_idx = main_source.find("for selected_signal in selected:")
    assert for_idx != -1, "Expected `for selected_signal in selected:` loop in main.py"
    build_idx = main_source.find("used_margin=used_margin", for_idx)
    assert build_idx != -1, "Expected `used_margin=used_margin` keyword arg in main.py"

    slice_text = main_source[for_idx:build_idx]

    # Must initialize used_margin somewhere before the call.
    initialised = "used_margin = " in slice_text and (
        "used_margin = 0.0" in slice_text or "used_margin = float(" in slice_text
    )
    assert initialised, "used_margin must be initialized before build_effective_balance_for_risk"

    # Confirm the assignment happens OUTSIDE the `if config.can_send_real_orders:` block.
    init_idx = slice_text.find("used_margin = ")
    can_send_idx = slice_text.find("if config.can_send_real_orders:")
    assert can_send_idx != -1
    assert init_idx < can_send_idx, (
        "used_margin must be initialised BEFORE the can_send_real_orders branch, "
        "not inside it (otherwise paper/dry path still raises UnboundLocalError)."
    )


def test_paper_used_margin_uses_paper_trader_reserved_margin_when_available():
    """Reading the relevant slice confirms the default-safe value reads from
    paper_trader.reserved_margin when paper_trader is present."""
    main_source = Path("app/main.py").read_text(encoding="utf-8")
    assert "getattr(paper_trader, \"reserved_margin\", 0.0)" in main_source


# ---------------------------------------------------------------------------
# Task B — SQLite locking hardening
# ---------------------------------------------------------------------------


@pytest.fixture()
def db(tmp_path: Path, monkeypatch) -> Database:
    monkeypatch.setattr("app.database.PROJECT_ROOT", tmp_path)
    cfg = load_config()
    instance = Database(cfg, logging.getLogger("test"))
    instance.sqlite_path = tmp_path / "test.db"
    # Reset class-level WAL flag so we can confirm WAL is applied per-db.
    Database._sqlite_wal_initialised = False
    instance.initialize()
    return instance


def test_sqlite_connect_uses_timeout_and_busy_timeout(db: Database):
    """After first connection, busy_timeout should be set and journal_mode WAL."""
    with db._connect() as conn:
        busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        synchronous = conn.execute("PRAGMA synchronous").fetchone()[0]
    assert int(busy_timeout) >= 30000, f"busy_timeout too low: {busy_timeout}"
    assert str(journal_mode).lower() == "wal", f"expected WAL, got {journal_mode}"
    # synchronous 1 == NORMAL, 2 == FULL. Either is safe; we set NORMAL.
    assert int(synchronous) in (1, 2)


def test_sqlite_wal_persists_across_connections(db: Database):
    # First connection set WAL. Second connection should still see WAL since
    # WAL is a file-level setting persisted in the SQLite header.
    with db._connect() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS smoke (id INTEGER)")
        conn.execute("INSERT INTO smoke VALUES (1)")
    with db._connect() as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert str(mode).lower() == "wal"


def test_concurrent_writer_does_not_raise_database_locked(db: Database):
    """Two threads writing in parallel should succeed without 'database is locked'.

    With the WAL + busy_timeout configuration, concurrent readers AND a single
    writer at a time should work without the cryptic OperationalError.
    """
    errors: list[Exception] = []

    def write_batch(prefix: str, count: int):
        try:
            for i in range(count):
                db.record_event(
                    event_type="stability_test",
                    message=f"{prefix}_{i}",
                    payload={"i": i},
                )
        except Exception as exc:  # pragma: no cover - we WANT this to be empty
            errors.append(exc)

    threads = [
        threading.Thread(target=write_batch, args=("t1", 30)),
        threading.Thread(target=write_batch, args=("t2", 30)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, f"concurrent writes raised: {errors[:3]}"


def test_reader_does_not_block_writer_with_wal(db: Database):
    """Open a read connection, then write from another thread.

    Without WAL, a long read would block writes. With WAL, writes proceed.
    """
    db.record_event(event_type="setup", message="seed", payload={})
    write_errors: list[Exception] = []
    write_done = threading.Event()

    def writer():
        try:
            db.record_event(event_type="while_reading", message="ok", payload={})
            write_done.set()
        except Exception as exc:  # pragma: no cover
            write_errors.append(exc)
            write_done.set()

    # Open a reader and hold it briefly
    with db._connect() as reader_conn:
        rows = reader_conn.execute("SELECT * FROM events").fetchall()
        assert len(rows) >= 1
        t = threading.Thread(target=writer)
        t.start()
        # Give the writer up to 10s — far below busy_timeout (30s)
        write_done.wait(timeout=10.0)
        t.join(timeout=2)
    assert not write_errors, f"writer blocked or failed: {write_errors}"
    assert write_done.is_set(), "writer did not finish while reader was open"


def test_sqlite_connect_timeout_is_applied():
    """The Database class declares a connection timeout >= 30 seconds."""
    assert Database._SQLITE_CONNECT_TIMEOUT_SECONDS >= 30.0
    assert Database._SQLITE_BUSY_TIMEOUT_MS >= 30000


def test_gitignore_covers_sqlite_aux_files():
    """The WAL hotfix introduces *.db-wal/*.db-shm/*.db-journal aux files at
    runtime. They must be in .gitignore so they never reach a commit."""
    gitignore = Path(".gitignore").read_text(encoding="utf-8")
    for needle in ("*.db-wal", "*.db-shm", "*.db-journal"):
        assert needle in gitignore, f"missing gitignore entry: {needle}"
