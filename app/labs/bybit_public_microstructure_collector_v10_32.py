"""ResearchOps V10.32 - Bybit Full Microstructure Sample Collector (research only).

WHY: the Binance-native readiness path is permanently blocked on liquidations
from this network (fstream delivers zero frames -- live-probed). Bybit works
END TO END here: public REST (trades/tickers/OI/funding, live-probed retCode=0)
and the public liquidations ws (V10.30). This module builds ONE growing
bybit_linear dataset with ALL FIVE canonical kinds so the V10.24.3 validator
can judge a SINGLE-EXCHANGE Bybit sample on its own merits.

METHODOLOGY (option C, no cross-exchange mixing): everything in this dataset is
bybit_linear. Liquidations are SYNCED from the V10.30 dataset (same exchange --
this is consolidation, not cross-exchange readiness). The Binance dataset and
its readiness are untouched. READY here means "enough clean Bybit DATA to start
microstructure research"; it is NEVER an edge and NEVER unlocks live/paper.

Side conventions: V10.30 stores the Bybit POSITION side (Buy=long liquidated).
The canonical liquidation schema uses the ORDER side (Binance convention:
sell=long liquidated). The sync maps position long->sell, short->buy and keeps
the raw fields in the V10.30 dataset untouched.

Public GET only, exact allowlist, NO keys, NO private endpoints, dry-run by
default, staging-only writes, bounded cycles. FINAL_RECOMMENDATION: NO LIVE.
"""

from __future__ import annotations

import csv
import json
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import FINAL_RECOMMENDATION_NO_LIVE
from . import microstructure_sample_adapter_v10_24 as V24
from . import free_public_liquidations_ws_collector_v10_26 as V26
from . import bybit_public_liquidations_ws_collector_v10_30 as V30

TOOL_VERSION = "v10.32"
STAGING_MARKER = "bybit_microstructure_v10_32"
DEFAULT_STAGING_DIR = f"external_data/staging/{STAGING_MARKER}"
DATASET_SUBDIR = "dataset"
SOURCE_EXCHANGE = "bybit_linear"
_HOST = "api.bybit.com"
_SYM_RE = re.compile(r"^[A-Z0-9]{2,20}$")
_SEEN_CAP = 250_000

_FORBIDDEN_SEG = V26._FORBIDDEN_SEG
_FORBIDDEN_SUF = V26._FORBIDDEN_SUF
_SENSITIVE_QUERY = ("api_key", "apikey", "sign", "timestamp", "recv_window",
                    "recvwindow", "secret", "token", "signature")

KINDS = ("trades", "orderbook", "oi", "funding", "liquidations")
_FILES = {"trades": "trades.csv", "orderbook": "orderbook_l2.csv",
          "oi": "open_interest.csv", "funding": "funding.csv",
          "liquidations": "liquidations.csv"}
_HEADERS = {
    "trades": ["timestamp", "symbol", "price", "size", "aggressor_side", "trade_id",
               "source_exchange"],
    "orderbook": ["timestamp", "symbol", "bid_price_1", "bid_size_1",
                  "ask_price_1", "ask_size_1", "depth_level", "source_exchange"],
    "oi": ["timestamp", "symbol", "open_interest", "source_exchange"],
    "funding": ["timestamp", "symbol", "funding_rate", "source_exchange"],
    "liquidations": list(V26.CANON_HEADER),
}


