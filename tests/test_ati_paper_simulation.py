from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.labs.ati_paper import ACCOUNT_ID, safety_envelope
from app.labs.ati_paper.api import API_READERS, account_payload, api_payload, performance_payload
from app.labs.ati_paper.broker import AtiPaperBroker, decide_bar_exit
from app.labs.ati_paper.config import InstrumentRule, load_config
from app.labs.ati_paper.executor import AtiPaperExecutor
from app.labs.ati_paper.ledger import AtiPaperLedger
from app.labs.ati_paper.incident_migration import (
    CONFIRMATION, archive_and_restore_causal_incident,
)
from app.labs.ati_paper.public_market import (
    AtiPublicMarketError,
    MarketBar,
    MarketTick,
    _assert_public_get,
)
from app.labs import research_dashboard_v10_43c as DASH


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _signal(signal_id: str, *, direction: str = "LONG", invalidation: float | None = None) -> dict:
    return {
        "signal_id": signal_id,
        "setup_id": "ATI_TEST_SETUP",
        "setup_variant": "fixture",
        "symbol": "BTCUSDT",
        "direction": direction,
        "decision_ts": (_now() - timedelta(seconds=5)).isoformat(),
        "decision": "SHADOW_CANDIDATE",
        "exact_trigger": True,
        "invalidation_level": invalidation if invalidation is not None else (98.0 if direction == "LONG" else 102.0),
        "ati_score": 6,
        "score_components": {"fixture": 1},
        "policy_version": "ATI_SHADOW_POLICY_V2",
        "feature_version": "ATI_FEATURES_V2",
        "paper_feed_eligible": True,
    }


def _tick(price: float = 100.0) -> MarketTick:
    now = _now()
    return MarketTick("BTCUSDT", price, int(now.timestamp() * 1000), now.isoformat())


def _rule(step: float = 0.000001) -> InstrumentRule:
    return InstrumentRule("BTCUSDT", step, 1.0, step, 6, 2, "TEST_PUBLIC_RULE")


def _ledger(tmp_path: Path, *, fraction: float = 1.0, trailing: bool = False):
    cfg = dataclasses.replace(load_config(), position_fraction=fraction, trailing_enabled=trailing)
    ledger = AtiPaperLedger(tmp_path / "ati.sqlite")
    ledger.initialize(cfg, commit_hash="test")
    return cfg, ledger, AtiPaperBroker(ledger, cfg, commit_hash="test")


def _observe(ledger: AtiPaperLedger, signal_id: str, *, direction: str = "LONG", invalidation=None):
    ledger.observe_signal(
        _signal(signal_id, direction=direction, invalidation=invalidation),
        observed_at=(_now() - timedelta(seconds=2)).isoformat(), commit_hash="test",
    )


def _open(tmp_path: Path, *, fraction: float = 1.0, direction: str = "LONG", signal_id="s1", trailing=False):
    cfg, ledger, broker = _ledger(tmp_path, fraction=fraction, trailing=trailing)
    _observe(ledger, signal_id, direction=direction)
    opened = broker.open_from_signal(signal_id, _tick(), _rule())
    return cfg, ledger, broker, opened


def test_safety_envelope_is_simulation_only():
    safety = safety_envelope()
    assert safety["simulation_only"] is True
    assert safety["paper_trading"] is True
    assert safety["live_trading"] is False
    assert safety["paper_filter_enabled"] is False
    assert safety["can_send_real_orders"] is False
    assert safety["final_recommendation"] == "NO LIVE"


def test_first_start_credits_50_once_and_restart_does_not_recredit(tmp_path):
    cfg = load_config()
    ledger = AtiPaperLedger(tmp_path / "paper.sqlite")
    first = ledger.initialize(cfg)
    second = ledger.initialize(cfg)
    assert first["created"] is True and second["created"] is False
    account = ledger.account()
    assert account["account_id"] == ACCOUNT_ID
    assert account["initial_balance"] == account["cash_balance"] == 50.0
    events = ledger.rows("events", limit=20)
    assert sum(row["event_type"] == "ACCOUNT_CREATED" for row in events) == 1
    assert any(row["reason"] == "NO_RECREDIT" for row in events)


def test_frozen_ati_v2_policy_is_required(tmp_path):
    _, ledger, _ = _ledger(tmp_path)
    bad = _signal("bad")
    bad["policy_version"] = "CHANGED_POLICY"
    with pytest.raises(ValueError, match="SOURCE_POLICY_NOT_FROZEN"):
        ledger.observe_signal(bad)


