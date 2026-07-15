"""Regression contracts discovered during V10.47.24 local invariant validation."""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import inspect
import math


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
            "entry_availability": index * 60_000,
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
            "hypothesis_id": "PREREGISTERED_RANDOM_BASELINE_V10_47_23",
            "baseline_net_eur": 0.0,
        })
    return candidates, baselines


def _paired(candidates, baselines):
    from app.labs.v10_46 import causal_stats as CS
    from app.labs.v10_46 import campaign_authority as CA

    return CS.matched_random_paired(
        candidate_trades=candidates, baseline_trades=baselines,
        campaign_id=CA.CAMPAIGN_ID, symbol="BTCUSDT", timeframe="1m",
    )


def test_caller_cannot_replace_canonical_campaign_with_reduced_family():
    from app.labs.v10_46 import causal_stats as CS

    candidates, baselines = _rows(11)
    parameters = inspect.signature(CS.matched_random_paired).parameters
    forbidden = {
        "m_global", "m_campaign", "alpha", "correction_method",
        "tolerance_spec", "campaign_registry", "campaign_registry_sha",
        "baseline_spec_hash", "registry_hash",
    }
    assert forbidden.isdisjoint(parameters)
    result = _paired(candidates, baselines)
    assert result["m_campaign"] == 564
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
    result = _paired(candidates, baselines)
    assert result["pairing_status"] == "INVALID"
    assert result["promotion_allowed"] is False


def test_pair_side_and_numeric_ranges_fail_closed():
    candidates, baselines = _rows()
    candidates[0]["side"] = baselines[0]["side"] = "HOLD"
    assert _paired(candidates, baselines)["pairing_status"] == "INVALID"
    candidates, baselines = _rows()
    candidates[0]["notional_eur"] = baselines[0]["notional_eur"] = -1.0
    assert _paired(candidates, baselines)["pairing_status"] == "INVALID"


def test_pairing_rejects_economically_impossible_matching_rows():
    candidates, baselines = _rows()
    for row in (*candidates, *baselines):
        row["entry_availability"] = row["entry_timestamp"] + 1
    assert _paired(candidates, baselines)["pairing_status"] == "INVALID"
    candidates, baselines = _rows()
    for row in (*candidates, *baselines):
        row["leverage_simulated"] = 2.0
    assert _paired(candidates, baselines)["pairing_status"] == "INVALID"


def test_pairing_requires_explicit_canonical_outcome_fields():
    candidates, baselines = _rows()
    candidates[0]["net_eur"] = candidates[0].pop("candidate_net_eur")
    result = _paired(candidates, baselines)
    assert result["pairing_status"] == "INVALID"
    assert result["rejection_reasons"]["MISSING_CANONICAL_PAIR_OUTCOME"] == 1


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


def test_repeated_dependency_cluster_is_valid_pairing_evidence_not_duplication():
    candidates, baselines = _rows(2)
    for row in (*candidates, *baselines):
        row["dependency_cluster_id"] = "SHARED-DEPENDENCY"
    result = _paired(candidates, baselines)
    assert result["pairing_status"] == "VALID"
    assert "DUPLICATE_DEPENDENCY_CLUSTER_ID" not in result["rejection_reasons"]
    assert result["promotion_allowed"] is False
    assert result["promotion_scope"] == "BASELINE_COMPONENT_GATE_ONLY"


def test_missing_dependency_identity_forces_zero_effective_sample():
    from app.labs.v10_46.causal_stats import n_eff_estimate

    trade = {
        "cluster": "C", "session": "S", "day": "D", "opportunity_bar": 1,
        "entry_bar": 2, "exit_index": 3, "entry_ts": 60_000,
        "net_eur": 1.0,
    }
    report = n_eff_estimate([trade], timeframe="1m")
    assert report["n_eff_final"] == 0.0
    assert report["dependency_ids_complete"] is False
    assert report["underlying_ids_complete"] is False


def test_invalid_n_eff_rows_fail_closed_without_exception():
    from app.labs.v10_46.causal_stats import n_eff_estimate

    report = n_eff_estimate([{"net_eur": 1.0}], timeframe="1m")
    assert report["n_eff_final"] == 0.0
    assert report["input_valid"] is False
    assert report["invalid_reason"] == "MISSING_REQUIRED_N_EFF_FIELD"