def _safety() -> dict[str, Any]:
    return {"research_only": True, "shadow_only": True, "paper_ready": False,
            "live_ready": False, "can_send_real_orders": False,
            "paper_filter_enabled": False, "makes_no_trades": True,
            "uses_api_keys": False, "uses_db": False,
            "subscribes_private_channels": False,
            "source_exchange": SOURCE_EXCHANGE, "single_exchange_sample": True,
            "cross_exchange_liquidations_used_for_ready": False,
            "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------
# Exact public-GET allowlist + hardened staging (established patterns)
# --------------------------------------------------------------------------

def _planned_urls(symbol: str) -> dict[str, str]:
    if not _SYM_RE.match(symbol):
        raise ValueError(f"symbol not allowlisted: {symbol}")
    s = urllib.parse.quote(symbol)
    return {
        "trades": f"https://{_HOST}/v5/market/recent-trade?category=linear&symbol={s}&limit=1000",
        "orderbook": f"https://{_HOST}/v5/market/tickers?category=linear&symbol={s}",
        "oi": f"https://{_HOST}/v5/market/open-interest?category=linear&symbol={s}&intervalTime=5min&limit=200",
        "funding": f"https://{_HOST}/v5/market/funding/history?category=linear&symbol={s}&limit=200",
    }


_ALLOWED_PATHS = ("/v5/market/recent-trade", "/v5/market/tickers",
                  "/v5/market/open-interest", "/v5/market/funding/history")


def assert_safe_request(url: str, headers: dict | None = None) -> bool:
    p = urllib.parse.urlparse(url)
    if p.scheme != "https" or p.netloc != _HOST:
        raise ValueError(f"host not allowlisted: {url}")
    if p.path not in _ALLOWED_PATHS:
        raise ValueError(f"path not allowlisted: {p.path}")
    q = urllib.parse.parse_qs(p.query)
    for k in q:
        if k.lower() in _SENSITIVE_QUERY:
            raise ValueError(f"sensitive query param blocked: {k}")
    for k in (headers or {}):
        lk = str(k).lower()
        if lk in ("authorization", "x-bapi-api-key", "x-bapi-sign", "cookie") \
                or "key" in lk or "sign" in lk or "auth" in lk:
            raise ValueError(f"auth-like header blocked: {k}")
    return True


def default_transport(url: str, headers: dict[str, str]) -> bytes:
    assert_safe_request(url, headers)
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=12) as resp:
        return resp.read()


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def safe_staging_dir(base: str | None = None) -> str:
    root = base or DEFAULT_STAGING_DIR
    segs = [s for s in str(root).replace("\\", "/").split("/") if s]
    if ".." in segs:
        raise ValueError("staging traversal blocked")
    for s in (x.lower() for x in segs):
        if s in _FORBIDDEN_SEG or s.endswith(_FORBIDDEN_SUF) or ".env" in s:
            raise ValueError(f"forbidden staging segment: {s}")
    repo = _repo_root()
    logical_root = repo / "external_data" / "staging" / STAGING_MARKER
    for comp in (repo / "external_data", repo / "external_data" / "staging", logical_root):
        try:
            if comp.exists() and comp.is_symlink():
                raise ValueError(f"symlinked staging root component blocked: {comp}")
        except OSError:
            break
    target = Path(root)
    if not target.is_absolute():
        target = repo / target
    target = Path(os.path.normpath(str(target)))
    if target != logical_root and logical_root not in target.parents:
        raise ValueError(f"staging dir must be inside external_data/staging/{STAGING_MARKER}")
    for anc in [target, *target.parents]:
        if anc == repo or anc in repo.parents:
            break
        try:
            if anc.exists() and anc.is_symlink():
                raise ValueError(f"symlinked staging component blocked: {anc}")
        except OSError:
            break
    rtgt = target.resolve(strict=False)
    if not (rtgt == repo or _is_within(rtgt, repo)):
        raise ValueError("staging dir resolves outside the repo")
    return root


# --------------------------------------------------------------------------
# Converters (Bybit v5 payloads -> V10.24-canonical rows)
# --------------------------------------------------------------------------

def trades_to_canonical(payload: Any, symbol: str) -> list[dict]:
    out = []
    for r in ((payload or {}).get("result") or {}).get("list") or []:
        try:
            ts, p, sz = r.get("time"), r.get("price"), r.get("size")
            side = str(r.get("side") or "").lower()          # taker (aggressor) side
            if ts is None or p is None or sz is None or side not in ("buy", "sell"):
                continue
            if float(p) <= 0 or float(sz) <= 0:
                continue
            out.append({"timestamp": int(float(ts)), "symbol": symbol, "price": p,
                        "size": sz, "aggressor_side": side,
                        "trade_id": str(r.get("execId") or ""),
                        "source_exchange": SOURCE_EXCHANGE})
        except (TypeError, ValueError):
            continue
    return out


