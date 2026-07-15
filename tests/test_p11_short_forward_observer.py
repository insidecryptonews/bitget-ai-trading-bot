"""Operational contract tests for the continuous P11_SHORT observer.

All market bars are synthetic and all writes are confined to ``tmp_path``.
The suite exercises the forward lifecycle, the append-only store, restart and
fencing behaviour, causal boundary handling, reconciliation and the hard
research-only safety boundary.  No network or exchange client is used.
"""

from __future__ import annotations

import ast
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

import pytest

from app.labs import p11_short_forward_observer as observer_mod


I = observer_mod.INTERVAL_MS
T0 = 1_800_000_000_000  # exactly aligned to a 15-minute boundary
PROVENANCE = {"head": "a" * 40, "tree": "b" * 40}


def _bar(
    ts: int,
    *,
    open_: float = 100.0,
    high: float = 100.2,
    low: float = 99.8,
    close: float = 100.0,
    volume: float = 10.0,
) -> dict[str, Any]:
    return {
        "ts": int(ts),
        "open": float(open_),
        "high": float(high),
        "low": float(low),
        "close": float(close),
        "volume": float(volume),
    }


def _new_observer(
    path: Path,
    *,
    now_ms: int = T0,
    forward_start_ms: int = T0,
    lease_ttl_ms: int = 10_000,
) -> observer_mod.P11ShortForwardObserver:
    return observer_mod.P11ShortForwardObserver(
        output_dir=path,
        now_ms=now_ms,
        forward_start_ms=forward_start_ms,
        provenance=PROVENANCE,
        lease_ttl_ms=lease_ttl_ms,
    )


def _rows(
    observer: observer_mod.P11ShortForwardObserver,
    sql: str,
    params: Iterable[Any] = (),
) -> list[dict[str, Any]]:
    with observer.store.connect() as conn:
        return [dict(row) for row in conn.execute(sql, tuple(params)).fetchall()]


def _scalar(
    observer: observer_mod.P11ShortForwardObserver,
    sql: str,
    params: Iterable[Any] = (),
) -> Any:
    with observer.store.connect() as conn:
        return conn.execute(sql, tuple(params)).fetchone()[0]


def _actual_history(*, fires: bool) -> list[dict[str, Any]]:
    """59 bootstrap bars plus one boundary bar evaluated by real P11 code."""
    bars = [
        _bar(T0 - offset * I, high=100.1, low=99.9)
        for offset in range(59, 0, -1)
    ]
    if fires:
        # Largest recent true range, +1.5% return over 15 bars and an absolute
        # upper wick of 0.5: all three canonical P11 predicates are true.
        bars.append(
            _bar(T0, high=102.0, low=99.9, close=101.5, volume=10.0)
        )
    else:
        bars.append(_bar(T0, high=100.1, low=99.9, close=100.0))
    return bars


def _force_trade_only(
    monkeypatch: pytest.MonkeyPatch,
    observer: observer_mod.P11ShortForwardObserver,
    *signal_times: int,
) -> None:
    selected = {int(value) for value in signal_times}

    def decide(_history: list[dict[str, Any]], bar_ts: int):
        if int(bar_ts) in selected:
            signal = {
                "ok": True,
                "atr_pct": 1.0,
                "ret_15": 0.02,
                "upper_wick": 0.5,
                "slope": 0.001,
            }
            decision = {
                "decision_action": "TRADE",
                "side": "SHORT",
                "reason_codes": ["TRADE"],
                "calibrated_probability": 0.53,
                "regime": "TREND_UP",
            }
        else:
            signal = {
                "ok": True,
                "atr_pct": 0.5,
                "ret_15": 0.0,
                "upper_wick": 0.0,
                "slope": 0.0,
            }
            decision = {
                "decision_action": "ABSTAIN_LOW_REWARD",
                "side": "FLAT",
                "reason_codes": ["ABSTAIN_LOW_REWARD"],
                "calibrated_probability": 0.5,
                "regime": "TREND_DOWN",
            }
        return signal, decision

    monkeypatch.setattr(observer, "_signal_and_decision", decide)


def _complete_tp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    directory: str = "complete-tp",
) -> observer_mod.P11ShortForwardObserver:
    observer = _new_observer(tmp_path / directory)
    _force_trade_only(monkeypatch, observer, T0)
    observer.poll_once(now_ms=T0 + I, bars=[_bar(T0)])
    observer.poll_once(
        now_ms=T0 + 2 * I,
        bars=[_bar(T0 + I, high=100.2, low=98.7, close=99.0)],
    )
    return observer


