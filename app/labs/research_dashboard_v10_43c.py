"""ResearchOps V10.43C - Persistent WS research dashboard (static/local only).

Adds the V10.43C persistent WS health/continuity layer, the 3-way REST vs WS vs
WS-persistent comparison, hardened strategy lab status and exit optimization.
Everything is local/static/reporting-only: no network, no orders, no keys, NO LIVE.
"""

from __future__ import annotations

import html
import json
import os
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import FINAL_RECOMMENDATION_NO_LIVE
from . import continuous_edge_factory_v10_38 as CE
from . import research_dashboard_v10_43a as A
from . import ws_continuity_v10_43c as PWS
from . import strategy_lab_hardening_v10_43c as HARD
from . import exit_optimization_v10_43b as EXIT
from . import autonomous_strategy_lab_v10_43b as LAB

TOOL_VERSION = "v10.43c"
OUTPUT_SUBDIR = ("reports", "research", "dashboard_v10_43c")
STATUS_FILE = "dashboard_watch_status_v1043c.json"
LOG_FILE = "dashboard_watch_v1043c.log"
LOCK_FILE = "dashboard_watch_v1043c.lock"
DEFAULT_REFRESH_SECONDS = 30
MIN_REFRESH_SECONDS = 15
SLOW_STALE_SECONDS = 15 * 60
SLOW_SOURCE_REFRESH_SECONDS = 180
CACHE_FILE = "dashboard_fast_cache_v1043c.json"