def ticker_to_canonical(payload: Any, symbol: str) -> list[dict]:
    res = (payload or {}).get("result") or {}
    ts = (payload or {}).get("time")
    out = []
    for r in res.get("list") or []:
        try:
            bp, bs = r.get("bid1Price"), r.get("bid1Size")
            ap, asz = r.get("ask1Price"), r.get("ask1Size")
            if None in (ts, bp, bs, ap, asz):
                continue
            out.append({"timestamp": int(float(ts)), "symbol": symbol,
                        "bid_price_1": bp, "bid_size_1": bs,
                        "ask_price_1": ap, "ask_size_1": asz,
                        "depth_level": "L1_TICKER",
                        "source_exchange": SOURCE_EXCHANGE})
        except (TypeError, ValueError):
            continue
    return out


def oi_to_canonical(payload: Any, symbol: str) -> list[dict]:
    out = []
    for r in ((payload or {}).get("result") or {}).get("list") or []:
        try:
            ts, oi = r.get("timestamp"), r.get("openInterest")
            if ts is None or oi is None:
                continue
            out.append({"timestamp": int(float(ts)), "symbol": symbol,
                        "open_interest": oi, "source_exchange": SOURCE_EXCHANGE})
        except (TypeError, ValueError):
            continue
    return out


def funding_to_canonical(payload: Any, symbol: str) -> list[dict]:
    out = []
    for r in ((payload or {}).get("result") or {}).get("list") or []:
        try:
            if str(r.get("symbol") or "") != symbol:
                continue
            ts, fr = r.get("fundingRateTimestamp"), r.get("fundingRate")
            if ts is None or fr is None:
                continue
            out.append({"timestamp": int(float(ts)), "symbol": symbol,
                        "funding_rate": fr, "source_exchange": SOURCE_EXCHANGE})
        except (TypeError, ValueError):
            continue
    return out


