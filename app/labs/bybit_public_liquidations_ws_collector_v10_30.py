"""ResearchOps V10.30 - Bybit Public Liquidations Forward Collector (research only).

WHY THIS EXISTS: live probes (2026-07-04) showed Binance FUTURES websocket
(fstream.binance.com) completes the handshake but delivers ZERO frames on any
stream from this network (aggTrade/markPrice/forceOrder, raw+combined, browser
headers too), while Binance spot ws and Bybit v5 public linear ws flow
instantly. Binance-native liquidations are therefore unreachable here and the
V10.24.3 liquidations floor could never fill. This collector captures PUBLIC
Bybit linear liquidations as a SEPARATE, clearly-labelled source.

DESIGN = OPTION A (separate source, no mixing):
  - own staging marker `bybit_liquidations_v10_30` (never raw/prod);
  - the V10.29 assembler does NOT read it: Bybit rows can never leak into the
    Binance sample nor produce MICROSTRUCTURE_RESEARCH_READY;
  - dashboard/gap report may SHOW it as an alternative source only, with
    cross_exchange_liquidations_used_for_ready=False.

SIDE SEMANTICS (verified 2026-07-04 against the official Bybit v5 docs,
"All Liquidation" websocket topic): field `S` is the POSITION side --
"When you receive a Buy update, this means that a long position has been
liquidated"; Sell => short liquidated. NOTE this is the OPPOSITE convention
to Binance forceOrder (Binance reports the closing ORDER side: SELL => long
liquidated). Rows therefore store BOTH the raw Bybit side and the derived
`position_liquidated`. Price is the documented BANKRUPTCY price (not mark or
last trade) and is labelled as such.

Public stream only, NO keys, NO private topics, NO orders, NO DB, dry-run by
default, bounded runtime/events, staging-only writes. FINAL: NO LIVE.
"""

from __future__ import annotations

import csv
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from . import FINAL_RECOMMENDATION_NO_LIVE
from . import free_public_liquidations_ws_collector_v10_26 as V26

TOOL_VERSION = "v10.30"
STAGING_MARKER = "bybit_liquidations_v10_30"
DEFAULT_STAGING_DIR = f"external_data/staging/{STAGING_MARKER}"
DATASET_SUBDIR = "dataset"
SOURCE_EXCHANGE = "bybit_linear"
WS_URL = "wss://stream.bybit.com/v5/public/linear"
SIDE_MAPPING_VERIFIED = True   # official v5 docs, All Liquidation topic (2026-07-04)
LIQUIDATIONS_USABLE_FOR_RESEARCH = True
CROSS_EXCHANGE_USED_FOR_READY = False   # OPTION A: never feeds Binance readiness

_TOPIC_RE = re.compile(r"^allLiquidation\.[A-Z0-9]{2,20}$")
_PRIVATE_TOPIC_HINTS = ("order", "execution", "position", "wallet", "greek",
                        "private", "auth", "apikey", "api_key")
_SEEN_CAP = 250_000
_PING_EVERY_S = 15.0

_FORBIDDEN_SEG = V26._FORBIDDEN_SEG
_FORBIDDEN_SUF = V26._FORBIDDEN_SUF

CANON_HEADER = ["timestamp", "exchange", "symbol", "bybit_side_raw",
                "position_liquidated", "price", "price_type", "size", "notional",
                "source", "event_type", "raw_event_id", "received_at"]


