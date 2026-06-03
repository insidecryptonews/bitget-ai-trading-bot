"""ResearchOps V8/V9 — Strategy Experiment Registry (research-only).

A simple, in-memory + JSON-backed registry to track research experiments and
their state transitions without ever promoting anything automatically.

Hard safety:
- never opens orders,
- never flips ``paper_filter_enabled`` or ``can_send_real_orders``,
- never destructive DB migrations,
- read/write only of the JSON file under ``training_exports/`` if provided.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


FINAL_RECOMMENDATION_NO_LIVE = "NO LIVE"

EXP_STATE_REJECT = "REJECT"
EXP_STATE_WATCH_ONLY = "WATCH_ONLY"
EXP_STATE_NEED_MORE_DATA = "NEED_MORE_DATA"
EXP_STATE_SHADOW_CANDIDATE = "SHADOW_CANDIDATE"
EXP_STATE_PAPER_CANDIDATE_LABEL_ONLY = "PAPER_CANDIDATE_LABEL_ONLY"

VALID_STATES: tuple[str, ...] = (
    EXP_STATE_REJECT,
    EXP_STATE_WATCH_ONLY,
    EXP_STATE_NEED_MORE_DATA,
    EXP_STATE_SHADOW_CANDIDATE,
    EXP_STATE_PAPER_CANDIDATE_LABEL_ONLY,
)


@dataclass
class ExperimentRecord:
    strategy_id: str
    family: str
    hypothesis: str
    parameters: dict[str, Any]
    symbols: list[str]
    timeframe: str
    regime: str
    window_hours: int
    data_sources: list[str]
    cost_sources: list[str]
    results: dict[str, Any]
    folds: int
    state: str
    created_at: str
    updated_at: str
    history: list[dict[str, Any]] = field(default_factory=list)
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class StrategyExperimentRegistry:
    """Thread-safe JSON-backed registry. No automatic promotion."""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path else None
        self._lock = threading.Lock()
        self._records: dict[str, ExperimentRecord] = {}
        if self._path and self._path.exists():
            self._load()

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            for raw in data.get("records", []):
                rec = ExperimentRecord(**raw)
                self._records[rec.strategy_id] = rec
        except Exception:
            # Fail-open: never crash on registry I/O.
            self._records = {}

    def _persist(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": 1,
                "research_only": True,
                "paper_filter_enabled": False,
                "can_send_real_orders": False,
                "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE,
                "records": [r.as_dict() for r in self._records.values()],
            }
            self._path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception:
            pass

    def register(
        self,
        *,
        strategy_id: str,
        family: str,
        hypothesis: str,
        parameters: dict[str, Any] | None = None,
        symbols: Iterable[str] | None = None,
        timeframe: str = "5m",
        regime: str = "UNKNOWN",
        window_hours: int = 24,
        data_sources: Iterable[str] | None = None,
        cost_sources: Iterable[str] | None = None,
        results: dict[str, Any] | None = None,
        folds: int = 0,
        state: str = EXP_STATE_NEED_MORE_DATA,
    ) -> ExperimentRecord:
        if state not in VALID_STATES:
            state = EXP_STATE_NEED_MORE_DATA
        now = self._now()
        record = ExperimentRecord(
            strategy_id=strategy_id,
            family=family,
            hypothesis=hypothesis,
            parameters=dict(parameters or {}),
            symbols=sorted(set(symbols or [])),
            timeframe=timeframe,
            regime=regime,
            window_hours=int(window_hours),
            data_sources=list(data_sources or []),
            cost_sources=list(cost_sources or []),
            results=dict(results or {}),
            folds=int(folds),
            state=state,
            created_at=now,
            updated_at=now,
            history=[{"at": now, "state": state, "reason": "registered"}],
        )
        with self._lock:
            self._records[strategy_id] = record
            self._persist()
        return record

    def transition(self, strategy_id: str, *, new_state: str, reason: str = "") -> ExperimentRecord | None:
        if new_state not in VALID_STATES:
            return None
        with self._lock:
            rec = self._records.get(strategy_id)
            if rec is None:
                return None
            now = self._now()
            rec.state = new_state
            rec.updated_at = now
            rec.history.append({"at": now, "state": new_state, "reason": reason})
            self._persist()
            return rec

    def update_results(self, strategy_id: str, *, results: dict[str, Any]) -> ExperimentRecord | None:
        with self._lock:
            rec = self._records.get(strategy_id)
            if rec is None:
                return None
            rec.results.update(results)
            rec.updated_at = self._now()
            self._persist()
            return rec

    def get(self, strategy_id: str) -> ExperimentRecord | None:
        with self._lock:
            return self._records.get(strategy_id)

    def list(self, *, state: str | None = None) -> list[ExperimentRecord]:
        with self._lock:
            recs = list(self._records.values())
        if state:
            recs = [r for r in recs if r.state == state]
        return sorted(recs, key=lambda r: r.updated_at, reverse=True)

    def snapshot(self) -> dict[str, Any]:
        recs = self.list()
        return {
            "total": len(recs),
            "by_state": {s: sum(1 for r in recs if r.state == s) for s in VALID_STATES},
            "records": [r.as_dict() for r in recs],
            "research_only": True,
            "paper_filter_enabled": False,
            "can_send_real_orders": False,
            "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE,
        }
