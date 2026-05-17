from __future__ import annotations

from app.candidate_ranking import CandidateRanking
from app.config import BotConfig
from app.edge_guard import ALLOW_PAPER, EdgeGuard
from app.paper_policy_orchestrator import ALLOW_PAPER_CANDIDATE, PaperPolicyOrchestrator
from app.pre_move_event_labeler import PreMoveEventLabeler
from app.pre_move_pattern_miner import REJECT, TIME_DEATH_PATTERN, WATCH_ONLY, pattern_decision
from app.pre_move_smoke_test import _db, _seed, _seed_bad_generic_bucket, _seed_good_long_short


def cfg() -> BotConfig:
    return BotConfig(
        edge_guard_min_sample=5,
        net_edge_min_samples=5,
        paper_policy_min_samples=5,
        min_score_to_trade=70,
        live_trading=False,
        dry_run=True,
        paper_trading=True,
        enable_paper_policy_filter=False,
        paper_policy_filter_mode="shadow",
    )


def test_pre_move_event_labeler_detects_long_and_short(tmp_path):
    config = cfg()
    db = _db(config, tmp_path / "events.db")
    _seed_good_long_short(db)
    payload = PreMoveEventLabeler(config, db).build(hours=24)
    assert payload["long_events"] > 0
    assert payload["short_events"] > 0
    assert payload["final_recommendation"] == "NO LIVE"


def test_pre_move_pattern_rules_are_conservative():
    config = cfg()
    assert pattern_decision({"samples": 100, "net_EV": -0.1, "net_PF": 0.5}, config) == REJECT
    assert pattern_decision({"samples": 100, "net_EV": 0.2, "net_PF": 2.0, "TIME_after_signal": 0.95, "TP_after_signal": 0.01}, config) == TIME_DEATH_PATTERN
    assert pattern_decision({"samples": 1, "net_EV": 1.0, "net_PF": 3.0}, config) == WATCH_ONLY
    assert pattern_decision({"samples": 100, "direction": "LONG", "net_EV": 0.2, "net_PF": 2.0, "SL_after_signal": 0.30, "TP_after_signal": 0.01}, config) == REJECT
    assert pattern_decision({"samples": 100, "direction": "SHORT", "net_EV": 0.2, "net_PF": 2.0, "TIME_after_signal": 0.90, "TP_after_signal": 0.10}, config) == REJECT


def test_generic_90_bucket_cannot_allow_when_ranking_has_no_valid_candidates(tmp_path):
    config = cfg()
    db = _db(config, tmp_path / "bad_bucket.db")
    _seed_bad_generic_bucket(db)
    ranking = CandidateRanking(config, db).build(hours=24)
    edge = EdgeGuard(config, db).build_edge_guard_report(hours=24)
    orchestrator = PaperPolicyOrchestrator(config, db).build(hours=24)
    assert ranking["status"] == "NO_VALID_CANDIDATES"
    assert not any(row.get("group_type") == "score_bucket" and row.get("decision") == ALLOW_PAPER for row in edge.get("candidate_table", []))
    assert not any(row.get("group_type") == "score_bucket" and row.get("decision") == ALLOW_PAPER_CANDIDATE for row in orchestrator.get("policy_candidates", []))
    assert orchestrator["no_actionable_candidates"] is True


def test_pre_move_smoke_seed_does_not_open_paper(tmp_path):
    config = cfg()
    db = _db(config, tmp_path / "paper_safe.db")
    before = db.get_paper_trade_summary()["open"]
    _seed(db, idx=0, symbol="XRPUSDT", side="LONG", regime="RISK_ON", score=86, barrier="TP1", ret=1.0, mfe=1.2, mae=0.2)
    assert db.get_paper_trade_summary()["open"] == before
    assert config.live_trading is False
    assert config.dry_run is True
    assert config.paper_trading is True
