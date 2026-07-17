"""ResearchOps V10.43C - Persistent WS research dashboard (static/local only).

Adds the V10.43C persistent WS health/continuity layer, the 3-way REST vs WS vs
WS-persistent comparison, hardened strategy lab status and exit optimization.
Everything is local/static/reporting-only: no network, no orders, no keys, NO LIVE.
"""

from __future__ import annotations

import html
import json
import os
import subprocess
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
SLOW_SOURCE_REFRESH_SECONDS = 300
CACHE_FILE = "dashboard_fast_cache_v1043c.json"
P11_OBSERVER_OUTPUT_SUBDIR = ("reports", "research", "p11_short_forward_observer")
P11_OBSERVER_STATUS_FILE = "observer_status.json"
P11_OBSERVER_EXPORT_FILES = {
    "lifecycle_ledger": "lifecycle_ledger.jsonl",
    "outcomes": "outcomes.csv",
    "labels": "labels.csv",
    "reconciliation": "reconciliation_report.json",
    "summary": "summary.txt",
}


def _safety() -> dict[str, Any]:
    return {"research_only": True, "shadow_only": True, "can_send_real_orders": False,
            "paper_filter_enabled": False, "edge_validated": False,
            "not_actionable": True, "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}


def _ati_paper_snapshot() -> dict[str, Any]:
    try:
        from .ati_paper.api import dashboard_snapshot

        return dashboard_snapshot()
    except Exception as exc:
        return {
            "status": "ERROR", "error": f"{type(exc).__name__}:{str(exc)[:180]}",
            "account": {"account": None}, "positions": {"positions": []},
            "trades": {"trades": []}, "equity": {"equity": []},
            "events": {"events": []}, "signals": {"signals": []},
            "health": {"status": "ERROR"}, "performance": {"sample_size": 0},
            "simulation_only": True, "can_send_real_orders": False,
            "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE,
        }


def _cross_venue_snapshot() -> dict[str, Any]:
    try:
        from .cross_venue.api import dashboard_snapshot

        return dashboard_snapshot()
    except Exception as exc:
        return {
            "status": "ERROR", "error": f"{type(exc).__name__}:{str(exc)[:180]}",
            "venues": [], "signals": [], "positions": [], "trades": [], "equity": [],
            "normalized_price_series": {}, "leadlag": {"leaderboard": []},
            "leverage": {"scenarios": []}, "health": {"status": "ERROR"},
            "simulation_only": True, "research_only": True,
            "can_send_real_orders": False, "final_recommendation": "NO LIVE",
        }


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
    _publish_source_metrics_cache(symbol, continuity, compare, _output_dir())
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
            "exit_optimization": exits, "readiness_v1043c": readiness,
            "base_metrics": {"source": "EXPLICIT_HEAVY_BUILD",
                             "refreshed_at": _utc_now(),
                             "refresh_mode": "MANUAL_OR_EXPLICIT_CLI"},
            "p11_short_forward_observer": _load_p11_observer_status(),
            "ati_paper": _ati_paper_snapshot(), "cross_venue": _cross_venue_snapshot(), **_safety()}


def gather_state_fast(symbol: str = "BTCUSDT") -> dict[str, Any]:
    """Cheap dashboard state for the live watcher.

    The watcher must refresh every few seconds without re-running the full
    strategy lab, tournament or exit optimizer. It recomputes only fast health /
    continuity reads and reuses the latest slow sections from the previous
    dashboard JSON when available. Stale or missing slow sections are labelled
    explicitly instead of being fabricated.
    """
    out_dir = _output_dir()
    base = _cached_base_state(symbol, out_dir)
    try:
        health = PWS.ws_persistent_health(symbol)
    except Exception:
        health = {"status": "NO_DATA"}
    continuity, compare, source_meta = _cached_source_metrics(symbol, out_dir)
    previous = _read_json(out_dir / "dashboard_data_v10_43c.json") or {}
    strategy = previous.get("strategy_hardening") or _missing_slow("STRATEGY_METRICS_MISSING")
    tournament = previous.get("ws_persistent_tournament") or _missing_slow("TOURNAMENT_METRICS_MISSING")
    exits = previous.get("exit_optimization") or _missing_slow("EXIT_METRICS_MISSING")
    slow_meta = _slow_metrics_meta(previous, out_dir)
    readiness = _readiness(continuity, compare, strategy, exits)
    readiness = _apply_artifact_readiness_gates(
        readiness, base.get("base_metrics") or {}, source_meta)
    return {**base, "tool_version": TOOL_VERSION, "persistent_health": health,
            "persistent_continuity": continuity, "source_compare_3way": compare,
            "strategy_hardening": strategy, "ws_persistent_tournament": tournament,
            "exit_optimization": exits, "readiness_v1043c": readiness,
            "p11_short_forward_observer": _load_p11_observer_status(),
            "ati_paper": _ati_paper_snapshot(),
            "cross_venue": _cross_venue_snapshot(),
            "fast_metrics": {"last_updated_at": _utc_now(),
                             "source": "ARTIFACT_ONLY_FAST_WATCHER",
                             "heavy_analysis_executed": False},
            "slow_metrics": {**slow_meta, **source_meta}, **_safety()}


def _missing_slow(status: str) -> dict[str, Any]:
    return {"global_verdict": status, "verdict": status, "watchlist_or_better": 0,
            "status": status, "stale_or_missing": True, **_safety()}


def _iso_age_seconds(iso: str | None) -> float | None:
    """Age of a real run timestamp (`ran_at`), never of a regenerated file."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return round(datetime.now(timezone.utc).timestamp() - dt.timestamp(), 1)
    except Exception:
        return None


def _slow_metrics_meta(previous: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    """Staleness of the heavy sections from their REAL run timestamps.

    The watcher rewrites dashboard_data_v10_43c.json every cycle, so the file
    mtime is always fresh and must NEVER be used as the heavy-run age. Only the
    `ran_at` stamped by the strategy/exit runs counts; a missing stamp is
    reported as STALE_UNKNOWN instead of silently looking fresh."""
    strategy = previous.get("strategy_hardening") or {}
    exits = previous.get("exit_optimization") or {}
    s_age = _iso_age_seconds(strategy.get("ran_at"))
    e_age = _iso_age_seconds(exits.get("ran_at"))
    s_stale = s_age is None or s_age > SLOW_STALE_SECONDS
    e_stale = e_age is None or e_age > SLOW_STALE_SECONDS
    return {"strategy_last_updated_at": strategy.get("ran_at") or "STALE_UNKNOWN",
            "exit_last_updated_at": exits.get("ran_at") or "STALE_UNKNOWN",
            "strategy_age_seconds": s_age,
            "exit_age_seconds": e_age,
            "stale_after_seconds": SLOW_STALE_SECONDS,
            "strategy_stale": s_stale,
            "exit_stale": e_stale,
            "note": ("staleness measured from each section's real ran_at, not "
                     "from the regenerated dashboard JSON; run strategy/exit "
                     "CLIs (or research-heavy-run-v1044) to refresh them")}


def _cached_base_state(symbol: str, out_dir: Path) -> dict[str, Any]:
    """Load the V10.43A base exclusively from small published artifacts.

    A 30-second watcher must never reload or re-bar a growing dataset. Missing
    or stale artifacts remain explicit and fail closed until an explicit build.
    """
    previous = _read_json(out_dir / "dashboard_data_v10_43c.json") or {}
    same_symbol = str(previous.get("symbol") or "").upper() == symbol.upper()
    rd = CE._repo_root().joinpath("reports", "research")
    keys = ("health", "view", "data_quality", "shadow", "scoreboard", "bankroll",
            "readiness")
    if same_symbol:
        base = {key: previous.get(key) for key in keys}
        source = "DASHBOARD_ARTIFACT"
    else:
        view = {"status": "NO_DATA", "forward_n_bars": 0,
                "fit_for_fine_backtest": False, "fit_for_shadow_forward": False}
        dq = {"states": ["INSUFFICIENT_FORWARD_DATA"],
              "tournament_result_reliability": "NOT_RELIABLE_SAMPLE"}
        shadow = _read_json(rd / "shadow_simulation" / "shadow_summary_v1040.json")
        base = {
            "health": {"status": "UNKNOWN_ARTIFACT_ONLY",
                       "sub_states": ["EXPLICIT_REFRESH_REQUIRED"]},
            "view": view,
            "data_quality": dq,
            "shadow": shadow,
            "scoreboard": A._read_csv(
                rd / "shadow_simulation" / "shadow_scoreboard_v1040.csv"),
            "bankroll": _read_json(
                rd / "shadow_simulation" / "shadow_bankroll_20eur_v1040.json"),
            "readiness": A._readiness(view, dq, shadow),
        }
        source = "NO_DASHBOARD_ARTIFACT"

    # These are bounded report reads/file stats and stay cheap as data grows.
    base["ati"] = {
        "health": _read_json(rd / "ati" / "ati_health.json"),
        "summary": _read_json(rd / "ati" / "ati_summary.json"),
        "forward": _read_json(rd / "ati" / "ati_forward_state.json"),
    }
    base["ws_dataset"] = A._ws_dataset_meta()
    previous_meta = previous.get("base_metrics") if same_symbol else None
    refreshed_at = previous_meta.get("refreshed_at") if isinstance(previous_meta, dict) else None
    age = _iso_age_seconds(refreshed_at)
    base.update({
        "tool_version": A.TOOL_VERSION,
        "symbol": symbol,
        "generated_at": _utc_now(),
        # Git provenance is cheap and must reflect the code rendering this
        # artifact even when all heavy research metrics remain cached.
        "git_head": A._git_head(),
        "base_metrics": {
            "source": source,
            "refreshed_at": refreshed_at or "STALE_UNKNOWN",
            "age_seconds": age,
            "stale": age is None or age > SLOW_STALE_SECONDS,
            "refresh_mode": "EXPLICIT_ONLY",
            "note": ("fast watcher reads artifacts only; explicit dashboard build "
                     "refreshes base metrics"),
        },
        **_safety(),
    })
    return base


def _cached_source_metrics(symbol: str, out_dir: Path) -> tuple[dict, dict, dict]:
    """Read source metrics artifacts without scanning the growing datasets.

    Only an explicit dashboard build may refresh this cache. The watcher marks
    stale or missing metrics and blocks readiness instead of doing heavy work.
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
    )
    if usable:
        dataset_changed = cache.get("source_signature") != sig
        stale = age is None or age >= SLOW_SOURCE_REFRESH_SECONDS or dataset_changed
        meta = {
            "source_metrics_cache": "STALE_HIT" if stale else "HIT",
            "source_metrics_age_seconds": round(age, 1) if age is not None else None,
            "source_metrics_stale": stale,
            "source_metrics_refresh_seconds": SLOW_SOURCE_REFRESH_SECONDS,
            "source_dataset_changed_since_cache": bool(dataset_changed),
            "source_metrics_refresh_mode": "EXPLICIT_ONLY",
            "source_metrics_note": (
                "artifact-only watcher; run explicit dashboard build to refresh "
                "continuity/source compare")
        }
        return cache["continuity"], cache["compare"], meta
    previous = _read_json(out_dir / "dashboard_data_v10_43c.json") or {}
    continuity = previous.get("persistent_continuity")
    compare = previous.get("source_compare_3way")
    if not isinstance(continuity, dict) or not isinstance(compare, dict):
        continuity = {"verdict": "NO_CACHED_SOURCE_METRICS",
                      "max_contiguous_run": 0,
                      "fit_for_shadow_forward": False}
        compare = {"recommended_source": None,
                   "ready_for_shadow_forward": False,
                   "blockers": ["SOURCE_METRICS_EXPLICIT_REFRESH_REQUIRED"],
                   "rest": {}, "ws": {}, "ws_persistent": {}}
        cache_status = "MISS_NO_ARTIFACT"
    else:
        cache_status = "DASHBOARD_ARTIFACT_FALLBACK"
    meta = {"source_metrics_cache": cache_status,
            "source_metrics_age_seconds": None,
            "source_metrics_stale": True,
            "source_metrics_refresh_seconds": SLOW_SOURCE_REFRESH_SECONDS,
            "source_dataset_changed_since_cache": True,
            "source_metrics_refresh_mode": "EXPLICIT_ONLY",
            "source_metrics_note": "no valid cache; explicit dashboard build required"}
    return continuity, compare, meta


