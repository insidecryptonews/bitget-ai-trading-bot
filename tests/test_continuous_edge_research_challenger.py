from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.labs import continuous_edge_research_challenger as challenger


ROOT = Path(__file__).resolve().parents[1]


def _rows(count: int = 80) -> list[dict]:
    rows = []
    for symbol_index, symbol in enumerate(("BTCUSDT", "ETHUSDT")):
        for venue_index, venue in enumerate(("bybit", "binance")):
            for index in range(count):
                bucket = 1_700_000_000_000 + index * 1000
                price = 100.0 + symbol_index * 10 + index * 0.03 + venue_index * 0.001
                rows.append({
                    "canonical_symbol": symbol, "venue": venue,
                    "source_partition_id": f"{venue}:{symbol}:day",
                    "horizon_ms": 1000, "bucket_start_ms": bucket,
                    "first_event_timestamp_ms": bucket + 10,
                    "last_event_timestamp_ms": bucket + 900,
                    "causal_cutoff_ms": bucket + 900,
                    "first_midpoint": price, "last_midpoint": price + 0.01,
                    "spread_bps": 1.0, "book_imbalance": 0.8,
                    "aggressive_buy_volume": 10.0, "aggressive_sell_volume": 1.0,
                    "net_aggressor_volume": 9.0, "trade_intensity_per_second": 11.0,
                    "price_return_bps": 1.0, "gap_flag": 0,
                    "dataset_hash": "dataset", "feature_version": "v1",
                })
    rows.sort(key=lambda row: (row["bucket_start_ms"], row["canonical_symbol"], row["venue"]))
    return rows


def _extreme_spec(hold: int = 1) -> dict:
    return {
        "family": "extreme_flow", "flow_threshold": 0.45,
        "holding_buckets": hold,
        "required_features": ["aggressive_buy_volume", "aggressive_sell_volume"],
        "trial_id": "test",
    }


def test_trial_registry_is_bounded_and_has_no_outcome_features() -> None:
    specs = challenger.compile_trial_specs(max_families=5, max_trials=80)
    assert 0 < len(specs) <= 80
    assert len({spec["trial_id"] for spec in specs}) == len(specs)
    for spec in specs:
        for feature in spec["required_features"]:
            assert not any(token in feature.lower() for token in challenger.PROHIBITED_FEATURE_TOKENS)


def test_entry_is_next_bucket_and_future_mutation_does_not_change_signal() -> None:
    rows = _rows(8)
    stream = [row for row in rows if row["canonical_symbol"] == "BTCUSDT" and row["venue"] == "bybit"]
    challenger._augment_prefix_features(stream)
    start, end = stream[0]["bucket_start_ms"], stream[-1]["bucket_start_ms"] + 1000
    before = challenger._opportunities(stream, _extreme_spec(), start_ms=start, end_ms=end)
    assert before and before[0]["entry_timestamp_ms"] > before[0]["signal_timestamp_ms"]
    identity = {
        key: before[0][key]
        for key in ("signal_timestamp_ms", "entry_timestamp_ms", "exit_timestamp_ms", "side")
    }
    stream[-1]["last_midpoint"] *= 2
    after = challenger._opportunities(stream, _extreme_spec(), start_ms=start, end_ms=end)
    assert {key: after[0][key] for key in identity} == identity


def test_gap_path_is_skipped_and_incomplete_outcomes_are_not_counted() -> None:
    rows = _rows(8)
    stream = [row for row in rows if row["canonical_symbol"] == "BTCUSDT" and row["venue"] == "bybit"]
    stream[0]["gap_flag"] = 1
    challenger._augment_prefix_features(stream)
    outcomes = challenger._opportunities(
        stream, _extreme_spec(),
        start_ms=stream[0]["bucket_start_ms"],
        end_ms=stream[-1]["bucket_start_ms"] + 1000,
    )
    assert all(row["signal_timestamp_ms"] != stream[0]["causal_cutoff_ms"] for row in outcomes)
    tight_end = stream[2]["bucket_start_ms"]
    assert challenger._opportunities(
        stream[:3], _extreme_spec(hold=2),
        start_ms=stream[0]["bucket_start_ms"], end_ms=tight_end,
    ) == []


def test_costs_reduce_ev_and_effective_sample_never_exceeds_raw() -> None:
    outcomes = [
        {
            "entry_timestamp_ms": i * 1000,
            "exit_timestamp_ms": i * 1000 + 2500,
            "gross_bps": 20.0 if i % 2 == 0 else -5.0,
            "symbol": "BTCUSDT" if i % 2 else "ETHUSDT",
        }
        for i in range(20)
    ]
    low = challenger._metrics(outcomes, cost_bps=14.5, trials_total=20, seed=1)
    high = challenger._metrics(outcomes, cost_bps=18.0, trials_total=20, seed=1)
    assert high["net_ev_bps"] < low["net_ev_bps"] < low["gross_ev_bps"]
    assert 1 <= low["n_eff"] <= low["trades"]
    assert low["multiple_testing_alpha"] == pytest.approx(0.05 / 20)


