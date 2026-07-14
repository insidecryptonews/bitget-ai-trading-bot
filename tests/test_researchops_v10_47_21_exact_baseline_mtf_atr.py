"""V10.47.21 adversarial contract for pairing, MTF and ATR ledger."""

from __future__ import annotations

import copy
import inspect

import pytest


REQUIRED_MATCH_FIELDS = (
    "symbol", "timeframe", "side", "date", "session", "opportunity_id",
    "cluster_id", "regime_id", "entry_timestamp", "entry_availability",
    "max_holding_bars", "realised_holding_bars", "censoring_type",
    "end_of_dataset_censored", "notional_eur", "exposure_eur",
    "leverage_simulated", "fee_model_id", "spread_model_id",
    "slippage_model_id", "funding_settlements_crossed", "funding_cost_eur",
)


def _pairing_context():
    from app.labs.v10_46 import causal_tournament as CT

    campaign = CT.preregister_campaign()
    return {
        "m_campaign": campaign["m_campaign_effective_for_gate"],
        "campaign_registry": campaign["campaign_registry_contract"],
        "campaign_registry_sha": campaign["campaign_registry_sha"],
        "baseline_spec_hash": "b" * 64,
        "registry_hash": "c" * 64,
    }


def _pair_rows(n=1):
    candidates, baselines = [], []
    for i in range(n):
        common = {
            "symbol": "X", "timeframe": "1m", "side": "LONG",
            "date": "2026-01-01", "session": "ASIA",
            "opportunity_id": f"OP-{i}", "cluster_id": f"C-{i}",
            "regime_id": "RANGE", "entry_timestamp": i * 60_000,
            "entry_availability": i * 60_000 + 1,
            "max_holding_bars": 4, "realised_holding_bars": 4,
            "censoring_type": "NONE", "end_of_dataset_censored": False,
            "notional_eur": 5.0, "exposure_eur": 5.0,
            "leverage_simulated": 1.0, "fee_model_id": "fees-v1",
            "spread_model_id": "spread-v1", "slippage_model_id": "slip-v1",
            "funding_settlements_crossed": 0, "funding_cost_eur": 0.0,
        }
        candidates.append({
            **common, "candidate_trade_id": f"CAND-{i}",
            "candidate_net_eur": 1.0,
        })
        baselines.append({
            **common, "baseline_trade_id": f"BASE-{i}",
            "baseline_net_eur": 0.0,
        })
    return candidates, baselines


@pytest.mark.parametrize("field,bad", [
    ("symbol", "Y"),
    ("timeframe", "5m"),
    ("side", "SHORT"),
    ("date", "2026-01-02"),
    ("session", "EUROPE"),
    ("opportunity_id", "OTHER-OP"),
    ("cluster_id", "OTHER-CLUSTER"),
    ("regime_id", "TREND_DOWN"),
    ("entry_timestamp", 123),
    ("entry_availability", 124),
    ("max_holding_bars", 5),
    ("realised_holding_bars", 3),
    ("censoring_type", "END_OF_DATASET"),
    ("end_of_dataset_censored", True),
    ("notional_eur", 10.0),
    ("exposure_eur", 10.0),
    ("leverage_simulated", 2.0),
    ("fee_model_id", "fees-v2"),
    ("spread_model_id", "spread-v2"),
    ("slippage_model_id", "slip-v2"),
    ("funding_settlements_crossed", 1),
    ("funding_cost_eur", 0.2),
])
def test_incompatible_baseline_field_fails(field, bad):
    from app.labs.v10_46 import causal_stats as CS

    candidates, baselines = _pair_rows()
    baselines[0][field] = bad
    result = CS.matched_random_paired(
        candidate_trades=candidates, baseline_trades=baselines,
        timeframe="1m", m_global=10, **_pairing_context(),
    )
    assert result["match_status"] == "BASELINE_MATCH_INCOMPLETE"
    assert result["pairs_incompatible"] == 1
    assert result["pairs"][0]["match_status"] == "INCOMPATIBLE"
    assert result["pairs"][0]["unmatched_reason"] == (
        f"PAIR_FIELD_INCOMPATIBLE:{field.upper()}"
    )
    assert result["beats_matched_random"] is False


