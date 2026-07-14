"""V10.47.23 adversarial bijective pairing and campaign FWER contract."""

from __future__ import annotations

import copy
import inspect

import pytest


def _pair_rows(n: int = 1):
    candidates, baselines = [], []
    for index in range(n):
        common = {
            "symbol": "X", "timeframe": "1m", "side": "LONG",
            "date": "2026-01-01", "session": "ASIA",
            "opportunity_id": f"OP-{index}", "cluster_id": f"C-{index}",
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
            "candidate_net_eur": 1.0,
        })
        baselines.append({
            **common, "baseline_trade_id": f"BASE-{index}",
            "baseline_net_eur": 0.0,
        })
    return candidates, baselines


def _campaign_contract(*, m_campaign: int = 564, nominal: int | None = None,
                       deduplication_status: str | None = None,
                       include_valid_proof: bool = False):
    from app.labs.v10_46 import causal_stats as CS

    nominal = m_campaign if nominal is None else nominal
    deduplication_status = deduplication_status or (
        "AMBIGUOUS_USE_NOMINAL" if m_campaign == nominal
        else "SEMANTIC_EQUIVALENCE_PROVEN"
    )
    contract = {
        "schema": "v10_47_23_campaign_registry",
        "symbols": ["BTCUSDT", "DOGEUSDT", "ETHUSDT", "XRPUSDT"],
        "timeframes": ["1m", "5m", "15m"],
        "tournament_combinations": 12,
        "participants_per_tournament": 47,
        "m_campaign_nominal": nominal,
        "m_campaign_unique_hypotheses": nominal,
        "m_campaign_unique_results": m_campaign,
        "m_campaign_effective_for_gate": m_campaign,
        "deduplication_status": deduplication_status,
        "correction_method": "bonferroni",
        "alpha": 0.05,
        "closed": True,
        "closed_before_metrics": True,
    }
    if include_valid_proof:
        members = [f"HYP-{index}" for index in range(nominal - m_campaign + 1)]
        proof = {
            "schema": "v10_47_23_semantic_equivalence_proof",
            "groups": [{
                "semantic_fingerprint": "d" * 64,
                "members": members,
            }],
        }
        contract["semantic_equivalence_proof"] = proof
        contract["semantic_equivalence_proof_sha"] = CS._canonical_hash(proof)
    return contract, CS._canonical_hash(contract)


def _call(candidates, baselines, *, m_tournament: int = 47,
          m_campaign: int = 564, contract=None, registry_sha=None,
          baseline_spec_hash: str = "b" * 64, registry_hash: str = "c" * 64):
    """Call both the pre-fix and repaired API so the RED is reproducible."""
    from app.labs.v10_46 import causal_stats as CS

    if contract is None and registry_sha is None:
        contract, registry_sha = _campaign_contract(m_campaign=m_campaign)
    kwargs = {
        "candidate_trades": candidates,
        "baseline_trades": baselines,
        "timeframe": "1m",
        "m_global": m_tournament,
    }
    parameters = inspect.signature(CS.matched_random_paired).parameters
    optional = {
        "m_campaign": m_campaign,
        "campaign_registry": contract,
        "campaign_registry_sha": registry_sha,
        "baseline_spec_hash": baseline_spec_hash,
        "registry_hash": registry_hash,
    }
    for name, value in optional.items():
        if name in parameters:
            kwargs[name] = value
    return CS.matched_random_paired(**kwargs)


def _assert_invalid(result, reason: str):
    assert result.get("pairing_status") == "INVALID", result
    assert result.get("integrity_status") == "INVALID", result
    assert result.get("status") == "BASELINE_PAIRING_INVALID", result
    reasons = result.get("rejection_reasons", {})
    assert reason in reasons and reasons[reason] > 0, result
    assert result.get("baseline_gate") is False, result
    assert result.get("beats_matched_random") is False, result
    assert result.get("promotion_allowed") is False, result


def test_red_duplicate_candidate_can_not_be_reused_twelve_times():
    candidates, baselines = _pair_rows(12)
    for candidate in candidates:
        candidate["candidate_trade_id"] = "C1"
    result = _call(candidates, baselines)
    _assert_invalid(result, "DUPLICATE_CANDIDATE_TRADE_ID")
    assert result["pairs_accepted"] == 0
    assert result["duplicate_candidate_ids"] == 11


