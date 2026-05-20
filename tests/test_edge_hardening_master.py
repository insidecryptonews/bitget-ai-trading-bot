from __future__ import annotations

from datetime import datetime, timedelta, timezone
import time
import urllib.error
import urllib.request

from app.anti_overfit_gate import AntiOverfitGate
from app.config import BotConfig
from app.database import Database
from app.edge_hardening_smoke_test import EdgeHardeningSmokeTest
from app.ev_slippage_calibration_gate import EvSlippageCalibrationGate
from app.fast_runtime_smoke_test import FastRuntimeSmokeTest
from app.health_server import HealthState, start_health_server
from app.net_edge_lab import NetEdgeLab
from app.research_lab import ResearchLab
from app.structured_output_guard import StructuredOutputGuard, smoke_test_text


class DummyLogger:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass


def wait_for_server_ready(port: int, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    last_exc: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=0.4) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError) as exc:
            last_exc = exc
            time.sleep(0.05)
    raise AssertionError(f"health server did not become ready on port {port}: {last_exc}")


def shutdown_health_server(thread) -> None:
    server = getattr(thread, "server_box", {}).get("server") if hasattr(thread, "server_box") else None
    if server is not None:
        server.shutdown()
        server.server_close()


def cfg(tmp_path, **kwargs):
    base = {
        "data_vault_export_dir": str(tmp_path / "training_exports"),
        "data_vault_external_enabled": False,
        "net_edge_min_samples": 3,
        "net_edge_min_tp_ratio": 0.05,
        "enable_paper_policy_filter": False,
        "paper_policy_filter_mode": "shadow",
    }
    base.update(kwargs)
    return BotConfig(**base)


def make_db(tmp_path, config=None):
    config = config or cfg(tmp_path)
    db = Database(config, DummyLogger())
    db.sqlite_path = tmp_path / "edge.db"
    db.initialize()
    return db


def seed_label(db, *, symbol="XRPUSDT", side="LONG", regime="RISK_ON", score=84, barrier="TP1", ret=1.2, minutes_ago=30):
    ts = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    obs_id = db.record_signal_observation({
        "timestamp": ts,
        "symbol": symbol,
        "side": side,
        "strategy_type": "TEST",
        "confidence_score": score,
        "market_regime": regime,
        "entry_price": 100.0,
        "score_bucket": "80-89" if score < 90 else "90-94",
    })
    db.record_signal_label({
        "timestamp": ts,
        "observation_id": obs_id,
        "label": 1 if barrier.startswith("TP") else -1 if barrier == "SL" else 0,
        "first_barrier_hit": barrier,
        "bars_to_outcome": 8,
        "realized_return_pct": ret,
    })
    return obs_id


def seed_edge_data(db):
    for i in range(4):
        seed_label(db, barrier="TP1", ret=1.2, minutes_ago=20 + i)
    seed_label(db, barrier="SL", ret=-0.4, minutes_ago=28)
    for i in range(5):
        seed_label(db, symbol="BNBUSDT", side="LONG", regime="RISK_OFF", barrier="SL", ret=-1.0, minutes_ago=40 + i)
    for i in range(4):
        seed_label(db, symbol="DOGEUSDT", side="SHORT", regime="RANGE", barrier="TIME", ret=0.0, minutes_ago=60 + i)


def test_net_edge_lab_prints_cost_aware_start_end(tmp_path):
    config = cfg(tmp_path)
    db = make_db(tmp_path, config)
    seed_edge_data(db)
    text = NetEdgeLab(config, db).to_text(hours=24)
    assert "NET EDGE LAB START" in text
    assert "NET EDGE LAB END" in text
    assert "net_PF=" in text
    assert "final_recommendation: NO LIVE" in text


def test_anti_overfit_rejects_bad_groups(tmp_path):
    config = cfg(tmp_path)
    db = make_db(tmp_path, config)
    seed_edge_data(db)
    text = AntiOverfitGate(config, db).to_text(hours=24)
    assert "ANTI OVERFIT GATE START" in text
    assert "ANTI OVERFIT GATE END" in text
    assert "REJECT" in text