def test_exact_pair_records_have_one_to_one_ids_and_deltas():
    from app.labs.v10_46 import causal_stats as CS

    candidates, baselines = _pair_rows(4)
    result = CS.matched_random_paired(
        candidate_trades=candidates, baseline_trades=baselines,
        timeframe="1m", m_global=10, **_pairing_context(),
    )
    assert result["pairs_requested"] == 4
    assert result["pairs_found"] == 4
    assert len(result["pairs"]) == 4
    assert len({p["pair_id"] for p in result["pairs"]}) == 4
    assert len({p["baseline_trade_id"] for p in result["pairs"]}) == 4
    assert all(p["paired_delta_eur"] == 1.0 for p in result["pairs"])
    assert result["paired_deltas_eur"] == [1.0, 1.0, 1.0, 1.0]
    assert result["baseline_simulations_per_candidate"] == 1


@pytest.mark.parametrize("field", REQUIRED_MATCH_FIELDS)
def test_missing_field_on_both_sides_is_not_an_implicit_match(field):
    from app.labs.v10_46 import causal_stats as CS

    candidates, baselines = _pair_rows()
    candidates[0].pop(field)
    baselines[0].pop(field)
    result = CS.matched_random_paired(
        candidate_trades=candidates, baseline_trades=baselines,
        timeframe="1m", m_global=10, **_pairing_context(),
    )
    assert result["match_status"] == "BASELINE_MATCH_INCOMPLETE"
    assert result["pairs_incompatible"] == 1
    assert result["pairs"][0]["unmatched_reason"] == (
        f"PAIR_FIELD_INCOMPATIBLE:{field.upper()}"
    )


@pytest.mark.parametrize("missing_id,reason", [
    ("candidate_trade_id", "MISSING_CANDIDATE_TRADE_ID"),
    ("baseline_trade_id", "MISSING_BASELINE_TRADE_ID"),
])
def test_missing_pair_identity_fails_closed(missing_id, reason):
    from app.labs.v10_46 import causal_stats as CS

    candidates, baselines = _pair_rows()
    (candidates[0] if missing_id.startswith("candidate") else baselines[0]).pop(
        missing_id
    )
    result = CS.matched_random_paired(
        candidate_trades=candidates, baseline_trades=baselines,
        timeframe="1m", m_global=10, **_pairing_context(),
    )
    assert result["match_status"] == "BASELINE_PAIRING_INVALID"
    assert result["pairing_status"] == "INVALID"
    assert reason in result["rejection_reasons"]


def test_multiple_testing_correction_is_applied_to_baseline_gate():
    from app.labs.v10_46 import causal_stats as CS

    candidates, baselines = _pair_rows(5)
    result = CS.matched_random_paired(
        candidate_trades=candidates, baseline_trades=baselines,
        timeframe="1m", m_global=100, alpha=0.05,
        **_pairing_context(),
    )
    assert result["raw_p_value"] < 0.05
    assert result["corrected_p_value"] >= 0.05
    assert result["correction_method"] == "bonferroni"
    assert result["m_global"] == 100
    assert result["beats_matched_random"] is False


def test_registry_closes_specs_baseline_and_multiple_testing_before_metrics():
    from app.labs.v10_46 import causal_tournament as CT
    from app.labs.v10_46 import contracts as C

    registry = CT.preregister("X", "bitget", "1m", "g")
    assert registry["closed"] is True
    assert registry["closed_before_metrics"] is True
    assert registry["correction"] == "bonferroni"
    assert registry["alpha"] == 0.05
    assert registry["m_global"] == registry["m_unique_hypotheses"]
    assert len(registry["specs_hash"]) == 64
    assert len(registry["registry_hash"]) == 64
    assert registry["registry_hash"] == C.canonical_hash(
        registry["registry_contract"]
    )
    assert len(registry["baseline_policy_spec_hash"]) == 64
    assert registry["baseline_policy_spec"]["simulations_per_candidate"] == 1


