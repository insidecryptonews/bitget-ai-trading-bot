from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.candidate_ranking import CandidateRanking
from app.config import BotConfig
from app.database import Database
from app.edge_hardening_utils import apply_net_costs, cost_config, fetch_group_metrics
from app.execution_safety import ExecutionSafetyAudit, check_clock_drift
from app.net_edge_lab import NetEdgeLab
from app.score_calibration import load_score_rows


class DummyLogger:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass


def cfg(tmp_path, **kwargs):
    base = {
        "data_vault_export_dir": str(tmp_path / "training_exports"),
        "data_vault_external_enabled": False,
        "enable_paper_policy_filter": False,
        "net_edge_min_samples": 2,
        "net_edge_min_net_pf": 1.05,
        "net_edge_min_tp_ratio": 0.01,
        "net_edge_max_time_ratio": 0.95,
    }
    base.update(kwargs)
    return BotConfig(**base)


def make_db(tmp_path, config=None):
    config = config or cfg(tmp_path)
    db = Database(config, DummyLogger())
    db.sqlite_path = tmp_path / "clean_views.db"
    db.initialize()
    return db


def seed_row(
    db,
    *,
    idx: int,
    source: str = "trade_signal",
    symbol: str = "XRPUSDT",
    side: str = "LONG",
    regime: str = "RISK_ON",
    barrier: str = "TP1",
    ret: float = 1.0,
):
    ts = (datetime.now(timezone.utc) - timedelta(minutes=idx)).isoformat()
    obs_id = db.record_signal_observation({
        "timestamp": ts,
        "symbol": symbol,
        "side": side,
        "strategy_type": "TEST",
        "confidence_score": 88,
        "market_regime": regime,
        "entry_price": 100.0,
        "score_bucket": "80-89",
    })
    db.record_signal_label({
        "timestamp": ts,
        "observation_id": obs_id,
        "label": 1 if ret > 0 else -1 if ret < 0 else 0,
        "first_barrier_hit": barrier,
        "bars_to_outcome": 8,
        "realized_return_pct": ret,
    })
    db.upsert_signal_path_metric({
        "observation_id": obs_id,
        "source": source,
        "symbol": symbol,
        "side": side,
        "score": 88,
        "score_bucket": "80-89",
        "market_regime": regime,
        "final_return_pct": ret,
        "first_barrier_hit": barrier,
        "status": "matured",
        "created_at": ts,
        "updated_at": ts,
    })
    return obs_id


def test_record_signal_label_blocks_duplicate_insert(tmp_path):
    db = make_db(tmp_path)
    obs_id = seed_row(db, idx=1)
    first = db.fetch_signal_label_for_observation(obs_id)
    duplicate_id = db.record_signal_label({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "observation_id": obs_id,
        "label": -1,
        "first_barrier_hit": "SL",
        "bars_to_outcome": 2,
        "realized_return_pct": -9.0,
    })

    with db._connect() as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM signal_labels WHERE observation_id=?", (obs_id,)).fetchone()["c"]

    assert duplicate_id == first["id"]
    assert count == 1
    assert db.fetch_signal_label_for_observation(obs_id)["first_barrier_hit"] == "TP1"


def test_fetch_group_metrics_uses_clean_view_and_excludes_market_probe(tmp_path):
    config = cfg(tmp_path)
    db = make_db(tmp_path, config)
    seed_row(db, idx=1, source="trade_signal", symbol="XRPUSDT", ret=1.0)
    seed_row(db, idx=2, source="trade_signal", symbol="XRPUSDT", ret=1.0)
    seed_row(db, idx=3, source="market_probe", symbol="BNBUSDT", ret=5.0)
    seed_row(db, idx=4, source="market_probe", symbol="BNBUSDT", ret=5.0)

    rows = fetch_group_metrics(
        db,
        since=(datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(),
        group_key="symbol",
        min_samples=1,
    )
    by_symbol = {row["group_value"]: apply_net_costs(row, cost_config(config)) for row in rows}

    assert set(by_symbol) == {"XRPUSDT"}
    assert by_symbol["XRPUSDT"]["samples"] == 2
    assert by_symbol["XRPUSDT"]["clean_view"] is True
    assert by_symbol["XRPUSDT"]["market_probe_excluded"] is True


def test_net_edge_and_candidate_ranking_do_not_promote_market_probe(tmp_path):
    config = cfg(tmp_path)
    db = make_db(tmp_path, config)
    for idx in range(4):
        seed_row(db, idx=idx, source="market_probe", symbol="BNBUSDT", ret=10.0)

    net_edge = NetEdgeLab(config, db).build(hours=24)
    ranking = CandidateRanking(config, db).build(hours=24)

    assert net_edge["top_candidates"] == []
    assert ranking["top_candidates"] == []
    assert ranking["status"] == "NO_VALID_CANDIDATES"


def test_score_calibration_loader_excludes_duplicate_and_market_probe_rows(tmp_path):
    db = make_db(tmp_path)
    trade_obs = seed_row(db, idx=1, source="trade_signal", symbol="XRPUSDT", ret=1.0)
    seed_row(db, idx=2, source="market_probe", symbol="BNBUSDT", ret=5.0)
    # Direct raw insert bypasses record_signal_label to mimic an already
    # contaminated DB; loader must still keep one clean row per observation.
    with db._connect() as conn:
        conn.execute(
            """
            INSERT INTO signal_labels(timestamp, observation_id, label, first_barrier_hit, bars_to_outcome, realized_return_pct)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (datetime.now(timezone.utc).isoformat(), trade_obs, -1, "SL", 2, -9.0),
        )

    rows = load_score_rows(db, hours=24)

    assert len(rows) == 1
    assert rows[0]["observation_id"] == trade_obs
    assert rows[0]["source"] == "trade_signal"
    assert rows[0]["clean_view"] is True


def test_clock_drift_unknown_blocks_pre_live_readiness_contract():
    unknown = check_clock_drift(exchange_time=None)
    assert unknown["clock_drift_status"] == "UNKNOWN"
    assert unknown["clock_drift_status"] != "OK"
    text = ExecutionSafetyAudit(cfg(Path("."))).to_text()
    assert "clock_drift: UNKNOWN" in text
    assert "pre_live_readiness_clock_gate: BLOCKED_CLOCK_DRIFT_UNKNOWN" in text


def test_default_config_safety_flags_remain_paper_safe(tmp_path):
    config = cfg(tmp_path)
    assert config.live_trading is False
    assert config.dry_run is True
    assert config.paper_trading is True
    assert config.enable_paper_policy_filter is False
    assert config.can_send_real_orders is False
