"""ResearchOps V10.27 - Continuous Forward Collection Runner (research only).

Leave this running for days/weeks to ACCUMULATE free forward microstructure into
one growing, deduplicated, V10.24.3-compatible dataset:
  - trades        (Binance aggTrades REST, via V10.25) [V10.27.2: required for READY]
  - liquidations  (Binance forceOrder websocket, via V10.26)
  - orderbook L1  (Binance bookTicker REST snapshot, via V10.25)
  - open interest (Binance metrics REST, via V10.25)
  - funding       (Binance fundingRate REST, via V10.25)

It reuses the already-hardened safety primitives of V10.25/V10.26 (exact GET/WS
allowlists, no auth, no keys, no private channels) and adds: a persistent
append-only dataset, cross-restart deduplication, a cumulative manifest/checkpoint,
and a `status` that runs the V10.24.3 validator so you can watch readiness grow.

Public network only, NO API keys, NO DB, NO raw/prod writes, NO orders, dry-run
by default, staging-only writes, bounded per cycle. FINAL_RECOMMENDATION: NO LIVE.
"""

from __future__ import annotations

import csv
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from . import FINAL_RECOMMENDATION_NO_LIVE
from . import free_public_microstructure_collector_v10_25 as V25
from . import free_public_liquidations_ws_collector_v10_26 as V26
from . import microstructure_sample_adapter_v10_24 as V24

TOOL_VERSION = "v10.27"
STAGING_MARKER = "continuous_forward_v10_27"
DEFAULT_STAGING_DIR = f"external_data/staging/{STAGING_MARKER}"
DATASET_SUBDIR = "dataset"          # single growing dataset (NOT per-run)
# V10.27.2: trades added -- V10.24.3 requires trades>=1000 for READY, so a
# runner without trades could NEVER reach MICROSTRUCTURE_RESEARCH_READY.
KINDS = ("trades", "liquidations", "orderbook", "oi", "funding")
_SEEN_CAP = 250_000                  # bound persisted dedup memory

_FORBIDDEN_SEG = V26._FORBIDDEN_SEG
_FORBIDDEN_SUF = V26._FORBIDDEN_SUF

# canonical headers (reuse the exact V10.24.3-compatible schemas)
_HEADERS = {
    "trades": V25._CANON["trades"][1],
    "liquidations": V26.CANON_HEADER,
    "orderbook": V25._CANON["orderbook"][1],
    "oi": V25._CANON["oi"][1],
    "funding": V25._CANON["funding"][1],
}
_FILES = {
    "trades": "trades.csv",
    "liquidations": "liquidations.csv",
    "orderbook": "orderbook_l2.csv",
    "oi": "open_interest.csv",
    "funding": "funding.csv",
}


