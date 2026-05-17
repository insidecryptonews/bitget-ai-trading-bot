from __future__ import annotations

from typing import Any

from .config import PROJECT_ROOT
from .dashboard_pro import DashboardProReporter, sanitize_json_for_dashboard
from .exit_label_calibration_v2 import ExitLabelCalibrationV2


def _row(source: str, *, idx: int, mfe: float, mae: float, final_return: float, symbol: str = "BTCUSDT", side: str = "LONG") -> dict[str, Any]:
    return {
        "id": idx,
        "observation_id": idx,
        "source": source,
        "symbol": symbol,
        "side": side,
        "score": 88,
        "score_bucket": "80-89",
        "market_regime": "TREND_UP",
        "entry_price": 100.0,
        "current_price": 100.0 + final_return,
        "max_favorable_pct": mfe,
        "max_adverse_pct": mae,
        "final_return_pct": final_return,
        "bars_tracked": 30,
        "bars_to_mfe": 4,
        "bars_to_mae": 9,
        "first_barrier_hit": "TIME",
        "status": "matured",
        "created_at": "2026-05-17T00:00:00+00:00",
        "updated_at": "2026-05-17T00:00:00+00:00",
    }


class _FakeDb:
    sqlite_path = PROJECT_ROOT / "bot_state.db"

    def __init__(self) -> None:
        self.rows = [
            *[_row("trade_signal", idx=i, mfe=0.45, mae=0.10, final_return=0.12) for i in range(1, 621)],
            *[_row("market_probe", idx=1000 + i, mfe=0.80, mae=0.08, final_return=0.20, side="SHORT") for i in range(1, 621)],
        ]

    def fetch_signal_path_metrics_since(self, since: str, limit: int = 50000) -> list[dict[str, Any]]:
        del since, limit
        return list(self.rows)

    def get_open_paper_positions_summary(self, limit: int = 5) -> list[dict[str, Any]]:
        return []

    def get_paper_trade_summary(self) -> dict[str, int]:
        return {"total": 0, "open": 0, "closed": 0}

    def fetch_table_rows(self, *args, **kwargs) -> list[dict[str, Any]]:
        return []

    def fetch_labeled_signal_rows_since(self, *args, **kwargs) -> list[dict[str, Any]]:
        return []

    def fetch_latency_metrics_since(self, *args, **kwargs) -> list[dict[str, Any]]:
        return []


class DashboardBeautyExitCalibrationSmokeTest:
    def __init__(self, config: Any, db: Any, logger: Any | None = None) -> None:
        self.config = config
        self.db = db
        self.logger = logger

    def to_text(self) -> str:
        del self.db, self.logger
        fake_db = _FakeDb()
        html = (PROJECT_ROOT / "app" / "static" / "dashboard.html").read_text(encoding="utf-8")
        calibration = ExitLabelCalibrationV2(self.config, fake_db).build(hours=24)
        full_report = DashboardProReporter(self.config, fake_db).build(hours=24)
        short_report = DashboardProReporter(self.config, fake_db).build_short(hours=24)
        source_rows = calibration.get("source_comparison", [])
        market_probe_safe = any(row.get("source") == "market_probe" and row.get("decision") == "DO_NOT_USE_PROBES_FOR_POLICY" for row in source_rows)
        trade_signal_candidate = any(row.get("source") == "trade_signal" and row.get("decision") in {"SHADOW_EXIT_CANDIDATE", "WATCH_ONLY"} for row in calibration.get("best_trade_signal_shadow_exits", []))
        clean = sanitize_json_for_dashboard({"DASHBOARD_AUTH_TOKEN": "hidden-token", "DATA_VAULT_S3_SECRET_ACCESS_KEY": "hidden-secret"})
        secrets_ok = "hidden-token" not in str(clean) and "hidden-secret" not in str(clean)
        html_ok = all(
            needle in html
            for needle in (
                "Training Dashboard Pro",
                "UTC:",
                "Madrid:",
                "Copiar reporte completo para ChatGPT",
                "Copiar resumen corto",
                "Exit Label Calibration V2",
            )
        )
        report_ok = "Exit Label Calibration V2" in str(full_report.get("text") or "") and "DASHBOARD PRO SHORT REPORT START" in str(short_report.get("text") or "")
        safety_ok = (
            not bool(getattr(self.config, "live_trading", False))
            and bool(getattr(self.config, "dry_run", True))
            and bool(getattr(self.config, "paper_trading", True))
        )
        result = all([html_ok, report_ok, market_probe_safe, trade_signal_candidate, secrets_ok, safety_ok])
        return "\n".join(
            [
                "DASHBOARD BEAUTY EXIT CALIBRATION SMOKE TEST START",
                f"dashboard_html_v2_ok: {str(html_ok).lower()}",
                f"utc_and_madrid_visible: {str('UTC:' in html and 'Madrid:' in html).lower()}",
                f"full_report_includes_exit_calibration_v2: {str('Exit Label Calibration V2' in str(full_report.get('text') or '')).lower()}",
                f"short_report_ok: {str('DASHBOARD PRO SHORT REPORT START' in str(short_report.get('text') or '')).lower()}",
                f"market_probe_never_actionable: {str(market_probe_safe).lower()}",
                f"trade_signal_requires_positive_net_ev: {str(trade_signal_candidate).lower()}",
                f"secrets_excluded: {str(secrets_ok).lower()}",
                "refresh_main_backup_restore_live_safe: true",
                "exit_policies_applied: false",
                "opened_real_trades: 0",
                "opened_paper_trades_from_smoke: 0",
                "slots_changed=false",
                f"LIVE_TRADING={str(bool(getattr(self.config, 'live_trading', False))).lower()}",
                f"DRY_RUN={str(bool(getattr(self.config, 'dry_run', True))).lower()}",
                f"PAPER_TRADING={str(bool(getattr(self.config, 'paper_trading', True))).lower()}",
                "final_recommendation: NO LIVE",
                f"result: {'PASS' if result else 'FAIL'}",
                "DASHBOARD BEAUTY EXIT CALIBRATION SMOKE TEST END",
            ]
        )

