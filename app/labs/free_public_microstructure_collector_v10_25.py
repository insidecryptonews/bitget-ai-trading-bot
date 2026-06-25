"""ResearchOps V10.25 - Free Public Microstructure Collector (research only).

Find and convert FREE public microstructure-ish data into the V10.24.3 canonical
format so it can be validated by `microstructure-sample-validate-v1024`.

Honest scope (every verdict below was probed live, not invented):
- Binance USD-M futures public data is the strongest FREE source:
  * data.binance.vision aggTrades dumps  -> historical TRADES with aggressor side
  * data.binance.vision metrics dumps    -> historical OPEN INTEREST
  * fapi /fapi/v1/fundingRate (REST)     -> FUNDING history
  * fapi /fapi/v1/ticker/bookTicker      -> L1 orderbook (best bid/ask + sizes), LIVE
- The hard FREE gap is LIQUIDATIONS: no free historical dump
  (liquidationSnapshot returns 404) -> forward-only via websocket. So a fully
  MICROSTRUCTURE_RESEARCH_READY sample requires forward-collecting liquidations
  (and L1 orderbook snapshots) for >=30 days, while trades/OI/funding are free
  and historical right now.

This module: public GET only, NO API keys, NO auth headers, NO private endpoints,
NO DB, NO raw/prod writes, NO orders, dry-run by default, staging-only writes.
FINAL_RECOMMENDATION: NO LIVE.
"""

from __future__ import annotations

import csv
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import FINAL_RECOMMENDATION_NO_LIVE

TOOL_VERSION = "v10.25"
STAGING_MARKER = "free_microstructure_v10_25"
DEFAULT_STAGING_DIR = f"external_data/staging/{STAGING_MARKER}"

# EXACT runtime allowlist: only the public, no-auth GET endpoints this module
# actually calls. Hosts/paths that only appear in the documentation registry
# (Bybit, OKX, Kaggle) are NOT runtime-allowlisted -- the helper must never be
# reusable to reach a private/account/order endpoint.
_ALLOWED_EXACT = {
    "fapi.binance.com": frozenset({
        "/fapi/v1/aggTrades",
        "/fapi/v1/ticker/bookTicker",
        "/fapi/v1/fundingRate",
        "/fapi/v1/depth",
        "/futures/data/openInterestHist",
    }),
}
# data.binance.vision serves only static public dump FILES (no private API exists
# on that host), so a GET-only path prefix is the right scoping for bulk dumps.
_ALLOWED_PREFIX = {
    "data.binance.vision": ("/data/",),
}
# Defense-in-depth: never reach anything that smells private/account/order even
# if an allowlist entry were ever loosened.
_DENY_PATH_SUBSTR = ("/account", "/order", "/leverage", "/margintype", "/margin/",
                     "/positionrisk", "/position", "/userdatastream", "/listenkey",
                     "/apikey", "/withdraw", "/balance", "/transfer", "/adlquantile")
_AUTH_HEADER_KEYS = ("authorization", "cookie", "x-api-key", "apikey", "api-key",
                     "x-mbx-apikey", "token", "signature")
# signed/auth query params must never appear, even on a public allowlisted endpoint
_SENSITIVE_QUERY_KEYS = frozenset({"signature", "apikey", "api_key", "api-key",
                                   "timestamp", "recvwindow", "secret", "token",
                                   "x-mbx-apikey", "accesskey", "access_key"})

_FORBIDDEN_SEG = ("raw", "backup", "backups", "vault", "vaults", "prod", "production",
                  "live", "real", "private", "secret", "secrets", "credential",
                  "credentials", "db", "database", ".git", "node_modules")
_FORBIDDEN_SUF = (".env", ".db", ".sqlite", ".pem", ".key")