def test_rejection_boundary_bootstrap_and_forming_bar_are_separated(tmp_path):
    observer = _new_observer(tmp_path / "boundary")
    forming = _bar(T0 + I, high=150.0, low=50.0, close=120.0)
    status = observer.poll_once(
        # T0 is closed; T0+I is still forming by one millisecond.
        now_ms=T0 + 2 * I - 1,
        bars=[*_actual_history(fires=False), forming],
    )

    lifecycles = _rows(observer, "SELECT * FROM p11_lifecycles")
    assert len(lifecycles) == 1
    assert lifecycles[0]["signal_bar_ms"] == T0
    assert lifecycles[0]["state"] == "REJECTED_FINAL"
    assert lifecycles[0]["canonical_signal"] == 0
    assert lifecycles[0]["rejection_reason"] == "P11_CONDITION_FALSE"

    bars = _rows(
        observer,
        "SELECT bar_open_ms,is_forward FROM p11_bars ORDER BY bar_open_ms",
    )
    assert len(bars) == 60
    assert sum(row["is_forward"] for row in bars) == 1
    assert all(row["bar_open_ms"] != T0 + I for row in bars)
    assert status["boundary"]["historical_snapshots_imported"] == 0
    assert status["boundary"]["bootstrap_is_feature_only"] is True
    assert status["reconciliation"]["status"] == "PASS"
    assert status["reconciliation"]["lhs_forward_opportunities"] == 1
    assert status["safety"]["paper_filter_enabled"] is False
    assert status["safety"]["can_send_real_orders"] is False
    assert status["safety"]["final_recommendation"] == "NO LIVE"
    assert status["final_recommendation"] == "NO LIVE"
    assert status["heartbeat"]["source_finality_lag_ms"] == 120_000


def test_integrated_real_p11_signal_to_tp_outcome_and_label(tmp_path):
    """Canonical P11, not a stub: 1 lifecycle -> entry -> TP -> outcome -> label."""
    observer = _new_observer(tmp_path / "integrated")
    first = observer.poll_once(
        now_ms=T0 + I,
        bars=_actual_history(fires=True),
    )
    assert first["metrics"]["forward_signals"] == 1
    assert first["metrics"]["forward_open_positions"] == 0
    assert _rows(observer, "SELECT state FROM p11_lifecycles")[0]["state"] == "ENTRY_PLANNED"

    final = observer.poll_once(
        now_ms=T0 + 2 * I,
        bars=[_bar(T0 + I, high=100.2, low=98.7, close=99.0)],
    )
    lifecycle = _rows(
        observer, "SELECT * FROM p11_lifecycles WHERE signal_bar_ms=?", (T0,)
    )[0]
    outcome = _rows(observer, "SELECT * FROM p11_outcomes")[0]
    label = _rows(observer, "SELECT * FROM p11_labels")[0]
    events = [
        row["event_type"]
        for row in _rows(
            observer,
            "SELECT event_type FROM p11_events WHERE lifecycle_id=? ORDER BY seq",
            (lifecycle["lifecycle_id"],),
        )
    ]

    assert lifecycle["state"] == "LABEL_FINALIZED"
    assert outcome["exit_reason"] == "TP"
    assert outcome["entry_bar_ms"] == T0 + I
    assert outcome["exit_bar_ms"] == T0 + I
    assert label["finalization_status"] == "FINAL"
    propagated_ids = (
        "opportunity_id", "signal_id", "candidate_trade_id",
        "underlying_trade_id", "hypothesis_id", "global_event_id",
        "dependency_cluster_id", "entry_bar_id", "exit_bar_id",
    )
    assert all(outcome[field] for field in propagated_ids)
    assert all(label[field] == outcome[field] for field in propagated_ids)
    finalized_events = _rows(
        observer,
        """SELECT event_type,finalization_status FROM p11_events
           WHERE lifecycle_id=? AND event_type IN ('OUTCOME_FINALIZED','LABEL_FINALIZED')
           ORDER BY seq""",
        (lifecycle["lifecycle_id"],),
    )
    assert [row["finalization_status"] for row in finalized_events] == ["FINAL", "FINAL"]
    assert events == [
        "SIGNAL_OBSERVED",
        "SIGNAL_ELIGIBLE",
        "SHADOW_ENTRY_PLANNED",
        "SHADOW_ENTRY_OPENED",
        "SHADOW_POSITION_UPDATED",
        "SHADOW_EXITED",
        "OUTCOME_FINALIZED",
        "LABEL_FINALIZED",
    ]
    assert final["metrics"]["forward_entries"] == 1
    assert final["metrics"]["forward_closed_outcomes"] == 1
    assert final["metrics"]["forward_finalized_labels"] == 1
    assert final["reconciliation"]["status"] == "PASS"
    assert final["safety"]["can_send_real_orders"] is False


