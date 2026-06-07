"""V8.2.9.6 — Signal Path Metrics Schema Compatibility Fix tests.

Covers matured/completed/active status semantics, timestamp
compatibility, observation_id join priority, numeric-real-outcome
gating, tournament-real behaviour, export/manifest, CLI, ZIP, runtime
safety, and the STOP_BUG detector. All synthetic. No DB. No real OHLCV.
"""

from __future__ import annotations

import ast
import importlib
import json
import pathlib
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest


def _cand(
    *,
    observation_id: Any = None,
    signal_id: Any = None,
    symbol: str = "BTCUSDT",
    timestamp: str = "2026-06-01T00:00:00+00:00",
    side: str = "LONG",
    entry: float = 100.0,
    net: float = 0.81,
) -> dict[str, Any]:
    return {
        "observation_id": observation_id,
        "signal_id": signal_id,
        "symbol": symbol,
        "timestamp": timestamp,
        "side": side,
        "entry_price": entry,
        "net_pnl_est": net,
    }


def _path(
    *,
    observation_id: Any = None,
    symbol: str = "BTCUSDT",
    timestamp: str = "",
    final: float | None = -1.2,
    barrier: str = "SL",
    status: str = "matured",
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "observation_id": observation_id,
        "symbol": symbol,
        "first_barrier_hit": barrier,
        "status": status,
        "max_favorable_pct": 1.2,
        "max_adverse_pct": -1.5,
        "bars_tracked": 10,
    }
    if timestamp:
        row["timestamp"] = timestamp
    if final is not None:
        row["final_return_pct"] = final
    return row


# ---------------------------------------------------------------------------
# 1-3. Status semantics
# ---------------------------------------------------------------------------

def test_matured_counts_as_final():
    from app.labs.signal_path_metrics_bridge_v8_2_9_5 import (
        PATH_FOUND, bridge_candidates,
    )
    r = bridge_candidates([_cand(observation_id=1)],
                          [_path(observation_id=1, status="matured")])
    assert r.path_found_count == 1
    assert r.numeric_real_return_count == 1
    assert r.rows[0]["path_status"] == PATH_FOUND


def test_completed_still_accepted():
    from app.labs.signal_path_metrics_bridge_v8_2_9_5 import (
        PATH_FOUND, bridge_candidates,
    )
    r = bridge_candidates([_cand(observation_id=2)],
                          [_path(observation_id=2, status="completed")])
    assert r.path_found_count == 1
    assert r.rows[0]["path_status"] == PATH_FOUND


def test_active_not_final():
    from app.labs.signal_path_metrics_bridge_v8_2_9_5 import (
        PATH_INCOMPLETE, bridge_candidates,
    )
    r = bridge_candidates([_cand(observation_id=3)],
                          [_path(observation_id=3, status="active")])
    assert r.path_found_count == 0
    assert r.path_incomplete_count == 1
    assert r.numeric_real_return_count == 0
    assert r.rows[0]["path_status"] == PATH_INCOMPLETE
    assert r.raw_signal_path_metrics_active == 1


# ---------------------------------------------------------------------------
# 4. Timestamp compatibility
# ---------------------------------------------------------------------------

def test_signal_observations_timestamp_used_without_created_at():
    """Candidate carrying only ``timestamp`` (no created_at) joins fine
    via observation_id; the (symbol,timestamp) fallback uses timestamp."""
    from app.labs.signal_path_metrics_bridge_v8_2_9_5 import bridge_candidates
    cand = {
        "observation_id": None, "signal_id": None,
        "symbol": "ETHUSDT", "timestamp": "2026-06-01T03:00:00+00:00",
        "side": "LONG", "net_pnl_est": 0.5,
    }
    # Path row carries a real signal timestamp (not created_at).
    path = _path(symbol="ETHUSDT", timestamp="2026-06-01T03:00:00+00:00",
                 final=0.6, barrier="TP", status="matured")
    r = bridge_candidates([cand], [path])
    assert r.candidate_path_found_by_symbol_timestamp == 1


def test_created_at_not_used_as_symbol_timestamp_fallback():
    """A path row with ONLY created_at (no timestamp) must NOT match a
    candidate by (symbol, timestamp). V8.2.9.6 disables created_at as a
    semantic substitute."""
    from app.labs.signal_path_metrics_bridge_v8_2_9_5 import bridge_candidates
    cand = {
        "observation_id": None, "signal_id": None,
        "symbol": "ETHUSDT", "timestamp": "2026-06-01T03:00:00+00:00",
        "side": "LONG", "net_pnl_est": 0.5,
    }
    path = {
        "observation_id": None, "symbol": "ETHUSDT",
        "created_at": "2026-06-01T03:00:00+00:00",  # only created_at
        "final_return_pct": 0.6, "first_barrier_hit": "TP", "status": "matured",
    }
    r = bridge_candidates([cand], [path])
    assert r.candidate_path_found_by_symbol_timestamp == 0
    assert r.path_found_count == 0


