"""ResearchOps V10.24 - Microstructure Sample Adapter tests.

Offline-only. Verifies schema detection, per-type validation, the readiness
classification, path-safety (no .env/db/raw/backups/vault/traversal),
staging-only normalization, CLI isolation, and the NO-LIVE invariants.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

import pytest

from app import research_lab
from app.labs import microstructure_sample_adapter_v10_24 as M

MODULE_PATH = "app/labs/microstructure_sample_adapter_v10_24.py"
B = 1_700_000_000_000  # ms
DAY = M.DAY_MS


def _write(path: Path, header, rows):
    lines = [",".join(header)] + [",".join(str(c) for c in r) for r in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _trades(side=True, n=1200, span_days=35, symbol="BTCUSDT"):
    rows = []
    for i in range(n):
        ts = B + int(i * span_days * DAY / n)
        base = [ts, symbol, 50000 + (i % 100), 0.5]
        rows.append(base + (["buy" if i % 2 else "sell"] if side else []))
    header = ["timestamp", "symbol", "price", "size"] + (["aggressor_side"] if side else [])
    return header, rows


# ---- plan -----------------------------------------------------------------

def test_plan_no_network_no_writes_no_live():
    p = M.microstructure_plan()
    assert p["uses_network"] is False and p["uses_db"] is False and p["writes_on_plan"] is False
    assert p["reads_only_local_files"] is True
    assert p["paper_ready"] is False and p["live_ready"] is False
    assert p["can_send_real_orders"] is False and p["final_recommendation"] == "NO LIVE"
    txt = json.dumps(p)
    for banned in ("PAPER_READY", "LIVE_READY", "APPROVED_FOR_LIVE", "APPROVED_FOR_PAPER"):
        assert banned not in txt


# ---- empty / detection ----------------------------------------------------

def test_validate_empty_is_no_sample(tmp_path):
    rep = M.validate_sample(str(tmp_path))
    assert rep["classification"]["verdict"] == M.C_NO_SAMPLE
    assert rep["final_recommendation"] == "NO LIVE" and rep["uses_network"] is False


def test_trades_with_aggressor_detected(tmp_path):
    h, r = _trades(side=True)
    _write(tmp_path / "trades.csv", h, r)
    rep = M.validate_sample(str(tmp_path))
    tr = rep["by_type"].get("trades", {})
    assert tr.get("valid") is True and tr.get("has_aggressor_side") is True
    assert tr["buy_sell_imbalance"] is not None


def test_trades_without_aggressor_needs_aggressor(tmp_path):
    h, r = _trades(side=False, span_days=35)   # enough history so the gap is aggressor, not history
    _write(tmp_path / "trades.csv", h, r)
    rep = M.validate_sample(str(tmp_path))
    assert rep["by_type"]["trades"]["has_aggressor_side"] is False
    assert rep["classification"]["verdict"] == M.C_NEEDS_AGGRESSOR
    assert M.C_NEEDS_AGGRESSOR in rep["classification"]["gaps"]


def test_orderbook_crossed_is_invalid(tmp_path):
    h = ["timestamp", "symbol", "bid_price_1", "bid_size_1", "ask_price_1", "ask_size_1"]
    rows = [[B + i * 1000, "BTCUSDT", 50010, 1, 50000, 1] for i in range(20)]  # bid>=ask
    _write(tmp_path / "orderbook_l2.csv", h, rows)
    rep = M.validate_sample(str(tmp_path))
    ob = rep["by_type"].get("orderbook", {})
    assert ob.get("valid") is False and ob.get("crossed_book_rows", 0) > 0


def test_orderbook_valid_spread_computed(tmp_path):
    h = ["timestamp", "symbol", "bid_price_1", "bid_size_1", "ask_price_1", "ask_size_1"]
    rows = [[B + i * 1000, "BTCUSDT", 49995, 1, 50005, 1] for i in range(20)]
    _write(tmp_path / "orderbook_l2.csv", h, rows)
    rep = M.validate_sample(str(tmp_path))
    ob = rep["by_type"]["orderbook"]
    assert ob["valid"] is True and ob["spread_median"] is not None and ob["depth_levels"] >= 1
    assert ob["l1_imbalance_median"] == 0
    assert ob["l5_imbalance_median"] == 0


def test_liquidations_schema_detected(tmp_path):
    h = ["timestamp", "symbol", "side", "price", "size", "notional"]
    rows = [[B + i * DAY, "BTCUSDT", "sell" if i % 2 else "buy", 50000, 2, 100000] for i in range(10)]
    _write(tmp_path / "liquidations.csv", h, rows)
    rep = M.validate_sample(str(tmp_path))
    liq = rep["by_type"].get("liquidations", {})
    assert liq.get("valid") is True and liq.get("side_valid") is True and liq.get("notional_calculable") is True


def test_oi_and_funding_schema_detected(tmp_path):
    _write(tmp_path / "open_interest.csv", ["timestamp", "symbol", "open_interest"],
           [[B + i * DAY, "BTCUSDT", 1000 + i] for i in range(10)])
    _write(tmp_path / "funding.csv", ["timestamp", "symbol", "funding_rate"],
           [[B + i * DAY, "BTCUSDT", 0.0001 * (i + 1)] for i in range(10)])
    rep = M.validate_sample(str(tmp_path))
    assert rep["by_type"]["oi"]["valid"] is True
    assert rep["by_type"]["funding"]["valid"] is True


def _orderbook_l5_rows(n=150, span_days=35, symbol="BTCUSDT"):
    """5-level orderbook WITH sizes so L1 and L5 imbalance are computable."""
    h = ["timestamp", "symbol"]
    for lv in range(1, 6):
        h += [f"bid_price_{lv}", f"bid_size_{lv}", f"ask_price_{lv}", f"ask_size_{lv}"]
    rows = []
    for i in range(n):
        ts = B + int(i * span_days * DAY / n)
        row = [ts, symbol]
        for lv in range(1, 6):
            row += [49995 - lv, 1.0, 50005 + lv, 1.0]
        rows.append(row)
    return h, rows


def _dense_oi(n=40, span_days=35, symbol="BTCUSDT"):
    return (["timestamp", "symbol", "open_interest"],
            [[B + int(i * span_days * DAY / n), symbol, 1000 + i] for i in range(n)])


def _dense_liq(n=40, span_days=35, symbol="BTCUSDT"):
    return (["timestamp", "symbol", "side", "price", "size", "notional"],
            [[B + int(i * span_days * DAY / n), symbol, "sell" if i % 2 else "buy", 50000, 2, 100000]
             for i in range(n)])


def _ready_sample(tmp_path, symbol="BTCUSDT"):
    """Write a fully dense, aligned, valid 4-type sample that SHOULD reach READY."""
    _write(tmp_path / "trades.csv", *_trades(side=True, symbol=symbol))
    _write(tmp_path / "orderbook_l2.csv", *_orderbook_l5_rows(symbol=symbol))
    _write(tmp_path / "open_interest.csv", *_dense_oi(symbol=symbol))
    _write(tmp_path / "liquidations.csv", *_dense_liq(symbol=symbol))


def test_full_sample_research_ready(tmp_path):
    h, r = _trades(side=True, span_days=40)
    _write(tmp_path / "trades.csv", h, r)
    obh, obr = _orderbook_l5_rows()
    _write(tmp_path / "orderbook_l2.csv", obh, obr)
    _write(tmp_path / "open_interest.csv", ["timestamp", "symbol", "open_interest"],
           [[B + i * DAY, "BTCUSDT", 1000 + i] for i in range(40)])
    _write(tmp_path / "liquidations.csv", ["timestamp", "symbol", "side", "price", "size", "notional"],
           [[B + i * DAY, "BTCUSDT", "sell" if i % 2 else "buy", 50000, 2, 100000]
            for i in range(40)])
    rep = M.validate_sample(str(tmp_path))
    cls = rep["classification"]
    assert cls["verdict"] == M.C_READY
    assert cls["active_gaps"] == []
    assert cls["invalid_recognized_files"] == 0
    assert cls["orderbook_l1_ready"] is True
    assert rep["by_type"]["orderbook"]["l1_imbalance_median"] is not None
    assert rep["by_type"]["orderbook"]["l5_imbalance_median"] is not None
    assert cls["can_research_microstructure"] is True
    assert cls["funding_optional_reason"]
    assert cls["future_research_ready_if_sample_passes"] is True
    assert cls["future_labs_ready"]["aggressive_flow_imbalance"] is True


def test_ready_not_emitted_when_liquidations_missing(tmp_path):
    h, r = _trades(side=True, span_days=40)
    _write(tmp_path / "trades.csv", h, r)
    obh, obr = _orderbook_l5_rows()
    _write(tmp_path / "orderbook_l2.csv", obh, obr)
    _write(tmp_path / "open_interest.csv", ["timestamp", "symbol", "open_interest"],
           [[B + i * DAY, "BTCUSDT", 1000 + i] for i in range(40)])
    rep = M.validate_sample(str(tmp_path))
    assert rep["classification"]["verdict"] != M.C_READY
    assert M.C_NEEDS_LIQ in rep["classification"]["active_gaps"]
    assert rep["classification"]["can_research_microstructure"] is False


# ---- V10.24.2 multi-file fail-closed + L1/L5 sizes ------------------------

def test_invalid_file_blocks_ready_even_with_valid_sibling(tmp_path):
    # adversarial: one crossed (invalid) orderbook + one good orderbook + the rest valid
    h, r = _trades(side=True, span_days=40)
    _write(tmp_path / "trades.csv", h, r)
    obh, obr = _orderbook_l5_rows()
    _write(tmp_path / "orderbook_good.csv", obh, obr)
    _write(tmp_path / "orderbook_bad.csv",
           ["timestamp", "symbol", "bid_price_1", "bid_size_1", "ask_price_1", "ask_size_1"],
           [[B + i * DAY, "BTCUSDT", 50010, 1, 50000, 1] for i in range(40)])  # crossed
    _write(tmp_path / "open_interest.csv", ["timestamp", "symbol", "open_interest"],
           [[B + i * DAY, "BTCUSDT", 1000 + i] for i in range(40)])
    _write(tmp_path / "liquidations.csv", ["timestamp", "symbol", "side", "price", "size", "notional"],
           [[B + i * DAY, "BTCUSDT", "sell", 50000, 2, 100000] for i in range(40)])
    rep = M.validate_sample(str(tmp_path))
    cls = rep["classification"]
    assert cls["verdict"] != M.C_READY
    assert cls["invalid_recognized_files"] >= 1
    assert any("orderbook_bad.csv" in e for e in cls["critical_errors"])
    files = {fr["file"] for fr in cls["file_results"]}
    assert {"orderbook_good.csv", "orderbook_bad.csv"} <= files
    ob_sum = cls["type_summary"]["orderbook"]
    assert ob_sum["valid_files"] >= 1 and ob_sum["invalid_files"] >= 1


def test_empty_recognized_csv_is_invalid_not_no_sample(tmp_path):
    _write(tmp_path / "trades.csv", ["timestamp", "symbol", "price", "size", "aggressor_side"], [])
    rep = M.validate_sample(str(tmp_path))
    cls = rep["classification"]
    assert cls["verdict"] == M.C_INVALID
    assert cls["verdict"] != M.C_NO_SAMPLE
    assert any("empty_recognized_csv" in e for e in cls["critical_errors"])
    assert cls["critical_errors_by_file"].get("trades.csv")


def test_orderbook_without_sizes_not_ready_and_l1_none(tmp_path):
    _write(tmp_path / "trades.csv", *_trades(side=True, span_days=40))
    _write(tmp_path / "orderbook_l2.csv",
           ["timestamp", "symbol", "bid_price_1", "ask_price_1"],
           [[B + i * DAY, "BTCUSDT", 49995, 50005] for i in range(40)])
    _write(tmp_path / "open_interest.csv", ["timestamp", "symbol", "open_interest"],
           [[B + i * DAY, "BTCUSDT", 1000 + i] for i in range(40)])
    _write(tmp_path / "liquidations.csv", ["timestamp", "symbol", "side", "price", "size", "notional"],
           [[B + i * DAY, "BTCUSDT", "sell", 50000, 2, 100000] for i in range(40)])
    rep = M.validate_sample(str(tmp_path))
    cls = rep["classification"]
    assert cls["verdict"] != M.C_READY
    assert rep["by_type"]["orderbook"]["l1_imbalance_median"] is None
    assert cls["orderbook_l1_ready"] is False
    assert M.C_NEEDS_ORDERBOOK in cls["active_gaps"]


def test_orderbook_l1_only_computes_l1_not_l5(tmp_path):
    _write(tmp_path / "orderbook_l2.csv",
           ["timestamp", "symbol", "bid_price_1", "bid_size_1", "ask_price_1", "ask_size_1"],
           [[B + i * 1000, "BTCUSDT", 49995, 2, 50005, 1] for i in range(20)])
    rep = M.validate_sample(str(tmp_path))
    ob = rep["by_type"]["orderbook"]
    assert ob["l1_imbalance_available"] is True and ob["l1_imbalance_median"] is not None
    assert ob["depth_levels_available"] == 1
    assert ob["l5_optional_reason"]


def test_orderbook_five_levels_computes_l5(tmp_path):
    obh, obr = _orderbook_l5_rows(n=20)
    _write(tmp_path / "orderbook_l2.csv", obh, obr)
    rep = M.validate_sample(str(tmp_path))
    ob = rep["by_type"]["orderbook"]
    assert ob["depth_levels_available"] == 5
    assert ob["l5_imbalance_available"] is True and ob["l5_imbalance_median"] is not None


@pytest.mark.parametrize("kind,header,row", [
    ("trades", ["symbol", "price", "size", "aggressor_side"], ["BTCUSDT", 50000, 1, "buy"]),
    ("orderbook_l2", ["symbol", "bid_price_1", "bid_size_1", "ask_price_1", "ask_size_1"],
     ["BTCUSDT", 49995, 1, 50005, 1]),
    ("open_interest", ["symbol", "open_interest"], ["BTCUSDT", 1000]),
    ("funding", ["symbol", "funding_rate"], ["BTCUSDT", 0.0001]),
    ("liquidations", ["symbol", "side", "price", "size", "notional"], ["BTCUSDT", "sell", 50000, 1, 50000]),
])
def test_missing_timestamp_by_type_invalid(tmp_path, kind, header, row):
    _write(tmp_path / f"{kind}.csv", header, [row for _ in range(20)])
    rep = M.validate_sample(str(tmp_path))
    cls = rep["classification"]
    assert cls["verdict"] == M.C_INVALID
    assert any("invalid_timestamps" in e for e in cls["critical_errors"])
    assert cls["invalid_recognized_files"] >= 1


def test_output_base_outside_v10_24_falls_back(tmp_path):
    assert M._safe_output_base("reports/research/v10_24") == "reports/research/v10_24"
    assert M._safe_output_base("reports/research/v10_24/sub") == "reports/research/v10_24/sub"
    assert M._safe_output_base(str(tmp_path / "evil")) == M.OUTPUT_ROOT
    assert M._safe_output_base("reports/research/v10_8") == M.OUTPUT_ROOT
    assert M._safe_output_base("external_data/raw/x") == M.OUTPUT_ROOT


def test_trade_zero_price_or_negative_size_invalidates(tmp_path):
    _write(tmp_path / "trades.csv",
           ["timestamp", "symbol", "price", "size", "aggressor_side"],
           [[B + i * DAY, "BTCUSDT", 50000, 1, "buy"] for i in range(38)]
           + [[B + 38 * DAY, "BTCUSDT", 0, 1, "sell"],
              [B + 39 * DAY, "BTCUSDT", 50000, -1, "sell"]])
    rep = M.validate_sample(str(tmp_path))
    tr = rep["by_type"]["trades"]
    assert tr["valid"] is False
    assert "trades:trade_price_not_strictly_positive" in rep["classification"]["critical_errors"]
    assert "trades:trade_size_not_strictly_positive" in rep["classification"]["critical_errors"]
    assert rep["classification"]["verdict"] == M.C_INVALID


def test_liquidations_without_notional_or_size_invalidates(tmp_path):
    _write(tmp_path / "liquidations.csv",
           ["timestamp", "symbol", "side", "price", "size", "notional"],
           [[B + i * DAY, "BTCUSDT", "sell", 50000, "", ""] for i in range(40)])
    rep = M.validate_sample(str(tmp_path))
    liq = rep["by_type"]["liquidations"]
    assert liq["valid"] is False
    assert liq["notional_calculable"] is False
    assert "liquidations:liquidation_size_not_strictly_positive" in rep["classification"]["critical_errors"]


def test_liquidations_calculates_notional_when_price_size_present(tmp_path):
    _write(tmp_path / "liquidations.csv",
           ["timestamp", "symbol", "side", "price", "size"],
           [[B + i * DAY, "BTCUSDT", "sell", 50000, 2] for i in range(40)])
    rep = M.validate_sample(str(tmp_path))
    liq = rep["by_type"]["liquidations"]
    assert liq["valid"] is True
    assert liq["notional_calculable"] is True


def test_liquidations_invalid_side_invalidates(tmp_path):
    _write(tmp_path / "liquidations.csv",
           ["timestamp", "symbol", "side", "price", "size", "notional"],
           [[B + i * DAY, "BTCUSDT", "hold", 50000, 2, 100000] for i in range(40)])
    rep = M.validate_sample(str(tmp_path))
    assert rep["by_type"]["liquidations"]["valid"] is False
    assert "liquidations:liquidation_side_invalid" in rep["classification"]["critical_errors"]


def test_oi_and_funding_numeric_range_fail_closed(tmp_path):
    _write(tmp_path / "open_interest.csv", ["timestamp", "symbol", "open_interest"],
           [[B + i * DAY, "BTCUSDT", -1 if i % 2 else "bad"] for i in range(40)])
    _write(tmp_path / "funding.csv", ["timestamp", "symbol", "funding_rate"],
           [[B + i * DAY, "BTCUSDT", 0.5 if i % 2 else "bad"] for i in range(40)])
    rep = M.validate_sample(str(tmp_path))
    assert rep["by_type"]["oi"]["valid"] is False
    assert rep["by_type"]["funding"]["valid"] is False
    assert "oi:oi_negative_or_non_numeric" in rep["classification"]["critical_errors"]
    assert "funding:funding_rate_invalid_or_absurd" in rep["classification"]["critical_errors"]


def test_future_and_non_monotonic_timestamps_invalid(tmp_path):
    future = 4_000_000_000_000
    _write(tmp_path / "trades.csv",
           ["timestamp", "symbol", "price", "size", "aggressor_side"],
           [[B + DAY, "BTCUSDT", 50000, 1, "buy"],
            [B, "BTCUSDT", 50001, 1, "sell"],
            [future, "BTCUSDT", 50002, 1, "buy"]])
    rep = M.validate_sample(str(tmp_path))
    assert rep["by_type"]["trades"]["valid"] is False
    errs = rep["classification"]["critical_errors"]
    assert "trades:future_timestamps" in errs
    assert "trades:non_monotonic_timestamps" in errs


def test_huge_timestamp_gap_blocks_ready(tmp_path):
    ts = [B, B + DAY, B + 2 * DAY, B + 40 * DAY]
    _write(tmp_path / "trades.csv", ["timestamp", "symbol", "price", "size", "aggressor_side"],
           [[t, "BTCUSDT", 50000, 1, "buy"] for t in ts])
    _write(tmp_path / "orderbook_l2.csv", ["timestamp", "symbol", "bid_price_1", "ask_price_1"],
           [[t, "BTCUSDT", 49995, 50005] for t in ts])
    _write(tmp_path / "open_interest.csv", ["timestamp", "symbol", "open_interest"],
           [[t, "BTCUSDT", 1000] for t in ts])
    _write(tmp_path / "liquidations.csv", ["timestamp", "symbol", "side", "price", "size", "notional"],
           [[t, "BTCUSDT", "sell", 50000, 1, 50000] for t in ts])
    rep = M.validate_sample(str(tmp_path))
    assert rep["classification"]["verdict"] != M.C_READY
    assert M.C_NEEDS_HISTORY in rep["classification"]["active_gaps"]


def test_duplicate_timestamps_severe_invalidates(tmp_path):
    _write(tmp_path / "trades.csv",
           ["timestamp", "symbol", "price", "size", "aggressor_side"],
           [[B, "BTCUSDT", 50000 + i, 1, "buy"] for i in range(40)])
    rep = M.validate_sample(str(tmp_path))
    tr = rep["by_type"]["trades"]
    assert tr["duplicate_count"] == 39
    assert tr["valid"] is False
    assert "trades:duplicate_timestamps" in rep["classification"]["critical_errors"]


def test_same_ms_distinct_trades_are_valid_market_data(tmp_path):
    # V10.24.4: bursts of DISTINCT trades sharing a millisecond are real
    # (Binance aggTrades) -- must NOT invalidate the file (<50% collisions,
    # zero exact duplicate rows).
    rows = []
    for i in range(100):
        t = B + i * 60_000
        rows.append([t, "BTCUSDT", 50000 + i, 1, "buy" if i % 2 else "sell"])
        if i % 4 == 0:   # 25 same-ms distinct trades -> 25% collision ratio
            rows.append([t, "BTCUSDT", 50000 + i + 0.5, 2, "sell"])
    _write(tmp_path / "trades.csv",
           ["timestamp", "symbol", "price", "size", "aggressor_side"], rows)
    rep = M.validate_sample(str(tmp_path))
    tr = rep["by_type"]["trades"]
    assert tr["valid"] is True
    assert tr["coverage"]["ts_collision_count"] == 25
    assert tr["coverage"]["exact_duplicate_rows"] == 0
    assert "trades:duplicate_timestamps" not in rep["classification"]["critical_errors"]


def test_exact_duplicate_trade_rows_still_invalidate(tmp_path):
    # 10 exact copies (same ts+price+size+side) in 60 rows -> corruption.
    base = [[B + i * 60_000, "BTCUSDT", 50000, 1, "buy"] for i in range(50)]
    _write(tmp_path / "trades.csv",
           ["timestamp", "symbol", "price", "size", "aggressor_side"],
           base + base[:10])
    rep = M.validate_sample(str(tmp_path))
    tr = rep["by_type"]["trades"]
    assert tr["valid"] is False
    assert tr["coverage"]["exact_duplicate_rows"] == 10
    assert "trades:duplicate_timestamps" in rep["classification"]["critical_errors"]


def test_orderbook_negative_bid_or_ask_invalid(tmp_path):
    h = ["timestamp", "symbol", "bid_price_1", "bid_size_1", "ask_price_1", "ask_size_1"]
    rows = [[B + i * DAY, "BTCUSDT", -1, 1, 50005, 1] for i in range(40)]
    _write(tmp_path / "orderbook_l2.csv", h, rows)
    rep = M.validate_sample(str(tmp_path))
    ob = rep["by_type"]["orderbook"]
    assert ob["valid"] is False
    assert ob["invalid_price_rows"] == 40
    assert "orderbook:orderbook_bid_ask_not_strictly_positive" in rep["classification"]["critical_errors"]


# ---- path safety ----------------------------------------------------------

def test_unsafe_sample_dir_blocked():
    for bad in ("external_data/raw/x", "secrets/.env", "a/../b", "x/db/y",
                "backups/z", "vault/z", "x/credentials/y", "prod/sample",
                "production/sample", "live/sample", "private/sample"):
        with pytest.raises(ValueError):
            M.assert_safe_sample_dir(bad)


def test_validate_unsafe_dir_returns_invalid():
    rep = M.validate_sample("external_data/raw/evil")
    assert rep["classification"]["verdict"] == M.C_INVALID
    assert any("unsafe" in e for e in rep["errors"])


def test_normalization_writes_only_staging_marker(tmp_path):
    _ready_sample(tmp_path)   # only a fully READY sample may normalize
    rep = M.validate_sample(str(tmp_path), apply_normalization=True)
    nrm = rep["normalization"]
    assert rep["classification"]["verdict"] == M.C_READY
    try:
        assert nrm["applied"] is True
        assert M.STAGING_MARKER in nrm["out_dir"]
        assert nrm["wrote_only_staging_marker"] is True
        assert any(p.endswith("trades_normalized.csv") for p in nrm["files"])
    finally:
        shutil.rmtree(nrm["out_dir"], ignore_errors=True)
        try:
            os.rmdir(os.path.dirname(nrm["out_dir"]))
        except OSError:
            pass


def test_normalization_blocked_when_not_ready(tmp_path):
    # only trades (valid) -> NEEDS_ORDERBOOK -> normalization must NOT run
    _write(tmp_path / "trades.csv", *_trades(side=True))
    rep = M.validate_sample(str(tmp_path), apply_normalization=True)
    assert rep["classification"]["verdict"] != M.C_READY
    assert rep["normalization"]["applied"] is False
    assert rep["classification"]["normalization_allowed"] is False
    assert rep["normalization"]["normalization_blockers"]


def test_normalization_blocked_by_active_gaps(tmp_path):
    # full sample but liquidations missing -> active gap -> no normalization
    _write(tmp_path / "trades.csv", *_trades(side=True))
    _write(tmp_path / "orderbook_l2.csv", *_orderbook_l5_rows())
    _write(tmp_path / "open_interest.csv", *_dense_oi())
    rep = M.validate_sample(str(tmp_path), apply_normalization=True)
    assert rep["normalization"]["applied"] is False
    assert "active_gaps" in rep["classification"]["normalization_blockers"]


def test_normalization_blocked_by_rep_errors(tmp_path):
    _ready_sample(tmp_path)
    target = tmp_path.parent / "outside_norm.csv"
    _write(target, ["timestamp", "symbol", "price", "size", "aggressor_side"], [[B, "BTCUSDT", 1, 1, "buy"]])
    link = tmp_path / "zz_trades_link.csv"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    rep = M.validate_sample(str(tmp_path), apply_normalization=True)
    assert rep["errors"]
    assert rep["normalization"]["applied"] is False
    assert rep["classification"]["normalization_allowed"] is False


def test_safe_normalized_dir_requires_exact_staging_marker(tmp_path):
    M.safe_normalized_dir("run1", f"external_data/staging/{M.STAGING_MARKER}")
    for bad in (f"external_data/staging/{M.STAGING_MARKER}/nested",
                "external_data/staging/other", "external_data/raw", "vault"):
        with pytest.raises(ValueError):
            M.safe_normalized_dir("run1", bad)
    with pytest.raises(ValueError):
        M.safe_normalized_dir("../run1", f"external_data/staging/{M.STAGING_MARKER}")
    fake_marker = tmp_path / M.STAGING_MARKER
    fake_marker.mkdir()
    with pytest.raises(ValueError):
        M.safe_normalized_dir("run1", str(fake_marker))


def test_symlinked_sample_file_blocked(tmp_path):
    target = tmp_path / "outside.csv"
    _write(target, ["timestamp", "symbol", "price", "size", "aggressor_side"],
           [[B, "BTCUSDT", 50000, 1, "buy"]])
    sample = tmp_path / "sample"
    sample.mkdir()
    link = sample / "trades.csv"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable on this platform")
    rep = M.validate_sample(str(sample))
    assert rep["classification"]["verdict"] == M.C_INVALID   # unsafe file -> fail-closed
    assert any("unsafe_sample_file" in e for e in rep["errors"])
    assert rep["classification"]["can_research_microstructure"] is False


# ---- CLI isolation (no config/.env/DB) ------------------------------------

def _run_main(argv):
    import sys
    old = sys.argv
    sys.argv = ["prog"] + argv
    try:
        research_lab.main()
    finally:
        sys.argv = old


# ---- V10.24.3 data-contract final hardening -------------------------------

def test_valid_sample_plus_unsafe_symlink_is_invalid_never_ready(tmp_path):
    _ready_sample(tmp_path)
    target = tmp_path.parent / "outside_lll.csv"
    _write(target, ["timestamp", "symbol", "price", "size", "aggressor_side"], [[B, "BTCUSDT", 1, 1, "buy"]])
    link = tmp_path / "zz_trades_symlink.csv"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    cls = M.validate_sample(str(tmp_path))["classification"]
    assert cls["verdict"] == M.C_INVALID and cls["verdict"] != M.C_READY
    assert cls["unsafe_file_count"] >= 1
    assert any("unsafe_sample_file" in e for e in cls["critical_errors"])
    assert "_sample" in cls["critical_errors_by_file"]
    assert cls["can_research_microstructure"] is False
    assert cls["normalization_allowed"] is False


def test_credentials_csv_blocks_ready(tmp_path):
    _ready_sample(tmp_path)
    _write(tmp_path / "credentials.csv", ["api_key", "secret"], [["abc", "xyz"]])
    cls = M.validate_sample(str(tmp_path))["classification"]
    assert cls["verdict"] != M.C_READY
    assert "credentials.csv" in cls["unknown_suspicious_files"]
    assert any("credentials.csv" in str(e) for e in cls["critical_errors"])
    assert any(fr["file"] == "credentials.csv" for fr in cls["file_results"])


def test_secrets_csv_blocks_ready(tmp_path):
    _ready_sample(tmp_path)
    _write(tmp_path / "secrets.csv", ["timestamp", "value"], [[B, 1]])
    cls = M.validate_sample(str(tmp_path))["classification"]
    assert cls["verdict"] != M.C_READY and "secrets.csv" in cls["unknown_suspicious_files"]


def test_secret_like_headers_block_ready(tmp_path):
    _ready_sample(tmp_path)
    _write(tmp_path / "hard_creds.csv", ["timestamp", "password", "access_key"], [[B, "p", "k"]])
    cls = M.validate_sample(str(tmp_path))["classification"]
    assert cls["verdict"] != M.C_READY and "hard_creds.csv" in cls["unknown_suspicious_files"]


def test_benign_unknown_file_does_not_block(tmp_path):
    _ready_sample(tmp_path)
    _write(tmp_path / "notes.csv", ["note"], [["all good"]])
    cls = M.validate_sample(str(tmp_path))["classification"]
    assert cls["verdict"] == M.C_READY and cls["unknown_suspicious_files"] == []


def test_trades_without_symbol_not_ready(tmp_path):
    _ready_sample(tmp_path)
    # overwrite trades with a symbol-less file
    rows = [[B + int(i * 35 * DAY / 1200), 50000, 0.5, "buy" if i % 2 else "sell"] for i in range(1200)]
    _write(tmp_path / "trades.csv", ["timestamp", "price", "size", "aggressor_side"], rows)
    cls = M.validate_sample(str(tmp_path))["classification"]
    assert cls["verdict"] != M.C_READY
    assert cls["symbol_alignment_ok"] is False
    assert "MISSING_SYMBOL_TRADES" in cls["why_not_ready"]


def test_cross_symbol_misalignment_not_ready(tmp_path):
    _write(tmp_path / "trades.csv", *_trades(side=True, symbol="BTCUSDT"))
    _write(tmp_path / "orderbook_l2.csv", *_orderbook_l5_rows(symbol="ETHUSDT"))
    _write(tmp_path / "open_interest.csv", *_dense_oi(symbol="XRPUSDT"))
    _write(tmp_path / "liquidations.csv", *_dense_liq(symbol="DOGEUSDT"))
    cls = M.validate_sample(str(tmp_path))["classification"]
    assert cls["verdict"] != M.C_READY
    assert cls["symbol_alignment_ok"] is False
    assert cls["common_symbols_required"] == []
    assert "SYMBOL_ALIGNMENT_FAIL" in cls["why_not_ready"]


def test_common_symbol_reported_when_aligned(tmp_path):
    _ready_sample(tmp_path, symbol="BTCUSDT")
    cls = M.validate_sample(str(tmp_path))["classification"]
    assert cls["verdict"] == M.C_READY
    assert cls["common_symbols_required"] == ["BTCUSDT"]
    assert cls["symbol_alignment_ok"] is True


def test_two_rows_in_40_days_not_ready(tmp_path):
    for kind, hdr_rows in {
        "trades": (["timestamp", "symbol", "price", "size", "aggressor_side"],
                   [[B, "BTCUSDT", 50000, 1, "buy"], [B + 40 * DAY, "BTCUSDT", 50001, 1, "sell"]]),
        "orderbook_l2": (["timestamp", "symbol", "bid_price_1", "bid_size_1", "ask_price_1", "ask_size_1"],
                         [[B, "BTCUSDT", 49995, 1, 50005, 1], [B + 40 * DAY, "BTCUSDT", 49995, 1, 50005, 1]]),
        "open_interest": (["timestamp", "symbol", "open_interest"], [[B, "BTCUSDT", 1000], [B + 40 * DAY, "BTCUSDT", 1001]]),
        "liquidations": (["timestamp", "symbol", "side", "price", "size", "notional"],
                         [[B, "BTCUSDT", "sell", 50000, 1, 50000], [B + 40 * DAY, "BTCUSDT", "buy", 50000, 1, 50000]]),
    }.items():
        _write(tmp_path / f"{kind}.csv", hdr_rows[0], hdr_rows[1])
    cls = M.validate_sample(str(tmp_path))["classification"]
    assert cls["verdict"] != M.C_READY
    assert cls["density_ok"] is False


def test_trade_side_hold_is_invalid_sample(tmp_path):
    _write(tmp_path / "trades.csv", ["timestamp", "symbol", "price", "size", "aggressor_side"],
           [[B + i * DAY, "BTCUSDT", 50000, 1, "hold"] for i in range(40)])
    rep = M.validate_sample(str(tmp_path))
    assert rep["by_type"]["trades"]["valid"] is False
    assert rep["classification"]["verdict"] == M.C_INVALID
    assert "trades:trade_side_invalid" in rep["classification"]["critical_errors"]


def test_orderbook_without_sizes_future_labs_pressure_false(tmp_path):
    _write(tmp_path / "orderbook_l2.csv", ["timestamp", "symbol", "bid_price_1", "ask_price_1"],
           [[B + i * DAY, "BTCUSDT", 49995, 50005] for i in range(40)])
    cls = M.validate_sample(str(tmp_path))["classification"]
    assert cls["future_labs_ready"]["orderbook_pressure"] is False


def test_trades_without_aggressor_future_flow_false(tmp_path):
    _write(tmp_path / "trades.csv", *_trades(side=False))
    cls = M.validate_sample(str(tmp_path))["classification"]
    assert cls["future_labs_ready"]["aggressive_flow_imbalance"] is False


def test_run_id_collision_free():
    a, b = M._now_stamp(), M._now_stamp()
    assert a != b and re.match(r"\d{8}T\d{6}\d{6}Z_[0-9a-f]{8}", a)


def test_corrupt_recognized_csv_is_invalid(tmp_path):
    # invalid UTF-8 bytes make the reader raise -> parse_error -> INVALID (never degraded)
    p = tmp_path / "trades.csv"
    p.write_bytes(b"timestamp,symbol,price,size,aggressor_side\n\xff\xfe not valid utf8 bytes\n")
    rep = M.validate_sample(str(tmp_path))
    cls = rep["classification"]
    assert cls["verdict"] == M.C_INVALID
    assert any("csv_parse_error" in e for e in cls["critical_errors"])


def test_ready_scorecard_invariants(tmp_path):
    _ready_sample(tmp_path)
    rep = M.validate_sample(str(tmp_path))
    cls = rep["classification"]
    if cls["verdict"] == M.C_READY:
        assert cls["critical_errors"] == []
        assert rep["errors"] == []
        assert cls["active_gaps"] == []
        assert cls["why_not_ready"] == []
        assert cls["invalid_recognized_files"] == 0
        assert cls["unsafe_file_count"] == 0
        assert cls["unknown_suspicious_files"] == []
        assert cls["normalization_allowed"] is True
        assert cls["can_research_microstructure"] is True
        assert cls["common_symbols_required"] != []
        assert cls["density_ok"] is True
        assert cls["symbol_alignment_ok"] is True
        assert rep["final_recommendation"] == "NO LIVE"
    else:
        raise AssertionError(f"expected READY, got {cls['verdict']} why={cls['why_not_ready']}")


def test_cli_commands_allowlisted_and_isolated(monkeypatch, capsys):
    cmds = research_lab.PUBLIC_RESEARCH_ONLY_COMMANDS
    for c in ("microstructure-sample-plan-v1024", "microstructure-sample-validate-v1024",
              "microstructure-sample-report-v1024"):
        assert c in cmds
    monkeypatch.setattr(research_lab, "load_config",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no config")))
    monkeypatch.setattr(research_lab, "Database",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no db")))
    _run_main(["microstructure-sample-plan-v1024"])
    assert "MICROSTRUCTURE SAMPLE PLAN V10.24" in capsys.readouterr().out
    _run_main(["microstructure-sample-validate-v1024", "--sample-dir", "external_data/staging/missing-v1024"])
    out = capsys.readouterr().out
    assert "MICROSTRUCTURE SAMPLE VALIDATE V10.24" in out
    assert "NO LIVE" in out
    _run_main(["microstructure-sample-report-v1024", "--output-dir", "reports/research/v10_24"])
    assert "MICROSTRUCTURE SAMPLE REPORT V10.24" in capsys.readouterr().out


def test_module_no_dangerous_primitives():
    import re
    src = Path(MODULE_PATH).read_text(encoding="utf-8")
    scan = re.sub(r'"never":\s*\[.*?\],', '"never": [],', src, flags=re.DOTALL)
    for tok in ["load_dotenv", "os.environ", "private_get", "private_post",
                "import requests", "import socket", "urllib.request", "db.execute",
                "INSERT INTO", "import torch", "import tensorflow", "import jax"]:
        assert tok not in scan, tok
    for name in ["place_order", "create_order", "set_leverage", "set_margin_mode", "open_position"]:
        assert f"{name}(" not in scan and f".{name}" not in scan, name
    assert "ExecutionEngine(" not in scan and "PaperTrader(" not in scan