def test_stop_loss_exit_is_finalized_once(tmp_path, monkeypatch):
    observer = _new_observer(tmp_path / "sl")
    _force_trade_only(monkeypatch, observer, T0)
    observer.poll_once(now_ms=T0 + I, bars=[_bar(T0)])
    observer.poll_once(
        now_ms=T0 + 2 * I,
        bars=[_bar(T0 + I, high=101.0, low=99.5, close=100.5)],
    )

    outcome = _rows(observer, "SELECT * FROM p11_outcomes")[0]
    assert outcome["exit_reason"] == "SL"
    assert outcome["exit_price"] == pytest.approx(100.8)
    assert _scalar(observer, "SELECT COUNT(*) FROM p11_outcomes") == 1
    assert _scalar(observer, "SELECT COUNT(*) FROM p11_labels") == 1


def test_time_exit_counts_entry_bar_and_closes_on_bar_15(tmp_path, monkeypatch):
    observer = _new_observer(tmp_path / "time")
    _force_trade_only(monkeypatch, observer, T0)
    observer.poll_once(now_ms=T0 + I, bars=[_bar(T0)])
    holding = [_bar(T0 + index * I) for index in range(1, 16)]
    status = observer.poll_once(now_ms=T0 + 16 * I, bars=holding)

    outcome = _rows(observer, "SELECT * FROM p11_outcomes")[0]
    assert outcome["exit_reason"] == "TIME"
    assert outcome["bars_held"] == 15
    assert outcome["entry_bar_ms"] == T0 + I
    assert outcome["exit_bar_ms"] == T0 + 15 * I
    assert status["metrics"]["time_exits"] == 1
    assert status["reconciliation"]["status"] == "PASS"


def test_same_bar_sl_and_tp_is_conservatively_stop_first(tmp_path, monkeypatch):
    observer = _new_observer(tmp_path / "stop-first")
    _force_trade_only(monkeypatch, observer, T0)
    observer.poll_once(now_ms=T0 + I, bars=[_bar(T0)])
    observer.poll_once(
        now_ms=T0 + 2 * I,
        bars=[_bar(T0 + I, high=101.0, low=98.0, close=99.5)],
    )

    outcome = _rows(observer, "SELECT * FROM p11_outcomes")[0]
    exit_event = _rows(
        observer,
        "SELECT payload_json FROM p11_events WHERE event_type='SHADOW_EXITED'",
    )[0]
    assert outcome["exit_reason"] == "SL"
    assert json.loads(exit_event["payload_json"])["stop_first"] is True


def test_gap_through_stop_fills_at_gap_open(tmp_path, monkeypatch):
    observer = _new_observer(tmp_path / "gap-through")
    _force_trade_only(monkeypatch, observer, T0)
    observer.poll_once(now_ms=T0 + I, bars=[_bar(T0)])
    observer.poll_once(now_ms=T0 + 2 * I, bars=[_bar(T0 + I)])
    gap = _bar(
        T0 + 2 * I,
        open_=102.0,
        high=102.5,
        low=101.5,
        close=102.0,
    )
    observer.poll_once(now_ms=T0 + 3 * I, bars=[gap])

    outcome = _rows(observer, "SELECT * FROM p11_outcomes")[0]
    assert outcome["exit_reason"] == "SL"
    assert outcome["exit_price"] == pytest.approx(102.0)
    assert outcome["exit_ts_ms"] == T0 + 2 * I
    assert outcome["bars_held"] == 1


def test_restart_recovers_planned_entry_without_duplication(tmp_path, monkeypatch):
    path = tmp_path / "restart-planned"
    first = _new_observer(path)
    _force_trade_only(monkeypatch, first, T0)
    first.poll_once(now_ms=T0 + I, bars=[_bar(T0)])
    original_ids = _rows(
        first,
        "SELECT lifecycle_id,opportunity_id,signal_id,candidate_trade_id FROM p11_lifecycles",
    )[0]
    first.close()

    restarted = _new_observer(path, now_ms=T0 + I)
    _force_trade_only(monkeypatch, restarted, T0)
    restarted.poll_once(
        now_ms=T0 + 2 * I,
        bars=[_bar(T0 + I, high=100.2, low=98.7, close=99.0)],
    )
    recovered_ids = _rows(
        restarted,
        "SELECT lifecycle_id,opportunity_id,signal_id,candidate_trade_id FROM p11_lifecycles WHERE signal_bar_ms=?",
        (T0,),
    )[0]

    assert recovered_ids == original_ids
    assert _scalar(
        restarted,
        "SELECT COUNT(*) FROM p11_events WHERE event_type='SHADOW_ENTRY_OPENED'",
    ) == 1
    assert _scalar(restarted, "SELECT COUNT(*) FROM p11_outcomes") == 1
    assert _scalar(restarted, "SELECT COUNT(*) FROM p11_labels") == 1
    assert observer_mod.reconcile_store(restarted.store)["status"] == "PASS"


