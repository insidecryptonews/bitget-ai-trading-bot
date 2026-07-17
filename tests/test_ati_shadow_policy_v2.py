from __future__ import annotations

import ast
import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app.labs.ati.contracts import EXPECTED_RULES, contract_receipt, load_policy, load_priors
from app.labs.ati.features import AtiDataError, build_feature_frame, canonicalize_ohlcv, resample_closed
from app.labs.ati.levels import CausalLevelEngine, LevelSnapshot, PriceLevel, level_snapshot
from app.labs.ati.metrics import chronological_validation, summarize_trades
from app.labs.ati.replay import AtiCostModel, replay_candidates, simulate_trade
from app.labs.ati import report as ati_report
from app.labs.ati import shadow_engine as ati_shadow
from app.labs.ati.report import _atomic_write, _safe_output_dir, run_historical_replay
from app.labs.ati.rules import evaluate_rules_at
from app.labs.ati.shadow_engine import _closed_forward_trades, _latest_available_at, _merge_unique
from app.research_lab import PUBLIC_RESEARCH_ONLY_COMMANDS, build_argument_parser


def _raw_minutes(count: int = 300, *, symbol: str = "BTCUSDT") -> pd.DataFrame:
    ts = pd.date_range("2026-01-01", periods=count, freq="1min", tz="UTC")
    close = 100 + np.sin(np.arange(count) / 20) * 0.5
    return pd.DataFrame({
        "timestamp": ts,
        "open": close,
        "high": close + 0.2,
        "low": close - 0.2,
        "close": close,
        "volume": np.ones(count),
        "symbol": symbol,
        "timeframe": "1m",
    })


def _rule_frame() -> pd.DataFrame:
    ts = pd.date_range("2026-01-01", periods=8, freq="15min", tz="UTC")
    frame = pd.DataFrame({
        "timestamp": ts,
        "available_at": ts + pd.Timedelta(minutes=15),
        "open": [100, 100, 100, 100, 101, 103, 103, 103],
        "high": [101, 101, 101, 101, 104, 104, 104, 104],
        "low": [99, 99, 99, 99, 99.5, 102, 102, 102],
        "close": [100, 100, 100, 100, 103, 103, 103, 103],
        "volume": [1] * 8,
        "atr14": [1.0] * 8,
        "body_strength": [0.6] * 8,
        "regime": ["TREND_UP"] * 8,
        "volatility_regime": ["NORMAL_VOL"] * 8,
        "h1_regime": ["TREND_UP"] * 8,
        "h4_regime": ["TREND_UP"] * 8,
        "h1_volatility_regime": ["NORMAL_VOL"] * 8,
        "h4_volatility_regime": ["NORMAL_VOL"] * 8,
        "h1_close": [103] * 8,
        "h4_close": [103] * 8,
        "h1_ema20": [102] * 8,
        "h1_ema50": [101] * 8,
        "h4_ema20": [102] * 8,
        "h4_ema50": [101] * 8,
        "feature_ready": [True] * 8,
    })
    return frame


def _level(kind: str, price: float, *, fatigue: bool = False) -> PriceLevel:
    return PriceLevel(
        kind=kind, price=price, touch_count=2, touch_indices=(0, 3),
        first_touch_at="2026-01-01T00:00:00+00:00",
        last_touch_at="2026-01-01T00:45:00+00:00",
        tolerance=0.2, strength=2.0, fatigue=fatigue,
    )


def test_contract_is_frozen_and_never_actionable() -> None:
    policy = load_policy()
    priors = load_priors()
    receipt = contract_receipt()
    assert tuple(rule["rule_id"] for rule in policy["rules"]) == EXPECTED_RULES
    assert policy["can_send_real_orders"] is False
    assert policy["paper_filter_enabled"] is False
    assert policy["promotion_gate"]["current_decision"] == "NO_LIVE"
    assert priors["status"] == "research_prior_only"
    assert receipt["final_recommendation"] == "NO LIVE"
    assert receipt["paper_ready"] is False and receipt["live_ready"] is False


