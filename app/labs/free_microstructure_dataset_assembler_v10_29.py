"""ResearchOps V10.29 - Free Microstructure Dataset Assembler + Readiness Monitor.

Merge everything the free collectors have accumulated (V10.25 run dirs, V10.26
liquidation run dirs, V10.27 continuous dataset) into ONE single-symbol sample
that the V10.24.3 validator can judge, and report EXACTLY what is still missing
to reach MICROSTRUCTURE_RESEARCH_READY -- with honest, clearly-rough estimates
of the days remaining at the observed accumulation rate.

Rules: offline module (NO network at all), dry-run by default, --apply required
to write, staging-only writes under the v10_29 marker, read-only access limited
to the three known source markers, dedup per kind, sort by timestamp, one symbol
per sample (multi-symbol CSVs are INVALID in V10.24.3), never invent missing
data, never invent READY -- the verdict comes only from the V10.24.3 validator.

NO keys, NO auth, NO DB, NO orders, NO live, NO paper. FINAL: NO LIVE.
"""

from __future__ import annotations

import csv
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import FINAL_RECOMMENDATION_NO_LIVE
from . import microstructure_sample_adapter_v10_24 as V24
from . import free_public_microstructure_collector_v10_25 as V25
from . import free_public_liquidations_ws_collector_v10_26 as V26
from . import continuous_forward_collection_v10_27 as V27

TOOL_VERSION = "v10.29"
STAGING_MARKER = "free_microstructure_samples_v10_29"
DEFAULT_STAGING_DIR = f"external_data/staging/{STAGING_MARKER}"

KINDS = ("trades", "orderbook", "oi", "funding", "liquidations")
_FILES = dict(V27._FILES)          # identical canonical filenames -- no schema drift
_HEADERS = dict(V27._HEADERS)      # identical canonical headers
_MAX_ROWS_PER_FILE = 1_500_000     # hard bound against corrupt/huge inputs

SOURCE_MARKERS = {
    "v10_25_forward_runs": V25.STAGING_MARKER,
    "v10_26_liquidation_runs": V26.STAGING_MARKER,
    "v10_27_continuous_dataset": V27.STAGING_MARKER,
}
_ALL_READ_MARKERS = tuple(SOURCE_MARKERS.values()) + (STAGING_MARKER,)

_FORBIDDEN_SEG = V26._FORBIDDEN_SEG
_FORBIDDEN_SUF = V26._FORBIDDEN_SUF


