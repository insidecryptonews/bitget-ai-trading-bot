"""V8.2.9.5 — Signal Path Metrics Bridge + Real Outcome + Tournament Real
tests. All synthetic. No DB. No real OHLCV.
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
    final: float = -1.2,
    mfe: float = 0.3,
    mae: float = -1.5,
    barrier: str = "SL",
    status: str = "completed",
) -> dict[str, Any]:
    return {
        "observation_id": observation_id,
        "symbol": symbol,
        "timestamp": timestamp,
        "final_return_pct": final,
        "max_favorable_pct": mfe,
        "max_adverse_pct": mae,
        "first_barrier_hit": barrier,
        "bars_tracked": 10,
        "bars_to_mfe": 2,
        "bars_to_mae": 5,
        "status": status,
    }


# ---------------------------------------------------------------------------
# Bridge join
# ---------------------------------------------------------------------------

def test_bridge_joins_by_observation_id():
    from app.labs.signal_path_metrics_bridge_v8_2_9_5 import (
        JOIN_OBSERVATION_ID,
        PATH_FOUND,
        bridge_candidates,
    )
    cands = [_cand(observation_id=42, net=0.81)]
    paths = [_path(observation_id=42, final=-1.2, barrier="SL")]
    r = bridge_candidates(cands, paths)
    assert r.path_found_count == 1
    row = r.rows[0]
    assert row["path_join_method"] == JOIN_OBSERVATION_ID
    assert row["path_status"] == PATH_FOUND
    assert row["real_final_return_pct"] == pytest.approx(-1.2)


def test_bridge_refuses_ambiguous_timestamp_join():
    """Two path rows at the same (symbol, timestamp) and a candidate
    without observation_id → AMBIGUOUS, never a false match."""
    from app.labs.signal_path_metrics_bridge_v8_2_9_5 import (
        JOIN_AMBIGUOUS,
        PATH_AMBIGUOUS_JOIN,
        bridge_candidates,
    )
    ts = "2026-06-01T00:00:00+00:00"
    cands = [_cand(observation_id=None, signal_id=None, symbol="BTCUSDT", timestamp=ts)]
    paths = [
        _path(symbol="BTCUSDT", timestamp=ts, final=-1.0),
        _path(symbol="BTCUSDT", timestamp=ts, final=+0.5),
    ]
    r = bridge_candidates(cands, paths)
    row = r.rows[0]
    assert row["path_join_method"] == JOIN_AMBIGUOUS
    assert row["path_status"] == PATH_AMBIGUOUS_JOIN
    assert r.path_ambiguous_count == 1


def test_bridge_symbol_timestamp_fallback_only_when_unique():
    from app.labs.signal_path_metrics_bridge_v8_2_9_5 import (
        JOIN_SYMBOL_TIMESTAMP_UNIQUE,
        bridge_candidates,
    )
    ts = "2026-06-01T00:00:00+00:00"
    cands = [_cand(observation_id=None, signal_id=None, symbol="ETHUSDT", timestamp=ts)]
    paths = [_path(symbol="ETHUSDT", timestamp=ts, final=0.5, barrier="TP")]
    r = bridge_candidates(cands, paths)
    assert r.rows[0]["path_join_method"] == JOIN_SYMBOL_TIMESTAMP_UNIQUE
    assert r.path_found_count == 1


def test_bridge_computes_proxy_vs_real_delta():
    from app.labs.signal_path_metrics_bridge_v8_2_9_5 import bridge_candidates
    cands = [_cand(observation_id=7, net=0.81)]
    paths = [_path(observation_id=7, final=0.30, barrier="TP")]
    r = bridge_candidates(cands, paths)
    row = r.rows[0]
    assert row["proxy_vs_real_delta"] == pytest.approx(0.81 - 0.30)


def test_bridge_detects_sign_mismatch():
    from app.labs.signal_path_metrics_bridge_v8_2_9_5 import (
        MM_SIGN_MISMATCH,
        bridge_candidates,
    )
    # proxy says +0.81 (win), real says -1.2 (loss) → sign mismatch.
    cands = [_cand(observation_id=9, net=0.81)]
    paths = [_path(observation_id=9, final=-1.2, barrier="SL")]
    r = bridge_candidates(cands, paths)
    assert r.rows[0]["proxy_mismatch_type"] == MM_SIGN_MISMATCH
    assert r.proxy_sign_mismatch_count == 1


# ---------------------------------------------------------------------------
# Canonical real
# ---------------------------------------------------------------------------

def test_canonical_real_prefers_signal_path_metrics():
    from app.labs.canonical_outcome_real_v8_2_9_5 import (
        SOURCE_SIGNAL_PATH_METRICS,
        canonicalize_real,
    )
    cands = [_cand(observation_id=3, net=0.81)]
    paths = [_path(observation_id=3, final=-1.2, barrier="SL")]
    r = canonicalize_real(cands, paths)
    row = r.rows[0]
    assert row["canonical_source"] == SOURCE_SIGNAL_PATH_METRICS
    assert row["canonical_is_real"] is True
    assert row["canonical_net_pnl_est"] == pytest.approx(-1.2)
    assert row["canonical_win"] is False


def test_canonical_real_falls_to_proxy_with_warning():
    from app.labs.canonical_outcome_real_v8_2_9_5 import (
        SOURCE_BASELINE_PROXY,
        WARN_PROXY_ONLY,
        canonicalize_real,
    )
    # No path rows → proxy fallback, flagged not-for-edge-validation.
    cands = [_cand(observation_id=5, net=0.81)]
    r = canonicalize_real(cands, [])
    row = r.rows[0]
    assert row["canonical_source"] == SOURCE_BASELINE_PROXY
    assert row["canonical_is_real"] is False
    assert row["canonical_warning"] == WARN_PROXY_ONLY


def test_canonical_real_need_data_when_no_path_no_proxy():
    from app.labs.canonical_outcome_real_v8_2_9_5 import (
        SOURCE_NEED_DATA,
        canonicalize_real,
    )
    cands = [{"observation_id": 11, "symbol": "BTCUSDT",
              "timestamp": "2026-06-01T00:00:00+00:00", "side": "LONG"}]
    r = canonicalize_real(cands, [])
    row = r.rows[0]
    assert row["canonical_source"] == SOURCE_NEED_DATA
    assert row["canonical_is_real"] is False
    assert row["canonical_net_pnl_est"] is None


# ---------------------------------------------------------------------------
# Tournament real
# ---------------------------------------------------------------------------

def test_tournament_real_uses_canonical_net_pnl_est():
    """When real coverage is sufficient, the tournament scores
    canonical_net_pnl_est (real), not the proxy."""
    from app.labs.strategy_tournament_real_outcomes_v8_2_9_5 import (
        OUTCOME_FIELD_REAL,
        run_tournament_real,
    )
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    cands = []
    paths = []
    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT"]
    # 300 candidates, all with a REAL path. Proxy says +0.81 but REAL
    # says loss for most → tournament must reflect REAL (negative).
    for i in range(300):
        oid = 1000 + i
        ts = (start + timedelta(hours=i)).isoformat()
        cands.append(_cand(observation_id=oid, symbol=symbols[i % 5],
                           timestamp=ts, net=0.81))
        real = 0.81 if i % 5 == 0 else -0.75  # 20% win → negative EV
        paths.append(_path(observation_id=oid, final=real,
                          barrier="TP" if real > 0 else "SL"))
    assert OUTCOME_FIELD_REAL == "canonical_net_pnl_est"
    r = run_tournament_real(cands, paths)
    assert r.coverage_sufficient is True
    assert r.real_rows_used >= 40
    # The all-cohort strategy must be REJECT on real negative EV.
    by_name = {x["name"]: x for x in r.results}
    assert by_name["rebound_long_all"]["status"] == "REJECT"


def test_tournament_real_need_more_data_when_coverage_low():
    """No path rows → real coverage 0 → everything NEED_MORE_DATA."""
    from app.labs.strategy_tournament_real_outcomes_v8_2_9_5 import (
        run_tournament_real,
    )
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    cands = [
        _cand(observation_id=i, timestamp=(start + timedelta(hours=i)).isoformat())
        for i in range(100)
    ]
    r = run_tournament_real(cands, [])  # no paths
    assert r.coverage_sufficient is False
    assert r.tournament_real_status == "NEED_MORE_DATA"
    assert r.tournament_real_best_status == "NEED_MORE_DATA"
    assert r.paper_sandbox_candidates_real == 0


def test_tournament_real_does_not_use_ret_or_mfe_mae_as_feature():
    """The default suite's predicates declare only ex-ante features.
    Injecting ret_* / mfe / mae as a feature must raise in RC1's
    validate (used by the real wrapper too)."""
    from app.labs.strategy_tournament_rc1 import StrategySpec, run_tournament
    for bad_feature in ("ret_4h_pct", "mfe_pct_outcome", "mae_pct_outcome",
                        "barrier_result_outcome", "net_pnl_est"):
        spec = StrategySpec(
            name="leaky", side="LONG", logic="leak",
            entry_features=(bad_feature,), predicate=lambda r: True,
        )
        with pytest.raises(ValueError):
            run_tournament([], [spec])


def test_no_paper_sandbox_when_not_real():
    """A proxy-only canonical row must never be marked real, so the
    tournament cannot promote it. With proxy-only coverage the wrapper
    returns NEED_MORE_DATA."""
    from app.labs.canonical_outcome_real_v8_2_9_5 import canonicalize_real
    from app.labs.strategy_tournament_real_outcomes_v8_2_9_5 import (
        run_tournament_real,
    )
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    # All proxy (no paths) — canonical_is_real=False everywhere.
    cands = [
        _cand(observation_id=i, timestamp=(start + timedelta(hours=i)).isoformat(),
              net=0.81)
        for i in range(100)
    ]
    canon = canonicalize_real(cands, [])
    assert all(row["canonical_is_real"] is False for row in canon.rows)
    r = run_tournament_real(cands, [])
    assert r.paper_sandbox_candidates_real == 0
    assert r.tournament_real_status == "NEED_MORE_DATA"


# ---------------------------------------------------------------------------
# Export V8.2.9.5
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


def test_export_v8295_emits_new_csvs(tmp_path):
    from app.labs.research_export_v8_2_9 import export_research_v829
    rows = [_dataset_row(i) for i in range(60)]
    base = tmp_path / "v8295_csvs"
    export_research_v829(None, rows=rows, base_dir=base)
    for name in (
        "signal_path_metrics_bridge_v1.csv",
        "canonical_outcome_real_v1.csv",
        "strategy_tournament_real_outcomes_v1.csv",
    ):
        assert (base / name).exists(), f"missing {name}"


def test_export_v8295_manifest_v5(tmp_path):
    from app.labs.research_export_v8_2_9 import export_research_v829
    rows = [_dataset_row(i) for i in range(60)]
    base = tmp_path / "v8295_manifest"
    export_research_v829(None, rows=rows, base_dir=base)
    manifest = json.loads((base / "manifest_v1.json").read_text(encoding="utf-8"))
    assert manifest["version"] == "v8.2.9.v5"
    for key in (
        "signal_path_metrics_coverage_ratio",
        "path_found_count", "path_missing_count", "path_ambiguous_count",
        "proxy_sign_mismatch_ratio", "proxy_net_ev_avg",
        "real_net_ev_avg", "real_winrate",
        "canonical_real_ok_ratio",
        "tournament_real_status", "tournament_real_best_strategy",
        "tournament_real_best_status", "paper_sandbox_candidates_real",
    ):
        assert key in manifest, f"manifest missing {key}"
    # With db=None there is no real path → coverage 0 → NEED_MORE_DATA.
    assert manifest["signal_path_metrics_coverage_ratio"] == 0.0
    assert manifest["tournament_real_status"] == "NEED_MORE_DATA"


def test_export_v8295_summary_has_new_keys(tmp_path):
    from app.labs.research_export_v8_2_9 import export_research_v829
    rows = [_dataset_row(i) for i in range(60)]
    base = tmp_path / "v8295_summary"
    export_research_v829(None, rows=rows, base_dir=base)
    summary = (base / "research_v8_2_9_summary.txt").read_text(encoding="utf-8")
    for key in (
        "signal_path_metrics_coverage_ratio:",
        "path_found_count:", "path_missing_count:",
        "proxy_sign_mismatch_ratio:", "real_net_ev_avg:",
        "canonical_real_ok_ratio:", "tournament_real_status:",
        "paper_sandbox_candidates_real:",
        "final_recommendation: NO LIVE",
    ):
        assert key in summary, f"summary missing {key}"


def test_export_v8295_zip_only_allowed(tmp_path):
    from app.labs.research_export_v8_2_9 import export_research_v829
    rows = [_dataset_row(i) for i in range(40)]
    base = tmp_path / "v8295_zip"
    export_research_v829(None, rows=rows, base_dir=base)
    zip_path = base / "research_v8_2_9_exports.zip"
    assert zip_path.exists()
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
    for name in names:
        assert name.endswith((".csv", ".txt", ".json"))
    assert "signal_path_metrics_bridge_v1.csv" in names
    assert "canonical_outcome_real_v1.csv" in names
    assert "strategy_tournament_real_outcomes_v1.csv" in names


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_v8295_cli_commands_parse():
    from app.research_lab import build_argument_parser
    parser = build_argument_parser()
    for argv in [
        ["signal-path-bridge-v8295", "--hours", "168"],
        ["canonical-real-outcome-v8295", "--hours", "168"],
        ["strategy-tournament-real-v8295", "--hours", "168"],
        ["export-research-v8295", "--hours", "168"],
        ["research-pack-v8295", "--hours", "168"],
    ]:
        ns = parser.parse_args(argv)
        assert ns.command == argv[0]


def test_v8295_parser_no_duplicate_option_strings():
    from app.research_lab import build_argument_parser
    parser = build_argument_parser()
    counts: dict[str, int] = {}
    for action in parser._actions:
        for opt in action.option_strings or []:
            counts[opt] = counts.get(opt, 0) + 1
    duplicates = [opt for opt, c in counts.items() if c > 1]
    assert not duplicates, f"Duplicate option strings: {duplicates}"


# ---------------------------------------------------------------------------
# DB readers are read-only (no writes)
# ---------------------------------------------------------------------------

def test_db_readers_are_select_only():
    """The V8.2.9.5 readers must contain only SELECT — never INSERT /
    UPDATE / DELETE / CREATE / ALTER / DROP."""
    import inspect
    from app.database import Database
    for name in ("fetch_signal_path_metrics", "fetch_ohlcv_path_for_observation"):
        fn = getattr(Database, name)
        src = inspect.getsource(fn).upper()
        for forbidden in ("INSERT ", "UPDATE ", "DELETE ", "CREATE ", "ALTER ", "DROP "):
            assert forbidden not in src, f"{name} contains {forbidden.strip()}"
        assert "SELECT" in src


# ---------------------------------------------------------------------------
# Safety AST scan
# ---------------------------------------------------------------------------

V8295_MODULES = [
    "app.labs.signal_path_metrics_bridge_v8_2_9_5",
    "app.labs.canonical_outcome_real_v8_2_9_5",
    "app.labs.strategy_tournament_real_outcomes_v8_2_9_5",
]


def test_v8295_modules_have_no_forbidden_calls():
    forbidden = {
        "place_order", "set_leverage", "set_margin_mode",
        "private_get", "private_post",
    }
    for mod in V8295_MODULES:
        path = pathlib.Path(importlib.import_module(mod).__file__)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = getattr(fn, "attr", None) or getattr(fn, "id", None)
                assert name not in forbidden, f"{mod} calls {name}"


def test_v8295_modules_no_forbidden_true_assigns():
    forbidden = {
        "LIVE_TRADING", "ENABLE_PAPER_POLICY_FILTER",
        "can_send_real_orders", "allow_real_writes",
    }
    for mod in V8295_MODULES:
        path = pathlib.Path(importlib.import_module(mod).__file__)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    name = getattr(tgt, "id", None) or getattr(tgt, "attr", None)
                    if (
                        name in forbidden
                        and isinstance(node.value, ast.Constant)
                        and node.value.value is True
                    ):
                        raise AssertionError(f"{mod} {name}=True")


def test_v8295_reports_carry_no_live():
    from app.labs.canonical_outcome_real_v8_2_9_5 import CanonicalRealReport
    from app.labs.signal_path_metrics_bridge_v8_2_9_5 import BridgeReport
    from app.labs.strategy_tournament_real_outcomes_v8_2_9_5 import (
        TournamentRealReport,
    )
    for inst in [
        BridgeReport(hours=1, generated_at="t"),
        CanonicalRealReport(hours=1, generated_at="t"),
        TournamentRealReport(hours=1, generated_at="t"),
    ]:
        assert inst.research_only is True
        assert inst.paper_filter_enabled is False
        assert inst.can_send_real_orders is False
        assert inst.final_recommendation == "NO LIVE"
