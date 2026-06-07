"""V8.2.9.5 — Strategy Tournament with REAL outcomes (research-only).

Thin wrapper over ``strategy_tournament_rc1`` that feeds the tournament
the canonical REAL outcome (``canonical_net_pnl_est`` from
``canonical_outcome_real_v8_2_9_5``) instead of the fixed proxy. Only
rows with ``canonical_is_real == True`` are scored. When real-outcome
coverage is too low, EVERYTHING returns ``NEED_MORE_DATA`` — no
strategy can earn a sandbox label off proxy data.

Hard contract: research-only. Entry/cohort predicates still use ONLY
ex-ante features (inherited from RC1's whitelist). Real outcomes are
used ONLY to score, never as entry features.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from . import FINAL_RECOMMENDATION_NO_LIVE, STATUS_NEED_DATA, STATUS_OK
from .canonical_outcome_real_v8_2_9_5 import canonicalize_real
from .strategy_tournament_rc1 import (
    STATUS_NEED_MORE_DATA,
    StrategySpec,
    default_strategy_suite,
    run_tournament,
)


# Minimum fraction of candidates that must have a REAL canonical outcome
# before any tournament verdict is trusted. Below this, force
# NEED_MORE_DATA across the board.
MIN_REAL_COVERAGE_RATIO = 0.50
# Minimum absolute count of real-outcome rows.
MIN_REAL_ROWS = 40

OUTCOME_FIELD_REAL = "canonical_net_pnl_est"


@dataclass
class TournamentRealReport:
    hours: int
    generated_at: str
    candidates_input: int = 0
    canonical_real_ok_ratio: float = 0.0
    real_rows_used: int = 0
    canonical_source_top: str = ""
    coverage_sufficient: bool = False
    tournament_real_status: str = STATUS_NEED_MORE_DATA
    tournament_real_best_strategy: str = ""
    tournament_real_best_status: str = STATUS_NEED_MORE_DATA
    paper_sandbox_candidates_real: int = 0
    results: list[dict[str, Any]] = field(default_factory=list)
    by_status: dict[str, int] = field(default_factory=dict)
    # Proxy-vs-real diagnostics (passthrough from canonical/bridge).
    proxy_only_count: int = 0
    need_data_count: int = 0
    status: str = STATUS_NEED_DATA
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _merge_real_outcome(
    candidates: list[dict[str, Any]],
    canonical_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Attach ``canonical_net_pnl_est`` + ``canonical_is_real`` to each
    candidate by (observation_id, symbol, timestamp)."""
    canon_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for cr in canonical_rows:
        key = (
            str(cr.get("observation_id") or ""),
            str(cr.get("symbol") or ""),
            str(cr.get("timestamp") or ""),
        )
        canon_by_key.setdefault(key, cr)
    merged: list[dict[str, Any]] = []
    for c in candidates:
        key = (
            str(c.get("observation_id") or c.get("signal_id") or ""),
            str(c.get("symbol") or ""),
            str(c.get("timestamp") or ""),
        )
        cr = canon_by_key.get(key)
        row = dict(c)
        if cr:
            row["canonical_net_pnl_est"] = cr.get("canonical_net_pnl_est")
            row["canonical_is_real"] = bool(cr.get("canonical_is_real"))
            row["canonical_source"] = cr.get("canonical_source")
        else:
            row["canonical_net_pnl_est"] = None
            row["canonical_is_real"] = False
        merged.append(row)
    return merged


def run_tournament_real(
    candidates: Iterable[dict[str, Any]] | None,
    path_rows: Iterable[dict[str, Any]] | None,
    *,
    hours: int = 168,
    strategies: Iterable[StrategySpec] | None = None,
) -> TournamentRealReport:
    """Run the strategy tournament on REAL canonical outcomes."""
    report = TournamentRealReport(
        hours=int(hours),
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
    cand_list = list(candidates or [])
    report.candidates_input = len(cand_list)
    if not cand_list:
        return report

    canonical = canonicalize_real(cand_list, path_rows or [], hours=hours)
    report.canonical_real_ok_ratio = canonical.canonical_real_ok_ratio
    report.canonical_source_top = canonical.canonical_source_top
    report.proxy_only_count = canonical.proxy_only_count
    report.need_data_count = canonical.need_data_count

    merged = _merge_real_outcome(cand_list, canonical.rows)
    # V8.2.9.6.1 — strict local contract. Even though the canonicalizer
    # is supposed to guarantee both invariants, the tournament refuses
    # to depend blindly on it: a row contributes only when
    # ``canonical_is_real is True`` AND ``canonical_net_pnl_est`` is a
    # finite numeric value (no None, no NaN, no bool). Anything else is
    # excluded — proxy or partial outcomes can never reach a sandbox
    # status this way.
    def _is_numeric_real_row(r: dict) -> bool:
        if r.get("canonical_is_real") is not True:
            return False
        v = r.get("canonical_net_pnl_est")
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return False
        return v == v  # excludes NaN
    real_rows = [r for r in merged if _is_numeric_real_row(r)]
    report.real_rows_used = len(real_rows)

    coverage_ok = (
        canonical.canonical_real_ok_ratio >= MIN_REAL_COVERAGE_RATIO
        and len(real_rows) >= MIN_REAL_ROWS
    )
    report.coverage_sufficient = coverage_ok
    report.status = STATUS_OK

    if not coverage_ok:
        # Insufficient real coverage → everything NEED_MORE_DATA. We do
        # NOT run the tournament on proxy data for a verdict.
        report.tournament_real_status = STATUS_NEED_MORE_DATA
        report.tournament_real_best_status = STATUS_NEED_MORE_DATA
        report.tournament_real_best_strategy = ""
        report.paper_sandbox_candidates_real = 0
        return report

    specs = list(strategies) if strategies is not None else default_strategy_suite()
    # Force outcome_field to the real canonical column for every spec
    # BEFORE running, so the tournament scores REAL outcomes only.
    for spec in specs:
        spec.outcome_field = OUTCOME_FIELD_REAL
    inner = run_tournament(real_rows, specs, hours=hours)
    report.results = inner.results
    report.by_status = inner.by_status
    report.tournament_real_best_strategy = inner.best_strategy
    report.tournament_real_best_status = inner.best_status
    report.tournament_real_status = inner.best_status
    # Paper sandbox count — and HARD rule: paper sandbox requires real.
    # Since all scored rows are canonical_is_real=True here, the RC1
    # PAPER_SANDBOX_CANDIDATE_RESEARCH_ONLY status is admissible.
    report.paper_sandbox_candidates_real = sum(
        1 for r in inner.results
        if r.get("status") == "PAPER_SANDBOX_CANDIDATE_RESEARCH_ONLY"
    )
    return report