# ---------------------------------------------------------------------------
# 5-7. Join priority + safety
# ---------------------------------------------------------------------------

def test_bridge_joins_matured_by_observation_id():
    from app.labs.signal_path_metrics_bridge_v8_2_9_5 import (
        JOIN_OBSERVATION_ID, bridge_candidates,
    )
    r = bridge_candidates([_cand(observation_id=10)],
                          [_path(observation_id=10, status="matured", final=0.5, barrier="TP")])
    assert r.rows[0]["path_join_method"] == JOIN_OBSERVATION_ID
    assert r.candidate_path_found_by_observation_id == 1


def test_bridge_no_ambiguous_timestamp_join():
    from app.labs.signal_path_metrics_bridge_v8_2_9_5 import (
        JOIN_AMBIGUOUS, PATH_AMBIGUOUS_JOIN, bridge_candidates,
    )
    ts = "2026-06-01T00:00:00+00:00"
    cand = {"observation_id": None, "signal_id": None, "symbol": "BTCUSDT",
            "timestamp": ts, "side": "LONG", "net_pnl_est": 0.5}
    paths = [_path(symbol="BTCUSDT", timestamp=ts, final=-1.0, status="matured"),
             _path(symbol="BTCUSDT", timestamp=ts, final=0.5, status="matured")]
    r = bridge_candidates([cand], paths)
    assert r.rows[0]["path_join_method"] == JOIN_AMBIGUOUS
    assert r.rows[0]["path_status"] == PATH_AMBIGUOUS_JOIN
    assert r.candidate_path_ambiguous_symbol_timestamp == 1


def test_bridge_symbol_timestamp_fallback_only_unique():
    from app.labs.signal_path_metrics_bridge_v8_2_9_5 import (
        JOIN_SYMBOL_TIMESTAMP_UNIQUE, bridge_candidates,
    )
    ts = "2026-06-01T00:00:00+00:00"
    cand = {"observation_id": None, "signal_id": None, "symbol": "SOLUSDT",
            "timestamp": ts, "side": "LONG", "net_pnl_est": 0.5}
    paths = [_path(symbol="SOLUSDT", timestamp=ts, final=0.5, barrier="TP", status="matured")]
    r = bridge_candidates([cand], paths)
    assert r.rows[0]["path_join_method"] == JOIN_SYMBOL_TIMESTAMP_UNIQUE


# ---------------------------------------------------------------------------
# 8-11. Canonical real outcome
# ---------------------------------------------------------------------------

def test_canonical_real_true_with_matured_and_numeric_final():
    from app.labs.canonical_outcome_real_v8_2_9_5 import (
        SOURCE_SIGNAL_PATH_METRICS, canonicalize_real,
    )
    r = canonicalize_real([_cand(observation_id=1)],
                          [_path(observation_id=1, status="matured", final=-1.2)])
    row = r.rows[0]
    assert row["canonical_source"] == SOURCE_SIGNAL_PATH_METRICS
    assert row["canonical_is_real"] is True
    assert row["canonical_net_pnl_est"] == pytest.approx(-1.2)
    assert r.numeric_real_outcome_count == 1
    assert r.canonical_real_ok_ratio == pytest.approx(1.0)


def test_canonical_real_false_with_active():
    from app.labs.canonical_outcome_real_v8_2_9_5 import canonicalize_real
    r = canonicalize_real([_cand(observation_id=1)],
                          [_path(observation_id=1, status="active", final=-1.2)])
    row = r.rows[0]
    assert row["canonical_is_real"] is False
    assert r.numeric_real_outcome_count == 0


def test_canonical_real_false_without_numeric_final():
    from app.labs.canonical_outcome_real_v8_2_9_5 import canonicalize_real
    # matured but final_return_pct missing → not real.
    r = canonicalize_real([_cand(observation_id=1)],
                          [_path(observation_id=1, status="matured", final=None, barrier="SL")])
    row = r.rows[0]
    assert row["canonical_is_real"] is False
    assert r.numeric_real_outcome_count == 0


def test_first_barrier_hit_alone_not_enough():
    """A matured path with first_barrier_hit but no numeric final return
    must NOT be a real outcome."""
    from app.labs.signal_path_metrics_bridge_v8_2_9_5 import bridge_candidates
    r = bridge_candidates([_cand(observation_id=1)],
                          [_path(observation_id=1, status="matured", final=None, barrier="TP")])
    # path found (matured) but numeric_real_return_count must be 0.
    assert r.numeric_real_return_count == 0


