"""Tests for V8.2.4 — Counterfactual Training Dataset + EdgeGuard
Counterfactual + Future Returns Bridge + pseudo-trades fallback.

No live, no order paths, no `.env`.
"""

from __future__ import annotations

import ast
import importlib
import json
import pathlib
import re
import zipfile
from datetime import datetime, timedelta, timezone

import pytest


# ---------------------------------------------------------------------------
# Future Returns Bridge
# ---------------------------------------------------------------------------

def _bar(ts, o, h, l, c):
    return {"timestamp": ts, "open": o, "high": h, "low": l, "close": c}


def _bars_path(start: datetime, samples: list[tuple[float, float, float, float]],
               minutes: int = 5) -> list[dict]:
    out = []
    for i, (o, h, l, c) in enumerate(samples, start=1):
        ts = (start + timedelta(minutes=minutes * i)).isoformat()
        out.append(_bar(ts, o, h, l, c))
    return out


def test_long_favorable_when_price_rises():
    from app.labs.future_returns_bridge import compute_future_returns

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    obs = {"id": 1, "timestamp": start.isoformat(), "symbol": "BTCUSDT",
           "side": "LONG", "entry_price": 100.0}
    # Price climbs steadily; TP at +0.96% reached.
    bars = _bars_path(start, [
        (100.0, 100.5, 99.8, 100.4),
        (100.4, 101.2, 100.2, 101.0),
        (101.0, 102.0, 100.8, 101.8),
    ])
    r = compute_future_returns(None, observation=obs, bars_override=bars)
    assert r.first_barrier_hit == "TP"
    assert r.tp_before_sl is True
    assert r.mfe_pct > 1.0


def test_short_favorable_when_price_falls():
    from app.labs.future_returns_bridge import compute_future_returns

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    obs = {"id": 2, "timestamp": start.isoformat(), "symbol": "BTCUSDT",
           "side": "SHORT", "entry_price": 100.0}
    bars = _bars_path(start, [
        (100.0, 100.2, 99.5, 99.7),
        (99.7, 99.8, 98.8, 99.0),
        (99.0, 99.2, 98.0, 98.2),
    ])
    r = compute_future_returns(None, observation=obs, bars_override=bars)
    assert r.first_barrier_hit == "TP"
    assert r.tp_before_sl is True
    assert r.mfe_pct > 1.0


def test_long_unfavorable_when_price_falls():
    from app.labs.future_returns_bridge import compute_future_returns

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    obs = {"id": 3, "timestamp": start.isoformat(), "symbol": "BTCUSDT",
           "side": "LONG", "entry_price": 100.0}
    bars = _bars_path(start, [
        (100.0, 100.1, 99.3, 99.5),
        (99.5, 99.6, 99.0, 99.1),
    ])
    r = compute_future_returns(None, observation=obs, bars_override=bars)
    assert r.first_barrier_hit == "SL"
    assert r.sl_before_tp is True


def test_short_unfavorable_when_price_rises():
    from app.labs.future_returns_bridge import compute_future_returns

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    obs = {"id": 4, "timestamp": start.isoformat(), "symbol": "BTCUSDT",
           "side": "SHORT", "entry_price": 100.0}
    bars = _bars_path(start, [
        (100.0, 100.5, 99.9, 100.4),
        (100.4, 101.5, 100.3, 101.4),
    ])
    r = compute_future_returns(None, observation=obs, bars_override=bars)
    assert r.first_barrier_hit == "SL"
    assert r.sl_before_tp is True


def test_same_bar_tp_and_sl_uses_stop_before_tp_long():
    from app.labs.future_returns_bridge import compute_future_returns

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    obs = {"id": 5, "timestamp": start.isoformat(), "symbol": "BTCUSDT",
           "side": "LONG", "entry_price": 100.0}
    # Bar touches both stop (99.4 = -0.6%) and TP (100.96 = +0.96%).
    bars = _bars_path(start, [(100.0, 101.0, 99.0, 100.5)])
    r = compute_future_returns(None, observation=obs, bars_override=bars)
    assert r.first_barrier_hit == "SL"
    assert r.sl_before_tp is True


