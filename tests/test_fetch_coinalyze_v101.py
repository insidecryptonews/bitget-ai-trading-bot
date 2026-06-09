"""Tests for the research-only Coinalyze fetcher scripts/fetch_coinalyze_v101.py.

No network. No API key. No real data written to the repo. These tests
prove: clean abort without key, the key is never printed, builders emit
V10.1-valid rows, liquidations map to LONG/SHORT, missing price is skipped
(never invented), and the module is free of forbidden calls / .env reads.
"""

from __future__ import annotations

import ast
import pathlib
import sys

import pytest

_SCRIPTS = str(pathlib.Path(__file__).resolve().parents[1] / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import fetch_coinalyze_v101 as fc  # noqa: E402

from app.labs.external_edge_ingest_v10_1 import ingest_rows  # noqa: E402
from app.labs.external_edge_schemas_v10_1 import DS_PERP_LIQ, DS_PERP_MARKET  # noqa: E402

BASE_T = 1_780_000_000  # unix seconds
NORM = {"BTCUSDT_PERP.A": "BTCUSDT", "ETHUSDT_PERP.A": "ETHUSDT"}


def _ohlcv(csym, n=30, p0=69000.0):
    return {"symbol": csym, "history": [
        {"t": BASE_T + i * 3600, "o": p0 + i, "h": p0 + i + 50, "l": p0 + i - 50,
         "c": p0 + i + 5, "v": 1500.0 + i} for i in range(n)]}


def _hist_c(csym, val, n=30):
    return {"symbol": csym, "history": [{"t": BASE_T + i * 3600, "c": val} for i in range(n)]}


def _hist_r(csym, val, n=30):
    return {"symbol": csym, "history": [{"t": BASE_T + i * 3600, "r": val} for i in range(n)]}


# ---------------------------------------------------------------------------
# Abort without key + key never printed
# ---------------------------------------------------------------------------

def test_abort_without_key(monkeypatch, capsys):
    monkeypatch.delenv("COINALYZE_API_KEY", raising=False)
    rc = fc.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "ABORT" in out
    assert "COINALYZE_API_KEY is not set" in out
    assert "NO LIVE" in out


def test_main_never_prints_key(monkeypatch, capsys):
    fake = "SECRET_KEY_DO_NOT_LEAK_1234567890"
    monkeypatch.setenv("COINALYZE_API_KEY", fake)
    # Short-circuit any network: discovery returns no symbols => clean abort.
    monkeypatch.setattr(fc, "discover_bitget_symbols", lambda *a, **k: {})
    rc = fc.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert fake not in out  # the key is NEVER printed
    assert "NO LIVE" in out


# ---------------------------------------------------------------------------
# Builders produce V10.1-valid rows
# ---------------------------------------------------------------------------

def test_build_market_rows_valid_for_ingest():
    ohlcv = [_ohlcv("BTCUSDT_PERP.A"), _ohlcv("ETHUSDT_PERP.A", p0=3600.0)]
    oi = [_hist_c("BTCUSDT_PERP.A", 1.8e9), _hist_c("ETHUSDT_PERP.A", 9.0e8)]
    funding = [_hist_c("BTCUSDT_PERP.A", 0.0001), _hist_c("ETHUSDT_PERP.A", 0.00012)]
    lsr = [_hist_r("BTCUSDT_PERP.A", 1.05)]
    rows = fc.build_market_rows(ohlcv=ohlcv, oi=oi, funding=funding, lsr=lsr, coinalyze_to_norm=NORM)
    assert len(rows) == 60  # 2 symbols x 30 hours
    now_ms = (BASE_T + 30 * 3600) * 1000
    rep, clean = ingest_rows(rows, DS_PERP_MARKET, now_ms=now_ms)
    assert rep.rows_valid == 60 and rep.rows_invalid == 0
    assert rep.data_quality_status == "DATA_OK"
    # timestamps normalized to ms, exchange/source labelled
    assert all(r["exchange"] == "bitget" and r["source"] == "coinalyze" for r in rows)
    assert all(isinstance(r["timestamp"], int) and r["timestamp"] > 1_000_000_000_000 for r in rows)


def test_build_liquidation_rows_sides_and_validity():
    liq = [{"symbol": "BTCUSDT_PERP.A", "history": [
        {"t": BASE_T + i * 3600, "l": (250000.0 if i % 5 == 0 else 0.0),
         "s": (180000.0 if i % 7 == 0 else 0.0)} for i in range(30)]}]
    market_rows = fc.build_market_rows(ohlcv=[_ohlcv("BTCUSDT_PERP.A")], oi=[], funding=[], lsr=[], coinalyze_to_norm=NORM)
    price_lookup = fc.price_lookup_from_market_rows(market_rows)
    rows, skipped = fc.build_liquidation_rows(liquidations=liq, coinalyze_to_norm=NORM, price_by_symbol_ts=price_lookup)
    assert rows
    assert skipped == 0
    assert all(r["side"] in ("LONG", "SHORT") for r in rows)  # schema-valid sides
    now_ms = (BASE_T + 30 * 3600) * 1000
    rep, clean = ingest_rows(rows, DS_PERP_LIQ, now_ms=now_ms)
    assert rep.rows_valid == len(rows) and rep.rows_invalid == 0


def test_liquidation_skipped_when_no_price():
    liq = [{"symbol": "BTCUSDT_PERP.A", "history": [
        {"t": BASE_T + i * 3600, "l": 100000.0, "s": 0.0} for i in range(5)]}]
    # empty price lookup => no price => rows skipped, never invented
    rows, skipped = fc.build_liquidation_rows(liquidations=liq, coinalyze_to_norm=NORM, price_by_symbol_ts={})
    assert rows == []
    assert skipped == 5


def test_nearest_price_never_uses_future():
    price_map = {1000: 10.0, 2000: 20.0, 3000: 30.0}
    # at t=2500 -> closest EARLIER is 2000 (never 3000)
    assert fc._nearest_price(price_map, 2500) == 20.0
    # exact match
    assert fc._nearest_price(price_map, 3000) == 30.0
    # before everything -> None (no invention)
    assert fc._nearest_price(price_map, 500) is None


# ---------------------------------------------------------------------------
# No real data written to repo / no .env / no forbidden calls
# ---------------------------------------------------------------------------

def test_builders_do_not_write_any_files():
    raw = pathlib.Path(_SCRIPTS).parent / "external_data" / "raw"
    before = {p.name for d in raw.glob("*") if d.is_dir() for p in d.iterdir()} if raw.exists() else set()
    fc.build_market_rows(ohlcv=[_ohlcv("BTCUSDT_PERP.A")], oi=[], funding=[], lsr=[], coinalyze_to_norm=NORM)
    fc.build_liquidation_rows(liquidations=[], coinalyze_to_norm=NORM, price_by_symbol_ts={})
    after = {p.name for d in raw.glob("*") if d.is_dir() for p in d.iterdir()} if raw.exists() else set()
    # builders are pure: they create no files. Only .gitkeep may pre-exist.
    assert before == after
    assert all(n == ".gitkeep" for n in after)


def test_source_has_no_env_read_or_private_calls():
    src = pathlib.Path(fc.__file__).read_text(encoding="utf-8")
    for tok in ('open(".env', "open('.env", "load_dotenv", "os.environ[",
                "import ccxt", "private_get(", "private_post(", "place_order("):
        assert tok not in src, f"script references {tok}"
    # key is read, never written/printed by value
    assert 'os.environ.get("COINALYZE_API_KEY")' in src
    assert "print(key" not in src


def test_no_forbidden_calls_ast():
    src = pathlib.Path(fc.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    forbidden = {"place_order", "set_leverage", "set_margin_mode",
                 "private_get", "private_post", "execute", "open_position"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            assert name not in forbidden, f"calls {name}"


def test_writes_only_under_external_data_raw():
    src = pathlib.Path(fc.__file__).read_text(encoding="utf-8")
    assert 'RAW_MARKET_DIR = "external_data/raw/perp_market_state"' in src
    assert 'RAW_LIQ_DIR = "external_data/raw/perp_liquidations"' in src


# ---------------------------------------------------------------------------
# Symbol-mapping fix (Coinalyze exchange letter codes, e.g. ".A" = Bitget)
# ---------------------------------------------------------------------------

# Exactly what the VPS diagnosis returned from /future-markets.
_VPS_MARKETS = [
    {"symbol": "BTCUSDT_PERP.A", "exchange": "A", "symbol_on_exchange": "BTCUSDT",
     "base_asset": "BTC", "quote_asset": "USDT", "expire_at": None,
     "has_buy_sell_data": True, "is_perpetual": True, "margined": "STABLE",
     "oi_lq_vol_denominated_in": "BASE_ASSET", "has_long_short_ratio_data": True,
     "has_ohlcv_data": True},
    {"symbol": "ETHUSDT_PERP.A", "exchange": "A", "symbol_on_exchange": "ETHUSDT",
     "base_asset": "ETH", "quote_asset": "USDT", "expire_at": None,
     "has_buy_sell_data": True, "is_perpetual": True, "margined": "STABLE",
     "oi_lq_vol_denominated_in": "BASE_ASSET", "has_long_short_ratio_data": True,
     "has_ohlcv_data": True},
    # decoys that must be ignored by field matching:
    {"symbol": "BTCUSD_PERP.Z", "exchange": "Z", "symbol_on_exchange": "BTCUSD",
     "base_asset": "BTC", "quote_asset": "USD", "is_perpetual": True,
     "margined": "COIN", "has_ohlcv_data": True},  # not USDT, coin-margined
    {"symbol": "BTCUSDT.X", "exchange": "X", "symbol_on_exchange": "BTCUSDT",
     "base_asset": "BTC", "quote_asset": "USDT", "is_perpetual": False,
     "has_ohlcv_data": True},  # not perpetual
    {"symbol": "ETHUSDT_PERP.Q", "exchange": "Q", "symbol_on_exchange": "ETHUSDT",
     "base_asset": "ETH", "quote_asset": "USDT", "is_perpetual": True,
     "margined": "STABLE", "has_ohlcv_data": False},  # no ohlcv data
]


def test_select_bitget_symbols_field_match_no_exchanges_name():
    # /exchanges never resolved "Bitget" by name; selection is field-based.
    out = fc.select_bitget_symbols(_VPS_MARKETS, {"BTCUSDT", "ETHUSDT"})
    assert out == {"BTCUSDT": "BTCUSDT_PERP.A", "ETHUSDT": "ETHUSDT_PERP.A"}


def test_select_prefers_dot_a_suffix_when_multiple():
    markets = _VPS_MARKETS + [
        {"symbol": "BTCUSDT_PERP.6", "exchange": "6", "symbol_on_exchange": "BTCUSDT",
         "base_asset": "BTC", "quote_asset": "USDT", "is_perpetual": True,
         "margined": "STABLE", "has_ohlcv_data": True},
    ]
    out = fc.select_bitget_symbols(markets, {"BTCUSDT", "ETHUSDT"})
    assert out["BTCUSDT"] == "BTCUSDT_PERP.A"  # .A preferred over .6


def test_select_missing_symbol_not_invented():
    out = fc.select_bitget_symbols(_VPS_MARKETS, {"BTCUSDT", "ETHUSDT", "SOLUSDT"})
    assert "SOLUSDT" not in out  # no SOL market present => not fabricated


def test_parse_symbol_override():
    out = fc.parse_symbol_override("BTCUSDT=BTCUSDT_PERP.A,ETHUSDT=ETHUSDT_PERP.A")
    assert out == {"BTCUSDT": "BTCUSDT_PERP.A", "ETHUSDT": "ETHUSDT_PERP.A"}
    assert fc.parse_symbol_override("") == {}
    assert fc.parse_symbol_override("garbage_no_equals") == {}


def test_main_manual_override_used_and_no_network(monkeypatch, capsys):
    fake = "SECRET_OVERRIDE_KEY_9999"
    monkeypatch.setenv("COINALYZE_API_KEY", fake)
    # discovery must NOT be called when override is provided
    def _boom(*a, **k):
        raise AssertionError("discovery should be skipped with override")
    monkeypatch.setattr(fc, "discover_bitget_symbols", _boom)
    # neutralize network + file writes
    monkeypatch.setattr(fc, "fetch_history", lambda *a, **k: [])
    monkeypatch.setattr(fc, "_write_csv", lambda *a, **k: None)
    rc = fc.main(["--coinalyze-symbols", "BTCUSDT=BTCUSDT_PERP.A,ETHUSDT=ETHUSDT_PERP.A"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "symbol_mode: manual_override" in out
    assert "discovered_symbols: BTCUSDT=BTCUSDT_PERP.A ETHUSDT=ETHUSDT_PERP.A" in out
    assert fake not in out  # key never printed


def test_main_aborts_when_required_symbol_missing(monkeypatch, capsys):
    fake = "SECRET_MISSING_KEY_7777"
    monkeypatch.setenv("COINALYZE_API_KEY", fake)
    monkeypatch.setattr(fc, "fetch_history", lambda *a, **k: [])
    # override only has BTC, ETH missing => clean abort BEFORE any fetch
    rc = fc.main(["--coinalyze-symbols", "BTCUSDT=BTCUSDT_PERP.A"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "ABORT: missing required symbols" in out
    assert "ETHUSDT" in out
    assert fake not in out
    assert "NO LIVE" in out


def test_main_discovery_path_prints_equals_format(monkeypatch, capsys):
    fake = "SECRET_DISCOVERY_KEY_5555"
    monkeypatch.setenv("COINALYZE_API_KEY", fake)
    # discovery returns the VPS-style mapping; neutralize network + writes
    monkeypatch.setattr(fc, "discover_bitget_symbols",
                        lambda *a, **k: {"BTCUSDT": "BTCUSDT_PERP.A", "ETHUSDT": "ETHUSDT_PERP.A"})
    monkeypatch.setattr(fc, "fetch_history", lambda *a, **k: [])
    monkeypatch.setattr(fc, "_write_csv", lambda *a, **k: None)
    rc = fc.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "symbol_mode: auto_discovery" in out
    assert "discovered_symbols: BTCUSDT=BTCUSDT_PERP.A ETHUSDT=ETHUSDT_PERP.A" in out
    assert fake not in out
