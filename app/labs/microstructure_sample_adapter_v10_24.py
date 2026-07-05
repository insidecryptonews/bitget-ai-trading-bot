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
import uuid
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
    # microsecond precision + short uuid so two normalizations never collide
    return (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            + "_" + uuid.uuid4().hex[:8])


# Suspicious tokens: a recognized OR unknown file matching these (by name or
# header) is a credential/secret/raw-data leak risk and must block readiness.
_SECRET_HEADER_TOKENS = ("api_key", "apikey", "api-key", "secret", "token",
                         "private_key", "password", "passwd", "passphrase",
                         "access_key", "credential", "auth")
_SUSPICIOUS_NAME_TOKENS = _SECRET_HEADER_TOKENS + (
    "env", "db", "database", "raw", "prod", "production", "live", "real",
    "vault", "backup", "zip")
# benign unknown files that never block (read for completeness, not validated)
_BENIGN_UNKNOWN = ("readme", "notes", ".gitkeep", "manifest", "metadata")

# Conservative density floors required for MICROSTRUCTURE_RESEARCH_READY.
_MIN_ROWS = {"trades": 1000, "orderbook": 100, "oi": 24, "liquidations": 20}
_MIN_ROWS_PER_DAY = {"trades": 5.0, "orderbook": 1.0, "oi": 0.5, "liquidations": 0.5}


def _is_suspicious_file(name: str, header: list[str]) -> bool:
    low_name = name.lower()
    if any(tok in low_name for tok in _SUSPICIOUS_NAME_TOKENS):
        return True
    cols = " ".join(c.lower() for c in (header or []))
    return any(tok in cols for tok in _SECRET_HEADER_TOKENS)


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
    """Reject reading from dangerous locations (.env/db/raw/prod/backups/vault),
    the dir being a symlink, OR any ANCESTOR component being a symlink (an
    ancestor symlink could resolve the whole tree outside the allowed area)."""
    if not isinstance(path, str) or not path.strip():
        raise ValueError("empty sample dir")
    _assert_no_forbidden_path(path, "sample")
    resolved = _resolved(path)
    _assert_no_forbidden_path(resolved, "sample")
    p = Path(path)
    if p.exists() and p.is_symlink():
        raise ValueError("sample dir symlink blocked")
    for anc in p.parents:
        try:
            if anc.is_symlink():
                raise ValueError(f"symlinked ancestor blocked: {anc}")
        except OSError:
            break
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
    """Reports may ONLY be written under reports/research/v10_24. Anything that
    resolves outside (incl. via symlink/traversal) falls back to OUTPUT_ROOT."""
    if not output_dir:
        return OUTPUT_ROOT
    try:
        _assert_no_forbidden_path(output_dir, "output")
    except ValueError:
        return OUTPUT_ROOT
    expected = _resolved(OUTPUT_ROOT, relative_to_repo=True)
    resolved = _resolved(output_dir, relative_to_repo=True)
    if resolved == expected or _is_relative_to(resolved, expected):
        return output_dir
    return OUTPUT_ROOT


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


