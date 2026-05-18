from __future__ import annotations

import csv
import io
import json
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .config import PROJECT_ROOT


SENSITIVE_KEY_RE = re.compile(
    r"(api[_-]?key|secret|token|password|passphrase|private[_-]?key|"
    r"dashboard[_-]?auth[_-]?token|data[_-]?vault[_-]?s3[_-]?access[_-]?key[_-]?id|"
    r"data[_-]?vault[_-]?s3[_-]?secret[_-]?access[_-]?key|"
    r"r2[_-]?(access|secret|token|key)|bitget[_-]?(access|secret|token|key))",
    re.IGNORECASE,
)
ASSIGNMENT_RE = re.compile(
    r"(?i)([\"']?\b[A-Z0-9_]*(?:API_KEY|SECRET|TOKEN|PASSWORD|PASSPHRASE|PRIVATE_KEY|"
    r"DASHBOARD_AUTH_TOKEN|DATA_VAULT_S3_ACCESS_KEY_ID|DATA_VAULT_S3_SECRET_ACCESS_KEY|"
    r"R2|BITGET)[A-Z0-9_]*[\"']?\s*[:=]\s*[\"']?)([^\s,;\"']+)"
)
GENERIC_ASSIGNMENT_RE = re.compile(
    r"(?i)([\"']?\b(key|token|secret|password|passphrase|private_key)[\"']?\s*[:=]\s*[\"']?)([^\s,;\"']+)"
)


def sanitize_text_for_dashboard(text: Any) -> str:
    value = "" if text is None else str(text)
    value = ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}***", value)
    value = GENERIC_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}***", value)
    return value


def sanitize_json_for_dashboard(value: Any) -> Any:
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            if SENSITIVE_KEY_RE.search(str(key)):
                clean[str(key)] = "***"
            else:
                clean[str(key)] = sanitize_json_for_dashboard(item)
        return clean
    if isinstance(value, list):
        return [sanitize_json_for_dashboard(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_json_for_dashboard(item) for item in value]
    if isinstance(value, str):
        return sanitize_text_for_dashboard(value)
    return value


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _since(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=max(1, int(hours or 24)))).isoformat()


def _safe_limit(limit: int | None, default: int = 1000, maximum: int = 5000) -> int:
    try:
        raw = int(limit or default)
    except (TypeError, ValueError):
        raw = default
    return max(1, min(maximum, raw))


def _git_version() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        return result.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


@dataclass
class ReportSection:
    name: str
    text: str
    status: str
    duration_ms: int

    def to_dict(self) -> dict[str, Any]:
        return sanitize_json_for_dashboard(
            {
                "name": self.name,
                "status": self.status,
                "duration_ms": self.duration_ms,
                "text": self.text,
            }
        )


