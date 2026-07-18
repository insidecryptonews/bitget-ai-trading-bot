from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.labs import edge_research_review_v10_44 as EDGE
from app.labs import research_dashboard_v10_43c as DASH


def _iso(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000.0, timezone.utc).isoformat()


def _make_db(path: Path, rows: list[dict]) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE signals (signal_id TEXT PRIMARY KEY, symbol TEXT, direction TEXT, "
        "decision_ts TEXT, decision_monotonic_ns INTEGER, status TEXT, rejection_reason TEXT, "
        "payload_json TEXT, created_at TEXT)"
    )
    for row in rows:
        conn.execute(
            "INSERT INTO signals VALUES (?,?,?,?,?,?,?,?,?)",
            (
                row["signal_id"], row["symbol"], row["direction"], row["decision_ts"],
                row.get("decision_monotonic_ns", 1), row.get("status", "REJECTED_COSTS"),
                row.get("rejection_reason"), json.dumps(row["payload"]), row["decision_ts"],
            ),
        )
    conn.commit()
    conn.close()


def _signal(signal_id: str, episode: str, timestamp_ms: int, *, symbol: str = "BTCUSDT",
            side: str = "LONG", leaders: tuple[str, ...] = ("binance", "bybit")) -> dict:
    return {
        "signal_id": signal_id,
        "symbol": symbol,
        "direction": side,
        "decision_ts": _iso(timestamp_ms),
        "status": "REJECTED_COSTS",
        "payload": {
            "episode_id": episode,
            "leader_venues": list(leaders),
            "features": {
                "average_leader_move_bps": 10.0 if side == "LONG" else -10.0,
                "target_move_bps": 2.0 if side == "LONG" else -2.0,
                "spread_bps": 0.2,
            },
        },
    }


def _write_quotes(root: Path, symbol: str, rows: list[tuple[int, float, float]]) -> Path:
    path = root / "bitget" / "normalized" / symbol / "book_l1" / "2026-01-01" / "events.jsonl"
    path.parent.mkdir(parents=True)
    payloads = [
        {
            "event_id": f"q{index}", "event_type": "book_l1", "source_status": "OK",
            "canonical_symbol": symbol, "local_receive_wall_ms": timestamp,
            "best_bid": bid, "best_ask": ask,
        }
        for index, (timestamp, bid, ask) in enumerate(rows)
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in payloads), encoding="utf-8")
    return path


def test_review_uses_read_only_ledger_dedups_episode_and_blocks_promotion(tmp_path: Path) -> None:
    start = 1_767_225_600_000
    db = tmp_path / "ledger.sqlite"
    _make_db(db, [
        _signal("s1", "episode-1", start),
        _signal("s2", "episode-1", start + 20),
        _signal("single", "episode-2", start + 5_000, leaders=("binance",)),
    ])
    events = tmp_path / "events"
    _write_quotes(events, "BTCUSDT", [
        (start + 100, 100.00, 100.02),
        (start + 1000, 100.03, 100.05),
    ])
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    report = EDGE.run_edge_research_review(
        db_path=db, events_root=events, horizons_ms=(1000,), write_reports=False,
    )
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    assert report["coverage"]["raw_evaluations"] == 3
    assert report["coverage"]["consensus_evaluations"] == 2
    assert report["coverage"]["unique_consensus_episodes"] == 1
    assert report["coverage"]["duplicate_consensus_evaluations"] == 1
    assert report["coverage"]["filled_episodes"] == 1
    result = report["horizon_results"][0]
    assert result["gross_executable"]["mean_bps"] > 0
    assert result["REALISTIC_BASE"]["mean_bps"] < 0
    assert report["verdict"] == "REJECTED_CURRENT_SIGNAL_NO_EXECUTABLE_EDGE"
    assert report["paper_ready"] is False
    assert report["live_ready"] is False
    assert report["can_send_real_orders"] is False
    assert report["final_recommendation"] == "NO LIVE"