def test_duplicate_baseline_can_not_be_reused_twelve_times():
    candidates, baselines = _pair_rows(12)
    for baseline in baselines:
        baseline["baseline_trade_id"] = "B1"
    _assert_invalid(_call(candidates, baselines), "DUPLICATE_BASELINE_TRADE_ID")


def test_duplicate_pair_id_fails_closed(monkeypatch):
    from app.labs.v10_46 import causal_stats as CS

    candidates, baselines = _pair_rows(2)
    monkeypatch.setattr(CS, "deterministic_pair_id", lambda **kwargs: "a" * 64,
                        raising=False)
    result = _call(candidates, baselines)
    _assert_invalid(result, "DUPLICATE_PAIR_ID")
    assert not any(pair["match_status"] == "OK" for pair in result["pairs"])


def test_exact_duplicate_row_is_not_silently_deduplicated():
    candidates, baselines = _pair_rows()
    candidates.append(copy.deepcopy(candidates[0]))
    baselines.append(copy.deepcopy(baselines[0]))
    result = _call(candidates, baselines)
    _assert_invalid(result, "DUPLICATE_CANDIDATE_TRADE_ID")
    assert result["candidate_rows_received"] == 2


def test_empty_candidate_id_fails_closed():
    candidates, baselines = _pair_rows()
    candidates[0]["candidate_trade_id"] = ""
    _assert_invalid(_call(candidates, baselines), "MISSING_CANDIDATE_TRADE_ID")


def test_empty_baseline_id_fails_closed():
    candidates, baselines = _pair_rows()
    baselines[0]["baseline_trade_id"] = ""
    _assert_invalid(_call(candidates, baselines), "MISSING_BASELINE_TRADE_ID")


def test_empty_pair_id_fails_closed(monkeypatch):
    from app.labs.v10_46 import causal_stats as CS

    candidates, baselines = _pair_rows()
    monkeypatch.setattr(CS, "deterministic_pair_id", lambda **kwargs: "",
                        raising=False)
    _assert_invalid(_call(candidates, baselines), "MISSING_PAIR_ID")


def test_missing_candidate_id_fails_closed():
    candidates, baselines = _pair_rows()
    candidates[0].pop("candidate_trade_id")
    _assert_invalid(_call(candidates, baselines), "MISSING_CANDIDATE_TRADE_ID")


def test_missing_baseline_id_fails_closed():
    candidates, baselines = _pair_rows()
    baselines[0].pop("baseline_trade_id")
    _assert_invalid(_call(candidates, baselines), "MISSING_BASELINE_TRADE_ID")


def test_missing_pair_id_fails_closed(monkeypatch):
    from app.labs.v10_46 import causal_stats as CS

    candidates, baselines = _pair_rows()
    monkeypatch.setattr(CS, "deterministic_pair_id", lambda **kwargs: None,
                        raising=False)
    _assert_invalid(_call(candidates, baselines), "MISSING_PAIR_ID")


@pytest.mark.parametrize("target,value,reason", [
    ("candidate", 1, "INVALID_ID_TYPE"),
    ("baseline", 1, "INVALID_ID_TYPE"),
    ("candidate", True, "INVALID_ID_TYPE"),
    ("baseline", object(), "INVALID_ID_TYPE"),
    ("candidate", " CAND-0", "INVALID_ID_FORMAT"),
    ("baseline", "BASE-0 ", "INVALID_ID_FORMAT"),
])
def test_non_string_or_ambiguous_ids_fail_closed(target, value, reason):
    candidates, baselines = _pair_rows()
    if target == "candidate":
        candidates[0]["candidate_trade_id"] = value
    else:
        baselines[0]["baseline_trade_id"] = value
    _assert_invalid(_call(candidates, baselines), reason)


def test_same_candidate_with_two_baselines_fails_closed():
    candidates, baselines = _pair_rows(2)
    candidates[1]["candidate_trade_id"] = candidates[0]["candidate_trade_id"]
    _assert_invalid(_call(candidates, baselines), "DUPLICATE_CANDIDATE_TRADE_ID")


def test_two_candidates_with_same_baseline_fails_closed():
    candidates, baselines = _pair_rows(2)
    baselines[1]["baseline_trade_id"] = baselines[0]["baseline_trade_id"]
    _assert_invalid(_call(candidates, baselines), "DUPLICATE_BASELINE_TRADE_ID")


