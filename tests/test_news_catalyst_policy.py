from __future__ import annotations

import json
import socket
import time
import urllib.request
from datetime import datetime, timedelta, timezone

from app.catalyst_classifier import CatalystClassifier
from app.catalyst_registry import CatalystRegistry
from app.config import BotConfig, PROJECT_ROOT
from app.database import Database
from app.health_server import HealthState, start_health_server
from app.news_catalyst_ingestor import NewsCatalystIngestor
from app.news_risk_gate import NEWS_BLOCK_ALL_PAPER, NEWS_BLOCK_SYMBOL, NEWS_CATALYST_BOOST_RESEARCH_ONLY, NewsRiskGate
from app.paper_policy_lab import PaperPolicyLab
from app.policy_backtest import PolicyBacktest
from app.policy_news_smoke_test import PolicyNewsSmokeTest
from app.telegram_notifier import TelegramNotifier
from app.training_pulse import TrainingPulse
from app.walk_forward_validation import WalkForwardValidation
from app.evolution_score import EvolutionScore
from app.training_summary import TrainingSummary


class DummyLogger:
    def warning(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass


def make_db(tmp_path):
    db = Database(BotConfig(), DummyLogger())
    db.sqlite_path = tmp_path / "news_policy.db"
    db.initialize()
    return db


def cfg(**kwargs):
    base = {
        "edge_guard_min_sample": 5,
        "edge_guard_require_recent_stability": False,
        "min_score_to_trade": 70,
    }
    base.update(kwargs)
    return BotConfig(**base)


def seed_label(db, *, symbol="XRPUSDT", side="LONG", regime="RISK_ON", score=86, barrier="TP1", ret=1.0, minutes_ago=10):
    ts = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    obs_id = db.record_signal_observation({
        "timestamp": ts,
        "symbol": symbol,
        "side": side,
        "strategy_type": "TEST",
        "confidence_score": score,
        "market_regime": regime,
        "entry_price": 100.0,
        "score_bucket": "80-89" if score < 90 else "90-100",
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


def seed_policy_dataset(db, wins=12, losses=2):
    for idx in range(wins):
        seed_label(db, barrier="TP1", ret=1.0, minutes_ago=200 - idx)
    for idx in range(losses):
        seed_label(db, barrier="SL", ret=-0.5, minutes_ago=80 - idx)
    for idx in range(8):
        seed_label(db, symbol="BTCUSDT", side="SHORT", regime="RISK_OFF", score=92, barrier="SL", ret=-1.0, minutes_ago=40 - idx)


def test_catalyst_registry_creates_and_lists(tmp_path):
    db = make_db(tmp_path)
    registry = CatalystRegistry(cfg(), db)
    saved = registry.add_manual(
        catalyst_id="test_xrp_clarity",
        title="XRP regulatory clarity",
        symbols=["XRPUSDT"],
        category="regulation",
        direction="bullish",
        severity="high",
        confidence=0.8,
        hours_back=1,
        hours_forward=24,
    )
    assert saved > 0
    rows = registry.list(hours=72)
    assert rows[0]["catalyst_id"] == "test_xrp_clarity"


def test_catalyst_classifier_core_categories():
    classifier = CatalystClassifier()
    assert classifier.classify(title="Crypto market structure clarity bill advances for XRP").direction == "bullish"
    assert classifier.classify(title="Regulator will ban and prohibit crypto exchange activity").direction == "bearish"
    assert classifier.classify(title="Bridge exploit drains funds from protocol").category == "hack"
    assert classifier.classify(title="Exchange announces DOGE delisting").category == "exchange_delisting"
    assert classifier.classify(title="Fed rate hike sparks risk-off crypto selloff").category == "macro"


def test_catalyst_summary_separates_with_without(tmp_path):
    db = make_db(tmp_path)
    config = cfg()
    CatalystRegistry(config, db).add_manual(
        catalyst_id="cat_xrp",
        title="XRP clarity",
        symbols=["XRPUSDT"],
        category="regulation",
        direction="bullish",
        severity="high",
        confidence=0.8,
        hours_back=4,
        hours_forward=24,
    )
    seed_label(db, symbol="XRPUSDT", barrier="TP1", ret=1.0)
    seed_label(db, symbol="DOGEUSDT", barrier="SL", ret=-1.0)
    report = CatalystRegistry(config, db).build_summary(hours=24)
    assert report["with_catalyst"]["samples"] >= 1
    assert report["without_catalyst"]["samples"] >= 1
    assert "CATALYST SUMMARY START" in CatalystRegistry(config, db).to_summary_text(hours=24)


def test_news_ingestion_no_feeds_and_deduplicates(tmp_path):
    db = make_db(tmp_path)
    path = tmp_path / "events.json"
    path.write_text(json.dumps([{"title": "XRP ETF approval clarity", "symbols": "XRPUSDT"}, {"title": "XRP ETF approval clarity", "symbols": "XRPUSDT"}]), encoding="utf-8")
    config = cfg(news_catalyst_manual_events_file=str(path))
    first = NewsCatalystIngestor(config, db, DummyLogger()).run()
    second = NewsCatalystIngestor(config, db, DummyLogger()).run()
    assert first.items_seen == 2
    assert second.duplicates_or_updates >= 1
    empty = NewsCatalystIngestor(cfg(news_catalyst_manual_events_file=""), db, DummyLogger()).run()
    assert empty.items_seen == 0


def test_news_risk_gate_blocks_critical_and_hacked_symbol(tmp_path):
    db = make_db(tmp_path)
    config = cfg()
    reg = CatalystRegistry(config, db)
    reg.add_manual(catalyst_id="global_hack", title="critical exploit", symbols=["GLOBAL"], category="hack", direction="bearish", severity="critical", confidence=0.9, hours_back=1, hours_forward=24)
    reg.add_manual(catalyst_id="symbol_hack", title="DOGE exploit", symbols=["DOGEUSDT"], category="hack", direction="bearish", severity="high", confidence=0.8, hours_back=1, hours_forward=24)
    report = NewsRiskGate(config, db).build(hours=24)
    assert report["global_decision"] == NEWS_BLOCK_ALL_PAPER
    assert any(row["decision"] == NEWS_BLOCK_SYMBOL and row["symbol"] == "DOGEUSDT" for row in report["symbol_decisions"])


def test_news_risk_gate_marks_bullish_catalyst_research_only(tmp_path):
    db = make_db(tmp_path)
    config = cfg()
    CatalystRegistry(config, db).add_manual(catalyst_id="bull_xrp", title="XRP clarity", symbols=["XRPUSDT"], category="regulation", direction="bullish", severity="high", confidence=0.8, hours_back=1, hours_forward=24)
    report = NewsRiskGate(config, db).build(hours=24)
    assert any(row["decision"] == NEWS_CATALYST_BOOST_RESEARCH_ONLY for row in report["symbol_decisions"])


def test_paper_policy_lab_creates_xrp_policy_and_blocks_bad_groups(tmp_path):
    db = make_db(tmp_path)
    config = cfg()
    seed_policy_dataset(db)
    CatalystRegistry(config, db).add_manual(catalyst_id="cat_xrp", title="XRP clarity", symbols=["XRPUSDT"], category="regulation", direction="bullish", severity="high", confidence=0.8, hours_back=4, hours_forward=24)
    report = PaperPolicyLab(config, db).build(hours=24)
    assert any(row["symbol_allowlist"] == "XRPUSDT" for row in report["candidate_policies"])
    assert any(row["requires_catalyst"] for row in report["candidate_policies"])
    assert any(row["group"] in {"SHORT", "RISK_OFF", "BTCUSDT"} for row in report["blocked"])
    assert config.enable_edge_guard_paper_filter is False


def test_walk_forward_detects_sample_and_dependency(tmp_path):
    db = make_db(tmp_path)
    config = cfg(edge_guard_min_sample=20)
    CatalystRegistry(config, db).add_manual(catalyst_id="cat_xrp", title="XRP clarity", symbols=["XRPUSDT"], category="regulation", direction="bullish", severity="high", confidence=0.8, hours_back=12, hours_forward=24)
    for idx in range(400):
        seed_label(db, barrier="TP1" if idx < 300 else "SL", ret=1.0 if idx < 300 else -0.5, minutes_ago=600 - idx)
    report = WalkForwardValidation(config, db).build(hours=24)
    assert report["policies"]
    assert any(row["reason"] in {"catalyst_dependent", "stable_candidate", "recent_deterioration"} for row in report["policies"])


def test_policy_backtest_compares_baseline_and_does_not_open_trades(tmp_path):
    db = make_db(tmp_path)
    config = cfg()
    seed_policy_dataset(db)
    before = db.get_paper_trade_summary()["open"]
    report = PolicyBacktest(config, db).build(hours=24)
    assert "baseline" in report
    assert "policy_filtered" in report
    assert db.get_paper_trade_summary()["open"] == before
    assert "POLICY BACKTEST START" in PolicyBacktest(config, db).to_text(hours=24)


def test_evolution_and_acceleration_include_policy_context(tmp_path):
    db = make_db(tmp_path)
    config = cfg()
    seed_policy_dataset(db)
    payload = EvolutionScore(config, db).build(hours=24)
    assert "policy_quality" in payload
    assert payload["go_live_gates"]["live_allowed"] is False
    plan = TrainingSummary(config, db).acceleration_plan(hours=24)
    assert "catalyst-summary --hours 24" in plan or "final_recommendation: NO LIVE" in plan


def test_policy_news_smoke_test_passes(tmp_path):
    db = make_db(tmp_path)
    text = PolicyNewsSmokeTest(cfg(edge_guard_min_sample=20), db, DummyLogger()).to_text()
    assert "POLICY NEWS SMOKE TEST START" in text
    assert "result: PASS" in text
    assert "opened_paper_trades: 0" in text


def test_new_research_lab_commands_exist():
    text = (PROJECT_ROOT / "app" / "research_lab.py").read_text(encoding="utf-8")
    for command in ("catalyst-summary", "news-risk-gate", "paper-policy-lab", "walk-forward", "policy-backtest", "policy-news-smoke-test"):
        assert f'"{command}"' in text


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _get(url: str) -> tuple[int, str]:
    with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310 - local test server
        return int(response.status), response.read().decode("utf-8")


def test_dashboard_policy_news_endpoints(tmp_path):
    db = make_db(tmp_path)
    config = cfg()
    seed_policy_dataset(db)
    port = _free_port()
    start_health_server(
        HealthState(mode=config.mode),
        port,
        DummyLogger(),
        config=config,
        db=db,
        training_pulse=TrainingPulse(),
        telegram_notifier=TelegramNotifier(config, DummyLogger()),
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(30):
        try:
            _get(base + "/health")
            break
        except Exception:
            time.sleep(0.05)
    for path, marker in (
        ("/api/training/catalyst-summary?hours=24", "CATALYST SUMMARY START"),
        ("/api/training/news-risk-gate?hours=24", "NEWS RISK GATE START"),
        ("/api/training/paper-policy-lab?hours=24", "PAPER POLICY LAB START"),
        ("/api/training/walk-forward?hours=24", "WALK FORWARD VALIDATION START"),
        ("/api/training/policy-backtest?hours=24", "POLICY BACKTEST START"),
    ):
        status, body = _get(base + path)
        assert status == 200
        assert marker in json.loads(body)["text"]
        assert "BITGET_API_KEY" not in body
    assert _get(base + "/health")[0] == 200
