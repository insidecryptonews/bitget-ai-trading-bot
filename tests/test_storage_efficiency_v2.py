from __future__ import annotations

import gzip
import hashlib
import json
import socket
import time
import urllib.request
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from app import health_server
from app.config import BotConfig
from app.health_server import HealthState, start_health_server
from app.labs import research_dashboard_v10_43c as dashboard
from app.labs import storage_efficiency_v2 as storage


ROOT = Path(__file__).resolve().parents[1]


def _isolated_storage(tmp_path: Path, monkeypatch) -> tuple[Path, dict]:
    root = tmp_path / "external_data" / "staging" / "cross_venue_v1"
    runtime = tmp_path / "data" / "runtime" / "storage_efficiency_v2"
    analytics = root / "derived" / "analytics_v2"
    features = root / "derived" / "features_v2"
    config = {
        "schema": "storage_efficiency_v2.config.v1",
        "mode": "COMPRESSION_ONLY_NO_DELETE",
        "normalized_hot_max_bytes": 1024,
        "raw_hot_max_bytes": 1024,
        "write_partitioned_normalized_jsonl": False,
        "compression_worker_interval_seconds": 300,
        "compression_max_files_per_cycle": 1,
        "rollover_compression_max_segments_per_cycle": 1,
        "compression_retry_backoff_seconds": 300,
        "warm_compression_codec": "zstd",
        "warm_compression_level": 1,
        "analytics_max_segments_per_cycle": 1,
        "challenger_min_interval_hours": 6,
        "challenger_min_new_partitions": 1,
        "challenger_max_families": 5,
        "challenger_max_trials": 80,
        "challenger_max_runtime_minutes": 30,
        "challenger_max_feature_rows": 500000,
        "minimum_free_disk_bytes": 1,
        "materialized_feature_horizons_ms": [1000],
        "available_on_demand_horizons_ms": [10, 25, 50, 100, 250, 500],
        "parquet_compression": "zstd",
        "parquet_compression_level": 3,
        "no_delete_without_verified_remote_backup": True,
        "r2_verified": False,
        "paper_filter_enabled": False,
        "can_send_real_orders": False,
        "final_recommendation": "NO LIVE",
    }
    config_path = tmp_path / "storage.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr(storage, "STAGING_ROOT", root)
    monkeypatch.setattr(storage, "ANALYTICS_ROOT", analytics)
    monkeypatch.setattr(storage, "FEATURE_ROOT", features)
    monkeypatch.setattr(storage, "RUNTIME_ROOT", runtime)
    monkeypatch.setattr(storage, "REPORT_ROOT", tmp_path / "reports")
    monkeypatch.setattr(storage, "CONFIG_PATH", config_path)
    monkeypatch.setattr(storage, "MANIFEST_PATH", runtime / "storage_manifest.json")
    monkeypatch.setattr(storage, "STATUS_PATH", runtime / "storage_status.json")
    monkeypatch.setattr(storage, "ANALYTICS_MANIFEST_PATH", runtime / "analytics_manifest.json")
    monkeypatch.setattr(storage, "FEATURE_MANIFEST_PATH", runtime / "feature_manifest.json")
    return root, config


def _events() -> tuple[list[dict], bytes]:
    rows = [
        {
            "venue": "bybit", "canonical_symbol": "BTCUSDT", "symbol": "BTCUSDT",
            "event_type": "trade", "local_receive_wall_ms": 1_700_000_000_100 + i * 300,
            "exchange_event_ts": 1_700_000_000_000 + i * 300,
            "sequence_id": str(i + 1), "trade_id": f"trade-{i + 1}",
            "price": 100.0 + i, "size": 1.0 + i, "taker_side": "BUY",
            "source_status": "OK", "event_id": f"event-{i + 1}",
        }
        for i in range(3)
    ]
    payload = b"".join(
        (json.dumps(row, separators=(",", ":")) + "\n").encode("utf-8") for row in rows
    )
    return rows, payload


def _pending_segment(root: Path, payload: bytes, *, state: str = "CONSUMED_DERIVED_SEGMENT_PENDING_GZIP") -> tuple[Path, Path]:
    normalized = root / "bybit" / "normalized"
    segment = normalized / "consumed_hot_segments" / "stream_test.jsonl"
    segment.parent.mkdir(parents=True)
    segment.write_bytes(payload)
    manifest_path = normalized / "rollover_manifest.json"
    manifest_path.write_text(json.dumps({
        "segments": [{
            "segment_path": segment.relative_to(root / "bybit").as_posix(),
            "stream_bytes": len(payload), "state": state,
        }],
    }), encoding="utf-8")
    return segment, manifest_path


def test_config_and_safety_are_fail_closed(tmp_path: Path, monkeypatch) -> None:
    _, config = _isolated_storage(tmp_path, monkeypatch)
    loaded = storage.load_storage_config()
    assert loaded["mode"] == "COMPRESSION_ONLY_NO_DELETE"
    assert loaded["no_delete_without_verified_remote_backup"] is True
    assert loaded["r2_verified"] is False
    assert storage.research_safety()["delete_allowed"] is False
    config["can_send_real_orders"] = True
    storage.CONFIG_PATH.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(ValueError, match="REAL_ORDERS_BLOCKED"):
        storage.load_storage_config()