def _bar(hour, *, day=0, close=100.0):
    ts = (day * 24 + hour) * 3_600_000
    return {"ts": ts, "open": close, "high": close + 1,
            "low": close - 1, "close": close, "volume": 1}


def test_incomplete_4h_bucket_is_not_published():
    from app.labs.v10_46 import det_strategies as DET

    agg = DET.aggregate_complete_regime_bars([_bar(0), _bar(4)])
    assert agg["bars"] == []
    assert agg["diagnostics"]["incomplete_buckets"] >= 1


def test_4h_bucket_with_gap_is_rejected():
    from app.labs.v10_46 import det_strategies as DET

    agg = DET.aggregate_complete_regime_bars([_bar(0), _bar(1), _bar(3), _bar(4)])
    assert agg["bars"] == []
    assert agg["diagnostics"]["gap_buckets"] >= 1


def test_4h_bucket_with_duplicate_is_rejected():
    from app.labs.v10_46 import det_strategies as DET

    bars = [_bar(0), _bar(1), copy.deepcopy(_bar(1)), _bar(2), _bar(3), _bar(4)]
    agg = DET.aggregate_complete_regime_bars(bars)
    assert agg["bars"] == []
    assert agg["diagnostics"]["duplicate_buckets"] >= 1


def test_4h_bucket_with_out_of_order_rows_is_rejected():
    from app.labs.v10_46 import det_strategies as DET

    agg = DET.aggregate_complete_regime_bars(
        [_bar(0), _bar(2), _bar(1), _bar(3)]
    )
    assert agg["bars"] == []
    assert agg["diagnostics"]["out_of_order_buckets"] == 1


def test_complete_bucket_is_published_only_at_close_boundary():
    from app.labs.v10_46 import det_strategies as DET

    bars = [_bar(0), _bar(1), _bar(2), _bar(3), _bar(4)]
    agg = DET.aggregate_complete_regime_bars(bars)
    assert len(agg["bars"]) == 1
    assert agg["bars"][0]["ts"] == 0
    assert agg["bars"][0]["close_ts"] == 4 * 3_600_000
    sigs = DET.precompute_det_sig_mtf(bars, entry_tf="1h", regime_tf="4h")
    assert all(s["regime_4h_close_ts"] == 0 for s in sigs[:4])


def test_complete_buckets_cross_day_boundary_without_overlap():
    from app.labs.v10_46 import det_strategies as DET

    bars = [_bar(hour) for hour in range(20, 28)]
    agg = DET.aggregate_complete_regime_bars(bars)
    assert [row["ts"] for row in agg["bars"]] == [
        20 * 3_600_000, 24 * 3_600_000,
    ]
    assert [row["close_ts"] for row in agg["bars"]] == [
        24 * 3_600_000, 28 * 3_600_000,
    ]


