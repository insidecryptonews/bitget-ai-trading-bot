"""ResearchOps V10.24 - Microstructure Sample Adapter (research only, OFFLINE).

Validate and normalize LOCAL microstructure samples (trades / orderbook L2 /
open interest / funding / liquidations) that a human downloaded from a provider.
This module NEVER touches the network, NO API keys, NO paid download, NO DB, NO
raw/production writes. It only READS local files and (optionally) writes
normalized CSVs into the v10_24 staging marker.

It produces a readiness scorecard so a later phase can decide whether
microstructure-aware research labs can be built -- but it builds NO strategy and
promotes nothing. FINAL_RECOMMENDATION: NO LIVE.
"""

from __future__ import annotations

import csv
import json
import math
import os
import re
import statistics as st
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import FINAL_RECOMMENDATION_NO_LIVE

TOOL_VERSION = "v10.24"
STAGING_MARKER = "microstructure_samples_v10_24"
DEFAULT_SAMPLE_DIR = f"external_data/staging/{STAGING_MARKER}"
OUTPUT_ROOT = "reports/research/v10_24"
DAY_MS = 86_400_000
MIN_HISTORY_DAYS = 30
MAX_ROWS = 2_000_000

# classification verdicts
C_NO_SAMPLE = "NO_SAMPLE"
C_INVALID = "INVALID_SAMPLE"
C_PARTIAL = "PARTIAL_SAMPLE"
C_READY = "MICROSTRUCTURE_RESEARCH_READY"
C_NEEDS_HISTORY = "NEEDS_MORE_HISTORY"
C_NEEDS_ORDERBOOK = "NEEDS_ORDERBOOK"
C_NEEDS_AGGRESSOR = "NEEDS_AGGRESSOR_SIDE"
C_NEEDS_LIQ = "NEEDS_LIQUIDATIONS"
C_NEEDS_OI = "NEEDS_OI"

_FORBIDDEN_SEG = ("raw", "backup", "backups", "vault", "vaults", "prod",
                  "production", "live", "real", "private", "training_exports",
                  "secret", "secrets", "credential", "credentials", "db",
                  "database", ".git", "node_modules", "codex_result.md",
                  "code_result.md")
_FORBIDDEN_SUF = (".env", ".db", ".sqlite", ".sqlite3", ".pem", ".key")

_TS_FIELDS = ("timestamp", "ts", "exchange_timestamp", "time", "datetime")
_SIZE_FIELDS = ("size", "qty", "amount", "quantity", "volume")
_SIDE_FIELDS = ("side", "aggressor_side", "taker_side", "maker_taker", "direction")
_OI_FIELDS = ("open_interest", "oi", "openinterest")


