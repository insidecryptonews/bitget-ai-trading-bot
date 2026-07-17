"""Cross-venue causal engine service and artifact-only status aggregation."""

from __future__ import annotations

import json
import heapq
import os
import time
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import ENGINE_SNAPSHOT_PATH, ENGINE_STATUS_PATH, RUNTIME_ROOT, code_revision, safety_envelope
from .leadlag import LeadLagEngine
from .ledger import CrossVenueLedger
from .leverage import LeverageLab
from .paper import PaperSimulator
from .providers import load_config
from .storage import (
    atomic_json, read_json, read_next_jsonl_record, safe_staging_root,
    storage_status, stream_rollover_lock,
)

OFFSETS_PATH = RUNTIME_ROOT / "stream_offsets.json"
HEALTHY_COLLECTOR_STATUSES = {"HEALTHY", "HEALTHY_WITH_RECONNECTS"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str) + "\n"
    with path.open("a", encoding="utf-8", newline="") as handle:
        handle.write(line); handle.flush()


def collector_health(venue: str, *, root: Path | None = None, stale_after_seconds: float = 20.0) -> dict[str, Any]:
    root = safe_staging_root(root)
    path = root / venue / "health.json"
    payload = read_json(path, {}) or {}
    age = None if not path.is_file() else max(0.0, time.time() - path.stat().st_mtime)
    status = str(payload.get("status") or "CONNECTING")
    if age is not None and age > stale_after_seconds and status not in {"ERROR", "DISABLED_BY_CONFIG"}:
        status = "STALE"
    return {
        **payload, "component": f"CROSS_VENUE_{venue.upper()}", "venue": venue,
        "status": status, "health_file_age_seconds": age, **safety_envelope(),
    }