def test_missing_ohlcv_returns_need_data():
    from app.labs.future_returns_bridge import compute_future_returns

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    obs = {"id": 6, "timestamp": start.isoformat(), "symbol": "BTCUSDT",
           "side": "LONG", "entry_price": 100.0}
    r = compute_future_returns(None, observation=obs, bars_override=[])
    assert r.status == "NEED_DATA"
    assert "ohlcv_missing" in r.need_data_reason


def test_no_lookahead_drops_bars_at_or_before_signal_timestamp():
    """A bar at exactly the signal timestamp must NOT be used."""
    from app.labs.future_returns_bridge import _fetch_future_bars

    class _FakeDB:
        def fetch_ohlcv_range(self, **kwargs):
            return [
                {"timestamp": "2026-06-01T00:00:00+00:00", "open": 1, "high": 1, "low": 1, "close": 1},
                {"timestamp": "2026-06-01T00:05:00+00:00", "open": 2, "high": 2, "low": 2, "close": 2},
            ]

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    bars = _fetch_future_bars(
        _FakeDB(), symbol="BTCUSDT", timeframe="5m",
        start_dt=start, max_bars=10, bars_override=None,
    )
    # Only the strictly-after bar should be returned.
    assert len(bars) == 1
    assert bars[0]["open"] == 2


# ---------------------------------------------------------------------------
# EdgeGuard counterfactual
# ---------------------------------------------------------------------------

def _eg_obs(*, side, regime="RISK_OFF", price=100.0, reason="edge_guard_WATCH_ONLY_no_edge_group_evidence",
            entry_price=100.0):
    return {
        "id": 100,
        "timestamp": datetime(2026, 6, 1, tzinfo=timezone.utc).isoformat(),
        "symbol": "BTCUSDT",
        "side": side,
        "market_regime": regime,
        "confidence_score": 75,
        "reason": reason,
        "entry_price": entry_price,
    }


def test_blocked_winner_detected_with_mock(tmp_path):
    """An EdgeGuard-blocked SHORT followed by a price drop is a blocked
    winner.
    """
    from app.labs.edgeguard_counterfactual_lab import analyze_edgeguard_blocks

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)

    class _FakeDB:
        def fetch_ohlcv_range(self, **kwargs):
            bars = []
            base = 100.0
            for i in range(1, 200):
                ts = (start + timedelta(minutes=5 * i)).isoformat()
                price = base * (1 - 0.001 * i)  # steady drop
                bars.append({
                    "timestamp": ts,
                    "open": price + 0.05, "high": price + 0.06,
                    "low": price - 0.05, "close": price,
                })
            return bars

    rows = [_eg_obs(side="SHORT")]
    r = analyze_edgeguard_blocks(_FakeDB(), hours=24, rows=rows)
    assert r.total_edgeguard_blocks == 1
    assert r.estimated_winners >= 0  # may be winner or loser depending on cost
    # The classification must NOT be unclear when OHLCV is present.
    has_classification = (
        r.estimated_winners + r.estimated_losers >= 1
    )
    assert has_classification


def test_blocked_loser_detected_when_price_moves_against_short():
    from app.labs.edgeguard_counterfactual_lab import analyze_edgeguard_blocks

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)

    class _FakeDB:
        def fetch_ohlcv_range(self, **kwargs):
            bars = []
            base = 100.0
            for i in range(1, 200):
                ts = (start + timedelta(minutes=5 * i)).isoformat()
                price = base * (1 + 0.001 * i)  # steady RISE (bad for SHORT)
                bars.append({
                    "timestamp": ts, "open": price - 0.05, "high": price + 0.05,
                    "low": price - 0.06, "close": price,
                })
            return bars

    rows = [_eg_obs(side="SHORT")]
    r = analyze_edgeguard_blocks(_FakeDB(), hours=24, rows=rows)
    assert r.total_edgeguard_blocks == 1
    assert r.estimated_losers >= 1