def test_resample_drops_incomplete_bucket_and_future_mutation_is_invariant() -> None:
    raw = _raw_minutes(16)
    first = resample_closed(raw, "15m", source_timeframe="1m",
                            as_of=pd.Timestamp("2026-01-01T00:15:00Z"))
    mutated = raw.copy()
    mutated.loc[15, ["open", "high", "low", "close"]] = [900, 901, 899, 900]
    second = resample_closed(mutated, "15m", source_timeframe="1m",
                             as_of=pd.Timestamp("2026-01-01T00:15:00Z"))
    assert len(first) == 1
    pd.testing.assert_frame_equal(first, second)


def test_higher_timeframe_incomplete_candle_is_not_joined() -> None:
    raw = _raw_minutes(239)
    features = build_feature_frame(raw)
    assert len(features) > 0
    assert features["h4_close"].isna().all()


def test_pivot_is_unavailable_until_right_confirmation_bars_close() -> None:
    ts = pd.date_range("2026-01-01", periods=15, freq="15min", tz="UTC")
    frame = pd.DataFrame({
        "timestamp": ts, "close": [100] * 15, "atr14": [1] * 15,
        "low": [99, 99, 99, 99, 99, 90, 99, 99, 99, 99, 99, 99, 99, 99, 99],
        "high": [101] * 15,
    })
    early = level_snapshot(frame, 7, min_touches=1)
    confirmed = level_snapshot(frame, 8, min_touches=1)
    assert early.support is None or early.support.price != 90
    assert confirmed.support is not None and confirmed.support.price == 90


def test_precomputed_level_engine_matches_reference_and_ignores_future_mutation() -> None:
    ts = pd.date_range("2026-01-01", periods=180, freq="15min", tz="UTC")
    center = 100 + np.sin(np.arange(180) / 7) * 3
    frame = pd.DataFrame({
        "timestamp": ts,
        "close": center,
        "atr14": np.full(180, 1.25),
        "low": center - (0.5 + np.cos(np.arange(180) / 5) * 0.1),
        "high": center + (0.5 + np.sin(np.arange(180) / 5) * 0.1),
    })
    engine = CausalLevelEngine(frame)
    for idx in (12, 48, 96, 120, 150):
        assert engine.snapshot(frame, idx) == level_snapshot(frame, idx)

    cutoff = 120
    mutated = frame.copy()
    mutated.loc[cutoff + 1:, ["low", "high", "close"]] = [1.0, 10_000.0, 5_000.0]
    mutated_engine = CausalLevelEngine(mutated)
    assert mutated_engine.snapshot(mutated, cutoff) == engine.snapshot(frame, cutoff)


def test_long_r1_requires_post_break_hold_or_retest_and_enters_next_open() -> None:
    frame = _rule_frame()
    frame.loc[3, ["open", "high", "low", "close"]] = [100, 103, 99, 102]
    resistance = _level("RESISTANCE", 101)

    def snapshots(_frame, idx):
        return LevelSnapshot(None, resistance if idx == 2 else None, 0.2, 0.1, idx)

    rows = evaluate_rules_at(frame, 4, symbol="BTCUSDT",
                             dataset_source="fixture", snapshot_fn=snapshots)
    long_r1 = next(row for row in rows if row.setup_id == "LONG_R1")
    assert long_r1.exact_trigger is True
    assert long_r1.setup_variant == "RETEST"
    assert long_r1.decision == "SHADOW_CANDIDATE"
    assert long_r1.entry_ts == pd.Timestamp(frame.iloc[5]["timestamp"]).isoformat()
    assert long_r1.entry_price == frame.iloc[5]["open"]