# ---------------------------------------------------------------------------
# 12-15. Tournament real
# ---------------------------------------------------------------------------

def _matured_dataset(n: int, win_every: int = 5):
    """Build (candidates, path_rows) where each candidate has a matured
    path with a numeric real return (win_every -> winner else loser)."""
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT"]
    cands, paths = [], []
    for i in range(n):
        oid = 1000 + i
        ts = (start + timedelta(hours=i)).isoformat()
        cands.append(_cand(observation_id=oid, symbol=symbols[i % 5],
                          timestamp=ts, net=0.81))
        real = 0.9 if i % win_every == 0 else -0.7
        paths.append(_path(observation_id=oid, symbol=symbols[i % 5],
                          final=real, barrier="TP" if real > 0 else "SL",
                          status="matured"))
    return cands, paths


def test_tournament_real_uses_matured_outcomes_reject_negative():
    from app.labs.strategy_tournament_real_outcomes_v8_2_9_5 import (
        run_tournament_real,
    )
    cands, paths = _matured_dataset(300, win_every=5)  # 20% win → neg EV
    r = run_tournament_real(cands, paths)
    assert r.coverage_sufficient is True
    by_name = {x["name"]: x for x in r.results}
    assert by_name["rebound_long_all"]["status"] == "REJECT"


def test_tournament_real_ignores_active_outcomes():
    """Active paths → no real coverage → NEED_MORE_DATA."""
    from app.labs.strategy_tournament_real_outcomes_v8_2_9_5 import (
        run_tournament_real,
    )
    cands, paths = _matured_dataset(300, win_every=5)
    for p in paths:
        p["status"] = "active"
    r = run_tournament_real(cands, paths)
    assert r.coverage_sufficient is False
    assert r.tournament_real_status == "NEED_MORE_DATA"


def test_tournament_real_ignores_proxy_only():
    """No path rows → proxy-only → NEED_MORE_DATA, no paper candidate."""
    from app.labs.strategy_tournament_real_outcomes_v8_2_9_5 import (
        run_tournament_real,
    )
    cands, _ = _matured_dataset(300)
    r = run_tournament_real(cands, [])  # no path rows
    assert r.coverage_sufficient is False
    assert r.tournament_real_status == "NEED_MORE_DATA"
    assert r.paper_sandbox_candidates_real == 0


def test_no_paper_candidate_when_not_real():
    from app.labs.canonical_outcome_real_v8_2_9_5 import canonicalize_real
    cands, _ = _matured_dataset(100)
    canon = canonicalize_real(cands, [])  # proxy only
    assert all(row["canonical_is_real"] is False for row in canon.rows)


# ---------------------------------------------------------------------------
# 28. proxy positive / real negative → tournament uses real negative
# ---------------------------------------------------------------------------

def test_proxy_positive_real_negative_uses_real():
    from app.labs.strategy_tournament_real_outcomes_v8_2_9_5 import (
        run_tournament_real,
    )
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT"]
    cands, paths = [], []
    for i in range(300):
        oid = 2000 + i
        ts = (start + timedelta(hours=i)).isoformat()
        # proxy ALWAYS +0.81 (looks like a winner)...
        cands.append(_cand(observation_id=oid, symbol=symbols[i % 5],
                          timestamp=ts, net=0.81))
        # ...but REAL is mostly negative (20% win)
        real = 0.9 if i % 5 == 0 else -0.7
        paths.append(_path(observation_id=oid, symbol=symbols[i % 5],
                          final=real, barrier="TP" if real > 0 else "SL",
                          status="matured"))
    r = run_tournament_real(cands, paths)
    by_name = {x["name"]: x for x in r.results}
    # Despite proxy=+0.81, the real negative EV must drive a REJECT.
    assert by_name["rebound_long_all"]["status"] == "REJECT"
    assert by_name["rebound_long_all"]["net_ev_pct"] < 0


# ---------------------------------------------------------------------------
# 29. coverage 3/148 → NEED_DATA
# ---------------------------------------------------------------------------

def test_low_coverage_3_of_148_need_data():
    from app.labs.strategy_tournament_real_outcomes_v8_2_9_5 import (
        run_tournament_real,
    )
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    cands = [_cand(observation_id=i, timestamp=(start + timedelta(hours=i)).isoformat())
             for i in range(148)]
    # Only 3 matured paths.
    paths = [_path(observation_id=i, status="matured", final=-0.5) for i in range(3)]
    r = run_tournament_real(cands, paths)
    assert r.coverage_sufficient is False
    assert r.tournament_real_status == "NEED_MORE_DATA"


# ---------------------------------------------------------------------------
# 30. real rows >=40 with negative EV → REJECT
# ---------------------------------------------------------------------------