def _safety() -> dict[str, Any]:
    return {"research_only": True, "shadow_only": True, "paper_ready": False,
            "live_ready": False, "can_send_real_orders": False,
            "paper_filter_enabled": False, "makes_no_trades": True,
            "uses_api_keys": False, "uses_db": False,
            "subscribes_private_channels": False,
            "source_exchange": SOURCE_EXCHANGE,
            "side_mapping_verified": SIDE_MAPPING_VERIFIED,
            "liquidations_usable_for_research": LIQUIDATIONS_USABLE_FOR_RESEARCH,
            "cross_exchange_liquidations_used_for_ready": CROSS_EXCHANGE_USED_FOR_READY,
            "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


# --------------------------------------------------------------------------
# Safety gates: exact ws endpoint, public-topic allowlist, hardened staging
# --------------------------------------------------------------------------

def assert_safe_ws(url: str, headers: dict | None = None) -> bool:
    if url != WS_URL:
        raise ValueError(f"ws url not allowlisted (exact match required): {url}")
    for k in (headers or {}):
        lk = str(k).lower()
        if lk in ("authorization", "x-bapi-api-key", "x-bapi-sign", "x-mbx-apikey",
                  "cookie") or "key" in lk or "sign" in lk or "auth" in lk:
            raise ValueError(f"auth-like header blocked: {k}")
    return True


def assert_safe_topics(topics: list[str]) -> list[str]:
    out = []
    for t in topics:
        t = str(t)
        low = t.lower()
        if any(h in low for h in _PRIVATE_TOPIC_HINTS):
            raise ValueError(f"private-looking topic blocked: {t}")
        if not _TOPIC_RE.match(t):
            raise ValueError(f"topic not allowlisted (allLiquidation.<SYMBOL> only): {t}")
        out.append(t)
    if not out:
        raise ValueError("no topics to subscribe")
    return out


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
# Parse (official v5 All Liquidation payload)
# --------------------------------------------------------------------------

def parse_bybit_all_liquidation(msg: Any, received_at: int | None = None
                                ) -> tuple[list[dict], list[str]]:
    """One ws frame -> (canonical rows, rejection reasons). Never raises."""
    rows: list[dict] = []
    rejects: list[str] = []
    try:
        obj = msg if isinstance(msg, dict) else json.loads(msg)
    except Exception:
        return [], ["json_decode_error"]
    if "topic" not in obj:            # subscribe ack / pong / op frames
        return [], []
    if not str(obj.get("topic", "")).startswith("allLiquidation."):
        return [], ["unexpected_topic"]
    data = obj.get("data")
    entries = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
    for e in entries:
        try:
            sym = e.get("s") or e.get("symbol")
            side_raw = str(e.get("S") or e.get("side") or "").strip()
            price = e.get("p") or e.get("price")
            size = e.get("v") or e.get("size")
            ts = e.get("T") or e.get("updatedTime") or obj.get("ts")
            if not sym or price is None or size is None or ts is None:
                rejects.append("missing_required_field")
                continue
            # OFFICIAL semantics: S is the POSITION side (Buy => long liquidated)
            if side_raw.lower() == "buy":
                pos = "long"
            elif side_raw.lower() == "sell":
                pos = "short"
            else:
                rejects.append(f"unknown_side:{side_raw}")
                continue
            pf, sf = float(price), float(size)
            if pf <= 0 or sf <= 0:
                rejects.append("non_positive_price_or_size")
                continue
            ts_i = int(float(ts))
            rows.append({
                "timestamp": ts_i, "exchange": SOURCE_EXCHANGE, "symbol": str(sym),
                "bybit_side_raw": side_raw, "position_liquidated": pos,
                "price": price, "price_type": "bankruptcy_price", "size": size,
                "notional": round(pf * sf, 8),
                "source": "stream.bybit.com/v5/public/linear/allLiquidation",
                "event_type": "liquidation",
                "raw_event_id": f"{SOURCE_EXCHANGE}:{sym}:{ts_i}:{side_raw}:{size}:{price}",
                "received_at": int(received_at if received_at is not None else _now_ms())})
        except (TypeError, ValueError):
            rejects.append("parse_error")
    return rows, rejects


# --------------------------------------------------------------------------
# Real event source (websocket-client; apply-only; observably honest)
# --------------------------------------------------------------------------

def _default_event_source(url: str, topics: list[str], max_runtime_seconds: float,
                          max_events: int, diagnostics: dict) -> Iterable[str]:
    assert_safe_ws(url, {})
    topics = assert_safe_topics(topics)
    try:
        import websocket  # websocket-client (declared in requirements.txt)
    except Exception as e:
        raise RuntimeError(f"websocket_client_unavailable:{type(e).__name__}") from e
    ws = websocket.create_connection(url, timeout=min(15, max(5, int(max_runtime_seconds))))
    diagnostics["connected"] = True
    ws.send(json.dumps({"op": "subscribe", "args": topics}))
    start = last_ping = time.time()
    n = 0
    consec_errors = 0
    try:
        ws.settimeout(2)
        while time.time() - start < max_runtime_seconds and n < max_events:
            if time.time() - last_ping > _PING_EVERY_S:      # Bybit keepalive
                try:
                    ws.send(json.dumps({"op": "ping"}))
                except Exception:
                    pass
                last_ping = time.time()
            try:
                msg = ws.recv()
                consec_errors = 0
            except Exception as e:
                k = type(e).__name__
                if k == "WebSocketTimeoutException":
                    diagnostics["recv_timeouts"] = diagnostics.get("recv_timeouts", 0) + 1
                    continue
                # Incident-2 lesson: a dead socket must NOT look like a quiet
                # market -- record it and stop instead of spinning silently.
                diagnostics.setdefault("recv_errors", []).append(k)
                consec_errors += 1
                if consec_errors >= 3:
                    diagnostics["aborted_dead_socket"] = True
                    break
                continue
            if msg:
                diagnostics["frames"] = diagnostics.get("frames", 0) + 1
                n += 1
                yield msg
    finally:
        try:
            ws.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# Plan / collect
# --------------------------------------------------------------------------

def plan() -> dict[str, Any]:
    return {
        "tool_version": TOOL_VERSION,
        "objective": ("capture PUBLIC Bybit linear liquidations as a SEPARATE "
                      "cross-exchange source (design OPTION A: never mixed into "
                      "the Binance sample, never used for READY)"),
        "why": ("Binance futures ws delivers zero frames from this network "
                "(verified by live probes); Bybit public linear ws flows"),
        "ws_url": WS_URL, "topic_format": "allLiquidation.<SYMBOL>",
        "side_semantics": ("OFFICIAL Bybit v5: S is the POSITION side -- Buy => "
                           "long liquidated; Sell => short liquidated (opposite "
                           "of the Binance ORDER-side convention)"),
        "price_semantics": "bankruptcy price (per official docs), labelled price_type",
        "dataset_dir": f"{DEFAULT_STAGING_DIR}/{DATASET_SUBDIR}",
        "default_mode": "DRY_RUN (no network, no writes)",
        "never": ["api_keys", "private_topics", "orders", "db_write", "raw_write",
                  "mixing_into_binance_sample", "invented_READY"],
        "writes_on_plan": False, "uses_network": False, **_safety()}


def _load_seen(dataset_dir: str) -> set[str]:
    try:
        return set(json.loads((Path(dataset_dir) / "_seen.json").read_text(encoding="utf-8")))
    except Exception:
        return set()


def collect(symbols: list[str], apply: bool = False, max_runtime_seconds: float = 5.0,
            max_events: int = 5, output_dir: str | None = None,
            event_source: Iterable[Any] | None = None) -> dict[str, Any]:
    syms = [str(s).strip().upper() for s in (symbols or []) if str(s).strip()] or ["BTCUSDT"]
    topics = [f"allLiquidation.{s}" for s in syms]
    rep: dict[str, Any] = {"tool_version": TOOL_VERSION, "exchange": SOURCE_EXCHANGE,
                           "symbols": syms, "topics": topics, "apply": bool(apply),
                           "max_runtime_seconds": float(max_runtime_seconds),
                           "max_events": int(max_events), "cycle_time": _now_iso(),
                           "errors": [], "rejected": 0, **_safety()}
    try:
        assert_safe_topics(topics)
    except ValueError as e:
        rep["mode"] = "APPLY" if apply else "DRY_RUN"
        rep["writes"] = False
        rep["errors"].append(f"unsafe_topic:{e}")
        return rep
    if not apply:
        rep["mode"] = "DRY_RUN"
        rep["writes"] = False
        rep["note"] = "dry-run: no network, no writes; use --apply to collect"
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

    diagnostics: dict[str, Any] = {"connected": False, "frames": 0}
    rows_new: list[dict] = []
    seen = _load_seen(dataset_dir)
    start = time.time()
    try:
        src = event_source if event_source is not None else _default_event_source(
            WS_URL, topics, max_runtime_seconds, max_events, diagnostics)
        for raw in src:
            if (time.time() - start) > max_runtime_seconds:
                break
            rows, rejects = parse_bybit_all_liquidation(raw, received_at=_now_ms())
            rep["rejected"] += len(rejects)
            for r in rows:
                if r["raw_event_id"] in seen:
                    continue
                seen.add(r["raw_event_id"])
                rows_new.append(r)
                if len(rows_new) >= max_events:
                    break
            if len(rows_new) >= max_events:
                break
    except Exception as e:
        rep["errors"].append(f"liquidations:{type(e).__name__}:{str(e)[:80]}")
    rep["diagnostics"] = diagnostics
    if diagnostics.get("connected") and diagnostics.get("frames", 0) == 0 and not rep["errors"]:
        rep["errors"].append("connected_but_zero_frames:verify_stream_or_quiet_market")

    # append rows (never write an empty file for a 0-row cycle)
    added = 0
    if rows_new:
        path = os.path.join(dataset_dir, "liquidations.csv").replace("\\", "/")
        new_file = not os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CANON_HEADER)
            if new_file:
                w.writeheader()
            for r in rows_new:
                w.writerow({c: r.get(c, "") for c in CANON_HEADER})
        added = len(rows_new)
        keep = list(seen)[-_SEEN_CAP:]
        (Path(dataset_dir) / "_seen.json").write_text(json.dumps(keep), encoding="utf-8")
    rep["added"] = added
    rep["writes"] = added > 0

    # cumulative manifest + checkpoint (state saved every cycle)
    man_path = Path(dataset_dir) / "manifest.json"
    try:
        prev = json.loads(man_path.read_text(encoding="utf-8"))
    except Exception:
        prev = {}
    cumulative = int(prev.get("cumulative_rows", 0)) + added
    last_event_ts = max([r["timestamp"] for r in rows_new], default=prev.get("last_event_ts"))
    manifest = {"tool_version": TOOL_VERSION, "source_exchange": SOURCE_EXCHANGE,
                "first_cycle": prev.get("first_cycle", rep["cycle_time"]),
                "last_cycle": rep["cycle_time"], "cycles": int(prev.get("cycles", 0)) + 1,
                "symbols": syms, "cumulative_rows": cumulative,
                "last_event_ts": last_event_ts,
                "errors_last_cycle": rep["errors"],
                "side_mapping_verified": SIDE_MAPPING_VERIFIED,
                "cross_exchange_liquidations_used_for_ready": CROSS_EXCHANGE_USED_FOR_READY,
                "note": ("SEPARATE cross-exchange source; never merged into the "
                         "Binance sample; never produces READY"),
                "research_only": True, "shadow_only": True, "live_ready": False,
                "can_send_real_orders": False,
                "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}
    man_path.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    (Path(dataset_dir) / "checkpoint.json").write_text(json.dumps({
        "last_run_at": rep["cycle_time"], "rows_total": cumulative,
        "seen_count": len(seen), "last_event_ts": last_event_ts}, default=str),
        encoding="utf-8")
    rep["manifest"] = str(man_path).replace("\\", "/")
    rep["cumulative_rows"] = cumulative
    return rep


# --------------------------------------------------------------------------
# Read-only status for the V10.29 dashboard/gap report (OPTION A: informative)
# --------------------------------------------------------------------------

def alt_liquidations_status() -> dict[str, Any]:
    """Read-only summary of the Bybit alternative source. NEVER contributes to
    MICROSTRUCTURE_RESEARCH_READY (cross_exchange_liquidations_used_for_ready
    is hardcoded False by design OPTION A)."""
    out: dict[str, Any] = {
        "alternative_liquidations_source": SOURCE_EXCHANGE,
        "cross_exchange_liquidations_available": False,
        "cross_exchange_liquidations_used_for_ready": CROSS_EXCHANGE_USED_FOR_READY,
        "bybit_liquidations_rows": 0, "bybit_liquidations_last_event": None,
        "bybit_liquidations_errors": [], "side_mapping_verified": SIDE_MAPPING_VERIFIED,
        "warning": ("Bybit liquidations are alternative cross-exchange observations, "
                    "not Binance-native liquidations")}
    man = (_repo_root() / "external_data" / "staging" / STAGING_MARKER
           / DATASET_SUBDIR / "manifest.json")
    if man.is_file() and not man.is_symlink():
        try:
            m = json.loads(man.read_text(encoding="utf-8"))
            out["bybit_liquidations_rows"] = int(m.get("cumulative_rows") or 0)
            out["bybit_liquidations_last_event"] = m.get("last_event_ts")
            out["bybit_liquidations_errors"] = [str(e) for e in
                                                (m.get("errors_last_cycle") or [])][:5]
            out["cross_exchange_liquidations_available"] = out["bybit_liquidations_rows"] > 0
        except Exception:
            pass
    return out