def test_candidate_gapping_through_invalidation_is_rejected_before_replay() -> None:
    frame = _rule_frame()
    frame.loc[3, ["open", "high", "low", "close"]] = [100, 103, 99, 102]
    frame.loc[5, "open"] = 100
    resistance = _level("RESISTANCE", 101)

    def snapshots(_frame, idx):
        return LevelSnapshot(None, resistance if idx == 2 else None, 0.2, 0.1, idx)

    rows = evaluate_rules_at(frame, 4, symbol="BTCUSDT",
                             dataset_source="fixture", snapshot_fn=snapshots)
    long_r1 = next(row for row in rows if row.setup_id == "LONG_R1")
    assert long_r1.exact_trigger is True
    assert long_r1.decision == "REJECT_INVALIDATED_BEFORE_ENTRY"
    assert simulate_trade(frame, long_r1.to_dict()) is None


def test_short_r1_exact_rejection_is_symmetric_and_causal() -> None:
    frame = _rule_frame()
    frame.loc[4, ["open", "high", "low", "close"]] = [101, 101, 97, 98]
    frame.loc[5, ["open", "high", "low", "close"]] = [99, 100, 98, 99]
    frame.loc[3, "low"] = 99
    frame.loc[:, ["h1_regime", "h4_regime", "regime"]] = "TREND_DOWN"
    frame.loc[:, "h1_close"] = 98
    resistance = _level("RESISTANCE", 101)

    def snapshots(_frame, idx):
        return LevelSnapshot(None, resistance if idx == 4 else None, 0.2, 0.1, idx)

    before = evaluate_rules_at(frame, 4, symbol="BTCUSDT",
                               dataset_source="fixture", snapshot_fn=snapshots)
    mutated = frame.copy()
    mutated.loc[6:, ["open", "high", "low", "close"]] = 1_000_000
    after = evaluate_rules_at(mutated, 4, symbol="BTCUSDT",
                              dataset_source="fixture", snapshot_fn=snapshots)
    short_before = next(row for row in before if row.setup_id == "SHORT_R1")
    short_after = next(row for row in after if row.setup_id == "SHORT_R1")
    assert short_before.exact_trigger and short_before.decision == "SHADOW_CANDIDATE"
    assert short_before.to_dict() == short_after.to_dict()


def test_short_s1_requires_break_then_non_recovery() -> None:
    frame = _rule_frame()
    frame.loc[3, ["open", "high", "low", "close"]] = [101, 101, 97.5, 98]
    frame.loc[4, ["open", "high", "low", "close"]] = [98, 99, 96.5, 97]
    frame.loc[5, ["open", "high", "low", "close"]] = [99, 100, 98, 99]
    frame.loc[:, ["h1_regime", "h4_regime", "regime"]] = "TREND_DOWN"
    support = _level("SUPPORT", 100, fatigue=True)

    def snapshots(_frame, idx):
        return LevelSnapshot(support if idx == 2 else None, None, 0.2, 0.1, idx)

    rows = evaluate_rules_at(frame, 4, symbol="BTCUSDT",
                             dataset_source="fixture", snapshot_fn=snapshots)
    short_s1 = next(row for row in rows if row.setup_id == "SHORT_S1")
    assert short_s1.exact_trigger is True
    assert short_s1.decision == "SHADOW_CANDIDATE"
    assert short_s1.entry_price == frame.iloc[5]["open"]


def test_long_s1_requires_two_non_lower_lows_and_reclaim() -> None:
    frame = _rule_frame()
    frame.loc[2, ["open", "high", "low", "close"]] = [100, 101, 99, 100]
    frame.loc[3, ["open", "high", "low", "close"]] = [100, 101, 99, 100]
    frame.loc[4, ["open", "high", "low", "close"]] = [100, 102, 100, 102]
    support = _level("SUPPORT", 100)

    def snapshots(_frame, idx):
        return LevelSnapshot(support if idx == 1 else None, None, 0.2, 0.1, idx)

    rows = evaluate_rules_at(frame, 4, symbol="BTCUSDT",
                             dataset_source="fixture", snapshot_fn=snapshots)
    long_s1 = next(row for row in rows if row.setup_id == "LONG_S1")
    assert long_s1.exact_trigger is True
    assert long_s1.decision == "SHADOW_CANDIDATE"
    assert long_s1.entry_price == frame.iloc[5]["open"]


