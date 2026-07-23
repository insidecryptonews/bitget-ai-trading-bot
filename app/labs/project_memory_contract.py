"""Persistent, fail-closed memory contract for local research processes.

The contract records immutable research boundaries and account identity without
changing ATI, P11, Cross-Venue, any trading policy, or any exchange path. Its
runtime state lives under ``data/runtime`` and is deliberately outside Git.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .cross_venue import REPO_ROOT


CONTRACT_PATH = REPO_ROOT / "config" / "project" / "BITGET_RESEARCH_PROJECT_CONTRACT.json"
RUNTIME_ROOT = REPO_ROOT / "data" / "runtime" / "project_memory"
STATE_PATH = RUNTIME_ROOT / "contract_state.json"
DECISION_LEDGER_PATH = RUNTIME_ROOT / "research_decision_ledger.jsonl"

DEFAULT_SOURCES = {
    "ati_boundary": REPO_ROOT / "reports" / "research" / "ati" / "ati_forward_boundary.json",
    "ati_state": REPO_ROOT / "reports" / "research" / "ati" / "ati_forward_state.json",
    "ati_account": REPO_ROOT / "data" / "runtime" / "ati_paper" / "ati_paper.sqlite",
    "ati_executor": REPO_ROOT / "data" / "runtime" / "ati_paper" / "executor_status.json",
    "p11_status": REPO_ROOT / "reports" / "research" / "p11_short_forward_observer" / "observer_status.json",
    "p11_reconciliation": REPO_ROOT / "reports" / "research" / "p11_short_forward_observer" / "reconciliation_report.json",
    "cross_boundary": REPO_ROOT / "data" / "runtime" / "cross_venue" / "forward_boundary.json",
    "cross_offsets": REPO_ROOT / "data" / "runtime" / "cross_venue" / "stream_offsets.json",
    "cross_account": REPO_ROOT / "data" / "runtime" / "cross_venue" / "cross_venue_paper.sqlite",
    "cross_status": REPO_ROOT / "data" / "runtime" / "cross_venue" / "dashboard_snapshot.json",
    "storage_manifest": REPO_ROOT / "data" / "runtime" / "storage_efficiency_v2" / "storage_manifest.json",
    "feature_manifest": REPO_ROOT / "data" / "runtime" / "storage_efficiency_v2" / "feature_manifest.json",
    "challenger_status": REPO_ROOT / "data" / "runtime" / "storage_efficiency_v2" / "challenger_status.json",
}

PROTECTED_POLICY_PATHS = (
    "config/ati/ATI_SHADOW_POLICY_V2.json",
    "config/ati/ATI_PAPER_SIMULATION_V1.json",
    "config/cross_venue/CROSS_VENUE_RESEARCH_V1.json",
    "app/labs/ati_paper/broker.py",
    "app/labs/ati_paper/executor.py",
    "app/labs/cross_venue/paper.py",
    "app/labs/cross_venue/service.py",
    "app/labs/p11_short_forward_observer.py",
    "app/execution_engine.py",
    "app/paper_trader.py",
)

SECRET_KEY_TOKENS = ("secret", "password", "passphrase", "api_key", "apikey", "token", "credential")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return default


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("PROJECT_MEMORY_SYMLINK_BLOCKED")
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _git(*args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True,
            timeout=5, check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "UNKNOWN"


def _env_metadata(path: Path | None = None) -> dict[str, Any]:
    """Fingerprint .env without reading or exposing any value."""
    target = path or (REPO_ROOT / ".env")
    try:
        stat = target.stat()
    except OSError:
        return {"exists": False, "size": 0, "mtime_ns": None, "content_read": False}
    return {
        "exists": True, "size": int(stat.st_size), "mtime_ns": int(stat.st_mtime_ns),
        "content_read": False,
    }


def _policy_hashes(paths: Iterable[str] = PROTECTED_POLICY_PATHS) -> dict[str, str]:
    result: dict[str, str] = {}
    for relative in paths:
        path = REPO_ROOT / relative
        result[relative] = _sha_file(path) if path.is_file() and not path.is_symlink() else "MISSING"
    return result


def _safe_config_snapshot() -> dict[str, Any]:
    try:
        from app.config import load_config

        config = load_config()
        return {
            "status": "OK",
            "PAPER_TRADING": bool(config.paper_trading),
            "LIVE_TRADING": bool(config.live_trading),
            "DRY_RUN": bool(config.dry_run),
            "ENABLE_PAPER_POLICY_FILTER": bool(config.enable_paper_policy_filter),
            "ENABLE_CANDIDATE_SHADOW_MONITOR": bool(config.enable_candidate_shadow_monitor),
            "can_send_real_orders": bool(config.can_send_real_orders),
        }
    except Exception as exc:
        return {
            "status": "ERROR", "error": f"{type(exc).__name__}:{str(exc)[:160]}",
            "PAPER_TRADING": None, "LIVE_TRADING": None, "DRY_RUN": None,
            "ENABLE_PAPER_POLICY_FILTER": None,
            "ENABLE_CANDIDATE_SHADOW_MONITOR": None,
            "can_send_real_orders": None,
        }


def _read_account_identity(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        return {"status": "MISSING", "path": path.name}
    try:
        uri = f"file:{path.resolve().as_posix()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=3)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                "SELECT account_id, initial_balance, created_at FROM account LIMIT 2"
            ).fetchall()
        finally:
            connection.close()
        if len(row) != 1:
            return {"status": "INVALID", "path": path.name, "account_rows": len(row)}
        value = dict(row[0])
        balance = float(value.get("initial_balance"))
        if not math.isfinite(balance) or balance <= 0:
            return {"status": "INVALID", "path": path.name, "reason": "INITIAL_BALANCE_INVALID"}
        return {
            "status": "OK", "path": path.name,
            "account_id": str(value.get("account_id") or ""),
            "initial_balance": balance,
            "created_at": str(value.get("created_at") or ""),
            "read_only": True,
        }
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        return {"status": "ERROR", "path": path.name, "error": f"{type(exc).__name__}:{str(exc)[:120]}"}


def _iso_ms(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            number = float(value)
            return int(number) if math.isfinite(number) else None
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp() * 1000)
    except (TypeError, ValueError, OverflowError):
        return None


def _boundary_snapshot(sources: dict[str, Path]) -> dict[str, Any]:
    ati = _read_json(sources["ati_boundary"], {}) or {}
    p11 = _read_json(sources["p11_status"], {}) or {}
    cross = _read_json(sources["cross_boundary"], {}) or {}
    offsets = _read_json(sources["cross_offsets"], {}) or {}
    p11_boundary = p11.get("boundary") if isinstance(p11.get("boundary"), dict) else {}
    initial_offsets = cross.get("initial_offsets") if isinstance(cross.get("initial_offsets"), dict) else {}
    current_offsets = {
        key: int(offsets[key]) for key in ("bitget", "binance", "bybit", "okx", "hyperliquid")
        if isinstance(offsets.get(key), int) and not isinstance(offsets.get(key), bool)
    }
    return {
        "ati_forward_ms": _iso_ms(ati.get("forward_boundary")),
        "ati_forward": ati.get("forward_boundary"),
        "p11_forward_ms": _iso_ms(p11_boundary.get("forward_start_ms")),
        "p11_forward": p11_boundary.get("forward_start_timestamp"),
        "cross_initial_offsets": {
            key: int(value) for key, value in sorted(initial_offsets.items())
            if isinstance(value, int) and not isinstance(value, bool)
        },
        "cross_current_offsets": current_offsets,
    }


def _manifest_summary(path: Path) -> dict[str, Any]:
    value = _read_json(path, {}) or {}
    if not isinstance(value, dict):
        return {"status": "INVALID"}
    segments = value.get("segments") if isinstance(value.get("segments"), dict) else {}
    partitions = value.get("partitions") if isinstance(value.get("partitions"), dict) else {}
    return {
        "status": "OK" if path.is_file() else "MISSING",
        "sha256": _sha_file(path) if path.is_file() and not path.is_symlink() else None,
        "segments": len(segments), "partitions": len(partitions),
        "updated_at": value.get("updated_at"),
    }


def _evidence_snapshot(sources: dict[str, Path]) -> dict[str, Any]:
    ati = _read_json(sources["ati_state"], {}) or {}
    p11 = _read_json(sources["p11_status"], {}) or {}
    cross = _read_json(sources["cross_status"], {}) or {}
    challenger = _read_json(sources["challenger_status"], {}) or {}
    p11_metrics = p11.get("metrics") if isinstance(p11.get("metrics"), dict) else {}
    p11_safety = p11.get("safety") if isinstance(p11.get("safety"), dict) else {}
    cross_reconciliation = cross.get("reconciliation") if isinstance(cross.get("reconciliation"), dict) else {}
    ati_reconciliation = ati.get("reconciliation") if isinstance(ati.get("reconciliation"), dict) else {}
    return {
        "dataset_hashes": {
            "ati": ati.get("dataset_snapshot_sha256"),
            "challenger": challenger.get("dataset_hash"),
            "storage_manifest": _manifest_summary(sources["storage_manifest"]),
            "feature_manifest": _manifest_summary(sources["feature_manifest"]),
        },
        "holdout": {
            "challenger_status": challenger.get("holdout_status", "UNKNOWN"),
            "challenger_access_count": int(challenger.get("holdout_access_count") or 0),
            "p11_holdout_opened": bool(p11_safety.get("holdout_opened", False)),
        },
        "reconciliation": {
            "ati": ati_reconciliation.get("status") or ati.get("reconciliation_status") or "UNKNOWN",
            "p11": p11_metrics.get("reconciliation_status") or "UNKNOWN",
            "cross_venue": cross_reconciliation.get("status") or "UNKNOWN",
        },
        "latest_results": {
            "ati_signals": int(ati.get("signals_total") or 0),
            "ati_outcomes": int(ati.get("closed_outcomes") or 0),
            "p11_signals": int(p11_metrics.get("forward_signals") or 0),
            "p11_outcomes": int(p11_metrics.get("forward_closed_outcomes") or 0),
            "challenger_state": challenger.get("state") or "NEED_MORE_DATA",
            "strategy_verdict": challenger.get("strategy_verdict") or "NO DEFENSIBLE EDGE FOUND",
        },
        "next_action": "KEEP_COLLECTING_AND_REEVALUATE_NEW_VERIFIED_PARTITIONS",
    }


def _contract(path: Path = CONTRACT_PATH) -> dict[str, Any]:
    value = _read_json(path, {}) or {}
    if not isinstance(value, dict) or value.get("schema") != "bitget_research_project_contract.v1":
        raise ValueError("PROJECT_MEMORY_CONTRACT_INVALID")
    return value


def _safety_violations(snapshot: dict[str, Any]) -> list[str]:
    expected = {
        "PAPER_TRADING": True,
        "LIVE_TRADING": False,
        "DRY_RUN": True,
        "ENABLE_PAPER_POLICY_FILTER": False,
        "ENABLE_CANDIDATE_SHADOW_MONITOR": False,
        "can_send_real_orders": False,
    }
    if snapshot.get("status") != "OK":
        return ["SAFETY_CONFIG_UNREADABLE"]
    return [f"SAFETY_FLAG_MISMATCH:{key}" for key, value in expected.items() if snapshot.get(key) is not value]


def _boundary_violations(previous: dict[str, Any], current: dict[str, Any]) -> list[str]:
    violations: list[str] = []
    for key in ("ati_forward_ms", "p11_forward_ms"):
        old, new = previous.get(key), current.get(key)
        if old is None or new is None:
            violations.append(f"BOUNDARY_UNAVAILABLE:{key}")
        elif int(new) != int(old):
            violations.append(f"BOUNDARY_CHANGED:{key}")
    old_initial = previous.get("cross_initial_offsets") if isinstance(previous.get("cross_initial_offsets"), dict) else {}
    new_initial = current.get("cross_initial_offsets") if isinstance(current.get("cross_initial_offsets"), dict) else {}
    current_offsets = current.get("cross_current_offsets") if isinstance(current.get("cross_current_offsets"), dict) else {}
    for key, old in old_initial.items():
        if key not in new_initial:
            violations.append(f"BOUNDARY_UNAVAILABLE:cross_initial_offsets:{key}")
        elif int(new_initial[key]) != int(old):
            violations.append(f"BOUNDARY_CHANGED:cross_initial_offsets:{key}")
        if key not in current_offsets:
            violations.append(f"BOUNDARY_UNAVAILABLE:cross_current_offsets:{key}")
        else:
            try:
                if int(current_offsets[key]) < 0:
                    violations.append(f"BOUNDARY_INVALID:cross_current_offsets:{key}")
            except (TypeError, ValueError):
                violations.append(f"BOUNDARY_INVALID:cross_current_offsets:{key}")
    return violations


def _account_violations(previous: dict[str, Any], current: dict[str, Any]) -> list[str]:
    violations: list[str] = []
    for key in ("ati", "cross_venue"):
        old = previous.get(key) if isinstance(previous.get(key), dict) else {}
        new = current.get(key) if isinstance(current.get(key), dict) else {}
        if old.get("status") != "OK" or new.get("status") != "OK":
            violations.append(f"ACCOUNT_IDENTITY_UNAVAILABLE:{key}")
            continue
        for field in ("account_id", "initial_balance", "created_at"):
            if new.get(field) != old.get(field):
                violations.append(f"ACCOUNT_RESET_DETECTED:{key}:{field}")
    return violations


def verify_decision_ledger(path: Path = DECISION_LEDGER_PATH) -> dict[str, Any]:
    if not path.exists():
        return {"status": "EMPTY", "records": 0, "chain_head": None}
    previous = "GENESIS"
    records = 0
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                row = json.loads(line)
                claimed = str(row.pop("record_hash", ""))
                if row.get("previous_hash") != previous or _sha_bytes(_canonical(row)) != claimed:
                    return {"status": "INVALID", "records": records, "chain_head": previous}
                previous = claimed
                records += 1
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return {"status": "INVALID", "records": records, "chain_head": previous}
    return {"status": "PASS", "records": records, "chain_head": previous}


def _sanitize_decision(value: Any, key: str = "") -> Any:
    lowered = key.lower()
    if any(token in lowered for token in SECRET_KEY_TOKENS):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): _sanitize_decision(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_decision(item, key) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def append_research_decision(
    decision: dict[str, Any], *, path: Path = DECISION_LEDGER_PATH,
) -> dict[str, Any]:
    chain = verify_decision_ledger(path)
    if chain["status"] == "INVALID":
        raise ValueError("PROJECT_MEMORY_DECISION_LEDGER_INVALID")
    clean = _sanitize_decision(decision)
    row = {
        "schema": "research_decision_ledger.v1",
        "timestamp": utc_now(),
        "dataset_hash": clean.get("dataset_hash"),
        "commit": clean.get("commit") or _git("rev-parse", "HEAD"),
        "hypothesis": clean.get("hypothesis") or "PROJECT_MEMORY",
        "proposed_change": clean.get("proposed_change"),
        "rejected_change": clean.get("rejected_change"),
        "reason": clean.get("reason"),
        "tests": clean.get("tests") or [],
        "result": clean.get("result"),
        "final_state": clean.get("final_state") or "RESEARCH_ONLY",
        "human_approval_required": True,
        "previous_hash": chain.get("chain_head") or "GENESIS",
    }
    row["record_hash"] = _sha_bytes(_canonical(row))
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("PROJECT_MEMORY_SYMLINK_BLOCKED")
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(row, sort_keys=True, ensure_ascii=True, allow_nan=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return {"status": "APPENDED", "record_hash": row["record_hash"], "records": int(chain["records"]) + 1}


def run_contract_audit(
    *, apply: bool = False, contract_path: Path = CONTRACT_PATH,
    state_path: Path = STATE_PATH, decision_path: Path = DECISION_LEDGER_PATH,
    sources: dict[str, Path] | None = None, env_path: Path | None = None,
    policy_paths: Iterable[str] = PROTECTED_POLICY_PATHS,
) -> dict[str, Any]:
    contract = _contract(contract_path)
    source_paths = {**DEFAULT_SOURCES, **(sources or {})}
    flags = _safe_config_snapshot()
    branch = _git("branch", "--show-current")
    head = _git("rev-parse", "HEAD")
    tree = _git("rev-parse", "HEAD^{tree}")
    env = _env_metadata(env_path)
    policies = _policy_hashes(policy_paths)
    accounts = {
        "ati": _read_account_identity(source_paths["ati_account"]),
        "cross_venue": _read_account_identity(source_paths["cross_account"]),
    }
    boundaries = _boundary_snapshot(source_paths)
    evidence = _evidence_snapshot(source_paths)
    ledger = verify_decision_ledger(decision_path)
    contract_hash = _sha_bytes(_canonical(contract))
    previous = _read_json(state_path, {}) or {}
    activated = isinstance(previous, dict) and previous.get("baseline") is not None
    violations = _safety_violations(flags)
    if branch != contract.get("allowed_branch"):
        violations.append("BRANCH_NOT_ALLOWED")
    if any(value == "MISSING" for value in policies.values()):
        violations.append("PROTECTED_POLICY_MISSING")
    if any(value.get("status") != "OK" for value in accounts.values()):
        violations.append("ACCOUNT_IDENTITY_UNAVAILABLE")
    required_boundaries = (boundaries.get("ati_forward_ms"), boundaries.get("p11_forward_ms"))
    if any(value is None for value in required_boundaries) or not boundaries.get("cross_initial_offsets"):
        violations.append("BOUNDARY_SOURCE_UNAVAILABLE")
    if ledger.get("status") == "INVALID":
        violations.append("DECISION_LEDGER_CHAIN_INVALID")
    if evidence["holdout"]["challenger_access_count"] > 1:
        violations.append("HOLDOUT_ACCESS_LIMIT_EXCEEDED")
    if evidence["holdout"]["p11_holdout_opened"]:
        violations.append("P11_HOLDOUT_UNEXPECTEDLY_OPEN")
    challenger = _read_json(source_paths["challenger_status"], {}) or {}
    if challenger.get("auto_promotion") is not False and challenger:
        violations.append("AUTO_PROMOTION_CONTRACT_BREACH")

    baseline = previous.get("baseline") if activated else None
    if activated:
        if previous.get("contract_hash") != contract_hash:
            violations.append("CONTRACT_HASH_CHANGED_REQUIRES_MANUAL_MIGRATION")
        if baseline.get("env_metadata") != env:
            violations.append("ENV_METADATA_CHANGED")
        if baseline.get("protected_policy_hashes") != policies:
            violations.append("PROTECTED_ORDER_OR_POLICY_PATH_CHANGED")
        violations.extend(_account_violations(baseline.get("accounts") or {}, accounts))
        violations.extend(_boundary_violations(previous.get("last_seen_boundaries") or {}, boundaries))
    elif not apply:
        violations.append("CONTRACT_BASELINE_NOT_FROZEN")

    violations = sorted(set(violations))
    can_activate = not violations
    first_activation = bool(apply and not activated and can_activate)
    if first_activation:
        baseline = {
            "frozen_at": utc_now(), "env_metadata": env,
            "protected_policy_hashes": policies, "accounts": accounts,
            "initial_boundaries": boundaries,
        }
        activated = True
    guardrails_status = "PASS" if activated and not violations else "FAIL"
    state = {
        "schema": "bitget_research_project_memory_state.v1",
        "updated_at": utc_now(), "contract_hash": contract_hash,
        "contract_version": contract.get("contract_version"),
        "allowed_branch": contract.get("allowed_branch"), "actual_branch": branch,
        "head": head, "tree": tree, "baseline": baseline,
        "last_seen_boundaries": boundaries,
        "accounts": accounts, "flags": flags, "env_metadata": env,
        "protected_policy_hashes": policies, "evidence": evidence,
        "decision_ledger": ledger, "guardrails_status": guardrails_status,
        "violations": violations, "can_continue_research": guardrails_status == "PASS",
        "research_only": True, "simulation_only": True,
        "paper_filter_enabled": False, "can_send_real_orders": False,
        "auto_promotion": False, "active_policy_modified": False,
        "final_recommendation": "NO LIVE",
    }
    if apply and activated:
        _atomic_json(state_path, state)
        if first_activation:
            append_research_decision({
                "dataset_hash": evidence["dataset_hashes"].get("challenger"),
                "commit": head, "hypothesis": "PROJECT_MEMORY_CONTRACT",
                "proposed_change": "FREEZE_RESEARCH_SAFETY_BASELINE",
                "reason": "PERSIST_BOUNDARIES_ACCOUNTS_POLICIES_AND_SAFETY_FLAGS",
                "tests": ["CONTRACT_GUARDRAILS_PASS"],
                "result": "BASELINE_FROZEN", "final_state": "RESEARCH_ONLY",
            }, path=decision_path)
            state["decision_ledger"] = verify_decision_ledger(decision_path)
            _atomic_json(state_path, state)
    return state


def migrate_protected_policy_baseline(
    *, relative_path: str, expected_old_sha256: str,
    expected_new_sha256: str, reason: str, apply: bool = False,
    contract_path: Path = CONTRACT_PATH, state_path: Path = STATE_PATH,
    decision_path: Path = DECISION_LEDGER_PATH,
    sources: dict[str, Path] | None = None, env_path: Path | None = None,
    policy_paths: Iterable[str] = PROTECTED_POLICY_PATHS,
) -> dict[str, Any]:
    """Migrate one reviewed protected file by exact old/new content hashes.

    This is deliberately narrower than resetting the project-memory baseline. It
    refuses any concurrent safety, account, boundary, environment, contract, or
    second-policy change and records the approved hash transition in the
    append-only decision ledger before changing the baseline.
    """
    allowed_paths = tuple(str(item).replace("\\", "/") for item in policy_paths)
    target = str(relative_path or "").replace("\\", "/").strip()
    old_expected = str(expected_old_sha256 or "").strip().lower()
    new_expected = str(expected_new_sha256 or "").strip().lower()
    clean_reason = " ".join(str(reason or "").split())

    def valid_sha(value: str) -> bool:
        return len(value) == 64 and all(char in "0123456789abcdef" for char in value)

    blockers: list[str] = []
    if target not in allowed_paths:
        blockers.append("MIGRATION_TARGET_NOT_PROTECTED")
    if not valid_sha(old_expected):
        blockers.append("EXPECTED_OLD_SHA256_INVALID")
    if not valid_sha(new_expected):
        blockers.append("EXPECTED_NEW_SHA256_INVALID")
    if old_expected == new_expected and valid_sha(old_expected):
        blockers.append("POLICY_HASH_DID_NOT_CHANGE")
    if len(clean_reason) < 16:
        blockers.append("HUMAN_REVIEW_REASON_TOO_SHORT")

    previous = _read_json(state_path, {}) or {}
    baseline = previous.get("baseline") if isinstance(previous, dict) else None
    if not isinstance(baseline, dict):
        blockers.append("CONTRACT_BASELINE_NOT_FROZEN")

    current = run_contract_audit(
        apply=False, contract_path=contract_path, state_path=state_path,
        decision_path=decision_path, sources=sources, env_path=env_path,
        policy_paths=allowed_paths,
    )
    old_hashes = baseline.get("protected_policy_hashes") if isinstance(baseline, dict) else {}
    old_hashes = old_hashes if isinstance(old_hashes, dict) else {}
    new_hashes = current.get("protected_policy_hashes")
    new_hashes = new_hashes if isinstance(new_hashes, dict) else {}
    changed_paths = sorted(
        key for key in set(old_hashes) | set(new_hashes)
        if old_hashes.get(key) != new_hashes.get(key)
    )
    if changed_paths != [target]:
        blockers.append("MIGRATION_REQUIRES_EXACTLY_ONE_MATCHING_POLICY_CHANGE")
    if old_hashes.get(target) != old_expected:
        blockers.append("BASELINE_SHA256_MISMATCH")
    if new_hashes.get(target) != new_expected:
        blockers.append("CURRENT_SHA256_MISMATCH")

    allowed_violation = "PROTECTED_ORDER_OR_POLICY_PATH_CHANGED"
    unrelated = [
        item for item in current.get("violations") or []
        if item != allowed_violation
    ]
    if unrelated:
        blockers.extend(f"UNRELATED_CONTRACT_VIOLATION:{item}" for item in unrelated)
    if allowed_violation not in (current.get("violations") or []):
        blockers.append("EXPECTED_POLICY_CHANGE_VIOLATION_NOT_PRESENT")
    blockers = sorted(set(blockers))

    payload: dict[str, Any] = {
        "schema": "project_memory_policy_migration.v1",
        "status": "BLOCKED" if blockers else "READY_FOR_EXPLICIT_APPLY",
        "apply_requested": bool(apply),
        "target": target,
        "expected_old_sha256": old_expected,
        "expected_new_sha256": new_expected,
        "changed_paths": changed_paths,
        "reason": clean_reason,
        "blockers": blockers,
        "research_only": True,
        "simulation_only": True,
        "paper_filter_enabled": False,
        "can_send_real_orders": False,
        "auto_promotion": False,
        "final_recommendation": "NO LIVE",
    }
    if blockers or not apply:
        return payload

    migration_id = _sha_bytes(_canonical({
        "target": target,
        "old_sha256": old_expected,
        "new_sha256": new_expected,
        "reason": clean_reason,
        "contract_hash": current.get("contract_hash"),
    }))
    ledger_result = append_research_decision({
        "dataset_hash": (current.get("evidence") or {}).get("dataset_hashes", {}).get("challenger"),
        "commit": current.get("head"),
        "hypothesis": "PROJECT_MEMORY_PROTECTED_POLICY_MIGRATION",
        "proposed_change": {
            "migration_id": migration_id,
            "path": target,
            "old_sha256": old_expected,
            "new_sha256": new_expected,
        },
        "reason": clean_reason,
        "tests": [
            "EXACT_ONE_POLICY_PATH_CHANGED",
            "OLD_AND_NEW_SHA256_MATCH",
            "NO_UNRELATED_CONTRACT_VIOLATIONS",
        ],
        "result": "HUMAN_REVIEWED_HASH_MIGRATION_APPROVED",
        "final_state": "RESEARCH_ONLY",
    }, path=decision_path)

    seed = json.loads(json.dumps(previous))
    seed["baseline"]["protected_policy_hashes"] = dict(new_hashes)
    seed["updated_at"] = utc_now()
    seed["guardrails_status"] = "FAIL"
    seed["can_continue_research"] = False
    seed["violations"] = ["PROJECT_MEMORY_POLICY_MIGRATION_IN_PROGRESS"]
    _atomic_json(state_path, seed)
    final = run_contract_audit(
        apply=True, contract_path=contract_path, state_path=state_path,
        decision_path=decision_path, sources=sources, env_path=env_path,
        policy_paths=allowed_paths,
    )
    if final.get("guardrails_status") != "PASS":
        payload["status"] = "BLOCKED_AFTER_APPLY"
        payload["blockers"] = list(final.get("violations") or ["POST_MIGRATION_AUDIT_FAILED"])
        payload["migration_id"] = migration_id
        payload["decision_ledger"] = ledger_result
        return payload

    payload.update({
        "status": "MIGRATED",
        "migration_id": migration_id,
        "decision_ledger": ledger_result,
        "guardrails_status": "PASS",
        "can_continue_research": True,
    })
    return payload


def contract_status() -> dict[str, Any]:
    value = _read_json(STATE_PATH, {}) or {}
    if not isinstance(value, dict) or not value:
        return {
            "status": "NOT_ACTIVATED", "guardrails_status": "FAIL",
            "violations": ["CONTRACT_BASELINE_NOT_FROZEN"],
            "research_only": True, "simulation_only": True,
            "paper_filter_enabled": False, "can_send_real_orders": False,
            "final_recommendation": "NO LIVE",
        }
    return value