def _safety() -> dict[str, Any]:
    return {"research_only": True, "shadow_only": True, "paper_ready": False,
            "live_ready": False, "can_send_real_orders": False,
            "paper_filter_enabled": False, "paper_candidate_future": False,
            "makes_no_trades": True, "uses_network": False, "uses_db": False,
            "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# --------------------------------------------------------------------------
# Path safety
# --------------------------------------------------------------------------

def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _resolved(path: str | os.PathLike[str], *, relative_to_repo: bool = False) -> Path:
    p = Path(path)
    if relative_to_repo and not p.is_absolute():
        p = _repo_root() / p
    return p.resolve(strict=False)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _path_segments(path: str | os.PathLike[str]) -> list[str]:
    return [p.lower() for p in Path(path).parts if p not in ("", os.sep, ".")]


def _assert_no_forbidden_path(path: str | os.PathLike[str], what: str) -> None:
    segs = _path_segments(path)
    if ".." in segs:
        raise ValueError(f"{what} traversal blocked")
    for seg in segs:
        if seg in _FORBIDDEN_SEG or seg.endswith(_FORBIDDEN_SUF) or ".env" in seg:
            raise ValueError(f"forbidden {what} segment: {seg}")


def assert_safe_sample_dir(path: str) -> str:
    """Reject reading from dangerous locations (.env/db/raw/prod/backups/vault)."""
    if not isinstance(path, str) or not path.strip():
        raise ValueError("empty sample dir")
    _assert_no_forbidden_path(path, "sample")
    resolved = _resolved(path)
    _assert_no_forbidden_path(resolved, "sample")
    if Path(path).exists() and Path(path).is_symlink():
        raise ValueError("sample dir symlink blocked")
    return path


def assert_safe_sample_file(sample_dir: str, file_path: str) -> None:
    base = _resolved(sample_dir)
    original = Path(file_path)
    resolved = _resolved(file_path)
    _assert_no_forbidden_path(original, "sample file")
    _assert_no_forbidden_path(resolved, "sample file")
    if original.exists() and original.is_symlink():
        raise ValueError("sample file symlink blocked")
    if not _is_relative_to(resolved, base):
        raise ValueError("sample file escapes sample dir")


def safe_normalized_dir(run_id: str, base: str | None = None) -> str:
    """Normalized outputs may ONLY be written under the v10_24 staging marker."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", str(run_id or "")):
        raise ValueError("unsafe normalization run_id")
    expected_root = _resolved(DEFAULT_SAMPLE_DIR, relative_to_repo=True)
    root = _resolved(base or DEFAULT_SAMPLE_DIR, relative_to_repo=True)
    _assert_no_forbidden_path(root, "normalized")
    if root != expected_root:
        raise ValueError("normalization must live under the exact v10_24 staging marker")
    return str(root.joinpath(run_id, "normalized")).replace("\\", "/")


def _safe_output_base(output_dir: str | None) -> str:
    base = output_dir or OUTPUT_ROOT
    segs = [s for s in base.replace("\\", "/").split("/") if s]
    if ".." in segs:
        return OUTPUT_ROOT
    for s in (x.lower() for x in segs):
        if s in _FORBIDDEN_SEG or s.endswith(_FORBIDDEN_SUF) or ".env" in s:
            return OUTPUT_ROOT
    return base


# --------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------

def _to_ms(v: Any) -> int | None:
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    try:
        n = float(s)
        if n <= 0:
            return None
        if n < 1e11:          # seconds
            return int(n * 1000)
        if n < 1e14:          # milliseconds
            return int(n)
        return int(n / 1000)  # microseconds
    except ValueError:
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1000)
        except ValueError:
            return None


def _first(row: dict[str, Any], fields: tuple[str, ...]) -> Any:
    low = {k.lower(): v for k, v in row.items()}
    for f in fields:
        if f in low and str(low[f]).strip() != "":
            return low[f]
    return None


def _norm_side(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in ("buy", "b", "bid", "long", "taker_buy", "buyer", "1"):
        return "buy"
    if s in ("sell", "s", "ask", "short", "taker_sell", "seller", "-1", "0"):
        return "sell"
    return None


def _read_csv(path: str) -> tuple[list[str], list[dict[str, str]], bool]:
    rows: list[dict[str, str]] = []
    header: list[str] = []
    truncated = False
    try:
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            header = [h for h in (reader.fieldnames or [])]
            for i, r in enumerate(reader):
                if i >= MAX_ROWS:
                    truncated = True
                    break
                rows.append(r)
    except Exception:
        return [], [], False
    return header, rows, truncated


# --------------------------------------------------------------------------
# Schema detection
# --------------------------------------------------------------------------

def detect_type(fname: str, header: list[str]) -> str:
    f = fname.lower()
    cols = {c.lower() for c in header}
    if "trade" in f:
        return "trades"
    if "orderbook" in f or "order_book" in f or "_l2" in f or "book" in f:
        return "orderbook"
    if "liquidation" in f or re.search(r"(^|[_\-])liq", f):
        return "liquidations"
    if "funding" in f:
        return "funding"
    if "open_interest" in f or re.search(r"(^|[_\-])oi([_\-.]|$)", f):
        return "oi"
    # column fallback
    if "funding_rate" in cols:
        return "funding"
    if cols & set(_OI_FIELDS):
        return "oi"
    if any(c.startswith("bid_price") for c in cols) or {"bids", "asks"} & cols:
        return "orderbook"
    if "price" in cols and (cols & set(_SIZE_FIELDS)):
        return "trades"
    return "unknown"


def _parse_float(v: Any) -> float | None:
    try:
        n = float(str(v).strip())
    except (TypeError, ValueError):
        return None
    if math.isnan(n) or math.isinf(n):
        return None
    return n


def _parse_positive(v: Any) -> float | None:
    n = _parse_float(v)
    return n if n is not None and n > 0 else None


def _parse_nonnegative(v: Any) -> float | None:
    n = _parse_float(v)
    return n if n is not None and n >= 0 else None


def _median(vals: list[float]) -> float | None:
    return round(st.median(vals), 6) if vals else None


def _p95(vals: list[float]) -> float | None:
    if len(vals) <= 5:
        return None
    idx = min(len(vals) - 1, int((len(vals) - 1) * 0.95))
    return round(sorted(vals)[idx], 6)


def _coverage(ts_list: list[int], invalid_ts: int = 0) -> dict[str, Any]:
    if not ts_list:
        return {"rows": 0, "coverage_days": 0.0, "first_ts": None, "last_ts": None,
                "monotonic": True, "duplicates": 0, "duplicate_count": 0,
                "future_ts": 0, "future_ts_count": 0, "invalid_ts_count": invalid_ts,
                "median_gap_seconds": None, "max_gap_seconds": None,
                "gap_count": 0, "expected_interval_seconds": None}
    s = sorted(ts_list)
    gaps = [(s[i + 1] - s[i]) / 1000 for i in range(len(s) - 1) if s[i + 1] > s[i]]
    expected = st.median(gaps) if gaps else None
    gap_threshold = max((expected or 0) * 10, 3600) if expected else None
    gap_count = sum(1 for g in gaps if gap_threshold and g > gap_threshold)
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000) + DAY_MS
    duplicates = len(ts_list) - len(set(ts_list))
    future = sum(1 for t in s if t > now_ms)
    return {"rows": len(ts_list),
            "coverage_days": round((s[-1] - s[0]) / DAY_MS, 2),
            "first_ts": s[0], "last_ts": s[-1],
            "monotonic": all(ts_list[i] <= ts_list[i + 1] for i in range(len(ts_list) - 1)),
            "duplicates": duplicates, "duplicate_count": duplicates,
            "future_ts": future, "future_ts_count": future,
            "invalid_ts_count": invalid_ts,
            "median_gap_seconds": round(st.median(gaps), 3) if gaps else None,
            "max_gap_seconds": round(max(gaps), 3) if gaps else None,
            "gap_count": gap_count,
            "expected_interval_seconds": round(expected, 3) if expected else None}


def _duplicate_limit(cov: dict[str, Any]) -> int:
    rows = int(cov.get("rows") or 0)
    return max(1, int(rows * 0.01))


def _quality_flags(
    cov: dict[str, Any], *, require_no_gaps: bool = False, warning_mode: bool = False
) -> list[str]:
    flags: list[str] = []
    if cov.get("invalid_ts_count"):
        flags.append("invalid_timestamps")
    if cov.get("future_ts_count"):
        flags.append("future_timestamps")
    if cov.get("monotonic") is False:
        flags.append("non_monotonic_timestamps")
    duplicate_count = int(cov.get("duplicate_count") or 0)
    if duplicate_count and (warning_mode or duplicate_count > _duplicate_limit(cov)):
        flags.append("duplicate_timestamps")
    if require_no_gaps and cov.get("gap_count"):
        flags.append("timestamp_gaps")
    return flags


# --------------------------------------------------------------------------
# Per-type validators
# --------------------------------------------------------------------------

def validate_trades(rows: list[dict[str, str]]) -> dict[str, Any]:
    ts, prices, sizes, sides, syms = [], [], [], [], set()
    invalid_ts = missing_price = nonpositive_price = 0
    missing_size = nonpositive_size = missing_side = invalid_side = 0
    for r in rows:
        t = _to_ms(_first(r, _TS_FIELDS))
        if t is not None:
            ts.append(t)
        else:
            invalid_ts += 1
        p_raw = _first(r, ("price",))
        p = _parse_positive(p_raw)
        if p is not None:
            prices.append(p)
        elif p_raw is None:
            missing_price += 1
        else:
            nonpositive_price += 1
        sz_raw = _first(r, _SIZE_FIELDS)
        sz = _parse_positive(sz_raw)
        if sz is not None:
            sizes.append(sz)
        elif sz_raw is None:
            missing_size += 1
        else:
            nonpositive_size += 1
        side_raw = _first(r, _SIDE_FIELDS)
        sd = _norm_side(side_raw)
        if sd:
            sides.append(sd)
        elif side_raw is None:
            missing_side += 1
        else:
            invalid_side += 1
        sym = _first(r, ("symbol", "instrument", "pair"))
        if sym:
            syms.add(str(sym))
    cov = _coverage(ts, invalid_ts)
    has_aggr = bool(rows) and len(sides) == len(rows)
    buys = sides.count("buy")
    sells = sides.count("sell")
    critical = _quality_flags(cov)
    if missing_price or nonpositive_price:
        critical.append("trade_price_not_strictly_positive")
    if missing_size or nonpositive_size:
        critical.append("trade_size_not_strictly_positive")
    valid = bool(rows) and not critical and len(prices) == len(rows) and len(sizes) == len(rows) and has_aggr
    return {"type": "trades", "valid": valid, "has_aggressor_side": has_aggr,
            "symbols": sorted(syms), "coverage": cov,
            "buy_sell_imbalance": round((buys - sells) / (buys + sells), 4) if (buys + sells) else None,
            "trades_per_min": round(len(ts) / max(1.0, cov["coverage_days"] * 1440), 3) if cov["coverage_days"] else None,
            "price_positive": missing_price == 0 and nonpositive_price == 0,
            "size_positive": missing_size == 0 and nonpositive_size == 0,
            "side_available": missing_side == 0,
            "side_valid": missing_side == 0 and invalid_side == 0,
            "timestamp_valid": invalid_ts == 0 and cov.get("future_ts_count") == 0,
            "duplicate_count": cov.get("duplicate_count", 0),
            "monotonic": cov.get("monotonic", True),
            "future_ts_count": cov.get("future_ts_count", 0),
            "missing_price_count": missing_price,
            "nonpositive_price_count": nonpositive_price,
            "missing_size_count": missing_size,
            "nonpositive_size_count": nonpositive_size,
            "missing_side_count": missing_side,
            "invalid_side_count": invalid_side,
            "critical_errors": critical,
            "warnings": _quality_flags(cov, require_no_gaps=True, warning_mode=True)}


def validate_orderbook(rows: list[dict[str, str]]) -> dict[str, Any]:
    ts, spreads, crossed, depth_levels = [], [], 0, 0
    invalid_ts = invalid_price = invalid_size = 0
    l1_imbalance: list[float] = []
    l5_imbalance: list[float] = []
    syms = set()
    for r in rows:
        low = {k.lower(): v for k, v in r.items()}
        t = _to_ms(_first(r, _TS_FIELDS))
        if t is not None:
            ts.append(t)
        else:
            invalid_ts += 1
        sym = _first(r, ("symbol", "instrument", "pair"))
        if sym:
            syms.add(str(sym))
        bid = ask = None
        bids: list[tuple[float, float | None]] = []
        asks: list[tuple[float, float | None]] = []
        if "bids" in low and "asks" in low:               # nested json string
            try:
                raw_bids = json.loads(low["bids"])
                raw_asks = json.loads(low["asks"])
                for level in raw_bids[:20]:
                    is_level = isinstance(level, (list, tuple))
                    price = _parse_positive(level[0] if is_level and level else None)
                    size_raw = level[1] if is_level and len(level) > 1 else None
                    size = _parse_positive(size_raw)
                    if size_raw is not None and size is None:
                        invalid_size += 1
                    if price is not None:
                        bids.append((price, size))
                for level in raw_asks[:20]:
                    is_level = isinstance(level, (list, tuple))
                    price = _parse_positive(level[0] if is_level and level else None)
                    size_raw = level[1] if is_level and len(level) > 1 else None
                    size = _parse_positive(size_raw)
                    if size_raw is not None and size is None:
                        invalid_size += 1
                    if price is not None:
                        asks.append((price, size))
            except Exception:
                pass
        else:                                              # flat bid_price_1 / ask_price_1
            levels = sorted(
                int(m.group(1)) for k in low for m in [re.match(r"bid_price_(\d+)$", k)] if m
            )
            for level in levels[:20]:
                bid_p = _parse_positive(low.get(f"bid_price_{level}"))
                ask_p = _parse_positive(low.get(f"ask_price_{level}"))
                bid_s_raw = low.get(f"bid_size_{level}") or low.get(f"bid_qty_{level}")
                ask_s_raw = low.get(f"ask_size_{level}") or low.get(f"ask_qty_{level}")
                bid_s = _parse_positive(bid_s_raw)
                ask_s = _parse_positive(ask_s_raw)
                if bid_s_raw not in (None, "") and bid_s is None:
                    invalid_size += 1
                if ask_s_raw not in (None, "") and ask_s is None:
                    invalid_size += 1
                if bid_p is not None:
                    bids.append((bid_p, bid_s))
                if ask_p is not None:
                    asks.append((ask_p, ask_s))
        if bids and asks:
            bid, ask = bids[0][0], asks[0][0]
            depth_levels = max(depth_levels, min(len(bids), len(asks)))
        if bid is not None and ask is not None and bid > 0 and ask > 0:
            if bid >= ask:
                crossed += 1
            else:
                spreads.append((ask - bid) / ((ask + bid) / 2))
            if bids[0][1] is not None and asks[0][1] is not None:
                denom = (bids[0][1] or 0) + (asks[0][1] or 0)
                if denom > 0:
                    l1_imbalance.append(((bids[0][1] or 0) - (asks[0][1] or 0)) / denom)
            bid_sum = sum(sz for _, sz in bids[:5] if sz is not None)
            ask_sum = sum(sz for _, sz in asks[:5] if sz is not None)
            if bid_sum + ask_sum > 0:
                l5_imbalance.append((bid_sum - ask_sum) / (bid_sum + ask_sum))
        else:
            invalid_price += 1
    cov = _coverage(ts, invalid_ts)
    critical = _quality_flags(cov)
    if invalid_price:
        critical.append("orderbook_bid_ask_not_strictly_positive")
    if crossed:
        critical.append("crossed_orderbook")
    if invalid_size:
        critical.append("orderbook_size_not_strictly_positive")
    valid = bool(rows) and bool(spreads) and not critical
    return {"type": "orderbook", "valid": valid, "symbols": sorted(syms), "coverage": cov,
            "depth_levels": depth_levels, "crossed_book_rows": crossed,
            "spread_median": _median(spreads),
            "spread_p95": _p95(spreads),
            "l1_imbalance_median": _median(l1_imbalance),
            "l5_imbalance_median": _median(l5_imbalance),
            "invalid_price_rows": invalid_price,
            "invalid_size_rows": invalid_size,
            "critical_errors": critical,
            "warnings": _quality_flags(cov, require_no_gaps=True, warning_mode=True)}


def validate_oi(rows: list[dict[str, str]]) -> dict[str, Any]:
    ts, vals, syms = [], 0, set()
    invalid_ts = missing_value = nonpositive_value = missing_symbol = 0
    for r in rows:
        t = _to_ms(_first(r, _TS_FIELDS))
        if t is not None:
            ts.append(t)
        else:
            invalid_ts += 1
        raw = _first(r, _OI_FIELDS)
        val = _parse_nonnegative(raw)
        if val is not None:
            vals += 1
        elif raw is None:
            missing_value += 1
        else:
            nonpositive_value += 1
        sym = _first(r, ("symbol", "instrument", "pair"))
        if sym:
            syms.add(str(sym))
        else:
            missing_symbol += 1
    cov = _coverage(ts, invalid_ts)
    missing = round((missing_value + nonpositive_value) / len(rows), 4) if rows else 1.0
    critical = _quality_flags(cov)
    if missing_value or nonpositive_value:
        critical.append("oi_negative_or_non_numeric")
    if missing_symbol:
        critical.append("oi_symbol_missing")
    return {"type": "oi", "valid": bool(rows) and not critical and vals == len(rows),
            "symbols": sorted(syms), "coverage": cov, "missing_ratio": missing,
            "missing_symbol_count": missing_symbol,
            "critical_errors": critical,
            "warnings": _quality_flags(cov, require_no_gaps=True, warning_mode=True)}


def validate_funding(rows: list[dict[str, str]]) -> dict[str, Any]:
    ts, vals, syms = [], 0, set()
    invalid_ts = missing_value = invalid_value = missing_symbol = 0
    for r in rows:
        t = _to_ms(_first(r, _TS_FIELDS))
        if t is not None:
            ts.append(t)
        else:
            invalid_ts += 1
        low = {k.lower(): v for k, v in r.items()}
        raw = low.get("funding_rate")
        val = _parse_float(raw)
        if val is not None and abs(val) <= 0.05:
            vals += 1
        elif raw is None or str(raw).strip() == "":
            missing_value += 1
        else:
            invalid_value += 1
        sym = _first(r, ("symbol", "instrument", "pair"))
        if sym:
            syms.add(str(sym))
        else:
            missing_symbol += 1
    cov = _coverage(ts, invalid_ts)
    critical = _quality_flags(cov)
    if missing_value or invalid_value:
        critical.append("funding_rate_invalid_or_absurd")
    if missing_symbol:
        critical.append("funding_symbol_missing")
    return {"type": "funding", "valid": bool(rows) and not critical and vals == len(rows),
            "symbols": sorted(syms), "coverage": cov,
            "missing_ratio": round((missing_value + invalid_value) / len(rows), 4) if rows else 1.0,
            "missing_symbol_count": missing_symbol,
            "critical_errors": critical,
            "warnings": _quality_flags(cov, require_no_gaps=True, warning_mode=True)}


def validate_liquidations(rows: list[dict[str, str]]) -> dict[str, Any]:
    ts, sides, notional_ok, syms = [], [], 0, set()
    invalid_ts = missing_side = invalid_side = 0
    missing_price = nonpositive_price = missing_size = nonpositive_size = 0
    missing_notional = nonpositive_notional = missing_symbol = 0
    for r in rows:
        t = _to_ms(_first(r, _TS_FIELDS))
        if t is not None:
            ts.append(t)
        else:
            invalid_ts += 1
        side_raw = _first(r, _SIDE_FIELDS)
        sd = _norm_side(side_raw)
        if sd:
            sides.append(sd)
        elif side_raw is None:
            missing_side += 1
        else:
            invalid_side += 1
        low = {k.lower(): v for k, v in r.items()}
        p_raw = _first(r, ("price",))
        sz_raw = _first(r, _SIZE_FIELDS)
        p = _parse_positive(p_raw)
        sz = _parse_positive(sz_raw)
        if p is None:
            if p_raw is None:
                missing_price += 1
            else:
                nonpositive_price += 1
        if sz is None:
            if sz_raw is None:
                missing_size += 1
            else:
                nonpositive_size += 1
        n_raw = low.get("notional")
        n = _parse_positive(n_raw)
        if n is not None:
            notional_ok += 1
        elif p is not None and sz is not None:
            notional_ok += 1
        elif n_raw is None or str(n_raw).strip() == "":
            missing_notional += 1
        else:
            nonpositive_notional += 1
        sym = _first(r, ("symbol", "instrument", "pair"))
        if sym:
            syms.add(str(sym))
        else:
            missing_symbol += 1
    cov = _coverage(ts, invalid_ts)
    critical = _quality_flags(cov)
    if missing_price or nonpositive_price:
        critical.append("liquidation_price_not_strictly_positive")
    if missing_size or nonpositive_size:
        critical.append("liquidation_size_not_strictly_positive")
    if missing_notional or nonpositive_notional:
        critical.append("liquidation_notional_not_calculable")
    if missing_side or invalid_side:
        critical.append("liquidation_side_invalid")
    if missing_symbol:
        critical.append("liquidation_symbol_missing")
    return {"type": "liquidations", "valid": bool(rows) and not critical and notional_ok == len(rows)
            and len(sides) == len(rows), "symbols": sorted(syms), "coverage": cov,
            "side_valid": missing_side == 0 and invalid_side == 0,
            "notional_calculable": notional_ok == len(rows),
            "missing_side_count": missing_side,
            "invalid_side_count": invalid_side,
            "missing_symbol_count": missing_symbol,
            "critical_errors": critical,
            "warnings": _quality_flags(cov, require_no_gaps=True, warning_mode=True)}


_VALIDATORS = {"trades": validate_trades, "orderbook": validate_orderbook,
               "oi": validate_oi, "funding": validate_funding,
               "liquidations": validate_liquidations}


# --------------------------------------------------------------------------
# Plan / validate / classify
# --------------------------------------------------------------------------

def microstructure_plan() -> dict[str, Any]:
    return {
        "tool_version": TOOL_VERSION,
        "objective": "validate + normalize LOCAL microstructure samples; build NO strategy",
        "reads_only_local_files": True, "uses_network": False, "uses_db": False,
        "default_sample_dir": DEFAULT_SAMPLE_DIR,
        "expected_types": ["trades", "orderbook", "oi", "funding", "liquidations"],
        "expected_fields": {
            "trades": ["timestamp", "symbol", "price", "size|qty|amount", "side|aggressor_side"],
            "orderbook": ["timestamp", "symbol", "bid_price_1..N/ask_price_1..N or bids/asks json"],
            "oi": ["timestamp", "symbol", "open_interest|oi"],
            "funding": ["timestamp", "symbol", "funding_rate"],
            "liquidations": ["timestamp", "symbol", "side", "price", "size|qty", "notional?"]},
        "verdicts": [C_NO_SAMPLE, C_INVALID, C_PARTIAL, C_READY, C_NEEDS_HISTORY,
                     C_NEEDS_ORDERBOOK, C_NEEDS_AGGRESSOR, C_NEEDS_LIQ, C_NEEDS_OI],
        "never": ["order_submission", "leverage_or_margin_change", "private_exchange_endpoint",
                  "paid_download", "db_write", "raw_write", "paper_or_live_promotion"],
        "writes_on_plan": False, **_safety()}


def _future_research_ready(present: dict[str, dict]) -> dict[str, bool]:
    ob = present.get("orderbook", {}).get("valid", False)
    tr = present.get("trades", {})
    tr_aggr = tr.get("valid", False) and tr.get("has_aggressor_side", False)
    return {
        "orderbook_pressure": ob,
        "spread_slippage_estimator": ob,
        "liquidation_cluster_detector": present.get("liquidations", {}).get("valid", False),
        "aggressive_flow_imbalance": tr_aggr,
        "oi_expansion_contraction": present.get("oi", {}).get("valid", False),
        "funding_crowding": present.get("funding", {}).get("valid", False),
        "microstructure_aware_exit_simulation": bool(ob and tr_aggr)}


def classify_sample(present: dict[str, dict], files_seen: int) -> dict[str, Any]:
    gaps: list[str] = []
    critical_errors: list[str] = []
    warnings: list[str] = []
    valid_types = {k for k, v in present.items() if v.get("valid")}
    if files_seen == 0 or not present:
        return {"verdict": C_NO_SAMPLE, "gaps": [], "active_gaps": [],
                "valid_types": [], "critical_errors": [],
                "warnings": [], "can_research_microstructure": False,
                "future_research_ready_if_sample_passes": False,
                "why_not_ready": ["no sample files"]}
    max_cov = max((v.get("coverage", {}).get("coverage_days", 0) or 0) for v in present.values())
    required_types = ("trades", "orderbook", "oi", "liquidations")
    required_covs = [
        present[t].get("coverage", {}).get("coverage_days", 0) or 0
        for t in required_types
        if present.get(t, {}).get("valid")
    ]
    min_required_cov = min(required_covs) if required_covs else 0
    tr = present.get("trades", {})
    for dtype, metrics in sorted(present.items()):
        for err in metrics.get("critical_errors", []):
            if dtype == "trades" and err in ("missing_or_invalid_aggressor_side",):
                continue
            critical_errors.append(f"{dtype}:{err}")
        for warn in metrics.get("warnings", []):
            warnings.append(f"{dtype}:{warn}")
        cov = metrics.get("coverage", {})
        if cov.get("gap_count"):
            gaps.append(C_NEEDS_HISTORY)
    if tr and not tr.get("has_aggressor_side"):
        gaps.append(C_NEEDS_AGGRESSOR)
    if "trades" not in valid_types:
        gaps.append(C_NEEDS_AGGRESSOR)
    if "orderbook" not in valid_types:
        gaps.append(C_NEEDS_ORDERBOOK)
    if "oi" not in valid_types:
        gaps.append(C_NEEDS_OI)
    if "liquidations" not in valid_types:
        gaps.append(C_NEEDS_LIQ)
    if max_cov < MIN_HISTORY_DAYS or any(
        present.get(t, {}).get("valid")
        and (present[t].get("coverage", {}).get("coverage_days", 0) or 0) < MIN_HISTORY_DAYS
        for t in required_types
    ):
        gaps.append(C_NEEDS_HISTORY)
    funding_optional_reason = None
    if "funding" not in present and "oi" in valid_types:
        funding_optional_reason = "funding is optional for V10.24 readiness when OI is present and valid"
    if "funding" in present and "funding" not in valid_types:
        critical_errors.append("funding:present_but_invalid")
    active_gaps = sorted(set(gaps))
    critical_errors = sorted(set(critical_errors))
    ready = (
        tr.get("valid")
        and tr.get("has_aggressor_side")
        and "orderbook" in valid_types
        and "oi" in valid_types
        and "liquidations" in valid_types
        and min_required_cov >= MIN_HISTORY_DAYS
        and not active_gaps
        and not critical_errors
    )
    if ready:
        verdict = C_READY
    elif critical_errors:
        verdict = C_INVALID
    elif C_NEEDS_HISTORY in active_gaps and valid_types:
        verdict = C_NEEDS_HISTORY
    elif tr and not tr.get("has_aggressor_side"):
        verdict = C_NEEDS_AGGRESSOR
    elif "orderbook" not in valid_types:
        verdict = C_NEEDS_ORDERBOOK
    elif "liquidations" not in valid_types:
        verdict = C_NEEDS_LIQ
    elif "oi" not in valid_types:
        verdict = C_NEEDS_OI
    else:
        verdict = C_PARTIAL
    why_not_ready = critical_errors + active_gaps
    return {"verdict": verdict, "readiness_verdict": verdict,
            "gaps": active_gaps, "active_gaps": active_gaps,
            "critical_errors": critical_errors,
            "warnings": sorted(set(warnings)),
            "why_not_ready": why_not_ready,
            "valid_types": sorted(valid_types),
            "max_coverage_days": max_cov,
            "min_required_coverage_days": min_required_cov,
            "can_research_microstructure": bool(ready),
            "future_research_ready_if_sample_passes": bool(ready),
            "funding_optional_reason": funding_optional_reason,
            "future_labs_ready": _future_research_ready(present)}


def validate_sample(sample_dir: str, apply_normalization: bool = False) -> dict[str, Any]:
    rep: dict[str, Any] = {"tool_version": TOOL_VERSION, "generated_at": _now_stamp(),
                           "sample_dir": sample_dir, "timezone": "UTC (timestamps normalized to ms)",
                           "files": [], "by_type": {}, "errors": [], **_safety()}
    try:
        assert_safe_sample_dir(sample_dir)
    except ValueError as e:
        rep["errors"].append(f"unsafe_sample_dir:{e}")
        rep["classification"] = {"verdict": C_INVALID, "gaps": ["unsafe sample dir"],
                                 "active_gaps": ["unsafe sample dir"],
                                 "valid_types": [],
                                 "critical_errors": [f"unsafe_sample_dir:{e}"],
                                 "warnings": [],
                                 "can_research_microstructure": False,
                                 "future_research_ready_if_sample_passes": False,
                                 "why_not_ready": [f"unsafe_sample_dir:{e}"]}
        return rep
    if not os.path.isdir(sample_dir):
        rep["classification"] = {"verdict": C_NO_SAMPLE, "gaps": [], "active_gaps": [],
                                 "valid_types": [], "critical_errors": [], "warnings": [],
                                 "can_research_microstructure": False,
                                 "future_research_ready_if_sample_passes": False,
                                 "why_not_ready": ["sample dir missing"]}
        return rep
    present: dict[str, dict] = {}
    files_seen = 0
    for name in sorted(os.listdir(sample_dir)):
        path = os.path.join(sample_dir, name)
        if not os.path.isfile(path) or not name.lower().endswith((".csv", ".tsv")):
            continue
        files_seen += 1
        try:
            assert_safe_sample_file(sample_dir, path)
        except ValueError as e:
            rep["errors"].append(f"unsafe_sample_file:{name}:{e}")
            rep["files"].append({"file": name, "detected_type": "unknown", "rows": 0,
                                 "truncated": False, "columns": [], "valid": False,
                                 "error": f"unsafe_sample_file:{e}"})
            continue
        header, rows, truncated = _read_csv(path)
        dtype = detect_type(name, header)
        info = {"file": name, "detected_type": dtype, "rows": len(rows),
                "truncated": truncated, "columns": header}
        if dtype in _VALIDATORS and rows:
            res = _VALIDATORS[dtype](rows)
            info.update({"valid": res["valid"], "metrics": res})
            # keep the richest validation per type
            if dtype not in present or (res["valid"] and not present[dtype].get("valid")):
                present[dtype] = res
        else:
            info["valid"] = False
        rep["files"].append(info)
    rep["by_type"] = {k: v for k, v in present.items()}
    rep["classification"] = classify_sample(present, files_seen)
    rep["critical_errors"] = rep["classification"].get("critical_errors", [])
    rep["warnings"] = rep["classification"].get("warnings", [])
    if (apply_normalization and rep["classification"].get("valid_types")
            and not rep["classification"].get("critical_errors")):
        rep["normalization"] = _write_normalized(sample_dir, rep)
    else:
        reason = "flag off or no valid types"
        if apply_normalization and rep["classification"].get("critical_errors"):
            reason = "blocked by critical validation errors"
        rep["normalization"] = {"applied": False, "reason": reason}
    return rep


def _write_normalized(sample_dir: str, rep: dict[str, Any]) -> dict[str, Any]:
    run_id = _now_stamp()
    out_dir = safe_normalized_dir(run_id)
    os.makedirs(out_dir, exist_ok=True)
    written = []
    # re-read each recognized file and emit a canonical normalized CSV per type
    headers = {"trades": ["ts_ms", "symbol", "price", "size", "side"],
               "orderbook": ["ts_ms", "symbol", "bid_price_1", "ask_price_1", "spread"],
               "oi": ["ts_ms", "symbol", "open_interest"],
               "funding": ["ts_ms", "symbol", "funding_rate"],
               "liquidations": ["ts_ms", "symbol", "side", "price", "size", "notional"]}
    for finfo in rep["files"]:
        dtype = finfo["detected_type"]
        if dtype not in headers or not finfo.get("valid"):
            continue
        src_path = os.path.join(sample_dir, finfo["file"])
        assert_safe_sample_file(sample_dir, src_path)
        header, rows, _ = _read_csv(src_path)
        out_path = os.path.join(out_dir, f"{dtype}_normalized.csv").replace("\\", "/")
        new_file = not os.path.exists(out_path)
        with open(out_path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new_file:
                w.writerow(headers[dtype])
            for r in rows:
                w.writerow(_normalize_row(dtype, r))
        if out_path not in written:
            written.append(out_path)
    return {"applied": True, "out_dir": out_dir, "files": written,
            "wrote_only_staging_marker": STAGING_MARKER in out_dir}


def _normalize_row(dtype: str, r: dict[str, str]) -> list[Any]:
    ts = _to_ms(_first(r, _TS_FIELDS))
    sym = _first(r, ("symbol", "instrument", "pair")) or ""
    if dtype == "trades":
        return [ts, sym, _first(r, ("price",)), _first(r, _SIZE_FIELDS), _norm_side(_first(r, _SIDE_FIELDS))]
    if dtype == "orderbook":
        low = {k.lower(): v for k, v in r.items()}
        try:
            bid = float(low.get("bid_price_1")); ask = float(low.get("ask_price_1"))
            spread = (ask - bid) / ((ask + bid) / 2) if (bid > 0 and ask > 0) else ""
        except (TypeError, ValueError):
            bid = ask = spread = ""
        return [ts, sym, low.get("bid_price_1", ""), low.get("ask_price_1", ""), spread]
    if dtype == "oi":
        return [ts, sym, _first(r, _OI_FIELDS)]
    if dtype == "funding":
        return [ts, sym, {k.lower(): v for k, v in r.items()}.get("funding_rate", "")]
    if dtype == "liquidations":
        low = {k.lower(): v for k, v in r.items()}
        return [ts, sym, _norm_side(_first(r, _SIDE_FIELDS)), _first(r, ("price",)),
                _first(r, _SIZE_FIELDS), low.get("notional", "")]
    return [ts, sym]


def write_reports(rep: dict[str, Any], output_dir: str | None = None) -> dict[str, str]:
    base = _safe_output_base(output_dir)
    os.makedirs(base, exist_ok=True)
    sc = os.path.join(base, "microstructure_sample_scorecard.json").replace("\\", "/")
    with open(sc, "w", encoding="utf-8") as f:
        json.dump(rep, f, indent=2, default=str)
    cls = rep.get("classification", {})
    md = os.path.join(base, "microstructure_sample_readiness_report.md").replace("\\", "/")
    lines = ["# V10.24 Microstructure Sample Readiness (research only)", "",
             f"sample_dir: {rep.get('sample_dir')}", f"verdict: {cls.get('verdict')}",
             f"readiness_verdict: {cls.get('readiness_verdict') or cls.get('verdict')}",
             f"can_research_microstructure: {cls.get('can_research_microstructure')}",
             f"valid_types: {cls.get('valid_types')}", f"active_gaps: {cls.get('active_gaps')}",
             f"critical_errors: {cls.get('critical_errors')}",
             f"why_not_ready: {cls.get('why_not_ready')}",
             f"max_coverage_days: {cls.get('max_coverage_days')}",
             f"min_required_coverage_days: {cls.get('min_required_coverage_days')}",
             f"funding_optional_reason: {cls.get('funding_optional_reason')}",
             f"future_research_ready_if_sample_passes: {cls.get('future_research_ready_if_sample_passes')}",
             "", "## future labs ready (only if a passing sample arrives)"]
    for lab, ok in (cls.get("future_labs_ready") or {}).items():
        lines.append(f"- {lab}: {ok}")
    lines += ["", "final_recommendation: NO LIVE"]
    with open(md, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return {"scorecard": sc, "report": md}