def test_restart_recovers_open_position_and_preserves_path(tmp_path, monkeypatch):
    path = tmp_path / "restart-open"
    first = _new_observer(path)
    _force_trade_only(monkeypatch, first, T0)
    first.poll_once(now_ms=T0 + I, bars=[_bar(T0)])
    first.poll_once(now_ms=T0 + 2 * I, bars=[_bar(T0 + I)])
    assert _rows(first, "SELECT state FROM p11_lifecycles WHERE signal_bar_ms=?", (T0,))[0]["state"] == "OPEN_SHADOW"
    first.close()

    restarted = _new_observer(path, now_ms=T0 + 2 * I)
    _force_trade_only(monkeypatch, restarted, T0)
    restarted.poll_once(
        now_ms=T0 + 3 * I,
        bars=[_bar(T0 + 2 * I, high=100.2, low=98.7, close=99.0)],
    )

    outcome = _rows(restarted, "SELECT * FROM p11_outcomes")[0]
    assert outcome["entry_bar_ms"] == T0 + I
    assert outcome["exit_bar_ms"] == T0 + 2 * I
    assert outcome["exit_reason"] == "TP"
    assert _scalar(
        restarted,
        "SELECT COUNT(*) FROM p11_events WHERE event_type='SHADOW_ENTRY_OPENED'",
    ) == 1
    assert observer_mod.reconcile_store(restarted.store)["status"] == "PASS"


def test_duplicate_bar_is_idempotent_and_conflict_is_fail_closed(tmp_path, monkeypatch):
    observer = _new_observer(tmp_path / "duplicates")
    _force_trade_only(monkeypatch, observer)
    original = _bar(T0)
    observer.poll_once(now_ms=T0 + I, bars=[original, dict(original)])
    before = (
        _scalar(observer, "SELECT COUNT(*) FROM p11_bars"),
        _scalar(observer, "SELECT COUNT(*) FROM p11_lifecycles"),
        _scalar(observer, "SELECT COUNT(*) FROM p11_events"),
    )
    observer.poll_once(now_ms=T0 + I, bars=[dict(original)])
    after = (
        _scalar(observer, "SELECT COUNT(*) FROM p11_bars"),
        _scalar(observer, "SELECT COUNT(*) FROM p11_lifecycles"),
        _scalar(observer, "SELECT COUNT(*) FROM p11_events"),
    )
    assert after == before

    conflicting = _bar(T0, high=100.3)
    with pytest.raises(observer_mod.ObserverDataError, match="BAR_PAYLOAD_CONFLICT"):
        observer.poll_once(now_ms=T0 + I, bars=[conflicting])
    assert _scalar(observer, "SELECT COUNT(*) FROM p11_bars") == 1
    assert _scalar(observer, "SELECT COUNT(*) FROM p11_events") == before[2]
    assert observer.store.checkpoint()["observer_status"] == "HALTED_FAIL_CLOSED"
    diagnostic = _rows(
        observer,
        "SELECT bar_open_ms,details_json FROM p11_diagnostics WHERE code=?",
        ("BAR_PAYLOAD_CONFLICT",),
    )[0]
    details = json.loads(diagnostic["details_json"])
    assert diagnostic["bar_open_ms"] == T0
    assert details["stored_payload"]["high"] == original["high"]
    assert details["incoming_payload"]["high"] == conflicting["high"]
    assert details["stored_payload_hash"] != details["incoming_payload_hash"]


def test_restart_accepts_unrelated_repo_provenance_but_keeps_frozen_origin(tmp_path):
    path = tmp_path / "provenance-continuity"
    first = observer_mod.ObserverStore(
        path, now_ms=T0, forward_start_ms=T0, provenance=PROVENANCE,
    )
    changed_repo = {"head": "c" * 40, "tree": "d" * 40}
    restarted = observer_mod.ObserverStore(
        path, now_ms=T0 + I, forward_start_ms=T0 + I,
        provenance=changed_repo,
    )

    assert restarted.run_id == first.run_id
    assert restarted.forward_start_ms == T0
    assert restarted.run["repo_head"] == PROVENANCE["head"]
    assert restarted.run["repo_tree"] == PROVENANCE["tree"]
    assert restarted.config["dependency_closure_fingerprint"]
    assert restarted.config["source_finality_lag_ms"] == 120_000