@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
def test_open_and_manual_close_are_symmetric_and_reconcile(tmp_path, direction):
    _, ledger, broker, opened = _open(tmp_path, direction=direction)
    exit_price = 101.0 if direction == "LONG" else 99.0
    closed = broker.close_position(
        opened["position_id"], event_type="SIM_MANUAL_RESEARCH_CLOSE",
        exit_reason="TEST", reference_price=exit_price, source_ts=(_now() + timedelta(minutes=2)).isoformat(),
    )
    assert closed["net_pnl"] > 0
    assert ledger.reconcile()["status"] == "PASS"
    trade = ledger.rows("trades", limit=1)[0]
    assert trade["gross_pnl"] > trade["net_pnl"]
    assert trade["fees"] > 0 and trade["slippage"] > 0
    assert trade["funding_status"] == "UNKNOWN"


def test_gain_and_loss_change_next_notional_with_realized_equity(tmp_path):
    cfg, ledger, broker = _ledger(tmp_path, fraction=0.5)
    notionals = []
    for signal_id, exit_price in (("win", 110.0), ("loss", 90.0)):
        _observe(ledger, signal_id)
        opened = broker.open_from_signal(signal_id, _tick(), _rule())
        notionals.append(opened["notional"])
        broker.close_position(
            opened["position_id"], event_type="SIM_MANUAL_RESEARCH_CLOSE",
            exit_reason="TEST", reference_price=exit_price,
            source_ts=(_now() + timedelta(minutes=2)).isoformat(),
        )
    _observe(ledger, "after-loss")
    third = broker.open_from_signal("after-loss", _tick(), _rule())
    assert notionals[1] > notionals[0]
    assert third["notional"] < notionals[1]
    assert third["configured_sizing_fraction"] == cfg.position_fraction == 0.5
    assert ledger.reconcile()["status"] == "PASS"


def test_unrealized_profit_is_not_used_for_new_position_sizing(tmp_path):
    cfg, ledger, broker = _ledger(tmp_path, fraction=0.4)
    _observe(ledger, "first")
    first = broker.open_from_signal("first", _tick(), _rule())
    broker.mark_tick(first["position_id"], _tick(120.0))
    account = ledger.account()
    assert account["total_equity"] > account["realized_equity"]
    _observe(ledger, "second")
    second = broker.open_from_signal("second", _tick(), _rule())
    expected = account["realized_equity"] * cfg.position_fraction
    assert second["notional"] == pytest.approx(expected, rel=0, abs=0.001)


def test_gap_invalidated_signal_is_rejected_without_position(tmp_path):
    _, ledger, broker = _ledger(tmp_path)
    _observe(ledger, "gap", invalidation=99.0)
    with pytest.raises(ValueError, match="GAP_INVALIDATED"):
        broker.open_from_signal("gap", _tick(98.0), _rule())
    assert ledger.open_positions() == []


def test_quantity_step_and_minimum_notional_are_enforced(tmp_path):
    _, ledger, broker = _ledger(tmp_path)
    _observe(ledger, "step")
    opened = broker.open_from_signal("step", _tick(100.0), _rule(step=0.03))
    assert opened["quantity"] / 0.03 == pytest.approx(round(opened["quantity"] / 0.03))
    _, ledger2, broker2 = _ledger(tmp_path / "other")
    _observe(ledger2, "minimum")
    with pytest.raises(ValueError, match="MINIMUM"):
        broker2.open_from_signal("minimum", _tick(100.0), InstrumentRule("BTCUSDT", 1, 1000, 1, 0, 2, "TEST"))


def test_same_bar_stop_and_target_is_stop_before_tp_for_both_sides():
    bar = MarketBar("BTCUSDT", 60_000, 120_000, 100, 103, 97, 100, 1)
    for direction, stop, target in (("LONG", 98, 103), ("SHORT", 102, 97)):
        decision = decide_bar_exit({
            "direction": direction, "stop_price": stop,
            "take_profit_price": target, "trailing_stop": None,
        }, bar)
        assert decision.exit_reason == "STOP_BEFORE_TP"
        assert decision.ambiguity_rule == "STOP_BEFORE_TP"


def test_gap_stop_is_filled_at_adverse_open_but_favorable_tp_is_capped():
    long = {"direction": "LONG", "stop_price": 98, "take_profit_price": 103, "trailing_stop": None}
    assert decide_bar_exit(long, MarketBar("BTCUSDT", 1, 2, 95, 96, 94, 95, 1)).reference_price == 95
    assert decide_bar_exit(long, MarketBar("BTCUSDT", 1, 2, 106, 107, 105, 106, 1)).reference_price == 103