def v30_liq_to_canonical(row: dict) -> dict | None:
    """V10.30 row (POSITION side) -> canonical liquidation row (ORDER side).
    Same exchange consolidation; raw V10.30 data stays untouched at source."""
    pos = str(row.get("position_liquidated") or "").lower()
    if pos == "long":
        side = "sell"          # closing a long is a sell order (Binance convention)
    elif pos == "short":
        side = "buy"
    else:
        return None
    try:
        return {"timestamp": int(float(row["timestamp"])), "exchange": SOURCE_EXCHANGE,
                "symbol": row["symbol"], "side": side, "price": row["price"],
                "size": row["size"], "notional": row.get("notional", ""),
                "source": "bybit_v10_30_sync(order-side convention)",
                "event_type": "liquidation", "raw_event_id": row["raw_event_id"],
                "received_at": row.get("received_at", "")}
    except (KeyError, TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# Dataset helpers (dedup persisted across cycles; no empty files ever)
# --------------------------------------------------------------------------

def _dedup_key(kind: str, row: dict) -> str:
    if kind == "trades":
        return str(row.get("trade_id") or f"{row.get('timestamp')}:{row.get('price')}:{row.get('size')}:{row.get('aggressor_side')}")
    if kind == "liquidations":
        return str(row.get("raw_event_id"))
    return f"{row.get('symbol')}:{row.get('timestamp')}"


def _load_seen(dataset_dir: str, kind: str) -> set[str]:
    try:
        return set(json.loads(Path(dataset_dir, f"_seen_{kind}.json").read_text(encoding="utf-8")))
    except Exception:
        return set()


def _save_seen(dataset_dir: str, kind: str, seen: set[str]) -> None:
    Path(dataset_dir, f"_seen_{kind}.json").write_text(
        json.dumps(list(seen)[-_SEEN_CAP:]), encoding="utf-8")


def _migrate_add_source_exchange(dataset_dir: str) -> list[str]:
    """V10.36: legacy CSVs written before the source_exchange column existed
    are migrated IN PLACE (atomic rewrite, column stamped bybit_linear). Loud,
    one-shot, never hides: returns the list of migrated files."""
    migrated = []
    for kind in ("trades", "orderbook", "oi", "funding"):
        path = Path(dataset_dir) / _FILES[kind]
        if not path.is_file() or path.is_symlink():
            continue
        with open(path, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            fields = list(reader.fieldnames or [])
            if "source_exchange" in fields:
                continue
            rows = list(reader)
        tmp = Path(dataset_dir) / (_FILES[kind] + ".tmp")
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=_HEADERS[kind])
            w.writeheader()
            for r in rows:
                r["source_exchange"] = SOURCE_EXCHANGE
                w.writerow({c: r.get(c, "") for c in _HEADERS[kind]})
        os.replace(tmp, path)
        migrated.append(_FILES[kind])
    return migrated


def check_source_consistency(dataset_dir: str) -> dict[str, Any]:
    """Every row in every CSV must be bybit_linear. Any mismatch or missing
    stamp is reported; a mismatch INVALIDATES the sample (fail-closed)."""
    out: dict[str, Any] = {"source_consistency_ok": True, "mismatched": {},
                           "missing_stamp": {}}
    for kind in KINDS:
        path = Path(dataset_dir) / _FILES[kind]
        if not path.is_file() or path.is_symlink():
            continue
        col = "exchange" if kind == "liquidations" else "source_exchange"
        bad = missing = 0
        with open(path, "r", newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                v = str(r.get(col) or "")
                if not v:
                    missing += 1
                elif v != SOURCE_EXCHANGE:
                    bad += 1
        if bad:
            out["mismatched"][kind] = bad
            out["source_consistency_ok"] = False
        if missing:
            out["missing_stamp"][kind] = missing
            out["source_consistency_ok"] = False
    return out


def _append_rows(dataset_dir: str, kind: str, rows: list[dict], seen: set[str]) -> int:
    new_rows = []
    for r in rows:
        k = _dedup_key(kind, r)
        if k in seen:
            continue
        seen.add(k)
        new_rows.append(r)
    if not new_rows:
        return 0
    # Bybit v5 lists arrive NEWEST-FIRST: always append in ascending timestamp
    # order so the growing CSV stays monotonic for the V10.24 validator
    new_rows.sort(key=lambda r: int(r.get("timestamp", 0)))
    header = _HEADERS[kind]
    path = os.path.join(dataset_dir, _FILES[kind]).replace("\\", "/")
    new_file = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header)
        if new_file:
            w.writeheader()
        for r in new_rows:
            w.writerow({c: r.get(c, "") for c in header})
    return len(new_rows)


# --------------------------------------------------------------------------
# Plan / run_cycle / status
# --------------------------------------------------------------------------

def plan() -> dict[str, Any]:
    return {"tool_version": TOOL_VERSION,
            "objective": ("build ONE growing bybit_linear dataset with all five "
                          "canonical kinds so V10.24.3 can judge a SINGLE-EXCHANGE "
                          "Bybit sample -- the unblocked path to research-readiness"),
            "why": ("Binance-native liquidations are unreachable from this network; "
                    "Bybit REST + ws are live-verified working end to end"),
            "kinds": list(KINDS),
            "rest_endpoints": _planned_urls("BTCUSDT"),
            "liquidations_source": ("synced from the V10.30 bybit dataset (SAME "
                                    "exchange; position side mapped to order side)"),
            "dataset_dir": f"{DEFAULT_STAGING_DIR}/{DATASET_SUBDIR}",
            "default_mode": "DRY_RUN (no network, no writes)",
            "honesty": ("READY means enough clean Bybit DATA for research; it is "
                        "NOT an edge and never unlocks live/paper"),
            "never": ["api_keys", "private_endpoints", "orders", "db_write",
                      "raw_write", "cross_exchange_mixing", "invented_READY"],
            "writes_on_plan": False, **_safety()}


def run_cycle(symbol: str = "BTCUSDT", apply: bool = False,
              output_dir: str | None = None, transport: Callable | None = None,
              orderbook_polls: int = 3, poll_spacing_seconds: float = 1.0,
              liq_source_dir: str | None = None) -> dict[str, Any]:
    symbol = str(symbol or "").strip().upper()
    rep: dict[str, Any] = {"tool_version": TOOL_VERSION, "symbol": symbol,
                           "apply": bool(apply), "cycle_time": _now_iso(),
                           "added": {}, "errors": [], **_safety()}
    if not _SYM_RE.match(symbol):
        rep["mode"] = "APPLY" if apply else "DRY_RUN"
        rep["writes"] = False
        rep["errors"].append(f"symbol_not_allowlisted:{symbol!r}")
        return rep
    if not apply:
        rep["mode"] = "DRY_RUN"
        rep["writes"] = False
        rep["planned_urls"] = _planned_urls(symbol)
        return rep
    rep["mode"] = "APPLY"
    try:
        staging = safe_staging_dir(output_dir)
    except ValueError as e:
        rep["errors"].append(f"unsafe_output_dir:{e}")
        rep["writes"] = False
        return rep
    dataset_dir = os.path.join(staging, DATASET_SUBDIR).replace("\\", "/")
    os.makedirs(dataset_dir, exist_ok=True)
    rep["dataset_dir"] = dataset_dir
    migrated = _migrate_add_source_exchange(dataset_dir)
    if migrated:
        rep["migrated_source_exchange_column"] = migrated
    tr = transport or default_transport
    hdr = {"User-Agent": "researchops/1.0", "Accept": "application/json"}
    urls = _planned_urls(symbol)
    converters = {"trades": trades_to_canonical, "orderbook": ticker_to_canonical,
                  "oi": oi_to_canonical, "funding": funding_to_canonical}

    rep["rate_limit_events"] = []
    for kind in ("trades", "orderbook", "oi", "funding"):
        seen = _load_seen(dataset_dir, kind)
        total = 0
        repeats = max(1, int(orderbook_polls)) if kind == "orderbook" else 1
        backoff_attempt = 0
        for poll in range(repeats):
            while True:                              # bounded retry (max 2 backoffs)
                try:
                    payload = json.loads(tr(urls[kind], hdr))
                    if str(payload.get("retCode")) not in ("0", "None"):
                        rep["errors"].append(f"{kind}:BYBIT_RETCODE:{payload.get('retCode')}:"
                                             f"{str(payload.get('retMsg'))[:40]}")
                    else:
                        total += _append_rows(dataset_dir, kind,
                                              converters[kind](payload, symbol), seen)
                    break
                except Exception as e:
                    msg = str(e)
                    if "429" in msg or "rate" in msg.lower():
                        # V10.36 backoff: exponential with conservative cap +
                        # deterministic mini-jitter; LOUD in report + manifest;
                        # never more than 2 retries per kind (no hammering)
                        backoff_attempt += 1
                        backoff_s = min(30.0, 5.0 * (2 ** (backoff_attempt - 1))
                                        + (backoff_attempt % 2))
                        rep["rate_limit_events"].append(
                            {"endpoint": kind, "status": "RATE_LIMITED",
                             "attempt": backoff_attempt,
                             "backoff_seconds": backoff_s, "detail": msg[:50]})
                        rep["errors"].append(f"{kind}:RATE_LIMITED:attempt{backoff_attempt}:"
                                             f"backoff{backoff_s:.0f}s")
                        if backoff_attempt > 2:
                            break                    # give up this kind this cycle
                        time.sleep(backoff_s)
                        continue                     # one more try after backoff
                    rep["errors"].append(f"{kind}:{type(e).__name__}:{msg[:60]}")
                    break
            if backoff_attempt > 2:
                break
            if repeats > 1 and poll < repeats - 1 and poll_spacing_seconds > 0:
                time.sleep(float(poll_spacing_seconds))
        _save_seen(dataset_dir, kind, seen)
        rep["added"][kind] = total

    # liquidations: same-exchange sync from the V10.30 dataset
    rep["added"]["liquidations"] = 0
    try:
        src = Path(liq_source_dir) if liq_source_dir else (
            _repo_root() / "external_data" / "staging" / V30.STAGING_MARKER
            / V30.DATASET_SUBDIR)
        src_csv = src / "liquidations.csv"
        if src_csv.is_file() and not src_csv.is_symlink():
            seen = _load_seen(dataset_dir, "liquidations")
            rows = []
            with open(src_csv, "r", newline="", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    if str(r.get("symbol") or "").upper() != symbol:
                        continue
                    canon = v30_liq_to_canonical(r)
                    if canon is not None:
                        rows.append(canon)
            rep["added"]["liquidations"] = _append_rows(dataset_dir, "liquidations", rows, seen)
            _save_seen(dataset_dir, "liquidations", seen)
    except Exception as e:
        rep["errors"].append(f"liquidations_sync:{type(e).__name__}:{str(e)[:60]}")

    rep["writes"] = any(v > 0 for v in rep["added"].values())
    # cumulative manifest + atomic checkpoint (state saved every cycle)
    man_path = Path(dataset_dir) / "manifest.json"
    try:
        prev = json.loads(man_path.read_text(encoding="utf-8"))
    except Exception:
        prev = {}
    cumulative = dict(prev.get("cumulative_added", {}))
    for k, v in rep["added"].items():
        cumulative[k] = int(cumulative.get(k, 0)) + int(v)
    manifest = {"tool_version": TOOL_VERSION, "source_exchange": SOURCE_EXCHANGE,
                "first_cycle": prev.get("first_cycle", rep["cycle_time"]),
                "last_cycle": rep["cycle_time"], "cycles": int(prev.get("cycles", 0)) + 1,
                "symbol": symbol, "this_cycle_added": rep["added"],
                "cumulative_added": cumulative, "errors_last_cycle": rep["errors"],
                "rate_limit_events_last_cycle": rep.get("rate_limit_events", []),
                "single_exchange_sample": True,
                "note": "bybit_linear full sample; validate with bybit-microstructure-status-v1032",
                "research_only": True, "shadow_only": True, "live_ready": False,
                "can_send_real_orders": False,
                "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}
    man_path.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    ck_tmp = Path(dataset_dir) / "checkpoint.json.tmp"
    ck_tmp.write_text(json.dumps({
        "last_cycle": rep["cycle_time"], "cycles": manifest["cycles"],
        "symbol": symbol, "exchange": SOURCE_EXCHANGE,
        "rows_by_type": cumulative, "errors_last_cycle": rep["errors"],
        "research_only": True, "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE},
        indent=2, default=str), encoding="utf-8")
    os.replace(ck_tmp, Path(dataset_dir) / "checkpoint.json")
    rep["manifest"] = str(man_path).replace("\\", "/")
    rep["cumulative_added"] = cumulative
    return rep


def status(output_dir: str | None = None) -> dict[str, Any]:
    rep: dict[str, Any] = {"tool_version": TOOL_VERSION, "checked_at": _now_iso(), **_safety()}
    try:
        staging = safe_staging_dir(output_dir)
    except ValueError as e:
        rep["error"] = f"unsafe_output_dir:{e}"
        return rep
    dataset_dir = os.path.join(staging, DATASET_SUBDIR).replace("\\", "/")
    rep["dataset_dir"] = dataset_dir
    try:
        man = json.loads(Path(dataset_dir, "manifest.json").read_text(encoding="utf-8"))
        rep["cycles"] = man.get("cycles")
        rep["last_cycle"] = man.get("last_cycle")
        rep["cumulative_added"] = man.get("cumulative_added", {})
        rep["errors_last_cycle"] = man.get("errors_last_cycle", [])
    except Exception:
        pass
    if not os.path.isdir(dataset_dir):
        rep["readiness_verdict"] = V24.C_NO_SAMPLE
        rep["note"] = "no bybit dataset yet -- run bybit-microstructure-run-cycle-v1032 --apply"
        return rep
    consistency = check_source_consistency(dataset_dir)
    rep["source_consistency"] = consistency
    vr = V24.validate_sample(dataset_dir)
    cls = vr.get("classification", {})
    rep["readiness_verdict"] = cls.get("verdict")
    if not consistency["source_consistency_ok"]:
        # fail-closed: mixed or unstamped sources can never be READY
        rep["readiness_verdict"] = V24.C_INVALID
        rep["why_not_ready"] = ["SOURCE_MISMATCH_OR_MISSING_STAMP"]
        rep["can_research_microstructure"] = False
    rep["active_gaps"] = cls.get("active_gaps")
    rep["valid_types"] = cls.get("valid_types")
    rep["density_ok"] = cls.get("density_ok")
    rep["why_not_ready"] = cls.get("why_not_ready")
    rep["can_research_microstructure"] = cls.get("can_research_microstructure")
    rep["honesty"] = ("this is BYBIT data readiness; it never touches the Binance "
                      "readiness and READY is data-readiness, not an edge")
    return rep