def test_atomic_lease_blocks_second_process_and_fences_old_holder(tmp_path):
    path = tmp_path / "fencing"
    first = observer_mod.ObserverStore(
        path, now_ms=T0, forward_start_ms=T0, provenance=PROVENANCE,
        lease_ttl_ms=10_000,
    )
    first_epoch = first.acquire_or_renew_lease(T0)
    second = observer_mod.ObserverStore(
        path, now_ms=T0, forward_start_ms=T0, provenance=PROVENANCE,
        lease_ttl_ms=10_000,
    )

    with pytest.raises(observer_mod.ObserverAlreadyRunning):
        second.acquire_or_renew_lease(T0 + 1)
    second_epoch = second.acquire_or_renew_lease(T0 + 10_001)
    assert first_epoch == 1
    assert second_epoch == 2

    with pytest.raises(
        observer_mod.ObserverAlreadyRunning,
        match="OBSERVER_FENCED_OR_LEASE_EXPIRED",
    ):
        with first.transaction(T0 + 10_002):
            pass

    checkpoint_before_stale_release = second.checkpoint()
    first.release_lease(T0 + 10_003)
    assert second.checkpoint() == checkpoint_before_stale_release


def test_reconciler_detects_orphan_even_if_db_was_externally_corrupted(tmp_path):
    observer = _new_observer(tmp_path / "orphan")
    # Deliberately bypass the store connection's FK pragma to emulate a damaged
    # legacy/imported file.  The reconciler must still detect the orphan.
    conn = sqlite3.connect(observer.store.db_path)
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            """INSERT INTO p11_labels (
              label_id,run_id,outcome_id,lifecycle_id,opportunity_id,signal_id,
              candidate_trade_id,underlying_trade_id,hypothesis_id,
              global_event_id,dependency_cluster_id,entry_bar_id,exit_bar_id,
              label,label_name,label_method,finalization_status,finalized_at_ms,
              provenance_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "orphan-label", observer.run_id, "missing-outcome",
                "missing-lifecycle", "opportunity", "signal", "candidate",
                "underlying", observer_mod.HYPOTHESIS_ID, "global", "cluster",
                "entry-bar", "exit-bar", 0, "NON_WIN", "TEST", "FINAL",
                T0, "{}",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    report = observer_mod.reconcile_store(observer.store, now_ms=T0)
    assert report["status"] == "FAIL"
    assert report["orphan_count"] == 1
    assert any(issue.startswith("ORPHANS:") for issue in report["issues"])


def test_outcome_and_label_have_database_enforced_one_to_one_uniques(tmp_path, monkeypatch):
    observer = _complete_tp(tmp_path, monkeypatch, directory="unique-final")

    with observer.store.connect() as conn:
        outcome = dict(conn.execute("SELECT * FROM p11_outcomes").fetchone())
        duplicate = dict(outcome)
        duplicate["outcome_id"] = "different-outcome-id"
        columns = list(duplicate)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                f"INSERT INTO p11_outcomes ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                tuple(duplicate[key] for key in columns),
            )

    with observer.store.connect() as conn:
        label = dict(conn.execute("SELECT * FROM p11_labels").fetchone())
        duplicate = dict(label)
        duplicate["label_id"] = "different-label-id"
        columns = list(duplicate)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                f"INSERT INTO p11_labels ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                tuple(duplicate[key] for key in columns),
            )

    assert _scalar(observer, "SELECT COUNT(*) FROM p11_outcomes") == 1
    assert _scalar(observer, "SELECT COUNT(*) FROM p11_labels") == 1


def test_temporary_data_loss_is_visible_then_exactly_recovered(tmp_path, monkeypatch):
    observer = _new_observer(tmp_path / "temporary-gap")
    _force_trade_only(monkeypatch, observer, T0)
    observer.poll_once(now_ms=T0 + I, bars=[_bar(T0)])

    missing = observer.poll_once(
        now_ms=T0 + 3 * I,
        bars=[_bar(T0 + 2 * I, high=100.2, low=98.7, close=99.0)],
    )
    assert missing["observer_status"] == "WAITING_FOR_DATA_GAP"
    assert missing["reconciliation"]["status"] == "FAIL"
    assert missing["reconciliation"]["pending_structured_errors"] == 1
    assert observer.store.checkpoint()["last_processed_bar_ms"] == T0

    recovered = observer.poll_once(
        now_ms=T0 + 3 * I,
        bars=[
            _bar(T0 + I),
            _bar(T0 + 2 * I, high=100.2, low=98.7, close=99.0),
        ],
    )
    assert recovered["observer_status"] == "OBSERVER_CONNECTED"
    assert recovered["reconciliation"]["status"] == "PASS"
    assert recovered["reconciliation"]["pending_structured_errors"] == 0
    assert recovered["metrics"]["forward_closed_outcomes"] == 1
    assert observer.store.checkpoint()["last_processed_bar_ms"] == T0 + 2 * I


def test_out_of_order_input_is_rejected_before_any_bar_is_persisted(tmp_path, monkeypatch):
    observer = _new_observer(tmp_path / "out-of-order")
    _force_trade_only(monkeypatch, observer)
    with pytest.raises(observer_mod.ObserverDataError, match="BAR_TIMESTAMP_OUT_OF_ORDER"):
        observer.poll_once(
            now_ms=T0 + 2 * I,
            bars=[_bar(T0 + I), _bar(T0)],
        )

    assert _scalar(observer, "SELECT COUNT(*) FROM p11_bars") == 0
    assert _scalar(observer, "SELECT COUNT(*) FROM p11_lifecycles") == 0
    assert observer.store.checkpoint()["observer_status"] == "HALTED_FAIL_CLOSED"


def test_nonfinite_cost_result_rolls_back_entry_and_finalization(
    tmp_path, monkeypatch
):
    observer = _new_observer(tmp_path / "nonfinite")
    _force_trade_only(monkeypatch, observer, T0)
    observer.poll_once(now_ms=T0 + I, bars=[_bar(T0)])
    original_simulator = observer_mod.sim_oms.simulate_trade

    def nonfinite_simulator(**kwargs):
        result = original_simulator(**kwargs)
        result["fee_eur"] = float("nan")
        return result

    monkeypatch.setattr(
        observer_mod.sim_oms, "simulate_trade", nonfinite_simulator
    )
    with pytest.raises(observer_mod.ObserverError, match="NONFINITE_OUTCOME_FIELD"):
        observer.poll_once(
            now_ms=T0 + 2 * I,
            bars=[_bar(T0 + I, high=100.2, low=98.7, close=99.0)],
        )

    lifecycle = _rows(
        observer, "SELECT * FROM p11_lifecycles WHERE signal_bar_ms=?", (T0,)
    )[0]
    assert lifecycle["state"] == "ENTRY_PLANNED"
    assert lifecycle["entry_ts_ms"] is None
    assert _scalar(
        observer,
        "SELECT COUNT(*) FROM p11_events WHERE event_type='SHADOW_ENTRY_OPENED'",
    ) == 0
    assert _scalar(observer, "SELECT COUNT(*) FROM p11_outcomes") == 0
    assert _scalar(observer, "SELECT COUNT(*) FROM p11_labels") == 0
    assert observer.store.checkpoint()["last_processed_bar_ms"] == T0


def test_entry_without_signal_and_close_without_entry_are_invalid_transitions(
    tmp_path, monkeypatch
):
    observer = _new_observer(tmp_path / "invalid-transitions")
    _force_trade_only(monkeypatch, observer)
    observer.poll_once(now_ms=T0 + I, bars=[_bar(T0)])
    lifecycle = _rows(observer, "SELECT * FROM p11_lifecycles")[0]
    assert lifecycle["state"] == "REJECTED_FINAL"

    for target, event_type in (
        ("OPEN_SHADOW", "SHADOW_ENTRY_OPENED"),
        ("EXITED", "SHADOW_EXITED"),
    ):
        observer.store.acquire_or_renew_lease(T0 + 2 * I)
        with pytest.raises(observer_mod.InvalidTransition):
            with observer.store.transaction(T0 + 2 * I) as conn:
                observer.store._advance(
                    conn,
                    lifecycle["lifecycle_id"],
                    to_state=target,
                    event_type=event_type,
                    bar_open_ms=T0 + I,
                    event_timestamp_ms=T0 + 2 * I,
                    availability_timestamp_ms=T0 + 2 * I,
                    processing_timestamp_ms=T0 + 2 * I,
                    payload={"test": "must_fail_closed"},
                )

    assert _rows(observer, "SELECT state FROM p11_lifecycles")[0]["state"] == "REJECTED_FINAL"
    assert observer_mod.reconcile_store(observer.store, now_ms=T0 + 2 * I)["status"] == "PASS"


def test_reconciliation_equation_is_exact_after_rejection_and_closed_trade(
    tmp_path, monkeypatch
):
    observer = _complete_tp(tmp_path, monkeypatch, directory="reconcile")
    report = observer_mod.reconcile_store(observer.store, now_ms=T0 + 2 * I)

    assert report["status"] == "PASS"
    assert report["lhs_forward_opportunities"] == 2
    assert report["rhs_partition_total"] == 2
    assert report["signals_p11_short"] == 1
    assert report["rejections"] == 1
    assert report["shadow_entries"] == 1
    assert report["closed_outcomes"] == 1
    assert report["closed_labels"] == 1
    assert report["duplicate_count"] == 0
    assert report["orphan_count"] == 0
    assert report["invalid_transition_count"] == 0


def test_n_eff_uses_only_finalized_outcomes_not_planned_or_open_state(
    tmp_path, monkeypatch
):
    observer = _new_observer(tmp_path / "n-eff")
    _force_trade_only(monkeypatch, observer, T0)
    planned = observer.poll_once(now_ms=T0 + I, bars=[_bar(T0)])
    assert planned["metrics"]["forward_n_raw"] == 0
    assert planned["metrics"]["forward_n_eff"] == observer_mod.NA

    opened = observer.poll_once(now_ms=T0 + 2 * I, bars=[_bar(T0 + I)])
    assert opened["metrics"]["forward_open_positions"] == 1
    assert opened["metrics"]["forward_n_raw"] == 0
    assert opened["metrics"]["forward_n_eff"] == observer_mod.NA

    closed = observer.poll_once(
        now_ms=T0 + 3 * I,
        bars=[_bar(T0 + 2 * I, high=100.2, low=98.7, close=99.0)],
    )
    assert closed["metrics"]["forward_open_positions"] == 0
    assert closed["metrics"]["forward_n_raw"] == 1
    assert closed["metrics"]["forward_n_eff"] != observer_mod.NA


def test_reconciliation_binds_checkpoint_bars_and_lifecycles_fail_closed(
    tmp_path,
):
    observer = _new_observer(tmp_path / "bar-lifecycle-bijection")
    healthy = observer.poll_once(
        now_ms=T0 + I, bars=_actual_history(fires=False)
    )
    assert healthy["reconciliation"]["status"] == "PASS"
    assert healthy["recommendation"] == "START_FORWARD_SHADOW_NOW"

    with observer.store.connect() as conn:
        conn.execute(
            """UPDATE p11_checkpoints SET last_processed_bar_ms=?
               WHERE run_id=?""",
            (T0 + I, observer.run_id),
        )
    degraded = observer._publish(T0 + 2 * I)
    assert degraded["reconciliation"]["status"] == "FAIL"
    assert "PROCESSED_BAR_COUNT:1!=2" in degraded["reconciliation"]["issues"]
    assert degraded["recommendation"] == "WAIT_FOR_OBSERVER_RECOVERY"
    assert "START_FORWARD_SHADOW_NOW" not in degraded["activation_state"]
    assert "OBSERVER_BLOCKED_FAIL_CLOSED" in degraded["activation_state"]
    summary = (observer.output_dir / "summary.txt").read_text(encoding="utf-8")
    assert "recommendation=WAIT_FOR_OBSERVER_RECOVERY" in summary
    assert "recommendation=START_FORWARD_SHADOW_NOW" not in summary
    assert "paper_filter_enabled=false" in summary
    assert "final_recommendation=NO LIVE" in summary


def test_public_provider_waits_for_explicit_source_finality_lag(
    monkeypatch,
):
    raw_rows = []
    for index in range(15):
        ts_ms = T0 + index * 60_000
        raw_rows.append([ts_ms, 100.0, 100.2, 99.8, 100.0, 1.0, 100.0])
    requested_end_ms = []

    def fake_fetch(_symbol, days, log, end_ms):
        requested_end_ms.append(end_ms)
        return raw_rows

    monkeypatch.setattr(observer_mod.bitget_data, "fetch_bitget_1m", fake_fetch)
    provider = observer_mod.BitgetClosedBarProvider()
    before_finality = T0 + I + observer_mod.SOURCE_FINALITY_LAG_MS - 1
    with pytest.raises(
        observer_mod.PublicDataUnavailable,
        match="BITGET_STRICT_15M_DATA_UNAVAILABLE",
    ):
        provider.fetch(now_ms=before_finality, since_ms=T0)

    settled_now = T0 + I + observer_mod.SOURCE_FINALITY_LAG_MS
    bars = provider.fetch(now_ms=settled_now, since_ms=T0)
    assert [bar["ts"] for bar in bars] == [T0]
    assert requested_end_ms == [T0 + I - 1, T0 + I]


def test_public_poll_never_returns_a_stale_healthy_snapshot_after_publish_failure(
    tmp_path,
    monkeypatch,
):
    observer = _new_observer(tmp_path / "publish-fail-closed")
    observer.poll_once(now_ms=T0 + I, bars=[_bar(T0)])

    class ConflictingProvider:
        def fetch(self, *, now_ms, since_ms):
            return [_bar(T0, high=100.3)]

    observer.provider = ConflictingProvider()
    monkeypatch.setattr(
        observer,
        "_publish",
        lambda _now_ms: (_ for _ in ()).throw(OSError("simulated publish failure")),
    )
    result = observer.poll_once(
        now_ms=T0 + 2 * I + observer_mod.SOURCE_FINALITY_LAG_MS
    )

    assert result["observer_status"] == "HALTED_FAIL_CLOSED"
    assert result["recommendation"] == "WAIT_FOR_OBSERVER_RECOVERY"
    assert result["can_send_real_orders"] is False
    assert result["final_recommendation"] == "NO LIVE"


def _dotted_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def test_observer_has_no_order_private_wallet_or_env_imports_or_calls():
    path = Path(observer_mod.__file__)
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: list[str] = []
    calls: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
        elif isinstance(node, ast.Call):
            calls.append(_dotted_name(node.func))

    forbidden_import_fragments = (
        "app.main",
        "app.config",
        "bitget_client",
        "execution_engine",
        "paper_trader",
        "order_manager",
        "position_manager",
        "wallet",
        "dotenv",
    )
    assert not [
        name
        for name in imported
        if any(fragment in name.lower() for fragment in forbidden_import_fragments)
    ]
    forbidden_calls = (
        "getenv",
        "load_dotenv",
        "place_order",
        "send_order",
        "set_leverage",
        "set_margin",
        "private_request",
        "withdraw",
    )
    assert not [
        name
        for name in calls
        if any(fragment in name.lower() for fragment in forbidden_calls)
    ]
    assert "os.environ" not in source
    assert "can_send_real_orders" in source
    assert observer_mod.load_policy_binding()["orders_allowed"] is False


def test_public_bitget_http_failure_is_visible_and_retryable(tmp_path, monkeypatch):
    def unavailable(_symbol, days, log, end_ms):
        log("  bitget HTTP 503")
        return []

    monkeypatch.setattr(observer_mod.bitget_data, "fetch_bitget_1m", unavailable)
    public_provider = observer_mod.BitgetClosedBarProvider()
    with pytest.raises(observer_mod.PublicDataUnavailable, match="BITGET_PUBLIC_HTTP_ERROR"):
        public_provider.fetch(now_ms=T0 + I, since_ms=T0 - 260 * I)

    class FlakyProvider:
        calls = 0

        def fetch(self, *, now_ms, since_ms):
            self.calls += 1
            if self.calls == 1:
                raise observer_mod.PublicDataUnavailable(
                    "BITGET_PUBLIC_HTTP_ERROR:503"
                )
            return _actual_history(fires=False)

    observer = observer_mod.P11ShortForwardObserver(
        output_dir=tmp_path / "retryable",
        now_ms=T0,
        forward_start_ms=T0,
        provenance=PROVENANCE,
        provider=FlakyProvider(),
    )
    first_poll_ms = T0 + I + observer_mod.SOURCE_FINALITY_LAG_MS
    waiting = observer.poll_once(now_ms=first_poll_ms)
    assert waiting["observer_status"] == "WAITING_FOR_DATA"
    assert "PublicDataUnavailable" in waiting["heartbeat"]["last_error"]
    assert waiting["reconciliation"]["status"] == "PASS"

    recovered = observer.poll_once(now_ms=first_poll_ms)
    assert recovered["observer_status"] == "OBSERVER_CONNECTED"
    assert recovered["heartbeat"]["last_error"] is None
    correlation = f"public-source:{first_poll_ms // I}"
    with observer.store.connect() as conn:
        phases = conn.execute(
            """SELECT phase FROM p11_diagnostics
               WHERE correlation_key=? ORDER BY phase""",
            (correlation,),
        ).fetchall()
    assert {row["phase"] for row in phases} == {"DETECTED", "RESOLVED"}
    observer.close()