def test_n_eff_is_independent_of_input_row_order():
    from app.labs.v10_46.causal_stats import n_eff_estimate

    day = 86_400_000
    trades = [{
        "cluster": f"C-{i}", "session": f"S-{i}", "day": f"D-{i}",
        "opportunity_bar": i, "entry_bar": i * 2, "exit_index": i * 2 + 1,
        "entry_ts": i * day, "net_eur": (-1.0, 0.5, 2.0)[i % 3],
        "dependency_cluster_id": f"DEP-{i}", "underlying_trade_id": f"U-{i}",
    } for i in range(12)]
    assert n_eff_estimate(trades, timeframe="1m") == n_eff_estimate(
        list(reversed(trades)), timeframe="1m",
    )


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


def _manifest_bound_discovery(tmp_path):
    from app.labs.v10_46.discovery_dataset import DiscoveryDatasetLoader

    combo = tmp_path / "combo"
    root = combo / "discovery"
    manifest_files = []
    for offset, name in enumerate(("train", "validation", "walk_forward")):
        rows = [{
            "ts": offset * 120_000 + step * 60_000,
            "open": 100.0, "high": 101.0, "low": 99.0,
            "close": 100.0, "volume": 1.0,
        } for step in range(2)]
        path = root / name / "bars.json"
        path.parent.mkdir(parents=True)
        raw = json.dumps(rows, separators=(",", ":")).encode("utf-8")
        path.write_bytes(raw)
        manifest_files.append({
            "partition": name, "path": f"discovery/{name}/bars.json",
            "sha256": hashlib.sha256(raw).hexdigest(), "rows": len(rows),
            "first_ts": rows[0]["ts"], "last_ts": rows[-1]["ts"],
        })
    (combo / "dataset_manifest.json").write_text(
        json.dumps({"files": manifest_files}), encoding="utf-8",
    )
    return DiscoveryDatasetLoader(root).load(), combo / "dataset_manifest.json"


def test_discovery_partitions_are_cryptographically_bound_to_manifest(tmp_path):
    from app.labs.v10_46.discovery_dataset import verify_discovery_partitions

    partitions, manifest = _manifest_bound_discovery(tmp_path)
    report = verify_discovery_partitions(partitions, manifest)
    assert report["status"] == "DISCOVERY_PARTITIONS_VERIFIED"
    assert len(report["partitions"]) == 3


def test_in_memory_partition_forgery_cannot_borrow_manifest_authority(tmp_path):
    import pytest
    from app.labs.v10_46.discovery_dataset import (
        DiscoveryDatasetError, verify_discovery_partitions,
    )

    partitions, manifest = _manifest_bound_discovery(tmp_path)
    forged_train = [copy.deepcopy(row) for row in partitions.train]
    forged_train[0]["close"] = 100.5
    forged = dataclasses.replace(partitions, train=tuple(forged_train))
    with pytest.raises(DiscoveryDatasetError, match="partition evidence mismatch"):
        verify_discovery_partitions(forged, manifest)


def test_unmanifested_reference_data_is_rejected(tmp_path):
    import pytest
    from app.labs.v10_46.discovery_dataset import (
        DiscoveryDatasetError, load_verified_reference,
    )

    partitions, manifest = _manifest_bound_discovery(tmp_path)
    reference_path = manifest.parent / "discovery" / "reference" / "bars.json"
    reference_path.parent.mkdir()
    reference_path.write_text(
        json.dumps([{"ts": 1, "close": 100.0}]), encoding="utf-8",
    )
    with pytest.raises(DiscoveryDatasetError, match="unmanifested reference"):
        load_verified_reference(partitions.source_root, manifest)