def test_line_contract_rejects_partial_and_corrupt_jsonl(tmp_path: Path) -> None:
    partial = tmp_path / "partial.jsonl"
    partial.write_bytes(b'{"ok":true}\n{"partial":')
    with pytest.raises(ValueError, match="PARTIAL_JSONL_LINE"):
        storage._line_contract(partial)
    corrupt = tmp_path / "corrupt.jsonl"
    corrupt.write_bytes(b"not-json\n")
    with pytest.raises(ValueError, match="CORRUPT_JSONL"):
        storage._line_contract(corrupt)


def test_rollover_gzip_roundtrip_replaces_only_derived_source(
    tmp_path: Path, monkeypatch,
) -> None:
    root, _ = _isolated_storage(tmp_path, monkeypatch)
    _, payload = _events()
    segment, manifest_path = _pending_segment(root, payload)
    raw = root / "bybit" / "raw" / "2026-07-18" / "frames.jsonl"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b'{"raw":true}\n')
    result = storage.compress_pending_rollover_segments(
        root=root, apply=True, max_segments=1,
    )
    compressed = segment.with_suffix(".jsonl.zst")
    assert result["status"] == "COMPLETED"
    assert result["raw_deleted"] is False
    assert not segment.exists() and compressed.is_file()
    assert raw.read_bytes() == b'{"raw":true}\n'
    assert b"".join(storage._iter_compressed_lines(compressed)) == payload
    row = json.loads(manifest_path.read_text(encoding="utf-8"))["segments"][0]
    assert row["state"] == "VERIFIED_COMPRESSED_DERIVED_SEGMENT"
    assert row["compression_codec"] == "zstd"
    assert row["raw_sha256"] == hashlib.sha256(payload).hexdigest()
    assert row["raw_audit_sources_untouched"] is True


def test_transparent_compression_queue_skips_already_verified_files(
    tmp_path: Path, monkeypatch,
) -> None:
    root, _ = _isolated_storage(tmp_path, monkeypatch)
    closed = root / "bybit" / "raw" / "2026-07-17" / "frames.jsonl"
    closed.parent.mkdir(parents=True)
    closed.write_bytes(b'{"event_id":"one"}\n')
    contract = storage._line_contract(closed)
    manifest = {
        "schema": "storage_efficiency_v2.manifest.v1",
        "partitions": {closed.relative_to(root).as_posix(): {
            **contract, "status": "ALREADY_COMPRESSED",
        }},
    }
    storage.MANIFEST_PATH.parent.mkdir(parents=True)
    storage.MANIFEST_PATH.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(storage, "_is_ntfs_compressed", lambda path: path == closed)
    result = storage.compress_closed_partitions(root=root, apply=False, max_files=8)
    assert result["candidate_files"] == 0
    assert result["rows"] == []


def test_status_does_not_claim_hash_pass_with_partial_manifest(
    tmp_path: Path, monkeypatch,
) -> None:
    root, _ = _isolated_storage(tmp_path, monkeypatch)
    closed = root / "bybit" / "raw" / "2026-07-17" / "frames.jsonl"
    closed.parent.mkdir(parents=True)
    closed.write_bytes(b'{"event_id":"one"}\n')
    monkeypatch.setattr(storage, "_physical_bytes", lambda path: path.stat().st_size)
    monkeypatch.setattr(storage, "_is_ntfs_compressed", lambda path: True)
    monkeypatch.setattr(storage.shutil, "disk_usage", lambda path: type(
        "Disk", (), {"free": 10_000_000, "total": 20_000_000, "used": 10_000_000}
    )())
    result = storage.storage_status()
    assert result["manifest_verification_queue"] == 1
    assert result["manifest_status"] == "PARTIAL"
    assert result["hash_verification"] == "NEED_MORE_DATA"


def test_failed_gzip_retains_source_and_obeys_retry_backoff(
    tmp_path: Path, monkeypatch,
) -> None:
    root, _ = _isolated_storage(tmp_path, monkeypatch)
    segment, manifest_path = _pending_segment(root, b'{"partial":true}')
    with pytest.raises(ValueError, match="PARTIAL_ROLLOVER_LINE"):
        storage.compress_pending_rollover_segments(root=root, apply=True, max_segments=1)
    assert segment.is_file()
    row = json.loads(manifest_path.read_text(encoding="utf-8"))["segments"][0]
    assert row["state"] == "COMPRESSION_ERROR_DERIVED_SEGMENT_RETAINED"
    retry = storage.compress_pending_rollover_segments(
        root=root, apply=False, max_segments=1, retry_backoff_seconds=300,
    )
    assert retry["pending_segments"] == 0


