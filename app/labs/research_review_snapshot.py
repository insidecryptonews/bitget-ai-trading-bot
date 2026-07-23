"""Sanitized, local-only review exports for the active-runtime edge sprint."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .cross_venue import REPO_ROOT
from .edge_sprint_48h import REPORT_ROOT, STATE_PATH, STATUS_PATH, safety


REVIEW_ROOT = REPO_ROOT / "reports" / "research" / "review_snapshots"
CONTRACT_STATE = REPO_ROOT / "data" / "runtime" / "project_memory" / "contract_state.json"
CONTRACT_JSON = REPO_ROOT / "config" / "project" / "BITGET_RESEARCH_PROJECT_CONTRACT.json"
CONTRACT_MD = REPO_ROOT / "docs" / "research" / "BITGET_RESEARCH_PROJECT_CONTRACT.md"
STORAGE_STATE = REPO_ROOT / "data" / "runtime" / "storage_efficiency_v2" / "storage_status.json"
SCHEDULER_STATE = REPO_ROOT / "data" / "runtime" / "storage_efficiency_v2" / "scheduler_status.json"
DISK_STATE = REPO_ROOT / "data" / "runtime" / "storage_efficiency_v2" / "disk_guard_status.json"
REMOTE_STATE = REPO_ROOT / "data" / "runtime" / "storage_efficiency_v2" / "remote_restore_status.json"
CHALLENGER_STATE = REPO_ROOT / "data" / "runtime" / "storage_efficiency_v2" / "challenger_status.json"
ATI_SHADOW_STATE = REPO_ROOT / "reports" / "research" / "ati" / "ati_forward_state.json"
ATI_PAPER_STATE = REPO_ROOT / "data" / "runtime" / "ati_paper" / "executor_status.json"
P11_STATE = REPO_ROOT / "reports" / "research" / "p11_short_forward_observer" / "observer_status.json"
CROSS_STATE = REPO_ROOT / "data" / "runtime" / "cross_venue" / "dashboard_snapshot.json"
DIAGNOSTIC_STATE = REPO_ROOT / "data" / "runtime" / "edge_sprint_48h" / "operability_diagnostic_demo_status.json"
EDGE_DEMO_STATE = REPO_ROOT / "data" / "runtime" / "edge_sprint_48h" / "edge_candidate_demo_status.json"
DASHBOARD_HTML = REPO_ROOT / "reports" / "research" / "dashboard_v10_43c" / "index.html"
VALIDATION_STATE = REPO_ROOT / "data" / "runtime" / "validation" / "latest.json"
QA_STATE = REPO_ROOT / "data" / "runtime" / "edge_sprint_48h" / "qa_status.json"

SECRET_KEYS = {
    "api_key", "apikey", "secret", "secret_key", "access_key", "password", "passwd",
    "token", "bearer", "authorization", "private_key", "client_secret", "account_secret",
    "r2_secret", "r2_access_key",
}
SAFE_SECRET_METADATA = {
    "uses_api_keys", "has_bitget_credentials", "private_endpoints_used",
    "uses_private_endpoints", "source_watch_status",
}
FORBIDDEN_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".parquet", ".jsonl", ".gz", ".env"}
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\b(?:sk|xox[baprs])-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"(?i)(?:signature|x-amz-signature|api[_-]?key|secret|password|token)=[^&\s\"']{8,}"),
    re.compile(r"(?i)(?:authorization|bearer)\s*[:=]\s*[A-Za-z0-9._~+/-]{8,}"),
)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {"value": value}
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {"status": "UNAVAILABLE", "source": str(path), "reason": type(exc).__name__}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sanitize_text(value: str) -> str:
    text = value.replace(str(REPO_ROOT), "<REPO_ROOT>").replace(str(REPO_ROOT).replace("\\", "/"), "<REPO_ROOT>")
    try:
        home = str(Path.home())
        text = text.replace(home, "<USER_HOME>").replace(home.replace("\\", "/"), "<USER_HOME>")
    except RuntimeError:
        pass
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("[REDACTED_SECRET]", text)
    return text


def _sanitize(value: Any, key: str = "") -> Any:
    lowered = key.lower()
    if lowered not in SAFE_SECRET_METADATA and any(token in lowered for token in SECRET_KEYS):
        if isinstance(value, bool) or value is None:
            return value
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): _sanitize(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize(item, key) for item in value]
    if isinstance(value, str):
        return _sanitize_text(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _sanitize_text(str(value))


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_sanitize(payload), indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False) + "\n",
        encoding="utf-8", newline="\n",
    )


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_sanitize_text(text), encoding="utf-8", newline="\n")


def _loopback_health() -> dict[str, Any]:
    try:
        request = urllib.request.Request("http://127.0.0.1:8765/health", method="GET")
        with urllib.request.urlopen(request, timeout=8) as response:  # noqa: S310 - fixed loopback URL
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        return {"status": "UNAVAILABLE", "reason": type(exc).__name__, **safety()}


def _process_snapshot() -> dict[str, Any]:
    script = REPO_ROOT / "scripts" / "status_local_stack.ps1"
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=90, check=True,
        )
        return json.loads(result.stdout)
    except Exception as exc:
        return {"status": "UNAVAILABLE", "reason": type(exc).__name__, **safety()}


def _git_snapshot() -> dict[str, Any]:
    def run(*args: str) -> str:
        try:
            return subprocess.run(
                ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True,
                timeout=15, check=True,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return "UNKNOWN"

    branch = run("branch", "--show-current")
    return {
        "branch": branch,
        "head": run("rev-parse", "HEAD"),
        "tree": run("rev-parse", "HEAD^{tree}"),
        "remote_head": run("rev-parse", f"origin/{branch}"),
        "ahead_behind": run("rev-list", "--left-right", "--count", f"origin/{branch}...HEAD"),
        "log_oneline_10": run("log", "--oneline", "-10").splitlines(),
        "status_short": run("status", "--short").splitlines(),
        "staged": run("diff", "--cached", "--name-only").splitlines(),
        "main_modified_by_export": False,
    }


def _candidate_sources(state: dict[str, Any], include_final: bool) -> list[tuple[str, Path, str]]:
    rows = [
        ("sprint/sprint_status.json", STATUS_PATH, "json"),
        ("sprint/sprint_state.json", STATE_PATH, "json"),
        ("memory/contract_state.json", CONTRACT_STATE, "json"),
        ("memory/BITGET_RESEARCH_PROJECT_CONTRACT.json", CONTRACT_JSON, "json"),
        ("memory/BITGET_RESEARCH_PROJECT_CONTRACT.md", CONTRACT_MD, "text"),
        ("storage/storage_status.json", STORAGE_STATE, "json"),
        ("storage/scheduler_status.json", SCHEDULER_STATE, "json"),
        ("storage/disk_guard_status.json", DISK_STATE, "json"),
        ("storage/remote_restore_status.json", REMOTE_STATE, "json"),
        ("challenger/challenger_status.json", CHALLENGER_STATE, "json"),
        ("ati/ati_shadow_forward_state.json", ATI_SHADOW_STATE, "json"),
        ("ati/ati_paper_executor_status.json", ATI_PAPER_STATE, "json"),
        ("p11/observer_status.json", P11_STATE, "json"),
        ("cross_venue/dashboard_snapshot.json", CROSS_STATE, "json"),
        ("demos/operability_diagnostic_demo_status.json", DIAGNOSTIC_STATE, "json"),
        ("demos/edge_candidate_demo_status.json", EDGE_DEMO_STATE, "json"),
        ("dashboard/index.html", DASHBOARD_HTML, "text"),
        ("validation/latest.json", VALIDATION_STATE, "json"),
        ("validation/qa_status.json", QA_STATE, "json"),
    ]
    sprint_dir = REPORT_ROOT / str(state.get("sprint_id"))
    for path in sorted((sprint_dir / "snapshots").glob("snapshot_*.json")) if (sprint_dir / "snapshots").is_dir() else []:
        rows.append((f"sprint/snapshots/{path.name}", path, "json"))
    if include_final:
        rows.extend([
            ("sprint/FINAL_REPORT.json", sprint_dir / "FINAL_REPORT.json", "json"),
            ("sprint/FINAL_REPORT.md", sprint_dir / "FINAL_REPORT.md", "text"),
        ])
    return rows


def _scan_tree(root: Path) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        lowered = rel.lower()
        if any(lowered.endswith(suffix) for suffix in FORBIDDEN_SUFFIXES) or ".env" in lowered:
            findings.append({"type": "FORBIDDEN_PATH", "path": rel})
            continue
        if path.stat().st_size > 20 * 1024 * 1024:
            findings.append({"type": "FILE_OVER_20MB", "path": rel})
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            findings.append({"type": "NON_TEXT_PAYLOAD", "path": rel})
            continue
        for pattern in SECRET_PATTERNS:
            match = pattern.search(text)
            if match:
                findings.append({
                    "type": "POSSIBLE_SECRET",
                    "path": rel,
                    "evidence_hash_prefix": hashlib.sha256(match.group(0).encode()).hexdigest()[:12],
                })
                break
    return findings


def _build_manifest(root: Path) -> dict[str, Any]:
    generated = datetime.now(timezone.utc).isoformat()
    files = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name != "FILE_MANIFEST.json":
            files.append({
                "path": path.relative_to(root).as_posix(),
                "size": path.stat().st_size,
                "sha256": _sha256(path),
                "type": path.suffix.lower().lstrip(".") or "text",
                "generated_at": generated,
                "source": "SANITIZED_LOCAL_RESEARCH_ARTIFACT",
            })
    return {
        "schema": "bot_review_snapshot_manifest.v1",
        "generated_at": generated,
        "files": files,
        "manifest_self_excluded": True,
        "manifest_self_excluded_reason": "A_FILE_CANNOT_CONTAIN_ITS_OWN_FINAL_SHA256",
        **safety(),
    }


def _summary(state: dict[str, Any], final: bool) -> str:
    populations = state.get("populations") if isinstance(state.get("populations"), dict) else {}
    lines = [
        "BITGET BOT SANITIZED RESEARCH HANDOFF",
        f"sprint_id: {state.get('sprint_id')}",
        f"export_type: {'FINAL_HANDOFF' if final else 'REVIEW_SNAPSHOT'}",
        f"sprint_status: {state.get('status')}",
        f"active_runtime_seconds: {state.get('accumulated_active_runtime_seconds')}",
        f"active_runtime_remaining_seconds: {state.get('active_runtime_remaining_seconds')}",
        f"holdout_accesses: {state.get('holdout_accesses')}",
        f"strategy_verdict: {state.get('strategy_verdict')}",
        f"infrastructure_verdict: {state.get('infrastructure_verdict')}",
        f"ati_shadow_forward_outcomes: {populations.get('ati_shadow_forward_outcomes')}",
        f"ati_paper_trades: {populations.get('ati_paper_trades')}",
        f"p11_outcomes: {populations.get('p11_outcomes')}",
        f"cross_venue_paper_trades: {populations.get('cross_venue_paper_trades')}",
        "SIMULATION ONLY",
        "PAPER_TRADING=True",
        "LIVE_TRADING=False",
        "DRY_RUN=True",
        "ENABLE_PAPER_POLICY_FILTER=False",
        "can_send_real_orders=false",
        "FINAL_RECOMMENDATION=NO LIVE",
    ]
    return "\n".join(lines) + "\n"


def export_review_snapshot(*, apply: bool = False, final: bool = False) -> dict[str, Any]:
    state = _read_json(STATE_PATH)
    target = int(state.get("target_active_runtime_seconds") or 172800)
    accumulated = int(state.get("accumulated_active_runtime_seconds") or 0)
    sprint_dir = REPORT_ROOT / str(state.get("sprint_id"))
    final_ready = bool(
        state.get("status") == "COMPLETED"
        and accumulated >= target
        and (sprint_dir / "FINAL_REPORT.json").is_file()
        and (sprint_dir / "FINAL_REPORT.md").is_file()
    )
    if final and not final_ready:
        return {
            "status": "BLOCKED_FINAL_HANDOFF_NOT_READY",
            "active_runtime_seconds": accumulated,
            "active_runtime_remaining_seconds": max(0, target - accumulated),
            "holdout_accesses": int(state.get("holdout_accesses") or 0),
            **safety(),
        }
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output_dir = sprint_dir if final else REVIEW_ROOT / stamp
    zip_name = "HANDOFF_REVIEW_PACK.zip" if final else "BOT_REVIEW_SNAPSHOT.zip"
    if not apply:
        return {
            "status": "DRY_RUN_NO_WRITE",
            "output": str(output_dir / zip_name),
            "final_ready": final_ready,
            "active_runtime_seconds": accumulated,
            "active_runtime_remaining_seconds": max(0, target - accumulated),
            **safety(),
        }
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="review_pack_", dir=output_dir) as temp_name:
        stage = Path(temp_name) / "payload"
        stage.mkdir(parents=True)
        for rel, source, kind in _candidate_sources(state, include_final=final):
            target_path = stage / rel
            if kind == "json":
                _write_json(target_path, _read_json(source))
            elif source.is_file():
                _write_text(target_path, source.read_text(encoding="utf-8-sig", errors="replace"))
            else:
                _write_text(target_path, f"status: UNAVAILABLE\nsource: {rel}\n")
        _write_json(stage / "health" / "health.json", _loopback_health())
        _write_json(stage / "health" / "processes.json", _process_snapshot())
        _write_json(stage / "git" / "git_final.json", _git_snapshot())
        _write_text(stage / "HANDOFF_SUMMARY.txt", _summary(state, final))
        findings = _scan_tree(stage)
        if findings:
            return {"status": "BLOCKED_SECRET_OR_PAYLOAD_SCAN", "findings": findings, **safety()}
        manifest = _build_manifest(stage)
        _write_json(stage / "FILE_MANIFEST.json", manifest)
        zip_path = output_dir / zip_name
        zip_tmp = zip_path.with_suffix(".zip.tmp")
        with zipfile.ZipFile(zip_tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for path in sorted(stage.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(stage).as_posix())
        os.replace(zip_tmp, zip_path)
        extract_root = Path(temp_name) / "validated_extract"
        with zipfile.ZipFile(zip_path, "r") as archive:
            archive.extractall(extract_root)
        extracted_findings = _scan_tree(extract_root)
        extracted_manifest = _read_json(extract_root / "FILE_MANIFEST.json")
        manifest_ok = all(
            (extract_root / row["path"]).is_file()
            and _sha256(extract_root / row["path"]) == row["sha256"]
            for row in extracted_manifest.get("files") or []
        )
        if extracted_findings or not manifest_ok:
            zip_path.unlink(missing_ok=True)
            return {
                "status": "BLOCKED_POST_ZIP_VALIDATION",
                "findings": extracted_findings,
                "manifest_ok": manifest_ok,
                **safety(),
            }
    return {
        "status": "FINAL_HANDOFF_CREATED" if final else "REVIEW_SNAPSHOT_CREATED",
        "path": str(zip_path),
        "bytes": zip_path.stat().st_size,
        "sha256": _sha256(zip_path),
        "file_count": len(manifest.get("files") or []) + 1,
        "secret_scan": "PASS",
        "manifest_validation": "PASS",
        "holdout_accesses": int(state.get("holdout_accesses") or 0),
        **safety(),
    }