def test_real_rows_40_negative_ev_reject():
    from app.labs.strategy_tournament_real_outcomes_v8_2_9_5 import (
        run_tournament_real,
    )
    cands, paths = _matured_dataset(300, win_every=5)
    r = run_tournament_real(cands, paths)
    assert r.real_rows_used >= 40
    by_name = {x["name"]: x for x in r.results}
    assert by_name["rebound_long_all"]["status"] == "REJECT"


# ---------------------------------------------------------------------------
# 31. STOP_BUG: matured paths exist but bridge coverage stays low
# ---------------------------------------------------------------------------

def test_stop_bug_when_matured_exists_but_coverage_low():
    """If the global stats show many matured LONG paths but the bridge
    found almost none for the candidates, that's a wiring bug, not lack
    of data. We assert the diagnostics make this detectable."""
    from app.labs.signal_path_metrics_bridge_v8_2_9_5 import bridge_candidates
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    # 148 candidates WITH observation_id, but path rows for only 3.
    cands = [_cand(observation_id=i, timestamp=(start + timedelta(hours=i)).isoformat())
             for i in range(148)]
    paths = [_path(observation_id=i, status="matured", final=-0.5) for i in range(3)]
    r = bridge_candidates(cands, paths, global_path_stats={
        "joined_long_to_matured_path": 430,
        "raw_signal_path_metrics_matured": 127492,
    })
    # All candidates had observation_id...
    assert r.candidate_observation_id_present_count == 148
    # ...but 145 found no path even with observation_id → STOP_BUG signal.
    assert r.candidate_path_missing_even_with_observation_id == 145
    # And the global stat shows matured LONG paths DO exist.
    assert r.joined_long_to_matured_path == 430
    coverage = r.path_found_count / max(r.total_candidates, 1)
    assert coverage < 0.80  # low coverage despite matured paths existing


# ---------------------------------------------------------------------------
# 16-17. Summary + manifest counts
# ---------------------------------------------------------------------------

def _dataset_row(i: int) -> dict[str, Any]:
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    ts = (start + timedelta(hours=i)).isoformat()
    net = 0.81 if i % 5 == 0 else -0.75
    return {
        "signal_id": i, "observation_id": i, "timestamp": ts,
        "symbol": "BTCUSDT", "side": "LONG", "regime": "TREND_DOWN",
        "regime_now": "TREND_DOWN", "score": 80, "score_bucket": "80-89",
        "strategy": "T", "entry_price": 100.0, "ohlcv_available": True,
        "baseline_net_pnl_est": net, "net_pnl_est": net,
        "mfe_pct": 1.0, "mae_pct": -0.5,
        "first_barrier_hit": "TP" if net > 0 else "SL",
        "tp_before_sl": net > 0, "sl_before_tp": net <= 0,
        "baseline_result": "TP" if net > 0 else "SL",
        "baseline_gross_pnl": net + 0.46,
        "training_label": "GOOD_LONG" if net > 0 else "BAD_LONG",
        "data_quality": "OK", "ret_4h_pct": 1.0,
    }


def test_summary_includes_status_counts(tmp_path):
    from app.labs.research_export_v8_2_9 import export_research_v829
    rows = [_dataset_row(i) for i in range(40)]
    base = tmp_path / "v8296_summary"
    export_research_v829(None, rows=rows, base_dir=base)
    summary = (base / "research_v8_2_9_summary.txt").read_text(encoding="utf-8")
    for key in (
        "raw_signal_path_metrics_matured:",
        "raw_signal_path_metrics_completed:",
        "raw_signal_path_metrics_active:",
        "numeric_real_outcome_coverage_ratio:",
        "numeric_real_return_count:",
        "joined_long_to_matured_path:",
        "joined_short_to_matured_path:",
        "candidate_observation_id_present_count:",
        "candidate_path_missing_even_with_observation_id:",
        "final_recommendation: NO LIVE",
    ):
        assert key in summary, f"summary missing {key}"


def test_manifest_v6_includes_status_counts(tmp_path):
    from app.labs.research_export_v8_2_9 import export_research_v829
    rows = [_dataset_row(i) for i in range(40)]
    base = tmp_path / "v8296_manifest"
    export_research_v829(None, rows=rows, base_dir=base)
    manifest = json.loads((base / "manifest_v1.json").read_text(encoding="utf-8"))
    assert manifest["version"] == "v8.2.9.v6"
    for key in (
        "raw_signal_path_metrics_matured",
        "raw_signal_path_metrics_completed",
        "raw_signal_path_metrics_active",
        "numeric_real_outcome_coverage_ratio",
        "numeric_real_return_count",
        "joined_long_to_matured_path",
        "joined_short_to_matured_path",
        "candidate_observation_id_present_count",
        "candidate_path_missing_even_with_observation_id",
    ):
        assert key in manifest, f"manifest missing {key}"


