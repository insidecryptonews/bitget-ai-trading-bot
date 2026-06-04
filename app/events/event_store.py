"""ResearchOps V8.1 — Event Store (idempotent, research-only).

Provides namespaced ``event_*`` storage:

- ``event_raw`` — raw events as received from each source.
- ``event_canonical`` — canonicalised events after normalisation.
- ``event_candidates`` — registry of candidates with full schema.
- ``event_sources`` — provenance of each datapoint per candidate.
- ``event_registry_runs`` — bookkeeping of registry runs.

Storage is JSONL under ``training_exports/events_v8_1/`` by default. The path
is configurable for tests via ``EventStore(base_path=...)``. The class never
deletes records and uses ``CREATE-IF-NOT-EXISTS`` semantics on disk
(``mkdir(parents=True, exist_ok=True)``).

Hard contract:
- never opens orders,
- never writes to the main bot DB,
- never calls private endpoints,
- ``research_only=True`` / ``can_send_real_orders=False`` / ``NO LIVE``.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from . import (
    FINAL_RECOMMENDATION_NO_LIVE,
    STATUS_DETECTED,
    SUPPORTED_FAMILIES,
    VALID_STATUSES,
)


@dataclass
class EventCandidate:
    """Schema mínimo V8.1."""

    event_id: str
    family: str
    symbol: str
    event_time_utc: str
    source_primary: str
    source_secondary: str | None = None
    source_conflict_flag: bool = False
    conflict_reason: str = ""
    headline_size_usd: float | None = None
    effective_size_usd: float | None = None
    size_pct_circ: float | None = None
    float_pct: float | None = None
    fdv_usd: float | None = None
    age_days_since_listing: int | None = None
    perp_available_bitget: bool = False
    venue_count: int = 0
    shortability_score: float | None = None
    status: str = STATUS_DETECTED
    created_at: str = ""
    updated_at: str = ""
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class EventStore:
    """Thread-safe namespaced storage for ``event_*`` collections."""

    DEFAULT_BASE_PATH = Path("training_exports") / "events_v8_1"

    COLLECTIONS = (
        "event_raw",
        "event_canonical",
        "event_candidates",
        "event_sources",
        "event_registry_runs",
    )

    def __init__(self, base_path: str | Path | None = None) -> None:
        self._base_path = Path(base_path) if base_path else self.DEFAULT_BASE_PATH
        self._lock = threading.Lock()
        # In-memory caches per collection.
        self._raw: list[dict[str, Any]] = []
        self._canonical: dict[str, dict[str, Any]] = {}
        self._candidates: dict[str, EventCandidate] = {}
        self._sources: list[dict[str, Any]] = []
        self._runs: list[dict[str, Any]] = []
        self._loaded = False

    # ---- Setup ----

    def _path(self, collection: str) -> Path:
        return self._base_path / f"{collection}.jsonl"

    def ensure_storage(self) -> None:
        """Create the directory and JSONL files if they don't exist.

        Idempotent. Never deletes data.
        """
        try:
            self._base_path.mkdir(parents=True, exist_ok=True)
            for coll in self.COLLECTIONS:
                path = self._path(coll)
                if not path.exists():
                    path.touch()
        except Exception:
            # Fail-open: store can run in memory-only mode if filesystem is RO.
            pass

    def _load_if_needed(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            self.ensure_storage()
            # event_raw is append-only
            for line in self._read_lines("event_raw"):
                self._raw.append(json.loads(line))
            for line in self._read_lines("event_canonical"):
                row = json.loads(line)
                self._canonical[row["event_id"]] = row
            for line in self._read_lines("event_candidates"):
                row = json.loads(line)
                rec = EventCandidate(**row)
                self._candidates[rec.event_id] = rec
            for line in self._read_lines("event_sources"):
                self._sources.append(json.loads(line))
            for line in self._read_lines("event_registry_runs"):
                self._runs.append(json.loads(line))
        except Exception:
            pass

    def _read_lines(self, collection: str) -> list[str]:
        path = self._path(collection)
        if not path.exists():
            return []
        try:
            return [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        except Exception:
            return []

    def _append_line(self, collection: str, payload: dict[str, Any]) -> None:
        try:
            self.ensure_storage()
            with self._path(collection).open("a", encoding="utf-8") as f:
                f.write(json.dumps(payload, default=str) + "\n")
        except Exception:
            pass

    def _rewrite_candidates(self) -> None:
        try:
            self.ensure_storage()
            with self._path("event_candidates").open("w", encoding="utf-8") as f:
                for rec in self._candidates.values():
                    f.write(json.dumps(rec.as_dict(), default=str) + "\n")
        except Exception:
            pass

    # ---- Raw ----

    def insert_raw(self, payload: dict[str, Any]) -> None:
        self._load_if_needed()
        row = dict(payload)
        row.setdefault("received_at", _now_iso())
        with self._lock:
            self._raw.append(row)
            self._append_line("event_raw", row)

    def list_raw(self) -> list[dict[str, Any]]:
        self._load_if_needed()
        return list(self._raw)

    # ---- Canonical ----

    def upsert_canonical(self, event_id: str, payload: dict[str, Any]) -> None:
        self._load_if_needed()
        row = dict(payload)
        row["event_id"] = event_id
        row.setdefault("updated_at", _now_iso())
        with self._lock:
            self._canonical[event_id] = row
            # Canonical is rewritten as a snapshot, not append-only.
            try:
                self.ensure_storage()
                with self._path("event_canonical").open("w", encoding="utf-8") as f:
                    for v in self._canonical.values():
                        f.write(json.dumps(v, default=str) + "\n")
            except Exception:
                pass

    def get_canonical(self, event_id: str) -> dict[str, Any] | None:
        self._load_if_needed()
        return self._canonical.get(event_id)

    def list_canonical(self) -> list[dict[str, Any]]:
        self._load_if_needed()
        return list(self._canonical.values())

    # ---- Candidates ----

    def upsert_candidate(self, candidate: EventCandidate) -> EventCandidate:
        """Idempotent: re-inserting the same ``event_id`` updates in place."""
        if candidate.family not in SUPPORTED_FAMILIES:
            raise ValueError(f"unsupported family: {candidate.family}")
        if candidate.status not in VALID_STATUSES:
            candidate.status = STATUS_DETECTED
        self._load_if_needed()
        with self._lock:
            existing = self._candidates.get(candidate.event_id)
            now = _now_iso()
            if existing is None:
                candidate.created_at = candidate.created_at or now
                candidate.updated_at = now
                self._candidates[candidate.event_id] = candidate
            else:
                # Preserve created_at, refresh updated_at, merge fields.
                existing.updated_at = now
                # Merge non-None fields from candidate into existing.
                for fname, fval in candidate.as_dict().items():
                    if fname in ("created_at",):
                        continue
                    if fval is None or fval == "" or fval == [] or fval is False:
                        # Skip falsy unless they are explicitly meaningful (status changes are explicit).
                        if fname == "status" and fval:
                            setattr(existing, fname, fval)
                        continue
                    setattr(existing, fname, fval)
                # Always allow status update when present.
                if candidate.status:
                    existing.status = candidate.status
                candidate = existing
            self._rewrite_candidates()
            return candidate

    def get_candidate(self, event_id: str) -> EventCandidate | None:
        self._load_if_needed()
        return self._candidates.get(event_id)

    def list_candidates(
        self,
        *,
        family: str | None = None,
        status: str | None = None,
    ) -> list[EventCandidate]:
        self._load_if_needed()
        with self._lock:
            recs = list(self._candidates.values())
        if family:
            recs = [r for r in recs if r.family == family]
        if status:
            recs = [r for r in recs if r.status == status]
        return sorted(recs, key=lambda r: r.updated_at, reverse=True)

    # ---- Sources ----

    def record_source(self, event_id: str, source: str, payload: dict[str, Any]) -> None:
        self._load_if_needed()
        row = {
            "event_id": event_id,
            "source": source,
            "payload": payload,
            "recorded_at": _now_iso(),
        }
        with self._lock:
            self._sources.append(row)
            self._append_line("event_sources", row)

    def list_sources(self, *, event_id: str | None = None) -> list[dict[str, Any]]:
        self._load_if_needed()
        rows = list(self._sources)
        if event_id:
            rows = [r for r in rows if r.get("event_id") == event_id]
        return rows

    # ---- Runs ----

    def record_run(self, metadata: dict[str, Any]) -> None:
        self._load_if_needed()
        row = dict(metadata)
        row.setdefault("run_at", _now_iso())
        with self._lock:
            self._runs.append(row)
            self._append_line("event_registry_runs", row)

    def list_runs(self) -> list[dict[str, Any]]:
        self._load_if_needed()
        return list(self._runs)

    # ---- Stats ----

    def snapshot(self) -> dict[str, Any]:
        self._load_if_needed()
        recs = self.list_candidates()
        by_family = {f: sum(1 for r in recs if r.family == f) for f in SUPPORTED_FAMILIES}
        by_status = {s: sum(1 for r in recs if r.status == s) for s in VALID_STATUSES}
        return {
            "base_path": str(self._base_path),
            "raw_count": len(self._raw),
            "canonical_count": len(self._canonical),
            "candidates_count": len(recs),
            "sources_count": len(self._sources),
            "runs_count": len(self._runs),
            "by_family": by_family,
            "by_status": by_status,
            "research_only": True,
            "paper_filter_enabled": False,
            "can_send_real_orders": False,
            "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE,
        }