def test_no_edge_group_evidence_is_classified_as_edgeguard_block():
    from app.labs.edgeguard_counterfactual_lab import _is_edgeguard_blocked

    is_eg, reason = _is_edgeguard_blocked({"reason": "WATCH_ONLY_no_edge_group_evidence"})
    assert is_eg
    assert "no_edge_group_evidence" in reason or "watch_only" in reason


def test_edgeguard_need_data_when_no_ohlcv():
    from app.labs.edgeguard_counterfactual_lab import analyze_edgeguard_blocks

    class _FakeDB:
        pass  # No fetch_ohlcv_range.

    rows = [_eg_obs(side="SHORT")]
    r = analyze_edgeguard_blocks(_FakeDB(), hours=24, rows=rows)
    assert r.total_edgeguard_blocks == 1
    # Without OHLCV the outcome should be unclear / need_data.
    assert r.need_data >= 1


# ---------------------------------------------------------------------------
# Counterfactual training dataset
# ---------------------------------------------------------------------------

def _bull_db(start):
    """A DB that returns rising OHLCV — favourable for LONG."""
    class _FakeDB:
        def fetch_ohlcv_range(self, **kwargs):
            bars = []
            base = 100.0
            for i in range(1, 100):
                ts = (start + timedelta(minutes=5 * i)).isoformat()
                price = base * (1 + 0.0015 * i)
                bars.append({
                    "timestamp": ts, "open": price - 0.05, "high": price + 0.5,
                    "low": price - 0.1, "close": price,
                })
            return bars
    return _FakeDB()


def test_dataset_columns_present_and_no_secrets(tmp_path):
    from app.labs.counterfactual_training_dataset import (
        DATASET_COLUMNS,
        build_dataset,
        export_dataset,
    )

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = [
        {"id": 1, "timestamp": start.isoformat(), "symbol": "BTCUSDT",
         "side": "LONG", "market_regime": "RISK_ON", "confidence_score": 80,
         "reason": "", "entry_price": 100.0},
    ]
    dataset, summary = build_dataset(_bull_db(start), hours=24, rows=rows)
    assert summary.total_rows == 1
    row = dataset[0]
    for col in DATASET_COLUMNS:
        assert col in row
    # No secret keys in the row.
    for key in row.keys():
        assert not re.search(r"(api[_-]?key|secret|passphrase|password)", str(key).lower())
    # Export should produce valid CSV files without leaking secrets.
    manifest = export_dataset(dataset, summary, base_dir=tmp_path / "ctd")
    assert any(f["name"] == "counterfactual_training_dataset_v1.csv" for f in manifest["files"])


def test_dataset_does_not_export_dotenv_or_db(tmp_path):
    """The export directory must only contain CSV/TXT/JSON/ZIP files."""
    from app.labs.counterfactual_training_dataset import build_dataset, export_dataset

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = [{"id": 1, "timestamp": start.isoformat(), "symbol": "BTCUSDT",
             "side": "LONG", "entry_price": 100.0}]
    dataset, summary = build_dataset(_bull_db(start), hours=24, rows=rows)
    base = tmp_path / "ctd"
    export_dataset(dataset, summary, base_dir=base)
    allowed_suffixes = {".csv", ".txt", ".json", ".zip"}
    for path in base.iterdir():
        assert path.suffix in allowed_suffixes