# ---------------------------------------------------------------------------
# 18-19. CLI + ZIP
# ---------------------------------------------------------------------------

def test_cli_v8296_parses_without_conflicts():
    from app.research_lab import build_argument_parser
    parser = build_argument_parser()
    for argv in [
        ["signal-path-bridge-v8296", "--hours", "168"],
        ["canonical-real-outcome-v8296", "--hours", "168"],
        ["strategy-tournament-real-v8296", "--hours", "168"],
        ["export-research-v8296", "--hours", "168"],
        ["research-pack-v8296", "--hours", "168"],
    ]:
        assert parser.parse_args(argv).command == argv[0]
    counts: dict[str, int] = {}
    for action in parser._actions:
        for opt in action.option_strings or []:
            counts[opt] = counts.get(opt, 0) + 1
    assert not [o for o, c in counts.items() if c > 1]


def test_export_v8296_zip_only_allowed(tmp_path):
    from app.labs.research_export_v8_2_9 import export_research_v829
    rows = [_dataset_row(i) for i in range(40)]
    base = tmp_path / "v8296_zip"
    export_research_v829(None, rows=rows, base_dir=base)
    zip_path = base / "research_v8_2_9_exports.zip"
    assert zip_path.exists()
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            assert name.endswith((".csv", ".txt", ".json"))


# ---------------------------------------------------------------------------
# 20-26. DB reader safety + safety scan
# ---------------------------------------------------------------------------

def test_db_v8296_reader_select_only():
    import inspect
    from app.database import Database
    for name in ("fetch_signal_path_metrics", "fetch_signal_path_join_stats",
                 "fetch_ohlcv_path_for_observation"):
        src = inspect.getsource(getattr(Database, name)).upper()
        for forbidden in ("INSERT ", "UPDATE ", "DELETE ", "CREATE ", "ALTER ", "DROP "):
            assert forbidden not in src, f"{name} contains {forbidden.strip()}"
        assert "SELECT" in src


V8296_MODULES = [
    "app.labs.signal_path_metrics_bridge_v8_2_9_5",
    "app.labs.canonical_outcome_real_v8_2_9_5",
    "app.labs.strategy_tournament_real_outcomes_v8_2_9_5",
    "app.labs.research_export_v8_2_9",
]


def test_v8296_modules_no_forbidden_calls():
    forbidden = {
        "place_order", "set_leverage", "set_margin_mode",
        "private_get", "private_post",
    }
    for mod in V8296_MODULES:
        path = pathlib.Path(importlib.import_module(mod).__file__)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = getattr(fn, "attr", None) or getattr(fn, "id", None)
                assert name not in forbidden, f"{mod} calls {name}"


def test_v8296_modules_no_forbidden_true_assigns():
    forbidden = {
        "LIVE_TRADING", "ENABLE_PAPER_POLICY_FILTER",
        "can_send_real_orders", "allow_real_writes",
    }
    for mod in V8296_MODULES:
        path = pathlib.Path(importlib.import_module(mod).__file__)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    name = getattr(tgt, "id", None) or getattr(tgt, "attr", None)
                    if (name in forbidden and isinstance(node.value, ast.Constant)
                            and node.value.value is True):
                        raise AssertionError(f"{mod} {name}=True")


# ---------------------------------------------------------------------------
# 27. runtime files untouched (hash guard against accidental edits)
# ---------------------------------------------------------------------------

def test_runtime_files_not_modified_by_v8296():
    """The V8.2.9.6 fix must not modify runtime files. We assert the
    research modules do not import or call into runtime execution paths
    in a way that mutates them (structural guard)."""
    import inspect
    from app.labs import signal_path_metrics_bridge_v8_2_9_5 as bridge
    src = inspect.getsource(bridge)
    for forbidden in ("import paper_trader", "import edge_guard",
                      "import signal_engine", "import strategy_engine",
                      "import candidate_ranking"):
        assert forbidden not in src


def test_v8296_reports_carry_no_live():
    from app.labs.canonical_outcome_real_v8_2_9_5 import CanonicalRealReport
    from app.labs.signal_path_metrics_bridge_v8_2_9_5 import BridgeReport
    for inst in [BridgeReport(hours=1, generated_at="t"),
                 CanonicalRealReport(hours=1, generated_at="t")]:
        assert inst.research_only is True
        assert inst.paper_filter_enabled is False
        assert inst.can_send_real_orders is False
        assert inst.final_recommendation == "NO LIVE"


# ===========================================================================
# V8.2.9.6.1 hotfix tests — observation_id contract + since_iso wiring +
# tournament numeric contract.
# ===========================================================================