def test_sha_mismatch_fails_closed(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "staging"
    feature = root / "derived" / "features_v2" / "horizon_ms=1000" / "x.parquet"
    feature.parent.mkdir(parents=True)
    feature.write_bytes(b"not-the-declared-hash")
    manifest = tmp_path / "feature_manifest.json"
    manifest.write_text(json.dumps({
        "segments": {"x": {"status": "VERIFIED_FEATURES", "outputs": [{
            "path": feature.relative_to(root).as_posix(), "sha256": "0" * 64,
        }]}}
    }), encoding="utf-8")
    monkeypatch.setattr(challenger, "STAGING_ROOT", root)
    with pytest.raises(RuntimeError, match="FEATURE_SHA_MISMATCH"):
        challenger._dataset_contract(manifest)


def test_feature_loader_honors_memory_row_budget(tmp_path: Path, monkeypatch) -> None:
    import hashlib
    import pyarrow as pa
    import pyarrow.parquet as pq

    root = tmp_path / "staging"
    path = root / "derived" / "features_v2" / "horizon_ms=1000" / "x.parquet"
    path.parent.mkdir(parents=True)
    pq.write_table(pa.Table.from_pylist(_rows(2)), path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "segments": {"x": {"status": "VERIFIED_FEATURES", "outputs": [{
            "path": path.relative_to(root).as_posix(), "sha256": digest,
        }]}}
    }), encoding="utf-8")
    monkeypatch.setattr(challenger, "STAGING_ROOT", root)
    rows, status = challenger.load_feature_rows(manifest, max_rows=2)
    assert rows == []
    assert status["status"] == "RESOURCE_BUDGET_EXCEEDED"
    assert status["reason"] == "FEATURE_ROW_LIMIT_EXCEEDED"


def test_challenger_keeps_holdout_sealed_and_cannot_auto_promote(
    tmp_path: Path, monkeypatch,
) -> None:
    rows = _rows(80)
    monkeypatch.setattr(challenger, "load_feature_rows", lambda *args, **kwargs: (
        rows, {"status": "OK", "dataset_hash": "abc", "verified_feature_files": 4,
               "source_partition_ids": ["a", "b"], "rows": len(rows)},
    ))
    monkeypatch.setattr(challenger, "load_storage_config", lambda: {
        "challenger_max_families": 5, "challenger_max_trials": 20,
        "challenger_max_runtime_minutes": 1,
    })
    monkeypatch.setattr(challenger, "STATUS_PATH", tmp_path / "status.json")
    result = challenger.run_challenger(
        max_trials=20, max_runtime_minutes=1, report_root=tmp_path / "reports",
    )
    assert result["status"] == "COMPLETED"
    assert result["state"] in {"REJECTED", "NEED_MORE_DATA", "WATCH_ONLY"}
    assert result["holdout_access_count"] == 0
    assert result["holdout_status"] == "SEALED_NOT_EVALUATED"
    assert result["snapshot"]["holdout_evaluated"] is False
    assert result["budget"]["max_trials"] <= 80
    assert all(candidate["auto_promoted"] is False for candidate in result["candidates"])
    assert all(candidate["sealed_holdout"]["metrics"] is None for candidate in result["candidates"])
    assert all(candidate["sealed_holdout"]["access_count"] == 0 for candidate in result["candidates"])
    assert result["strategy_verdict"] == "NINGUN EDGE NUEVO VALIDADO"
    assert result["can_send_real_orders"] is False
    assert result["final_recommendation"] == "NO LIVE"


def test_insufficient_data_is_honest_not_edge(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(challenger, "load_feature_rows", lambda *args, **kwargs: (
        [], {"status": "NEED_MORE_DATA", "dataset_hash": "empty"},
    ))
    monkeypatch.setattr(challenger, "load_storage_config", lambda: {
        "challenger_max_families": 5, "challenger_max_trials": 80,
        "challenger_max_runtime_minutes": 30,
    })
    monkeypatch.setattr(challenger, "STATUS_PATH", tmp_path / "status.json")
    result = challenger.run_challenger(report_root=tmp_path / "reports")
    assert result["state"] == "NEED_MORE_DATA"
    assert result["families_tested"] == 0
    assert result["holdout_access_count"] == 0
    assert result["auto_promotion"] is False
    assert result["final_recommendation"] == "NO LIVE"


def test_challenger_source_has_no_runtime_or_order_path() -> None:
    source = (ROOT / "app" / "labs" / "continuous_edge_research_challenger.py").read_text(
        encoding="utf-8"
    )
    forbidden = (
        "place_order(", "private_get(", "private_post(", "set_leverage(",
        "set_margin_mode(", "ExecutionEngine.execute", "PaperTrader.open_position",
        "LIVE_TRADING=True", "ENABLE_PAPER_POLICY_FILTER=True",
        "can_send_real_orders=True",
    )
    assert not any(token in source for token in forbidden)