def _safety() -> dict[str, Any]:
    return {"research_only": True, "shadow_only": True, "paper_ready": False,
            "live_ready": False, "can_send_real_orders": False,
            "paper_filter_enabled": False, "paper_candidate_future": False,
            "makes_no_trades": True, "uses_api_keys": False, "uses_db": False,
            "subscribes_private_channels": False,
            "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
# Persistent dedup + append
# --------------------------------------------------------------------------

def _dedup_key(kind: str, row: dict) -> str:
    if kind == "liquidations":
        return str(row.get("raw_event_id"))
    if kind == "trades":
        # canonical trades carry no unique id; same-ms trades at different
        # price/size/side are legitimate distinct events -- key on all of them.
        return (f"{row.get('symbol')}:{row.get('timestamp')}:{row.get('price')}:"
                f"{row.get('size')}:{row.get('aggressor_side')}")
    return f"{row.get('symbol')}:{row.get('timestamp')}"


def _load_seen(dataset_dir: str, kind: str) -> set[str]:
    p = os.path.join(dataset_dir, f"_seen_{kind}.json")
    try:
        return set(json.loads(Path(p).read_text(encoding="utf-8")))
    except Exception:
        return set()


def _save_seen(dataset_dir: str, kind: str, seen: set[str]) -> None:
    keep = list(seen)[-_SEEN_CAP:]
    Path(os.path.join(dataset_dir, f"_seen_{kind}.json")).write_text(
        json.dumps(keep), encoding="utf-8")


def _append_rows(dataset_dir: str, kind: str, rows: list[dict], seen: set[str]) -> int:
    if kind not in _HEADERS:
        return 0
    # filter to genuinely NEW rows first; never create a header-only (empty) file
    # for a type with no data yet -- an empty recognized CSV is INVALID in V10.24.3.
    new_rows = []
    for r in rows:
        k = _dedup_key(kind, r)
        if k in seen:
            continue
        seen.add(k)
        new_rows.append(r)
    if not new_rows:
        return 0
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
# Plan
# --------------------------------------------------------------------------

def plan() -> dict[str, Any]:
    return {
        "tool_version": TOOL_VERSION,
        "objective": "accumulate free forward microstructure into one growing V10.24.3 dataset",
        "default_mode": "DRY_RUN (no network, no writes)",
        "kinds": list(KINDS),
        "dataset_dir": f"{DEFAULT_STAGING_DIR}/{DATASET_SUBDIR}",
        "how_to_run_for_weeks": [
            "Run one bounded cycle repeatedly (cron/loop): "
            "continuous-collection-run-cycle-v1027 --symbols BTCUSDT --apply "
            "--max-runtime-seconds 3600 --max-events 100000",
            "Each cycle appends + dedups into the SAME dataset, so density grows across runs.",
            "Check progress: continuous-collection-status-v1027 (runs the V10.24.3 validator).",
        ],
        "honest_summary": (
            "Forward-only accumulation. Liquidations/orderbook-L1 are sparse-to-moderate; "
            "expect weeks before density gates pass. This builds DATA, not an edge, and "
            "never reaches READY instantly."),
        "reuses": ["V10.26 forceOrder ws (liquidations)",
                   "V10.25 REST aggTrades/bookTicker/oi/funding",
                   "V10.25/26 exact allowlists + hardened staging", "V10.24.3 validator"],
        "never": ["api_keys", "auth_headers", "private_channels", "orders", "db_write",
                  "raw_write", "paid_provider", "paper_or_live_promotion"],
        "writes_on_plan": False, **_safety()}


# --------------------------------------------------------------------------
# One bounded collection cycle
# --------------------------------------------------------------------------

def _rest_batch(symbol: str, kind: str, transport: Callable) -> list[dict]:
    urls = V25._planned_urls(symbol)
    hdr = {"User-Agent": "researchops/1.0", "Accept": "application/json"}
    raw = transport(urls[kind], hdr)
    data = json.loads(raw)
    if kind == "trades":
        return V25.aggtrades_to_canonical(data, symbol)
    if kind == "orderbook":
        return V25.bookticker_to_canonical(data if isinstance(data, list) else [data], symbol)
    if kind == "oi":
        return V25.oi_to_canonical(data, symbol)
    if kind == "funding":
        return V25.funding_to_canonical(data, symbol)
    return []


def run_cycle(exchange: str, symbols: list[str], kinds: list[str], apply: bool = False,
              max_runtime_seconds: float = 60.0, max_events: int = 10_000,
              output_dir: str | None = None,
              liq_event_source: Iterable[Any] | None = None,
              rest_transport: Callable | None = None) -> dict[str, Any]:
    exchange = (exchange or "binance_usdm").lower()
    kinds = [k for k in (kinds or list(KINDS)) if k in KINDS]
    rep: dict[str, Any] = {"tool_version": TOOL_VERSION, "exchange": exchange,
                           "symbols": symbols, "kinds": kinds, "apply": bool(apply),
                           "max_runtime_seconds": float(max_runtime_seconds),
                           "max_events": int(max_events), "added": {}, "errors": [],
                           "cycle_time": _now_iso(),
                           "note": "forward-only; run repeatedly for weeks to build density",
                           **_safety()}
    if not apply:
        rep["mode"] = "DRY_RUN"
        rep["writes"] = False
        return rep
    rep["mode"] = "APPLY"
    if exchange != "binance_usdm":
        rep["errors"].append(f"exchange_not_implemented:{exchange}")
        rep["writes"] = False
        return rep
    try:
        staging = safe_staging_dir(output_dir)
    except ValueError as e:
        rep["errors"].append(f"unsafe_output_dir:{e}")
        rep["writes"] = False
        return rep
    dataset_dir = os.path.join(staging, DATASET_SUBDIR).replace("\\", "/")
    os.makedirs(dataset_dir, exist_ok=True)
    rep["dataset_dir"] = dataset_dir
    start = time.time()
    transport = rest_transport or V25.default_transport

    # liquidations (websocket) -- bounded
    if "liquidations" in kinds:
        try:
            seen = _load_seen(dataset_dir, "liquidations")
            url = V26._binance_ws_url(symbols)
            V26.assert_safe_ws(url, {})
            src = liq_event_source if liq_event_source is not None else V26._default_event_source(
                url, max_runtime_seconds, max_events)
            rows = []
            for raw in src:
                if (time.time() - start) > max_runtime_seconds or len(rows) >= max_events:
                    break
                row, _why = V26._parse_event(exchange, raw, received_at=int(time.time() * 1000))
                if row is not None:
                    rows.append(row)
            rep["added"]["liquidations"] = _append_rows(dataset_dir, "liquidations", rows, seen)
            _save_seen(dataset_dir, "liquidations", seen)
        except Exception as e:
            rep["errors"].append(f"liquidations:{type(e).__name__}:{str(e)[:80]}")

    # trades / orderbook L1 / oi / funding (REST snapshots/history) -- one batch per symbol
    for kind in ("trades", "orderbook", "oi", "funding"):
        if kind not in kinds:
            continue
        seen = _load_seen(dataset_dir, kind)
        total = 0
        for sym in symbols:
            try:
                rows = _rest_batch(sym, kind, transport)
                total += _append_rows(dataset_dir, kind, rows, seen)
                time.sleep(0.0)
            except Exception as e:
                rep["errors"].append(f"{kind}:{sym}:{type(e).__name__}:{str(e)[:60]}")
        _save_seen(dataset_dir, kind, seen)
        rep["added"][kind] = total

    rep["writes"] = True
    rep["manifest"] = _write_manifest(dataset_dir, rep)
    return rep


def _write_manifest(dataset_dir: str, rep: dict[str, Any]) -> str:
    path = os.path.join(dataset_dir, "manifest.json").replace("\\", "/")
    prev = {}
    try:
        prev = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        prev = {}
    cumulative = dict(prev.get("cumulative_added", {}))
    for k, v in rep.get("added", {}).items():
        cumulative[k] = int(cumulative.get(k, 0)) + int(v)
    manifest = {
        "tool_version": TOOL_VERSION, "first_cycle": prev.get("first_cycle", rep["cycle_time"]),
        "last_cycle": rep["cycle_time"], "cycles": int(prev.get("cycles", 0)) + 1,
        "exchange": rep["exchange"], "symbols": rep["symbols"], "kinds": rep["kinds"],
        "this_cycle_added": rep.get("added", {}), "cumulative_added": cumulative,
        "errors_last_cycle": rep.get("errors", []),
        "notes": "forward-only continuous accumulation; validate readiness with "
                 "microstructure-sample-validate-v1024 --sample-dir <dataset_dir>.",
        "research_only": True, "shadow_only": True, "live_ready": False,
        "can_send_real_orders": False, "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}
    Path(path).write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# Status (readiness toward MICROSTRUCTURE_RESEARCH_READY)
# --------------------------------------------------------------------------

def status(output_dir: str | None = None) -> dict[str, Any]:
    rep: dict[str, Any] = {"tool_version": TOOL_VERSION, "checked_at": _now_iso(), **_safety()}
    try:
        staging = safe_staging_dir(output_dir)
    except ValueError as e:
        rep["error"] = f"unsafe_output_dir:{e}"
        return rep
    dataset_dir = os.path.join(staging, DATASET_SUBDIR).replace("\\", "/")
    rep["dataset_dir"] = dataset_dir
    man_path = os.path.join(dataset_dir, "manifest.json")
    if os.path.isfile(man_path):
        try:
            man = json.loads(Path(man_path).read_text(encoding="utf-8"))
            rep["cumulative_added"] = man.get("cumulative_added", {})
            rep["cycles"] = man.get("cycles")
            rep["first_cycle"] = man.get("first_cycle")
            rep["last_cycle"] = man.get("last_cycle")
        except Exception:
            rep["manifest_error"] = True
    if not os.path.isdir(dataset_dir):
        rep["readiness_verdict"] = V24.C_NO_SAMPLE
        rep["note"] = "no dataset yet -- run a cycle with --apply first"
        return rep
    vr = V24.validate_sample(dataset_dir)
    cls = vr.get("classification", {})
    rep["readiness_verdict"] = cls.get("verdict")
    rep["active_gaps"] = cls.get("active_gaps")
    rep["valid_types"] = cls.get("valid_types")
    rep["density_ok"] = cls.get("density_ok")
    rep["why_not_ready"] = cls.get("why_not_ready")
    rep["can_research_microstructure"] = cls.get("can_research_microstructure")
    return rep