def test_parquet_preserves_exact_source_rows_and_feature_cutoff(
    tmp_path: Path, monkeypatch,
) -> None:
    root, _ = _isolated_storage(tmp_path, monkeypatch)
    rows, payload = _events()
    segment, _ = _pending_segment(root, payload)
    storage.compress_pending_rollover_segments(root=root, apply=True, max_segments=1)
    result = storage.compact_verified_segments(root=root, apply=True, max_segments=1)
    assert result["status"] == "COMPLETED"
    output = result["segments"][0]["outputs"][0]
    parquet_path = root / output["path"]
    table = pq.read_table(parquet_path).to_pylist()
    assert [row["source_row_index"] for row in table] == [0, 1, 2]
    assert [json.loads(row["source_json"]) for row in table] == rows
    assert segment.with_suffix(".jsonl.zst").is_file()
    assert result["segments"][0]["source_preserved"] is True

    features = storage.build_incremental_features(apply=True, max_segments=1)
    assert features["status"] == "COMPLETED"
    feature_path = root / features["segments"][0]["outputs"][0]["path"]
    feature_rows = pq.read_table(feature_path).to_pylist()
    assert feature_rows
    assert all(row["first_event_timestamp_ms"] <= row["causal_cutoff_ms"] for row in feature_rows)
    assert all(row["last_event_timestamp_ms"] == row["causal_cutoff_ms"] for row in feature_rows)
    assert all(row["source_partition_id"] for row in feature_rows)
    second = storage.build_incremental_features(apply=True, max_segments=1)
    assert second["status"] == "NO_NEW_SEGMENTS"


def test_health_artifact_and_dashboard_panels_are_read_only(
    tmp_path: Path, monkeypatch,
) -> None:
    status = tmp_path / "storage.json"
    status.write_text(json.dumps({
        "status": "OK", "mode": "COMPRESSION_ONLY_NO_DELETE",
        "delete_allowed": False, "raw_hot_bytes": 10,
    }), encoding="utf-8")
    payload = health_server._research_status_artifact(status, default_status="NEED_MORE_DATA")
    assert payload["status"] == "OK"
    assert payload["research_only"] is True
    assert payload["paper_filter_enabled"] is False
    assert payload["can_send_real_orders"] is False
    assert payload["final_recommendation"] == "NO LIVE"
    source = (ROOT / "app" / "health_server.py").read_text(encoding="utf-8")
    assert source.count('"/api/research/storage-efficiency-v2"') == 2
    assert source.count('"/api/research/continuous-edge-challenger"') == 2
    storage_html = dashboard._panel_storage_efficiency({
        "storage_efficiency_v2": {"status": payload, "scheduler": {}},
    })
    challenger_html = dashboard._panel_continuous_challenger({
        "continuous_edge_challenger": {"state": "NEED_MORE_DATA"},
    })
    assert "COMPRESSION_ONLY_NO_DELETE" in storage_html
    assert "Raw audit evidence is never deleted" in storage_html
    assert "no automatic promotion" in challenger_html


def test_http_status_endpoints_read_artifacts_without_running_heavy_work(
    tmp_path: Path, monkeypatch,
) -> None:
    storage_status = tmp_path / "storage.json"
    challenger_status = tmp_path / "challenger.json"
    storage_status.write_text(json.dumps({
        "status": "OK", "storage_mode": "COMPRESSION_ONLY_NO_DELETE",
        "delete_allowed": False,
    }), encoding="utf-8")
    challenger_status.write_text(json.dumps({
        "status": "COMPLETED", "state": "REJECTED", "holdout_access_count": 0,
    }), encoding="utf-8")
    monkeypatch.setattr(health_server, "_STORAGE_EFFICIENCY_V2_STATUS", storage_status)
    monkeypatch.setattr(
        health_server, "_CONTINUOUS_EDGE_CHALLENGER_STATUS", challenger_status,
    )
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])

    class Logger:
        def info(self, *args, **kwargs):
            return None

    start_health_server(
        HealthState(mode="paper"), port, Logger(), config=BotConfig(), host="127.0.0.1",
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(30):
        try:
            urllib.request.urlopen(base + "/health", timeout=2).close()  # noqa: S310
            break
        except OSError:
            time.sleep(0.05)
    for route in (
        "/api/research/storage-efficiency-v2",
        "/api/research/continuous-edge-challenger",
    ):
        with urllib.request.urlopen(base + route, timeout=2) as response:  # noqa: S310
            payload = json.loads(response.read())
        assert payload["research_only"] is True
        assert payload["paper_filter_enabled"] is False
        assert payload["can_send_real_orders"] is False
        assert payload["final_recommendation"] == "NO LIVE"


def test_scheduler_is_bounded_low_priority_and_never_trades() -> None:
    source = (ROOT / "scripts" / "run_storage_edge_scheduler.ps1").read_text(encoding="utf-8")
    assert "BelowNormal" in source
    assert "MaxCycles" in source
    assert "minimum_free_disk_bytes" in source
    assert "heavy_research" in source
    assert "storage-efficiency-cycle-v2" in source
    assert "continuous-edge-challenger-v2" in source
    assert "can_send_real_orders" in source
    forbidden = ("place_order", "private_post", "set_leverage", "set_margin_mode")
    assert not any(token in source for token in forbidden)