def _publish_source_metrics_cache(symbol: str, continuity: dict, compare: dict,
                                  out_dir: Path) -> bool:
    """Publish an explicit heavy-run result for artifact-only watcher reads."""
    cache_path = out_dir / CACHE_FILE
    payload = {"symbol": symbol, "updated_at": _utc_now(),
               "updated_ts": datetime.now(timezone.utc).timestamp(),
               "source_signature": _source_signature(symbol),
               "continuity": continuity, "compare": compare,
               "mode": "RESEARCH_ONLY",
               "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        os.replace(tmp, cache_path)
        return True
    except OSError:
        return False


def _apply_artifact_readiness_gates(readiness: dict[str, Any], base_meta: dict,
                                    source_meta: dict) -> dict[str, Any]:
    """Stale watcher artifacts are never allowed to look research-ready."""
    result = dict(readiness)
    states = list(result.get("states") or [])
    blockers = list(result.get("blockers") or [])
    if base_meta.get("stale"):
        states.append("BASE_METRICS_STALE_EXPLICIT_REFRESH_REQUIRED")
        blockers.append("BASE_METRICS_STALE_EXPLICIT_REFRESH_REQUIRED")
    if source_meta.get("source_metrics_stale"):
        states.append("SOURCE_METRICS_STALE_EXPLICIT_REFRESH_REQUIRED")
        blockers.append("SOURCE_METRICS_STALE_EXPLICIT_REFRESH_REQUIRED")
    if source_meta.get("source_dataset_changed_since_cache"):
        states.append("SOURCE_DATASET_CHANGED_SINCE_EXPLICIT_REFRESH")
        blockers.append("SOURCE_DATASET_CHANGED_SINCE_EXPLICIT_REFRESH")
    result["states"] = list(dict.fromkeys(states))
    result["blockers"] = list(dict.fromkeys(blockers))
    if "BLOCKED_BY_DATA_GAP" not in result["states"] and blockers:
        result["primary"] = "RESEARCH_METRICS_STALE_EXPLICIT_REFRESH_REQUIRED"
    result.update({"micro_live_ready": False, "paper_ready": False,
                   "live_ready": False, **_safety()})
    return result


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


def _p11_observer_output_dir() -> Path:
    """Fixed local output owned by the observer; the dashboard never writes it."""
    return CE._repo_root().joinpath(*P11_OBSERVER_OUTPUT_SUBDIR)


def _load_p11_observer_status() -> dict[str, Any]:
    """Read only the observer's atomically published status snapshot.

    There is deliberately no import of the observer, its persistence layer, or
    any trading/runtime module here.  A missing or malformed snapshot is an
    explicit unavailable state, never a fabricated all-zero observation.
    """
    status_path = _p11_observer_output_dir() / P11_OBSERVER_STATUS_FILE
    payload = _read_json(status_path)
    if not isinstance(payload, dict):
        return {
            "observer_status": "OBSERVER_STATUS_UNAVAILABLE",
            "_snapshot_available": False,
            "_snapshot_path": str(status_path).replace("\\", "/"),
            "_snapshot_age_seconds": None,
        }
    return {
        **payload,
        "_snapshot_available": True,
        "_snapshot_path": str(status_path).replace("\\", "/"),
        "_snapshot_age_seconds": _file_age_seconds(status_path),
    }


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
        A._kv("Session messages (this process)", h.get("messages_count")) +
        A._kv("Session new trades (this process)", h.get("trades_count")) +
        A._kv("Session reconnects", h.get("reconnect_count")) +
        A._kv("Dataset trades (deduped, all time)", c.get("trades")) +
        A._kv("Dataset bars (1m)", c.get("bars")) +
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
    base = d.get("base_metrics") or {}
    return (
        A._kv("Auto refresh", w.get("auto_refresh"), "ok" if w.get("auto_refresh") else "warn") +
        A._kv("Watcher status", w.get("watcher_status"), A._state_kind(w.get("watcher_status"))) +
        A._kv("Interval seconds", w.get("interval_seconds")) +
        A._kv("Last refresh", w.get("last_refresh_at")) +
        A._kv("Dashboard age (s)", w.get("dashboard_age_seconds")) +
        A._kv("Fast metrics updated", fast.get("last_updated_at")) +
        A._kv("Heavy analysis in watcher", fast.get("heavy_analysis_executed"), "ok") +
        A._kv("Base artifact refreshed", base.get("refreshed_at")) +
        A._kv("Base artifact stale", base.get("stale"),
              "warn" if base.get("stale") else "ok") +
        A._kv("Strategy metrics updated", slow.get("strategy_last_updated_at")) +
        A._kv("Strategy metrics stale", slow.get("strategy_stale"),
              "warn" if slow.get("strategy_stale") else "ok") +
        A._kv("Exit metrics updated", slow.get("exit_last_updated_at")) +
        A._kv("Exit metrics stale", slow.get("exit_stale"),
              "warn" if slow.get("exit_stale") else "ok") +
        A._kv("Continuity cache", slow.get("source_metrics_cache")) +
        A._kv("Continuity cache age (s)", slow.get("source_metrics_age_seconds")) +
        A._kv("Continuity cache stale", slow.get("source_metrics_stale"),
              "warn" if slow.get("source_metrics_stale") else "ok") +
        A._kv("Dataset changed since cache", slow.get("source_dataset_changed_since_cache"),
              "warn" if slow.get("source_dataset_changed_since_cache") else "ok") +
        '<div class="sub">Fast watcher is artifact-only: it never scans growing datasets or runs strategy lab / tournament / exit optimization. Stale artifacts require an explicit dashboard build.</div>')


def _ati_metric(label: str, value: Any, element_id: str) -> str:
    display = "N/A" if value is None or value == "" else str(value)
    return (
        '<div class="ati-paper-metric"><span>' + html.escape(label) + '</span>'
        f'<strong id="{html.escape(element_id, quote=True)}">{html.escape(display)}</strong></div>'
    )


def _table_rows(rows: list[dict[str, Any]], columns: tuple[str, ...], *, empty: str) -> str:
    if not rows:
        return f'<tr><td colspan="{len(columns)}">{html.escape(empty)}</td></tr>'
    return "".join(
        "<tr>" + "".join(
            f"<td>{html.escape(str(row.get(column) if row.get(column) is not None else 'N/A'))}</td>"
            for column in columns
        ) + "</tr>"
        for row in rows
    )


def _panel_ati_paper(d: dict) -> str:
    snapshot = d.get("ati_paper") or {}
    account_wrap = snapshot.get("account") if isinstance(snapshot.get("account"), dict) else {}
    account = account_wrap.get("account") if isinstance(account_wrap.get("account"), dict) else {}
    health = snapshot.get("health") if isinstance(snapshot.get("health"), dict) else {}
    perf = snapshot.get("performance") if isinstance(snapshot.get("performance"), dict) else {}
    sizing = account_wrap.get("sizing") if isinstance(account_wrap.get("sizing"), dict) else {}
    positions = ((snapshot.get("positions") or {}).get("positions") or [])
    trades = ((snapshot.get("trades") or {}).get("trades") or [])
    events = ((snapshot.get("events") or {}).get("events") or [])
    position_rows = _table_rows(positions, (
        "symbol", "direction", "entry_reference_price", "last_price", "stop_price",
        "take_profit_price", "quantity", "notional", "estimated_net_pnl", "status",
    ), empty="No open simulated positions in this snapshot")
    trade_rows = _table_rows(trades, (
        "trade_id", "exit_ts", "symbol", "direction", "entry_reference_price",
        "exit_reference_price", "notional", "fees", "slippage", "net_pnl", "exit_reason",
    ), empty="No closed simulated trades in this snapshot")
    event_rows = "".join(
        f'<div>{html.escape(str(row.get("timestamp") or "N/A"))} | '
        f'{html.escape(str(row.get("event_type") or "N/A"))} | '
        f'{html.escape(str(row.get("reason") or "N/A"))}</div>'
        for row in events[:80]
    ) or "No paper events in this snapshot"
    metrics = "".join((
        _ati_metric("Executor", health.get("status") or "DEGRADED", "atiPaperExecutor"),
        _ati_metric("Account", account.get("account_id") or "ATI_PAPER_50", "atiPaperAccount"),
        _ati_metric("Initial USDT", account.get("initial_balance"), "atiPaperInitial"),
        _ati_metric("Cash", account.get("cash_balance"), "atiPaperCash"),
        _ati_metric("Realized equity", account.get("realized_equity"), "atiPaperRealized"),
        _ati_metric("Total equity", account.get("total_equity"), "atiPaperTotal"),
        _ati_metric("Unrealized PnL", account.get("unrealized_pnl"), "atiPaperUnrealized"),
        _ati_metric("Realized PnL", account.get("realized_pnl_total"), "atiPaperRealizedPnl"),
        _ati_metric("Daily PnL", account_wrap.get("daily_pnl"), "atiPaperDaily"),
        _ati_metric("Return", account_wrap.get("cumulative_return_pct"), "atiPaperReturn"),
        _ati_metric("Equity peak", account.get("equity_peak"), "atiPaperPeak"),
        _ati_metric("Current DD", account.get("drawdown_pct"), "atiPaperDrawdown"),
        _ati_metric("Max DD", account.get("max_drawdown_pct"), "atiPaperMaxDrawdown"),
        _ati_metric("Open exposure", account_wrap.get("open_exposure"), "atiPaperExposure"),
        _ati_metric("Sizing", sizing.get("method") or "realized_equity_fraction", "atiPaperSizing"),
        _ati_metric("Sizing fraction", sizing.get("configured_position_fraction"), "atiPaperFraction"),
        _ati_metric("Trades", perf.get("total_trades", 0), "atiPaperTradesCount"),
        _ati_metric("Win rate", perf.get("win_rate"), "atiPaperWinRate"),
        _ati_metric("Profit factor", perf.get("profit_factor"), "atiPaperProfitFactor"),
        _ati_metric("Net EV", perf.get("net_ev_pct"), "atiPaperNetEv"),
        _ati_metric("Last heartbeat", health.get("last_heartbeat"), "atiPaperHeartbeat"),
        _ati_metric("Market age", health.get("market_data_age_seconds"), "atiPaperMarketAge"),
        _ati_metric("Policy", health.get("policy_version") or "ATI_PAPER_SIMULATION_V1", "atiPaperPolicy"),
        _ati_metric("Process start commit", health.get("commit_hash") or "N/A", "atiPaperCommit"),
    ))
    return f'''
<div class="ati-paper-shell" id="atiPaperPanel">
  <div class="ati-paper-head">
    <div><h3>ATI PAPER TRADING - 50 USDT SIMULADOS</h3>
      <div class="sub">Forward ATI V2 only. Persistent simulated ledger. No historical fills.</div></div>
    <div class="ati-paper-badges"><span>SIMULATION ONLY</span><span>PAPER_TRADING=True</span><span>DRY_RUN=True</span><span>NO LIVE</span><span>can_send_real_orders=false</span></div>
  </div>
  <div class="ati-paper-status" id="atiPaperPollStatus">Static snapshot; connecting to local read-only API...</div>
  <div class="ati-paper-metrics">{metrics}</div>
  <div class="ati-paper-charts">
    <div><h4>Public market + simulated levels</h4><canvas id="atiPaperMarketChart" width="900" height="300"></canvas></div>
    <div><h4>Equity / drawdown</h4><canvas id="atiPaperEquityChart" width="900" height="220"></canvas></div>
  </div>
  <div class="ati-paper-columns">
    <section><h4>Open positions</h4><div class="ati-paper-table-wrap"><table class="tbl"><thead><tr><th>Symbol</th><th>Side</th><th>Entry</th><th>Last</th><th>Stop</th><th>TP</th><th>Qty</th><th>Notional</th><th>Net mark</th><th>Status</th></tr></thead><tbody id="atiPaperPositions">{position_rows}</tbody></table></div></section>
    <section><h4>Closed simulated trades</h4><div class="ati-paper-table-wrap"><table class="tbl"><thead><tr><th>Trade</th><th>UTC</th><th>Symbol</th><th>Side</th><th>Entry</th><th>Exit</th><th>Notional</th><th>Fees</th><th>Slip</th><th>Net</th><th>Reason</th></tr></thead><tbody id="atiPaperTrades">{trade_rows}</tbody></table></div></section>
  </div>
  <div class="ati-paper-columns">
    <section><h4>Audit feed</h4><div class="ati-paper-feed" id="atiPaperEvents">{event_rows}</div></section>
    <section><h4>Selected trade / policy contract</h4><pre class="ati-paper-detail" id="atiPaperTradeDetail">SIMULATION ONLY\nSizing: realized equity fraction\nUnrealized PnL is never compounded\nSTOP_BEFORE_TP\nFunding: UNKNOWN unless verified\nFINAL_RECOMMENDATION: NO LIVE</pre></section>
  </div>
</div>'''


def _panel_cross_venue(d: dict) -> str:
    snapshot = d.get("cross_venue") if isinstance(d.get("cross_venue"), dict) else {}
    health = snapshot.get("health") if isinstance(snapshot.get("health"), dict) else {}
    account = snapshot.get("account") if isinstance(snapshot.get("account"), dict) else {}
    venues = snapshot.get("venues") if isinstance(snapshot.get("venues"), list) else []
    signals = snapshot.get("signals") if isinstance(snapshot.get("signals"), list) else []
    positions = snapshot.get("positions") if isinstance(snapshot.get("positions"), list) else []
    trades = snapshot.get("trades") if isinstance(snapshot.get("trades"), list) else []
    providers = snapshot.get("providers") if isinstance(snapshot.get("providers"), dict) else {}
    leadlag = snapshot.get("leadlag") if isinstance(snapshot.get("leadlag"), dict) else {}
    counts = leadlag.get("evaluation_counts") if isinstance(leadlag.get("evaluation_counts"), dict) else {}
    episodes = leadlag.get("recent_episodes") if isinstance(leadlag.get("recent_episodes"), list) else []
    storage = snapshot.get("storage") if isinstance(snapshot.get("storage"), dict) else {}
    venue_rows = "".join(
        "<tr>" + "".join(f"<td>{html.escape(str(value if value is not None else 'N/A'))}</td>" for value in (
            row.get("venue"), row.get("symbol"), row.get("price"), row.get("best_bid"), row.get("best_ask"),
            row.get("spread_bps"), row.get("microprice"), row.get("book_imbalance_l1"),
            row.get("trade_events_1s"), row.get("funding_rate"), row.get("open_interest"),
            row.get("last_event_age_ms"), row.get("collector_status"), row.get("signal_eligible"),
        )) + "</tr>" for row in venues[:30]
    ) or '<tr><td colspan="14">WAITING FOR REAL PUBLIC EVENTS</td></tr>'
    signal_rows = "".join(
        "<tr>" + "".join(f"<td>{html.escape(str(value if value is not None else 'N/A'))}</td>" for value in (
            row.get("symbol"), row.get("direction"), ",".join(row.get("leader_venues") or []),
            row.get("expected_remaining_move_bps"), row.get("estimated_total_cost_bps"),
            row.get("unlevered_net_edge_bps"),
            json.dumps(row.get("estimated_cost_breakdown_bps") or {}, sort_keys=True),
            row.get("status"), row.get("rejection_reason"),
        )) + "</tr>" for row in signals[-20:]
    ) or '<tr><td colspan="9">WAITING FOR SIGNAL; NO ACTIVITY IS FORCED</td></tr>'
    episode_rows = _table_rows(episodes[-30:], (
        "episode_id", "symbol", "direction", "first_observed_at", "last_observed_at",
        "evaluations", "candidate_evaluations", "last_status", "last_rejection_reason",
    ), empty="WAITING FOR UNIQUE MARKET EPISODES")
    storage_rows = _table_rows(storage.get("venues") or [], (
        "venue", "stream_size_bytes", "stream_growth_bytes_per_hour_this_process",
        "rotation_state", "raw_compression", "derived_compaction_status", "collector_status",
    ), empty="STORAGE TELEMETRY NOT AVAILABLE")
    return f'''
<div class="cv-shell" id="crossVenuePanel">
  <div class="cv-head"><div><h3>CROSS-VENUE INTELLIGENCE - PAPER RESEARCH</h3>
    <div class="sub">Causal public-feed research. Local monotonic ordering. Exchange clocks are diagnostic only.</div></div>
    <div class="cv-badges"><span>SIMULATION ONLY</span><span>RESEARCH ONLY</span><span>NOT ACTIONABLE</span><span>NO LIVE</span></div></div>
  <div class="cv-status" id="cvPollStatus">Static snapshot; connecting to local read-only API...</div>
  <div class="cv-metrics">
    {_ati_metric("System", health.get("status") or "CONNECTING", "cvSystemStatus")}
    {_ati_metric("Active venues", providers.get("active_venue_count", 0), "cvVenueCount")}
    {_ati_metric("Active streams", providers.get("active_stream_count", len(venues)), "cvStreamCount")}
    {_ati_metric("Raw evaluations", counts.get("raw_evaluations", 0), "cvSignalCount")}
    {_ati_metric("Unique episodes", counts.get("unique_market_episodes", 0), "cvEpisodeCount")}
    {_ati_metric("Candidate signals", counts.get("candidate_signals", 0), "cvCandidateCount")}
    {_ati_metric("Open simulated", len(positions), "cvPositionCount")}
    {_ati_metric("Closed simulated", len(trades), "cvTradeCount")}
    {_ati_metric("Account", account.get("account_id") or "CROSS_VENUE_PAPER_50", "cvAccountId")}
    {_ati_metric("Cash USDT", account.get("cash"), "cvCash")}
    {_ati_metric("Equity USDT", account.get("total_equity"), "cvEquity")}
    {_ati_metric("Realized PnL", account.get("realized_pnl"), "cvRealized")}
    {_ati_metric("Fees", account.get("fees"), "cvFees")}
    {_ati_metric("Slippage", account.get("slippage"), "cvSlippage")}
    {_ati_metric("Max drawdown", account.get("max_drawdown_pct"), "cvMaxDrawdown")}
    {_ati_metric("Edge validated", "false", "cvEdgeValidated")}
  </div>
  <div class="cv-grid">
    <section><h4>Venue Matrix</h4><div class="cv-scroll"><table class="tbl"><thead><tr><th>Venue</th><th>Symbol</th><th>Price</th><th>Bid</th><th>Ask</th><th>Spread bps</th><th>Microprice</th><th>Book imbalance</th><th>Trades 1s</th><th>Funding</th><th>OI</th><th>Age ms</th><th>Feed</th><th>Eligible</th></tr></thead><tbody id="cvVenues">{venue_rows}</tbody></table></div></section>
    <section><h4>Synchronized normalized prices</h4><canvas id="cvPriceChart" width="900" height="270"></canvas><div class="sub">Receive-time aligned; each venue rebased to 100. No interpolation.</div></section>
    <section><h4>Order-flow / book pressure</h4><div class="cv-scroll"><table class="tbl"><thead><tr><th>Venue</th><th>Symbol</th><th>Trades 1s</th><th>Buy volume</th><th>Sell volume</th><th>Net aggressor</th><th>Book imbalance</th><th>Microprice</th></tr></thead><tbody id="cvOrderflow"><tr><td colspan="8">NEED_DATA</td></tr></tbody></table></div></section>
    <section><h4>Leader Board / validation</h4><div class="cv-scroll"><table class="tbl"><thead><tr><th>Venue</th><th>Symbol</th><th>Horizon</th><th>N</th><th>Continuation</th><th>Reversal</th><th>Status</th></tr></thead><tbody id="cvLeaders"><tr><td colspan="7">NEED_MORE_DATA</td></tr></tbody></table></div></section>
    <section><h4>Lead-lag horizon heatmap</h4><div class="cv-heatmap" id="cvHeatmap">NEED_MORE_DATA - no historical reliability is inferred</div></section>
    <section><h4>Raw evaluations: candidates and rejections</h4><div class="cv-scroll"><table class="tbl"><thead><tr><th>Symbol</th><th>Side</th><th>Leaders</th><th>Remaining bps</th><th>Cost bps</th><th>Net edge bps</th><th>Cost breakdown</th><th>Status</th><th>Reason</th></tr></thead><tbody id="cvSignals">{signal_rows}</tbody></table></div></section>
    <section><h4>Unique market episodes</h4><div class="cv-scroll"><table class="tbl"><thead><tr><th>Episode</th><th>Symbol</th><th>Side</th><th>First</th><th>Last</th><th>Evaluations</th><th>Candidates</th><th>Status</th><th>Reason</th></tr></thead><tbody id="cvEpisodes">{episode_rows}</tbody></table></div></section>
    <section><h4>CROSS_VENUE_PAPER_50</h4><div class="cv-scroll"><table class="tbl"><thead><tr><th>UTC</th><th>Symbol</th><th>Side</th><th>Net</th><th>Fees</th><th>Slippage</th><th>Exit</th></tr></thead><tbody id="cvTrades"><tr><td colspan="7">No simulated forward trades</td></tr></tbody></table></div></section>
    <section><h4>Paper equity curve</h4><canvas id="cvEquityChart" width="900" height="240"></canvas><div class="sub">Isolated simulated account. No ATI/P11 balance is read or modified.</div></section>
    <section><h4>Leverage Lab - same fill and path</h4><div class="cv-scroll"><table class="tbl"><thead><tr><th>x</th><th>N</th><th>PnL</th><th>Equity</th><th>Net EV</th><th>PF</th><th>DD</th><th>Liquidations</th><th>Status</th></tr></thead><tbody id="cvLeverage"><tr><td colspan="9">NEED_MORE_DATA</td></tr></tbody></table></div></section>
    <section><h4>Component health</h4><div class="cv-scroll"><table class="tbl"><thead><tr><th>Component</th><th>Status</th><th>Last age ms</th><th>Reconnects total/hour</th><th>Gaps</th><th>Recovery</th><th>Error</th></tr></thead><tbody id="cvHealth"><tr><td colspan="7">CONNECTING</td></tr></tbody></table></div></section>
    <section><h4>Append-only storage / derived compaction</h4><div class="cv-scroll"><table class="tbl"><thead><tr><th>Venue</th><th>Bytes</th><th>Growth/hour</th><th>Rotation</th><th>Raw compression</th><th>Derived compaction</th><th>Collector</th></tr></thead><tbody id="cvStorage">{storage_rows}</tbody></table></div><div class="sub">Raw JSONL is append-only. Parquet compaction is a separate optional job and never deletes raw evidence.</div></section>
    <section><h4>Activity feed</h4><div class="cv-activity" id="cvActivity">No simulated account events yet</div></section>
  </div>
  <div class="cv-foot"><span id="cvReconciliation">RECONCILIATION: waiting</span><span>Public feeds only</span><span>can_send_real_orders=false</span><span>FINAL_RECOMMENDATION: NO LIVE</span></div>
</div>'''


def _git_runtime_provenance(d: dict[str, Any]) -> str:
    root = CE._repo_root()
    def git(*args: str) -> str:
        try:
            return subprocess.run(
                ["git", *args], cwd=root, check=True, capture_output=True,
                text=True, timeout=2,
            ).stdout.strip()
        except Exception:
            return "UNKNOWN"
    return (
        A._kv("Repository HEAD", d.get("git_head") or git("rev-parse", "HEAD")) +
        A._kv("Git tree", git("rev-parse", "HEAD^{tree}")) +
        A._kv("Branch", git("branch", "--show-current")) +
        A._kv("Artifact generated", d.get("generated_at")) +
        A._kv("Fast watcher mode", "ARTIFACT_ONLY + LOCAL READ-ONLY API POLLING") +
        '<div class="sub">Static HTML contains the generation snapshot. ATI Paper and Cross-Venue tables poll local read-only APIs; historical research remains explicitly cached/stale.</div>'
    )


def _relationship_graph(d: dict[str, Any]) -> str:
    rest = (d.get("view") or {}).get("status") or "UNKNOWN"
    ws = (d.get("source_compare_3way") or {}).get("ws", {}).get("status") or "UNKNOWN"
    persistent = (d.get("persistent_continuity") or {}).get("verdict") or "UNKNOWN"
    ati = ((d.get("ati") or {}).get("health") or {}).get("status") or "UNKNOWN"
    paper = ((d.get("ati_paper") or {}).get("health") or {}).get("status") or "UNKNOWN"
    cross = ((d.get("cross_venue") or {}).get("health") or {}).get("status") or "UNKNOWN"
    nodes = (
        ("REST historical fragments", rest), ("Persistent public WS", persistent),
        ("Legacy WS artifact", ws), ("ATI Shadow", ati),
        ("ATI Paper simulation", paper), ("Cross-Venue public research", cross),
    )
    return '<div class="runtime-graph">' + ''.join(
        f'<div><strong>{html.escape(label)}</strong><span>{html.escape(str(status))}</span></div>'
        for label, status in nodes
    ) + '</div>'


def _p11_pick(snapshot: dict[str, Any], *paths: str) -> Any:
    """First present value across flat and nested snapshot schema spellings."""
    for path in paths:
        node: Any = snapshot
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                node = None
                break
            node = node[part]
        if node is not None and node != "":
            return node
    return None


def _p11_has_closed_sample(snapshot: dict[str, Any]) -> bool:
    value = _p11_pick(
        snapshot,
        "metrics.forward_closed_outcomes",
        "forward_closed_outcomes",
        "counts.forward_closed_outcomes",
        "closed_outcomes",
    )
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def _p11_sample_metric(snapshot: dict[str, Any], *paths: str) -> Any:
    """Economic statistics are N/A until at least one outcome is finalized."""
    return _p11_pick(snapshot, *paths) if _p11_has_closed_sample(snapshot) else None


def _p11_state_kind(value: Any) -> str:
    state = str(value or "").upper()
    if state in {"RUNNING", "HEALTHY", "PASS", "RECONCILED", "OBSERVER_CONNECTED"}:
        return "ok"
    if state.startswith("WAITING_") or state in {"STARTING", "STALE", "DEGRADED"}:
        return "warn"
    if state in {"ERROR", "FAILED", "FAIL", "INVALID", "UNRECONCILED",
                 "HALTED_FAIL_CLOSED", "WAITING_FOR_DATA_GAP"}:
        return "bad"
    return "muted"


def _panel_p11_forward_observer(d: dict) -> str:
    p = d.get("p11_short_forward_observer") or {}
    observer_status = _p11_pick(p, "observer_status", "status", "state")
    reconciliation = _p11_pick(
        p, "reconciliation.status", "reconciliation_status", "metrics.reconciliation_status")
    closed = _p11_pick(
        p, "metrics.forward_closed_outcomes", "forward_closed_outcomes",
        "counts.forward_closed_outcomes", "closed_outcomes")

    contract = (
        A._kv("Observer status", observer_status,
              _p11_state_kind(observer_status)) +
        A._kv("Symbol", _p11_pick(p, "identity.symbol", "contract.symbol", "symbol")) +
        A._kv("Venue", _p11_pick(p, "identity.venue", "contract.venue", "venue")) +
        A._kv("Timeframe", _p11_pick(p, "identity.timeframe", "contract.timeframe", "timeframe")) +
        A._kv("Hypothesis", _p11_pick(
            p, "identity.hypothesis", "identity.hypothesis_id", "contract.hypothesis",
            "hypothesis", "hypothesis_id")) +
        A._kv("Mode", _p11_pick(p, "identity.mode", "contract.mode", "mode")) +
        A._kv("Forward boundary", _p11_pick(
            p, "boundary.forward_start_timestamp", "forward_start_timestamp")) +
        A._kv("Last closed bar", _p11_pick(
            p, "checkpoint.last_closed_bar", "metrics.last_closed_bar", "last_closed_bar",
            "last_processed_bar")) +
        A._kv("Observer heartbeat", _p11_pick(
            p, "metrics.observer_heartbeat", "observer_heartbeat", "heartbeat")) +
        A._kv("Observer lag (s)", _p11_pick(
            p, "metrics.observer_lag_seconds", "observer_lag_seconds", "lag_seconds")) +
        A._kv("Orders allowed", False, "ok")
    )
    lifecycle = (
        A._kv("Forward opportunities", _p11_pick(
            p, "metrics.forward_opportunities", "forward_opportunities")) +
        A._kv("Forward signals", _p11_pick(p, "metrics.forward_signals", "forward_signals")) +
        A._kv("Forward rejections", _p11_pick(
            p, "metrics.forward_rejections", "forward_rejections")) +
        A._kv("Forward entries", _p11_pick(p, "metrics.forward_entries", "forward_entries")) +
        A._kv("Open positions", _p11_pick(
            p, "metrics.forward_open_positions", "forward_open_positions", "open_positions")) +
        A._kv("Closed outcomes", closed) +
        A._kv("Finalized labels", _p11_pick(
            p, "metrics.forward_finalized_labels", "forward_finalized_labels", "finalized_labels")) +
        A._kv("Forward n_raw", _p11_pick(p, "metrics.forward_n_raw", "forward_n_raw")) +
        A._kv("Forward n_eff", _p11_sample_metric(
            p, "metrics.forward_n_eff", "forward_n_eff", "n_eff")) +
        A._kv("Time exits", _p11_sample_metric(p, "metrics.time_exits", "time_exits")) +
        A._kv("Duplicate count", _p11_pick(
            p, "metrics.duplicate_count", "duplicate_count")) +
        A._kv("Orphan count", _p11_pick(p, "metrics.orphan_count", "orphan_count")) +
        A._kv("Reconciliation", reconciliation, _p11_state_kind(reconciliation)) +
        A._kv("Errors", _p11_pick(
            p, "heartbeat.last_error", "reconciliation.pending_errors", "errors",
            "pending_errors", "metrics.errors", "metrics.error_count", "last_error"))
    )
    economics = (
        A._kv("Gross PnL", _p11_sample_metric(p, "metrics.gross_pnl", "gross_pnl")) +
        A._kv("Net PnL", _p11_sample_metric(p, "metrics.net_pnl", "net_pnl")) +
        A._kv("Fees", _p11_sample_metric(p, "metrics.fees", "fees")) +
        A._kv("Spread", _p11_sample_metric(p, "metrics.spread", "spread")) +
        A._kv("Slippage", _p11_sample_metric(p, "metrics.slippage", "slippage")) +
        A._kv("Funding", _p11_sample_metric(p, "metrics.funding", "funding")) +
        A._kv("MFE", _p11_sample_metric(p, "metrics.mfe", "metrics.MFE", "mfe", "MFE")) +
        A._kv("MAE", _p11_sample_metric(p, "metrics.mae", "metrics.MAE", "mae", "MAE")) +
        A._kv("Win rate", _p11_sample_metric(p, "metrics.win_rate", "win_rate")) +
        A._kv("Payoff", _p11_sample_metric(p, "metrics.payoff", "payoff")) +
        A._kv("Profit factor", _p11_sample_metric(
            p, "metrics.profit_factor", "profit_factor"))
    )
    provenance = (
        A._kv("Snapshot available", p.get("_snapshot_available")) +
        A._kv("Snapshot age (s)", p.get("_snapshot_age_seconds")) +
        A._kv("Snapshot path", p.get("_snapshot_path")) +
        A._kv("Schema version", _p11_pick(p, "provenance.schema_version", "schema_version")) +
        A._kv("Code HEAD", _p11_pick(
            p, "provenance.code_head", "provenance.head", "code_head", "head")) +
        A._kv("Code tree", _p11_pick(
            p, "provenance.code_tree", "provenance.tree", "code_tree", "tree")) +
        A._kv("Policy fingerprint", _p11_pick(
            p, "provenance.policy_fingerprint", "policy_fingerprint")) +
        A._kv("Config hash", _p11_pick(p, "provenance.config_hash", "config_hash"))
    )
    return (
        '<div class="p11-metrics-grid">'
        f'<div><h4 class="p11-subhead">Contract / heartbeat</h4>{contract}</div>'
        f'<div><h4 class="p11-subhead">Lifecycle / reconciliation</h4>{lifecycle}</div>'
        f'<div><h4 class="p11-subhead">Closed-outcome economics</h4>{economics}</div>'
        f'<div><h4 class="p11-subhead">Frozen provenance</h4>{provenance}</div>'
        '</div><div class="sub">Read-only projection of observer_status.json. '
        'This dashboard does not import, start, reconcile or execute the observer. '
        'No outcome sample means economic metrics are N/A, not zero.</div>'
    )


def _p11_export_filename(snapshot: dict[str, Any], key: str, default: str) -> str:
    exports = snapshot.get("exports") if isinstance(snapshot.get("exports"), dict) else {}
    aliases = {
        "lifecycle_ledger": ("lifecycle_ledger", "ledger"),
        "outcomes": ("outcomes", "outcome_export"),
        "labels": ("labels", "label_export"),
        "reconciliation": ("reconciliation", "reconciliation_report"),
        "summary": ("summary", "short_summary"),
    }
    raw: Any = None
    for alias in aliases.get(key, (key,)):
        if alias in exports:
            raw = exports[alias]
            break
    if isinstance(raw, dict):
        raw = raw.get("filename") or raw.get("path")
    # Only a basename inside the fixed observer output is accepted.  The status
    # snapshot cannot turn the local dashboard into an arbitrary file browser.
    name = Path(str(raw)).name if raw else default
    return name or default


def _panel_p11_exports(d: dict) -> str:
    snapshot = d.get("p11_short_forward_observer") or {}
    out_dir = _p11_observer_output_dir()
    labels = {
        "lifecycle_ledger": "Lifecycle ledger",
        "outcomes": "Outcomes",
        "labels": "Labels",
        "reconciliation": "Reconciliation report",
        "summary": "Short summary",
    }
    items: list[str] = []
    for key, default in P11_OBSERVER_EXPORT_FILES.items():
        filename = _p11_export_filename(snapshot, key, default)
        path = out_dir / filename
        label = html.escape(labels[key])
        if path.is_file():
            href = html.escape(path.resolve().as_uri(), quote=True)
            safe_name = html.escape(filename, quote=True)
            items.append(
                f'<a class="p11-export-link" href="{href}" download="{safe_name}">'
                f'<strong>{label}</strong><span>{safe_name}</span></a>')
        else:
            items.append(
                f'<div class="p11-export-link missing"><strong>{label}</strong>'
                f'<span>N/A — not published ({html.escape(filename)})</span></div>')
    return (
        f'<div class="p11-export-grid">{"".join(items)}</div>'
        '<div class="sub">Local, read-only artifacts published by the observer. '
        'Missing files stay N/A; the dashboard never synthesizes or mutates them.</div>'
    )


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


def _panel_ai_copilot(d: dict) -> str:
    rd = CE._repo_root().joinpath("reports", "research", "v10_45_ai_copilot")
    cop = _read_json(rd / "ai_copilot_last_run_v10_45.json") or {}
    sim = _read_json(rd / "ai_simulated_trader_v10_45.json") or {}
    m = sim.get("metrics") or {}
    b = sim.get("baselines") or {}
    enabled = bool(cop or sim)
    return (
        A._kv("AI copilot", "ENABLED (research/sim only)" if enabled else "NOT_RUN",
              "warn" if enabled else "muted") +
        A._kv("Provider", sim.get("provider") or cop.get("provider") or "mock") +
        A._kv("Copilot last run", cop.get("ran_at") or "NOT_RUN") +
        A._kv("Ideas generated / rejected",
              f"{cop.get('ideas_generated', 0)} / {cop.get('ideas_rejected', 0)}") +
        A._kv("Sim last run", sim.get("ran_at") or "NOT_RUN") +
        A._kv("Sim decisions", sim.get("n_decisions")) +
        A._kv("Sim trades (ledger only)", m.get("n_trades")) +
        A._kv("Sim net_EV", m.get("net_EV")) +
        A._kv("Sim net_EV lower bound", m.get("net_EV_lower_bound")) +
        A._kv("Sim PF / maxDD", f"{m.get('profit_factor')} / {m.get('max_drawdown')}") +
        A._kv("Beats random / buy&hold",
              f"{b.get('beats_random')} / {b.get('beats_buy_hold')}") +
        A._kv("Sim verdict", sim.get("verdict") or "NOT_RUN",
              A._state_kind(sim.get("verdict") or "WAITING_DATA")) +
        A._kv("Dangerous outputs blocked", sim.get("n_dangerous_outputs")) +
        '<div class="sub">AI = research/simulation assistant ONLY. Decisions live in an '
        'isolated paper ledger; no orders, no keys, no live. Privacy: only public '
        'research summaries are ever sent to a provider.</div>')


def _panel_edge_discovery(d: dict) -> str:
    rd = CE._repo_root().joinpath("reports", "research", "v10_45_6_edge_discovery")
    s = _read_json(rd / "edge_discovery_summary_v10_45_6.json") or {}
    conn = _read_json(rd / "provider_connectivity_v10_45_1.json") or {}
    provs = ", ".join(f"{p.get('provider')}={'OK' if p.get('available') else 'DOWN'}"
                      for p in (conn.get("providers") or [])) or "NOT_RUN"
    counts = s.get("state_counts") or {}
    top = (s.get("top_candidates") or [{}])[0] if s.get("top_candidates") else {}
    hm = top.get("holdout_metrics") or {}
    sp = _read_json(rd / "sprint_summary_v10_45_6.json") or {}
    seal = _read_json(rd / "commit_seal_v10_45_6.json") or {}
    ptr = _read_json(rd / "CURRENT_OUTPUT_MANIFEST.json") or {}
    tf_rows = "".join(
        f'<div class="sub">{r.get("timeframe")}: funnel={r.get("funnel")} '
        f'holdout_accesses={r.get("holdout_accesses")}</div>'
        for r in (sp.get("runs") or []))
    return (
        A._kv("Engine last run", s.get("ran_at") or "NOT_RUN") +
        A._kv("Sprint / m_global",
              f"{sp.get('sprint_id')} / m={sp.get('m_global')} "
              f"[{sp.get('registry_state')}]") +
        A._kv("Commit / tree",
              f"{str(seal.get('repo_commit_head'))[:12]} / "
              f"{str(seal.get('git_tree_oid'))[:12]} · "
              f"dirty={seal.get('dirty_tracked_files')}") +
        A._kv("Output manifest",
              f"{str(ptr.get('output_manifest_id'))[:24]} · "
              f"sha={str(ptr.get('output_manifest_sha256'))[:12]}") +
        A._kv("Seal", "MATCH" if seal.get("match") else "NO_MATCH",
              A._state_kind("OK" if seal.get("match") else "WAITING_DATA")) +
        A._kv("Sprint verdict", (sp.get("verdict") or "N/A")[:80]) +
        tf_rows +
        A._kv("Providers", provs) +
        A._kv("Data", s.get("data_note") or "N/A") +
        A._kv("Hypotheses (proc + AI)",
              f"{s.get('hypotheses_total', 0)} ({s.get('procedural', 0)} + "
              f"{s.get('ai_generated', 0)})") +
        A._kv("Executed / dup / invalid",
              f"{s.get('executed', 0)} / {s.get('duplicates', 0)} / {s.get('invalid', 0)}") +
        A._kv("Funnel", s.get("funnel")) +
        A._kv("State counts", counts) +
        A._kv("Best (holdout)", top.get("strategy_id") or "NONE") +
        A._kv("Best state", top.get("state") or "N/A",
              A._state_kind(top.get("state") or "WAITING_DATA")) +
        A._kv("Best holdout EV / lb",
              f"{hm.get('net_EV')} / {hm.get('net_EV_lower_bound')}") +
        A._kv("Data quality pass", (s.get("data_quality") or {}).get("quality_pass")) +
        A._kv("Trials (multiple testing m)", s.get("n_trials_total")) +
        '<div class="sub">Judge = deterministic replay funnel with locked holdout; '
        'AI (Ollama/Groq/Gemini) only proposes/critiques. Execution proxies cap '
        'promotion at SHADOW. NO LIVE.</div>')


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
    base = base.replace("</style>", _P11_CSS + _ATI_PAPER_CSS + _CROSS_VENUE_CSS + "</style>", 1)
    extra = _EXTRA.format(
        provenance=_git_runtime_provenance(d),
        watch=_panel_watch(d), ati_paper=_panel_ati_paper(d), cross_venue=_panel_cross_venue(d),
        pws=_panel_persistent_ws(d), compare=_panel_compare(d),
        p11=_panel_p11_forward_observer(d),
        p11_exports=_panel_p11_exports(d),
        strategy=_panel_strategy(d), exits=_panel_exit(d),
        alpha=_panel_alpha_factory(d),
        ai=_panel_ai_copilot(d),
        edge=_panel_edge_discovery(d),
        lattice=_panel_lattice(d),
        graph=_relationship_graph(d),
        gen=html.escape(datetime.now(timezone.utc).isoformat()))
    marker = '<div class="foot">'
    base = base.replace(marker, extra + marker, 1) if marker in base else base.replace("</body>", extra + "</body>", 1)
    base = base.replace("</body>", _ATI_PAPER_JS + _CROSS_VENUE_JS + "</body>", 1)
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
  <div class="section-band">A. LIVE LOCAL RUNTIME TELEMETRY <span>read-only, public/local, no execution</span></div>
  <div class="card wide"><h3>Runtime Provenance</h3>{provenance}</div>
  <div class="card wide"><h3>Dashboard Auto Refresh</h3>{watch}</div>
  <div class="section-band">B. FORWARD SHADOW / PAPER SIMULATION <span>not actionable, no orders</span></div>
  <div class="card full ati-paper-card">{ati_paper}</div>
  <div class="card full cv-card">{cross_venue}</div>
  <div class="card full p11-panel"><h3>P11_SHORT FORWARD OBSERVER</h3>{p11}</div>
  <div class="card full p11-panel"><h3>Reports &amp; Exports — P11_SHORT</h3>{p11_exports}</div>
  <div class="section-band">C. CACHED / HISTORICAL RESEARCH ARTIFACTS <span>timestamps and staleness shown explicitly</span></div>
  <div class="card wide"><h3>Persistent WS Panel</h3>{pws}</div>
  <div class="card wide"><h3>REST fragments vs WS snapshots vs Persistent WS</h3>{compare}<div class="sub">REST and WS sources have different collection contracts; coverage percentages are not interchangeable.</div></div>
  <div class="card wide"><h3>Alpha Factory V10.44</h3>{alpha}</div>
  <div class="card wide"><h3>AI Research Co-Pilot V10.45</h3>{ai}</div>
  <div class="card wide"><h3>Multi-AI Edge Discovery V10.45.1</h3>{edge}</div>
  <div class="card"><h3>Strategy Lab Hardened</h3>{strategy}</div>
  <div class="card"><h3>Exit Optimization Panel</h3>{exits}</div>
  <div class="card"><h3>Probability Lattice</h3>{lattice}</div>
  <div class="card wide"><h3>Relationship Graph</h3>{graph}<div class="sub">No invented correlations. Alt symbols WAITING_DATA until collected.</div></div>
</div>
<div class="sub" style="text-align:center;margin-top:8px">V10.43C generated {gen} · RESEARCH_ONLY · NO LIVE</div>
"""


_P11_CSS = """
.section-band{grid-column:1/-1;border:1px solid #355060;background:#101a21;color:#d7e6f4;padding:10px 12px;font:700 12px ui-monospace,monospace;letter-spacing:.05em}
.section-band span{color:var(--muted);font-weight:400;letter-spacing:0;margin-left:8px}
.runtime-graph{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:7px}.runtime-graph div{border:1px solid var(--line);background:var(--panel2);padding:8px;border-radius:5px;min-width:0}.runtime-graph strong,.runtime-graph span{display:block;overflow-wrap:anywhere}.runtime-graph strong{font-size:10px;color:var(--muted);text-transform:uppercase}.runtime-graph span{margin-top:5px;font:600 11px ui-monospace,monospace;color:var(--txt)}
.p11-panel .kv .v{max-width:68%;overflow-wrap:anywhere;word-break:break-word;text-align:right}
.p11-metrics-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}
.p11-subhead{margin:0 0 7px;color:var(--txt);font-size:11px;letter-spacing:.08em;text-transform:uppercase}
.p11-export-grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:9px}
.p11-export-link{display:flex;min-width:0;flex-direction:column;gap:5px;padding:10px;border:1px solid var(--line);border-radius:8px;background:var(--panel2);color:var(--txt);text-decoration:none;overflow-wrap:anywhere;word-break:break-word}
.p11-export-link:hover{border-color:var(--accent)}
.p11-export-link span{color:var(--muted);font-size:11px}
.p11-export-link.missing{cursor:not-allowed;opacity:.72}
@media(max-width:900px){.p11-metrics-grid{grid-template-columns:1fr}.p11-export-grid{grid-template-columns:1fr}.p11-panel .kv .v{max-width:60%}.runtime-graph{grid-template-columns:1fr}.section-band span{display:block;margin:4px 0 0}}
"""


_ATI_PAPER_CSS = """
.ati-paper-card{grid-column:1/-1;padding:0!important;overflow:hidden}
.ati-paper-shell{padding:16px;min-width:0;background:#10151b}
.ati-paper-head{display:flex;align-items:flex-start;justify-content:space-between;gap:14px;border-bottom:1px solid var(--line);padding-bottom:12px}
.ati-paper-head h3,.ati-paper-shell h4{margin:0 0 6px;letter-spacing:0;color:var(--txt)}
.ati-paper-head h3{font-size:18px}.ati-paper-shell h4{font-size:11px;text-transform:uppercase;color:var(--muted)}
.ati-paper-badges{display:flex;flex-wrap:wrap;justify-content:flex-end;gap:6px}
.ati-paper-badges span{border:1px solid #ad3c45;background:#32171b;color:#ffb2b8;padding:5px 8px;border-radius:6px;font:700 10px ui-monospace,monospace}
.ati-paper-status{margin:10px 0;color:var(--muted);font-size:11px;overflow-wrap:anywhere}
.ati-paper-metrics{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:7px}
.ati-paper-metric{min-width:0;border:1px solid var(--line);background:var(--panel2);padding:8px;border-radius:6px}
.ati-paper-metric span{display:block;color:var(--muted);font-size:9px;text-transform:uppercase;margin-bottom:5px}
.ati-paper-metric strong{display:block;color:var(--txt);font:600 11px ui-monospace,monospace;white-space:normal;overflow-wrap:anywhere}
.ati-paper-charts,.ati-paper-columns{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin-top:14px}
.ati-paper-charts>div,.ati-paper-columns>section{min-width:0;border:1px solid var(--line);background:var(--panel2);padding:10px;border-radius:6px}
.ati-paper-charts canvas{display:block;width:100%;height:240px;background:#0b0f14;border:1px solid #25313d;border-radius:4px}
#atiPaperEquityChart{height:240px}.ati-paper-table-wrap{max-width:100%;overflow:auto}
.ati-paper-table-wrap .tbl{min-width:760px}.ati-paper-feed{max-height:260px;overflow:auto;font:11px ui-monospace,monospace}
.ati-paper-feed div{border-bottom:1px solid var(--line);padding:7px 2px;overflow-wrap:anywhere}
.ati-paper-detail{margin:0;min-height:170px;max-height:260px;overflow:auto;white-space:pre-wrap;color:#c8d7e8;background:#0b0f14;border:1px solid #25313d;padding:10px;border-radius:4px;font-size:11px}
@media(max-width:1100px){.ati-paper-metrics{grid-template-columns:repeat(4,minmax(0,1fr))}}
@media(max-width:760px){.ati-paper-head{display:block}.ati-paper-badges{justify-content:flex-start;margin-top:9px}.ati-paper-metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.ati-paper-charts,.ati-paper-columns{grid-template-columns:1fr}.ati-paper-charts canvas,#atiPaperEquityChart{height:210px}}
"""


_CROSS_VENUE_CSS = """
.cv-card{grid-column:1/-1;padding:0!important;overflow:hidden}.cv-shell{padding:16px;min-width:0;background:#0d1419}
.cv-head{display:flex;align-items:flex-start;justify-content:space-between;gap:14px;border-bottom:1px solid var(--line);padding-bottom:12px}
.cv-head h3,.cv-shell h4{margin:0 0 6px;letter-spacing:0}.cv-head h3{font-size:18px}.cv-shell h4{font-size:11px;text-transform:uppercase;color:var(--muted)}
.cv-badges{display:flex;flex-wrap:wrap;justify-content:flex-end;gap:6px}.cv-badges span{border:1px solid #b34c54;background:#35181d;color:#ffbac0;padding:5px 8px;border-radius:5px;font:700 10px ui-monospace,monospace}
.cv-status{margin:10px 0;color:var(--muted);font-size:11px;overflow-wrap:anywhere}.cv-metrics{display:grid;grid-template-columns:repeat(8,minmax(0,1fr));gap:7px}
.cv-metrics .ati-paper-metric{background:#111c23}.cv-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin-top:14px}
.cv-grid section{min-width:0;border:1px solid var(--line);background:#111a21;padding:10px;border-radius:6px}.cv-scroll{overflow:auto;max-width:100%;max-height:300px}
.cv-scroll .tbl{min-width:740px}.cv-grid canvas{display:block;width:100%;height:270px;background:#090f13;border:1px solid #273640;border-radius:4px}
.cv-heatmap{display:grid;grid-template-columns:repeat(auto-fit,minmax(115px,1fr));gap:6px;min-height:80px;color:var(--muted);font:11px ui-monospace,monospace}
.cv-heat-cell{border:1px solid #2a3945;background:#101920;padding:8px;border-radius:4px;overflow-wrap:anywhere}.cv-heat-cell[data-state="positive"]{border-color:#326c54;background:#10251d}.cv-heat-cell[data-state="negative"]{border-color:#76424a;background:#2a171b}
.cv-activity{min-height:130px;max-height:260px;overflow:auto;white-space:pre-wrap;color:#c8d7e8;background:#090f13;border:1px solid #273640;padding:9px;border-radius:4px;font:10px ui-monospace,monospace}
.cv-foot{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}.cv-foot span{border:1px solid var(--line);background:#111a21;padding:6px 8px;border-radius:4px;color:var(--muted);font:10px ui-monospace,monospace}
@media(max-width:1200px){.cv-metrics{grid-template-columns:repeat(4,minmax(0,1fr))}}@media(max-width:800px){.cv-head{display:block}.cv-badges{justify-content:flex-start;margin-top:9px}.cv-metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.cv-grid{grid-template-columns:1fr}.cv-grid canvas{height:220px}}
"""


_ATI_PAPER_JS = r"""
<script>
(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  if (!$('atiPaperPanel')) return;
  const safe = value => (value === null || value === undefined || value === '' ? 'N/A' : String(value));
  const num = (value, digits=4) => {
    const n = Number(value); return Number.isFinite(n) ? n.toFixed(digits) : 'N/A';
  };
  const set = (id, value) => { const el=$(id); if(el) el.textContent=safe(value); };
  const get = async path => {
    const response = await fetch(path, {cache:'no-store', credentials:'same-origin'});
    if (!response.ok) throw new Error(`${path} HTTP ${response.status}`);
    return response.json();
  };
  const escapeCell = value => { const td=document.createElement('td'); td.textContent=safe(value); return td; };
  function renderPositions(rows) {
    const body=$('atiPaperPositions'); body.textContent='';
    if(!rows.length){const tr=document.createElement('tr');const td=escapeCell('No open simulated positions');td.colSpan=10;tr.append(td);body.append(tr);return;}
    rows.forEach(p=>{const tr=document.createElement('tr');[
      p.symbol,p.direction,num(p.entry_reference_price),num(p.last_price),num(p.stop_price),
      num(p.take_profit_price),num(p.quantity,8),num(p.notional),num(p.estimated_net_pnl),
      `${num(p.mfe)} / ${num(p.mae)}`].forEach(v=>tr.append(escapeCell(v)));
      const show=()=>{$('atiPaperTradeDetail').textContent=JSON.stringify(p,null,2)};
      tr.tabIndex=0;tr.addEventListener('click',show);tr.addEventListener('keydown',e=>{if(e.key==='Enter')show()});body.append(tr);});
  }
  function renderTrades(rows) {
    const body=$('atiPaperTrades'); body.textContent='';
    if(!rows.length){const tr=document.createElement('tr');const td=escapeCell('No closed simulated trades in current ledger');td.colSpan=11;tr.append(td);body.append(tr);return;}
    rows.forEach(t=>{const tr=document.createElement('tr');tr.tabIndex=0;[
      String(t.trade_id||'').slice(0,12),t.exit_ts,t.symbol,t.direction,
      num(t.entry_reference_price),num(t.exit_reference_price),num(t.notional),
      num(t.fees),num(t.slippage),num(t.net_pnl),t.exit_reason].forEach(v=>tr.append(escapeCell(v)));
      const show=()=>{$('atiPaperTradeDetail').textContent=JSON.stringify(t,null,2)};
      tr.addEventListener('click',show);tr.addEventListener('keydown',e=>{if(e.key==='Enter')show()});body.append(tr);});
  }
  function renderEvents(rows) {
    const feed=$('atiPaperEvents');feed.textContent='';
    if(!rows.length){feed.textContent='No paper events yet';return;}
    rows.slice(0,80).forEach(e=>{const line=document.createElement('div');
      line.textContent=`${safe(e.timestamp)} | ${safe(e.event_type)} | ${safe(e.reason)}`;feed.append(line);});
  }
  function fit(canvas){const dpr=Math.max(1,Math.min(2,window.devicePixelRatio||1));const rect=canvas.getBoundingClientRect();
    const w=Math.max(320,Math.floor(rect.width*dpr)),h=Math.max(160,Math.floor(rect.height*dpr));
    if(canvas.width!==w||canvas.height!==h){canvas.width=w;canvas.height=h}return {w,h,dpr};}
  function line(ctx,x1,y1,x2,y2,color,width=1){ctx.strokeStyle=color;ctx.lineWidth=width;ctx.beginPath();ctx.moveTo(x1,y1);ctx.lineTo(x2,y2);ctx.stroke()}
  function drawMarket(payload){const canvas=$('atiPaperMarketChart');const {w,h}=fit(canvas),ctx=canvas.getContext('2d');ctx.clearRect(0,0,w,h);
    const bars=(payload.bars||[]).slice(-120);if(!bars.length){ctx.fillStyle='#8291a2';ctx.fillText('WAITING FOR PUBLIC CLOSED BARS',16,26);return;}
    let low=Math.min(...bars.map(b=>Number(b.low))),high=Math.max(...bars.map(b=>Number(b.high)));
    (payload.positions||[]).forEach(p=>{low=Math.min(low,Number(p.stop_price),Number(p.take_profit_price));high=Math.max(high,Number(p.stop_price),Number(p.take_profit_price))});
    const pad=Math.max((high-low)*.08,high*.0002);low-=pad;high+=pad;const y=v=>h-20-(Number(v)-low)/(high-low)*(h-38);const step=(w-24)/bars.length;
    line(ctx,12,h-20,w-8,h-20,'#293642');bars.forEach((b,i)=>{const x=12+i*step+step/2,o=y(b.open),c=y(b.close),hi=y(b.high),lo=y(b.low),up=Number(b.close)>=Number(b.open),color=up?'#4ecb8d':'#ef6672';line(ctx,x,hi,x,lo,color,1);ctx.fillStyle=color;ctx.fillRect(x-Math.max(1,step*.28),Math.min(o,c),Math.max(2,step*.56),Math.max(1,Math.abs(c-o)));});
    (payload.positions||[]).forEach(p=>{[[p.stop_price,'#ef6672','SL'],[p.take_profit_price,'#4ecb8d','TP'],[p.entry_reference_price,'#58a6ff','ENTRY'],[p.trailing_stop,'#d69e2e','TRAIL']].forEach(([v,c,label])=>{if(v===null||v===undefined)return;const yy=y(v);line(ctx,12,yy,w-8,yy,c,1.5);ctx.fillStyle=c;ctx.fillText(label,14,yy-3)});});}
  function drawEquity(rows){const canvas=$('atiPaperEquityChart');const {w,h}=fit(canvas),ctx=canvas.getContext('2d');ctx.clearRect(0,0,w,h);rows=(rows||[]).slice(-500);
    if(!rows.length){ctx.fillStyle='#8291a2';ctx.fillText('WAITING FOR EQUITY LEDGER',16,26);return;}const vals=rows.map(r=>Number(r.total_equity)).filter(Number.isFinite);let lo=Math.min(...vals),hi=Math.max(...vals);const pad=Math.max((hi-lo)*.12,.01);lo-=pad;hi+=pad;const y=v=>h-18-(Number(v)-lo)/(hi-lo)*(h-34),x=i=>12+i*(w-24)/Math.max(1,rows.length-1);ctx.strokeStyle='#58a6ff';ctx.lineWidth=2;ctx.beginPath();rows.forEach((r,i)=>{const yy=y(r.total_equity);i?ctx.lineTo(x(i),yy):ctx.moveTo(x(i),yy)});ctx.stroke();ctx.fillStyle='#8291a2';ctx.fillText(`${lo.toFixed(2)} - ${hi.toFixed(2)} USDT`,14,14);}
  async function refresh(){try{
    const [a,p,t,e,v,h,perf]=await Promise.all([get('/api/ati-paper/account'),get('/api/ati-paper/positions'),get('/api/ati-paper/trades'),get('/api/ati-paper/equity'),get('/api/ati-paper/events'),get('/api/ati-paper/health'),get('/api/ati-paper/performance')]);
    const ac=a.account||{},sz=a.sizing||{};set('atiPaperExecutor',h.status);set('atiPaperAccount',ac.account_id);set('atiPaperInitial',num(ac.initial_balance));set('atiPaperCash',num(ac.cash_balance));set('atiPaperRealized',num(ac.realized_equity));set('atiPaperTotal',num(ac.total_equity));set('atiPaperUnrealized',num(ac.unrealized_pnl));set('atiPaperRealizedPnl',num(ac.realized_pnl_total));set('atiPaperDaily',num(a.daily_pnl));set('atiPaperReturn',`${num(a.cumulative_return_pct,2)}%`);set('atiPaperPeak',num(ac.equity_peak));set('atiPaperDrawdown',`${num(Number(ac.drawdown_pct)*100,2)}%`);set('atiPaperMaxDrawdown',`${num(Number(ac.max_drawdown_pct)*100,2)}%`);set('atiPaperExposure',num(a.open_exposure));set('atiPaperSizing',sz.method);set('atiPaperFraction',num(sz.configured_position_fraction,4));set('atiPaperTradesCount',perf.total_trades);set('atiPaperWinRate',perf.win_rate===null?'N/A':`${num(Number(perf.win_rate)*100,2)}%`);set('atiPaperProfitFactor',num(perf.profit_factor));set('atiPaperNetEv',perf.net_ev_pct===null?'N/A':`${num(perf.net_ev_pct,3)}%`);set('atiPaperHeartbeat',h.last_heartbeat);set('atiPaperMarketAge',h.market_data_age_seconds===null?'N/A':`${num(h.market_data_age_seconds,1)}s`);set('atiPaperPolicy',h.policy_version);set('atiPaperCommit',String(h.commit_hash||'N/A').slice(0,12));renderPositions(p.positions||[]);renderTrades(t.trades||[]);renderEvents(v.events||[]);drawEquity(e.equity||[]);const symbol=(p.positions&&p.positions[0]&&p.positions[0].symbol)||'BTCUSDT';drawMarket(await get(`/api/ati-paper/chart?symbol=${encodeURIComponent(symbol)}`));$('atiPaperPollStatus').textContent=`LIVE LOCAL API | ${new Date().toISOString()} | ${h.status} | SIMULATION ONLY | NO LIVE`;
  }catch(err){$('atiPaperPollStatus').textContent=`LOCAL API UNAVAILABLE: ${err.message} | static snapshot retained | NO LIVE`;}}
  refresh();setInterval(refresh,5000);window.addEventListener('resize',()=>refresh());
})();
</script>
"""


_CROSS_VENUE_JS = r"""
<script>
(() => {
  "use strict";
  const $ = id => document.getElementById(id); if (!$('crossVenuePanel')) return;
  const get = async path => { const r=await fetch(path,{cache:'no-store',credentials:'same-origin'}); if(!r.ok) throw new Error(`${path} HTTP ${r.status}`); return r.json(); };
  const text = v => (v===null||v===undefined||v===''?'N/A':String(v));
  const num = (v,d=3) => { const n=Number(v); return Number.isFinite(n)?n.toFixed(d):'N/A'; };
  const set = (id,v) => { const el=$(id); if(el) el.textContent=text(v); };
  function rows(id,data,columns,empty){const body=$(id);body.textContent='';if(!data.length){const tr=document.createElement('tr'),td=document.createElement('td');td.colSpan=columns.length;td.textContent=empty;tr.append(td);body.append(tr);return;}data.forEach(item=>{const tr=document.createElement('tr');columns.forEach(fn=>{const td=document.createElement('td');td.textContent=text(fn(item));tr.append(td)});body.append(tr)});}
  function draw(series){const c=$('cvPriceChart'),ratio=window.devicePixelRatio||1,rect=c.getBoundingClientRect(),w=Math.max(320,rect.width),h=Math.max(190,rect.height);c.width=Math.round(w*ratio);c.height=Math.round(h*ratio);const x=c.getContext('2d');x.setTransform(ratio,0,0,ratio,0,0);x.clearRect(0,0,w,h);const colors={bitget:'#58a6ff',binance:'#f0b90b',bybit:'#f4a261',okx:'#dfe7ee',hyperliquid:'#4ecb8d'};let lines=[];Object.entries(series||{}).forEach(([symbol,venues])=>Object.entries(venues||{}).forEach(([venue,points])=>{if(!points.length)return;const first=Number(points[0].price);if(!Number.isFinite(first)||first<=0)return;lines.push({name:`${symbol} ${venue}`,venue,pts:points.map(p=>[Number(p.monotonic_ns),Number(p.price)/first*100]).filter(p=>p.every(Number.isFinite))})}));if(!lines.length){x.fillStyle='#8291a2';x.fillText('WAITING FOR SYNCHRONIZED PUBLIC EVENTS',16,26);return;}const times=lines.flatMap(l=>l.pts.map(p=>p[0])),vals=lines.flatMap(l=>l.pts.map(p=>p[1]));const t0=Math.min(...times),t1=Math.max(...times),lo=Math.min(...vals),hi=Math.max(...vals),px=t=>12+(t-t0)/Math.max(1,t1-t0)*(w-24),py=v=>h-24-(v-lo)/Math.max(.000001,hi-lo)*(h-44);lines.forEach((l,j)=>{x.strokeStyle=colors[l.venue]||['#b388ff','#f06292','#80cbc4'][j%3];x.lineWidth=1.5;x.beginPath();l.pts.forEach((p,i)=>i?x.lineTo(px(p[0]),py(p[1])):x.moveTo(px(p[0]),py(p[1])));x.stroke();x.fillStyle=x.strokeStyle;x.fillText(l.name,14+(j%3)*145,14+Math.floor(j/3)*13)});}
  function drawEquity(data){const c=$('cvEquityChart'),ratio=window.devicePixelRatio||1,rect=c.getBoundingClientRect(),w=Math.max(320,rect.width),h=Math.max(180,rect.height);c.width=Math.round(w*ratio);c.height=Math.round(h*ratio);const x=c.getContext('2d');x.setTransform(ratio,0,0,ratio,0,0);x.clearRect(0,0,w,h);const points=(data||[]).slice().reverse().map(r=>Number(r.total_equity)).filter(Number.isFinite);if(!points.length){x.fillStyle='#8291a2';x.fillText('WAITING FOR ISOLATED EQUITY LEDGER',16,26);return;}let lo=Math.min(...points),hi=Math.max(...points);const pad=Math.max((hi-lo)*.1,.01);lo-=pad;hi+=pad;const px=i=>12+i*(w-24)/Math.max(1,points.length-1),py=v=>h-18-(v-lo)/Math.max(.000001,hi-lo)*(h-36);x.strokeStyle='#58a6ff';x.lineWidth=2;x.beginPath();points.forEach((v,i)=>i?x.lineTo(px(i),py(v)):x.moveTo(px(i),py(v)));x.stroke();x.fillStyle='#8291a2';x.fillText(`${lo.toFixed(2)} - ${hi.toFixed(2)} simulated USDT`,14,14);}
  function renderHeatmap(data){const box=$('cvHeatmap');box.textContent='';if(!data.length){box.textContent='NEED_MORE_DATA - no historical reliability is inferred';return;}data.forEach(r=>{const cell=document.createElement('div');cell.className='cv-heat-cell';const mature=Number(r.sample_size)>=200;const probability=Number(r.continuation_probability);cell.dataset.state=mature&&Number.isFinite(probability)?(probability>.5?'positive':'negative'):'neutral';cell.textContent=`${text(r.symbol)} | ${text(r.venue)} | ${text(r.horizon_ms)}ms | N=${text(r.sample_size)} | cont=${num(r.continuation_probability)} | ${text(r.status)}`;box.append(cell);});}
  function renderActivity(data){const box=$('cvActivity');box.textContent='';if(!data.length){box.textContent='No simulated account events yet';return;}data.slice(0,80).forEach(r=>{const line=document.createElement('div');line.textContent=`${text(r.timestamp)} | ${text(r.event_type)} | ${text(r.correlation_id)}`;box.append(line);});}
  function renderHealth(payload){const combined=[];Object.values(payload.collectors||{}).forEach(r=>combined.push(r));Object.entries(payload.components||{}).forEach(([name,r])=>combined.push({component:name,...r}));rows('cvHealth',combined,[r=>r.component||r.venue,r=>r.status,r=>num(r.last_event_age_ms,1),r=>`${text(r.reconnect_count_total??r.reconnect_count)}/${text(r.reconnects_last_hour??r.reconnections_last_hour)}`,r=>r.gaps_total??r.gaps,r=>r.recovery_result,r=>r.last_error],'CONNECTING');}
  async function refresh(){try{
    const paths=['/status','/venues','/prices','/orderflow','/leadlag','/signals','/episodes','/account','/trades','/equity','/events','/leverage','/health','/storage'];
    const [status,venues,prices,flow,lead,signals,episodes,account,trades,equity,events,lev,health,storage]=await Promise.all(paths.map(p=>get('/api/cross-venue'+p)));
    const vr=venues.venues||[],of=flow.orderflow||[],sg=signals.signals||[],ep=episodes.episodes||[],tr=trades.trades||[],eq=equity.equity||[],ev=events.events||[],ac=account.account||{},leaders=(lead.leadlag||{}).leaderboard||[],sc=(lev.leverage||{}).scenarios||[],ct=status.counts||{};
    set('cvSystemStatus',health.status);set('cvVenueCount',ct.active_venues||0);set('cvStreamCount',ct.active_streams||vr.length);set('cvSignalCount',ct.raw_evaluations||0);set('cvEpisodeCount',ct.unique_market_episodes||0);set('cvCandidateCount',ct.candidate_signals||0);set('cvPositionCount',ct.positions||0);set('cvTradeCount',tr.length);set('cvAccountId',ac.account_id||'CROSS_VENUE_PAPER_50');set('cvCash',num(ac.cash));set('cvEquity',num(ac.total_equity));set('cvRealized',num(ac.realized_pnl));set('cvFees',num(ac.fees));set('cvSlippage',num(ac.slippage));set('cvMaxDrawdown',num(ac.max_drawdown_pct));set('cvEdgeValidated','false');
    rows('cvVenues',vr,[r=>r.venue,r=>r.symbol,r=>num(r.price),r=>num(r.best_bid),r=>num(r.best_ask),r=>num(r.spread_bps),r=>num(r.microprice),r=>num(r.book_imbalance_l1),r=>r.trade_events_1s,r=>num(r.funding_rate,6),r=>num(r.open_interest),r=>num(r.last_event_age_ms??r.receive_age_ms,1),r=>r.collector_status,r=>r.signal_eligible],'WAITING FOR REAL PUBLIC EVENTS');
    rows('cvOrderflow',of,[r=>r.venue,r=>r.symbol,r=>r.trade_events_1s,r=>num(r.buy_volume_1s),r=>num(r.sell_volume_1s),r=>num(r.net_aggressor_volume_1s),r=>num(r.book_imbalance_l1),r=>num(r.microprice)],'NEED_DATA');
    rows('cvLeaders',leaders,[r=>r.venue,r=>r.symbol,r=>`${r.horizon_ms}ms`,r=>r.sample_size,r=>num(r.continuation_probability),r=>num(r.reversal_probability),r=>r.status],'NEED_MORE_DATA');
    rows('cvSignals',sg.slice(-30),[r=>r.symbol,r=>r.direction,r=>(r.leader_venues||[]).join(','),r=>num(r.expected_remaining_move_bps),r=>num(r.estimated_total_cost_bps),r=>num(r.unlevered_net_edge_bps),r=>JSON.stringify(r.estimated_cost_breakdown_bps||{}),r=>r.status,r=>r.rejection_reason],'WAITING FOR SIGNAL; NO ACTIVITY IS FORCED');
    rows('cvEpisodes',ep,[r=>String(r.episode_id||'').slice(0,16),r=>r.symbol,r=>r.direction,r=>r.first_observed_at,r=>r.last_observed_at,r=>r.evaluations,r=>r.candidate_evaluations,r=>r.last_status,r=>r.last_rejection_reason],'WAITING FOR UNIQUE MARKET EPISODES');
    rows('cvTrades',tr,[r=>r.exit_ts,r=>r.symbol,r=>r.direction,r=>num(r.net_pnl),r=>num(r.fees),r=>num(r.slippage),r=>r.exit_reason],'No simulated forward trades');
    rows('cvLeverage',sc,[r=>`${r.leverage}x`,r=>r.trades,r=>num(r.pnl),r=>num(r.equity),r=>num(r.net_ev),r=>num(r.profit_factor),r=>num(r.max_drawdown_pct),r=>r.liquidations,r=>r.status],'NEED_MORE_DATA');
    rows('cvStorage',storage.venues||[],[r=>r.venue,r=>r.stream_size_bytes,r=>num(r.stream_growth_bytes_per_hour_this_process,1),r=>r.rotation_state,r=>r.raw_compression,r=>r.derived_compaction_status,r=>r.collector_status],'STORAGE TELEMETRY NOT AVAILABLE');
    draw(prices.normalized_price_series||{});drawEquity(eq);renderHeatmap(leaders);renderActivity(ev);renderHealth(health);set('cvReconciliation',`RECONCILIATION: ${text((status.reconciliation||{}).status)}`);$('cvPollStatus').textContent=`LOCAL READ-ONLY API | ${new Date().toISOString()} | ${health.status} | ${ct.active_venues||0} venues / ${ct.active_streams||0} streams | SIMULATION ONLY | NOT ACTIONABLE | NO LIVE`;
  }catch(err){$('cvPollStatus').textContent=`LOCAL API UNAVAILABLE: ${err.message} | static snapshot retained | NO LIVE`;}}
  refresh();setInterval(refresh,3000);window.addEventListener('resize',()=>refresh());
})();
</script>
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
        html_tmp = d / "index.html.tmp"
        html_tmp.write_text(render_html(data, auto_refresh_seconds=interval), encoding="utf-8")
        os.replace(html_tmp, d / "index.html")
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