def test_pair_id_is_deterministic_and_binds_every_required_component():
    from app.labs.v10_46 import causal_stats as CS

    kwargs = {
        "candidate_trade_id": "C1", "baseline_trade_id": "B1",
        "symbol": "BTCUSDT", "timeframe": "1m",
        "matching_spec_hash": "a" * 64, "baseline_spec_hash": "b" * 64,
        "registry_hash": "c" * 64,
    }
    first = CS.deterministic_pair_id(**kwargs)
    assert first == CS.deterministic_pair_id(**dict(reversed(list(kwargs.items()))))
    assert len(first) == 64
    for field in kwargs:
        changed = dict(kwargs)
        changed[field] = changed[field] + "x"
        assert CS.deterministic_pair_id(**changed) != first


def test_integrity_metrics_hold_for_valid_bijection():
    candidates, baselines = _pair_rows(4)
    result = _call(candidates, baselines)
    assert result["pairing_status"] == "VALID"
    assert result["integrity_status"] == "PASS"
    assert result["pairs_accepted"] == 4
    assert result["pairs_accepted"] <= result["unique_candidate_ids"]
    assert result["pairs_accepted"] <= result["unique_baseline_ids"]
    assert result["pairs_accepted"] == result["unique_pair_ids"]


def test_campaign_correction_rejects_result_that_local_47_would_accept():
    candidates, baselines = _pair_rows(11)
    result = _call(candidates, baselines, m_tournament=47, m_campaign=564)
    assert result["raw_p_value"] == pytest.approx(0.0004882812)
    assert result["p_raw"] == result["raw_p_value"]
    assert result["p_tournament_corrected"] < 0.05
    assert result["p_campaign_corrected"] > 0.05
    assert result["method"] == "bonferroni"
    assert result["m_tournament"] == 47
    assert result["m_campaign"] == 564
    assert result["campaign_registry_sha"]
    assert result["beats_matched_random"] is False
    assert result["promotion_allowed"] is False


def test_changing_campaign_m_changes_gate():
    candidates, baselines = _pair_rows(11)
    local = _call(candidates, baselines, m_campaign=100)
    campaign = _call(candidates, baselines, m_campaign=564)
    assert local["p_campaign_corrected"] < 0.05
    assert campaign["p_campaign_corrected"] > 0.05
    assert local["beats_matched_random"] is True
    assert campaign["beats_matched_random"] is False


def test_missing_campaign_registry_fails_closed():
    candidates, baselines = _pair_rows(12)
    result = _call(candidates, baselines, contract=None, registry_sha="")
    _assert_invalid(result, "MISSING_CAMPAIGN_REGISTRY")


def test_missing_campaign_m_fails_closed():
    from app.labs.v10_46 import causal_stats as CS

    candidates, baselines = _pair_rows(12)
    contract, sha = _campaign_contract()
    result = CS.matched_random_paired(
        candidate_trades=candidates,
        baseline_trades=baselines,
        timeframe="1m",
        m_global=47,
        campaign_registry=contract,
        campaign_registry_sha=sha,
        baseline_spec_hash="b" * 64,
        registry_hash="c" * 64,
    )
    _assert_invalid(result, "INVALID_CAMPAIGN_MULTIPLICITY")


def test_campaign_m_smaller_than_tournament_fails_closed():
    candidates, baselines = _pair_rows(12)
    contract, sha = _campaign_contract(m_campaign=46)
    result = _call(candidates, baselines, m_tournament=47, m_campaign=46,
                   contract=contract, registry_sha=sha)
    _assert_invalid(result, "INVALID_CAMPAIGN_MULTIPLICITY")


def test_missing_campaign_registry_sha_fails_closed():
    candidates, baselines = _pair_rows(12)
    contract, _ = _campaign_contract()
    result = _call(candidates, baselines, contract=contract, registry_sha="")
    _assert_invalid(result, "MISSING_CAMPAIGN_REGISTRY_SHA")


def test_mutated_campaign_registry_invalidates_evidence():
    candidates, baselines = _pair_rows(12)
    contract, sha = _campaign_contract()
    contract["m_campaign_nominal"] = 563
    result = _call(candidates, baselines, contract=contract, registry_sha=sha)
    _assert_invalid(result, "CAMPAIGN_REGISTRY_SHA_MISMATCH")