class DashboardProReporter:
    def __init__(self, config: Any, db: Any, logger: Any | None = None) -> None:
        self.config = config
        self.db = db
        self.logger = logger

    def build(self, *, hours: int = 24) -> dict[str, Any]:
        from .research_lab import ResearchLab

        lab = ResearchLab(self.db, self.config, self.logger)
        sections: list[tuple[str, Callable[[], str]]] = [
            ("Safety", self._safety_section),
            ("Worker Health", self._worker_health_section),
            ("Paper Positions", self._paper_positions_section),
            ("Paper Summary", self._paper_summary_section),
            ("Training Summary 6h", lambda: lab.training_summary(hours=6)),
            ("Acceleration Plan 24h", lambda: lab.acceleration_plan(hours=hours)),
            ("Time Death Autopsy 24h", lambda: lab.time_death_autopsy(hours=hours)),
            ("Time Death Filter Proposal 24h", lambda: lab.time_death_filter_proposal(hours=hours)),
            ("Exit Cause Backtest 24h", lambda: lab.exit_cause_backtest(hours=hours)),
            ("Candidate Ranking 24h", lambda: lab.candidate_ranking(hours=hours)),
            ("Score Calibration 24h", lambda: lab.score_calibration(hours=hours)),
            ("Candidate Incubator 24h", lambda: lab.candidate_incubator(hours=hours)),
            ("Training Data Integrity 24h", lambda: lab.training_data_integrity(hours=hours)),
            ("Worker Health Audit", lambda: lab.worker_health_audit()),
            ("Data Vault Audit", lambda: lab.data_vault_audit()),
            ("Dashboard Data Binding Audit", lambda: lab.dashboard_data_binding_audit()),
            ("Edge Guard 24h", lambda: lab.edge_guard(hours=hours)),
            ("Paper Policy Orchestrator 24h", lambda: lab.paper_policy_orchestrator(hours=hours)),
            ("Net Edge Lab 24h", lambda: lab.net_edge_lab(hours=hours)),
            ("EV / Slippage Gate 24h", lambda: lab.ev_slippage_calibration_gate(hours=hours)),
            ("Anti Overfit Gate 24h", lambda: lab.anti_overfit_gate(hours=hours)),
            ("Policy Stability Matrix 24h", lambda: lab.policy_stability_matrix(hours=hours)),
            ("Pre-Move Event Labeler 24h", lambda: lab.pre_move_event_labeler(hours=hours)),
            ("Pre-Move Feature Snapshot 24h", lambda: lab.pre_move_feature_snapshot(hours=hours)),
            ("Pre-Move Pattern Miner 24h", lambda: lab.pre_move_pattern_miner(hours=hours)),
            ("Pre-Move Similarity Scanner 6h", lambda: lab.pre_move_similarity_scanner(hours=6)),
            ("Exit Simulation 24h", lambda: lab.exit_simulation(hours=hours)),
            ("Exit Label Calibration V2 24h", lambda: lab.exit_label_calibration_v2(hours=hours)),
            ("Exit Policy Backtest 24h", lambda: lab.exit_policy_backtest(hours=hours)),
            ("Latency Audit 24h", lambda: lab.latency_audit(hours=hours)),
            ("VPS Runtime Health", lambda: lab.vps_runtime_health()),
            ("Data Vault Status", lambda: lab.data_vault_status()),
        ]
        started = time.perf_counter()
        rendered = [self._run_section(name, callback) for name, callback in sections]
        duration_ms = int((time.perf_counter() - started) * 1000)
        text = self.to_text(rendered, hours=hours, duration_ms=duration_ms)
        return sanitize_json_for_dashboard(
            {
                "generated_at": _iso_now(),
                "hours": hours,
                "git_version": _git_version(),
                "duration_ms": duration_ms,
                "approx_size_bytes": len(text.encode("utf-8")),
                "sections": [section.to_dict() for section in rendered],
                "text": text,
                "final_recommendation": "NO LIVE",
                "recommended_next_action": "PAPER ONLY: revisar edge neto, TIME death y Candidate Ranking antes de cualquier cambio.",
            }
        )

    def build_short(self, *, hours: int = 24) -> dict[str, Any]:
        from .research_lab import ResearchLab

        lab = ResearchLab(self.db, self.config, self.logger)
        sections: list[tuple[str, Callable[[], str]]] = [
            ("Safety", self._safety_section),
            ("Worker Health", self._worker_health_section),
            ("Paper Positions", self._paper_positions_section),
            ("Training Summary 6h", lambda: lab.training_summary(hours=6)),
            ("Candidate Ranking 24h", lambda: lab.candidate_ranking(hours=hours)),
            ("Score Calibration 24h", lambda: lab.score_calibration(hours=hours)),
            ("Candidate Incubator 24h", lambda: lab.candidate_incubator(hours=hours)),
            ("Training Data Integrity 24h", lambda: lab.training_data_integrity(hours=hours)),
            ("Worker Health Audit", lambda: lab.worker_health_audit()),
            ("Data Vault Audit", lambda: lab.data_vault_audit()),
            ("Dashboard Data Binding Audit", lambda: lab.dashboard_data_binding_audit()),
            ("Edge Guard 24h", lambda: lab.edge_guard(hours=hours)),
            ("Paper Policy Orchestrator 24h", lambda: lab.paper_policy_orchestrator(hours=hours)),
            ("Time Death Autopsy 24h", lambda: lab.time_death_autopsy(hours=hours)),
            ("Exit Label Calibration V2 24h", lambda: lab.exit_label_calibration_v2(hours=hours)),
            ("Pre-Move Pattern Miner 24h", lambda: lab.pre_move_pattern_miner(hours=hours)),
            ("Data Vault Status", lambda: lab.data_vault_status()),
        ]
        started = time.perf_counter()
        rendered = [self._run_section(name, callback) for name, callback in sections]
        duration_ms = int((time.perf_counter() - started) * 1000)
        lines = [
            "DASHBOARD PRO SHORT REPORT START",
            f"timestamp: {_iso_now()}",
            f"hours: {hours}",
            f"git_version: {_git_version()}",
            f"duration_ms: {duration_ms}",
            "final_recommendation: NO LIVE",
            "",
        ]
        for section in rendered:
            lines.extend([f"[{section.name}]", section.text.strip()[:2500] or "not_loaded", ""])
        lines.append("DASHBOARD PRO SHORT REPORT END")
        text = sanitize_text_for_dashboard("\n".join(lines))
        return sanitize_json_for_dashboard(
            {
                "generated_at": _iso_now(),
                "hours": hours,
                "git_version": _git_version(),
                "duration_ms": duration_ms,
                "approx_size_bytes": len(text.encode("utf-8")),
                "sections": [section.to_dict() for section in rendered],
                "text": text,
                "final_recommendation": "NO LIVE",
            }
        )

    def to_text(self, sections: list[ReportSection], *, hours: int, duration_ms: int) -> str:
        lines = [
            "DASHBOARD PRO FULL REPORT START",
            f"timestamp: {_iso_now()}",
            f"hours: {hours}",
            f"git_version: {_git_version()}",
            f"duration_ms: {duration_ms}",
            "final_recommendation: NO LIVE",
            "recommended_next_action: PAPER ONLY / KEEP_RESEARCH",
            "",
        ]
        for section in sections:
            lines.extend(
                [
                    f"SECTION START: {section.name}",
                    f"status: {section.status}",
                    f"duration_ms: {section.duration_ms}",
                    section.text.strip() or "not_loaded",
                    f"SECTION END: {section.name}",
                    "",
                ]
            )
        lines.append("DASHBOARD PRO FULL REPORT END")
        return sanitize_text_for_dashboard("\n".join(lines))

    def _run_section(self, name: str, callback: Callable[[], str]) -> ReportSection:
        started = time.perf_counter()
        try:
            text = callback()
            status = "ok"
        except Exception as exc:
            text = f"ERROR_SANITIZED: {sanitize_text_for_dashboard(type(exc).__name__)}"
            status = "error"
        duration_ms = int((time.perf_counter() - started) * 1000)
        return ReportSection(name=name, text=sanitize_text_for_dashboard(text), status=status, duration_ms=duration_ms)

    def _safety_section(self) -> str:
        lines = [
            "SAFETY START",
            f"PAPER_TRADING={bool(getattr(self.config, 'paper_trading', True))}",
            f"LIVE_TRADING={bool(getattr(self.config, 'live_trading', False))}",
            f"DRY_RUN={bool(getattr(self.config, 'dry_run', True))}",
            f"WORKER_LIGHTWEIGHT_MODE={bool(getattr(self.config, 'worker_lightweight_mode', True))}",
            f"ENABLE_PAPER_POLICY_FILTER={bool(getattr(self.config, 'enable_paper_policy_filter', False))}",
            f"PAPER_POLICY_FILTER_MODE={getattr(self.config, 'paper_policy_filter_mode', 'shadow')}",
            "can_send_real_orders=false",
            "final_recommendation: NO LIVE",
            "SAFETY END",
        ]
        return "\n".join(lines)

    def _worker_health_section(self) -> str:
        db_size = getattr(self.db, "sqlite_path", None)
        size_mb = 0.0
        try:
            size_mb = db_size.stat().st_size / (1024 * 1024) if db_size and db_size.exists() else 0.0
        except Exception:
            size_mb = 0.0
        return "\n".join(
            [
                "WORKER HEALTH START",
                f"db_size_mb: {size_mb:.2f}",
                "dashboard_mode: read_only",
                "real_orders: disabled",
                "final_recommendation: NO LIVE",
                "WORKER HEALTH END",
            ]
        )

    def _paper_positions_section(self) -> str:
        rows: list[dict[str, Any]] = []
        try:
            rows = self.db.get_open_paper_positions_summary(limit=10)
        except Exception:
            rows = []
        lines = ["PAPER POSITIONS START", f"open_positions: {len(rows)}"]
        if rows:
            for row in rows[:10]:
                lines.append(
                    "- "
                    f"symbol={row.get('symbol')} side={row.get('side')} entry={row.get('entry_price')} "
                    f"score={row.get('score')} status={row.get('status')} opened_at={row.get('opened_at')}"
                )
        else:
            lines.append("- none")
        lines.extend(["opened_real_trades: 0", "final_recommendation: NO LIVE", "PAPER POSITIONS END"])
        return "\n".join(lines)

    def _paper_summary_section(self) -> str:
        try:
            summary = self.db.get_paper_trade_summary()
        except Exception:
            summary = {"total": 0, "open": 0, "closed": 0}
        return "\n".join(
            [
                "PAPER SUMMARY START",
                f"total: {summary.get('total', 0)}",
                f"open: {summary.get('open', 0)}",
                f"closed: {summary.get('closed', 0)}",
                "real_orders: disabled",
                "final_recommendation: NO LIVE",
                "PAPER SUMMARY END",
            ]
        )


