#!/usr/bin/env python3
"""ResearchOps V10.1 — Coinalyze external-data fetcher (research-only).

Downloads Bitget perpetual market data from the Coinalyze API and writes
CSV files compatible with the V10.1 ingest schemas
(``perp_market_state`` and ``perp_liquidations``).

HARD CONTRACT — research only / security:

- reads the API key ONLY from the ``COINALYZE_API_KEY`` environment
  variable; if absent, aborts with a clear message and exit code 0
  (no crash, no stack trace),
- NEVER prints the key, NEVER writes the key to any file (the key is
  sent only via the ``api_key`` request header),
- NEVER touches ``.env``, the database, or any runtime/execution module,
- NEVER places orders, never calls private exchange endpoints — only
  Coinalyze public market-data GET endpoints,
- writes only under ``external_data/raw/...`` (which is git-ignored),
- this is a DATA tool. It does not decide to trade. NO LIVE.

Usage (on a machine where the key is exported):

    export COINALYZE_API_KEY=...        # never committed, never printed
    python -m scripts.fetch_coinalyze_v101 --symbols BTCUSDT,ETHUSDT --days 75 --interval 1hour
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

API_BASE = "https://api.coinalyze.net/v1"
EXCHANGE_LABEL = "bitget"
SOURCE_LABEL = "coinalyze"
RAW_MARKET_DIR = "external_data/raw/perp_market_state"
RAW_LIQ_DIR = "external_data/raw/perp_liquidations"

# Coinalyze intraday history is limited per request; chunk the window.
CHUNK_DAYS = 15
RATE_SLEEP_S = 1.6  # ~37 req/min, under the typical 40/min limit


# --------------------------------------------------------------------------
# Pure builders (unit-testable without network)
# --------------------------------------------------------------------------


def _f(v: Any):
    try:
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        f = float(v)
        return f if f == f and f not in (float("inf"), float("-inf")) else None
    except (TypeError, ValueError):
        return None


def _hist_index(resp: list[dict[str, Any]] | None) -> dict[str, dict[int, dict]]:
    """Map coinalyze_symbol -> {t_seconds -> point dict}."""
    out: dict[str, dict[int, dict]] = {}
    for entry in resp or []:
        sym = entry.get("symbol")
        if not sym:
            continue
        pts = {}
        for p in entry.get("history") or []:
            t = p.get("t")
            if t is None:
                continue
            pts[int(t)] = p
        out[sym] = pts
    return out


def build_market_rows(
    *,
    ohlcv: list[dict] | None,
    oi: list[dict] | None,
    funding: list[dict] | None,
    lsr: list[dict] | None,
    coinalyze_to_norm: dict[str, str],
) -> list[dict[str, Any]]:
    """Merge OHLCV + OI + funding + long/short ratio into V10.1
    ``perp_market_state`` rows keyed by (symbol, hour). The OHLCV bar is
    the spine; OI/funding/LSR are overlaid by exact timestamp."""
    ohlcv_idx = _hist_index(ohlcv)
    oi_idx = _hist_index(oi)
    funding_idx = _hist_index(funding)
    lsr_idx = _hist_index(lsr)
    rows: list[dict[str, Any]] = []
    now_iso = datetime.now(timezone.utc).isoformat()
    for csym, points in ohlcv_idx.items():
        norm = coinalyze_to_norm.get(csym)
        if not norm:
            continue
        for t, p in sorted(points.items()):
            close = _f(p.get("c"))
            vol = _f(p.get("v"))
            oi_pt = oi_idx.get(csym, {}).get(t, {})
            fund_pt = funding_idx.get(csym, {}).get(t, {})
            lsr_pt = lsr_idx.get(csym, {}).get(t, {})
            oi_usd_close = _f(oi_pt.get("c"))
            funding_rate = _f(fund_pt.get("c"))
            ls_ratio = _f(lsr_pt.get("r"))
            # volume_usd: Coinalyze 'v' is base volume; approximate USD as
            # base_volume * close (documented approximation).
            volume_usd = round(vol * close, 2) if (vol is not None and close is not None) else None
            rows.append({
                "symbol": norm,
                "exchange": EXCHANGE_LABEL,
                "timestamp": int(t) * 1000,  # UNIX ms UTC
                "price_open": _f(p.get("o")),
                "price_high": _f(p.get("h")),
                "price_low": _f(p.get("l")),
                "price_close": close,
                "volume_usd": volume_usd,
                "funding_rate": funding_rate,
                "oi_usd_close": oi_usd_close,
                "source": SOURCE_LABEL,
                "long_short_ratio": ls_ratio,
                "ingested_at": now_iso,
            })
    rows.sort(key=lambda r: (r["symbol"], r["timestamp"]))
    return rows


def build_liquidation_rows(
    *,
    liquidations: list[dict] | None,
    coinalyze_to_norm: dict[str, str],
    price_by_symbol_ts: dict[str, dict[int, float]],
) -> tuple[list[dict[str, Any]], int]:
    """Build V10.1 ``perp_liquidations`` rows. Coinalyze liquidation
    buckets carry long (l) and short (s) notional separately; emit one row
    per side that is > 0. ``side`` is mapped to the schema-valid LONG/SHORT
    (the side of the liquidated position) — the V10.1 validator only
    accepts LONG/SHORT/BUY/SELL, so we do NOT use 'long_liq'/'short_liq'.

    ``price`` is taken from the same-hour ``price_close``; if no price is
    available the row is SKIPPED (never invented). Returns (rows, skipped).
    """
    liq_idx = _hist_index(liquidations)
    now_iso = datetime.now(timezone.utc).isoformat()
    rows: list[dict[str, Any]] = []
    skipped = 0
    for csym, points in liq_idx.items():
        norm = coinalyze_to_norm.get(csym)
        if not norm:
            continue
        price_map = price_by_symbol_ts.get(norm, {})
        for t, p in sorted(points.items()):
            t_ms = int(t) * 1000
            long_liq = _f(p.get("l"))
            short_liq = _f(p.get("s"))
            price = _nearest_price(price_map, t_ms)
            for side, notional in (("LONG", long_liq), ("SHORT", short_liq)):
                if notional is None or notional <= 0:
                    continue
                if price is None:
                    skipped += 1
                    continue
                rows.append({
                    "symbol": norm,
                    "exchange": EXCHANGE_LABEL,
                    "timestamp": t_ms,
                    "side": side,
                    "notional_usd": round(notional, 2),
                    "price": price,
                    "source": SOURCE_LABEL,
                    "ingested_at": now_iso,
                })
    rows.sort(key=lambda r: (r["symbol"], r["timestamp"], r["side"]))
    return rows, skipped


def _nearest_price(price_map: dict[int, float], t_ms: int) -> float | None:
    """Exact same-hour price, else the closest EARLIER price (no future)."""
    if not price_map:
        return None
    if t_ms in price_map:
        return price_map[t_ms]
    earlier = [ts for ts in price_map if ts <= t_ms]
    if not earlier:
        return None
    return price_map[max(earlier)]


def price_lookup_from_market_rows(market_rows: list[dict[str, Any]]) -> dict[str, dict[int, float]]:
    out: dict[str, dict[int, float]] = {}
    for r in market_rows:
        pc = _f(r.get("price_close"))
        if pc is None:
            continue
        out.setdefault(r["symbol"], {})[int(r["timestamp"])] = pc
    return out


# --------------------------------------------------------------------------
# Network (only runs when a key is present)
# --------------------------------------------------------------------------


def _headers(key: str) -> dict[str, str]:
    return {"api_key": key, "Accept": "application/json"}


def _get(session, path: str, params: dict, key: str, *, retries: int = 4):
    import requests  # local import; not needed for the pure builders
    url = f"{API_BASE}{path}"
    for attempt in range(retries):
        resp = session.get(url, params=params, headers=_headers(key), timeout=30)
        if resp.status_code == 429:
            time.sleep(2 ** attempt * 2)
            continue
        resp.raise_for_status()
        return resp.json()
    resp.raise_for_status()
    return resp.json()


def discover_bitget_symbols(session, key: str, wanted_norm: set[str]) -> dict[str, str]:
    """Return {normalized_symbol -> coinalyze_symbol} for Bitget perps.
    Discovers the real Coinalyze symbol; never assumes a suffix."""
    exchanges = _get(session, "/exchanges", {}, key)
    bitget_code = None
    for ex in exchanges or []:
        if str(ex.get("name", "")).lower() == "bitget" or "bitget" in str(ex.get("name", "")).lower():
            bitget_code = ex.get("code")
            break
    markets = _get(session, "/future-markets", {}, key)
    out: dict[str, str] = {}
    for m in markets or []:
        if not m.get("is_perpetual"):
            continue
        base = str(m.get("base_asset", "")).upper()
        quote = str(m.get("quote_asset", "")).upper()
        norm = f"{base}{quote}"
        if norm not in wanted_norm:
            continue
        ex_code = m.get("exchange")
        if bitget_code is not None and ex_code != bitget_code:
            continue
        if bitget_code is None and "bitget" not in str(m.get("exchange", "")).lower():
            # fallback if /exchanges lacked a clean code
            continue
        # Prefer the first match per normalized symbol.
        out.setdefault(norm, m.get("symbol"))
    return out


def _chunks(frm_s: int, to_s: int, days: int):
    step = days * 86400
    a = frm_s
    while a < to_s:
        b = min(a + step, to_s)
        yield a, b
        a = b


def fetch_history(session, key: str, path: str, csymbols: list[str], interval: str,
                  frm_s: int, to_s: int, extra: dict | None = None) -> list[dict]:
    """Fetch a history endpoint across time chunks; merge per symbol."""
    merged: dict[str, dict[int, dict]] = {}
    sym_param = ",".join(csymbols)
    for a, b in _chunks(frm_s, to_s, CHUNK_DAYS):
        params = {"symbols": sym_param, "interval": interval, "from": a, "to": b}
        if extra:
            params.update(extra)
        data = _get(session, path, params, key)
        for entry in data or []:
            sym = entry.get("symbol")
            if not sym:
                continue
            bucket = merged.setdefault(sym, {})
            for p in entry.get("history") or []:
                if p.get("t") is not None:
                    bucket[int(p["t"])] = p
        time.sleep(RATE_SLEEP_S)
    return [{"symbol": s, "history": [pts[t] for t in sorted(pts)]} for s, pts in merged.items()]


def _write_csv(rows: list[dict[str, Any]], fields: list[str], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in fields})


MARKET_FIELDS = ["symbol", "exchange", "timestamp", "price_open", "price_high",
                 "price_low", "price_close", "volume_usd", "funding_rate",
                 "oi_usd_close", "source", "long_short_ratio", "ingested_at"]
LIQ_FIELDS = ["symbol", "exchange", "timestamp", "side", "notional_usd", "price",
              "source", "ingested_at"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Coinalyze V10.1 research-only fetcher")
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT")
    ap.add_argument("--days", type=int, default=75)
    ap.add_argument("--interval", default="1hour")
    args = ap.parse_args(argv)

    key = os.environ.get("COINALYZE_API_KEY")
    if not key:
        print("ABORT: COINALYZE_API_KEY is not set in the environment.")
        print("  Export it locally (never commit it, it will not be printed):")
        print("    export COINALYZE_API_KEY=...   # then re-run")
        print("  No data downloaded. No files written. No DB writes. NO LIVE.")
        print("final_recommendation: NO LIVE")
        return 0

    try:
        import requests
        session = requests.Session()
    except Exception as exc:  # pragma: no cover
        print(f"ABORT: HTTP client unavailable ({type(exc).__name__}). NO LIVE.")
        return 0

    wanted = {s.strip().upper() for s in args.symbols.split(",") if s.strip()}
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    to_s = int(datetime.now(timezone.utc).timestamp())
    frm_s = to_s - args.days * 86400

    try:
        sym_map = discover_bitget_symbols(session, key, wanted)
    except Exception as exc:
        print(f"ABORT: symbol discovery failed ({type(exc).__name__}). No files written. NO LIVE.")
        return 0
    if not sym_map:
        print("ABORT: no matching Bitget perpetual symbols found on Coinalyze. NO LIVE.")
        return 0
    print("discovered_symbols: " + ", ".join(f"{k}->{v}" for k, v in sym_map.items()))
    csymbols = list(sym_map.values())
    coinalyze_to_norm = {v: k for k, v in sym_map.items()}

    try:
        ohlcv = fetch_history(session, key, "/ohlcv-history", csymbols, args.interval, frm_s, to_s)
        oi = fetch_history(session, key, "/open-interest-history", csymbols, args.interval, frm_s, to_s, {"convert_to_usd": "true"})
        funding = fetch_history(session, key, "/funding-rate-history", csymbols, args.interval, frm_s, to_s)
        liq = fetch_history(session, key, "/liquidation-history", csymbols, args.interval, frm_s, to_s, {"convert_to_usd": "true"})
        try:
            lsr = fetch_history(session, key, "/long-short-ratio-history", csymbols, args.interval, frm_s, to_s)
        except Exception:
            lsr = []  # optional
    except Exception as exc:
        print(f"ABORT: history fetch failed ({type(exc).__name__}). NO LIVE.")
        return 0

    market_rows = build_market_rows(ohlcv=ohlcv, oi=oi, funding=funding, lsr=lsr,
                                    coinalyze_to_norm=coinalyze_to_norm)
    price_lookup = price_lookup_from_market_rows(market_rows)
    liq_rows, skipped = build_liquidation_rows(liquidations=liq, coinalyze_to_norm=coinalyze_to_norm,
                                               price_by_symbol_ts=price_lookup)

    market_path = Path(RAW_MARKET_DIR) / f"coinalyze_bitget_btc_eth_1h_{stamp}.csv"
    liq_path = Path(RAW_LIQ_DIR) / f"coinalyze_bitget_btc_eth_1h_{stamp}.csv"
    _write_csv(market_rows, MARKET_FIELDS, market_path)
    _write_csv(liq_rows, LIQ_FIELDS, liq_path)

    print(f"perp_market_state_rows: {len(market_rows)}")
    print(f"perp_liquidations_rows: {len(liq_rows)} (skipped_no_price: {skipped})")
    print(f"market_csv: {market_path}")
    print(f"liquidations_csv: {liq_path}")
    print("db_writes: 0")
    print("api_key_printed: false")
    print("final_recommendation: NO LIVE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