def test_trailing_activates_after_bar_and_cannot_close_on_activation_bar(tmp_path):
    _, ledger, broker, opened = _open(tmp_path, fraction=0.5, trailing=True)
    position = ledger.open_positions()[0]
    entry_ms = int(datetime.fromisoformat(position["entry_source_ts"]).timestamp() * 1000)
    bar_ms = ((entry_ms + 59_999) // 60_000) * 60_000
    first = MarketBar("BTCUSDT", bar_ms, bar_ms + 60_000, 100, 102.5, 99, 102, 1)
    result = broker.process_closed_bar(opened["position_id"], first)
    assert result["status"] == "OPEN"
    assert result["trailing_stop"] is not None
    second = MarketBar("BTCUSDT", bar_ms + 60_000, bar_ms + 120_000, 102, 102.5, 100.5, 101, 1)
    closed = broker.process_closed_bar(opened["position_id"], second)
    assert closed["status"] == "ATI_PAPER_POSITION_CLOSED"
    assert closed["exit_reason"] == "TRAIL"


def test_time_exit_uses_closed_bar_after_configured_horizon(tmp_path):
    cfg, ledger, broker, opened = _open(tmp_path, fraction=0.5)
    position = ledger.open_positions()[0]
    entry = datetime.fromisoformat(position["entry_source_ts"])
    ts = int((entry + timedelta(minutes=cfg.max_holding_minutes)).timestamp() // 60 * 60_000)
    bar = MarketBar("BTCUSDT", ts, ts + 60_000, 100, 101, 99, 100.2, 1)
    result = broker.process_closed_bar(opened["position_id"], bar)
    assert result["exit_reason"] == "TIME"


def test_open_position_and_balance_survive_ledger_restart(tmp_path):
    _, ledger, _, opened = _open(tmp_path, fraction=0.5)
    before = ledger.account()
    restarted = AtiPaperLedger(ledger.db_path)
    restarted.initialize(load_config())
    assert restarted.account()["realized_equity"] == before["realized_equity"]
    assert restarted.open_positions()[0]["position_id"] == opened["position_id"]
    assert restarted.reconcile()["status"] == "PASS"


def test_open_is_atomic_when_event_write_fails(tmp_path, monkeypatch):
    _, ledger, broker = _ledger(tmp_path)
    _observe(ledger, "atomic")
    monkeypatch.setattr(ledger, "_event", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError, match="boom"):
        broker.open_from_signal("atomic", _tick(), _rule())
    assert ledger.open_positions() == []
    assert ledger.account()["cash_balance"] == 50.0


def test_public_market_allowlist_is_exact_and_auth_free():
    _assert_public_get("https://api.bitget.com/api/v2/mix/market/ticker", {"symbol": "BTCUSDT"}, {})
    with pytest.raises(AtiPublicMarketError):
        _assert_public_get("https://api.bitget.com/api/v2/mix/order/place-order", {}, {})
    with pytest.raises(AtiPublicMarketError):
        _assert_public_get("https://api.bitget.com/api/v2/mix/market/ticker", {"signature": "fake"}, {})
    with pytest.raises(AtiPublicMarketError):
        _assert_public_get("https://api.bitget.com/api/v2/mix/market/ticker", {}, {"ACCESS-KEY": "fake"})


class _FakeMarket:
    def ticker(self, symbol):
        return _tick()

    def instrument_rule(self, symbol):
        return _rule()

    def closed_bars(self, *args, **kwargs):
        return []


def test_executor_rejects_preexisting_signal_but_accepts_new_live_signal(tmp_path):
    signal_path = tmp_path / "signals.jsonl"
    signal_path.write_text(json.dumps(_signal("old")) + "\n", encoding="utf-8")
    cfg = dataclasses.replace(load_config(), position_fraction=0.5)
    ledger = AtiPaperLedger(tmp_path / "executor.sqlite")
    executor = AtiPaperExecutor(
        config=cfg, ledger=ledger, market=_FakeMarket(), signal_path=signal_path,
        status_path=tmp_path / "status.json", commit_hash="test",
    )
    executor.initialize()
    assert ledger.signal("old")["status"] == "ATI_SIGNAL_REJECTED"
    signal_path.write_text(
        json.dumps(_signal("old")) + "\n" + json.dumps(_signal("new")) + "\n",
        encoding="utf-8",
    )
    status = executor.cycle_once()
    assert status["status"] == "HEALTHY"
    assert ledger.signal("new")["status"] == "ATI_PAPER_POSITION_OPEN"


def test_executor_blocks_signal_when_outcome_is_already_known(tmp_path):
    signal_path = tmp_path / "signals.jsonl"
    outcome_path = tmp_path / "outcomes.jsonl"
    signal_path.write_text("", encoding="utf-8")
    outcome_path.write_text("", encoding="utf-8")
    cfg = dataclasses.replace(load_config(), position_fraction=0.5)
    ledger = AtiPaperLedger(tmp_path / "executor.sqlite")
    executor = AtiPaperExecutor(
        config=cfg, ledger=ledger, market=_FakeMarket(), signal_path=signal_path,
        outcome_path=outcome_path, status_path=tmp_path / "status.json",
        commit_hash="test",
    )
    executor.initialize()
    signal_path.write_text(json.dumps(_signal("known")) + "\n", encoding="utf-8")
    outcome_path.write_text(json.dumps({"signal_id": "known"}) + "\n", encoding="utf-8")
    executor.cycle_once()
    assert ledger.signal("known")["status"] == "ATI_SIGNAL_REJECTED"
    assert ledger.signal("known")["rejection_reason"] == "PREKNOWN_OUTCOME_NOT_FORWARD_ELIGIBLE"
    assert ledger.open_positions() == []
    assert ledger.rows("trades") == []


def test_executor_and_broker_both_block_stale_signal_decisions(tmp_path):
    signal_path = tmp_path / "signals.jsonl"
    signal_path.write_text("", encoding="utf-8")
    cfg = dataclasses.replace(load_config(), position_fraction=0.5, signal_max_age_seconds=60)
    ledger = AtiPaperLedger(tmp_path / "executor.sqlite")
    executor = AtiPaperExecutor(
        config=cfg, ledger=ledger, market=_FakeMarket(), signal_path=signal_path,
        outcome_path=tmp_path / "outcomes.jsonl", status_path=tmp_path / "status.json",
        commit_hash="test",
    )
    executor.initialize()
    stale = _signal("stale")
    stale["decision_ts"] = (_now() - timedelta(minutes=5)).isoformat()
    signal_path.write_text(json.dumps(stale) + "\n", encoding="utf-8")
    executor.cycle_once()
    assert ledger.signal("stale")["rejection_reason"] == "SIGNAL_DECISION_STALE_AT_OBSERVATION"

    manual = _signal("broker-stale")
    manual["decision_ts"] = (_now() - timedelta(minutes=5)).isoformat()
    ledger.observe_signal(manual, observed_at=(_now() - timedelta(seconds=2)).isoformat())
    broker = AtiPaperBroker(ledger, cfg, commit_hash="test")
    with pytest.raises(ValueError, match="SIGNAL_DECISION_STALE_AT_ENTRY"):
        broker.open_from_signal("broker-stale", _tick(), _rule())


def test_causal_incident_is_archived_before_transactional_ledger_restore(tmp_path):
    _, ledger, broker, opened = _open(tmp_path, fraction=0.5, signal_id="contaminated")
    broker.close_position(
        opened["position_id"], event_type="SIM_MANUAL_RESEARCH_CLOSE",
        exit_reason="TEST", reference_price=101.0,
        source_ts=(_now() + timedelta(minutes=2)).isoformat(),
    )
    before = ledger.rows("trades", limit=1)[0]
    result = archive_and_restore_causal_incident(
        "contaminated", confirmation=CONFIRMATION, db_path=ledger.db_path,
        qa_root=tmp_path / "qa", status_path=tmp_path / "missing-status.json",
        evidence_paths=[], commit_hash="test",
    )
    assert result["status"] == "QA_ARCHIVED_AND_PRODUCTIVE_LEDGER_RESTORED"
    assert result["reconciliation"]["status"] == "PASS"
    assert result["account"]["cash_balance"] == pytest.approx(50.0)
    assert ledger.rows("trades") == []
    assert ledger.open_positions() == []
    assert ledger.signal("contaminated")["status"] == "ATI_SIGNAL_REJECTED"
    assert any(
        row["event_type"] == "ATI_PAPER_CAUSAL_INCIDENT_MIGRATED_TO_QA"
        for row in ledger.rows("events")
    )
    archive = Path(result["qa_archive_db"])
    assert archive.is_file()
    archived = AtiPaperLedger(archive)
    assert archived.rows("trades", limit=1)[0]["trade_id"] == before["trade_id"]


def test_api_is_read_only_and_reports_sample_warning(tmp_path):
    _, ledger, _, _ = _open(tmp_path, fraction=0.5)
    account = account_payload(ledger)
    perf = performance_payload(ledger)
    assert account["account"]["initial_balance"] == 50.0
    assert account["sizing"]["uses_unrealized_pnl"] is False
    assert perf["sample_size_warning"] is True
    assert set(API_READERS) == {
        "/api/ati-paper/account", "/api/ati-paper/positions", "/api/ati-paper/trades",
        "/api/ati-paper/equity", "/api/ati-paper/events", "/api/ati-paper/signals",
        "/api/ati-paper/health", "/api/ati-paper/chart", "/api/ati-paper/performance",
    }
    assert api_payload("/api/ati-paper/reset")[1] == 404
    assert api_payload("/api/ati-paper/order")[1] == 404


def test_ati_paper_modules_have_no_private_execution_dependencies():
    root = Path(__file__).resolve().parents[1] / "app" / "labs" / "ati_paper"
    productive = "\n".join(
        path.read_text(encoding="utf-8") for path in root.glob("*.py")
    )
    forbidden = (
        "private_get(", "private_post(", "place_order(", "set_leverage(",
        "set_margin_mode(", "ExecutionEngine.execute", "PaperTrader.open_position",
        "can_send_real_orders=True", "LIVE_TRADING=True", "ENABLE_PAPER_POLICY_FILTER=True",
    )
    assert all(token not in productive for token in forbidden)
    assert "app.paper_trader" not in productive
    assert "app.execution_engine" not in productive


def test_dashboard_has_one_dynamic_ati_paper_panel_and_no_mutating_routes(tmp_path):
    state = {
        "tool_version": "v10.43c", "symbol": "BTCUSDT", "generated_at": "now",
        "git_head": "test", "health": {}, "view": {}, "data_quality": {},
        "shadow": None, "scoreboard": [], "bankroll": None, "ws_dataset": {},
        "persistent_health": {}, "persistent_continuity": {}, "source_compare_3way": {},
        "strategy_hardening": {}, "ws_persistent_tournament": {}, "exit_optimization": {},
        "readiness_v1043c": {"primary": "RESEARCH_ONLY_NOT_ACTIONABLE", "states": []},
        "ati_paper": {"account": {"account": None}, "health": {"status": "WAITING_FOR_SIGNAL"},
                      "performance": {"total_trades": 0}},
    }
    result = DASH.build_dashboard("BTCUSDT", state=state, out_dir=tmp_path, write=False)
    page = result["html_str"]
    assert page.count('id="atiPaperPanel"') == 1
    assert page.count('id="atiPaperPositions"') == 1
    assert page.count('id="atiPaperTrades"') == 1
    assert "setInterval(refresh,5000)" in page
    assert "SIMULATION ONLY" in page and "NO LIVE" in page
    assert "/api/ati-paper/reset" not in page
    assert "/api/ati-paper/order" not in page


def test_local_stack_scripts_are_closed_scope_and_research_only():
    root = Path(__file__).resolve().parents[1]
    names = (
        "start_local_stack.ps1", "stop_local_stack.ps1", "restart_local_stack.ps1",
        "status_local_stack.ps1", "run_ati_paper_forever.ps1",
        "run_research_server.ps1", "run_dashboard_watcher_forever.ps1",
        "run_heavy_research_scheduler.ps1", "local_stack_common.ps1",
    )
    combined = "\n".join((root / "scripts" / name).read_text(encoding="utf-8") for name in names)
    for name in names:
        assert (root / "scripts" / name).is_file()
    forbidden = (
        "place_order", "private_get", "private_post", "set_leverage", "set_margin_mode",
        "ExecutionEngine.execute", "PaperTrader.open_position", "LIVE_TRADING=True",
        "ENABLE_PAPER_POLICY_FILTER=True", "can_send_real_orders=True", "ssh ", "tmux",
    )
    assert all(token not in combined for token in forbidden)
    assert "SAFE_PAPER_ONLY" in combined
    assert "NO LIVE" in combined
    assert "collect_bybit_trades_ws_forever.ps1" not in (
        root / "scripts" / "start_local_stack.ps1"
    ).read_text(encoding="utf-8")
    status_script = (root / "scripts" / "status_local_stack.ps1").read_text(encoding="utf-8")
    for field in ("command_line", "uptime_seconds", "memory_mb", "cpu_seconds",
                  "listening_ports", "last_log_line", "artifact_age_seconds"):
        assert field in status_script
    for wrapper in ("run_ati_paper_forever.ps1", "run_dashboard_watcher_forever.ps1",
                    "run_research_server.ps1", "run_heavy_research_scheduler.ps1"):
        text = (root / "scripts" / wrapper).read_text(encoding="utf-8")
        assert "Tee-Object" in text
        assert "data\\runtime\\local_stack\\logs" in text
    scheduler = (root / "scripts" / "run_heavy_research_scheduler.ps1").read_text(encoding="utf-8")
    assert "next_run_at" in scheduler
    assert "Previous heavy refresh is current" in scheduler