def build_dashboard_full_report(config: Any, db: Any, *, hours: int = 24, logger: Any | None = None) -> dict[str, Any]:
    return DashboardProReporter(config, db, logger).build(hours=hours)


def full_report_text(config: Any, db: Any, *, hours: int = 24, logger: Any | None = None) -> str:
    return str(build_dashboard_full_report(config, db, hours=hours, logger=logger).get("text") or "")


def build_dashboard_short_report(config: Any, db: Any, *, hours: int = 24, logger: Any | None = None) -> dict[str, Any]:
    return DashboardProReporter(config, db, logger).build_short(hours=hours)


def export_csv(config: Any, db: Any, kind: str, *, hours: int = 24, limit: int = 1000) -> tuple[str, str]:
    since = _since(hours)
    safe_limit = _safe_limit(limit)
    rows: list[dict[str, Any]]
    if kind == "signals":
        rows = _fetch_table(db, "signal_observations", since_iso=since, timestamp_column="timestamp", limit=safe_limit)
    elif kind == "paper-trades":
        rows = [
            row
            for row in _fetch_table(db, "trades", since_iso=_since(max(hours, 168)), timestamp_column="timestamp", limit=safe_limit)
            if str(row.get("mode") or "").lower() == "paper"
        ]
    elif kind == "labels":
        try:
            rows = db.fetch_labeled_signal_rows_since(since, limit=safe_limit)
        except Exception:
            rows = _fetch_table(db, "signal_labels", since_iso=since, timestamp_column="timestamp", limit=safe_limit)
    elif kind == "latency":
        try:
            rows = db.fetch_latency_metrics_since(since, limit=safe_limit)
        except Exception:
            rows = _fetch_table(db, "latency_metrics", since_iso=since, timestamp_column="timestamp", limit=safe_limit)
    elif kind == "pre-move-events":
        try:
            from .pre_move_event_labeler import PreMoveEventLabeler

            payload = PreMoveEventLabeler(config, db).build(hours=hours)
            rows = payload.get("events", []) if isinstance(payload, dict) else []
        except Exception:
            rows = []
    elif kind == "candidates":
        try:
            from .candidate_ranking import CandidateRanking

            payload = CandidateRanking(config, db).build(hours=hours)
            rows = _candidate_rows(payload)
        except Exception:
            rows = []
    else:
        raise ValueError(f"unknown export kind: {kind}")
    filename = f"{kind}_{hours}h.csv"
    return filename, rows_to_csv(rows[:safe_limit])