def test_same_bar_tp_and_sl_is_stop_before_tp_for_long_and_short() -> None:
    ts = pd.date_range("2026-01-01", periods=4, freq="15min", tz="UTC")
    frame = pd.DataFrame({
        "timestamp": ts, "available_at": ts + pd.Timedelta(minutes=15),
        "open": [100, 100, 100, 100], "high": [100, 102, 102, 100],
        "low": [100, 98, 98, 100], "close": [100, 100, 100, 100],
    })
    base = {
        "signal_id": "x", "setup_id": "X", "setup_variant": "X",
        "symbol": "BTCUSDT", "signal_idx": 0, "decision_ts": ts[0].isoformat(),
        "decision": "SHADOW_CANDIDATE", "regime": "RANGE", "atr15": 1.0,
    }
    long = simulate_trade(frame, {**base, "direction": "LONG", "invalidation_level": 99})
    short = simulate_trade(frame, {**base, "signal_id": "y", "direction": "SHORT", "invalidation_level": 101})
    assert long and short
    assert long["exit_reason"] == short["exit_reason"] == "STOP_BEFORE_TP"
    assert long["ambiguity_rule"] == short["ambiguity_rule"] == "STOP_BEFORE_TP"
    assert long["mfe"] == short["mfe"] == 0.0
    assert long["mae"] == short["mae"] == pytest.approx(0.01)


def test_flat_trade_is_negative_after_explicit_costs() -> None:
    ts = pd.date_range("2026-01-01", periods=3, freq="15min", tz="UTC")
    frame = pd.DataFrame({
        "timestamp": ts, "available_at": ts + pd.Timedelta(minutes=15),
        "open": [100] * 3, "high": [100.1] * 3, "low": [99.9] * 3, "close": [100] * 3,
    })
    candidate = {
        "signal_id": "flat", "setup_id": "X", "setup_variant": "X",
        "symbol": "BTCUSDT", "signal_idx": 0, "decision_ts": ts[0].isoformat(),
        "decision": "SHADOW_CANDIDATE", "regime": "RANGE", "atr15": 2.0,
        "direction": "LONG", "invalidation_level": 98,
    }
    model = AtiCostModel()
    result = simulate_trade(frame, candidate, costs=model, max_holding_bars=1)
    assert result is not None and result["gross_return"] == 0
    assert result["net_return"] < 0
    assert result["net_return"] == pytest.approx(-model.entry_exit_cost_fraction - result["funding_fraction"])


def test_trailing_activation_cannot_fill_retroactively_on_same_bar() -> None:
    ts = pd.date_range("2026-01-01", periods=4, freq="15min", tz="UTC")
    frame = pd.DataFrame({
        "timestamp": ts, "available_at": ts + pd.Timedelta(minutes=15),
        "open": [100, 100, 101.8, 101.8], "high": [100, 102, 102, 102],
        "low": [100, 99.8, 101.0, 101.7], "close": [100, 101.8, 101.2, 101.8],
    })
    candidate = {
        "signal_id": "trail", "setup_id": "X", "setup_variant": "X",
        "symbol": "BTCUSDT", "signal_idx": 0, "decision_ts": ts[0].isoformat(),
        "decision": "SHADOW_CANDIDATE", "regime": "RANGE", "atr15": 4.0,
        "direction": "LONG", "invalidation_level": 96,
    }
    result = simulate_trade(frame, candidate, max_holding_bars=3,
                            trailing_activation=0.01, trailing_distance=0.005,
                            policy_name="trail_test")
    assert result is not None
    assert result["exit_reason"] == "TRAIL"
    assert result["exit_idx"] == 2  # not the activation bar at index 1


