"""Auditable ATI-paper causal-incident quarantine and ledger restoration.

This module is intentionally offline. It has no exchange, network or runtime
execution dependency. The productive executor must be stopped before use.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import ACCOUNT_ID, DEFAULT_DB_PATH, DEFAULT_RUNTIME_DIR, DEFAULT_STATUS_PATH, safety_envelope
from .ledger import AtiPaperLedger, canonical_json, utc_now

CONFIRMATION = "ARCHIVE_CAUSAL_INCIDENT_AND_RESTORE_ATI_PAPER_50"
DEFAULT_QA_ROOT = DEFAULT_RUNTIME_DIR.parent / "ati_paper_qa" / "causal_incidents"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="") as handle:
            handle.write(json.dumps(payload, indent=2, sort_keys=True, default=str))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(
                ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
                and code.value == 259
            )
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def _require_executor_stopped(status_path: Path) -> None:
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    pid = int(status.get("pid") or 0) if isinstance(status, dict) else 0
    if _pid_alive(pid):
        raise RuntimeError(f"ATI_PAPER_EXECUTOR_MUST_BE_STOPPED:{pid}")


def _sqlite_backup(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(f"{destination.name}.{os.getpid()}.tmp")
    temp.unlink(missing_ok=True)
    source_conn = sqlite3.connect(source)
    target_conn = sqlite3.connect(temp)
    try:
        integrity = source_conn.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            raise RuntimeError("ATI_PAPER_SOURCE_INTEGRITY_FAILED")
        source_conn.backup(target_conn)
        target_conn.commit()
        target_integrity = target_conn.execute("PRAGMA integrity_check").fetchone()
        if not target_integrity or target_integrity[0] != "ok":
            raise RuntimeError("ATI_PAPER_QA_BACKUP_INTEGRITY_FAILED")
    finally:
        target_conn.close()
        source_conn.close()
    os.replace(temp, destination)


def _row(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
    value = conn.execute(sql, params).fetchone()
    return dict(value) if value else None


def _recompute_account(conn: sqlite3.Connection) -> None:
    account = conn.execute("SELECT * FROM account WHERE account_id=?", (ACCOUNT_ID,)).fetchone()
    if account is None:
        raise RuntimeError("ATI_PAPER_ACCOUNT_MISSING")
    trades = conn.execute("SELECT * FROM trades").fetchall()
    open_positions = conn.execute("SELECT * FROM positions WHERE status='OPEN'").fetchall()
    initial = float(account["initial_balance"])
    open_entry_cost = sum(float(row["entry_fee"]) + float(row["entry_slippage"]) for row in open_positions)
    realized_pnl = sum(float(row["net_pnl"]) for row in trades)
    realized = initial + realized_pnl - open_entry_cost
    cash = realized - sum(float(row["reserved_notional"]) for row in open_positions)
    unrealized = sum(float(row["unrealized_pnl"]) for row in open_positions)
    total = realized + unrealized
    curve = conn.execute("SELECT total_equity,drawdown_pct FROM equity_curve").fetchall()
    peak = max([initial, total] + [float(row["total_equity"]) for row in curve])
    drawdown_abs = max(0.0, peak - total)
    drawdown_pct = drawdown_abs / peak if peak > 0 else 0.0
    max_drawdown = max([drawdown_pct, 0.0] + [float(row["drawdown_pct"]) for row in curve])
    fees = sum(float(row["fees"]) for row in trades) + sum(float(row["entry_fee"]) for row in open_positions)
    slippage = sum(float(row["slippage"]) for row in trades) + sum(float(row["entry_slippage"]) for row in open_positions)
    funding = sum(float(row["funding"]) for row in trades)
    values = (
        cash, realized, unrealized, total, peak, drawdown_abs, drawdown_pct,
        max_drawdown, realized_pnl, fees, slippage, funding, utc_now(), ACCOUNT_ID,
    )
    if not all(math.isfinite(float(value)) for value in values[:-2]):
        raise RuntimeError("ATI_PAPER_RESTORED_ACCOUNT_NON_FINITE")
    conn.execute(
        """UPDATE account SET cash_balance=?,realized_equity=?,unrealized_pnl=?,
           total_equity=?,equity_peak=?,drawdown_abs=?,drawdown_pct=?,
           max_drawdown_pct=?,realized_pnl_total=?,fees_total=?,slippage_total=?,
           funding_total=?,updated_at=? WHERE account_id=?""",
        values,
    )


def archive_and_restore_causal_incident(
    signal_id: str, *, confirmation: str, db_path: Path | str = DEFAULT_DB_PATH,
    qa_root: Path | str = DEFAULT_QA_ROOT,
    status_path: Path | str = DEFAULT_STATUS_PATH,
    evidence_paths: list[Path | str] | None = None,
    commit_hash: str = "unknown",
) -> dict[str, Any]:
    """Move one proven causal contamination to an immutable QA snapshot.

    The original database is copied and hashed before a single productive row
    is changed. The cleanup then occurs in one SQLite transaction and retains a
    rejected signal plus an explicit incident event in the productive ledger.
    """
    if confirmation != CONFIRMATION:
        raise ValueError("ATI_PAPER_INCIDENT_CONFIRMATION_REQUIRED")
    signal_id = str(signal_id or "").strip()
    if not signal_id:
        raise ValueError("ATI_PAPER_INCIDENT_SIGNAL_ID_REQUIRED")
    source = Path(db_path)
    if not source.is_file() or source.is_symlink():
        raise ValueError("ATI_PAPER_INCIDENT_SOURCE_DB_INVALID")
    _require_executor_stopped(Path(status_path))
    ledger = AtiPaperLedger(source)
    conn = ledger._connect(read_only=True)
    try:
        signal = _row(conn, "SELECT * FROM signals WHERE signal_id=?", (signal_id,))
        position = _row(conn, "SELECT * FROM positions WHERE signal_id=?", (signal_id,))
        trade = _row(conn, "SELECT * FROM trades WHERE signal_id=?", (signal_id,))
        account_before = _row(conn, "SELECT * FROM account WHERE account_id=?", (ACCOUNT_ID,))
        if signal is None or (position is None and trade is None):
            raise ValueError("ATI_PAPER_CAUSAL_INCIDENT_NOT_PRESENT")
        related_events = [dict(row) for row in conn.execute(
            "SELECT * FROM events WHERE signal_id=? OR correlation_id=? ORDER BY timestamp",
            (signal_id, signal_id),
        ).fetchall()]
        related_orders = [dict(row) for row in conn.execute(
            "SELECT * FROM simulated_orders WHERE signal_id=? ORDER BY created_at", (signal_id,),
        ).fetchall()]
        baseline = conn.execute(
            "SELECT MAX(id) FROM equity_curve WHERE timestamp < ?", (signal["observed_at"],),
        ).fetchone()[0]
        baseline_equity_id = int(baseline or 0)
    finally:
        conn.close()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    incident_id = f"{stamp}_{signal_id}"
    root = Path(qa_root)
    incident_dir = root / incident_id
    if root.exists() and root.is_symlink():
        raise ValueError("ATI_PAPER_QA_ROOT_SYMLINK_BLOCKED")
    if incident_dir.exists():
        raise FileExistsError(incident_dir)
    incident_dir.mkdir(parents=True)
    archive_db = incident_dir / "ati_paper_contaminated.sqlite"
    _sqlite_backup(source, archive_db)
    evidence = []
    for raw_path in evidence_paths or []:
        path = Path(raw_path)
        if path.is_file() and not path.is_symlink():
            evidence.append({"path": str(path), "sha256": _sha256(path), "bytes": path.stat().st_size})
    manifest: dict[str, Any] = {
        "schema": "ati_paper_causal_incident.v1",
        "incident_id": incident_id,
        "status": "ARCHIVED_BEFORE_RESTORE",
        "classification": "QA_CAUSAL_CONTAMINATION_ARCHIVE",
        "reason": "PREKNOWN_OUTCOME_ENTERED_AFTER_HISTORICAL_REPLAY_PUBLICATION",
        "signal_id": signal_id,
        "position_id": (position or {}).get("position_id"),
        "trade_id": (trade or {}).get("trade_id"),
        "source_db": str(source),
        "source_db_sha256_at_archive": _sha256(source),
        "qa_archive_db": str(archive_db),
        "qa_archive_sha256": _sha256(archive_db),
        "signal": signal,
        "position": position,
        "trade": trade,
        "orders": related_orders,
        "events": related_events,
        "account_before": account_before,
        "baseline_equity_id": baseline_equity_id,
        "evidence_files": evidence,
        "created_at": utc_now(),
        "commit_hash": commit_hash,
        **safety_envelope(),
    }
    manifest_path = incident_dir / "incident_manifest.json"
    _atomic_json(manifest_path, manifest)

    position_id = str((position or {}).get("position_id") or "")
    with ledger.transaction() as write:
        write.execute("DELETE FROM trades WHERE signal_id=?", (signal_id,))
        write.execute("DELETE FROM simulated_orders WHERE signal_id=?", (signal_id,))
        write.execute("DELETE FROM positions WHERE signal_id=?", (signal_id,))
        write.execute(
            "DELETE FROM events WHERE signal_id=? OR correlation_id=? OR position_id=?",
            (signal_id, signal_id, position_id),
        )
        write.execute("DELETE FROM equity_curve WHERE id>?", (baseline_equity_id,))
        write.execute(
            """UPDATE signals SET status='ATI_SIGNAL_REJECTED', rejection_reason=?,
               accepted_at=NULL, updated_at=? WHERE signal_id=?""",
            ("MIGRATED_PREKNOWN_OUTCOME_CAUSAL_CONTAMINATION", utc_now(), signal_id),
        )
        _recompute_account(write)
        ledger._event(
            write,
            event_type="ATI_PAPER_CAUSAL_INCIDENT_MIGRATED_TO_QA",
            correlation_id=incident_id,
            signal_id=signal_id,
            previous_state="CONTAMINATED_FORWARD_PAPER",
            new_state="REJECTED_QA_ARCHIVED",
            reason="PREKNOWN_OUTCOME_NOT_FORWARD_ELIGIBLE",
            source_ts=str(signal.get("decision_ts") or ""),
            commit_hash=commit_hash,
            payload={
                "incident_id": incident_id,
                "qa_archive_sha256": manifest["qa_archive_sha256"],
                "trade_id": (trade or {}).get("trade_id"),
                "position_id": (position or {}).get("position_id"),
                "restoration_transactional": True,
            },
        )
        ledger._append_equity(write, reason="CAUSAL_INCIDENT_RESTORED_FROM_QA_ARCHIVE")

    reconciliation = ledger.reconcile()
    if reconciliation.get("status") != "PASS":
        raise RuntimeError(f"ATI_PAPER_POST_MIGRATION_RECONCILIATION_FAILED:{reconciliation}")
    manifest.update({
        "status": "QA_ARCHIVED_AND_PRODUCTIVE_LEDGER_RESTORED",
        "completed_at": utc_now(),
        "account_after": ledger.account(),
        "reconciliation_after": reconciliation,
        "productive_db_sha256_after": _sha256(source),
    })
    _atomic_json(manifest_path, manifest)
    return {
        "status": manifest["status"],
        "incident_id": incident_id,
        "manifest_path": str(manifest_path),
        "qa_archive_db": str(archive_db),
        "qa_archive_sha256": manifest["qa_archive_sha256"],
        "reconciliation": reconciliation,
        "account": ledger.account(),
        **safety_envelope(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Archive one ATI causal incident to QA and restore the paper ledger")
    parser.add_argument("--signal-id", required=True)
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args(argv)
    result = archive_and_restore_causal_incident(
        args.signal_id,
        confirmation=args.confirm,
        evidence_paths=[
            DEFAULT_RUNTIME_DIR.parent.parent.parent / "reports" / "research" / "ati" / "ati_forward_signals.jsonl",
            DEFAULT_RUNTIME_DIR.parent.parent.parent / "reports" / "research" / "ati" / "ati_forward_outcomes.jsonl",
        ],
    )
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