class CrossVenueService:
    def __init__(self, *, config: dict[str, Any] | None = None, root: Path | None = None,
                 ledger: CrossVenueLedger | None = None, bootstrap_existing: bool = False):
        self.config = config or load_config()
        self.root = safe_staging_root(root)
        self.ledger = ledger or CrossVenueLedger()
        self.engine = LeadLagEngine(self.config)
        self.paper = PaperSimulator(self.config, self.ledger)
        self.leverage = LeverageLab(self.config, self.ledger)
        self.code_commit = code_revision()
        self.forward_boundary_path = OFFSETS_PATH.with_name("forward_boundary.json")
        saved_offsets = read_json(OFFSETS_PATH, None)
        resume_valid = (
            isinstance(saved_offsets, dict)
            and saved_offsets.get("_schema") == "cross_venue_stream_offsets.v2"
        )
        active_venues = list(self.config.get("active_venues", []))
        if bootstrap_existing:
            self.offsets = {venue: 0 for venue in active_venues}
            boundary_mode = "EXPLICIT_TEST_REPLAY"
        elif resume_valid:
            self.offsets = {}
            for venue in active_venues:
                if venue in saved_offsets:
                    self.offsets[venue] = max(0, int(saved_offsets.get(venue) or 0))
                else:
                    stream = self.root / venue / "normalized" / "current.jsonl"
                    self.offsets[venue] = stream.stat().st_size if stream.is_file() else 0
            boundary_mode = "RESUMED_PERSISTED_FORWARD_OFFSETS"
        else:
            # First productive start is forward-only. Existing rows are research
            # history and must never be replayed into the simulated account.
            self.offsets = {}
            for venue in active_venues:
                stream = self.root / venue / "normalized" / "current.jsonl"
                self.offsets[venue] = stream.stat().st_size if stream.is_file() else 0
            boundary_mode = "FROZEN_AT_CURRENT_STREAM_END"
        saved_frontier = int(saved_offsets.get("_last_processed_monotonic_ns") or 0) if resume_valid else 0
        self.clock_epoch_reset = saved_frontier > time.monotonic_ns()
        self.last_processed_monotonic_ns = 0 if self.clock_epoch_reset else saved_frontier
        self.refreeze_venues: set[str] = set()
        existing_boundary = read_json(self.forward_boundary_path, {}) or {}
        self.forward_boundary = {
            "schema": "cross_venue_forward_boundary.v1",
            "boundary_mode": boundary_mode,
            "initial_boundary_mode": (
                existing_boundary.get("initial_boundary_mode")
                or existing_boundary.get("boundary_mode")
                or ("FROZEN_AT_CURRENT_STREAM_END" if resume_valid else boundary_mode)
            ),
            "frozen_at": (existing_boundary.get("frozen_at") or utc_now()) if resume_valid else utc_now(),
            "initial_offsets": (existing_boundary.get("initial_offsets") or dict(self.offsets)) if resume_valid else dict(self.offsets),
            "clock_epoch_reset_detected": self.clock_epoch_reset,
            "historical_rows_eligible_for_paper": False,
            "code_commit": self.code_commit,
            **safety_envelope(),
        }
        RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
        atomic_json(self.forward_boundary_path, self.forward_boundary)
        self.events_processed = 0; self.observations_recorded = 0; self.signals_recorded = 0; self.outcomes_recorded = 0
        self.late_events_dropped = 0
        self.max_late_event_lag_ms = 0.0
        self.last_event_at: str | None = None; self.errors: list[str] = []
        self.started_at = utc_now()

    def cycle(self, *, max_rows_per_venue: int = 20_000) -> dict[str, Any]:
        # A collector may roll a fully consumed derived hot stream at its size
        # cap. Holding the same cross-process mutex through cursor persistence
        # makes rename + offset reset atomic from the engine's perspective.
        with stream_rollover_lock(self.root):
            return self._cycle_locked(max_rows_per_venue=max_rows_per_venue)

    def _cycle_locked(self, *, max_rows_per_venue: int = 20_000) -> dict[str, Any]:
        cycle_events = 0; rows_consumed = 0; opened: list[dict[str, Any]] = []; closed: list[dict[str, Any]] = []
        signals_path = self.root / "analysis" / "signals.jsonl"
        outcomes_path = self.root / "analysis" / "outcomes.jsonl"
        # This legacy parameter is now a global cycle budget. A k-way merge of
        # the next durable row from each venue prevents a busy venue's unread
        # backlog from being overtaken by newer rows from a quieter venue.
        event_budget = max(1, int(max_rows_per_venue))
        cycle_floor_monotonic_ns = self.last_processed_monotonic_ns
        reorder_buffer_ms = float(self.config.get("causal_reorder_buffer_ms", 250.0))
        cycle_cutoff_monotonic_ns = time.monotonic_ns() - int(reorder_buffer_ms * 1_000_000)
        paper_active = bool(self.ledger.open_positions()) or bool(self.ledger.pending_signals())
        heap: list[tuple[int, int, str, int, dict[str, Any], int]] = []
        cursors: dict[str, tuple[Any, int]] = {}
        serial = 0

        with ExitStack() as stack:
            for venue in self.config.get("active_venues", []):
                stream = self.root / venue / "normalized" / "current.jsonl"
                old_offset = int(self.offsets.get(venue) or 0)
                if stream.is_symlink():
                    self.errors.append(f"{venue}:STREAM_SYMLINK_BLOCKED")
                    continue
                if not stream.is_file():
                    if old_offset > 0:
                        self.refreeze_venues.add(venue)
                        self.errors.append(f"{venue}:STREAM_MISSING_REFREEZE_REQUIRED")
                    self.offsets[venue] = 0
                    continue
                snapshot_size = stream.stat().st_size
                if venue in self.refreeze_venues:
                    self.offsets[venue] = snapshot_size
                    self.refreeze_venues.discard(venue)
                    self.errors.append(f"{venue}:STREAM_RECREATED_FORWARD_BOUNDARY_REFROZEN")
                    continue
                if old_offset < 0 or old_offset > snapshot_size:
                    self.offsets[venue] = snapshot_size
                    self.errors.append(f"{venue}:STREAM_CHANGED_FORWARD_BOUNDARY_REFROZEN")
                    continue
                handle = stack.enter_context(stream.open("rb"))
                handle.seek(old_offset)
                cursors[venue] = (handle, snapshot_size)
                event, end_offset, error = read_next_jsonl_record(handle, snapshot_size=snapshot_size)
                if error and error != "PARTIAL_LINE_WAITING":
                    self.errors.append(f"{venue}:{error}")
                if event is not None:
                    serial += 1
                    heapq.heappush(heap, (
                        int(event.get("local_receive_monotonic_ns") or 0),
                        int(event.get("local_receive_wall_ms") or 0), venue, serial, event, end_offset,
                    ))

            while heap and rows_consumed < event_budget:
                if heap[0][0] > 0 and heap[0][0] > cycle_cutoff_monotonic_ns:
                    break
                event_mono, _, venue, _, event, end_offset = heapq.heappop(heap)
                rows_consumed += 1
                self.offsets[venue] = end_offset
                if event_mono <= 0 or event_mono < cycle_floor_monotonic_ns:
                    self.late_events_dropped += 1
                    if event_mono > 0:
                        self.max_late_event_lag_ms = max(
                            self.max_late_event_lag_ms,
                            (cycle_floor_monotonic_ns - event_mono) / 1_000_000.0,
                        )
                else:
                    result = self.engine.process(event); cycle_events += 1
                    self.last_processed_monotonic_ns = max(self.last_processed_monotonic_ns, event_mono)
                    self.last_event_at = event.get("local_receive_wall_ts") or self.last_event_at
                    signal = result.get("signal")
                    if isinstance(signal, dict):
                        recorded = self.paper.on_signal(signal)
                        if recorded:
                            self.observations_recorded += 1
                            if signal.get("status") == "CANDIDATE_RESEARCH_ONLY":
                                self.signals_recorded += 1
                                paper_active = True
                        _append_jsonl(signals_path, signal)
                    for outcome in result.get("outcomes") or []:
                        _append_jsonl(outcomes_path, outcome); self.outcomes_recorded += 1
                    if paper_active and event.get("venue") == "bitget" and event.get("event_type") in {"book_l1", "ticker"}:
                        paper_result = self.paper.on_bitget_quote(event)
                        opened.extend(paper_result.get("opened") or []); closed.extend(paper_result.get("closed") or [])
                        paper_active = bool(self.ledger.open_positions()) or bool(self.ledger.pending_signals())

                handle, snapshot_size = cursors[venue]
                next_event, next_offset, error = read_next_jsonl_record(handle, snapshot_size=snapshot_size)
                if error and error != "PARTIAL_LINE_WAITING":
                    self.errors.append(f"{venue}:{error}")
                if next_event is not None:
                    serial += 1
                    heapq.heappush(heap, (
                        int(next_event.get("local_receive_monotonic_ns") or 0),
                        int(next_event.get("local_receive_wall_ms") or 0), venue, serial,
                        next_event, next_offset,
                    ))
        self.events_processed += cycle_events
        RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
        atomic_json(OFFSETS_PATH, {"_schema": "cross_venue_stream_offsets.v2", **self.offsets,
                                  "_last_processed_monotonic_ns": self.last_processed_monotonic_ns,
                                  "updated_at": utc_now(), **safety_envelope()})
        leverage = self.leverage.refresh()
        snapshot = self._snapshot(leverage)
        atomic_json(ENGINE_SNAPSHOT_PATH, snapshot)
        atomic_json(ENGINE_STATUS_PATH, snapshot["health"])
        return {"cycle_events": cycle_events, "opened": opened, "closed": closed, **snapshot}

    def _snapshot(self, leverage: dict[str, Any]) -> dict[str, Any]:
        leadlag = self.engine.snapshot()
        health = self._health(leadlag)
        flow_by_key = {
            (row.get("venue"), row.get("symbol")): row
            for row in leadlag.get("orderflow", [])
        }
        venues = []
        for row in leadlag.get("venues", []):
            collector = (health.get("collectors") or {}).get(str(row.get("venue")), {})
            flow = flow_by_key.get((row.get("venue"), row.get("symbol")), {})
            venues.append({
                **row,
                "microprice": flow.get("microprice"),
                "book_imbalance_l1": flow.get("book_imbalance_l1"),
                "trade_events_1s": flow.get("trade_events_1s"),
                "net_aggressor_volume_1s": flow.get("net_aggressor_volume_1s"),
                "collector_status": collector.get("status"),
                "connected": collector.get("connected"),
                "reconnect_count": collector.get("reconnect_count_total", collector.get("reconnect_count")),
                "reconnects_last_hour": collector.get("reconnects_last_hour"),
                "last_reconnect_at": collector.get("last_reconnect_at"),
                "last_close_code": collector.get("last_close_code"),
                "last_close_reason": collector.get("last_close_reason"),
                "reconnect_recovery_status": collector.get("reconnect_recovery_status"),
                "protocol_ping_count": collector.get("protocol_ping_count"),
                "protocol_pong_count": collector.get("protocol_pong_count"),
                "application_pong_count": collector.get("application_pong_count"),
                "gaps": collector.get("gaps_total", collector.get("gaps")),
                "last_event_age_ms": collector.get("last_event_age_ms"),
            })
        active_venues = list(self.config.get("active_venues", []))
        symbols = list(self.config.get("symbols", []))
        eligible = list(self.config.get("signal_eligible_venues", []))
        observation_only = list(self.config.get("observation_only_venues", []))
        evaluation_counts = dict(leadlag.get("evaluation_counts") or {})
        ledger_signals = self.ledger.rows("signals", 5000)
        evaluation_counts["accepted_simulated_signals"] = sum(
            1 for row in ledger_signals if str(row.get("status")) in {"OPEN", "CLOSED"}
        )
        return {
            "schema": "cross_venue_dashboard_snapshot.v1", "generated_at": utc_now(),
            "code_commit": self.code_commit, "forward_boundary": self.forward_boundary,
            "providers": {
                "active": active_venues,
                "active_venue_count": len(active_venues),
                "active_stream_count": len(active_venues) * len(symbols),
                "target": [self.config.get("target_venue", "bitget")],
                "target_venue_count": 1,
                "signal_eligible": eligible,
                "signal_eligible_venue_count": len(eligible),
                "observation_only": observation_only,
                "observation_only_venue_count": len(observation_only),
                "tier2_enabled": False,
            },
            "venues": venues, "prices": venues,
            "normalized_price_series": leadlag["normalized_price_series"],
            "orderflow": leadlag["orderflow"], "leadlag": {"leaderboard": leadlag["leaderboard"],
                                           "ordering_clock": leadlag["ordering_clock"],
                                           "pending_outcomes": leadlag["pending_outcomes"],
                                           "strategy_research_status": leadlag["strategy_research_status"],
                                           "recent_episodes": leadlag.get("recent_episodes", []),
                                           "evaluation_counts": evaluation_counts},
            "signals": leadlag["recent_signals"], "account": self.ledger.account(),
            "positions": self.ledger.open_positions(), "trades": self.ledger.rows("trades", 500),
            "equity": list(reversed(self.ledger.rows("equity", 2000))),
            "events": self.ledger.rows("events", 300), "leverage": leverage,
            "reconciliation": self.ledger.reconcile(), "health": health,
            "storage": storage_status(self.root),
            **safety_envelope(),
        }

    def _health(self, leadlag: dict[str, Any]) -> dict[str, Any]:
        venues = {venue: collector_health(venue, root=self.root) for venue in self.config.get("active_venues", [])}
        healthy_feeds = sum(1 for row in venues.values() if row["status"] in HEALTHY_COLLECTOR_STATUSES)
        target_healthy = venues.get(self.config.get("target_venue", "bitget"), {}).get("status") in HEALTHY_COLLECTOR_STATUSES
        healthy_leaders = sum(
            1 for venue in self.config.get("signal_eligible_venues", [])
            if venues.get(venue, {}).get("status") in HEALTHY_COLLECTOR_STATUSES
        )
        required_leaders = int(self.config.get("minimum_consensus_venues", 2))
        feed_set_ready = target_healthy and healthy_leaders >= required_leaders
        degraded_feeds = any(row.get("status") in {"STALE", "DEGRADED", "ERROR"} for row in venues.values())
        normalizer_status = "HEALTHY" if feed_set_ready else "DEGRADED" if healthy_feeds or degraded_feeds else "CONNECTING"
        logical = {
            "CROSS_VENUE_NORMALIZER": {
                "status": normalizer_status,
                "events_processed": self.events_processed, "last_event_at": self.last_event_at,
                "target_feed_healthy": target_healthy, "healthy_eligible_leaders": healthy_leaders,
                "required_eligible_leaders": required_leaders,
                "late_events_dropped_causal_guard": self.late_events_dropped,
                "max_late_event_lag_ms": self.max_late_event_lag_ms,
                "causal_reorder_buffer_ms": float(self.config.get("causal_reorder_buffer_ms", 250.0)),
                **safety_envelope(),
            },
            "CROSS_VENUE_LEADLAG": {
                "status": "WAITING_FOR_SIGNAL" if self.signals_recorded == 0 else "HEALTHY",
                "observations_recorded": self.observations_recorded,
                "candidate_signals_recorded": self.signals_recorded,
                "evaluation_counts": leadlag.get("evaluation_counts", {}),
                "pending_outcomes": leadlag["pending_outcomes"],
                "no_lookahead": True, **safety_envelope(),
            },
            "CROSS_VENUE_PAPER": {
                "status": "WAITING_FOR_SIGNAL" if not self.ledger.open_positions() and not self.ledger.rows("trades", 1) else "PAPER_RESEARCH",
                "reconciliation": self.ledger.reconcile(), **safety_envelope(),
            },
            "CROSS_VENUE_LEVERAGE_LAB": {
                "status": "WAITING_FOR_SIGNAL" if not self.ledger.rows("trades", 1) else "HEALTHY",
                "simulation_only": True, "real_leverage_changed": False, **safety_envelope(),
            },
        }
        statuses = [row["status"] for row in venues.values()]
        if any(status == "ERROR" for status in statuses) or self.errors:
            overall = "DEGRADED"
        elif feed_set_ready:
            overall = "PAPER_RESEARCH"
        elif any(status in {"STALE", "DEGRADED"} for status in statuses):
            overall = "DEGRADED"
        else:
            overall = "CONNECTING"
        return {
            "schema": "cross_venue_health.v1", "status": overall, "started_at": self.started_at,
            "heartbeat_at": utc_now(), "pid": os.getpid(), "events_processed": self.events_processed,
            "last_processed_monotonic_ns": self.last_processed_monotonic_ns,
            "clock_epoch_reset_detected": self.clock_epoch_reset,
            "forward_boundary": self.forward_boundary,
            "late_events_dropped_causal_guard": self.late_events_dropped,
            "max_late_event_lag_ms": self.max_late_event_lag_ms,
            "causal_reorder_buffer_ms": float(self.config.get("causal_reorder_buffer_ms", 250.0)),
            "last_event_at": self.last_event_at, "errors": self.errors[-20:],
            "collectors": venues, "components": logical, **safety_envelope(),
        }


def run_service(*, interval_seconds: float = 0.25, max_cycles: int | None = None,
                service: CrossVenueService | None = None, sleep_fn: Callable[[float], None] = time.sleep,
                stop_file: Path | str | None = None) -> dict[str, Any]:
    service = service or CrossVenueService(); cycles = 0; last: dict[str, Any] = {}
    stop_path = Path(stop_file) if stop_file is not None else None
    while max_cycles is None or cycles < max_cycles:
        if stop_path is not None and stop_path.is_file():
            break
        try:
            last = service.cycle(); cycles += 1
        except KeyboardInterrupt:
            break
        except Exception as exc:
            service.errors.append(f"{type(exc).__name__}:{str(exc)[:240]}")
            atomic_json(ENGINE_STATUS_PATH, service._health(service.engine.snapshot()))
        if max_cycles is None or cycles < max_cycles:
            sleep_fn(interval_seconds)
    return {"cycles": cycles, **last, **safety_envelope()}
