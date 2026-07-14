"""Regression contracts discovered during V10.47.24 local invariant validation."""

from __future__ import annotations

import copy
import json
import math


def _campaign_contract(m: int) -> tuple[dict, str]:
    from app.labs.v10_46 import causal_stats as CS

    contract = {
        "schema": "v10_47_23_campaign_registry",
        "symbols": ["BTCUSDT", "DOGEUSDT", "ETHUSDT", "XRPUSDT"],
        "timeframes": ["1m", "5m", "15m"],
        "tournament_combinations": 12,
        "participants_per_tournament": 47,
        "m_campaign_nominal": m,
        "m_campaign_unique_hypotheses": m,
        "m_campaign_unique_results": m,
        "m_campaign_effective_for_gate": m,
        "deduplication_status": "AMBIGUOUS_USE_NOMINAL",
        "correction_method": "bonferroni",
        "alpha": 0.05,
        "closed": True,
        "closed_before_metrics": True,
    }
    return contract, CS._canonical_hash(contract)


def _rows(n: int = 1) -> tuple[list[dict], list[dict]]:
    candidates, baselines = [], []
    for index in range(n):
        common = {
            "symbol": "BTCUSDT", "timeframe": "1m", "side": "LONG",
            "date": f"2026-01-{index + 1:02d}", "session": f"S-{index}",
            "opportunity_id": f"OP-{index}", "cluster_id": f"C-{index}",
            "global_event_id": f"EVENT-{index}",
            "dependency_cluster_id": f"DEP-{index}",
            "regime_id": "RANGE", "entry_timestamp": index * 60_000,
            "entry_availability": index * 60_000 + 1,
            "max_holding_bars": 4, "realised_holding_bars": 4,
            "censoring_type": "NONE", "end_of_dataset_censored": False,
            "notional_eur": 5.0, "exposure_eur": 5.0,
            "leverage_simulated": 1.0, "fee_model_id": "fees-v1",
            "spread_model_id": "spread-v1", "slippage_model_id": "slip-v1",
            "funding_settlements_crossed": 0, "funding_cost_eur": 0.0,
        }
        candidates.append({
            **common, "candidate_trade_id": f"CAND-{index}",
            "underlying_trade_id": f"UNDER-{index}",
            "hypothesis_id": "P11_LONG", "candidate_net_eur": 1.0,
        })
        baselines.append({
            **common, "baseline_trade_id": f"BASE-{index}",
            "underlying_trade_id": f"BASE-UNDER-{index}",
            "hypothesis_id": "RANDOM_BASELINE", "baseline_net_eur": 0.0,
        })
    return candidates, baselines


def _paired(candidates, baselines, *, m_campaign: int = 564):
    from app.labs.v10_46 import causal_stats as CS

    contract, sha = _campaign_contract(m_campaign)
    return CS.matched_random_paired(
        candidate_trades=candidates, baseline_trades=baselines,
        timeframe="1m", m_global=47, m_campaign=m_campaign,
        campaign_registry=contract, campaign_registry_sha=sha,
        baseline_spec_hash="b" * 64, registry_hash="c" * 64,
    )


def test_caller_cannot_replace_canonical_campaign_with_reduced_family():
    candidates, baselines = _rows(11)
    result = _paired(candidates, baselines, m_campaign=47)
    assert result["pairing_status"] == "INVALID"
    assert result["promotion_allowed"] is False


def test_missing_required_pair_values_fail_closed_even_if_equal_on_both_sides():
    candidates, baselines = _rows()
    candidates[0]["regime_id"] = None
    baselines[0]["regime_id"] = None
    result = _paired(candidates, baselines)
    assert result["pairing_status"] == "INVALID"


def test_nonfinite_pair_outcomes_fail_closed():
    candidates, baselines = _rows(11)
    for candidate in candidates:
        candidate["candidate_net_eur"] = math.inf
    result = _paired(candidates, baselines, m_campaign=47)
    assert result["pairing_status"] == "INVALID"
    assert result["promotion_allowed"] is False


def test_pairing_reconciliation_is_independent_of_candidate_order():
    candidates, baselines = _rows(2)
    candidates[1]["opportunity_id"] = candidates[0]["opportunity_id"]
    baselines = baselines[:1]
    bad = copy.deepcopy(candidates[1])
    bad["side"] = "SHORT"
    first = _paired([candidates[0], bad], baselines)
    second = _paired([bad, candidates[0]], baselines)
    keys = ("pairs_found", "pairs_impossible", "pairs_incompatible", "coverage")
    assert {key: first[key] for key in keys} == {key: second[key] for key in keys}


def test_ambiguous_unicode_identifier_fails_closed():
    candidates, baselines = _rows()
    candidates[0]["candidate_trade_id"] = "CAND-\N{CYRILLIC CAPITAL LETTER A}"
    assert _paired(candidates, baselines)["pairing_status"] == "INVALID"


def test_repeated_economic_dependency_caps_effective_sample_size():
    from app.labs.v10_46.causal_stats import n_eff_estimate

    day = 86_400_000
    trades = [{
        "cluster": f"C-{i}", "session": f"S-{i}", "day": f"D-{i}",
        "opportunity_bar": i, "entry_bar": i * 3, "exit_index": i * 3 + 1,
        "entry_ts": i * day, "net_eur": 1.0 if i % 2 == 0 else -0.5,
        "dependency_cluster_id": "ONE-DEPENDENCY",
        "underlying_trade_id": "ONE-TRADE",
    } for i in range(40)]
    assert n_eff_estimate(trades, timeframe="1m")["n_eff_final"] == 1.0


def test_discovery_loader_rejects_bad_ohlc_and_cross_partition_overlap(tmp_path):
    from app.labs.v10_46.discovery_dataset import (
        DiscoveryDatasetError, DiscoveryDatasetLoader,
    )
    import pytest

    root = tmp_path / "discovery"
    for name, ts in (("train", 1000), ("validation", 1000), ("walk_forward", 2000)):
        part = root / name
        part.mkdir(parents=True)
        row = {"ts": ts, "open": 100.0, "high": 90.0, "low": 110.0,
               "close": 100.0, "volume": 1.0}
        (part / "bars.json").write_text(json.dumps([row]), encoding="utf-8")
    with pytest.raises(DiscoveryDatasetError):
        DiscoveryDatasetLoader(root).load()


def test_sim_oms_rejects_unknown_side_and_zero_entry_without_exception():
    from app.labs.v10_46 import sim_oms as S

    bar = {"ts": 0, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0}
    invalid_side = S.simulate_trade(
        side="HOLD", entry_bar=bar, exit_bars=[{**bar, "ts": 60_000}],
        entry_ts_ms=0, stop_frac=0.01, tp_frac=0.01, time_exit=1,
    )
    zero = S.simulate_trade(
        side="LONG", entry_bar={**bar, "open": 0.0},
        exit_bars=[{**bar, "ts": 60_000}], entry_ts_ms=0,
        stop_frac=0.01, tp_frac=0.01, time_exit=1,
    )
    assert invalid_side["status"] == "INVALID_INPUT"
    assert zero["status"] == "INVALID_INPUT"
