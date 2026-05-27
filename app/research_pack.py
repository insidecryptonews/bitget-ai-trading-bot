from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .utils import utc_now


FINAL_RECOMMENDATION = "NO LIVE"
ALLOWED_RECENT_TABLES = {"signal_observations", "signal_labels", "trades", "events", "worker_lock"}


@dataclass
class ResearchPack:
    generated_at: str
    git_version: str
    current_phase: str
    safety: dict[str, Any]
    health: dict[str, Any]
    short_report: str | None = None
    phase8_validator_dot: dict[str, Any] | None = None
    phase9_readiness: dict[str, Any] | None = None
    cost_stress_dot: dict[str, Any] | None = None
    dot_fold_details: dict[str, Any] | None = None
    dot_regime_diagnosis: dict[str, Any] | None = None
    net_profit_lock_summary: dict[str, Any] | None = None
    data_freshness_summary: dict[str, Any] | None = None
    recent_signals: list[dict[str, Any]] = field(default_factory=list)
    recent_labels: list[dict[str, Any]] = field(default_factory=list)
    recent_paper_trades: list[dict[str, Any]] = field(default_factory=list)
    recent_errors: list[dict[str, Any]] = field(default_factory=list)
    api_429_count: int = 0
    worker_lock: dict[str, Any] = field(default_factory=dict)
    db_size: dict[str, Any] = field(default_factory=dict)
    ohlcv_summary: dict[str, Any] = field(default_factory=dict)
    candidate_ranking_summary: dict[str, Any] = field(default_factory=dict)
    score_incubator_summary: dict[str, Any] = field(default_factory=dict)
    time_death_summary: dict[str, Any] = field(default_factory=dict)
    exit_policy_summary: dict[str, Any] = field(default_factory=dict)
    runtime_latency: dict[str, Any] = field(default_factory=dict)
    backup_status: dict[str, Any] = field(default_factory=dict)
    omissions: list[str] = field(default_factory=list)
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_research_pack(config: Any, db: Any, *, hours: int = 24, include_short_report: bool = True) -> dict[str, Any]:
    """Build a compact, secret-free support pack for ChatGPT/human review.

    The pack is intentionally lightweight. Heavy 720h replay labs are represented
    by CLI commands/omissions, not executed here.
    """
    safety = {
        "LIVE_TRADING": bool(getattr(config, "live_trading", False)),
        "DRY_RUN": bool(getattr(config, "dry_run", True)),
        "PAPER_TRADING": bool(getattr(config, "paper_trading", True)),
        "ENABLE_PAPER_POLICY_FILTER": bool(getattr(config, "enable_paper_policy_filter", False)),
        "ENABLE_CANDIDATE_SHADOW_MONITOR": bool(getattr(config, "enable_candidate_shadow_monitor", False)),
        "can_send_real_orders": bool(getattr(config, "can_send_real_orders", False)),
    }
    omissions = [
        "heavy_720h_phase8_phase9_labs_not_run_inside_research_pack",
        "no_env_or_secret_values_included",
        "no_database_dump_included",
    ]
    pack = ResearchPack(
        generated_at=utc_now().isoformat(),
        git_version=_git_version(),
        current_phase="Phase 9 Pre-Paper/Demo Readiness",
        safety=safety,
        health={"status": "research_pack_local", "mode": "paper_research"},
        short_report=_safe_short_report(config, db, hours=hours) if include_short_report else None,
        data_freshness_summary=_safe_data_freshness(config, db),
        recent_signals=_recent_rows(db, "signal_observations", limit=20),
        recent_labels=_recent_rows(db, "signal_labels", limit=20),
        recent_paper_trades=_recent_rows(db, "trades", limit=20),
        recent_errors=_recent_rows(db, "events", where="LOWER(COALESCE(event_type,'')) LIKE '%error%'", limit=20),
        api_429_count=_count_events_like(db, "429"),
        worker_lock=_worker_lock_summary(db),
        db_size=_db_size_summary(db),
        ohlcv_summary=_ohlcv_summary(db),
        candidate_ranking_summary={"status": "not_run_in_pack", "command": "python -m app.research_lab candidate-ranking --hours 24"},
        score_incubator_summary={"status": "not_run_in_pack", "command": "python -m app.research_lab candidate-incubator --hours 24"},
        time_death_summary={"status": "not_run_in_pack", "command": "python -m app.research_lab time-death-autopsy --hours 24"},
        exit_policy_summary={"status": "not_run_in_pack", "command": "python -m app.research_lab exit-policy-v2 --hours 72 --symbols DOTUSDT"},
        runtime_latency={"status": "not_run_in_pack", "command": "python -m app.research_lab latency-audit --hours 24"},
        backup_status={"status": "not_run_in_pack", "command": "python -m app.research_lab data-vault-status"},
        omissions=omissions,
    )
    return _sanitize(pack.as_dict())