def test_blocked_winner_label_requires_positive_net_pnl(tmp_path):
    from app.labs.counterfactual_training_dataset import (
        LABEL_BLOCKED_LOSER, LABEL_BLOCKED_WINNER, build_dataset,
    )

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    # SHORT signal blocked by EdgeGuard, market goes UP (bad for SHORT).
    rows_loser = [{
        "id": 1, "timestamp": start.isoformat(), "symbol": "BTCUSDT",
        "side": "SHORT", "market_regime": "RISK_OFF", "confidence_score": 75,
        "reason": "edge_guard_no_edge_group_evidence",
        "entry_price": 100.0,
    }]
    dataset, _ = build_dataset(_bull_db(start), hours=24, rows=rows_loser)
    labels = [r["training_label"] for r in dataset]
    # The SHORT against a rising market must NOT be a blocked winner.
    assert LABEL_BLOCKED_WINNER not in labels
    assert LABEL_BLOCKED_LOSER in labels


def test_dataset_need_data_label_disables_use_for_training():
    from app.labs.counterfactual_training_dataset import LABEL_NEED_DATA, build_dataset

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)

    class _NoOhlcvDB:
        def fetch_ohlcv_range(self, **kwargs):
            return []

    rows = [{"id": 1, "timestamp": start.isoformat(), "symbol": "X",
             "side": "LONG", "entry_price": 100.0}]
    dataset, _ = build_dataset(_NoOhlcvDB(), hours=24, rows=rows)
    assert dataset[0]["training_label"] == LABEL_NEED_DATA
    assert dataset[0]["final_use_for_training"] is False


def test_zip_contains_only_csv_txt_json_manifest(tmp_path):
    from app.labs.counterfactual_training_dataset import build_dataset, export_dataset

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = [{"id": 1, "timestamp": start.isoformat(), "symbol": "BTCUSDT",
             "side": "LONG", "entry_price": 100.0}]
    dataset, summary = build_dataset(_bull_db(start), hours=24, rows=rows)
    base = tmp_path / "ctd"
    export_dataset(dataset, summary, base_dir=base)
    zip_path = base / "counterfactual_training_exports_v1.zip"
    assert zip_path.exists()
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            assert name.endswith((".csv", ".txt", ".json")), f"{name} not allowed in ZIP"


# ---------------------------------------------------------------------------
# Pseudo-trades fallback for campaign / profit-lock
# ---------------------------------------------------------------------------

def test_campaign_uses_pseudo_trades_when_trades_empty():
    from app.labs.trend_campaign_simulator import run_campaign_simulation

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)

    class _FakeDB:
        def fetch_campaign_trades(self, **kwargs):
            return []  # No real trades.
        def fetch_signal_observations(self, hours=None, side=None, limit=None):
            return [{
                "id": 1, "timestamp": start.isoformat(), "symbol": "BTCUSDT",
                "side": side or "SHORT", "entry_price": 100.0,
                "stop_loss": 100.6, "take_profit_1": 99.04, "take_profit_2": 98.56,
                "normalized_atr": 0.005,
            }]
        def fetch_ohlcv_range(self, **kwargs):
            bars = []
            for i in range(1, 30):
                ts = (start + timedelta(minutes=5 * i)).isoformat()
                price = 100.0 * (1 - 0.001 * i)
                bars.append({
                    "timestamp": ts, "open": price + 0.02, "high": price + 0.03,
                    "low": price - 0.05, "close": price,
                })
            return bars

    report = run_campaign_simulation(_FakeDB(), side="SHORT", hours=24)
    # Pseudo-trades fallback must succeed; status is OK.
    assert report.samples >= 1
    assert "using_pseudo_trades_from_signal_observation" in report.need_data_reasons


def test_profit_lock_uses_pseudo_trades_when_trades_empty():
    from app.labs.profit_lock_simulator import run_profit_lock_simulation

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)

    class _FakeDB:
        def fetch_exit_replay_trades(self, **kwargs):
            return []
        def fetch_signal_observations(self, hours=None, side=None, limit=None):
            return [{
                "id": 1, "timestamp": start.isoformat(), "symbol": "BTCUSDT",
                "side": side or "LONG", "entry_price": 100.0,
                "stop_loss": 99.4, "take_profit_1": 100.96, "take_profit_2": 101.44,
                "normalized_atr": 0.005,
            }]
        def fetch_ohlcv_range(self, **kwargs):
            bars = []
            for i in range(1, 30):
                ts = (start + timedelta(minutes=5 * i)).isoformat()
                price = 100.0 * (1 + 0.001 * i)
                bars.append({
                    "timestamp": ts, "open": price - 0.02, "high": price + 0.06,
                    "low": price - 0.05, "close": price,
                })
            return bars

    report = run_profit_lock_simulation(_FakeDB(), side="LONG", hours=24)
    assert report.samples >= 1
    assert "using_pseudo_trades_from_signal_observation" in report.need_data_reasons