def _safety() -> dict[str, Any]:
    return {"research_only": True, "shadow_only": True, "can_send_real_orders": False,
            "paper_filter_enabled": False, "edge_validated": False,
            "not_actionable": True, "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}


def gather_state(symbol: str = "BTCUSDT") -> dict[str, Any]:
    base = A.gather_state(symbol)
    try:
        health = PWS.ws_persistent_health(symbol)
    except Exception:
        health = {"status": "NO_DATA"}
    try:
        continuity = PWS.ws_continuity_audit(symbol)
    except Exception:
        continuity = {"verdict": "NO_WS_DATA", "max_contiguous_run": 0}
    try:
        compare = PWS.dataset_source_compare_3way(symbol)
    except Exception:
        compare = {"recommended_source": "rest", "blockers": ["COMPARE_FAILED"],
                   "rest": {}, "ws": {}, "ws_persistent": {}}
    try:
        strategy = HARD.run_hardened_lab(symbol, data_source="ws_persistent", write_reports=True)
    except Exception as e:
        strategy = {"global_verdict": "ERROR", "detail": str(e)[:160], "candidates_generated": 0}
    try:
        tournament = LAB.run_ws_tournament(symbol, source="ws_persistent", write_reports=True)
    except Exception as e:
        tournament = {"verdict": "ERROR", "detail": str(e)[:160], "micro_live_ready": False}
    try:
        exits = EXIT.run_exit_optimization(symbol, data_source="ws_persistent", write_reports=True)
    except Exception as e:
        exits = {"verdict": "ERROR", "detail": str(e)[:160], "watchlist_or_better": 0}
    readiness = _readiness(continuity, compare, strategy, exits)
    return {**base, "tool_version": TOOL_VERSION, "persistent_health": health,
            "persistent_continuity": continuity, "source_compare_3way": compare,
            "strategy_hardening": strategy, "ws_persistent_tournament": tournament,
            "exit_optimization": exits, "readiness_v1043c": readiness, **_safety()}


def gather_state_fast(symbol: str = "BTCUSDT") -> dict[str, Any]:
    """Cheap dashboard state for the live watcher.

    The watcher must refresh every few seconds without re-running the full
    strategy lab, tournament or exit optimizer. It recomputes only fast health /
    continuity reads and reuses the latest slow sections from the previous
    dashboard JSON when available. Stale or missing slow sections are labelled
    explicitly instead of being fabricated.
    """
    base = A.gather_state(symbol)
    try:
        health = PWS.ws_persistent_health(symbol)
    except Exception:
        health = {"status": "NO_DATA"}
    out_dir = _output_dir()
    continuity, compare, source_meta = _cached_source_metrics(symbol, out_dir)
    previous = _read_json(out_dir / "dashboard_data_v10_43c.json") or {}
    strategy = previous.get("strategy_hardening") or _missing_slow("STRATEGY_METRICS_MISSING")
    tournament = previous.get("ws_persistent_tournament") or _missing_slow("TOURNAMENT_METRICS_MISSING")
    exits = previous.get("exit_optimization") or _missing_slow("EXIT_METRICS_MISSING")
    slow_meta = _slow_metrics_meta(previous, out_dir)
    readiness = _readiness(continuity, compare, strategy, exits)
    return {**base, "tool_version": TOOL_VERSION, "persistent_health": health,
            "persistent_continuity": continuity, "source_compare_3way": compare,
            "strategy_hardening": strategy, "ws_persistent_tournament": tournament,
            "exit_optimization": exits, "readiness_v1043c": readiness,
            "fast_metrics": {"last_updated_at": _utc_now(), "source": "fast_watcher"},
            "slow_metrics": {**slow_meta, **source_meta}, **_safety()}


def _missing_slow(status: str) -> dict[str, Any]:
    return {"global_verdict": status, "verdict": status, "watchlist_or_better": 0,
            "status": status, "stale_or_missing": True, **_safety()}


def _slow_metrics_meta(previous: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    json_path = out_dir / "dashboard_data_v10_43c.json"
    age = _file_age_seconds(json_path)
    strategy = previous.get("strategy_hardening") or {}
    exits = previous.get("exit_optimization") or {}
    last_strategy = strategy.get("ran_at") or _mtime_iso(json_path)
    last_exit = exits.get("ran_at") or _mtime_iso(json_path)
    stale = age is None or age > SLOW_STALE_SECONDS
    return {"strategy_last_updated_at": last_strategy,
            "exit_last_updated_at": last_exit,
            "strategy_age_seconds": age,
            "exit_age_seconds": age,
            "stale_after_seconds": SLOW_STALE_SECONDS,
            "strategy_stale": stale,
            "exit_stale": stale,
            "note": ("slow metrics are reused from the latest dashboard JSON; "
                     "run strategy/exit CLIs to refresh them")}


def _cached_source_metrics(symbol: str, out_dir: Path) -> tuple[dict, dict, dict]:
    """Cache the expensive continuity/source comparison used by the watcher.

    Persistent health remains fresh every refresh. Continuity and 3-way source
    comparison can scan large CSVs, so the watcher recomputes them at a slower
    cadence and labels the cache age explicitly. A changed file before the slow
    cadence is not ignored; it is shown as `source_dataset_changed_since_cache`.
    """
    cache_path = out_dir / CACHE_FILE
    cache = _read_json(cache_path) or {}
    now_ts = datetime.now(timezone.utc).timestamp()
    sig = _source_signature(symbol)
    age = None
    try:
        age = now_ts - float(cache.get("updated_ts"))
    except Exception:
        age = None
    usable = (
        cache.get("symbol") == symbol
        and isinstance(cache.get("continuity"), dict)
        and isinstance(cache.get("compare"), dict)
        and age is not None
        and age < SLOW_SOURCE_REFRESH_SECONDS
    )
    if usable:
        dataset_changed = cache.get("source_signature") != sig
        meta = {
            "source_metrics_cache": "HIT",
            "source_metrics_age_seconds": round(age, 1),
            "source_metrics_stale": False,
            "source_metrics_refresh_seconds": SLOW_SOURCE_REFRESH_SECONDS,
            "source_dataset_changed_since_cache": bool(dataset_changed),
            "source_metrics_note": (
                "continuity/source compare cached for fast watcher; health is fresh")
        }
        return cache["continuity"], cache["compare"], meta
    try:
        continuity = PWS.ws_continuity_audit(symbol)
    except Exception:
        continuity = {"verdict": "NO_WS_DATA", "max_contiguous_run": 0,
                      "cache_error": "continuity_failed"}
    try:
        compare = PWS.dataset_source_compare_3way(symbol)
    except Exception:
        compare = {"recommended_source": "rest", "blockers": ["COMPARE_FAILED"],
                   "rest": {}, "ws": {}, "ws_persistent": {}}
    payload = {"symbol": symbol, "updated_at": _utc_now(), "updated_ts": now_ts,
               "source_signature": sig, "continuity": continuity, "compare": compare,
               "mode": "RESEARCH_ONLY", "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        os.replace(tmp, cache_path)
    except OSError:
        pass
    meta = {"source_metrics_cache": "MISS_REFRESHED",
            "source_metrics_age_seconds": 0.0,
            "source_metrics_stale": False,
            "source_metrics_refresh_seconds": SLOW_SOURCE_REFRESH_SECONDS,
            "source_dataset_changed_since_cache": False,
            "source_metrics_note": "continuity/source compare recomputed this refresh"}
    return continuity, compare, meta


def _source_signature(symbol: str) -> dict[str, Any]:
    try:
        p = PWS._persistent_path()
        st = p.stat() if p.is_file() else None
        return {"symbol": symbol, "path": str(p).replace("\\", "/"),
                "size": st.st_size if st else 0,
                "mtime": st.st_mtime if st else None}
    except Exception:
        return {"symbol": symbol, "path": None, "size": 0, "mtime": None}


def _output_dir() -> Path:
    return CE._repo_root().joinpath(*OUTPUT_SUBDIR)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _file_age_seconds(path: Path) -> float | None:
    try:
        return round(datetime.now(timezone.utc).timestamp() - path.stat().st_mtime, 1)
    except Exception:
        return None


def _mtime_iso(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
    except Exception:
        return None


def _readiness(continuity: dict, compare: dict, strategy: dict, exits: dict) -> dict[str, Any]:
    states = ["RESEARCH_ONLY"]
    blockers = list(compare.get("blockers") or [])
    if continuity.get("verdict") not in ("WS_READY_FOR_SHADOW_FORWARD",):
        states.append("BLOCKED_BY_DATA_GAP")
    if strategy.get("watchlist_or_better", 0) <= 0:
        states.append("BLOCKED_BY_NO_STRATEGY_EDGE")
    if exits.get("watchlist_or_better", 0) <= 0:
        states.append("BLOCKED_BY_NO_EXIT_EDGE")
    primary = "BLOCKED_BY_DATA_GAP" if "BLOCKED_BY_DATA_GAP" in states else "RESEARCH_ONLY_NOT_ACTIONABLE"
    return {"primary": primary, "states": states, "blockers": blockers,
            "micro_live_ready": False, "paper_ready": False, "live_ready": False,
            **_safety()}


def _panel_persistent_ws(d: dict) -> str:
    h = d.get("persistent_health", {})
    c = d.get("persistent_continuity", {})
    return (
        A._kv("Collector status", h.get("status"), A._state_kind(h.get("status"))) +
        A._kv("Connected", h.get("connected")) +
        A._kv("Health file age (s)", h.get("health_file_age_seconds")) +
        A._kv("Last message age (s)", h.get("age_seconds"),
              A._state_kind(h.get("status"))) +
        A._kv("Dataset file age (min)", h.get("dataset_file_age_min")) +
        A._kv("Messages", h.get("messages_count")) +
        A._kv("Persistent trades", h.get("trades_count")) +
        A._kv("Trades", c.get("trades")) +
        A._kv("Bars", c.get("bars")) +
        A._kv("Max contiguous run", c.get("max_contiguous_run")) +
        A._kv("Forward coverage", A._pct(c.get("forward_coverage"))) +
        A._kv("Continuity verdict", c.get("verdict"), A._state_kind(c.get("verdict"))) +
        A._kv("Fit shadow forward", c.get("fit_for_shadow_forward")))


def _panel_compare(d: dict) -> str:
    c = d.get("source_compare_3way", {})
    rows = []
    for name, label in (("rest", "REST"), ("ws", "WS v10.42"),
                        ("ws_persistent", "WS Persistent v10.43C")):
        m = c.get(name, {}) or {}
        rows.append(f'<tr><td>{label}</td><td>{m.get("bars","N/A")}</td>'
                    f'<td>{m.get("max_contiguous_run","N/A")}</td>'
                    f'<td>{A._pct(m.get("coverage"))}</td>'
                    f'<td>{html.escape(str(m.get("verdict","N/A")))}</td></tr>')
    blockers = ", ".join(c.get("blockers") or []) or "none"
    return (f'<table class="tbl"><tr><th>Source</th><th>Bars</th><th>Max run</th>'
            f'<th>Coverage</th><th>Verdict</th></tr>{"".join(rows)}</table>'
            f'<div class="sub">recommended_source: '
            f'{A._badge(c.get("recommended_source"), "warn")} · ready_for_shadow_forward='
            f'{html.escape(str(c.get("ready_for_shadow_forward")))}</div>'
            f'<div class="sub">blockers: {html.escape(blockers)}</div>')


def _panel_strategy(d: dict) -> str:
    s = d.get("strategy_hardening", {})
    best = s.get("best") or {}
    return (
        A._kv("Global verdict", s.get("global_verdict"), A._state_kind(s.get("global_verdict"))) +
        A._kv("Candidates", s.get("candidates_generated")) +
        A._kv("Verdict counts", s.get("verdict_counts")) +
        A._kv("Rejection categories", s.get("rejection_category_counts")) +
        A._kv("Best", best.get("strategy_name")) +
        A._kv("Best verdict", best.get("verdict"), A._state_kind(best.get("verdict"))) +
        A._kv("Best net_EV lower bound", s.get("best_net_EV_lower_bound")) +
        '<div class="sub">ranking: net_EV_lower_bound, robustness, sample, costs/slippage, baseline comparison</div>')


def _panel_exit(d: dict) -> str:
    e = d.get("exit_optimization", {})
    b = e.get("best_variant") or {}
    wl = e.get("winner_loser") or {}
    return (
        A._kv("Verdict", e.get("verdict"), A._state_kind(e.get("verdict"))) +
        A._kv("Entries", e.get("n_entries")) +
        A._kv("Variants x horizons", e.get("variants_x_horizons")) +
        A._kv("Best variant", f'{b.get("exit_variant")}@{b.get("horizon")}') +
        A._kv("Best verdict", b.get("verdict"), A._state_kind(b.get("verdict"))) +
        A._kv("Best net_EV lb", b.get("net_EV_lower_bound")) +
        A._kv("Partial TP model", b.get("partial_tp_model")) +
        A._kv("Avg MFE captured", wl.get("avg_pct_of_MFE_captured")) +
        '<div class="sub">exit variants are exits-only: no leverage, no sizing, no execution.</div>')


def _panel_watch(d: dict) -> str:
    w = d.get("dashboard_watch") or {}
    slow = d.get("slow_metrics") or {}
    fast = d.get("fast_metrics") or {}
    return (
        A._kv("Auto refresh", w.get("auto_refresh"), "ok" if w.get("auto_refresh") else "warn") +
        A._kv("Watcher status", w.get("watcher_status"), A._state_kind(w.get("watcher_status"))) +
        A._kv("Interval seconds", w.get("interval_seconds")) +
        A._kv("Last refresh", w.get("last_refresh_at")) +
        A._kv("Dashboard age (s)", w.get("dashboard_age_seconds")) +
        A._kv("Fast metrics updated", fast.get("last_updated_at")) +
        A._kv("Strategy metrics updated", slow.get("strategy_last_updated_at")) +
        A._kv("Strategy metrics stale", slow.get("strategy_stale"),
              "warn" if slow.get("strategy_stale") else "ok") +
        A._kv("Exit metrics updated", slow.get("exit_last_updated_at")) +
        A._kv("Exit metrics stale", slow.get("exit_stale"),
              "warn" if slow.get("exit_stale") else "ok") +
        A._kv("Continuity cache", slow.get("source_metrics_cache")) +
        A._kv("Continuity cache age (s)", slow.get("source_metrics_age_seconds")) +
        A._kv("Dataset changed since cache", slow.get("source_dataset_changed_since_cache"),
              "warn" if slow.get("source_dataset_changed_since_cache") else "ok") +
        '<div class="sub">Fast watcher does not run full strategy lab / tournament / exit optimization every refresh.</div>')


def _latest_v1044() -> dict[str, Any]:
    out = CE._repo_root().joinpath("reports", "research", "v10_44_alpha_sprint")
    return {
        "alpha": _read_json(out / "alpha_factory_v10_44.json") or {},
        "exit": _read_json(out / "exit_factory_v10_44.json") or {},
        "incubator": _read_json(out / "candidate_incubator_v10_44.json") or {},
        "out_dir": str(out).replace("\\", "/"),
    }


def _panel_alpha_factory(d: dict) -> str:
    v = _latest_v1044()
    alpha = v["alpha"]
    inc = v["incubator"]
    ex = v["exit"]
    counts = inc.get("state_counts") or alpha.get("candidate_status_counts") or {}
    best = inc.get("best_research_candidate") or alpha.get("best_candidate") or {}
    return (
        A._kv("Alpha verdict", alpha.get("overall_verdict") or "NOT_RUN",
              A._state_kind(alpha.get("overall_verdict") or "NEED_DATA")) +
        A._kv("Strategies tested", alpha.get("strategies_tested", 0)) +
        A._kv("Incubator verdict", inc.get("overall_verdict") or "NOT_RUN",
              A._state_kind(inc.get("overall_verdict") or "NEED_DATA")) +
        A._kv("State counts", counts) +
        A._kv("Best candidate", best.get("candidate_id") or "NONE") +
        A._kv("Best state", best.get("incubator_state") or best.get("status") or "NONE",
              A._state_kind(best.get("incubator_state") or best.get("status") or "NEED_DATA")) +
        A._kv("Exit verdict", ex.get("overall_verdict") or "NOT_RUN",
              A._state_kind(ex.get("overall_verdict") or "NEED_DATA")) +
        A._kv("Reports", v["out_dir"]) +
        '<div class="sub">V10.44 Alpha Factory is research-only. Candidate labels are NOT executable signals. NO LIVE.</div>')


def _panel_lattice(d: dict) -> str:
    cont = d.get("persistent_continuity", {})
    tour = d.get("ws_persistent_tournament") or {}
    best = tour.get("best_strategy") or {}
    n = best.get("n_signals") or 0
    if cont.get("verdict") in ("NO_WS_DATA", "WS_TOO_GAPPY", "WS_STALE", "WS_COLLECTOR_DOWN"):
        state = "DATA_GAP" if cont.get("verdict") == "WS_TOO_GAPPY" else cont.get("verdict")
    elif n < 20:
        state = "INSUFFICIENT_SAMPLE"
    else:
        state = None
    cells = []
    for label in ("TP", "SL", "TIME", "NO_TRADE"):
        val = state or "N/A"
        cells.append(f'<div class="cell"><div class="cell-h">{label}</div>'
                     f'<div class="cell-v" style="font-size:13px">{html.escape(str(val))}</div></div>')
    return f'<div class="lattice">{"".join(cells)}</div><div class="sub">Probability lattice is blocked until real contiguous sample exists.</div>'


def render_html(d: dict, auto_refresh_seconds: int | None = None) -> str:
    base = A.render_html({**d, "readiness": d.get("readiness_v1043c") or d.get("readiness")})
    base = _remove_legacy_probability_lattice(base)
    extra = _EXTRA.format(
        watch=_panel_watch(d),
        pws=_panel_persistent_ws(d), compare=_panel_compare(d),
        strategy=_panel_strategy(d), exits=_panel_exit(d),
        alpha=_panel_alpha_factory(d),
        lattice=_panel_lattice(d),
        graph=A._relationship_graph((d.get("persistent_continuity") or {}).get("verdict")),
        gen=html.escape(datetime.now(timezone.utc).isoformat()))
    marker = '<div class="foot">'
    base = base.replace(marker, extra + marker, 1) if marker in base else base.replace("</body>", extra + "</body>", 1)
    base = base.replace("V10.43A DASHBOARD", "V10.43C DASHBOARD")
    if auto_refresh_seconds:
        base = _inject_auto_refresh(base, auto_refresh_seconds)
    return base


def _inject_auto_refresh(rendered: str, seconds: int) -> str:
    sec = max(MIN_REFRESH_SECONDS, int(seconds))
    meta = f'<meta http-equiv="refresh" content="{sec}">'
    if 'http-equiv="refresh"' in rendered:
        return rendered
    return rendered.replace("<title>", meta + "\n<title>", 1)


def _remove_legacy_probability_lattice(rendered: str) -> str:
    """The V10.43A base includes a legacy 0%-filled probability card. V10.43C
    replaces it with a DATA_GAP/STALE/INSUFFICIENT_SAMPLE lattice, so the old
    card is removed to avoid false precision."""
    start = rendered.find('<div class="card"><h3>Probability Lattice</h3>')
    marker = '<div class="sub">outcome distribution of the best shadow policy (SIM, research-only)</div></div>'
    if start < 0:
        return rendered
    end = rendered.find(marker, start)
    if end < 0:
        return rendered
    return rendered[:start] + rendered[end + len(marker):]


_EXTRA = """
<div class="grid" style="margin-top:14px">
  <div class="card wide"><h3>Dashboard Auto Refresh</h3>{watch}</div>
  <div class="card wide"><h3>Persistent WS Panel</h3>{pws}</div>
  <div class="card wide"><h3>REST vs WS vs WS Persistent</h3>{compare}</div>
  <div class="card wide"><h3>Alpha Factory V10.44</h3>{alpha}</div>
  <div class="card"><h3>Strategy Lab Hardened</h3>{strategy}</div>
  <div class="card"><h3>Exit Optimization Panel</h3>{exits}</div>
  <div class="card"><h3>Probability Lattice</h3>{lattice}</div>
  <div class="card wide"><h3>Relationship Graph</h3>{graph}<div class="sub">No invented correlations. Alt symbols WAITING_DATA until collected.</div></div>
</div>
<div class="sub" style="text-align:center;margin-top:8px">V10.43C generated {gen} · RESEARCH_ONLY · NO LIVE</div>
"""


def build_dashboard(symbol: str = "BTCUSDT", state: dict | None = None,
                    out_dir=None, write: bool = True,
                    auto_refresh_seconds: int | None = None,
                    fast: bool = False,
                    watch_status: dict[str, Any] | None = None) -> dict[str, Any]:
    data = state if state is not None else (gather_state_fast(symbol) if fast else gather_state(symbol))
    data = {**data, **_safety()}
    d = Path(out_dir) if out_dir is not None else _output_dir()
    interval = _normalize_interval(auto_refresh_seconds) if auto_refresh_seconds else None
    watch = watch_status or _current_watch_view(d, interval)
    data["dashboard_watch"] = watch
    result = {"tool_version": TOOL_VERSION, "mode": "RESEARCH_ONLY", **_safety()}
    if write:
        d.mkdir(parents=True, exist_ok=True)
        (d / "index.html").write_text(render_html(data, auto_refresh_seconds=interval), encoding="utf-8")
        tmp = d / "dashboard_data_v10_43c.json.tmp"
        tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        os.replace(tmp, d / "dashboard_data_v10_43c.json")
        result["html"] = str((d / "index.html")).replace("\\", "/")
        result["json"] = str((d / "dashboard_data_v10_43c.json")).replace("\\", "/")
        result["url"] = "file:///" + result["html"].lstrip("/")
    else:
        result["html_str"] = render_html(data, auto_refresh_seconds=interval)
    result["ws_persistent_verdict"] = data.get("persistent_continuity", {}).get("verdict")
    result["readiness"] = data.get("readiness_v1043c", {}).get("primary")
    result["auto_refresh_seconds"] = interval
    return result


def _normalize_interval(interval_seconds: int | float | None) -> int:
    if interval_seconds is None:
        return DEFAULT_REFRESH_SECONDS
    try:
        return max(MIN_REFRESH_SECONDS, int(float(interval_seconds)))
    except Exception:
        return DEFAULT_REFRESH_SECONDS


def _current_watch_view(out_dir: Path, interval: int | None = None) -> dict[str, Any]:
    status = _read_json(out_dir / STATUS_FILE) or {}
    auto = bool(interval or status.get("interval_seconds"))
    html_path = out_dir / "index.html"
    return {"watcher_status": status.get("watcher_status", "NOT_RUNNING"),
            "auto_refresh": auto,
            "interval_seconds": interval or status.get("interval_seconds"),
            "last_refresh_at": status.get("last_refresh_at"),
            "next_refresh_at": status.get("next_refresh_at"),
            "refresh_count": status.get("refresh_count", 0),
            "last_error": status.get("last_error"),
            "dashboard_age_seconds": _file_age_seconds(html_path),
            "dashboard_html": str(html_path).replace("\\", "/"),
            "dashboard_json": str((out_dir / "dashboard_data_v10_43c.json")).replace("\\", "/"),
            "mode": "RESEARCH_ONLY",
            "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}


def run_dashboard_watch(symbol: str = "BTCUSDT", interval_seconds: int | float = DEFAULT_REFRESH_SECONDS,
                        open_browser: bool = False, once: bool = False,
                        out_dir=None, state_builder=None) -> dict[str, Any]:
    out = Path(out_dir) if out_dir is not None else _output_dir()
    out.mkdir(parents=True, exist_ok=True)
    interval = _normalize_interval(interval_seconds)
    lock = _WatchLock(out / LOCK_FILE)
    acquired, existing = lock.acquire()
    if not acquired:
        current = _read_json(out / STATUS_FILE) or {}
        status = {**current,
                  "watcher_status": "WATCHER_ALREADY_RUNNING",
                  "existing_pid": existing.get("pid"),
                  "started_at": existing.get("started_at"),
                  "interval_seconds": current.get("interval_seconds") or existing.get("interval_seconds"),
                  "dashboard_html": current.get("dashboard_html") or str((out / "index.html")).replace("\\", "/"),
                  "dashboard_json": current.get("dashboard_json") or str((out / "dashboard_data_v10_43c.json")).replace("\\", "/"),
                  "mode": "RESEARCH_ONLY",
                  "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}
        return {**status, **_safety()}
    started = _utc_now()
    refresh_count = 0
    last_error = None
    opened = False
    try:
        while True:
            cycle_start_ts = time.time()
            now = datetime.fromtimestamp(cycle_start_ts, timezone.utc)
            planned_next_ts = cycle_start_ts + interval
            planned_next = datetime.fromtimestamp(planned_next_ts, timezone.utc)
            refresh_count += 1
            status = {"watcher_status": "RUNNING",
                      "started_at": started,
                      "last_refresh_at": now.isoformat(),
                      "next_refresh_at": planned_next.isoformat(),
                      "interval_seconds": interval,
                      "refresh_count": refresh_count,
                      "last_error": last_error,
                      "dashboard_html": str((out / "index.html")).replace("\\", "/"),
                      "dashboard_json": str((out / "dashboard_data_v10_43c.json")).replace("\\", "/"),
                      "dashboard_age_seconds": _file_age_seconds(out / "index.html"),
                      "mode": "RESEARCH_ONLY",
                      "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}
            try:
                builder = state_builder or gather_state_fast
                state = builder(symbol)
                build_dashboard(symbol, state=state, out_dir=out, write=True,
                                auto_refresh_seconds=interval, fast=False,
                                watch_status={**status, "auto_refresh": True})
                last_error = None
                finish_ts = time.time()
                actual_next_ts = max(planned_next_ts, finish_ts)
                status["last_refresh_at"] = datetime.fromtimestamp(finish_ts, timezone.utc).isoformat()
                status["next_refresh_at"] = datetime.fromtimestamp(actual_next_ts, timezone.utc).isoformat()
                status["last_error"] = None
                status["dashboard_age_seconds"] = _file_age_seconds(out / "index.html")
                _write_watch_status(out, status)
                _append_watch_log(out, f"refresh={refresh_count} status=OK html={status['dashboard_html']}")
                if open_browser and not opened:
                    _open_dashboard(out / "index.html")
                    opened = True
            except Exception as exc:
                last_error = str(exc)[:500]
                status["last_error"] = last_error
                _write_watch_status(out, status)
                _append_watch_log(out, f"refresh={refresh_count} status=ERROR error={last_error}")
            if once:
                status = {**status, "watcher_status": "ONCE_COMPLETED",
                          "opened_browser": opened}
                _write_watch_status(out, status)
                return {**status, **_safety()}
            sleep_for = max(0.0, planned_next_ts - time.time())
            time.sleep(sleep_for)
    except KeyboardInterrupt:
        stopped = {"watcher_status": "STOPPED_BY_CTRL_C", "started_at": started,
                   "last_refresh_at": _utc_now(), "interval_seconds": interval,
                   "refresh_count": refresh_count, "last_error": last_error,
                   "mode": "RESEARCH_ONLY",
                   "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}
        _write_watch_status(out, stopped)
        _append_watch_log(out, "stopped_by_ctrl_c")
        return {**stopped, **_safety()}
    finally:
        lock.release()


class _WatchLock:
    def __init__(self, path: Path):
        self.path = path
        self.owner_id: str | None = None

    def acquire(self) -> tuple[bool, dict[str, Any]]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existing = _read_json(self.path) or {}
        pid = existing.get("pid")
        if pid and _pid_alive(pid):
            return False, existing
        if self.path.exists():
            try:
                self.path.unlink()
            except OSError:
                existing = _read_json(self.path) or existing
                pid = existing.get("pid")
                if pid and _pid_alive(pid):
                    return False, existing
                raise
        self.owner_id = f"{os.getpid()}:{time.time_ns()}"
        payload = {"pid": os.getpid(), "owner_id": self.owner_id,
                   "started_at": _utc_now(),
                   "mode": "RESEARCH_ONLY",
                   "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        fd = os.open(str(self.path), flags)
        try:
            os.write(fd, json.dumps(payload, indent=2).encode("utf-8"))
        finally:
            os.close(fd)
        return True, payload

    def release(self) -> None:
        if not self.owner_id:
            return
        current = _read_json(self.path) or {}
        if current.get("owner_id") == self.owner_id:
            try:
                self.path.unlink()
            except OSError:
                pass


def _pid_alive(pid: Any) -> bool:
    try:
        pid_i = int(pid)
    except Exception:
        return False
    if pid_i <= 0:
        return False
    if os.name == "nt":
        import subprocess
        try:
            cp = subprocess.run(["tasklist", "/FI", f"PID eq {pid_i}", "/FO", "CSV", "/NH"],
                                capture_output=True, text=True, timeout=2)
            return str(pid_i) in cp.stdout
        except Exception:
            return False
    try:
        os.kill(pid_i, 0)
        return True
    except OSError:
        return False


def _write_watch_status(out_dir: Path, status: dict[str, Any]) -> None:
    payload = {**status, "mode": "RESEARCH_ONLY",
               "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}
    tmp = out_dir / (STATUS_FILE + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, out_dir / STATUS_FILE)


def _append_watch_log(out_dir: Path, message: str) -> None:
    path = out_dir / LOG_FILE
    if path.exists() and path.stat().st_size > 1024 * 1024:
        try:
            path.replace(out_dir / (LOG_FILE + ".1"))
        except OSError:
            pass
    line = f"{_utc_now()} {message} RESEARCH_ONLY FINAL_RECOMMENDATION=NO LIVE\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)


def _open_dashboard(html_path: Path) -> bool:
    try:
        if os.name == "nt":
            os.startfile(str(html_path))  # type: ignore[attr-defined]
        else:
            webbrowser.open("file://" + str(html_path))
        return True
    except Exception:
        return False