def _candidate_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not isinstance(payload, dict):
        return rows
    for bucket in ("top_candidates", "watch_list", "reject_list", "candidates", "ranked_candidates"):
        values = payload.get(bucket) or []
        if isinstance(values, list):
            for item in values:
                if isinstance(item, dict):
                    row = dict(item)
                    row.setdefault("list", bucket)
                    rows.append(row)
    return rows


def _fetch_table(db: Any, table: str, *, since_iso: str, timestamp_column: str, limit: int) -> list[dict[str, Any]]:
    try:
        return db.fetch_table_rows(table, since_iso=since_iso, timestamp_column=timestamp_column, limit=limit)
    except Exception:
        return []


def rows_to_csv(rows: list[dict[str, Any]]) -> str:
    clean_rows = sanitize_json_for_dashboard(rows)
    if not clean_rows:
        return "status\nno_data\n"
    keys: list[str] = []
    for row in clean_rows:
        for key in row:
            if key not in keys:
                keys.append(str(key))
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=keys, extrasaction="ignore")
    writer.writeheader()
    for row in clean_rows:
        writer.writerow({key: _csv_value(row.get(key)) for key in keys})
    return buffer.getvalue()


def _csv_value(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(sanitize_json_for_dashboard(value), ensure_ascii=True, default=str)
    if isinstance(value, str):
        return sanitize_text_for_dashboard(value)
    return value