def test_future_mutation_does_not_change_prior_mtf_signal():
    from app.labs.v10_46 import det_strategies as DET

    bars = [_bar(i % 24, day=i // 24, close=100 + i / 100) for i in range(900)]
    before = DET.precompute_det_sig_mtf(bars, entry_tf="1h", regime_tf="4h")
    changed = copy.deepcopy(bars)
    changed[-1]["close"] *= 10
    after = DET.precompute_det_sig_mtf(changed, entry_tf="1h", regime_tf="4h")
    assert before[700] == after[700]


def test_future_incomplete_bucket_does_not_mutate_prior_signal_metadata():
    from app.labs.v10_46 import det_strategies as DET

    bars = [_bar(i, close=100 + i / 100) for i in range(900)]
    before = DET.precompute_det_sig_mtf(bars, entry_tf="1h", regime_tf="4h")
    after = DET.precompute_det_sig_mtf(
        bars + [_bar(900, close=109.0)], entry_tf="1h", regime_tf="4h"
    )
    assert before[:850] == after[:850]


def test_missing_closed_regime_bucket_blocks_fallback_to_older_regime():
    from app.labs.v10_46 import det_strategies as DET

    bars = [
        _bar(i, close=100 + i / 100)
        for i in range(900) if not 804 <= i <= 807
    ]
    signals = DET.precompute_det_sig_mtf(bars, entry_tf="1h", regime_tf="4h")
    at_808 = next(row for row in signals if row["ts"] == 808 * 3_600_000)
    assert at_808["regime_ready"] is False
    assert at_808["incomplete_bucket"] is True
    assert at_808["regime_4h_close_ts"] == 0


def test_deterministic_mtf_is_an_independent_experiment():
    from app.labs.v10_46 import det_strategies as DET

    report = DET.deterministic_mtf_experiment_registry()
    assert report["experiment_id"] == "DETERMINISTIC_MTF_1H_4H"
    assert report["status"] == "IMPLEMENTED"
    assert report["scientific_evaluation"] == "INSUFFICIENT_DATA"
    assert report["needs_2y_data"] is True
    assert set(report["participants"]) >= {
        "DET_EMA_ADX_PULLBACK_1H_4H", "DET_DONCHIAN_BREAKOUT_4H",
        "NO_TRADE", "EXACT_MATCH_BASELINE", "TREND_RIDER_1H_4H",
    }


@pytest.mark.parametrize("strategy,side", [
    ("ema", "LONG"), ("ema", "SHORT"),
    ("donchian", "LONG"), ("donchian", "SHORT"),
])
def test_deterministic_deciders_execute_long_short_at_next_open(strategy, side):
    from app.labs.v10_46 import causal_ledger as CL
    from app.labs.v10_46 import det_strategies as DET

    bars = [{"ts": i * 3_600_000, "open": 100.0, "high": 100.1,
             "low": 99.9, "close": 100.0, "volume": 1.0} for i in range(90)]
    signals = [{"ok": False, "atr": 2.0} for _ in bars]
    signal = {
        "ok": True, "atr": 2.0, "close": 100.0,
        "ema50": 99.0 if side == "LONG" else 101.0,
        "ema200": 100.0, "dist_ema50_atr": 0.0,
        "rsi_prev": 44.0 if side == "LONG" else 56.0,
        "rsi": 45.0 if side == "LONG" else 55.0,
        "regime_ready": True, "regime_4h_close_ts": 60 * 3_600_000,
        "ema50_4h": 110.0 if side == "LONG" else 90.0,
        "ema200_4h": 100.0, "adx_4h": 25.0,
        "plus_di_4h": 30.0 if side == "LONG" else 10.0,
        "minus_di_4h": 10.0 if side == "LONG" else 30.0,
        "don20_hi": 99.5, "don20_lo": 100.5,
        "don20_4h_hi": 99.5, "don20_4h_lo": 100.5,
    }
    signals[60] = signal
    factory = DET.ema_adx_pullback_decider if strategy == "ema" \
        else DET.donchian_breakout_decider
    decider = factory(symbol="X", venue="bitget", timeframe="1h", gen="g",
                      direction=side)
    output = CL.drive_causal(
        bars, signals, decider, DET.DET_EXIT_ATR, symbol="X", timeframe="1h"
    )
    assert len(output["trades"]) == 1
    assert output["trades"][0]["side"] == side
    assert output["trades"][0]["opportunity_bar"] == 60
    assert output["trades"][0]["entry_bar"] == 61


def _atr_ledger(side):
    from app.labs.v10_46 import causal_ledger as CL
    from app.labs.v10_46 import families as FAM

    bars = [{"ts": i * 60_000, "open": 100.0, "high": 100.2,
             "low": 99.8, "close": 100.0, "volume": 1.0} for i in range(90)]
    sigs = [{"atr": 2.0, "ok": True} for _ in bars]

    def decide(feats, event_id, dt, cluster):
        action = "TRADE" if feats["ts"] == 60 * 60_000 else "ABSTAIN_LOW_REWARD"
        return FAM._mk(action, side if action == "TRADE" else "FLAT", 0.6,
                       symbol="X", venue="bitget", timeframe="1m",
                       event_id=event_id, dt=dt, gen_id="g", reason="TEST")

    return CL.drive_causal(
        bars, sigs, decide,
        {"stop_atr_mult": 2.0, "tp_atr_mult": 6.0, "trail_atr_mult": 2.0,
         "trail_activate_r": 1.0, "atr_period": 14, "time_exit": 4},
        symbol="X", timeframe="1m",
    )


@pytest.mark.parametrize("side,expected_stop", [("LONG", 96.0), ("SHORT", 104.0)])
def test_atr_and_initial_stop_are_append_only_ledger_fields(side, expected_stop):
    out = _atr_ledger(side)
    ledger = out["ledger"]
    signal = ledger.by_kind("SIGNAL")[0]
    entry = ledger.by_kind("ENTRY")[0]
    position = ledger.by_kind("POSITION")[0]
    close = ledger.by_kind("CLOSE")[0]
    assert signal["atr_entry"] == 2.0
    assert signal["atr_period"] == 14
    assert signal["atr_multiplier"] == 2.0
    assert entry["entry_price"] == 100.0
    assert entry["initial_stop"] == expected_stop
    assert entry["stop_distance"] == 4.0
    assert entry["notional_eur"] == 5.0
    assert entry["leverage_simulated"] == 1.0
    assert position["immutable_initial_stop"] == expected_stop
    assert close["immutable_initial_stop"] == expected_stop
    assert close["actual_pnl_eur"] == out["trades"][0]["net_eur"]


def test_future_atr_cannot_mutate_initial_stop_ledger():
    out = _atr_ledger("LONG")
    before = out["ledger"].by_kind("ENTRY")[0]["initial_stop"]
    returned = out["ledger"].records()
    returned[0]["atr_entry"] = 999.0
    assert out["ledger"].by_kind("ENTRY")[0]["initial_stop"] == before == 96.0


def _trailing_ledger(side):
    from app.labs.v10_46 import causal_ledger as CL
    from app.labs.v10_46 import families as FAM

    bars = [{"ts": i * 60_000, "open": 100.0, "high": 100.2,
             "low": 99.8, "close": 100.0, "volume": 1.0} for i in range(90)]
    if side == "LONG":
        bars[62].update({"open": 100.0, "high": 105.0, "low": 99.0, "close": 104.0})
        bars[63].update({"open": 101.0, "high": 102.0, "low": 100.0, "close": 101.0})
    else:
        bars[62].update({"open": 100.0, "high": 101.0, "low": 95.0, "close": 96.0})
        bars[63].update({"open": 99.0, "high": 100.0, "low": 98.0, "close": 99.0})
    sigs = [{"atr": 2.0, "ok": True} for _ in bars]

    def decide(feats, event_id, dt, cluster):
        action = "TRADE" if feats["ts"] == 60 * 60_000 else "ABSTAIN_LOW_REWARD"
        return FAM._mk(action, side if action == "TRADE" else "FLAT", 0.6,
                       symbol="X", venue="bitget", timeframe="1m",
                       event_id=event_id, dt=dt, gen_id="g", reason="TEST")

    return CL.drive_causal(
        bars, sigs, decide,
        {"stop_atr_mult": 2.0, "tp_atr_mult": 6.0,
         "trail_atr_mult": 2.0, "trail_activate_r": 1.0,
         "atr_period": 14, "time_exit": 6},
        symbol="X", timeframe="1m",
    )


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_trailing_stop_is_append_only_never_widens_and_starts_next_bar(side):
    out = _trailing_ledger(side)
    states = [
        row for row in out["ledger"].by_kind("POSITION")
        if row["state"] == "BAR_AUDIT"
    ]
    first, second = states[0], states[1]
    assert first["derived_from_bar_ts"] is None
    assert first["pending_stop_next_bar"] is not None
    assert first["pending_stop_effective_ts"] == second["effective_ts"]
    assert second["derived_from_bar_ts"] == first["effective_ts"]
    active = [row["active_stop"] for row in states]
    if side == "LONG":
        assert active == sorted(active)
        assert all(value >= 96.0 for value in active)
    else:
        assert active == sorted(active, reverse=True)
        assert all(value <= 104.0 for value in active)