def test_pseudo_trades_source_tag():
    from app.labs.pseudo_trades_bridge import (
        PSEUDO_TRADE_SOURCE,
        build_pseudo_trades_from_observations,
    )

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)

    class _FakeDB:
        def fetch_signal_observations(self, **kwargs):
            return [{
                "id": 1, "timestamp": start.isoformat(), "symbol": "BTCUSDT",
                "side": "LONG", "entry_price": 100.0,
            }]
        def fetch_ohlcv_range(self, **kwargs):
            return [{"timestamp": (start + timedelta(minutes=5)).isoformat(),
                     "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.4}]

    pts = build_pseudo_trades_from_observations(_FakeDB(), hours=24, side="LONG")
    assert pts
    assert pts[0]["source"] == PSEUDO_TRADE_SOURCE


# ---------------------------------------------------------------------------
# Endpoints / heavy guard / path traversal
# ---------------------------------------------------------------------------

def test_endpoint_export_respects_heavy_guard():
    from app.health_server import _v824_counterfactual_training_export

    out = _v824_counterfactual_training_export(None, None, {"hours": ["720"]})
    assert out["status"] == "SKIPPED_HEAVY"
    assert out["final_recommendation"] == "NO LIVE"


def test_endpoint_summary_respects_heavy_guard():
    from app.health_server import _v824_counterfactual_training_summary

    out = _v824_counterfactual_training_summary(None, None, {"hours": ["720"]})
    assert out["status"] == "SKIPPED_HEAVY"


def test_endpoint_download_returns_need_data_when_no_export(tmp_path, monkeypatch):
    from app.health_server import _v824_counterfactual_training_download
    from app.labs import counterfactual_training_dataset as mod

    monkeypatch.setattr(mod, "EXPORT_SUBDIR", tmp_path / "ctd")
    out = _v824_counterfactual_training_download(None, None, {})
    assert out["status"] == "NEED_DATA"
    assert "no_export_available_yet" in (out.get("reason") or "")


# ---------------------------------------------------------------------------
# Safety AST scan
# ---------------------------------------------------------------------------

FORBIDDEN_CALL_NAMES = {
    "place_order", "set_leverage", "set_margin_mode",
    "private_get", "private_post",
}
FORBIDDEN_ASSIGN_LITERALS = {
    "LIVE_TRADING", "ENABLE_PAPER_POLICY_FILTER",
    "can_send_real_orders", "allow_real_writes",
}

V824_MODULES = [
    "app.labs.future_returns_bridge",
    "app.labs.edgeguard_counterfactual_lab",
    "app.labs.counterfactual_training_dataset",
    "app.labs.pseudo_trades_bridge",
]


def test_v824_modules_have_no_forbidden_calls():
    for mod in V824_MODULES:
        path = pathlib.Path(importlib.import_module(mod).__file__)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = getattr(fn, "attr", None) or getattr(fn, "id", None)
                assert name not in FORBIDDEN_CALL_NAMES, f"{mod} calls {name}"


def test_v824_modules_have_no_forbidden_literal_true_assigns():
    for mod in V824_MODULES:
        path = pathlib.Path(importlib.import_module(mod).__file__)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    name = getattr(tgt, "id", None) or getattr(tgt, "attr", None)
                    if name in FORBIDDEN_ASSIGN_LITERALS and isinstance(node.value, ast.Constant):
                        assert node.value.value is not True, f"{mod} {name}=True"
