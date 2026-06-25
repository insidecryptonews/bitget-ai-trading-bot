"""ResearchOps V10.26 - Free Public Liquidations WebSocket Forward Collector.

The one free microstructure gap left by V10.25.x is LIQUIDATIONS: there is no
free historical dump, only a public forward websocket stream. This module starts
collecting liquidations FROM NOW into the V10.24.3 canonical format.

Verified live (2026): fstream.binance.com:443 is TLS-reachable; the public,
no-auth Binance USD-M force-order (liquidation) streams are:
  wss://fstream.binance.com/ws/!forceOrder@arr   (all symbols)
  wss://fstream.binance.com/ws/<symbol>@forceOrder
forceOrder event payload (Binance public docs):
  {"e":"forceOrder","E":<eventMs>,"o":{"s":SYMBOL,"S":"SELL|BUY","q":qty,
    "p":price,"ap":avgPrice,"X":"FILLED","l":lastQty,"z":filledQty,"T":tradeMs}}
  S=SELL => a LONG was liquidated; S=BUY => a SHORT was liquidated.
Limitations: FORWARD ONLY (no history); events only occur when liquidations
happen, so quiet markets produce sparse data. Not enough density for a long time
=> V10.24.3 will say NEEDS_MORE_HISTORY, NOT READY.

Research-only. Public websocket only, NO API keys, NO auth, NO private channels,
NO DB, NO raw/prod writes, NO orders. Dry-run by default; staging-only writes.
FINAL_RECOMMENDATION: NO LIVE.
"""

from __future__ import annotations

import csv
import json
import os
import re
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from . import FINAL_RECOMMENDATION_NO_LIVE

TOOL_VERSION = "v10.26"
STAGING_MARKER = "free_liquidations_ws_v10_26"
DEFAULT_STAGING_DIR = f"external_data/staging/{STAGING_MARKER}"

# EXACT public websocket allowlist (no-auth liquidation streams only)
_ALLOWED_WS_HOSTS = ("fstream.binance.com",)
_DENY_WS_SUBSTR = ("listenkey", "userdata", "/account", "/order", "apikey",
                   "auth", "private", "/leverage", "/margintype")
_AUTH_HEADER_KEYS = ("authorization", "cookie", "x-api-key", "apikey", "api-key",
                     "x-mbx-apikey", "token", "signature")
_SENSITIVE_QUERY_KEYS = frozenset({"signature", "apikey", "api_key", "api-key",
                                   "timestamp", "recvwindow", "secret", "token",
                                   "x-mbx-apikey", "listenkey", "accesskey", "access_key"})

_FORBIDDEN_SEG = ("raw", "backup", "backups", "vault", "vaults", "prod", "production",
                  "live", "real", "private", "secret", "secrets", "credential",
                  "credentials", "db", "database", ".git", "node_modules")
_FORBIDDEN_SUF = (".env", ".db", ".sqlite", ".pem", ".key")

# liquidation stream verdicts
USABLE_FREE = "USABLE_FREE_FORWARD"
UNKNOWN = "UNKNOWN_NEEDS_MANUAL_CHECK"

CANON_HEADER = ["timestamp", "exchange", "symbol", "side", "price", "size",
                "notional", "source", "event_type", "raw_event_id", "received_at"]


