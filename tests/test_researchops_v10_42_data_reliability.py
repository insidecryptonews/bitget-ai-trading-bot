"""V10.42 Data Reliability: segmentation, forward-only view, quality gate,
gap repair, bottleneck map. Research-only."""

from __future__ import annotations

from app.labs import data_reliability_v10_42 as DR

DAY = 86_400_000
BAR = 60_000
T2026 = 1_760_000_000_000


def _bar(ts):
    return {"ts": ts, "close": 100.0, "open": 100.0, "high": 100.0, "low": 100.0}


def test_segmentation_separates_backfill_from_forward():
    # one 2020-ish day + a 2026-ish forward run, far apart
    backfill = [_bar(T2026 - 2000 * DAY + i * BAR) for i in range(50)]
    forward = [_bar(T2026 + i * BAR) for i in range(200)]
    seg = DR.segment_dataset(backfill + forward)
    assert seg["n_segments"] == 2
    assert seg["mixed_with_backfill"] is True
    assert seg["forward_n_bars"] == 200          # most recent segment is forward


def test_forward_view_ignores_backfill_for_readiness():
    backfill = [_bar(T2026 - 2000 * DAY + i * BAR) for i in range(50)]
    forward = [_bar(T2026 + i * BAR) for i in range(200)]     # contiguous forward
    v = DR.forward_dataset_view("SYN", bars=backfill + forward)
    assert v["mixed_with_backfill"] is True
    # global coverage is wrecked by the 6-year gap, forward-only is clean
    assert v["global_coverage_ratio"] < 0.01
    assert v["forward_coverage_ratio"] == 1.0
    assert v["forward_verdict"] == "CONTINUOUS_ENOUGH"
    assert v["final_recommendation"] == "NO LIVE"


def test_data_quality_gate_flags_mixed_and_gappy():
    backfill = [_bar(T2026 - 2000 * DAY + i * BAR) for i in range(50)]
    forward = [_bar(T2026 + i * BAR) for i in range(200)]
    v = DR.forward_dataset_view("SYN", bars=backfill + forward)
    dq = DR.data_quality_gate(v)
    assert "DATASET_MIXED_WITH_BACKFILL" in dq["states"]
    assert dq["tournament_result_reliability"] in ("USABLE", "EXPLORATORY",
                                                   "NOT_RELIABLE_GAPS",
                                                   "NOT_RELIABLE_SAMPLE")
    assert dq["can_send_real_orders"] is False


def test_gap_repair_never_invents_ticks():
    forward = [_bar(T2026 + i * BAR) for i in range(50)]
    forward += [_bar(T2026 + 50 * BAR + 5 * BAR * k) for k in range(1, 20)]  # cadence gaps
    p = DR.gap_repair_plan("SYN", bars=forward)
    assert p["never_invents_ticks"] is True
    assert p["apply_supported"] is False
    assert p["verdict"] == "UNREPAIRABLE_MICROSTRUCTURE_GAP"
    assert p["gap_classes"]["rest_cadence_le10min_UNREPAIRABLE_MICRO"] >= 1


def test_insufficient_forward_data_flagged():
    v = DR.forward_dataset_view("SYN", bars=[_bar(T2026 + i * BAR) for i in range(30)])
    dq = DR.data_quality_gate(v)
    assert "INSUFFICIENT_FORWARD_DATA" in dq["states"]
    assert dq["fit_for_shadow_forward"] is False


def test_states_constants_have_no_live():
    for s in DR.DQ_STATES + DR.HEALTH_STATES:
        assert s not in ("LIVE", "LIVE_READY", "CAN_SEND_REAL_ORDERS")


# ---- V10.42 hotfix: empty dataset / unknown symbol must fail closed ----------

def test_segment_dataset_empty_returns_full_contract():
    seg = DR.segment_dataset([])
    for key in ("segments_meta", "total_n_bars", "forward_n_bars", "span_days",
                "mixed_with_backfill", "global_min_ts", "global_max_ts",
                "forward_min_ts", "forward_max_ts", "forward_coverage",
                "max_contiguous_run", "gap_count", "status", "forward",
                "n_segments"):
        assert key in seg, key
    assert seg["total_n_bars"] == 0 and seg["forward_n_bars"] == 0
    assert seg["span_days"] == 0.0 and seg["mixed_with_backfill"] is False
    assert seg["status"] == "NO_DATA"
    # None input is treated the same (no crash)
    assert DR.segment_dataset(None)["total_n_bars"] == 0


def test_forward_dataset_view_empty_does_not_crash():
    v = DR.forward_dataset_view("SYN", bars=[])
    assert v["status"] in ("NO_DATA", "INSUFFICIENT_FORWARD_DATA")
    assert v["fit_for_fine_backtest"] is False
    assert v["fit_for_shadow_forward"] is False
    assert v["forward_n_bars"] == 0
    # the quality gate on top of it is also safe
    dq = DR.data_quality_gate(v)
    assert "INSUFFICIENT_FORWARD_DATA" in dq["states"]


def test_collector_health_unknown_symbol_is_conservative(monkeypatch):
    # force load_dataset to return no bars (unknown symbol) without touching disk
    monkeypatch.setattr(DR.CE, "load_dataset", lambda *a, **k: {"bars": []})
    h = DR.collector_health("DOESNOTEXISTUSDT")
    assert h["status"] in ("NO_DATA", "COLLECTOR_DOWN", "INSUFFICIENT_FORWARD_DATA")
    assert h["can_send_real_orders"] is False


def test_bottleneck_map_empty_does_not_crash(monkeypatch):
    monkeypatch.setattr(DR.CE, "load_dataset", lambda *a, **k: {"bars": []})
    m = DR.bottleneck_map("DOESNOTEXISTUSDT")
    assert "bottlenecks" in m and m["final_recommendation"] == "NO LIVE"
