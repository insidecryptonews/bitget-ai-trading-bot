from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.config import BotConfig
from app.database import Database
from app.exit_policy_backtest import ExitPolicyBacktest
from app.paper_policy_orchestrator import ALLOW_PAPER_CANDIDATE, ORCH_BLOCK_PAPER, ORCH_WATCH_ONLY, PaperPolicyOrchestrator
from app.phase_readiness_smoke_test import PhaseReadinessSmokeTest
from app.walk_forward_validation import WalkForwardValidation


class DummyLogger:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass


def cfg(tmp_path, **kwargs):
    base = {
        "data_vault_export_dir": str(tmp_path / "training_exports"),
        "edge_guard_min_sample": 20,
        "edge_guard_require_recent_stability": False,
        "paper_policy_min_samples": 20,
    }
    base.update(kwargs)
    return BotConfig(**base)


def make_db(tmp_path, config):
    db = Database(config, DummyLogger())
    db.sqlite_path = tmp_path / "phase.db"
    db.initialize()
    return db


def seed_label(db, *, idx=0, symbol="XRPUSDT", side="LONG", regime="RISK_ON", score=85, barrier="TP1", ret=1.0):
    ts = (datetime.now(timezone.utc) - timedelta(minutes=idx)).isoformat()
    bucket = "90-100" if score >= 90 else "80-89" if score >= 80 else "70-79"
    obs_id = db.record_signal_observation({
        "timestamp": ts,
        "symbol": symbol,
        "side": side,
        "strategy_type": "TEST",
        "confidence_score": score,
        "market_regime": regime,
        "entry_price": 100.0,
        "score_bucket": bucket,
    })
    db.record_signal_label({
        "timestamp": ts,
        "observation_id": obs_id,
        "label": 1 if barrier.startswith("TP") else -1 if barrier == "SL" else 0,
        "first_barrier_hit": barrier,
        "bars_to_outcome": 10,
        "realized_return_pct": ret,
    })
    db.upsert_signal_path_metric({
        "observation_id": obs_id,
        "source": "trade_signal",
        "symbol": symbol,
        "side": side,
        "score": score,
        "score_bucket": bucket,
        "market_regime": regime,
        "entry_price": 100.0,
        "current_price": 101.0,
        "max_favorable_pct": max(ret, 0.0) + 0.5,
        "max_adverse_pct": abs(min(ret, 0.0)),
        "final_return_pct": ret,
        "bars_tracked": 30,
        "status": "matured",
        "created_at": ts,
        "updated_at": ts,
    })


def seed_good_xrp(db):
    for idx in range(400):
        if idx % 10 in {0, 1, 2}:
            seed_label(db, idx=idx, barrier="TP1", ret=1.0)
        elif idx % 37 == 0:
            seed_label(db, idx=idx, barrier="SL", ret=-0.5)
        else:
            seed_label(db, idx=idx, barrier="TIME", ret=0.0)


def test_paper_policy_orchestrator_generates_markers_and_allow_candidate(tmp_path):
    config = cfg(tmp_path)
    db = make_db(tmp_path, config)
    seed_good_xrp(db)
    text = PaperPolicyOrchestrator(config, db).to_text(hours=24)
    payload = PaperPolicyOrchestrator(config, db).build(hours=24)
    assert "PAPER POLICY ORCHESTRATOR START" in text
    assert "PAPER POLICY ORCHESTRATOR END" in text
    assert any(row["decision"] == ALLOW_PAPER_CANDIDATE for row in payload["policy_candidates"])
    assert payload["live_allowed"] is False


def test_paper_policy_orchestrator_blocks_bad_short_and_risk_off(tmp_path):
    config = cfg(tmp_path)
    db = make_db(tmp_path, config)
    for idx in range(40):
        seed_label(db, idx=idx, symbol="DOGEUSDT", side="SHORT", regime="RISK_OFF", score=85, barrier="SL", ret=-1.0)
    payload = PaperPolicyOrchestrator(config, db).build(hours=24)
    assert any(row["decision"] == ORCH_BLOCK_PAPER for row in payload["policy_candidates"])
    assert any(str(row.get("group")).upper() in {"SHORT", "RISK_OFF"} for row in payload["blocked"])


def test_paper_policy_orchestrator_sample_small_watch_only(tmp_path):
    config = cfg(tmp_path, edge_guard_min_sample=500, paper_policy_min_samples=500)
    db = make_db(tmp_path, config)
    for idx in range(20):
        seed_label(db, idx=idx, barrier="TP1", ret=1.0)
    payload = PaperPolicyOrchestrator(config, db).build(hours=24)
    assert any(row["decision"] == ORCH_WATCH_ONLY for row in payload["policy_candidates"])


def test_paper_policy_filter_default_off_and_shadow_mode_no_block(tmp_path):
    db = make_db(tmp_path, cfg(tmp_path))
    assert cfg(tmp_path).enable_paper_policy_filter is False
    shadow = cfg(tmp_path, enable_paper_policy_filter=True, paper_policy_filter_mode="shadow")
    decision = PaperPolicyOrchestrator(shadow, db).evaluate_signal("XRPUSDT", "LONG", "RISK_ON", "80-89")
    assert decision.reason in {"shadow_mode_no_block", "no_orchestrator_evidence"}


def test_policy_backtest_variants_walk_forward_and_exit_policy(tmp_path):
    config = cfg(tmp_path)
    db = make_db(tmp_path, config)
    seed_good_xrp(db)
    walk = WalkForwardValidation(config, db).build(hours=24)
    exit_text = ExitPolicyBacktest(config, db).to_text(hours=24)
    assert walk["policies"]
    assert "deterioration_detected" in walk["policies"][0]
    assert "EXIT POLICY BACKTEST START" in exit_text
    assert "variants:" in exit_text


def test_phase_readiness_smoke_test_passes(tmp_path):
    config = cfg(tmp_path)
    db = make_db(tmp_path, config)
    seed_good_xrp(db)
    text = PhaseReadinessSmokeTest(config, db, DummyLogger()).to_text()
    assert "PHASE READINESS SMOKE TEST START" in text
    assert "result: PASS" in text
    assert "LIVE_TRADING=false" in text
    assert "opened_paper_trades_from_smoke: 0" in text