def _safety() -> dict[str, Any]:
    return {"research_only": True, "shadow_only": True, "paper_ready": False,
            "live_ready": False, "can_send_real_orders": False,
            "paper_filter_enabled": False, "paper_candidate_future": False,
            "makes_no_trades": True, "uses_api_keys": False, "uses_db": False,
            "subscribes_private_channels": False,
            "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


# --------------------------------------------------------------------------
# WebSocket request safety
# --------------------------------------------------------------------------

def assert_safe_ws(url: str, headers: dict[str, str] | None) -> None:
    p = urllib.parse.urlparse(url)
    if p.scheme != "wss":
        raise ValueError(f"only wss allowed, got {p.scheme}")
    if p.hostname not in _ALLOWED_WS_HOSTS:
        raise ValueError(f"ws host not allowlisted: {p.hostname}")
    low = p.path.lower()
    if any(tok in low for tok in _DENY_WS_SUBSTR):
        raise ValueError(f"private/auth-like ws path blocked: {p.path}")
    ok = low == "/ws/!forceorder@arr" or re.fullmatch(r"/ws/[a-z0-9]+@forceorder", low)
    if not ok:
        raise ValueError(f"ws path not an allowlisted public liquidation stream: {p.path}")
    qkeys = {k.lower() for k in urllib.parse.parse_qs(p.query, keep_blank_values=True)}
    if qkeys & _SENSITIVE_QUERY_KEYS:
        raise ValueError(f"sensitive ws query param blocked: {sorted(qkeys & _SENSITIVE_QUERY_KEYS)}")
    for k in (headers or {}):
        if k.lower() in _AUTH_HEADER_KEYS:
            raise ValueError(f"auth header blocked: {k}")


def _binance_ws_url(symbols: list[str]) -> str:
    syms = [s.lower() for s in symbols if s]
    if len(syms) == 1:
        return f"wss://fstream.binance.com/ws/{syms[0]}@forceOrder"
    return "wss://fstream.binance.com/ws/!forceOrder@arr"   # all-market liquidations


# --------------------------------------------------------------------------
# Staging safety (hardened V10.25.2-style: logical root, no symlink escape)
# --------------------------------------------------------------------------

def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def safe_staging_dir(base: str | None = None) -> str:
    """Validate-only: writes are allowed ONLY inside the EXACT logical root
    repo/external_data/staging/free_liquidations_ws_v10_26. Rejects traversal,
    forbidden segments, the marker-as-substring elsewhere, a symlinked root/marker,
    and a symlinked ancestor that could escape. Never creates dirs."""
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
        raise ValueError("staging dir must be inside external_data/staging/"
                         f"{STAGING_MARKER} (got {target})")
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
# Registry + plan
# --------------------------------------------------------------------------

def liquidation_streams_registry() -> list[dict[str, Any]]:
    return [
        {"exchange": "binance_usdm", "ws_url": "wss://fstream.binance.com/ws/!forceOrder@arr",
         "per_symbol": "wss://fstream.binance.com/ws/<symbol>@forceOrder",
         "requires_key": False, "event": "forceOrder",
         "fields": ["E(event_ms)", "o.s(symbol)", "o.S(side)", "o.p(price)",
                    "o.ap(avg_price)", "o.q(qty)", "o.z(filled_qty)", "o.T(trade_ms)", "o.X(status)"],
         "side_semantics": "S=SELL -> long liquidated; S=BUY -> short liquidated",
         "host_reachable_probe": "fstream.binance.com:443 TLS reachable (verified)",
         "limitations": ["forward only", "no history", "events only on liquidations",
                         "sparse in quiet markets"],
         "verdict": USABLE_FREE},
        {"exchange": "bybit", "ws_url": "wss://stream.bybit.com/v5/public/linear (liquidation topic)",
         "requires_key": False, "event": "liquidation",
         "fields": ["UNKNOWN - verify topic + payload manually"],
         "side_semantics": "UNKNOWN", "limitations": ["forward only"],
         "verdict": UNKNOWN},
        {"exchange": "bitget", "ws_url": "wss://ws.bitget.com/v2/ws/public (liquidation channel)",
         "requires_key": False, "event": "liquidation",
         "fields": ["UNKNOWN - verify channel + payload manually"],
         "side_semantics": "UNKNOWN", "limitations": ["forward only"],
         "verdict": UNKNOWN},
    ]


def liquidations_ws_plan() -> dict[str, Any]:
    return {
        "tool_version": TOOL_VERSION,
        "objective": "collect FREE public liquidations forward via websocket into V10.24.3 canonical CSV",
        "default_mode": "DRY_RUN (no websocket, no writes)",
        "implemented_exchanges": ["binance_usdm"],
        "canonical_header": CANON_HEADER,
        "honest_summary": (
            "Liquidations have NO free history -> forward-only. Run the collector "
            "for days/weeks to build density; quiet markets are sparse. This does "
            "NOT produce an instant MICROSTRUCTURE_RESEARCH_READY sample."),
        "streams": liquidation_streams_registry(),
        "never": ["api_keys", "auth_headers", "private_channels", "orders", "db_write",
                  "raw_write", "paid_provider", "paper_or_live_promotion"],
        "writes_on_plan": False, **_safety()}


# --------------------------------------------------------------------------
# Parsing: exchange event -> canonical liquidation row
# --------------------------------------------------------------------------

def _norm_side(v: Any) -> str | None:
    s = str(v).strip().lower()
    if s in ("sell", "s", "ask", "short"):
        return "sell"
    if s in ("buy", "b", "bid", "long"):
        return "buy"
    return None


def parse_binance_force_order(event: dict, received_at: int | None = None) -> tuple[dict | None, str | None]:
    """Returns (canonical_row, reason_if_rejected)."""
    try:
        if event.get("e") != "forceOrder" or "o" not in event:
            return None, "not_a_force_order_event"
        o = event["o"]
        symbol = o.get("s")
        side = _norm_side(o.get("S"))
        price = o.get("ap") or o.get("p")
        size = o.get("z") or o.get("q")
        ts = o.get("T") or event.get("E")
        if not symbol or price is None or size is None or ts is None:
            return None, "missing_required_field"
        if side is None:
            return None, "side_mapping_uncertain"
        pf, sf = float(price), float(size)
        if pf <= 0 or sf <= 0:
            return None, "non_positive_price_or_size"
        row = {"timestamp": int(float(ts)), "exchange": "binance_usdm", "symbol": str(symbol),
               "side": side, "price": price, "size": size, "notional": round(pf * sf, 8),
               "source": "fstream.binance.com/forceOrder", "event_type": "forceOrder",
               "raw_event_id": f"binance_usdm:{symbol}:{int(float(ts))}:{side}:{size}:{price}",
               "received_at": int(received_at if received_at is not None else _now_ms())}
        return row, None
    except (TypeError, ValueError):
        return None, "parse_error"


_PARSERS: dict[str, Callable[[dict, int | None], tuple[dict | None, str | None]]] = {
    "binance_usdm": parse_binance_force_order,
}


def _parse_event(exchange: str, raw: Any, received_at: int) -> tuple[dict | None, str]:
    parser = _PARSERS.get(exchange)
    if parser is None:
        return None, f"exchange_not_implemented:{exchange}"
    try:
        event = raw if isinstance(raw, dict) else json.loads(raw)
    except Exception:
        return None, "json_decode_error"
    row, why = parser(event, received_at)
    return row, (why or "")


# --------------------------------------------------------------------------
# Real event source (websocket-client; only used on apply, never in tests)
# --------------------------------------------------------------------------

def _default_event_source(url: str, max_runtime_seconds: float, max_events: int) -> Iterable[str]:
    assert_safe_ws(url, {})
    try:
        import websocket  # websocket-client (no auth, public stream)
    except Exception as e:  # pragma: no cover - exercised only in real runs
        raise RuntimeError(f"websocket_client_unavailable:{type(e).__name__}") from e
    ws = websocket.create_connection(url, timeout=min(15, max(5, int(max_runtime_seconds))))
    start = time.time()
    n = 0
    try:
        ws.settimeout(2)
        while time.time() - start < max_runtime_seconds and n < max_events:
            try:
                msg = ws.recv()
            except Exception:
                continue
            if msg:
                n += 1
                yield msg
    finally:
        try:
            ws.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# Collect (dry-run default; staging-only; bounded)
# --------------------------------------------------------------------------

def collect(exchange: str, symbols: list[str], apply: bool = False,
            max_runtime_seconds: float = 5.0, max_events: int = 5,
            output_dir: str | None = None,
            event_source: Iterable[Any] | None = None) -> dict[str, Any]:
    exchange = (exchange or "binance_usdm").lower()
    url = _binance_ws_url(symbols) if exchange == "binance_usdm" else f"<{exchange}_not_implemented>"
    rep: dict[str, Any] = {"tool_version": TOOL_VERSION, "exchange": exchange,
                           "symbols": symbols, "ws_url": url, "apply": bool(apply),
                           "max_runtime_seconds": float(max_runtime_seconds),
                           "max_events": int(max_events), "run_id": _now_stamp(),
                           "event_count": 0, "duplicates": 0, "rejected": [], "errors": [],
                           "note": "forward-only; no history; sparse in quiet markets",
                           **_safety()}
    if not apply:
        rep["mode"] = "DRY_RUN"
        rep["writes"] = False
        return rep
    rep["mode"] = "APPLY"
    if exchange not in _PARSERS:
        rep["errors"].append(f"exchange_not_implemented:{exchange}")
        rep["writes"] = False
        return rep
    try:
        staging = safe_staging_dir(output_dir)        # validate BEFORE any network/write
    except ValueError as e:
        rep["errors"].append(f"unsafe_output_dir:{e}")
        rep["writes"] = False
        return rep
    try:
        assert_safe_ws(url, {})
    except ValueError as e:
        rep["errors"].append(f"unsafe_ws:{e}")
        rep["writes"] = False
        return rep
    rows: list[dict] = []
    seen: set[str] = set()
    start = time.time()
    try:
        src = event_source if event_source is not None else _default_event_source(
            url, max_runtime_seconds, max_events)
        for raw in src:
            if (time.time() - start) > max_runtime_seconds or len(rows) >= max_events:
                break
            row, why = _parse_event(exchange, raw, received_at=_now_ms())
            if row is None:
                rep["rejected"].append(why)
                continue
            key = row["raw_event_id"]
            if key in seen:
                rep["duplicates"] += 1
                continue
            seen.add(key)
            rows.append(row)
    except Exception as e:
        rep["errors"].append(f"source:{type(e).__name__}:{str(e)[:80]}")
    out_dir = os.path.join(staging, rep["run_id"]).replace("\\", "/")
    os.makedirs(out_dir, exist_ok=True)
    out_file = os.path.join(out_dir, "liquidations.csv").replace("\\", "/")
    with open(out_file, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CANON_HEADER)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in CANON_HEADER})
    rep["event_count"] = len(rows)
    rep["output_file"] = out_file
    rep["staging_dir"] = out_dir
    rep["writes"] = True
    rep["manifest"] = _write_manifest(out_dir, rep, start)
    return rep


def _write_manifest(out_dir: str, rep: dict[str, Any], start_ts: float) -> str:
    manifest = {
        "run_id": rep["run_id"], "tool_version": TOOL_VERSION,
        "start_time": datetime.fromtimestamp(start_ts, timezone.utc).isoformat(),
        "end_time": datetime.now(timezone.utc).isoformat(),
        "exchange": rep["exchange"], "symbols": rep["symbols"], "ws_url": rep["ws_url"],
        "event_count": rep["event_count"], "duplicates": rep["duplicates"],
        "rejected_count": len(rep["rejected"]), "errors": rep["errors"],
        "output_file": rep.get("output_file"),
        "notes": "forward-only liquidations; no history; run for days/weeks for density; "
                 "validate with microstructure-sample-validate-v1024.",
        "research_only": True, "shadow_only": True, "live_ready": False,
        "can_send_real_orders": False, "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}
    path = os.path.join(out_dir, "manifest.json").replace("\\", "/")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, default=str)
    return path