# free-source verdicts
USABLE_FREE = "USABLE_FREE"
PARTIAL_FREE = "PARTIAL_FREE"
FORWARD_ONLY = "FORWARD_ONLY"
NOT_ENOUGH = "NOT_ENOUGH"
PAID_ONLY = "PAID_ONLY"
DO_NOT_USE = "DO_NOT_USE"
UNKNOWN = "UNKNOWN_NEEDS_MANUAL_CHECK"


def _safety() -> dict[str, Any]:
    return {"research_only": True, "shadow_only": True, "paper_ready": False,
            "live_ready": False, "can_send_real_orders": False,
            "paper_filter_enabled": False, "paper_candidate_future": False,
            "makes_no_trades": True, "uses_api_keys": False, "uses_db": False,
            "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


# --------------------------------------------------------------------------
# Network safety (public GET only, no auth)
# --------------------------------------------------------------------------

def assert_safe_request(url: str, headers: dict[str, str] | None, method: str = "GET") -> None:
    if str(method).upper() != "GET":
        raise ValueError(f"only GET allowed, got {method}")
    p = urllib.parse.urlparse(url)
    if p.scheme != "https":
        raise ValueError(f"non-https blocked: {url}")
    host = p.hostname
    path = p.path
    low_path = path.lower()
    if any(tok in low_path for tok in _DENY_PATH_SUBSTR):
        raise ValueError(f"private/account-like endpoint blocked: {path}")
    if host in _ALLOWED_EXACT:
        if path not in _ALLOWED_EXACT[host]:
            raise ValueError(f"path not in exact allowlist for {host}: {path}")
    elif host in _ALLOWED_PREFIX:
        if not any(path.startswith(pre) for pre in _ALLOWED_PREFIX[host]):
            raise ValueError(f"path prefix not allowlisted for {host}: {path}")
    else:
        raise ValueError(f"host not allowlisted: {host}")
    # defense in depth: no signed/auth query params even on a public endpoint
    qkeys = {k.lower() for k in urllib.parse.parse_qs(p.query, keep_blank_values=True)}
    bad_q = qkeys & _SENSITIVE_QUERY_KEYS
    if bad_q:
        raise ValueError(f"sensitive query param blocked: {sorted(bad_q)}")
    for k in (headers or {}):
        if k.lower() in _AUTH_HEADER_KEYS:
            raise ValueError(f"auth header blocked: {k}")


def default_transport(url: str, headers: dict[str, str]) -> bytes:
    assert_safe_request(url, headers, method="GET")
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read()


# --------------------------------------------------------------------------
# Staging safety
# --------------------------------------------------------------------------

def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _resolved(path: str) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = _repo_root() / p
    return p.resolve(strict=False)


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def safe_staging_dir(base: str | None = None) -> str:
    """Writes are allowed ONLY inside the EXACT resolved staging root
    repo/external_data/staging/free_microstructure_v10_25. Merely containing the
    marker string elsewhere (reports/.., tmp/.., ../) or a symlink that resolves
    outside is REJECTED. This validates only -- it never creates dirs."""
    root = base or DEFAULT_STAGING_DIR
    segs = [s for s in str(root).replace("\\", "/").split("/") if s]
    if ".." in segs:
        raise ValueError("staging traversal blocked")
    for s in (x.lower() for x in segs):
        if s in _FORBIDDEN_SEG or s.endswith(_FORBIDDEN_SUF) or ".env" in s:
            raise ValueError(f"forbidden staging segment: {s}")
    repo = _repo_root()                       # already the resolved real repo path
    # LOGICAL allowed root (do NOT resolve symlinks -- a symlinked marker that
    # resolves elsewhere must be rejected, not silently accepted as the new root).
    logical_root = repo / "external_data" / "staging" / STAGING_MARKER
    # 1) no symlink anywhere in repo -> external_data -> staging -> marker chain
    for comp in (repo / "external_data", repo / "external_data" / "staging", logical_root):
        try:
            if comp.exists() and comp.is_symlink():
                raise ValueError(f"symlinked staging root component blocked: {comp}")
        except OSError:
            break
    # 2) logical target (lexical normalize; .. already rejected)
    target = Path(root)
    if not target.is_absolute():
        target = repo / target
    target = Path(os.path.normpath(str(target)))
    # 3) target must be exactly the logical root or lexically inside it
    if target != logical_root and logical_root not in target.parents:
        raise ValueError("staging dir must be inside external_data/staging/"
                         f"{STAGING_MARKER} (got {target})")
    # 4) no symlink among existing ancestors from target up to (not incl.) repo
    for anc in [target, *target.parents]:
        if anc == repo or anc in repo.parents:
            break
        try:
            if anc.exists() and anc.is_symlink():
                raise ValueError(f"symlinked staging component blocked: {anc}")
        except OSError:
            break
    # 5) defense: the real resolved target must still live inside the real repo
    if not (target.resolve(strict=False) == repo or _is_within(target.resolve(strict=False), repo)):
        raise ValueError("staging dir resolves outside the repo")
    return root


# --------------------------------------------------------------------------
# Source registry (verdicts probed live 2026; re-verify before relying)
# --------------------------------------------------------------------------

def free_sources_registry() -> list[dict[str, Any]]:
    return [
        {"source": "Binance USD-M data.binance.vision aggTrades dumps",
         "data": ["trades(aggressor side)"], "free": True, "account": False, "api_key": False,
         "history": "bulk historical daily/monthly ZIP", "rate_limit": "static files, gentle",
         "quality": "high", "verdict": USABLE_FREE,
         "v1024_fit": "trades -> canonical trades.csv (timestamp,symbol,price,size,aggressor_side)"},
        {"source": "Binance USD-M data.binance.vision metrics dumps",
         "data": ["open_interest", "long_short_ratio"], "free": True, "account": False, "api_key": False,
         "history": "bulk historical daily ZIP", "rate_limit": "static files",
         "quality": "high", "verdict": USABLE_FREE,
         "v1024_fit": "open_interest -> canonical open_interest.csv"},
        {"source": "Binance fapi /fapi/v1/fundingRate (REST)",
         "data": ["funding"], "free": True, "account": False, "api_key": False,
         "history": "full history via pagination", "rate_limit": "weighted, be gentle",
         "quality": "high", "verdict": USABLE_FREE,
         "v1024_fit": "funding -> canonical funding.csv"},
        {"source": "Binance fapi /fapi/v1/ticker/bookTicker (REST, live)",
         "data": ["orderbook L1 (best bid/ask + sizes)"], "free": True, "account": False, "api_key": False,
         "history": "FORWARD only (poll now); historical daily dump returned 404",
         "rate_limit": "weighted", "quality": "L1 only (no full L2 depth free)",
         "verdict": FORWARD_ONLY,
         "v1024_fit": "orderbook L1 -> orderbook_l2.csv (bid_price_1,bid_size_1,ask_price_1,ask_size_1)"},
        {"source": "Binance fapi /fapi/v1/depth (REST, live)",
         "data": ["orderbook L2 snapshot"], "free": True, "account": False, "api_key": False,
         "history": "FORWARD only (snapshot now, no free historical L2)",
         "rate_limit": "heavy weight at depth>5", "quality": "L2 live snapshots",
         "verdict": FORWARD_ONLY, "v1024_fit": "multi-level orderbook (forward only)"},
        {"source": "Binance liquidations (forceOrder)",
         "data": ["liquidations"], "free": True, "account": False, "api_key": False,
         "history": "NO free historical dump (liquidationSnapshot 404); websocket forward only",
         "rate_limit": "ws stream", "quality": "forward only",
         "verdict": FORWARD_ONLY,
         "v1024_fit": "liquidations -> needs >=30d forward websocket collection"},
        {"source": "Bybit public.bybit.com/trading dumps",
         "data": ["trades"], "free": True, "account": False, "api_key": False,
         "history": "bulk historical CSV", "rate_limit": "static files",
         "quality": "high (trades)", "verdict": USABLE_FREE,
         "v1024_fit": "trades -> canonical trades.csv (cross-venue check)"},
        {"source": "OKX public REST/history",
         "data": ["trades", "ohlcv", "oi", "funding"], "free": True, "account": False, "api_key": False,
         "history": "recent via REST; bulk uncertain", "rate_limit": "weighted",
         "quality": "good", "verdict": UNKNOWN,
         "v1024_fit": "secondary cross-venue source; verify dump availability manually"},
        {"source": "CryptoDataDownload",
         "data": ["ohlcv"], "free": True, "account": False, "api_key": False,
         "history": "OHLCV CSV (no real microstructure depth/aggressor)",
         "rate_limit": "static", "quality": "OHLCV only", "verdict": NOT_ENOUGH,
         "v1024_fit": "no microstructure -> not enough for V10.24.3"},
        {"source": "Kaggle / GitHub crypto LOB datasets",
         "data": ["varies (some L2)"], "free": True, "account": "sometimes", "api_key": False,
         "history": "varies; freshness/licence varies", "rate_limit": "n/a",
         "quality": "unverified", "verdict": UNKNOWN,
         "v1024_fit": "manual check licence + schema before trusting"},
        {"source": "Tardis.dev / CoinGlass / Kaiko (full L2 + liquidations history)",
         "data": ["trades", "L2", "oi", "funding", "liquidations"], "free": False,
         "account": True, "api_key": True, "history": "deep historical",
         "rate_limit": "paid tiers", "quality": "best", "verdict": PAID_ONLY,
         "v1024_fit": "the paid unlock; NOT used here"},
    ]


def free_microstructure_plan() -> dict[str, Any]:
    reg = free_sources_registry()
    return {
        "tool_version": TOOL_VERSION,
        "objective": "convert FREE public data into V10.24.3 canonical format; NO strategy, NO orders",
        "best_free_route": [
            "1. TRADES (historical, free): download Binance data.binance.vision aggTrades daily "
            "dumps for BTCUSDT, then convert -> trades.csv (aggressor side from is_buyer_maker).",
            "2. OPEN INTEREST (historical, free): Binance metrics dumps -> open_interest.csv.",
            "3. FUNDING (historical, free): fapi /fapi/v1/fundingRate paginate -> funding.csv.",
            "4. ORDERBOOK L1 (forward, free): poll fapi bookTicker over time -> orderbook_l2.csv "
            "(L1 sizes give l1_imbalance; full L2 is not free historically).",
            "5. LIQUIDATIONS (forward, free): websocket !forceOrder is the only free path; needs "
            ">=30d forward collection. This is the gap that blocks full READY for free.",
        ],
        "honest_summary": (
            "FREE path = PARTIAL_FREE. trades+OI+funding are free AND historical now; "
            "orderbook is free but L1-only and forward; liquidations are forward-only. "
            "A fully MICROSTRUCTURE_RESEARCH_READY free sample needs ~30d of forward "
            "orderbook-L1 + liquidations collection on top of the historical trades/OI/funding."),
        "limitations": [
            "V10.25 is a PLAN + a partial FORWARD collector, NOT a full historical pipeline.",
            "There is NO end-to-end CLI that downloads+unzips Binance ZIP dumps yet; "
            "the collector exposes only in-process row converters + a bounded REST forward fetch.",
            "REST aggTrades returns only recent trades; it does NOT replace 180/365d dumps.",
            "orderbook_l2.csv produced from bookTicker is L1 (depth_level=L1_BOOKTICKER), "
            "NOT real historical L2 depth.",
            "Free historical liquidations do not exist (forward websocket only).",
            "This does NOT promise an instant MICROSTRUCTURE_RESEARCH_READY sample."],
        "v1024_canonical_targets": {
            "trades": ["timestamp", "symbol", "price", "size", "aggressor_side"],
            "orderbook": ["timestamp", "symbol", "bid_price_1", "bid_size_1", "ask_price_1", "ask_size_1"],
            "oi": ["timestamp", "symbol", "open_interest"],
            "funding": ["timestamp", "symbol", "funding_rate"]},
        "sources": reg,
        "never": ["api_keys", "auth_headers", "private_endpoints", "orders", "db_write",
                  "raw_write", "paid_provider", "paper_or_live_promotion"],
        "writes_on_plan": False, **_safety()}


# --------------------------------------------------------------------------
# Converters: raw free data -> V10.24.3 canonical rows (OFFLINE, deterministic)
# --------------------------------------------------------------------------

def _agg_side(is_buyer_maker: Any) -> str:
    # Binance aggTrades: m=True means buyer is the maker -> the AGGRESSOR is the seller.
    truthy = str(is_buyer_maker).strip().lower() in ("true", "1", "t", "yes")
    return "sell" if truthy else "buy"


def aggtrades_to_canonical(rows: list[dict], symbol: str) -> list[dict[str, Any]]:
    """Binance aggTrades JSON rows OR dump rows -> canonical trade rows."""
    out = []
    for r in rows:
        price = r.get("p", r.get("price"))
        qty = r.get("q", r.get("quantity"))
        ts = r.get("T", r.get("transact_time", r.get("time")))
        ibm = r.get("m", r.get("is_buyer_maker"))
        if price is None or qty is None or ts is None:
            continue
        out.append({"timestamp": int(float(ts)), "symbol": symbol, "price": price,
                    "size": qty, "aggressor_side": _agg_side(ibm)})
    return out


def bookticker_to_canonical(rows: list[dict], symbol: str) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        ts = r.get("time", r.get("T", r.get("timestamp")))
        bp, bq = r.get("bidPrice", r.get("best_bid_price")), r.get("bidQty", r.get("best_bid_qty"))
        ap, aq = r.get("askPrice", r.get("best_ask_price")), r.get("askQty", r.get("best_ask_qty"))
        if None in (ts, bp, bq, ap, aq):
            continue
        out.append({"timestamp": int(float(ts)), "symbol": symbol,
                    "bid_price_1": bp, "bid_size_1": bq, "ask_price_1": ap, "ask_size_1": aq,
                    "depth_level": "L1_BOOKTICKER"})  # honest: this is L1, not real L2
    return out


def oi_to_canonical(rows: list[dict], symbol: str) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        ts = r.get("timestamp", r.get("create_time", r.get("time")))
        oi = r.get("sumOpenInterest", r.get("sum_open_interest", r.get("open_interest")))
        if ts is None or oi is None:
            continue
        out.append({"timestamp": int(float(ts)), "symbol": symbol, "open_interest": oi})
    return out


def funding_to_canonical(rows: list[dict], symbol: str) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        ts = r.get("fundingTime", r.get("funding_time", r.get("timestamp")))
        fr = r.get("fundingRate", r.get("funding_rate"))
        if ts is None or fr is None:
            continue
        row_sym = r.get("symbol")
        if row_sym is not None and str(row_sym) != str(symbol):
            # fail-closed: never silently relabel a different instrument's funding
            raise ValueError(f"SYMBOL_MISMATCH: requested {symbol} but row has {row_sym}")
        out.append({"timestamp": int(float(ts)), "symbol": str(symbol), "funding_rate": fr})
    return out


_CANON = {
    "trades": ("trades.csv", ["timestamp", "symbol", "price", "size", "aggressor_side"]),
    "orderbook": ("orderbook_l2.csv", ["timestamp", "symbol", "bid_price_1", "bid_size_1",
                                       "ask_price_1", "ask_size_1", "depth_level"]),
    "oi": ("open_interest.csv", ["timestamp", "symbol", "open_interest"]),
    "funding": ("funding.csv", ["timestamp", "symbol", "funding_rate"]),
}


def _write_canonical(out_dir: str, kind: str, rows: list[dict[str, Any]]) -> str:
    fname, header = _CANON[kind]
    path = os.path.join(out_dir, fname).replace("\\", "/")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in header})
    return path