def test_ev_gate_rejects_negative_net_ev(tmp_path):
    config = cfg(tmp_path)
    db = make_db(tmp_path, config)
    for i in range(5):
        seed_label(db, symbol="BNBUSDT", side="LONG", regime="RISK_OFF", barrier="SL", ret=-1.0, minutes_ago=10 + i)
    payload = EvSlippageCalibrationGate(config, db).build(hours=24)
    assert any(row["final_decision"] == "REJECT" for row in payload["candidates"])


def test_structured_output_invalid_cannot_allow():
    guard = StructuredOutputGuard()
    result = guard.parse('{"decision":"ALLOW","score": NaN}', {"decision": str, "score": float})
    assert result["valid"] is False
    assert "ALLOW" not in result["final_decision"]
    assert "STRUCTURED OUTPUT GUARD SMOKE TEST START" in smoke_test_text()


def test_research_lab_new_commands_start_end(tmp_path):
    config = cfg(tmp_path)
    db = make_db(tmp_path, config)
    seed_edge_data(db)
    lab = ResearchLab(db, config, DummyLogger())
    checks = [
        (lab.net_edge_lab(24), "NET EDGE LAB START", "NET EDGE LAB END"),
        (lab.anti_overfit_gate(24), "ANTI OVERFIT GATE START", "ANTI OVERFIT GATE END"),
        (lab.ev_slippage_calibration_gate(24), "EV SLIPPAGE CALIBRATION GATE START", "EV SLIPPAGE CALIBRATION GATE END"),
        (lab.policy_stability_matrix(24), "POLICY STABILITY MATRIX START", "POLICY STABILITY MATRIX END"),
        (lab.candidate_ranking(24), "CANDIDATE RANKING START", "CANDIDATE RANKING END"),
        (lab.decision_ledger_audit(24), "DECISION LEDGER AUDIT START", "DECISION LEDGER AUDIT END"),
        (lab.adaptive_exit_backtest(24), "ADAPTIVE EXIT BACKTEST START", "ADAPTIVE EXIT BACKTEST END"),
        (lab.sizing_safety_lab(24), "SIZING SAFETY LAB START", "SIZING SAFETY LAB END"),
        (lab.fast_runtime_readiness(24), "FAST RUNTIME READINESS START", "FAST RUNTIME READINESS END"),
        (lab.websocket_migration_plan(24), "WEBSOCKET MIGRATION PLAN START", "WEBSOCKET MIGRATION PLAN END"),
    ]
    for text, start, end in checks:
        assert start in text
        assert end in text
        assert "NO LIVE" in text


def test_smoke_tests_pass_and_do_not_change_safety(tmp_path):
    config = cfg(tmp_path)
    db = make_db(tmp_path, config)
    seed_edge_data(db)
    assert "result: PASS" in EdgeHardeningSmokeTest(config, db, DummyLogger()).to_text()
    assert "result: PASS" in FastRuntimeSmokeTest(config, db, DummyLogger()).to_text()
    assert config.live_trading is False
    assert config.dry_run is True
    assert config.paper_trading is True


def test_dashboard_new_endpoints_return_safe_json(tmp_path):
    import socket

    config = cfg(tmp_path)
    db = make_db(tmp_path, config)
    seed_edge_data(db)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    thread = start_health_server(HealthState("paper"), port, DummyLogger(), config=config, db=db)
    assert thread.is_alive()
    try:
        wait_for_server_ready(port)
        for path in (
            "/api/training/net-edge-lab?hours=24",
            "/api/training/anti-overfit-gate?hours=24",
            "/api/training/ev-slippage-calibration-gate?hours=24",
            "/api/training/policy-stability-matrix?hours=24",
            "/api/training/candidate-ranking?hours=24",
            "/api/training/decision-ledger-audit?hours=24",
            "/api/training/sizing-safety-lab?hours=24",
            "/api/training/fast-runtime-readiness?hours=24",
            "/api/training/websocket-migration-plan?hours=24",
        ):
            body = urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5).read().decode("utf-8")
            assert "NO LIVE" in body
            assert "SECRET" not in body
            assert "PASSWORD" not in body
    finally:
        shutdown_health_server(thread)


def test_dashboard_server_waits_until_ready(tmp_path):
    import socket

    config = cfg(tmp_path)
    db = make_db(tmp_path, config)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    thread = start_health_server(HealthState("paper"), port, DummyLogger(), config=config, db=db)
    try:
        wait_for_server_ready(port)
        assert thread.is_alive()
    finally:
        shutdown_health_server(thread)