def test_outcome_is_prefix_stable_when_later_quote_changes(tmp_path: Path) -> None:
    start = 1_767_225_600_000
    db = tmp_path / "ledger.sqlite"
    _make_db(db, [_signal("s1", "episode-1", start)])
    events = tmp_path / "events"
    quote_path = _write_quotes(events, "BTCUSDT", [
        (start + 100, 100.00, 100.02),
        (start + 1000, 100.03, 100.05),
    ])
    first = EDGE.run_edge_research_review(
        db_path=db, events_root=events, horizons_ms=(1000,), write_reports=False,
    )
    with quote_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "event_id": "future", "event_type": "book_l1", "source_status": "OK",
            "canonical_symbol": "BTCUSDT", "local_receive_wall_ms": start + 5000,
            "best_bid": 150.0, "best_ask": 150.02,
        }) + "\n")
    second = EDGE.run_edge_research_review(
        db_path=db, events_root=events, horizons_ms=(1000,), write_reports=False,
    )
    assert first["horizon_results"] == second["horizon_results"]
    assert first["method"]["future_quotes"] == "COUNTERFACTUAL_OUTCOMES_ONLY_NOT_DECISION_FEATURES"


def test_long_short_executable_return_is_symmetric() -> None:
    start = 1_767_225_600_000
    episodes = [
        {**_signal("long", "long-episode", start), "episode_key": "long-episode",
         "decision_ms": start, "leaders": ["binance", "bybit"]},
        {**_signal("short", "short-episode", start, symbol="ETHUSDT", side="SHORT"),
         "episode_key": "short-episode", "decision_ms": start,
         "leaders": ["binance", "bybit"]},
    ]
    for row in episodes:
        row["payload"] = row.pop("payload")
    quotes = {
        "BTCUSDT": [(start + 100, 1, 99.99, 100.0), (start + 1000, 2, 100.99, 101.0)],
        "ETHUSDT": [(start + 100, 3, 100.0, 100.01), (start + 1000, 4, 99.0, 99.01)],
    }
    outcomes, missing = EDGE._outcomes(episodes, quotes, (1000,), 100)
    assert missing == 0
    assert outcomes[0]["gross_executable_bps"] == pytest.approx(
        outcomes[1]["gross_executable_bps"], rel=2e-3,
    )


def test_missing_ledger_is_honest_need_data(tmp_path: Path) -> None:
    report = EDGE.run_edge_research_review(
        db_path=tmp_path / "missing.sqlite", events_root=tmp_path,
        horizons_ms=(1000,), write_reports=False,
    )
    assert report["verdict"] == "NEED_DATA"
    assert report["coverage"]["filled_episodes"] == 0
    assert report["final_recommendation"] == "NO LIVE"


def test_dashboard_panel_labels_counterfactual_results_not_actionable(tmp_path: Path,
                                                                      monkeypatch) -> None:
    report_dir = tmp_path / "reports" / "research" / "edge_audit_v1044"
    report_dir.mkdir(parents=True)
    (report_dir / "edge_research_review_v10_44.json").write_text(json.dumps({
        "verdict": "REJECTED_CURRENT_SIGNAL_NO_EXECUTABLE_EDGE",
        "generated_at": "2026-01-01T00:00:00+00:00",
        "coverage": {"raw_evaluations": 20, "consensus_evaluations": 4,
                     "unique_consensus_episodes": 2, "duplicate_consensus_evaluations": 2,
                     "filled_episodes": 2, "missing_fill_episodes": 0},
        "best_diagnostic": {"horizon_ms": 1000, "realistic_base_mean_bps": -15.0},
        "chronological_60_20_20_at_1000ms": {"status": "NEED_MORE_DATA"},
        "hypotheses": [{"status": "REJECTED"}],
    }), encoding="utf-8")
    monkeypatch.setattr(DASH.CE, "_repo_root", lambda: tmp_path)
    rendered = DASH._panel_edge_research_review({})
    assert "REJECTED_CURRENT_SIGNAL_NO_EXECUTABLE_EDGE" in rendered
    assert "Future quotes are counterfactual outcomes only" in rendered
    assert "No candidate promotion" in rendered
    assert "NO LIVE" in rendered