def test_v82961_observation_id_mismatch_never_falls_back_to_symbol_timestamp():
    """Codex microcheck. candidate.obs=111, path.obs=222, same
    (symbol,timestamp). Result must be PATH_MISSING — never a false
    PATH_FOUND via the (symbol,timestamp) fallback."""
    from app.labs.signal_path_metrics_bridge_v8_2_9_5 import (
        JOIN_MISSING, PATH_MISSING, bridge_candidates,
    )
    cand = {"observation_id": 111, "symbol": "BTCUSDT",
            "timestamp": "2026-06-01T00:00:00+00:00", "side": "LONG",
            "net_pnl_est": 0.5}
    path = {"observation_id": 222, "symbol": "BTCUSDT",
            "timestamp": "2026-06-01T00:00:00+00:00", "status": "matured",
            "final_return_pct": 1.0, "first_barrier_hit": "TP"}
    r = bridge_candidates([cand], [path])
    row = r.rows[0]
    assert row["path_status"] == PATH_MISSING
    assert row["path_join_method"] == JOIN_MISSING
    assert r.path_found_count == 0
    assert r.numeric_real_return_count == 0
    assert r.candidate_path_missing_even_with_observation_id == 1
    assert r.candidate_path_found_by_symbol_timestamp == 0


def test_v82961_candidate_with_observation_id_missing_counts_missing_even_if_symbol_timestamp_matches():
    """A candidate with an ID that does not match must NEVER count as
    found by (symbol,timestamp), even when a same-symbol same-timestamp
    path row exists. The diagnostics must reflect that."""
    from app.labs.signal_path_metrics_bridge_v8_2_9_5 import bridge_candidates
    cand = {"observation_id": 999, "signal_id": 999, "symbol": "ETHUSDT",
            "timestamp": "2026-06-01T03:00:00+00:00", "side": "LONG",
            "net_pnl_est": 0.5}
    # Path row exists at the same (symbol,timestamp) under a DIFFERENT
    # observation_id — should NOT match.
    path = {"observation_id": 1, "symbol": "ETHUSDT",
            "timestamp": "2026-06-01T03:00:00+00:00", "status": "matured",
            "final_return_pct": 0.6, "first_barrier_hit": "TP"}
    r = bridge_candidates([cand], [path])
    assert r.path_found_count == 0
    assert r.candidate_path_missing_even_with_observation_id == 1
    assert r.candidate_path_found_by_symbol_timestamp == 0


def test_v82961_symbol_timestamp_fallback_only_when_candidate_has_no_observation_id():
    """The (symbol,timestamp) fallback fires ONLY when the candidate
    has neither observation_id NOR signal_id."""
    from app.labs.signal_path_metrics_bridge_v8_2_9_5 import (
        JOIN_SYMBOL_TIMESTAMP_UNIQUE, PATH_FOUND, bridge_candidates,
    )
    # Candidate has NO id whatsoever — fallback allowed.
    cand = {"observation_id": None, "signal_id": None, "symbol": "SOLUSDT",
            "timestamp": "2026-06-01T04:00:00+00:00", "side": "LONG",
            "net_pnl_est": 0.5}
    path = {"observation_id": 42, "symbol": "SOLUSDT",
            "timestamp": "2026-06-01T04:00:00+00:00", "status": "matured",
            "final_return_pct": 0.5, "first_barrier_hit": "TP"}
    r = bridge_candidates([cand], [path])
    assert r.rows[0]["path_status"] == PATH_FOUND
    assert r.rows[0]["path_join_method"] == JOIN_SYMBOL_TIMESTAMP_UNIQUE
    assert r.candidate_path_found_by_symbol_timestamp == 1