def test_reference_data_is_bound_to_manifest_content(tmp_path):
    import pytest
    from app.labs.v10_46.discovery_dataset import (
        DiscoveryDatasetError, load_verified_reference,
    )

    partitions, manifest = _manifest_bound_discovery(tmp_path)
    reference_rows = [
        {"ts": 1, "close": 100.0}, {"ts": 2, "close": 101.0},
    ]
    raw = json.dumps(reference_rows, separators=(",", ":")).encode("utf-8")
    reference_path = manifest.parent / "discovery" / "reference" / "bars.json"
    reference_path.parent.mkdir()
    reference_path.write_bytes(raw)
    manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
    manifest_value.update({
        "symbol": "BTCUSDT",
        "source": {"symbol": "BTCUSDT", "venue": "bitget"},
        "reference_source": {"symbol": "BTCUSDT", "venue": "bybit"},
    })
    manifest_value["files"].append({
        "partition": "reference_discovery",
        "path": "discovery/reference/bars.json",
        "sha256": hashlib.sha256(raw).hexdigest(),
        "rows": 2, "first_ts": 1, "last_ts": 2,
    })
    manifest.write_text(json.dumps(manifest_value), encoding="utf-8")
    reference, evidence = load_verified_reference(partitions.source_root, manifest)
    assert reference == {1: 100.0, 2: 101.0}
    assert evidence["status"] == "REFERENCE_VERIFIED"
    reference_path.write_text(
        json.dumps([{"ts": 1, "close": 999.0}]), encoding="utf-8",
    )
    with pytest.raises(DiscoveryDatasetError, match="reference evidence mismatch"):
        load_verified_reference(partitions.source_root, manifest)


def test_pairing_only_authorization_cannot_evaluate_candidate():
    import pytest
    from app.labs.v10_46 import campaign_authority as CA

    pairing_only = CA.authorize_pairing(
        campaign_id=CA.CAMPAIGN_ID, symbol="BTCUSDT", timeframe="1m",
    )
    with pytest.raises(
            CA.CampaignAuthorityError,
            match="TOURNAMENT_AUTHORIZATION_NOT_FACTORY_ISSUED"):
        CA.validate_full_authorization(pairing_only)


def test_holdout_commitment_is_loaded_from_manifest_without_opening_bars(tmp_path):
    from app.labs.v10_46.discovery_dataset import load_verified_holdout_commitment

    partitions, manifest = _manifest_bound_discovery(tmp_path)
    commitment = {
        "schema": "v10_47_20_holdout_commitment",
        "state": "SEALED", "symbol": "BTCUSDT", "timeframe": "1m",
        "data_file": "bars.json", "index_range": [6, 8], "n_bars": 2,
        "commitment_sha256": "f" * 64, "research_only": True,
        "final_recommendation": "NO LIVE",
    }
    path = manifest.parent / "sealed_holdout" / "commitment.json"
    path.parent.mkdir()
    raw = json.dumps(commitment, separators=(",", ":")).encode("utf-8")
    path.write_bytes(raw)
    value = json.loads(manifest.read_text(encoding="utf-8"))
    value.update({
        "symbol": "BTCUSDT", "timeframe": "1m",
        "split": {"holdout": [6, 8]},
    })
    value["files"].extend([
        {
            "partition": "holdout_commitment",
            "path": "sealed_holdout/commitment.json",
            "sha256": hashlib.sha256(raw).hexdigest(),
        },
        {
            "partition": "holdout_sealed", "path": "sealed_holdout/bars.json",
            "sha256": "f" * 64, "rows": 2,
        },
    ])
    manifest.write_text(json.dumps(value), encoding="utf-8")
    loaded, evidence = load_verified_holdout_commitment(
        partitions.source_root, manifest,
    )
    assert loaded == commitment
    assert evidence["status"] == "HOLDOUT_COMMITMENT_VERIFIED"
    assert evidence["sealed_data_opened"] is False


def test_campaign_authority_matches_all_twelve_current_registries():
    from app.labs.v10_46 import campaign_authority as CA
    from app.labs.v10_46 import causal_tournament as CT

    authority = CA.load_campaign_authority()
    assert len(authority["entries"]) == 12
    assert len({entry["key"] for entry in authority["entries"]}) == 12
    expected_venues = {
        "BTCUSDT": "bitget", "ETHUSDT": "bitget",
        "XRPUSDT": "bybit", "DOGEUSDT": "bitget",
    }
    for entry in authority["entries"]:
        assert entry["venue"] == expected_venues[entry["symbol"]]
        registry = CT.preregister(
            entry["symbol"], entry["venue"], entry["timeframe"],
            entry["dataset_source_generation_id"],
        )
        assert registry["registry_hash"] == entry["tournament_registry_hash"]
        assert registry["specs_hash"] == entry["participant_specs_hash"]
        assert registry["baseline_policy_spec_hash"] == entry["baseline_spec_hash"]
        assert registry["baseline_tolerance_spec_hash"] == entry["tolerance_spec_hash"]


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