def render_research_pack_text(payload: dict[str, Any]) -> str:
    lines = [
        "RESEARCH PACK START",
        f"generated_at: {payload.get('generated_at')}",
        f"git_version: {payload.get('git_version')}",
        f"current_phase: {payload.get('current_phase')}",
        f"final_recommendation: {payload.get('final_recommendation')}",
        "safety:",
    ]
    for key, value in (payload.get("safety") or {}).items():
        lines.append(f"- {key}: {value}")
    lines.append(f"data_freshness_summary: {payload.get('data_freshness_summary')}")
    lines.append(f"recent_signals_count: {len(payload.get('recent_signals') or [])}")
    lines.append(f"recent_labels_count: {len(payload.get('recent_labels') or [])}")
    lines.append(f"recent_paper_trades_count: {len(payload.get('recent_paper_trades') or [])}")
    lines.append(f"api_429_count: {payload.get('api_429_count')}")
    lines.append("omissions:")
    for item in payload.get("omissions") or []:
        lines.append(f"- {item}")
    lines.extend([
        "research_only: true",
        "paper_filter_enabled: false",
        "can_send_real_orders: false",
        "RESEARCH PACK END",
    ])
    return "\n".join(lines)


def _safe_short_report(config: Any, db: Any, *, hours: int) -> str | None:
    try:
        from .dashboard_pro import build_dashboard_short_report
        return str(build_dashboard_short_report(config, db, hours=min(int(hours), 24)).get("text") or "")
    except Exception as exc:
        return f"short_report_unavailable:{type(exc).__name__}"


def _safe_data_freshness(config: Any, db: Any) -> dict[str, Any]:
    try:
        from .data_freshness_gate import evaluate_freshness_many
        symbols = ["DOTUSDT"]
        verdicts = evaluate_freshness_many(db, symbols=symbols, timeframe=getattr(config, "main_timeframe", "5m"))
        return {symbol: verdict.as_dict() for symbol, verdict in verdicts.items()}
    except Exception as exc:
        return {"status": "unavailable", "error_type": type(exc).__name__}


def _recent_rows(db: Any, table: str, *, where: str = "", limit: int = 20) -> list[dict[str, Any]]:
    if table not in ALLOWED_RECENT_TABLES:
        return []
    if not db or not _table_exists(db, table):
        return []
    sql = f"SELECT * FROM {table}"
    if where:
        sql += f" WHERE {where}"
    sql += " ORDER BY 1 DESC LIMIT ?"
    if bool(getattr(db, "_use_postgres", False)):
        sql = sql.replace("?", "%s")
    try:
        with db._connect() as conn:
            rows = conn.execute(sql, (int(limit),)).fetchall()
        return [_row_to_public_dict(row) for row in rows]
    except Exception:
        return []


def _table_exists(db: Any, table: str) -> bool:
    try:
        return bool(db.table_exists(table))
    except Exception:
        return False


def _row_to_public_dict(row: Any) -> dict[str, Any]:
    try:
        data = dict(row)
    except Exception:
        try:
            data = {str(index): value for index, value in enumerate(row)}
        except Exception:
            return {}
    return _sanitize(data)


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if any(token in key_text.lower() for token in ("secret", "token", "api_key", "passphrase", "password")):
                sanitized[key_text] = "***"
            else:
                sanitized[key_text] = _sanitize(item)
        return sanitized
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, str) and any(token in value.lower() for token in ("api_key=", "secret=", "passphrase=", "token=")):
        return "***"
    return value


def _count_events_like(db: Any, needle: str) -> int:
    if not db or not _table_exists(db, "events"):
        return 0
    sql = "SELECT COUNT(*) FROM events WHERE LOWER(COALESCE(message,'')) LIKE ?"
    if bool(getattr(db, "_use_postgres", False)):
        sql = sql.replace("?", "%s")
    try:
        with db._connect() as conn:
            row = conn.execute(sql, (f"%{needle.lower()}%",)).fetchone()
        return int(row[0] if row else 0)
    except Exception:
        return 0


def _worker_lock_summary(db: Any) -> dict[str, Any]:
    rows = _recent_rows(db, "worker_lock", limit=1)
    return rows[0] if rows else {"status": "not_available"}


def _db_size_summary(db: Any) -> dict[str, Any]:
    path = getattr(db, "db_path", "") or getattr(getattr(db, "config", None), "database_path", "")
    return {"path_present": bool(path), "size_bytes": 0}


def _ohlcv_summary(db: Any) -> dict[str, Any]:
    if not _table_exists(db, "ohlcv_candles"):
        return {"status": "NEED_DATA", "rows": 0}
    try:
        with db._connect() as conn:
            row = conn.execute("SELECT COUNT(*), MIN(timestamp), MAX(timestamp) FROM ohlcv_candles").fetchone()
        return {"status": "OK", "rows": int(row[0] or 0), "oldest": row[1], "newest": row[2]}
    except Exception:
        return {"status": "UNKNOWN", "rows": 0}


def _git_version() -> str:
    try:
        import subprocess
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True, timeout=3).strip()
    except Exception:
        return "unknown"