def test_incomplete_horizon_is_not_a_closed_outcome_but_early_stop_is() -> None:
    ts = pd.date_range("2026-01-01", periods=3, freq="15min", tz="UTC")
    frame = pd.DataFrame({
        "timestamp": ts, "available_at": ts + pd.Timedelta(minutes=15),
        "open": [100, 100, 100], "high": [100, 100.2, 100.2],
        "low": [100, 99.8, 99.8], "close": [100, 100, 100],
    })
    candidate = {
        "signal_id": "pending", "setup_id": "X", "setup_variant": "X",
        "symbol": "BTCUSDT", "signal_idx": 0, "decision_ts": ts[0].isoformat(),
        "decision": "SHADOW_CANDIDATE", "regime": "RANGE", "atr15": 1.0,
        "direction": "LONG", "invalidation_level": 99,
    }
    pending = simulate_trade(frame, candidate, max_holding_bars=16)
    stopped_frame = frame.copy()
    stopped_frame.loc[1, "low"] = 98
    stopped = simulate_trade(stopped_frame, {**candidate, "signal_id": "stopped"}, max_holding_bars=16)
    assert pending and pending["exit_reason"] == "INCOMPLETE"
    assert pending["outcome_complete"] is False
    assert stopped and stopped["exit_reason"] == "SL"
    assert stopped["outcome_complete"] is True
    closed = _closed_forward_trades([pending, stopped], {"pending", "stopped"})
    assert [row["signal_id"] for row in closed] == ["stopped"]


def test_forward_boundary_uses_source_bar_close_not_open() -> None:
    boundary = _latest_available_at([{
        "last_timestamp": "2026-01-01T23:59:00+00:00",
        "expected_step_ms": 60_000,
    }])
    assert boundary == "2026-01-02T00:00:00+00:00"


def test_chronological_gate_never_promotes_small_or_negative_oos() -> None:
    rows = [{"decision_ts": f"2026-01-{idx + 1:02d}", "signal_id": str(idx),
             "net_return": 0.01 if idx < 16 else -0.02,
             "gross_return": 0.011 if idx < 16 else -0.019,
             "mfe": 0.02, "mae": 0.01, "held_bars": 2,
             "fee_fraction": 0.0012, "slippage_fraction": 0.0006,
             "funding_fraction": 0.0} for idx in range(20)]
    report = chronological_validation(rows)
    assert report["status"] == "NEED_MORE_DATA"
    assert report["paper_ready"] is False and report["live_ready"] is False
    assert any("test_net_ev_not_positive" == blocker for blocker in report["blockers"])