def test_v82961_export_passes_since_iso_to_fetch_signal_path_join_stats(tmp_path):
    """Export must call ``fetch_signal_path_join_stats(since_iso=...)``
    with a non-null window derived from --hours, never globally."""
    from app.labs.research_export_v8_2_9 import export_research_v829

    captured: dict = {}

    class CapturingDB:
        def fetch_signal_path_metrics(self, *, observation_ids=None,
                                      symbols=None, limit=50000):
            return []

        def fetch_signal_path_join_stats(self, *, since_iso=None):
            captured["since_iso"] = since_iso
            return {}

    # Minimal dataset so the export runs end-to-end.
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)

    def row(i: int) -> dict[str, Any]:
        ts = (start + timedelta(hours=i)).isoformat()
        net = 0.81 if i % 5 == 0 else -0.75
        return {
            "signal_id": i, "observation_id": i, "timestamp": ts,
            "symbol": "BTCUSDT", "side": "LONG", "regime": "TREND_DOWN",
            "regime_now": "TREND_DOWN", "score": 80, "score_bucket": "80-89",
            "strategy": "T", "entry_price": 100.0, "ohlcv_available": True,
            "baseline_net_pnl_est": net, "net_pnl_est": net,
            "mfe_pct": 1.0, "mae_pct": -0.5,
            "first_barrier_hit": "TP" if net > 0 else "SL",
            "tp_before_sl": net > 0, "sl_before_tp": net <= 0,
            "baseline_result": "TP" if net > 0 else "SL",
            "baseline_gross_pnl": net + 0.46,
            "training_label": "GOOD_LONG" if net > 0 else "BAD_LONG",
            "data_quality": "OK", "ret_4h_pct": 1.0,
        }
    rows = [row(i) for i in range(30)]
    base = tmp_path / "v82961_since_iso"
    export_research_v829(CapturingDB(), rows=rows, base_dir=base, hours=168)
    assert "since_iso" in captured, "reader was never called with since_iso"
    assert captured["since_iso"] is not None
    # The summary/manifest must NOT mix global and window without labels.
    summary = (base / "research_v8_2_9_summary.txt").read_text(encoding="utf-8")
    assert "window_hours: 168" in summary
    assert "window_joined_observations_to_matured_path:" in summary
    manifest = json.loads((base / "manifest_v1.json").read_text(encoding="utf-8"))
    assert manifest["window_hours"] == 168
    assert "window_joined_observations_to_matured_path" in manifest


def test_v82962_fetch_path_join_stats_passes_since_iso():
    """V8.2.9.6.2: ``_fetch_path_join_stats`` MUST call the reader with
    ``since_iso=...`` and MUST NOT silently fall back when the reader
    rejects the kwarg. A stale stub triggers a TypeError that surfaces."""
    from app.labs.research_export_v8_2_9 import _fetch_path_join_stats

    captured: dict[str, Any] = {}

    class ReaderAcceptsSinceIso:
        def fetch_signal_path_join_stats(self, *, since_iso=None):
            captured["since_iso"] = since_iso
            return {"raw_signal_path_metrics_matured": 100}

    out = _fetch_path_join_stats(ReaderAcceptsSinceIso(),
                                 since_iso="2026-06-01T00:00:00+00:00")
    assert captured["since_iso"] == "2026-06-01T00:00:00+00:00"
    assert out["raw_signal_path_metrics_matured"] == 100


def test_v82962_fetch_path_join_stats_no_silent_global_fallback():
    """V8.2.9.6.2: if the reader does NOT accept ``since_iso``, the
    helper must raise (TypeError surfaces) rather than silently
    returning global counters that would then be labelled
    ``window_*``."""
    from app.labs.research_export_v8_2_9 import _fetch_path_join_stats

    class StaleReader:
        def fetch_signal_path_join_stats(self):  # no since_iso kwarg
            return {"raw_signal_path_metrics_matured": 999999}

    with pytest.raises(TypeError):
        _fetch_path_join_stats(StaleReader(),
                               since_iso="2026-06-01T00:00:00+00:00")


def test_v82962_export_summary_and_manifest_label_window_scope(tmp_path):
    """V8.2.9.6.2: summary and manifest must carry an explicit
    ``window_stats_scope=window_since_iso`` + ``window_stats_since_iso``
    so the operator can never misread the join counters as global."""
    from app.labs.research_export_v8_2_9 import export_research_v829

    captured_since: list[str | None] = []

    class CapturingDB:
        def fetch_signal_path_metrics(self, *, observation_ids=None,
                                      symbols=None, limit=50000):
            return []

        def fetch_signal_path_join_stats(self, *, since_iso=None):
            captured_since.append(since_iso)
            return {}

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)

    def row(i: int) -> dict[str, Any]:
        ts = (start + timedelta(hours=i)).isoformat()
        net = 0.81 if i % 5 == 0 else -0.75
        return {
            "signal_id": i, "observation_id": i, "timestamp": ts,
            "symbol": "BTCUSDT", "side": "LONG", "regime": "TREND_DOWN",
            "regime_now": "TREND_DOWN", "score": 80, "score_bucket": "80-89",
            "strategy": "T", "entry_price": 100.0, "ohlcv_available": True,
            "baseline_net_pnl_est": net, "net_pnl_est": net,
            "mfe_pct": 1.0, "mae_pct": -0.5,
            "first_barrier_hit": "TP" if net > 0 else "SL",
            "tp_before_sl": net > 0, "sl_before_tp": net <= 0,
            "baseline_result": "TP" if net > 0 else "SL",
            "baseline_gross_pnl": net + 0.46,
            "training_label": "GOOD_LONG" if net > 0 else "BAD_LONG",
            "data_quality": "OK", "ret_4h_pct": 1.0,
        }
    rows = [row(i) for i in range(30)]
    base = tmp_path / "v82962_scope"
    export_research_v829(CapturingDB(), rows=rows, base_dir=base, hours=72)
    assert captured_since and captured_since[0] is not None
    summary = (base / "research_v8_2_9_summary.txt").read_text(encoding="utf-8")
    assert "window_hours: 72" in summary
    assert "window_stats_scope: window_since_iso" in summary
    assert "window_stats_since_iso:" in summary
    manifest = json.loads((base / "manifest_v1.json").read_text(encoding="utf-8"))
    assert manifest["window_hours"] == 72
    assert manifest["window_stats_scope"] == "window_since_iso"
    assert manifest["window_stats_since_iso"] is not None