def _safety() -> dict[str, Any]:
    return {"research_only": True, "shadow_only": True, "paper_ready": False,
            "live_ready": False, "can_send_real_orders": False,
            "paper_filter_enabled": False, "paper_candidate_future": False,
            "makes_no_trades": True, "uses_api_keys": False, "uses_db": False,
            "uses_network": False, "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# --------------------------------------------------------------------------
# Path safety (hardened: traversal / forbidden segments / symlinks / escape)
# --------------------------------------------------------------------------

def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _check_segments(rel: str) -> list[str]:
    segs = [s for s in str(rel).replace("\\", "/").split("/") if s]
    if ".." in segs:
        raise ValueError("traversal blocked")
    for s in (x.lower() for x in segs):
        if s in _FORBIDDEN_SEG or s.endswith(_FORBIDDEN_SUF) or ".env" in s:
            raise ValueError(f"forbidden segment: {s}")
    return segs


def _resolve_under_marker(rel: str, markers: tuple[str, ...]) -> Path:
    """Common fail-closed resolver: rel must live inside
    external_data/staging/<one-of-markers> of THIS repo, with no symlinked
    component and no resolve() escape."""
    _check_segments(rel)
    repo = _repo_root()
    target = Path(rel)
    if not target.is_absolute():
        target = repo / target
    target = Path(os.path.normpath(str(target)))
    ok = False
    for m in markers:
        logical = repo / "external_data" / "staging" / m
        if target == logical or logical in target.parents:
            ok = True
            break
    if not ok:
        raise ValueError(f"dir must be inside external_data/staging/{{{','.join(markers)}}}")
    for anc in [target, *target.parents]:
        if anc == repo or anc in repo.parents:
            break
        try:
            if anc.exists() and anc.is_symlink():
                raise ValueError(f"symlinked component blocked: {anc}")
        except OSError:
            break
    rtgt = target.resolve(strict=False)
    if not (rtgt == repo or _is_within(rtgt, repo)):
        raise ValueError("dir resolves outside the repo")
    return target


def safe_staging_dir(base: str | None = None) -> Path:
    """WRITE-side gate: only under the v10_29 marker."""
    return _resolve_under_marker(base or DEFAULT_STAGING_DIR, (STAGING_MARKER,))


def safe_read_dir(rel: str) -> Path:
    """READ-side gate: only the known free-data staging markers."""
    return _resolve_under_marker(rel, _ALL_READ_MARKERS)


# --------------------------------------------------------------------------
# Source discovery + CSV reading (read-only, header-strict, bounded)
# --------------------------------------------------------------------------

def discover_sources() -> dict[str, Any]:
    """List candidate source dirs holding canonical CSVs (read-only)."""
    repo = _repo_root()
    out: dict[str, Any] = {}
    for name, marker in SOURCE_MARKERS.items():
        root = repo / "external_data" / "staging" / marker
        entry: dict[str, Any] = {"marker": marker, "exists": root.is_dir(), "dirs": []}
        if root.is_dir():
            if name == "v10_27_continuous_dataset":
                ds = root / V27.DATASET_SUBDIR
                if ds.is_dir():
                    entry["dirs"].append(str(ds.relative_to(repo)).replace("\\", "/"))
            else:
                for child in sorted(root.iterdir()):
                    if child.is_dir() and not child.is_symlink():
                        entry["dirs"].append(str(child.relative_to(repo)).replace("\\", "/"))
        out[name] = entry
    return out


def _read_kind_rows(dir_rel: str, kind: str) -> tuple[list[dict], list[str]]:
    """Read one canonical CSV; header must match EXACTLY; bounded; never raises."""
    errors: list[str] = []
    try:
        base = safe_read_dir(dir_rel)
    except ValueError as e:
        return [], [f"unsafe_source_dir:{e}"]
    path = base / _FILES[kind]
    if not path.is_file():
        return [], []
    try:
        if path.is_symlink():
            return [], [f"symlinked_source_file:{_FILES[kind]}"]
        with open(path, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if list(reader.fieldnames or []) != list(_HEADERS[kind]):
                return [], [f"header_mismatch:{dir_rel}/{_FILES[kind]}"]
            rows = []
            for i, r in enumerate(reader):
                if i >= _MAX_ROWS_PER_FILE:
                    errors.append(f"truncated_at_cap:{dir_rel}/{_FILES[kind]}")
                    break
                rows.append(r)
            return rows, errors
    except Exception as e:
        return [], [f"read_error:{dir_rel}/{_FILES[kind]}:{type(e).__name__}"]


# --------------------------------------------------------------------------
# Plan
# --------------------------------------------------------------------------

def plan() -> dict[str, Any]:
    return {
        "tool_version": TOOL_VERSION,
        "objective": ("merge V10.25/V10.26/V10.27 free collector outputs into ONE "
                      "single-symbol V10.24.3-compatible sample and report exactly "
                      "what is missing for MICROSTRUCTURE_RESEARCH_READY"),
        "default_mode": "DRY_RUN (reads sources, writes NOTHING)",
        "kinds": list(KINDS),
        "sources": {k: f"external_data/staging/{m}" for k, m in SOURCE_MARKERS.items()},
        "output": f"{DEFAULT_STAGING_DIR}/<run_id>/",
        "rules": ["one symbol per assembled sample (multi-symbol CSV is INVALID)",
                  "dedup per kind (trades keyed on ts+price+size+side)",
                  "rows sorted by timestamp", "0-row kinds are gaps, never empty files",
                  "verdict ONLY from the V10.24.3 validator -- never invented"],
        "never": ["network", "api_keys", "db_write", "raw_write", "orders",
                  "paper_or_live_promotion", "invented_data", "invented_READY"],
        "writes_on_plan": False, **_safety()}


# --------------------------------------------------------------------------
# Assemble
# --------------------------------------------------------------------------

def assemble(symbol: str, apply: bool = False, output_dir: str | None = None) -> dict[str, Any]:
    symbol = str(symbol or "").strip().upper()
    rep: dict[str, Any] = {"tool_version": TOOL_VERSION, "symbol": symbol,
                           "apply": bool(apply), "assembled_at": _now_iso(),
                           "per_kind": {}, "gaps": [], "errors": [], **_safety()}
    if not symbol:
        rep["mode"] = "DRY_RUN" if not apply else "APPLY"
        rep["writes"] = False
        rep["errors"].append("symbol_required(one symbol per assembled sample)")
        return rep

    # APPLY: validate the write target BEFORE reading anything
    out_base: Path | None = None
    if apply:
        rep["mode"] = "APPLY"
        try:
            out_base = safe_staging_dir(output_dir)
        except ValueError as e:
            rep["errors"].append(f"unsafe_output_dir:{e}")
            rep["writes"] = False
            return rep
    else:
        rep["mode"] = "DRY_RUN"

    sources = discover_sources()
    rep["sources"] = sources
    merged: dict[str, list[dict]] = {}
    for kind in KINDS:
        seen: set[str] = set()
        rows_out: list[dict] = []
        raw_total = dropped_sym = dropped_ts = dup = 0
        src_files = 0
        for entry in sources.values():
            for d in entry["dirs"]:
                rows, errs = _read_kind_rows(d, kind)
                rep["errors"].extend(errs)
                if rows:
                    src_files += 1
                for r in rows:
                    raw_total += 1
                    if str(r.get("symbol", "")).strip().upper() != symbol:
                        dropped_sym += 1
                        continue
                    try:
                        ts = int(float(r.get("timestamp")))
                    except (TypeError, ValueError):
                        dropped_ts += 1
                        continue
                    k = V27._dedup_key(kind, r)
                    if k in seen:
                        dup += 1
                        continue
                    seen.add(k)
                    r["timestamp"] = ts
                    rows_out.append(r)
        rows_out.sort(key=lambda r: int(r["timestamp"]))
        merged[kind] = rows_out
        rep["per_kind"][kind] = {
            "source_files": src_files, "raw_rows": raw_total,
            "unique_rows": len(rows_out), "duplicates_dropped": dup,
            "other_symbol_dropped": dropped_sym, "bad_timestamp_dropped": dropped_ts,
            "first_ts": rows_out[0]["timestamp"] if rows_out else None,
            "last_ts": rows_out[-1]["timestamp"] if rows_out else None}
        if not rows_out:
            rep["gaps"].append(f"missing_{kind}")

    if not apply:
        rep["writes"] = False
        rep["note"] = "dry-run: counts only; use --apply to write the assembled sample"
        return rep

    run_id = _run_id()
    out_dir = out_base / run_id
    os.makedirs(out_dir, exist_ok=True)
    rep["sample_dir"] = str(out_dir).replace("\\", "/")
    rep["run_id"] = run_id
    written = {}
    for kind in KINDS:
        rows = merged[kind]
        if not rows:            # V10.27.1 lesson: an empty recognized CSV is INVALID
            continue
        header = _HEADERS[kind]
        path = out_dir / _FILES[kind]
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=header)
            w.writeheader()
            for r in rows:
                w.writerow({c: r.get(c, "") for c in header})
        written[kind] = len(rows)
    rep["written"] = written
    rep["writes"] = bool(written)

    manifest = {"tool_version": TOOL_VERSION, "run_id": run_id, "symbol": symbol,
                "assembled_at": rep["assembled_at"], "per_kind": rep["per_kind"],
                "gaps": rep["gaps"], "errors": rep["errors"],
                "sources": {k: v["dirs"] for k, v in sources.items()},
                "research_only": True, "shadow_only": True, "live_ready": False,
                "can_send_real_orders": False,
                "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    # honest verdict: only what the V10.24.3 validator says
    vr = V24.validate_sample(str(out_dir))
    cls = vr.get("classification", {})
    rep["readiness_verdict"] = cls.get("verdict")
    rep["active_gaps"] = cls.get("active_gaps")
    rep["why_not_ready"] = cls.get("why_not_ready")
    rep["can_research_microstructure"] = cls.get("can_research_microstructure")
    return rep


# --------------------------------------------------------------------------
# Readiness status + gap report (verdict is NEVER invented here)
# --------------------------------------------------------------------------

def _latest_assembled_dir() -> str | None:
    root = _repo_root() / "external_data" / "staging" / STAGING_MARKER
    if not root.is_dir():
        return None
    runs = sorted((d for d in root.iterdir() if d.is_dir() and not d.is_symlink()),
                  key=lambda d: d.name)
    if not runs:
        return None
    return str(runs[-1].relative_to(_repo_root())).replace("\\", "/")


def _pick_target(sample_dir: str | None) -> tuple[str | None, str]:
    if sample_dir:
        return sample_dir, "explicit"
    latest = _latest_assembled_dir()
    if latest:
        return latest, "latest_assembled_v10_29"
    ds = f"{V27.DEFAULT_STAGING_DIR}/{V27.DATASET_SUBDIR}"
    if (_repo_root() / ds).is_dir():
        return ds, "v10_27_continuous_dataset_fallback"
    return None, "none"


def readiness_status(sample_dir: str | None = None) -> dict[str, Any]:
    rep: dict[str, Any] = {"tool_version": TOOL_VERSION, "checked_at": _now_iso(), **_safety()}
    target, how = _pick_target(sample_dir)
    rep["target_selection"] = how
    if target is None:
        rep["readiness_verdict"] = V24.C_NO_SAMPLE
        rep["note"] = "no assembled sample and no V10.27 dataset yet"
        return rep
    try:
        tdir = safe_read_dir(target)
    except ValueError as e:
        rep["error"] = f"unsafe_sample_dir:{e}"
        return rep
    rep["sample_dir"] = str(target).replace("\\", "/")
    vr = V24.validate_sample(str(tdir))
    cls = vr.get("classification", {})
    rep["readiness_verdict"] = cls.get("verdict")
    rep["active_gaps"] = cls.get("active_gaps")
    rep["critical_errors"] = (cls.get("critical_errors") or [])[:20]
    rep["why_not_ready"] = cls.get("why_not_ready")
    rep["valid_types"] = cls.get("valid_types")
    rep["density_ok"] = cls.get("density_ok")
    rep["can_research_microstructure"] = cls.get("can_research_microstructure")
    rep["symbols_by_type"] = cls.get("symbols_by_type")

    required = ("trades", "orderbook", "oi", "liquidations")
    by_type = vr.get("by_type", {})
    detail: dict[str, Any] = {}
    for kind in KINDS:
        m = by_type.get(kind) or {}
        cov = m.get("coverage", {}) or {}
        rows = int(cov.get("rows") or 0)
        cdays = float(cov.get("coverage_days") or 0.0)
        rate = (rows / cdays) if cdays > 0 else None
        floor_rows = V24._MIN_ROWS.get(kind)
        floor_rpd = V24._MIN_ROWS_PER_DAY.get(kind)
        is_required = kind in required
        rows_needed = max(0, (floor_rows or 0) - rows) if is_required else 0
        days_needed_hist = max(0.0, V24.MIN_HISTORY_DAYS - cdays) if is_required else 0.0
        days_for_rows = (rows_needed / rate) if (rows_needed > 0 and rate and rate > 0) else (
            0.0 if rows_needed == 0 else None)
        est = None
        if is_required:
            if days_for_rows is None:
                est = None          # no observed rate -> honestly unknown
            else:
                est = round(max(days_for_rows, days_needed_hist), 1)
        detail[kind] = {
            "required_for_ready": is_required, "valid": bool(m.get("valid")),
            "rows": rows, "min_rows": floor_rows,
            "coverage_days": round(cdays, 2), "min_coverage_days": V24.MIN_HISTORY_DAYS if is_required else None,
            "rows_per_day_observed": round(rate, 2) if rate else None,
            "min_rows_per_day": floor_rpd,
            "first_ts": cov.get("first_ts"), "last_ts": cov.get("last_ts"),
            "rows_still_needed": rows_needed,
            "history_days_still_needed": round(days_needed_hist, 1),
            "estimated_days_remaining": est,
            "estimate_is_rough": True}
    rep["per_kind"] = detail
    ests = [d["estimated_days_remaining"] for d in detail.values()
            if d["required_for_ready"] and d["estimated_days_remaining"] is not None]
    unknown = [k for k, d in detail.items()
               if d["required_for_ready"] and d["estimated_days_remaining"] is None]
    rep["estimated_days_to_ready"] = (max(ests) if ests and not unknown else None)
    rep["estimate_unknown_for"] = unknown
    rep["estimate_note"] = ("rough forward extrapolation of the observed accumulation "
                            "rate; NOT a promise and NOT an edge")
    return rep


def gap_report(sample_dir: str | None = None) -> dict[str, Any]:
    st = readiness_status(sample_dir)
    rep: dict[str, Any] = {"tool_version": TOOL_VERSION, "checked_at": st["checked_at"],
                           "sample_dir": st.get("sample_dir"),
                           "readiness_verdict": st.get("readiness_verdict"),
                           "gaps": [], "actions": [], **_safety()}
    if "error" in st:
        rep["error"] = st["error"]
        return rep
    if st.get("readiness_verdict") == V24.C_NO_SAMPLE:
        rep["gaps"].append("no data at all: run the V10.27 collector with --apply first")
        rep["actions"].append("python -m app.research_lab continuous-collection-run-cycle-v1027 "
                              "--symbols BTCUSDT --apply")
        return rep
    for kind, d in (st.get("per_kind") or {}).items():
        if not d["required_for_ready"]:
            if d["rows"] == 0:
                rep["gaps"].append(f"{kind}: optional, currently absent")
            continue
        if d["rows"] == 0:
            rep["gaps"].append(f"{kind}: MISSING entirely (file absent or 0 usable rows)")
        else:
            probs = []
            if d["min_rows"] and d["rows"] < d["min_rows"]:
                probs.append(f"rows {d['rows']}/{d['min_rows']}")
            if d["history_days_still_needed"] > 0:
                probs.append(f"coverage {d['coverage_days']}/{d['min_coverage_days']} days")
            if (d["min_rows_per_day"] and d["rows_per_day_observed"] is not None
                    and d["rows_per_day_observed"] < d["min_rows_per_day"]):
                probs.append(f"density {d['rows_per_day_observed']}/{d['min_rows_per_day']} rows/day")
            if not d["valid"]:
                probs.append("file INVALID under V10.24.3")
            if probs:
                eta = d["estimated_days_remaining"]
                rep["gaps"].append(f"{kind}: " + "; ".join(probs)
                                   + (f"; ~{eta} days at current rate (rough)" if eta is not None else ""))
    for e in (st.get("critical_errors") or [])[:8]:
        rep["gaps"].append(f"critical: {e}")
    if not rep["gaps"]:
        rep["gaps"].append("none reported by V10.24.3 -- check readiness_verdict")
    rep["actions"].extend([
        "keep the continuous collector looping: continuous-collection-run-cycle-v1027 "
        "--symbols BTCUSDT --apply (V10.27.2 now also accumulates trades)",
        "watch progress: continuous-collection-status-v1027",
        "re-assemble + validate: free-microstructure-assemble-sample-v1029 "
        "--symbols BTCUSDT --apply, then free-microstructure-readiness-status-v1029"])
    rep["honesty"] = ("READY means enough clean DATA to start microstructure research; "
                      "it does NOT mean an edge exists")
    return rep
