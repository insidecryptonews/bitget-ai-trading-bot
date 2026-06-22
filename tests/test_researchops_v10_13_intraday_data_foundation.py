"""ResearchOps V10.13 - Intraday Data Foundation + Provider Readiness tests.

Pure/offline/deterministic. Verifies intraday OHLCV quality detection (1m/5m
presence, days, gaps, duplicates, non-monotonic ts, invalid OHLC, zero volume),
the provider-readiness matrix, the canonical schemas, the dry-run/staging-safe
Bitget probe, the sample builder (no raw/DB writes), the shadow-readiness bridge,
and the hard invariant that NOTHING is ever paper/live ready.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.labs import intraday_data_foundation_v10_13 as I

MODULE_PATH = "app/labs/intraday_data_foundation_v10_13.py"


def _write_ohlcv(sample_dir, sym, tf, rows):
    os.makedirs(sample_dir, exist_ok=True)
    lines = ["timestamp,open,high,low,close,volume"] + \
            [f"{r[0]},{r[1]},{r[2]},{r[3]},{r[4]},{r[5]}" for r in rows]
    Path(sample_dir, f"{sym}_{tf}_ohlcv.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _clean(tf, n, start=1700000000000, price=100.0):
    step = I.TF_MS[tf]
    rows = []
    for i in range(n):
        rows.append([start + i * step, price, price + 1, price - 1, price + 0.5, 10.0])
    return rows


# 1. absence of 1m/5m (only 4h/6h) -> NO_INTRADAY_DATA
def test_detects_absence_of_intraday(tmp_path):
    s = tmp_path / "s"
    _write_ohlcv(s, "BTCUSDT", "4h", _clean("4h", 100))
    _write_ohlcv(s, "BTCUSDT", "6h", _clean("6h", 100))
    r = I.intraday_data_readiness(str(s))
    assert r["status"] == I.NO_INTRADAY
    assert r["scalping_data_status"] == I.SCALPING_NOT_READY
    assert r["fallback_only"] is True
    assert r["intraday_timeframes_present"] == []


# 2. presence of 1m/5m in fixture
def test_detects_intraday_presence(tmp_path):
    s = tmp_path / "s"
    _write_ohlcv(s, "BTCUSDT", "1m", _clean("1m", 500))
    _write_ohlcv(s, "ETHUSDT", "5m", _clean("5m", 500))
    r = I.intraday_data_readiness(str(s))
    assert "1m" in r["intraday_timeframes_present"]
    assert "5m" in r["intraday_timeframes_present"]
    assert r["status"] in (I.PARTIAL, I.INTRADAY_READY)


# 3. days covered computed correctly
def test_days_covered():
    rows = _clean("1m", 1441)            # 1440 minutes = 1.0 day span
    q = I.analyze_ohlcv_series([{"ts": r[0], "open": r[1], "high": r[2], "low": r[3],
                                 "close": r[4], "volume": r[5]} for r in rows], "1m")
    assert abs(q["days_covered"] - 1.0) < 1e-6
    assert q["rows"] == 1441


def _rows(raw):
    return [{"ts": r[0], "open": r[1], "high": r[2], "low": r[3], "close": r[4],
             "volume": r[5]} for r in raw]


# 4. gaps detected
def test_detects_gaps():
    raw = _clean("1m", 10)
    raw = raw[:5] + [[r[0] + 50 * I.TF_MS["1m"], *r[1:]] for r in raw[5:]]  # jump
    q = I.analyze_ohlcv_series(_rows(raw), "1m")
    assert q["gaps"] >= 1


# 5. duplicates detected
def test_detects_duplicates():
    raw = _clean("1m", 5)
    raw.append(list(raw[-1]))            # duplicate last timestamp
    q = I.analyze_ohlcv_series(_rows(raw), "1m")
    assert q["duplicates"] >= 1


# 6. non-monotonic timestamps detected
def test_detects_non_monotonic():
    raw = _clean("1m", 5)
    raw[3][0] = raw[1][0] - 1000         # out-of-order
    q = I.analyze_ohlcv_series(_rows(raw), "1m")
    assert q["non_monotonic"] >= 1


# 7. invalid OHLC detected
def test_detects_invalid_ohlc():
    raw = _clean("1m", 5)
    raw[2] = [raw[2][0], 100.0, 90.0, 95.0, 99.0, 10.0]   # high < open/close, high < low-ish
    q = I.analyze_ohlcv_series(_rows(raw), "1m")
    assert q["invalid_ohlc"] >= 1


# 8. zero volume detected
def test_detects_zero_volume():
    raw = _clean("1m", 5)
    raw[1][5] = 0.0
    q = I.analyze_ohlcv_series(_rows(raw), "1m")
    assert q["zero_volume"] >= 1


# 9. provider matrix includes the 4 named providers
def test_provider_matrix_has_providers():
    m = I.provider_readiness_matrix()
    names = {p["name"] for p in m["providers"]}
    assert {"bitget_public", "coinalyze", "tardis_dev", "coinglass"} <= names
    assert m["no_paid_download"] is True and m["no_paid_activation"] is True


# 10. provider needing manual verification is not marked verified
def test_provider_not_marked_verified():
    m = I.provider_readiness_matrix()
    assert m["verified_count"] == 0
    for p in m["providers"]:
        assert p["state"] != "VERIFIED_PROVIDER"   # nothing is auto-verified
        if p["requires_api_key"]:
            assert p["state"] in (I.P_NEEDS_MANUAL, I.P_VERIFIED_SAMPLE, I.P_REJECTED, I.P_CANDIDATE)


# 11/12/13/20/21/22. source-level safety scan (no paid/private/auth/orders/.env)
def test_module_has_no_dangerous_primitives():
    import re
    src = Path(MODULE_PATH).read_text(encoding="utf-8")
    scan = re.sub(r'"never":\s*\[.*?\],', '"never": [],', src, flags=re.DOTALL)
    for token in ["import torch", "from torch", "import jax", "import tensorflow",
                  "import timesfm", "load_dotenv", "os.environ", "paid_download = True",
                  "ACCESS-KEY", "access-key", "access-sign", "db.execute", "INSERT INTO"]:
        assert token not in scan, token
    for name in ["place_order", "create_order", "private_get", "private_post",
                 "set_leverage", "set_margin_mode", "open_position"]:
        assert f"{name}(" not in scan and f".{name}" not in scan, name
    # module declares it never pays / never uses keys
    assert "no_paid_download" in src and "no_api_key_usage" in src


# 14. bitget intraday plan is dry-run and writes nothing / no network
def test_intraday_plan_dry_run_no_writes(tmp_path):
    p = I.bitget_intraday_plan(["BTCUSDT"], ["1m", "5m"], days=7)
    assert p["no_network"] is True and p["public_only"] is True
    assert p["dry_run_by_default"] is True
    assert p["auth"] == "none"
    assert p["final_recommendation"] == "NO LIVE"
    # probe in dry-run writes nothing and opens no socket
    calls = []
    rp = I.bitget_intraday_probe(symbols=["BTCUSDT"], timeframes=["1m"], apply=False,
                                 transport=lambda *a, **k: calls.append(1))
    assert rp["dry_run"] is True and calls == [] and rp.get("staging_dir") == ""


# 15. probe with a dangerous staging dir is blocked before any network call
def test_probe_blocks_unsafe_staging(tmp_path):
    calls = []
    rp = I.bitget_intraday_probe(symbols=["BTCUSDT"], timeframes=["1m"], apply=True,
                                 staging_root="external_data/raw/evil",
                                 transport=lambda *a, **k: calls.append(1) or {"data": []})
    assert rp.get("blocked") is True
    assert any("staging_dir_rejected" in e for e in rp["errors"])
    assert calls == []                    # never reached the network
    # the safety gate itself
    assert I.safe_intraday_staging_dir("external_data/raw/x") is not None
    assert I.safe_intraday_staging_dir(
        "external_data/staging/bitget_public_intraday_v10_13/run1") is None


# 16/17. sample builder never writes raw or DB; redirects unsafe output
def test_sample_builder_no_raw_no_db(tmp_path):
    s = tmp_path / "s"
    _write_ohlcv(s, "BTCUSDT", "1m", _clean("1m", 200))
    _write_ohlcv(s, "ETHUSDT", "1m", _clean("1m", 200))
    r = I.intraday_sample_build(str(s), output_dir="external_data/raw/evil", apply=True)
    assert r["status"] in (I.SB_STAGED, I.SB_WARNINGS, I.SB_REJECTED)
    assert r["manifest_dir"].startswith("reports/research/v10_13")
    assert "raw" not in r["manifest_dir"].split("/")
    # 4h-only sample -> nothing to build
    s2 = tmp_path / "s2"
    _write_ohlcv(s2, "BTCUSDT", "4h", _clean("4h", 100))
    r2 = I.intraday_sample_build(str(s2))
    assert r2["status"] == I.SB_SKIPPED


# 18. readiness never marks paper/live
def test_readiness_never_paper_live(tmp_path):
    s = tmp_path / "s"
    _write_ohlcv(s, "BTCUSDT", "1m", _clean("1m", 100))
    r = I.intraday_data_readiness(str(s))
    assert r["paper_ready"] is False and r["live_ready"] is False
    assert r["can_send_real_orders"] is False
    assert r["paper_candidate_future"] is False


# 19. final report says NO LIVE; reports are path-safe + gitignored
def test_reports_written_no_live(tmp_path):
    s = tmp_path / "s"
    _write_ohlcv(s, "BTCUSDT", "1m", _clean("1m", 100))
    r = I.intraday_data_readiness(str(s))
    m = I.provider_readiness_matrix()
    run = I.write_v1013_reports(r, m, output_dir="backups/x")   # unsafe -> redirected
    assert run.startswith("reports/research/v10_13")
    for fn in ("intraday_data_readiness_summary.json", "intraday_coverage_by_symbol_timeframe.csv",
               "intraday_quality_issues.csv", "provider_readiness_matrix.json",
               "provider_readiness_matrix.csv", "provider_gap_plan.md",
               "canonical_intraday_schema.md", "report.md"):
        assert os.path.isfile(os.path.join(run, fn)), fn
    md = Path(run, "report.md").read_text(encoding="utf-8").lower()
    assert "no live" in md and "not validatable" in md


# bridge: 4h-only sample cannot feed V10.10/11/12
def test_bridge_not_ready_without_intraday(tmp_path):
    s = tmp_path / "s"
    _write_ohlcv(s, "BTCUSDT", "4h", _clean("4h", 100))
    b = I.intraday_to_shadow_readiness(str(s))
    assert b["bridge_status"] == I.BR_NO_INTRADAY
    assert b["can_feed_v1010_micro_scalp"] is False
    assert b["can_feed_v1012_intelligent_shadow"] is False
    assert b["ready_for_paper"] is False and b["ready_for_live"] is False


# ==========================================================================
# V10.13.1 - intraday audit CLI contract hotfix (--symbols was ignored)
# ==========================================================================

def _bare_lab():
    from app.research_lab import ResearchLab
    return ResearchLab.__new__(ResearchLab)


def _three_sym_intraday(tmp_path):
    s = tmp_path / "stage"
    for sym in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        _write_ohlcv(s, sym, "1m", _clean("1m", 200))
    return s


# 1. audit --symbols BTCUSDT filters to BTCUSDT only (module level)
def test_audit_symbol_filter_single(tmp_path):
    s = _three_sym_intraday(tmp_path)
    r = I.bitget_intraday_audit(str(s), symbols=["BTCUSDT"])
    assert r["intraday_symbols"] == ["BTCUSDT"]
    assert r["n_intraday_symbols"] == 1
    assert r["audit_symbols_filter"] == ["BTCUSDT"]


# 2. audit --symbols BTCUSDT,ETHUSDT filters to those two
def test_audit_symbol_filter_two(tmp_path):
    s = _three_sym_intraday(tmp_path)
    r = I.bitget_intraday_audit(str(s), symbols=["BTCUSDT", "ETHUSDT"])
    assert sorted(r["intraday_symbols"]) == ["BTCUSDT", "ETHUSDT"]
    assert r["n_intraday_symbols"] == 2


# 3. no --symbols audits all available symbols
def test_audit_symbol_filter_all(tmp_path):
    s = _three_sym_intraday(tmp_path)
    r = I.bitget_intraday_audit(str(s))
    assert sorted(r["intraday_symbols"]) == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    assert r["audit_symbols_filter"] == "ALL"


# 1/2/3 again but through the CLI (the layer that had the dropped arg)
def test_audit_cli_symbol_filter(tmp_path):
    s = _three_sym_intraday(tmp_path)
    lab = _bare_lab()
    one = lab.bitget_intraday_audit_v1013_cli(staging_dir=str(s), symbols="BTCUSDT")
    assert "intraday_symbols: ['BTCUSDT']" in one
    assert "audit_symbols_filter: ['BTCUSDT']" in one
    two = lab.bitget_intraday_audit_v1013_cli(staging_dir=str(s), symbols="BTCUSDT,ETHUSDT")
    assert "SOLUSDT" not in two.split("intraday_symbols:")[1].split("\n")[0]
    assert "BTCUSDT" in two and "ETHUSDT" in two
    alls = lab.bitget_intraday_audit_v1013_cli(staging_dir=str(s), symbols="")
    assert "audit_symbols_filter: ALL" in alls
    # 5/6/7/8 safety invariants on every audit output
    for out in (one, two, alls):
        assert "paper_ready: false" in out and "live_ready: false" in out
        assert "can_send_real_orders: false" in out
        assert "final_recommendation: NO LIVE" in out


# 4. dangerous path still blocked; absolute outside repo blocked; relative ok
def test_staging_gate_hardening(tmp_path):
    assert I.safe_intraday_staging_dir("external_data/raw/x") is not None
    assert I.safe_intraday_staging_dir(
        "external_data/staging/bitget_public_intraday_v10_13/run1") is None
    # absolute path outside the repo is rejected even if it contains the marker
    bad_abs = "/tmp/external_data/staging/bitget_public_intraday_v10_13/run1"
    if os.path.isabs(bad_abs):
        assert I.safe_intraday_staging_dir(bad_abs) in (
            "absolute_path_outside_repo", "unresolvable_absolute_path")


# canonical schemas expose the 5 required tables
def test_canonical_schemas():
    sc = I.canonical_intraday_schemas()
    for tbl in ("ohlcv_intraday", "trades", "orderbook", "open_interest", "liquidations"):
        assert tbl in sc and isinstance(sc[tbl], list) and sc[tbl]
    assert "spread_bps" in sc["orderbook"]
    assert "open_interest" in sc["open_interest"]
    md = I.render_schema_md(sc).lower()
    assert "no live" in md and "no raw writes" in md