def _read_csv(path: str) -> tuple[list[str], list[dict[str, str]], bool, bool]:
    """Returns (header, rows, truncated, parse_error). parse_error=True means the
    file could not be read/parsed -> caller must treat a RECOGNIZED file as INVALID
    (never silently degrade corruption to 'unknown/no rows')."""
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
        return [], [], False, True
    return header, rows, truncated, False


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
    # V10.24.4/.5 correction: same-millisecond DISTINCT trades are genuine
    # market data (real Binance aggTrades reach >55% same-ms collision under
    # bursts), so NO collision-ratio threshold may invalidate trades. For
    # trades, corruption is: exact duplicate rows (ts+price+size+side) above
    # the 1% limit, or duplicated trade ids when an id column exists. Same-ms
    # collisions alone are surfaced as a WARNING, never INVALID.
    exact_dups = len(rows) - len({(str(_first(r, _TS_FIELDS)), str(_first(r, ("price",))),
                                   str(_first(r, _SIZE_FIELDS)), str(_first(r, _SIDE_FIELDS)))
                                  for r in rows})
    ts_collisions = int(cov.get("duplicate_count") or 0)
    trade_ids = [v for v in (_first(r, ("agg_trade_id", "trade_id", "raw_event_id", "id"))
                             for r in rows) if v not in (None, "")]
    id_dups = (len(trade_ids) - len(set(trade_ids))) if trade_ids else 0
    cov["ts_collision_count"] = ts_collisions
    cov["exact_duplicate_rows"] = exact_dups
    cov["id_duplicate_rows"] = id_dups
    # V10.24.6: when EVERY row carries a unique-id column, the id is the truth
    # source -- real bursts legitimately repeat (ts,price,size,side) with
    # distinct ids (live Bybit data reproduced this), so exact-tuple dups only
    # count when no per-row id exists. Duplicated ids always stay corruption.
    if trade_ids and len(trade_ids) == len(rows):
        severe = id_dups
    else:
        severe = max(exact_dups, id_dups)
    if severe > _duplicate_limit(cov):
        # true corruption: make the flag fire even when the duplicated rows
        # carry distinct timestamps (id dups without ts collisions)
        cov["duplicate_count"] = max(ts_collisions, severe)
        cov["duplicates"] = cov["duplicate_count"]
    else:
        cov["duplicate_count"] = 0
        cov["duplicates"] = 0
    has_aggr = bool(rows) and len(sides) == len(rows)
    buys = sides.count("buy")
    sells = sides.count("sell")
    critical = _quality_flags(cov)
    if missing_price or nonpositive_price:
        critical.append("trade_price_not_strictly_positive")
    if missing_size or nonpositive_size:
        critical.append("trade_size_not_strictly_positive")
    if invalid_side:
        # a present-but-unrecognized side (e.g. "hold") is corruption -> INVALID.
        # missing side (no column) is only a NEEDS_AGGRESSOR gap, not corruption.
        critical.append("trade_side_invalid")
    # structural validity (corrupt rows). Aggressor-side availability is a
    # SEPARATE readiness gate (has_aggressor_side) -> a clean trades file without
    # aggressor side is a NEEDS_AGGRESSOR gap, not an INVALID/corrupt file.
    valid = bool(rows) and not critical and len(prices) == len(rows) and len(sizes) == len(rows)
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
            "warnings": _quality_flags(cov, require_no_gaps=True, warning_mode=True)
            + ([f"same_ms_collision_warning(ratio={round(ts_collisions / len(rows), 3)})"]
               if ts_collisions and rows else [])}


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
    l1_med = _median(l1_imbalance)
    l5_med = _median(l5_imbalance)
    l1_available = l1_med is not None          # L1 imbalance needs bid_size_1 & ask_size_1
    l5_available = l5_med is not None
    l5_optional_reason = None
    if depth_levels < 5:
        l5_optional_reason = f"only {depth_levels} level(s) present; L5 imbalance optional (L1 required)"
    # structurally valid (no corrupt rows). L1-size availability is a SEPARATE
    # readiness gate handled in classify_sample, not a corruption error.
    valid = bool(rows) and bool(spreads) and not critical
    return {"type": "orderbook", "valid": valid, "symbols": sorted(syms), "coverage": cov,
            "depth_levels": depth_levels, "depth_levels_available": depth_levels,
            "crossed_book_rows": crossed,
            "spread_median": _median(spreads),
            "spread_p95": _p95(spreads),
            "l1_imbalance_median": l1_med,
            "l5_imbalance_median": l5_med,
            "l1_imbalance_available": l1_available,
            "l1_sizes_available": l1_available,
            "l5_imbalance_available": l5_available,
            "l5_optional_reason": l5_optional_reason,
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


def _future_research_ready(present: dict[str, dict], density_ok: dict[str, bool] | None = None) -> dict[str, bool]:
    """Each future lab flag reflects the REAL readiness of its component (not just
    valid=True): orderbook needs L1 sizes + no gaps, flow needs aggressor side,
    cluster/OI need density."""
    density_ok = density_ok or {}
    ob_m = present.get("orderbook", {})
    ob_clean = bool(ob_m.get("valid")) and not ob_m.get("coverage", {}).get("gap_count")
    ob_l1 = ob_clean and ob_m.get("l1_imbalance_median") is not None
    spread_ok = ob_clean and ob_m.get("spread_median") is not None
    tr = present.get("trades", {})
    tr_aggr = bool(tr.get("valid")) and bool(tr.get("has_aggressor_side"))
    liq_ok = bool(present.get("liquidations", {}).get("valid")) and density_ok.get("liquidations", False)
    oi_ok = bool(present.get("oi", {}).get("valid")) and density_ok.get("oi", False)
    return {
        "orderbook_pressure": ob_l1,
        "spread_slippage_estimator": spread_ok,
        "liquidation_cluster_detector": liq_ok,
        "aggressive_flow_imbalance": tr_aggr,
        "oi_expansion_contraction": oi_ok,
        "funding_crowding": bool(present.get("funding", {}).get("valid")),
        "microstructure_aware_exit_simulation": bool(ob_l1 and tr_aggr)}


def classify_sample(file_results: list[dict], by_type: dict[str, dict],
                    recognized_count: int, *, rep_errors: list[str] | None = None,
                    suspicious_files: list[str] | None = None) -> dict[str, Any]:
    """Fail-closed multi-file readiness. Inspects EVERY recognized file, blocks on
    any rep_errors (unsafe files), suspicious/secret-like files, symbol
    misalignment and absurd density -- so READY can only ever be emitted for a
    fully clean, aligned, dense, secret-free sample."""
    rep_errors = list(rep_errors or [])
    suspicious_files = list(suspicious_files or [])
    recognized = [fr for fr in file_results if fr.get("detected_type") in _VALIDATORS]
    required_types = ("trades", "orderbook", "oi", "liquidations")
    if recognized_count == 0 or not recognized:
        # still surface unsafe/suspicious so a dir of only-bad files is not NO_SAMPLE
        if rep_errors or suspicious_files:
            ce = [f"unsafe_or_suspicious:{e}" for e in (rep_errors + suspicious_files)]
            return {"verdict": C_INVALID, "readiness_verdict": C_INVALID, "gaps": [],
                    "active_gaps": [], "valid_types": [], "critical_errors": sorted(set(ce)),
                    "critical_errors_global": sorted(set(ce)),
                    "critical_errors_by_file": {"_sample": ce}, "warnings": [],
                    "why_not_ready": sorted(set(ce)), "invalid_recognized_files": 0,
                    "valid_recognized_files": 0, "file_results": file_results,
                    "type_summary": {}, "unsafe_file_count": len(rep_errors),
                    "unsafe_blocked_files": rep_errors, "unknown_suspicious_files": suspicious_files,
                    "blocked_file_count": len(rep_errors) + len(suspicious_files),
                    "symbol_alignment_ok": False, "density_ok": False,
                    "common_symbols_required": [], "symbols_by_type": {},
                    "orderbook_l1_ready": False, "normalization_allowed": False,
                    "normalization_blockers": sorted(set(ce)),
                    "can_research_microstructure": False,
                    "future_research_ready_if_sample_passes": False,
                    "future_labs_ready": _future_research_ready({}), "funding_optional_reason": None,
                    "max_coverage_days": 0, "min_required_coverage_days": 0}
        return {"verdict": C_NO_SAMPLE, "readiness_verdict": C_NO_SAMPLE, "gaps": [],
                "active_gaps": [], "valid_types": [], "critical_errors": [],
                "critical_errors_global": [], "critical_errors_by_file": {}, "warnings": [],
                "invalid_recognized_files": 0, "valid_recognized_files": 0,
                "file_results": file_results, "type_summary": {}, "unsafe_file_count": 0,
                "unsafe_blocked_files": [], "unknown_suspicious_files": [], "blocked_file_count": 0,
                "symbol_alignment_ok": False, "density_ok": False, "common_symbols_required": [],
                "symbols_by_type": {}, "orderbook_l1_ready": False,
                "normalization_allowed": False, "normalization_blockers": ["no recognized files"],
                "can_research_microstructure": False,
                "future_research_ready_if_sample_passes": False,
                "future_labs_ready": _future_research_ready({}), "funding_optional_reason": None,
                "max_coverage_days": 0, "min_required_coverage_days": 0,
                "why_not_ready": ["no recognized sample files"]}

    invalid_files = [fr for fr in recognized if not fr.get("valid")]
    valid_files = [fr for fr in recognized if fr.get("valid")]
    critical_errors: list[str] = []
    critical_by_file: dict[str, list[str]] = {}
    warnings: list[str] = []
    type_summary: dict[str, dict] = {}
    for fr in recognized:
        dtype = fr["detected_type"]
        tsum = type_summary.setdefault(dtype, {"valid_files": 0, "invalid_files": 0, "files": []})
        tsum["files"].append(fr["file"])
        tsum["valid_files" if fr.get("valid") else "invalid_files"] += 1
        errs = list(fr.get("critical_errors") or [])
        if not fr.get("valid") and not errs:
            errs = [fr.get("reason_if_invalid") or "invalid_recognized_file"]
        if errs:
            critical_by_file[fr["file"]] = errs
        for e in errs:
            critical_errors.append(f"{dtype}:{e}")
            critical_errors.append(f"{dtype}:{fr['file']}:{e}")
        for w in (fr.get("warnings") or []):
            warnings.append(f"{dtype}:{w}")
    # rep.errors (unsafe files) + suspicious files -> hard blockers
    for e in rep_errors:
        critical_errors.append(e)
        critical_by_file.setdefault("_sample", []).append(e)
    for s in suspicious_files:
        critical_errors.append(f"unknown_suspicious_file:{s}")
        critical_by_file.setdefault(s, []).append("unknown_suspicious_file")

    present = {t: v for t, v in by_type.items() if v.get("valid")}
    valid_types = set(present)
    tr = present.get("trades", {})
    ob = present.get("orderbook", {})
    ob_l1_ready = bool(ob.get("valid")) and ob.get("l1_imbalance_median") is not None

    covs = [v.get("coverage", {}).get("coverage_days", 0) or 0 for v in present.values()]
    max_cov = max(covs) if covs else 0
    required_covs = [present[t]["coverage"]["coverage_days"] or 0
                     for t in required_types if present.get(t, {}).get("valid")]
    min_required_cov = min(required_covs) if required_covs else 0

    # ---- symbol presence + alignment across required present types ----
    symbols_by_type = {t: sorted(set(present[t].get("symbols") or [])) for t in present}
    required_present = [t for t in required_types if t in valid_types]
    missing_symbol_types = [t for t in required_present if not symbols_by_type.get(t)]
    if required_present and not missing_symbol_types:
        common = set(symbols_by_type[required_present[0]])
        for t in required_present[1:]:
            common &= set(symbols_by_type[t])
    else:
        common = set()
    common_symbols_required = sorted(common)
    all_required_present = all(t in valid_types for t in required_types)
    symbol_alignment_ok = (all_required_present and not missing_symbol_types
                           and bool(common_symbols_required))

    # ---- density per required present type ----
    density_ok_by_type: dict[str, bool] = {}
    for t in required_types:
        m = present.get(t)
        if not (m and m.get("valid")):
            density_ok_by_type[t] = False
            continue
        cov = m.get("coverage", {})
        rows = int(cov.get("rows") or 0)
        cdays = cov.get("coverage_days", 0) or 0
        rpd = (rows / cdays) if cdays > 0 else float(rows)
        density_ok_by_type[t] = rows >= _MIN_ROWS[t] and rpd >= _MIN_ROWS_PER_DAY[t]
    density_ok = all_required_present and all(density_ok_by_type[t] for t in required_types)
    sparse_present = [t for t in required_types
                      if present.get(t, {}).get("valid") and not density_ok_by_type[t]]

    gaps: list[str] = []
    if not tr.get("valid") or not tr.get("has_aggressor_side"):
        gaps.append(C_NEEDS_AGGRESSOR)
    if "orderbook" not in valid_types or not ob_l1_ready:
        gaps.append(C_NEEDS_ORDERBOOK)
    if "oi" not in valid_types:
        gaps.append(C_NEEDS_OI)
    if "liquidations" not in valid_types:
        gaps.append(C_NEEDS_LIQ)
    if (max_cov < MIN_HISTORY_DAYS
            or any((present[t]["coverage"].get("coverage_days", 0) or 0) < MIN_HISTORY_DAYS
                   for t in required_types if present.get(t, {}).get("valid"))
            or sparse_present
            or any(v.get("coverage", {}).get("gap_count") for v in present.values())):
        gaps.append(C_NEEDS_HISTORY)

    symbol_why: list[str] = []
    for t in missing_symbol_types:
        symbol_why.append(f"MISSING_SYMBOL_{t.upper()}")
    if all_required_present and not missing_symbol_types and not common_symbols_required:
        symbol_why.append("SYMBOL_ALIGNMENT_FAIL")

    funding_optional_reason = None
    if "funding" not in {fr["detected_type"] for fr in recognized} and "oi" in valid_types:
        funding_optional_reason = "funding is optional for V10.24 readiness when OI is present and valid"

    active_gaps = sorted(set(gaps))
    critical_errors = sorted(set(critical_errors))
    unsafe_file_count = len(rep_errors)

    ready = (
        unsafe_file_count == 0
        and not suspicious_files
        and len(invalid_files) == 0
        and not critical_errors
        and tr.get("valid") and tr.get("has_aggressor_side")
        and "orderbook" in valid_types and ob_l1_ready
        and "oi" in valid_types
        and "liquidations" in valid_types
        and min_required_cov >= MIN_HISTORY_DAYS
        and symbol_alignment_ok
        and density_ok
        and not active_gaps
    )
    if ready:
        verdict = C_READY
    elif critical_errors or invalid_files:
        verdict = C_INVALID
    elif C_NEEDS_HISTORY in active_gaps:
        verdict = C_NEEDS_HISTORY
    elif not tr.get("valid") or not tr.get("has_aggressor_side"):
        verdict = C_NEEDS_AGGRESSOR
    elif "orderbook" not in valid_types or not ob_l1_ready:
        verdict = C_NEEDS_ORDERBOOK
    elif "liquidations" not in valid_types:
        verdict = C_NEEDS_LIQ
    elif "oi" not in valid_types:
        verdict = C_NEEDS_OI
    elif not symbol_alignment_ok:
        verdict = C_PARTIAL
    else:
        verdict = C_PARTIAL

    why_not_ready = sorted(set(list(critical_errors) + list(active_gaps) + symbol_why
                               + (["DENSITY_FAIL"] if not density_ok else [])
                               + (["SYMBOL_ALIGNMENT_FAIL"] if not symbol_alignment_ok and all_required_present else [])))
    normalization_blockers = []
    if verdict != C_READY:
        normalization_blockers.append("verdict_not_ready")
    if rep_errors:
        normalization_blockers.append("rep_errors")
    if critical_errors:
        normalization_blockers.append("critical_errors")
    if active_gaps:
        normalization_blockers.append("active_gaps")
    if invalid_files:
        normalization_blockers.append("invalid_recognized_files")
    if suspicious_files:
        normalization_blockers.append("unknown_suspicious_files")
    if not symbol_alignment_ok:
        normalization_blockers.append("symbol_alignment_fail")
    if not density_ok:
        normalization_blockers.append("density_fail")
    normalization_blockers = sorted(set(normalization_blockers))
    normalization_allowed = bool(ready) and not normalization_blockers
    return {"verdict": verdict, "readiness_verdict": verdict,
            "gaps": active_gaps, "active_gaps": active_gaps,
            "critical_errors": critical_errors, "critical_errors_global": critical_errors,
            "critical_errors_by_file": critical_by_file,
            "warnings": sorted(set(warnings)), "why_not_ready": why_not_ready,
            "valid_types": sorted(valid_types),
            "invalid_recognized_files": len(invalid_files),
            "valid_recognized_files": len(valid_files),
            "file_results": file_results, "type_summary": type_summary,
            "unsafe_file_count": unsafe_file_count, "unsafe_blocked_files": rep_errors,
            "unknown_suspicious_files": suspicious_files,
            "blocked_file_count": unsafe_file_count + len(suspicious_files),
            "symbols_by_type": symbols_by_type, "common_symbols_required": common_symbols_required,
            "symbol_alignment_ok": symbol_alignment_ok, "density_ok": density_ok,
            "density_ok_by_type": density_ok_by_type,
            "max_coverage_days": max_cov, "min_required_coverage_days": min_required_cov,
            "orderbook_l1_ready": ob_l1_ready,
            "normalization_allowed": normalization_allowed,
            "normalization_blockers": normalization_blockers,
            "can_research_microstructure": bool(ready),
            "future_research_ready_if_sample_passes": bool(ready),
            "funding_optional_reason": funding_optional_reason,
            "future_labs_ready": _future_research_ready(present, density_ok_by_type)}


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
    by_type: dict[str, dict] = {}          # best-per-type metrics (valid preferred)
    file_results: list[dict] = []
    suspicious_files: list[str] = []
    recognized_count = 0
    for name in sorted(os.listdir(sample_dir)):
        path = os.path.join(sample_dir, name)
        if not os.path.isfile(path) or not name.lower().endswith((".csv", ".tsv")):
            continue
        try:
            assert_safe_sample_file(sample_dir, path)
        except ValueError as e:
            rep["errors"].append(f"unsafe_sample_file:{name}:{e}")
            fr = {"file": name, "detected_type": "unknown", "rows": 0, "truncated": False,
                  "columns": [], "valid": False, "critical_errors": ["unsafe_sample_file"],
                  "warnings": [], "coverage": {}, "reason_if_invalid": f"unsafe_sample_file:{e}"}
            file_results.append(fr)
            rep["files"].append(fr)
            continue
        header, rows, truncated, parse_error = _read_csv(path)
        dtype = detect_type(name, header)
        suspicious = _is_suspicious_file(name, header)
        try:
            hardlinked = os.stat(path).st_nlink > 1
        except OSError:
            hardlinked = False
        fr = {"file": name, "detected_type": dtype, "rows": len(rows), "truncated": truncated,
              "columns": header, "valid": False, "critical_errors": [], "warnings": [],
              "coverage": {}, "reason_if_invalid": None, "schema": header,
              "suspicious": suspicious, "hardlinked": hardlinked, "parse_error": parse_error}
        if suspicious or (hardlinked and (suspicious or dtype == "unknown")):
            # credential/secret-like file (name or headers), or suspicious hardlink
            suspicious_files.append(name)
            fr["critical_errors"] = ["unknown_suspicious_file"]
            fr["reason_if_invalid"] = "suspicious_secret_like_file"
            file_results.append(fr)
            rep["files"].append(fr)
            continue
        if dtype in _VALIDATORS:
            recognized_count += 1
            if parse_error:
                fr["critical_errors"] = ["csv_parse_error"]
                fr["reason_if_invalid"] = "csv_parse_error"
            elif not rows:
                fr["critical_errors"] = ["empty_recognized_csv"]
                fr["reason_if_invalid"] = "empty_recognized_csv"
            else:
                res = _VALIDATORS[dtype](rows)
                fr["valid"] = bool(res["valid"])
                fr["critical_errors"] = list(res.get("critical_errors") or [])
                fr["warnings"] = list(res.get("warnings") or [])
                fr["coverage"] = res.get("coverage", {})
                fr["metrics"] = res
                if not res["valid"]:
                    fr["reason_if_invalid"] = ";".join(fr["critical_errors"]) or "invalid"
                if dtype not in by_type or (res["valid"] and not by_type[dtype].get("valid")):
                    by_type[dtype] = res
        else:
            # benign unknown .csv (not suspicious) -> recorded, never blocks readiness
            fr["warnings"] = ["benign_unknown_file_ignored"]
        file_results.append(fr)
        rep["files"].append(fr)
    rep["by_type"] = {k: v for k, v in by_type.items()}
    rep["classification"] = classify_sample(file_results, by_type, recognized_count,
                                            rep_errors=list(rep["errors"]),
                                            suspicious_files=suspicious_files)
    rep["file_results"] = file_results
    rep["type_summary"] = rep["classification"].get("type_summary", {})
    rep["critical_errors"] = rep["classification"].get("critical_errors", [])
    rep["warnings"] = rep["classification"].get("warnings", [])
    # NORMALIZATION GATE: only when the classifier explicitly allows it.
    if apply_normalization and rep["classification"].get("normalization_allowed"):
        rep["normalization"] = _write_normalized(sample_dir, rep)
    else:
        blockers = rep["classification"].get("normalization_blockers", ["flag_off"])
        if not apply_normalization:
            blockers = ["flag_off"]
        rep["normalization"] = {"applied": False, "normalization_allowed": False,
                                "normalization_blockers": blockers,
                                "reason": "normalization blocked: " + ",".join(blockers)}
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
        header, rows, _truncated, _parse_error = _read_csv(src_path)
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
    marker_root = _resolved(DEFAULT_SAMPLE_DIR, relative_to_repo=True)
    contained = _is_relative_to(_resolved(out_dir, relative_to_repo=True), marker_root)
    return {"applied": True, "normalization_allowed": True, "out_dir": out_dir,
            "files": written, "run_id": run_id,
            "wrote_only_staging_marker": bool(contained and STAGING_MARKER in out_dir)}


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