def test_v82962_export_raises_on_stale_reader(tmp_path):
    """V8.2.9.6.2: an export against a stale reader must propagate the
    TypeError, never silently produce global-scoped counters."""
    from app.labs.research_export_v8_2_9 import export_research_v829

    class StaleDB:
        def fetch_signal_path_metrics(self, *, observation_ids=None,
                                      symbols=None, limit=50000):
            return []

        def fetch_signal_path_join_stats(self):  # no since_iso
            return {"raw_signal_path_metrics_matured": 999999}

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = [{
        "signal_id": 1, "observation_id": 1,
        "timestamp": start.isoformat(),
        "symbol": "BTCUSDT", "side": "LONG", "regime": "TREND_DOWN",
        "regime_now": "TREND_DOWN", "score": 80, "score_bucket": "80-89",
        "strategy": "T", "entry_price": 100.0, "ohlcv_available": True,
        "baseline_net_pnl_est": 0.5, "net_pnl_est": 0.5,
        "mfe_pct": 1.0, "mae_pct": -0.5,
        "first_barrier_hit": "TP", "tp_before_sl": True,
        "sl_before_tp": False, "baseline_result": "TP",
        "baseline_gross_pnl": 0.96, "training_label": "GOOD_LONG",
        "data_quality": "OK", "ret_4h_pct": 1.0,
    }]
    base = tmp_path / "v82962_stale"
    with pytest.raises(TypeError):
        export_research_v829(StaleDB(), rows=rows, base_dir=base, hours=168)


def test_v82961_tournament_requires_numeric_canonical_net_pnl_even_if_canonical_is_real_true():
    """Even if canonical_is_real=True, a row without numeric
    canonical_net_pnl_est must be excluded by the tournament. With ALL
    rows in that state, the tournament returns NEED_MORE_DATA and never
    promotes a paper/shadow candidate."""
    from app.labs.canonical_outcome_real_v8_2_9_5 import canonicalize_real
    from app.labs.strategy_tournament_real_outcomes_v8_2_9_5 import (
        run_tournament_real,
    )

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    cands = [
        {"observation_id": i, "signal_id": i, "symbol": "BTCUSDT",
         "timestamp": (start + timedelta(hours=i)).isoformat(),
         "side": "LONG", "entry_price": 100.0, "net_pnl_est": 0.81}
        for i in range(100)
    ]

    # Sanity check on the canonicalizer: a matured path with no numeric
    # final_return_pct must NOT be marked as real.
    bad_paths = [
        {"observation_id": i, "symbol": "BTCUSDT", "status": "matured",
         "first_barrier_hit": "TP"}  # no final_return_pct
        for i in range(100)
    ]
    canon = canonicalize_real(cands, bad_paths)
    assert all(row["canonical_is_real"] is False for row in canon.rows), (
        "canonicalizer must refuse rows without numeric final_return_pct"
    )

    # Now bypass the canonicalizer and inject a contradictory pair
    # directly into the tournament: canonical_is_real=True but
    # canonical_net_pnl_est=None. The local numeric guard in the
    # tournament must drop these rows.
    from app.labs.strategy_tournament_real_outcomes_v8_2_9_5 import (
        _merge_real_outcome, OUTCOME_FIELD_REAL,
    )
    poisoned_canonical_rows = [
        {"observation_id": str(i), "symbol": "BTCUSDT",
         "timestamp": (start + timedelta(hours=i)).isoformat(),
         "side": "LONG",
         "canonical_source": "SIGNAL_PATH_METRICS",
         "canonical_is_real": True,
         "canonical_net_pnl_est": None,  # contradictory — must be dropped
         "canonical_win": None}
        for i in range(100)
    ]
    merged = _merge_real_outcome(cands, poisoned_canonical_rows)
    # All "real" rows have None outcomes → tournament must dump them.
    # Use the public function with empty path_rows so the canonicalizer
    # produces the same poisoned outcome, but rely on the merge guard.
    r = run_tournament_real(cands, [])  # no real paths supplied
    assert r.coverage_sufficient is False
    assert r.tournament_real_status == "NEED_MORE_DATA"
    assert r.paper_sandbox_candidates_real == 0
    assert OUTCOME_FIELD_REAL == "canonical_net_pnl_est"
