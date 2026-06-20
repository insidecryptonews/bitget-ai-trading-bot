"""ResearchOps V10.6 — adversarial tests for the local-first live-readiness
foundation. Everything here is offline/pure: no network, no DB, no live. The
core invariant under test: NOTHING in V10.6 can flip paper_ready/live_ready or
authorize real orders, and every gate is fail-closed."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.labs import readiness_gates_v10_6 as RG
from app.labs.provider_registry_v10_6 import (
    REC_PREFERRED_SAMPLE,
    ST_VERIFIED,
    build_provider_matrix,
    run_provider_matrix_v106,
)
from app.labs.provider_sample_validator_v10_6 import (
    CLS_INTERMEDIATE,
    CLS_LONG_READY,
    build_sample_manifest,
    validate_sample_dir,
)
from app.labs.data_foundation_v10_5 import (
    MANIFEST_V105_REQUIRED_FIELDS,
    SCHEMA_VERSION,
)
from app.labs.real_replay_backtester_v10_6 import (
    BR_NEED_CONTENT_VALIDATION,
    BR_NEED_DATA,
    BR_NEED_LONG_HISTORY,
    BR_NEED_VALID_MANIFEST,
    BR_READY,
    CostModel,
    evaluate_backtester_readiness,
    replay_backtester_contract,
    run_replay_research,
    simulate_position,
)

# A manifest shape that genuinely PASSES the real V10.5.6 gate (mirrors the
# fixture used by the V10.5 suite). Backtester readiness must re-derive
# promotability from this — never from declared fields.
_VALID_RANGE = {"start": "2025-06-11T00:00:00Z", "end": "2026-06-11T00:00:00Z"}


def _valid_manifest_v105(**over):
    man = {f: 0 for f in MANIFEST_V105_REQUIRED_FIELDS}
    man.update({
        "source_provider": "tardis_dev", "license_terms": "research",
        "requested_range": dict(_VALID_RANGE),
        "actual_covered_range": dict(_VALID_RANGE),
        "symbols": ["BTCUSDT"], "timeframes": ["1h"],
        "data_types": ["ohlcv", "open_interest", "funding", "liquidations"],
        "rows_by_type": {"ohlcv": 8760, "open_interest": 8760,
                         "funding": 1095, "liquidations": 5000},
        "missing_oi_ratio": 0.02, "missing_oi_status": "DATA_OK",
        "gap_count": 0, "duplicate_count": 0,
        "coverage_ratio": 0.97, "clean_days": 365.0,
        "checksums_sha256": {
            "BTCUSDT_1h_ohlcv.csv": "ab" * 32,
            "BTCUSDT_open_interest.csv": "cd" * 32,
            "BTCUSDT_funding.csv": "ef" * 32,
            "BTCUSDT_liquidations.csv": "12" * 32,
        },
        "explicit_human_authorization": True,
        "paid_download_authorized": True,
        "license_terms_confirmed": True,
        "authorization_reference": "HUMAN-V106-TEST-001",
        "missing_funding_ratio": 0.01,
        "missing_liquidations_ratio": 0.02,
        "timezone": "UTC", "timestamp_unit": "unix_ms",
        "generated_at": "2026-06-11T00:00:00Z",
        "schema_version": SCHEMA_VERSION,
        "import_status": "STAGED_READY_FOR_PROMOTE",
        "files": [
            {"path": "BTCUSDT_1h_ohlcv.parquet", "data_type": "ohlcv",
             "sha256": "ab" * 32, "rows": 8760},
            {"path": "BTCUSDT_open_interest.parquet", "data_type": "open_interest",
             "sha256": "cd" * 32, "rows": 8760},
            {"path": "BTCUSDT_funding.parquet", "data_type": "funding",
             "sha256": "ef" * 32, "rows": 1095},
            {"path": "BTCUSDT_liquidations.parquet", "data_type": "liquidations",
             "sha256": "12" * 32, "rows": 5000},
        ],
    })
    man.update(over)
    return man

DAY = 86_400_000
T0 = 1_700_000_000_000

V106_MODULES = [
    "app/labs/provider_registry_v10_6.py",
    "app/labs/provider_sample_validator_v10_6.py",
    "app/labs/real_replay_backtester_v10_6.py",
    "app/labs/readiness_gates_v10_6.py",
]


def _write_csv(path: Path, header: str, rows: list[str]) -> None:
    path.write_text(header + "\n" + "\n".join(rows) + "\n", encoding="utf-8")


def _ohlcv_rows(n: int, step_ms: int = DAY, bad_at: int | None = None,
                dup_at: int | None = None, skip_at: int | None = None) -> list[str]:
    rows = []
    t = T0
    for i in range(n):
        ts = t
        if dup_at is not None and i == dup_at:
            ts = T0 + (i - 1) * step_ms  # repeat previous timestamp
        elif skip_at is not None and i >= skip_at:
            ts = T0 + (i + 1) * step_ms  # introduce a one-bar gap
        else:
            ts = T0 + i * step_ms
        if bad_at is not None and i == bad_at:
            rows.append(f"{ts},100,80,90,85,1000")  # high < low => invalid
        else:
            rows.append(f"{ts},100,110,90,105,1000")
    return rows


# --------------------------------------------------------------------------
# A. Provider matrix
# --------------------------------------------------------------------------

def test_provider_matrix_no_verified_and_no_live():
    rep = run_provider_matrix_v106().as_dict()
    assert rep["preferred_sample_candidate"] == "tardis_dev"
    assert rep["any_verified"] is False
    assert rep["any_paid_download_authorized"] is False
    assert rep["no_network_calls"] is True
    assert rep["paper_ready"] is False and rep["live_ready"] is False
    assert rep["final_recommendation"] == "NO LIVE"
    # No provider may be pre-marked verified, and every provider carries the
    # offline safety contract.
    for p in rep["providers"]:
        assert p["integration_status"] != ST_VERIFIED
        assert p["safety"]["no_auto_download"] is True
        assert p["safety"]["no_paid_download"] is True
        assert p["safety"]["no_env_write"] is True


def test_provider_matrix_marketing_claims_not_facts():
    providers = {p.provider_id: p for p in build_provider_matrix()}
    tardis = providers["tardis_dev"]
    assert tardis.recommendation == REC_PREFERRED_SAMPLE
    # claimed history must never be presented as a confirmed number.
    assert "NEEDS_MANUAL_VERIFICATION" in str(tardis.expected_history_days_paid)


# --------------------------------------------------------------------------
# B. Sample validator
# --------------------------------------------------------------------------

def test_validate_missing_dir():
    rep = validate_sample_dir("does/not/exist", expected_days=180)
    assert "sample_dir_not_found" in rep["blockers"]
    assert rep["sample_ready"] is False
    assert rep["paper_ready"] is False and rep["live_ready"] is False


def test_validate_percent_encoded_path_blocked(tmp_path):
    # V10.5.6 path safety must reject any '%' in a declared dataset path.
    _write_csv(tmp_path / "BTCUSDT_1d_ohlcv%2e.csv",
               "timestamp,open,high,low,close,volume", _ohlcv_rows(10))
    rep = validate_sample_dir(str(tmp_path), expected_days=5)
    assert any(b.startswith("invalid_file_path") for b in rep["blockers"]), rep["blockers"]
    assert rep["sample_ready"] is False


def test_validate_real_sha256_and_duplicate_sha_blocked(tmp_path):
    rows = _ohlcv_rows(20)
    _write_csv(tmp_path / "BTCUSDT_1d_ohlcv.csv",
               "timestamp,open,high,low,close,volume", rows)
    # identical bytes, different recognized data_type => duplicate sha blocker.
    _write_csv(tmp_path / "BTCUSDT_1d_funding.csv",
               "timestamp,open,high,low,close,volume", rows)
    rep = validate_sample_dir(str(tmp_path), expected_days=10)
    assert len(rep["dataset_hash"]) == 64  # real sha256 hex digest
    assert "duplicate_file_sha256_across_data_types" in rep["blockers"]


def test_validate_duplicate_timestamps_blocked(tmp_path):
    _write_csv(tmp_path / "BTCUSDT_1d_ohlcv.csv",
               "timestamp,open,high,low,close,volume",
               _ohlcv_rows(20, dup_at=10))
    rep = validate_sample_dir(str(tmp_path), expected_days=10)
    assert any(b.startswith("duplicate_timestamps") for b in rep["blockers"])


def test_validate_gaps_counted(tmp_path):
    _write_csv(tmp_path / "BTCUSDT_1d_ohlcv.csv",
               "timestamp,open,high,low,close,volume",
               _ohlcv_rows(30, skip_at=15))
    rep = validate_sample_dir(str(tmp_path), expected_days=20)
    assert rep["quality"]["total_gap_count"] >= 1


def test_validate_ohlcv_invalid_rows_blocked(tmp_path):
    _write_csv(tmp_path / "BTCUSDT_1d_ohlcv.csv",
               "timestamp,open,high,low,close,volume",
               _ohlcv_rows(20, bad_at=5))
    rep = validate_sample_dir(str(tmp_path), expected_days=10)
    assert any(b.startswith("ohlcv_invalid_rows") for b in rep["blockers"])


def test_validate_oi_clustered_missing_blocked(tmp_path):
    rows = []
    for i in range(40):
        ts = T0 + i * DAY
        oi = "" if 10 <= i < 18 else "12345"  # 8 consecutive missing => clustered
        rows.append(f"{ts},{oi}")
    _write_csv(tmp_path / "BTCUSDT_1d_oi.csv", "timestamp,open_interest", rows)
    rep = validate_sample_dir(str(tmp_path), expected_days=20)
    assert "oi_missing_clustered" in rep["blockers"]


def test_validate_84d_is_intermediate(tmp_path):
    _write_csv(tmp_path / "BTCUSDT_1d_ohlcv.csv",
               "timestamp,open,high,low,close,volume", _ohlcv_rows(84))
    rep = validate_sample_dir(str(tmp_path), expected_days=180)
    assert rep["data_classification"] == CLS_INTERMEDIATE
    assert rep["paper_ready"] is False and rep["live_ready"] is False


def test_validate_200d_is_long_history_ready_but_never_live(tmp_path):
    _write_csv(tmp_path / "BTCUSDT_1d_ohlcv.csv",
               "timestamp,open,high,low,close,volume", _ohlcv_rows(200))
    rep = validate_sample_dir(str(tmp_path), expected_days=180)
    assert rep["data_classification"] == CLS_LONG_READY
    # required types still missing => not sample_ready, and never live.
    assert rep["paper_ready"] is False and rep["live_ready"] is False


# --------------------------------------------------------------------------
# C. Manifest builder
# --------------------------------------------------------------------------

def test_manifest_is_staged_and_human_gated(tmp_path):
    _write_csv(tmp_path / "BTCUSDT_1d_ohlcv.csv",
               "timestamp,open,high,low,close,volume", _ohlcv_rows(200))
    man = build_sample_manifest(str(tmp_path), expected_days=180,
                                provider_id="tardis_dev", write=False)
    assert man["import_status"] == "STAGED"
    assert man["explicit_human_authorization"] is False
    assert man["paid_download_authorized"] is False
    assert man["gate_promote_allowed"] is False
    assert man["written_path"] == ""  # not written unless write=True
    assert man["paper_ready"] is False and man["live_ready"] is False
    assert man["final_recommendation"] == "NO LIVE"


def test_manifest_write_goes_to_reports_dir_not_raw(tmp_path):
    _write_csv(tmp_path / "BTCUSDT_1d_ohlcv.csv",
               "timestamp,open,high,low,close,volume", _ohlcv_rows(50))
    man = build_sample_manifest(str(tmp_path), expected_days=30,
                                provider_id="tardis_dev", write=True)
    wp = man["written_path"]
    try:
        assert wp == "" or "reports" in wp.replace("\\", "/")
        assert wp == "" or "/raw/" not in wp.replace("\\", "/")
    finally:
        if wp and os.path.isfile(wp):
            os.remove(wp)


# --------------------------------------------------------------------------
# D. Backtester readiness
# --------------------------------------------------------------------------

_NOT_READY = {BR_NEED_DATA, BR_NEED_LONG_HISTORY, BR_NEED_CONTENT_VALIDATION,
              BR_NEED_VALID_MANIFEST}


def test_backtester_readiness_no_manifest_needs_data():
    r = evaluate_backtester_readiness(None).as_dict()
    assert r["status"] == BR_NEED_DATA
    assert r["paper_ready"] is False and r["live_ready"] is False


def test_backtester_readiness_crafted_minimal_manifest_is_not_ready():
    # The Codex bug: a 3-field self-declared manifest must NOT be READY.
    r = evaluate_backtester_readiness(
        {"clean_days": 200, "missing_oi_status": "DATA_OK",
         "promote_allowed": True}).as_dict()
    assert r["status"] != BR_READY
    assert r["status"] in (BR_NEED_VALID_MANIFEST, BR_NEED_CONTENT_VALIDATION)
    assert r["manifest_promotable"] is False
    assert "manifest_not_promotable" in r["blockers"]
    assert "declared_readiness_ignored" in r["blockers"]
    assert r["paper_ready"] is False and r["live_ready"] is False


def test_backtester_readiness_declared_valid_flags_without_inventory_blocked():
    r = evaluate_backtester_readiness(
        {"valid_manifest_v105": True, "promote_allowed": True,
         "clean_days": 365, "missing_oi_status": "DATA_OK"}).as_dict()
    assert r["status"] in _NOT_READY and r["status"] != BR_READY
    assert r["manifest_promotable"] is False
    assert "declared_readiness_ignored" in r["blockers"]


def test_backtester_readiness_declared_paper_live_ready_is_ignored():
    r = evaluate_backtester_readiness(
        {"paper_ready": True, "live_ready": True, "clean_days": 365,
         "missing_oi_status": "DATA_OK"}).as_dict()
    assert r["status"] != BR_READY
    assert r["manifest_promotable"] is False
    assert r["paper_ready"] is False and r["live_ready"] is False
    assert "declared_readiness_ignored" in r["blockers"]


def test_backtester_readiness_unsafe_path_in_files_blocked_via_gate():
    bad = _valid_manifest_v105()
    bad["files"][0]["path"] = "BTCUSDT_1h_ohlcv%2e.parquet"  # percent-encoded
    r = evaluate_backtester_readiness(bad).as_dict()
    assert r["status"] != BR_READY
    assert r["manifest_promotable"] is False
    assert any("manifest_gate" in b for b in r["blockers"])


def test_backtester_readiness_84d_declaring_ready_never_ready():
    short_range = {"start": "2026-03-19T00:00:00Z", "end": "2026-06-11T00:00:00Z"}
    m = _valid_manifest_v105(
        requested_range=dict(short_range), actual_covered_range=dict(short_range),
        clean_days=84.0, coverage_ratio=0.9, status="READY")
    r = evaluate_backtester_readiness(m).as_dict()
    assert r["status"] != BR_READY
    assert r["status"] in (BR_NEED_LONG_HISTORY, BR_NEED_CONTENT_VALIDATION,
                           BR_NEED_VALID_MANIFEST)
    assert "declared_readiness_ignored" in r["blockers"]


def test_backtester_readiness_healthy_authorized_manifest_is_ready_but_not_live():
    r = evaluate_backtester_readiness(_valid_manifest_v105()).as_dict()
    assert r["status"] == BR_READY
    assert r["manifest_promotable"] is True
    assert r["blockers"] == []
    # READY is for *research replay* — it still does not authorize paper/live.
    assert r["paper_ready"] is False and r["live_ready"] is False
    assert r["final_recommendation"] == "NO LIVE"


# --------------------------------------------------------------------------
# E. Replay skeleton — no-lookahead + worst-case
# --------------------------------------------------------------------------

def test_replay_needs_validated_data():
    rep = run_replay_research(bars_by_symbol=None, signals=None)
    assert rep["status"] == "NEED_VALIDATED_DATA"
    assert rep["paper_ready"] is False and rep["live_ready"] is False


def test_simulate_entry_is_next_bar_not_signal_bar():
    bars = [{"open": 100, "high": 101, "low": 99, "close": 100} for _ in range(6)]
    res = simulate_position(bars, 0, side="LONG", tp_pct=0.5, sl_pct=0.5,
                            time_limit_bars=4, costs=CostModel())
    assert res is not None
    assert res["entry_idx"] == 1  # latency_bars defaults to 1 => next bar


def test_simulate_cannot_fill_at_last_bar():
    bars = [{"open": 100, "high": 101, "low": 99, "close": 100} for _ in range(3)]
    assert simulate_position(bars, 2, side="LONG", tp_pct=0.01, sl_pct=0.01,
                             time_limit_bars=4, costs=CostModel()) is None


def test_simulate_same_bar_tp_and_sl_assumes_worst_case_sl():
    bars = [{"open": 100, "high": 101, "low": 99, "close": 100} for _ in range(5)]
    # entry bar (idx 1) spans both TP (+1%) and SL (-1%) => worst case = SL.
    bars[1] = {"open": 100, "high": 102, "low": 98, "close": 100}
    res = simulate_position(bars, 0, side="LONG", tp_pct=0.01, sl_pct=0.01,
                            time_limit_bars=3, costs=CostModel())
    assert res["exit_reason"] == "SL"
    assert res["net_ret"] < 0


def test_replay_cost_stress_monotonic_non_increasing():
    bars = [{"open": 100, "high": 110, "low": 99, "close": 105} for _ in range(6)]
    signals = [{"symbol": "BTCUSDT", "signal_idx": 0, "side": "LONG",
                "tp_pct": 0.03, "sl_pct": 0.05, "time_limit_bars": 4}]
    rep = run_replay_research(bars_by_symbol={"BTCUSDT": bars}, signals=signals)
    assert rep["status"] == "REPLAY_RESEARCH_COMPLETE"
    stress = rep["cost_x1_x2_x3_stress"]
    assert stress["x1"] >= stress["x2"] >= stress["x3"]
    assert rep["paper_ready"] is False and rep["live_ready"] is False


def test_replay_contract_never_includes_live():
    c = replay_backtester_contract()
    assert "lookahead" in c["never"] and "real_orders" in c["never"]
    assert c["paper_ready"] is False and c["live_ready"] is False


# --------------------------------------------------------------------------
# F-L. Readiness gates — fail-closed; paper/live never flip
# --------------------------------------------------------------------------

def test_edge_hunter_empty_is_blocked():
    rep = RG.edge_hunter_readiness()
    assert rep["status"] == "EDGE_NOT_READY"
    assert rep["blockers"]
    assert rep["paper_ready"] is False and rep["live_ready"] is False


def test_edge_hunter_full_evidence_is_research_ready_but_not_live():
    rep = RG.edge_hunter_readiness({
        "clean_days": 365, "samples": 500, "net_ev": 0.01, "net_pf": 1.5,
        "time_death": 0.4, "fees_x2_stress_pass": True,
        "slippage_stress_pass": True, "oos_pass": True,
        "walk_forward_stable": True, "anti_overfit_pass": True,
    })
    assert rep["status"] == "EDGE_RESEARCH_READY"
    assert rep["blockers"] == []
    # research-ready is NOT paper/live ready.
    assert rep["paper_ready"] is False and rep["live_ready"] is False


def test_walk_forward_gate():
    assert RG.walk_forward_readiness()["status"] == "WALK_FORWARD_NOT_READY"
    ok = RG.walk_forward_readiness({"dataset_validated": True, "labels": 500})
    assert ok["status"] == "WALK_FORWARD_CONTRACT_OK"
    assert ok["paper_ready"] is False and ok["live_ready"] is False


def test_meta_model_gate_and_no_runtime_activation():
    blocked = RG.meta_model_readiness()
    assert blocked["status"] == "META_MODEL_NOT_READY"
    assert blocked["activation"]["ENABLE_META_MODEL"] is False
    ready = RG.meta_model_readiness({
        "samples": 1000, "positives": 200, "negatives": 200,
        "leakage_checked": True, "calibrated": True, "oos_improvement": True,
        "net_ev_improvement_after_filter": True})
    assert ready["status"] == "META_MODEL_RESEARCH_READY"
    assert ready["activation"]["runtime_filter"] is False
    assert ready["paper_ready"] is False and ready["live_ready"] is False


def test_forecast_lab_is_future_offline_only():
    rep = RG.forecast_lab_readiness()
    assert rep["status"] == "FORECAST_LAB_FUTURE_OFFLINE_ONLY"
    assert rep["implemented"] is False
    assert rep["paper_ready"] is False and rep["live_ready"] is False


def test_paper_readiness_is_never_ready_even_with_perfect_state():
    perfect = {
        "clean_days": 9999, "content_validation_pass": True,
        "backtester_ready": True, "oos_pass": True, "walk_forward_pass": True,
        "has_edge_candidates": True, "missing_oi_clustered": False,
        "net_ev": 1.0, "paper_policy_disabled": False,
    }
    rep = RG.paper_readiness(perfect)
    assert rep["status"] == "PAPER_NOT_READY"
    assert rep["paper_ready"] is False and rep["live_ready"] is False


def test_live_readiness_is_never_ready_even_with_all_flags_true():
    everything = {k: True for k in [
        "paper_profitable_sustained", "min_paper_days_met", "min_paper_trades_met",
        "paper_net_ev_positive", "drawdown_within_limits", "no_duplicate_worker",
        "kill_switches_present", "manual_approval", "micro_live_risk_rules",
        "exchange_key_permissions_audited", "rollback_plan", "monitoring_alerts"]}
    rep = RG.live_readiness(everything)
    assert rep["status"] == "LIVE_NOT_READY"
    assert rep["live_audit_ready"] is False
    assert rep["can_send_real_orders"] is False
    assert rep["live_ready"] is False


def test_risk_framework_is_contract_only_not_active():
    rep = RG.risk_framework_contract()
    assert rep["status"] == "RISK_FRAMEWORK_CONTRACT_ONLY_NOT_ACTIVE"
    assert rep["paper_ready"] is False and rep["live_ready"] is False


# --------------------------------------------------------------------------
# Safety: static source scan of all V10.6 modules
# --------------------------------------------------------------------------

@pytest.mark.parametrize("rel", V106_MODULES)
def test_module_has_no_dangerous_primitives(rel):
    src = Path(rel).read_text(encoding="utf-8")
    # Scan for actual dangerous *operations/imports*, not documentation that
    # forbids them (the forecast lab legitimately names torch/jax/tensorflow/
    # timesfm in a "no new heavy deps" policy string).
    forbidden = [
        "place_order", "create_order", "private_get", "private_post",
        "set_leverage", "set_margin_mode", "PaperTrader.open_position",
        "import torch", "from torch", "import jax", "from jax",
        "import tensorflow", "from tensorflow", "import timesfm", "from timesfm",
        "load_dotenv", "import httpx", "import requests", "import socket",
        "urllib.request", "websocket", "import ccxt",
    ]
    for token in forbidden:
        assert token not in src, f"{rel} must not contain {token!r}"


@pytest.mark.parametrize("rel", V106_MODULES)
def test_module_declares_no_live(rel):
    src = Path(rel).read_text(encoding="utf-8")
    assert "FINAL_RECOMMENDATION_NO_LIVE" in src