# --------------------------------------------------------------------------
# Forward collector (REST GET, bounded, apply-gated, staging-only)
# --------------------------------------------------------------------------

_BINANCE = "https://fapi.binance.com"


def _planned_urls(symbol: str) -> dict[str, str]:
    s = urllib.parse.quote(symbol)
    return {
        "trades": f"{_BINANCE}/fapi/v1/aggTrades?symbol={s}&limit=1000",
        "orderbook": f"{_BINANCE}/fapi/v1/ticker/bookTicker?symbol={s}",
        "oi": f"{_BINANCE}/futures/data/openInterestHist?symbol={s}&period=5m&limit=500",
        "funding": f"{_BINANCE}/fapi/v1/fundingRate?symbol={s}&limit=1000",
    }


def forward_collect(symbol: str, kinds: list[str], apply: bool = False,
                    output_dir: str | None = None, rate_limit_seconds: float = 0.5,
                    transport: Callable[[str, dict[str, str]], bytes] | None = None,
                    orderbook_polls: int = 1) -> dict[str, Any]:
    """DRY-RUN by default: returns the exact public URLs it WOULD GET, no network,
    no writes. With apply=True it performs bounded public GETs and writes canonical
    CSVs ONLY under the v10_25 staging marker."""
    kinds = [k for k in kinds if k in _CANON]
    urls = _planned_urls(symbol)
    rep: dict[str, Any] = {"tool_version": TOOL_VERSION, "symbol": symbol, "kinds": kinds,
                           "apply": bool(apply), "planned_urls": {k: urls[k] for k in kinds},
                           "run_id": _now_stamp(), "written": [], "errors": [],
                           "note": "L1 orderbook only; liquidations require websocket (not collected here)",
                           **_safety()}
    if not apply:
        rep["mode"] = "DRY_RUN"
        rep["writes"] = False
        return rep
    rep["mode"] = "APPLY"
    try:
        staging = safe_staging_dir(output_dir)   # validate BEFORE any network/writes
    except ValueError as e:
        rep["errors"].append(f"unsafe_output_dir:{e}")
        rep["writes"] = False
        return rep
    tr = transport or default_transport
    hdr = {"User-Agent": "researchops/1.0", "Accept": "application/json"}
    out_dir = os.path.join(staging, rep["run_id"]).replace("\\", "/")
    os.makedirs(out_dir, exist_ok=True)
    rep["staging_dir"] = out_dir
    for k in kinds:
        try:
            if k == "orderbook":
                snaps = []
                for _ in range(max(1, int(orderbook_polls))):
                    raw = tr(urls["orderbook"], hdr)
                    obj = json.loads(raw)
                    snaps.append(obj if isinstance(obj, dict) else obj[0])
                    time.sleep(max(0.0, float(rate_limit_seconds)))
                canon = bookticker_to_canonical(snaps, symbol)
            else:
                raw = tr(urls[k], hdr)
                data = json.loads(raw)
                if k == "trades":
                    canon = aggtrades_to_canonical(data, symbol)
                elif k == "oi":
                    canon = oi_to_canonical(data, symbol)
                else:
                    canon = funding_to_canonical(data, symbol)
                time.sleep(max(0.0, float(rate_limit_seconds)))
            rep["written"].append({"kind": k, "rows": len(canon),
                                   "file": _write_canonical(out_dir, k, canon)})
        except Exception as e:
            rep["errors"].append(f"{k}:{type(e).__name__}:{str(e)[:80]}")
    return rep
