"""ResearchOps V10.1 — ingest tests. All synthetic / temp files. No DB, no network."""

from __future__ import annotations

import csv
import json

from app.labs.external_edge_ingest_v10_1 import (
    STATUS_BAD,
    STATUS_DATA_OK,
    STATUS_NEED_DATA,
    ingest_file_or_dir,
    ingest_rows,
    read_rows,
)
from app.labs.external_edge_schemas_v10_1 import DS_PERP_MARKET

BASE_MS = 1780272000000


def _row(i, sym="BTCUSDT", fr=0.0001):
    return {"symbol": sym, "exchange": "bitget", "timestamp": BASE_MS + i * 3600000,
            "price_open": 100 + i, "price_high": 101 + i, "price_low": 99 + i,
            "price_close": 100.5 + i, "volume_usd": 1e9, "funding_rate": fr,
            "oi_usd_close": 1.8e9, "source": "manual"}


def _now_after(n):
    return BASE_MS + n * 3600000


def _write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        for r in rows:
            w.writerow(r)


# 8. CSV valid -> clean output
def test_csv_valid_to_clean(tmp_path):
    rows = [_row(i) for i in range(30)]
    csvp = tmp_path / "sample.csv"
    _write_csv(csvp, rows)
    rep = ingest_file_or_dir(DS_PERP_MARKET, input_path=str(csvp),
                             clean_dir=str(tmp_path / "clean"),
                             reports_dir=str(tmp_path / "reports"))
    assert rep.rows_valid == 30
    assert rep.output_clean_csv and (tmp_path / "clean").exists()
    assert rep.output_clean_ndjson
    assert rep.db_writes == 0


# 9. NDJSON valid -> clean
def test_ndjson_valid_to_clean(tmp_path):
    rows = [_row(i) for i in range(25)]
    ndp = tmp_path / "sample.ndjson"
    ndp.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    back, fmt = read_rows(ndp)
    assert fmt == "ndjson" and len(back) == 25
    rep = ingest_file_or_dir(DS_PERP_MARKET, input_path=str(ndp),
                             clean_dir=str(tmp_path / "clean"))
    assert rep.rows_valid == 25


# 10. JSON array valid -> clean
def test_json_array_valid_to_clean(tmp_path):
    rows = [_row(i) for i in range(20)]
    jp = tmp_path / "sample.json"
    jp.write_text(json.dumps(rows), encoding="utf-8")
    back, fmt = read_rows(jp)
    assert fmt == "json" and len(back) == 20
    rep = ingest_file_or_dir(DS_PERP_MARKET, input_path=str(jp),
                             clean_dir=str(tmp_path / "clean"))
    assert rep.rows_valid == 20


# 11. duplicates detected
def test_duplicates_detected():
    rows = [_row(i) for i in range(20)] + [_row(i) for i in range(20)]
    rep, clean = ingest_rows(rows, DS_PERP_MARKET, now_ms=_now_after(20))
    assert rep.duplicate_count == 20
    assert len(clean) == 20


# 12. gaps detected
def test_gaps_detected():
    rows = [_row(i) for i in range(10)] + [_row(100 + i) for i in range(10)]
    rep, _ = ingest_rows(rows, DS_PERP_MARKET, now_ms=_now_after(200))
    assert rep.gap_count >= 1


# 13. invalid rows not counted as valid
def test_invalid_rows_excluded():
    rows = [_row(i) for i in range(10)]
    bad = _row(99)
    bad["funding_rate"] = float("nan")
    rows.append(bad)
    rep, clean = ingest_rows(rows, DS_PERP_MARKET, now_ms=_now_after(20))
    assert rep.rows_invalid == 1
    assert rep.rows_valid == 10
    assert all(r["timestamp_ms"] is not None for r in clean)


# 14. data_quality BAD when duplicate ratio high
def test_quality_bad_on_high_duplicates():
    rows = [_row(i) for i in range(20)] + [_row(i) for i in range(20)]  # 50% dup
    rep, _ = ingest_rows(rows, DS_PERP_MARKET, now_ms=_now_after(20))
    assert rep.data_quality_status == STATUS_BAD


# 15. no data -> NEED_DATA
def test_no_data_need_data():
    rep, clean = ingest_rows([], DS_PERP_MARKET)
    assert rep.data_quality_status == STATUS_NEED_DATA
    assert clean == []
    assert rep.final_recommendation == "NO LIVE"


# 16. no DB writes
def test_no_db_writes(tmp_path):
    rows = [_row(i) for i in range(15)]
    rep = ingest_file_or_dir(DS_PERP_MARKET, input_path=None, input_dir=None,
                             clean_dir=str(tmp_path / "clean"))
    # empty input -> NEED_DATA, still zero db writes
    assert rep.db_writes == 0
    rep2, _ = ingest_rows(rows, DS_PERP_MARKET, now_ms=_now_after(15))
    assert rep2.db_writes == 0


# 17 + 18. no .env reads / no API calls (structural — no real usage tokens)
def test_no_env_no_api_in_source():
    import importlib
    import pathlib
    src = pathlib.Path(importlib.import_module("app.labs.external_edge_ingest_v10_1").__file__).read_text(encoding="utf-8")
    # Real-usage tokens (not doc mentions): no env reads, no network clients,
    # no private/order calls.
    for tok in ("os.environ", "load_dotenv", 'open(".env', "open('.env",
                "import requests", "import ccxt", "import urllib", "import aiohttp",
                "private_get(", "private_post(", "place_order("):
        assert tok not in src, f"ingest module references {tok}"


def test_missing_file_is_empty_not_crash(tmp_path):
    rows, fmt = read_rows(tmp_path / "nope.csv")
    assert rows == [] and fmt == "MISSING_FILE"


def test_input_dir_aggregates(tmp_path):
    d = tmp_path / "raw"
    d.mkdir()
    _write_csv(d / "a.csv", [_row(i) for i in range(10)])
    _write_csv(d / "b.csv", [_row(i) for i in range(10, 20)])
    rep = ingest_file_or_dir(DS_PERP_MARKET, input_dir=str(d), clean_dir=str(tmp_path / "clean"))
    assert rep.rows_valid == 20
    assert len(rep.inputs) == 2