def test_sim_oms_rejects_timestamp_mismatch_gap_and_nonfinite_fill():
    from app.labs.v10_46 import sim_oms as S

    bar = {"ts": 0, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0}
    mismatch = S.simulate_trade(
        side="LONG", entry_bar=bar, exit_bars=[{**bar, "ts": 60_000}],
        entry_ts_ms=1, stop_frac=0.01, tp_frac=0.01, time_exit=1,
    )
    gap = S.simulate_trade(
        side="LONG", entry_bar=bar, exit_bars=[{**bar, "ts": 120_000}],
        entry_ts_ms=0, stop_frac=0.02, tp_frac=0.02, time_exit=2,
    )
    fill = S.simulate_fill(
        {"order_type": "market", "side": "LONG", "qty": math.nan},
        bar, S.COST_SCENARIOS["observed"],
    )
    assert mismatch["reason"] == "ENTRY_TIMESTAMP_MISMATCH"
    assert gap["reason"] == "EXIT_BAR_INTERVAL_GAP"
    assert fill == {"fill_status": "INVALID_INPUT", "reason": "ORDER_QTY_INVALID"}


def test_sim_oms_long_short_symmetry_and_same_bar_stop_priority():
    from app.labs.v10_46 import sim_oms as S

    entry = {"ts": 0, "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0}
    long_bar = {"ts": 60_000, "open": 100.0, "high": 101.5,
                "low": 99.5, "close": 100.0}
    short_bar = {"ts": 60_000, "open": 100.0, "high": 100.5,
                 "low": 98.5, "close": 100.0}
    long = S.simulate_trade(
        side="LONG", entry_bar=entry, exit_bars=[long_bar], entry_ts_ms=0,
        stop_frac=0.005, tp_frac=0.01, time_exit=2,
    )
    short = S.simulate_trade(
        side="SHORT", entry_bar=entry, exit_bars=[short_bar], entry_ts_ms=0,
        stop_frac=0.005, tp_frac=0.01, time_exit=2,
    )
    assert long["exit_reason"] == short["exit_reason"] == "SL"
    assert long["gross_pnl_eur"] == short["gross_pnl_eur"]


def test_sim_oms_entry_bar_is_first_exposure_bar_and_horizon_is_exact():
    from app.labs.v10_46 import sim_oms as S

    entry = {
        "ts": 0, "open": 100.0, "high": 100.2, "low": 99.8, "close": 100.1,
    }
    unused = {
        "ts": 60_000, "open": 100.1, "high": math.inf,
        "low": 1.0, "close": 100.0,
    }
    result = S.simulate_trade(
        side="LONG", entry_bar=entry, exit_bars=[unused], entry_ts_ms=0,
        stop_frac=0.01, tp_frac=0.01, time_exit=1,
    )
    assert result["status"] == "OK"
    assert result["exit_reason"] == "TIME"
    assert result["exit_ts_ms"] == 60_000
    assert result["bars_held"] == 1


def test_sim_oms_entry_bar_same_bar_ambiguity_is_stop_before_tp():
    from app.labs.v10_46 import sim_oms as S

    ambiguous = {
        "ts": 0, "open": 100.0, "high": 102.0, "low": 98.0, "close": 100.0,
    }
    result = S.simulate_trade(
        side="LONG", entry_bar=ambiguous, exit_bars=[], entry_ts_ms=0,
        stop_frac=0.01, tp_frac=0.01, time_exit=1,
    )
    assert result["status"] == "OK"
    assert result["exit_reason"] == "SL"
    assert result["bars_held"] == 1


def test_sim_oms_gap_open_uses_exact_open_timestamp_for_both_sides():
    from app.labs.v10_46 import sim_oms as S

    entry = {
        "ts": 0, "open": 100.0, "high": 100.2, "low": 99.8, "close": 100.0,
    }
    long_gap = {
        "ts": 60_000, "open": 95.0, "high": 95.2, "low": 94.8, "close": 95.0,
    }
    short_gap = {
        "ts": 60_000, "open": 105.0, "high": 105.2, "low": 104.8, "close": 105.0,
    }
    long = S.simulate_trade(
        side="LONG", entry_bar=entry, exit_bars=[long_gap], entry_ts_ms=0,
        stop_frac=0.01, tp_frac=0.05, time_exit=2,
    )
    short = S.simulate_trade(
        side="SHORT", entry_bar=entry, exit_bars=[short_gap], entry_ts_ms=0,
        stop_frac=0.01, tp_frac=0.05, time_exit=2,
    )
    assert long["exit_reason"] == short["exit_reason"] == "SL"
    assert long["exit_ts_ms"] == short["exit_ts_ms"] == 60_000
    assert long["bars_held"] == short["bars_held"] == 1


def test_tournament_metrics_reject_nonfinite_cost_components():
    from app.labs.v10_46 import causal_tournament as CT

    trade = {
        "net_eur": 1.0, "gross_eur": 1.1, "fee_eur": math.nan,
        "spread_eur": 0.01, "slippage_eur": 0.01, "funding_eur": 0.0,
        "cluster": "C", "session": "S", "day": "D",
        "opportunity_bar": 1, "entry_bar": 2, "exit_index": 3,
        "entry_ts": 60_000, "dependency_cluster_id": "DEP",
        "underlying_trade_id": "U",
    }
    result = CT._safe_metrics([trade], {}, "1m")
    assert result["metrics_valid"] is False
    assert result["classification"] == "INVALID_METRICS"


def test_tournament_summary_tolerates_fail_closed_null_statistics():
    from scripts import v10_47_22_regenerate_tournaments as runner

    value = {
        "symbol": "BTCUSDT", "timeframe": "1m",
        "results": {
            "P11_LONG": {
                "metrics": {"classification": "NET_EDGE_POSITIVE", "n_eff_final": 1.0},
                "gate": {
                    "walk_forward_called": False,
                    "matched_random_paired": {
                        "pairs_requested": 1, "pairs_found": 0,
                        "pairs_incompatible": 0, "coverage": 0.0,
                        "corrected_p_value": None,
                    },
                },
            },
        },
        "n_net_positive": 1, "shadow_candidates": [],
        "validation_admitted_candidates": [],
        "validation_rejected_candidates": ["P11_LONG"],
        "holdout": {"state": "SEALED", "physically_loaded": False},
    }
    summary = runner.summarize(value)
    assert summary["minimum_baseline_coverage"] == 0.0
    assert summary["minimum_corrected_p_value"] is None
    assert summary["shadow_candidates"] == []


def test_v104725_evidence_requires_hashed_test_logs_and_safe_security_output(tmp_path):
    import pytest
    from scripts import v10_47_25_generate_evidence as evidence

    execution_log = tmp_path / "pytest_execution.log"
    nodeids = tmp_path / "pytest_nodeids.txt"
    collection = tmp_path / "collection_record.json"
    execution_log.write_text("1 passed in 0.01s\n", encoding="utf-8")
    nodeids.write_text("tests/test_example.py::test_ok\n", encoding="utf-8")
    collection.write_text("{}\n", encoding="utf-8")
    record = {
        "schema": "v10_47_22_certified_execution",
        "head": evidence.git("rev-parse", "HEAD"),
        "tree": evidence.git("rev-parse", "HEAD^{tree}"),
        "exit_code": 0, "collected": 1, "unique_nodeids": 1,
        "duplicate_nodeids": [], "passed": 1, "failed": 0,
        "skipped": 0, "xfailed": 0, "xpassed": 0,
        "raw_log_sha256": evidence.sha256(execution_log),
        "nodeids_sha256": evidence.sha256(nodeids),
        "collection_record_sha256": evidence.sha256(collection),
    }
    (tmp_path / "execution_record.json").write_text(
        json.dumps(record), encoding="utf-8",
    )
    assert evidence.validate_execution(tmp_path)["passed"] == 1
    execution_log.write_text("tampered\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="certified execution"):
        evidence.validate_execution(tmp_path)

    security = tmp_path / "security.log"
    security.write_text(
        "final_security_status: SAFE_PAPER_ONLY\n"
        "can_send_real_orders: false\n"
        "final_recommendation: NO LIVE\n",
        encoding="utf-8",
    )
    evidence.validate_security_audit(security)
    security.write_text("final_security_status: UNKNOWN\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="SAFE_PAPER_ONLY"):
        evidence.validate_security_audit(security)