def test_manifest_and_raw_csv_are_both_revalidated(tmp_path: Path) -> None:
    sample = tmp_path / "sample"
    sample.mkdir()
    frame = _raw_minutes(300).rename(columns={"timestamp": "ts"})
    frame["ts"] = frame["ts"].astype("int64") // 1_000_000
    csv_path = sample / "bitget_BTCUSDT_1m.csv"
    frame[["ts", "open", "high", "low", "close", "volume"]].assign(turnover=1).to_csv(csv_path, index=False)
    manifest = {
        "symbol": "BTCUSDT", "timeframe": "1m", "quality_pass": True,
        "raw_quality_pass": True, "download_complete": True,
        "sha256": hashlib.sha256(csv_path.read_bytes()).hexdigest(),
        "uses_api_keys": False, "can_send_real_orders": False,
        "venue": "bitget",
    }
    (sample / "bitget_BTCUSDT_1m_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    report = run_historical_replay(sample_dir=sample, symbols=["BTCUSDT"], write=False)
    assert report["status"] == "INSUFFICIENT_DATA_OR_REJECTED"
    assert report["data_audits"][0]["status"] == "OK"
    # Manifest still says PASS, but corrupt raw OHLC must fail closed.
    corrupted = pd.read_csv(csv_path)
    corrupted.loc[5, "high"] = 1
    corrupted.to_csv(csv_path, index=False)
    manifest["sha256"] = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    (sample / "bitget_BTCUSDT_1m_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    failed = run_historical_replay(sample_dir=sample, symbols=["BTCUSDT"], write=False)
    assert failed["status"] == "NEED_DATA"
    assert "ATI_RAW_DATA_FAIL" in failed["blockers"][0]


def test_forward_merge_is_idempotent_and_detects_conflicting_outcome() -> None:
    row = {"signal_id": "a", "decision_ts": "2026-01-01", "value": 1}
    assert _merge_unique([row], [row], "signal_id") == [row]
    with pytest.raises(ValueError, match="ATI_FORWARD_ID_COLLISION"):
        _merge_unique([row], [{**row, "value": 2}], "signal_id")


def test_v10455_generation_adapter_requires_full_verified_current(monkeypatch, tmp_path: Path) -> None:
    generation = tmp_path / "bitget_BTCUSDT_1m" / "gen_abc"
    generation.mkdir(parents=True)
    csv_path = generation / "data.csv"
    manifest_path = generation / "manifest.json"
    csv_path.write_text("ts,open,high,low,close,volume,turnover\n", encoding="utf-8")
    manifest_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(ati_report, "DEFAULT_GENERATION_ROOT", tmp_path)
    from app.labs import public_data_backfill_v10_45_1 as backfill
    monkeypatch.setattr(backfill, "current_generation", lambda venue, symbol: {
        "generation_id": "abc", "csv_path": csv_path,
        "manifest_path": manifest_path,
    })
    monkeypatch.setattr(backfill, "verify_dataset", lambda venue, symbol, expected_timeframe="1m": {
        "status": "DATASET_VERIFIED", "sha256": "a" * 64,
        "generation_id": "abc",
        "manifest": {"venue": "bitget", "symbol": symbol, "timeframe": "1m"},
    })
    bundle = ati_report._generation_inputs(["BTCUSDT"])
    assert bundle is not None
    assert bundle["mode"] == "v10_45_5_verified_current_generations"
    assert bundle["entries"]["BTCUSDT"]["receipt"]["verification_status"] == "DATASET_VERIFIED"
    monkeypatch.setattr(backfill, "verify_dataset", lambda *args, **kwargs: {"status": "INVALID_SHA"})
    assert ati_report._generation_inputs(["BTCUSDT"]) is None


def test_source_snapshot_labels_source_separately_from_shadow_mode(monkeypatch, tmp_path: Path) -> None:
    dataset = tmp_path / "bitget_BTCUSDT_1m"
    generation = dataset / "gen_abc123"
    generation.mkdir(parents=True)
    (generation / "data.csv").write_text("fixture", encoding="utf-8")
    (generation / "manifest.json").write_text("{}", encoding="utf-8")
    marker = {"generation_id": "abc123", "csv_sha256": "x", "manifest_sha256": "y"}
    (dataset / "CURRENT.json").write_text(json.dumps(marker), encoding="utf-8")
    monkeypatch.setattr(ati_report, "DEFAULT_GENERATION_ROOT", tmp_path)
    from app.labs import public_data_backfill_v10_45_1 as backfill
    monkeypatch.setattr(backfill, "_dataset_dir", lambda venue, symbol: dataset)
    snapshot = ati_report.source_snapshot_status(symbols=["BTCUSDT"])
    assert snapshot["source_mode"] == "v10_45_5_current_markers"
    assert snapshot["mode"] == "SHADOW_RESEARCH_ONLY"


def test_observer_reuses_unchanged_snapshot_without_heavy_replay(monkeypatch) -> None:
    target = Path(__file__).resolve().parents[1] / "reports" / "research" / "ati" / "heartbeat_test"
    target.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    (target / "ati_summary.json").write_text("{}", encoding="utf-8")
    (target / "ati_forward_state.json").write_text(json.dumps({
        "status": "SHADOW_OBSERVATION_ONLY",
        "source_watch_token": "same-token",
        "forward_boundary": now,
        "dataset_available_at": now,
        "signals_total": 0,
        "open_positions": 0,
        "closed_outcomes": 0,
        "reconciliation_status": "PASS",
        "heavy_replay_last_run_at": now,
    }), encoding="utf-8")
    monkeypatch.setattr(ati_shadow, "source_snapshot_status", lambda **kwargs: {
        "status": "SNAPSHOT_AVAILABLE", "snapshot_watch_token": "same-token",
    })
    monkeypatch.setattr(
        ati_shadow, "run_shadow_once",
        lambda **kwargs: pytest.fail("unchanged snapshot must not run heavy replay"),
    )
    try:
        result = ati_shadow.run_shadow_loop(output_dir=target, max_cycles=1)
        state = result["last_state"]
        assert state["cache_status"] == "REUSED_UNCHANGED_SOURCE"
        assert state["heavy_replay_executed"] is False
        assert state["observer_status"] == "OBSERVER_CONNECTED"
        assert json.loads((target / "ati_health.json").read_text())["last_error"] is None
    finally:
        shutil.rmtree(target, ignore_errors=True)


def test_observer_refreshes_when_snapshot_token_changes(monkeypatch) -> None:
    target = Path(__file__).resolve().parents[1] / "reports" / "research" / "ati" / "refresh_test"
    target.mkdir(parents=True, exist_ok=True)
    (target / "ati_summary.json").write_text("{}", encoding="utf-8")
    (target / "ati_forward_state.json").write_text(json.dumps({
        "source_watch_token": "old-token",
    }), encoding="utf-8")
    calls = []
    monkeypatch.setattr(ati_shadow, "source_snapshot_status", lambda **kwargs: {
        "status": "SNAPSHOT_AVAILABLE", "snapshot_watch_token": "new-token",
    })
    monkeypatch.setattr(ati_shadow, "run_shadow_once", lambda **kwargs: calls.append(kwargs) or {
        "status": "SHADOW_OBSERVATION_ONLY", "source_watch_token": "new-token",
    })
    try:
        ati_shadow.run_shadow_loop(output_dir=target, max_cycles=1)
        assert len(calls) == 1
    finally:
        shutil.rmtree(target, ignore_errors=True)


def test_metrics_expose_gross_net_and_cost_totals() -> None:
    rows = [{
        "net_return": 0.01, "gross_return": 0.012,
        "mfe": 0.02, "mae": 0.005, "held_bars": 2,
        "fee_fraction": 0.001, "slippage_fraction": 0.0005,
        "funding_fraction": 0.0005,
    }]
    result = summarize_trades(rows)
    assert result["gross_pnl"] == pytest.approx(0.012)
    assert result["net_pnl"] == pytest.approx(0.01)
    assert result["total_cost"] == pytest.approx(0.002)


def test_incremental_public_refresh_deduplicates_exact_overlap_and_rejects_conflict() -> None:
    from scripts.refresh_ati_public_data import merge_verified_rows
    minute = 60_000
    start = 1_800_000_000_000
    existing = [
        [start, 100, 101, 99, 100, 1, 100],
        [start + minute, 100, 102, 99, 101, 2, 201],
    ]
    incoming = [
        [start + minute, 100, 102, 99, 101, 2, 201],
        [start + 2 * minute, 101, 103, 100, 102, 3, 306],
    ]
    merged = merge_verified_rows(
        existing, incoming, start_ms=start, end_ms=start + 3 * minute,
    )
    assert [row[0] for row in merged] == [start, start + minute, start + 2 * minute]
    with pytest.raises(ValueError, match="BAR_PAYLOAD_CONFLICT"):
        merge_verified_rows(
            existing, [[start + minute, 100, 999, 99, 101, 2, 201]],
            start_ms=start, end_ms=start + 2 * minute,
        )
    revisions = []
    replaced = merge_verified_rows(
        existing, [[start + minute, 100, 103, 99, 101, 2, 201]],
        start_ms=start, end_ms=start + 2 * minute,
        replace_conflicts_after_ms=start + minute,
        revised_tail_timestamps=revisions,
    )
    assert replaced[-1][2] == 103
    assert revisions == [start + minute]


def test_incremental_public_refresh_is_public_verified_and_atomic(monkeypatch) -> None:
    from scripts import refresh_ati_public_data as refresh
    end_ms = 200 * 86_400_000
    start_ms = end_ms - 86_400_000
    rows = [
        [start_ms + idx * 60_000, 100, 101, 99, 100, 1, 100]
        for idx in range(1440)
    ]
    bars = [{
        "ts": row[0], "open": row[1], "high": row[2], "low": row[3],
        "close": row[4], "volume": row[5], "turnover": row[6],
    } for row in rows]
    saved = []
    monkeypatch.setattr(
        refresh.backfill, "_now_ms",
        lambda: end_ms + 2 * refresh.backfill.BAR_MS,
    )
    monkeypatch.setattr(refresh.backfill, "verify_dataset", lambda *args, **kwargs: {
        "status": "DATASET_VERIFIED", "manifest": {"requested_end_ms": end_ms},
    })
    monkeypatch.setattr(refresh.backfill, "load_klines", lambda *args: bars)
    monkeypatch.setattr(refresh.backfill, "fetch_bitget_1m", lambda *args, **kwargs: rows[-10:])
    monkeypatch.setattr(refresh.backfill, "save_dataset", lambda *args, **kwargs: saved.append((args, kwargs)) or {
        "generation_id": "fixture", "sha256": "a" * 64,
        "n_bars": 1440, "actual_end": "fixture",
    })
    result = refresh.refresh_symbols(["BTCUSDT"], days=1, log=lambda *_: None)
    assert result["status"] == "REFRESHED_VERIFIED"
    assert result["public_endpoints_only"] is True
    assert result["uses_api_keys"] is False
    assert result["can_send_real_orders"] is False
    assert saved and len(saved[0][0][2]) == 1440


def test_output_is_contained_and_fixed_temp_name_cannot_redirect_write(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="ATI_OUTPUT_OUTSIDE_RESEARCH_ROOT"):
        _safe_output_dir(tmp_path)
    target = Path(__file__).resolve().parents[1] / "reports" / "research" / "ati" / "atomic_test.json"
    fixed_old_temp = target.with_suffix(target.suffix + ".tmp")
    fixed_old_temp.parent.mkdir(parents=True, exist_ok=True)
    fixed_old_temp.write_text("do-not-follow", encoding="utf-8")
    try:
        _atomic_write(target, "safe")
        assert target.read_text(encoding="utf-8") == "safe"
        assert fixed_old_temp.read_text(encoding="utf-8") == "do-not-follow"
    finally:
        target.unlink(missing_ok=True)
        fixed_old_temp.unlink(missing_ok=True)


def test_cli_commands_are_early_dispatched_research_only() -> None:
    parser = build_argument_parser()
    commands = {
        "ati-shadow-replay-v2", "ati-shadow-forward-once-v2",
        "ati-shadow-run-v2", "ati-shadow-status-v2",
    }
    assert commands <= PUBLIC_RESEARCH_ONLY_COMMANDS
    for command in commands:
        assert parser.parse_args([command]).command == command
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    runner = (
        (scripts / "run_ati_shadow.bat").read_text(encoding="utf-8")
        + (scripts / "run_ati_shadow_forever.ps1").read_text(encoding="utf-8")
    )
    assert "ati-shadow-run-v2" in runner
    assert "--max-scans 1" in runner
    assert "NO LIVE" in runner and "NO ORDERS" in runner.upper()


def test_ati_package_has_no_execution_or_private_exchange_surface() -> None:
    root = Path(__file__).resolve().parents[1] / "app" / "labs" / "ati"
    forbidden_calls = {
        "place_order", "private_get", "private_post", "set_leverage",
        "set_margin_mode", "open_position", "execute",
    }
    forbidden_import_fragments = {
        "execution_engine", "paper_trader", "bitget_client", "exchange_client",
    }
    for path in root.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [alias.name for alias in node.names]
                if isinstance(node, ast.ImportFrom) and node.module:
                    names.append(node.module)
                assert not any(fragment in name for fragment in forbidden_import_fragments for name in names)
            if isinstance(node, ast.Call):
                name = node.func.attr if isinstance(node.func, ast.Attribute) else (
                    node.func.id if isinstance(node.func, ast.Name) else ""
                )
                assert name not in forbidden_calls
        assert "LIVE_TRADING=True" not in source
        assert "ENABLE_PAPER_POLICY_FILTER=True" not in source
        assert "can_send_real_orders=True" not in source