def test_campaign_dedup_requires_hashed_semantic_equivalence_proof():
    candidates, baselines = _pair_rows(12)
    contract, sha = _campaign_contract(
        m_campaign=100, nominal=564,
        deduplication_status="SEMANTIC_EQUIVALENCE_PROVEN",
    )
    result = _call(candidates, baselines, m_campaign=100,
                   contract=contract, registry_sha=sha)
    _assert_invalid(result, "CAMPAIGN_DEDUP_PROOF_INVALID")


def test_hashed_semantic_equivalence_proof_can_reduce_campaign_m():
    candidates, baselines = _pair_rows(12)
    contract, sha = _campaign_contract(
        m_campaign=100, nominal=564,
        deduplication_status="SEMANTIC_EQUIVALENCE_PROVEN",
        include_valid_proof=True,
    )
    result = _call(candidates, baselines, m_campaign=100,
                   contract=contract, registry_sha=sha)
    assert result["pairing_status"] == "VALID"
    assert result["m_campaign"] == 100


def test_ambiguous_campaign_dedup_must_use_nominal_count():
    candidates, baselines = _pair_rows(12)
    contract, sha = _campaign_contract(
        m_campaign=100, nominal=564,
        deduplication_status="AMBIGUOUS_USE_NOMINAL",
    )
    result = _call(candidates, baselines, m_campaign=100,
                   contract=contract, registry_sha=sha)
    _assert_invalid(result, "AMBIGUOUS_CAMPAIGN_DEDUP")


def test_campaign_p_value_is_clamped_to_one():
    candidates, baselines = _pair_rows()
    result = _call(candidates, baselines)
    assert result["p_campaign_corrected"] == 1.0


def test_campaign_registry_is_closed_before_metrics_and_uses_nominal_fallback():
    from app.labs.v10_46 import causal_tournament as CT

    registry = CT.preregister_campaign()
    contract = registry["campaign_registry_contract"]
    assert contract["closed"] is True
    assert contract["closed_before_metrics"] is True
    assert contract["m_campaign_nominal"] == 564
    assert contract["m_campaign_effective_for_gate"] == 564
    assert contract["deduplication_status"] == "AMBIGUOUS_USE_NOMINAL"
    assert registry["campaign_registry_sha"] == CT.C.canonical_hash(contract)


def test_run_closes_campaign_before_real_market_signal_computation(monkeypatch):
    from app.labs.v10_46 import causal_tournament as CT
    from app.labs.v10_46.discovery_dataset import DiscoveryPartitions

    events = []
    campaign = CT.preregister_campaign()

    def close_campaign():
        events.append("campaign_closed")
        return copy.deepcopy(campaign)

    fake_registry = {
        "deciders": {}, "specs": {}, "fingerprints": {},
        "m_nominal": 47, "m_unique_hypotheses": 47,
        "m_unique_results": 45, "m_global": 47,
        "duplicated_runs": {}, "registry_hash": "a" * 64,
        "registry_contract": {}, "specs_hash": "b" * 64,
        "baseline_policy_spec": {}, "baseline_policy_spec_hash": "c" * 64,
        "correction": "bonferroni", "alpha": 0.05,
        "baseline_tolerance_spec_hash": "d" * 64,
        "closed": True, "closed_before_metrics": True,
    }
    monkeypatch.setattr(CT, "preregister_campaign", close_campaign)
    monkeypatch.setattr(CT, "preregister", lambda *a, **k: fake_registry)
    monkeypatch.setattr(
        CT.ES, "precompute_sigs",
        lambda bars: events.append("real_market_signals") or [None] * len(bars),
    )
    partitions = DiscoveryPartitions(
        train=({"ts": 1},), validation=({"ts": 2},),
        walk_forward=({"ts": 3},), source_root="synthetic",
    )
    CT.run_causal_tournament(
        partitions, symbol="BTCUSDT", venue="bitget", timeframe="1m",
        gen="synthetic", holdout_commitment={
            "state": "SEALED", "commitment_sha256": "e" * 64,
            "index_range": [3, 4], "n_bars": 1,
        },
    )
    assert events[0] == "campaign_closed"
    assert events.index("campaign_closed") < events.index("real_market_signals")
