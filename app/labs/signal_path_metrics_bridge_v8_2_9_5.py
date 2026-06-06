"""V8.2.9.5 — Signal Path Metrics Bridge (research-only).

Joins rebound LONG candidates (or any candidate dicts carrying
``observation_id`` / ``signal_id`` + ``symbol`` + ``timestamp``) to the
REAL path outcomes stored in ``signal_path_metrics``. Born from the
UltraCode finding that the V8.2.9.x rebound candidates only carried a
FIXED PROXY outcome (TP ≈ +0.81 / SL ≈ −0.75) and never touched the
real ``final_return_pct`` / ``max_favorable_pct`` recorded per
observation.

Hard contract (research-only / read-only):

- Never opens orders, never mutates runtime, never writes to the DB.
- The join key is ``observation_id`` (= ``signal_observations.id``).
  Fallback to ``(symbol, timestamp)`` ONLY when the candidate has no
  observation_id AND that (symbol, timestamp) maps to exactly one path
  row. Never join on timestamp alone across symbols.
- Real outcomes are used ONLY ex-post (to score / reconcile) — never as
  an entry feature.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from . import FINAL_RECOMMENDATION_NO_LIVE, STATUS_NEED_DATA, STATUS_OK


# Path status.
PATH_FOUND = "PATH_FOUND"
PATH_MISSING = "PATH_MISSING"
PATH_AMBIGUOUS_JOIN = "PATH_AMBIGUOUS_JOIN"
PATH_INCOMPLETE = "PATH_INCOMPLETE"

# Join method.
JOIN_OBSERVATION_ID = "observation_id"
JOIN_SYMBOL_TIMESTAMP_UNIQUE = "symbol_timestamp_unique"
JOIN_MISSING = "missing"
JOIN_AMBIGUOUS = "ambiguous"

# Proxy-vs-real mismatch classification.
MM_MATCH = "MATCH"
MM_SIGN_MISMATCH = "SIGN_MISMATCH"
MM_MAGNITUDE_MISMATCH = "MAGNITUDE_MISMATCH"
MM_MISSING_REAL = "MISSING_REAL"
MM_MISSING_PROXY = "MISSING_PROXY"
MM_AMBIGUOUS_JOIN = "AMBIGUOUS_JOIN"

REAL_OUTCOME_SOURCE = "SIGNAL_PATH_METRICS"

# A path row is considered usable when its status indicates a closed /
# completed observation (not still active / pending).
COMPLETED_STATUSES = frozenset({"completed", "closed", "done", "finalized"})

# Magnitude mismatch tolerance (percentage points) between proxy and real.
MAGNITUDE_TOLERANCE_PCT = 0.50


@dataclass
class BridgedRow:
    observation_id: Any
    symbol: str
    timestamp: str
    side: str
    entry_price: float | None
    path_status: str
    path_join_method: str
    real_final_return_pct: float | None
    real_max_favorable_pct: float | None
    real_max_adverse_pct: float | None
    real_first_barrier_hit: str
    real_bars_tracked: int | None
    real_bars_to_mfe: int | None
    real_bars_to_mae: int | None
    real_outcome_win: bool | None
    real_outcome_source: str
    proxy_net_pnl_est: float | None
    proxy_vs_real_delta: float | None
    proxy_matches_real_sign: bool | None
    proxy_mismatch_type: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BridgeReport:
    hours: int
    generated_at: str
    total_candidates: int = 0
    path_found_count: int = 0
    path_missing_count: int = 0
    path_ambiguous_count: int = 0
    path_incomplete_count: int = 0
    path_coverage_ratio: float = 0.0
    proxy_sign_mismatch_count: int = 0
    proxy_sign_mismatch_ratio: float = 0.0
    proxy_magnitude_mismatch_count: int = 0
    proxy_magnitude_mismatch_ratio: float = 0.0
    real_winrate: float = 0.0
    real_net_ev_avg: float = 0.0
    proxy_winrate: float = 0.0
    proxy_net_ev_avg: float = 0.0
    rows: list[dict[str, Any]] = field(default_factory=list)
    status: str = STATUS_NEED_DATA
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _f(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _i(value: Any) -> int | None:
    f = _f(value)
    return int(f) if f is not None else None


def _norm_id(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def build_path_indexes(
    path_rows: Iterable[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[tuple[str, str], dict[str, Any]], dict[tuple[str, str], int]]:
    """Index path-metric rows by observation_id and by (symbol, timestamp).

    ``(symbol, timestamp)`` is only safe when unique — ``st_counts``
    lets the join refuse ambiguous matches.
    """
    by_obs: dict[str, dict[str, Any]] = {}
    by_st: dict[tuple[str, str], dict[str, Any]] = {}
    st_counts: dict[tuple[str, str], int] = {}
    for r in path_rows:
        oid = _norm_id(r.get("observation_id"))
        if oid is not None:
            by_obs.setdefault(oid, r)
        sym = str(r.get("symbol") or "").upper()
        ts = str(r.get("timestamp") or r.get("created_at") or "")
        if sym and ts:
            key = (sym, ts)
            st_counts[key] = st_counts.get(key, 0) + 1
            by_st.setdefault(key, r)
    return by_obs, by_st, st_counts


def _join(
    candidate: dict[str, Any],
    by_obs: dict[str, dict[str, Any]],
    by_st: dict[tuple[str, str], dict[str, Any]],
    st_counts: dict[tuple[str, str], int],
) -> tuple[dict[str, Any] | None, str]:
    """Resolve the path row for a candidate. Returns ``(path_row, method)``."""
    oid = _norm_id(candidate.get("observation_id")) or _norm_id(
        candidate.get("signal_id")
    )
    if oid is not None and oid in by_obs:
        return by_obs[oid], JOIN_OBSERVATION_ID
    sym = str(candidate.get("symbol") or "").upper()
    ts = str(candidate.get("timestamp") or "")
    if sym and ts:
        key = (sym, ts)
        if st_counts.get(key, 0) == 1:
            return by_st[key], JOIN_SYMBOL_TIMESTAMP_UNIQUE
        if st_counts.get(key, 0) > 1:
            return None, JOIN_AMBIGUOUS
    return None, JOIN_MISSING


def _real_win(path_row: dict[str, Any]) -> bool | None:
    fr = _f(path_row.get("final_return_pct"))
    if fr is not None:
        return fr > 0
    barrier = str(path_row.get("first_barrier_hit") or "").upper()
    if barrier.startswith("TP"):
        return True
    if barrier.startswith("SL"):
        return False
    return None


def _classify_mismatch(
    proxy: float | None,
    real: float | None,
    join_method: str,
) -> tuple[str, float | None, bool | None]:
    """Return ``(mismatch_type, delta, matches_sign)``."""
    if join_method == JOIN_AMBIGUOUS:
        return MM_AMBIGUOUS_JOIN, None, None
    if real is None:
        return MM_MISSING_REAL, None, None
    if proxy is None:
        return MM_MISSING_PROXY, None, None
    delta = proxy - real
    same_sign = (proxy >= 0) == (real >= 0)
    if not same_sign:
        return MM_SIGN_MISMATCH, delta, False
    if abs(delta) > MAGNITUDE_TOLERANCE_PCT:
        return MM_MAGNITUDE_MISMATCH, delta, True
    return MM_MATCH, delta, True


def bridge_candidates(
    candidates: Iterable[dict[str, Any]] | None,
    path_rows: Iterable[dict[str, Any]] | None,
    *,
    hours: int = 168,
) -> BridgeReport:
    """Join candidates to real path outcomes and compute proxy-vs-real
    diagnostics."""
    report = BridgeReport(
        hours=int(hours),
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
    cand_list = list(candidates or [])
    report.total_candidates = len(cand_list)
    by_obs, by_st, st_counts = build_path_indexes(path_rows or [])
    if not cand_list:
        return report

    real_nets: list[float] = []
    proxy_nets: list[float] = []
    real_wins = 0
    real_n = 0
    proxy_wins = 0
    proxy_n = 0
    for c in cand_list:
        proxy = _f(c.get("net_pnl_est"))
        path_row, method = _join(c, by_obs, by_st, st_counts)
        if path_row is None:
            path_status = (
                PATH_AMBIGUOUS_JOIN if method == JOIN_AMBIGUOUS else PATH_MISSING
            )
            real_fr = real_mfe = real_mae = None
            real_barrier = ""
            real_bt = real_bmfe = real_bmae = None
            real_win = None
            if method == JOIN_AMBIGUOUS:
                report.path_ambiguous_count += 1
            else:
                report.path_missing_count += 1
        else:
            real_fr = _f(path_row.get("final_return_pct"))
            real_mfe = _f(path_row.get("max_favorable_pct"))
            real_mae = _f(path_row.get("max_adverse_pct"))
            real_barrier = str(path_row.get("first_barrier_hit") or "")
            real_bt = _i(path_row.get("bars_tracked"))
            real_bmfe = _i(path_row.get("bars_to_mfe"))
            real_bmae = _i(path_row.get("bars_to_mae"))
            real_win = _real_win(path_row)
            status_val = str(path_row.get("status") or "").lower()
            if real_fr is None and not real_barrier:
                path_status = PATH_INCOMPLETE
                report.path_incomplete_count += 1
            elif status_val and status_val not in COMPLETED_STATUSES and real_fr is None:
                path_status = PATH_INCOMPLETE
                report.path_incomplete_count += 1
            else:
                path_status = PATH_FOUND
                report.path_found_count += 1
                if real_fr is not None:
                    real_nets.append(real_fr)
                    real_n += 1
                    if real_fr > 0:
                        real_wins += 1
        mismatch_type, delta, matches_sign = _classify_mismatch(
            proxy, real_fr, method,
        )
        if proxy is not None:
            proxy_nets.append(proxy)
            proxy_n += 1
            if proxy > 0:
                proxy_wins += 1
        if mismatch_type == MM_SIGN_MISMATCH:
            report.proxy_sign_mismatch_count += 1
        elif mismatch_type == MM_MAGNITUDE_MISMATCH:
            report.proxy_magnitude_mismatch_count += 1
        report.rows.append(BridgedRow(
            observation_id=c.get("observation_id") or c.get("signal_id"),
            symbol=str(c.get("symbol") or ""),
            timestamp=str(c.get("timestamp") or ""),
            side=str(c.get("side") or "LONG"),
            entry_price=_f(c.get("entry_price")),
            path_status=path_status,
            path_join_method=method,
            real_final_return_pct=real_fr,
            real_max_favorable_pct=real_mfe,
            real_max_adverse_pct=real_mae,
            real_first_barrier_hit=real_barrier,
            real_bars_tracked=real_bt,
            real_bars_to_mfe=real_bmfe,
            real_bars_to_mae=real_bmae,
            real_outcome_win=real_win,
            real_outcome_source=REAL_OUTCOME_SOURCE,
            proxy_net_pnl_est=proxy,
            proxy_vs_real_delta=delta,
            proxy_matches_real_sign=matches_sign,
            proxy_mismatch_type=mismatch_type,
        ).as_dict())

    n = max(report.total_candidates, 1)
    report.path_coverage_ratio = report.path_found_count / n
    report.proxy_sign_mismatch_ratio = report.proxy_sign_mismatch_count / n
    report.proxy_magnitude_mismatch_ratio = (
        report.proxy_magnitude_mismatch_count / n
    )
    if real_n:
        report.real_winrate = real_wins / real_n
        report.real_net_ev_avg = sum(real_nets) / real_n
    if proxy_n:
        report.proxy_winrate = proxy_wins / proxy_n
        report.proxy_net_ev_avg = sum(proxy_nets) / proxy_n
    report.rows = report.rows[:5000]
    report.status = STATUS_OK
    return report
