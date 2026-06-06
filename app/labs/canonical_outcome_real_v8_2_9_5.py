"""V8.2.9.5 — Canonical Outcome (real-aware) (research-only).

Wraps the V8.2.9.3 canonicalizer with a higher-authority source: the
REAL path outcome from ``signal_path_metrics`` (via the V8.2.9.5
bridge). Priority cascade:

1. ``SIGNAL_PATH_METRICS`` — bridge ``path_status == PATH_FOUND``.
2. ``OHLCV_BARRIER_REPLAY`` — candidate carries a valid ``ohlcv_path``.
3. ``BASELINE_NET_PNL_PROXY`` — fixed proxy ``net_pnl_est`` /
   ``baseline_net_pnl_est``. Flagged as NOT-for-edge-validation.
4. ``NEED_DATA``.

Hard rule (enforced downstream): a row with ``canonical_is_real=False``
must never reach a paper-sandbox status. Proxy outcomes are diagnostic
only.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from . import FINAL_RECOMMENDATION_NO_LIVE, STATUS_NEED_DATA, STATUS_OK
from .exit_bar_by_bar_replay_v8_2_9_3 import (
    _has_valid_path,
    replay_long_baseline,
    replay_short_baseline,
)
from .signal_path_metrics_bridge_v8_2_9_5 import (
    PATH_AMBIGUOUS_JOIN,
    PATH_FOUND,
    bridge_candidates,
)


# Canonical source.
SOURCE_SIGNAL_PATH_METRICS = "SIGNAL_PATH_METRICS"
SOURCE_OHLCV_BARRIER_REPLAY = "OHLCV_BARRIER_REPLAY"
SOURCE_BASELINE_PROXY = "BASELINE_NET_PNL_PROXY"
SOURCE_NEED_DATA = "NEED_DATA"

# Canonical quality.
QUALITY_REAL_PATH = "REAL_PATH"
QUALITY_REAL_OHLCV_REPLAY = "REAL_OHLCV_REPLAY"
QUALITY_PROXY_ONLY = "PROXY_ONLY"
QUALITY_NEED_DATA = "NEED_DATA"

# Canonical warning.
WARN_NONE = "NONE"
WARN_PROXY_ONLY = "PROXY_ONLY_NOT_FOR_EDGE_VALIDATION"
WARN_PATH_MISSING = "PATH_MISSING"
WARN_JOIN_AMBIGUOUS = "JOIN_AMBIGUOUS"
WARN_SIGN_MISMATCH = "SIGN_MISMATCH"


@dataclass
class CanonicalRealRow:
    observation_id: Any
    symbol: str
    timestamp: str
    side: str
    canonical_source: str
    canonical_is_real: bool
    canonical_net_pnl_est: float | None
    canonical_win: bool | None
    canonical_mfe_pct: float | None
    canonical_mae_pct: float | None
    canonical_first_barrier_hit: str
    canonical_quality: str
    canonical_warning: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CanonicalRealReport:
    hours: int
    generated_at: str
    rows_audited: int = 0
    real_path_count: int = 0
    ohlcv_replay_count: int = 0
    proxy_only_count: int = 0
    need_data_count: int = 0
    canonical_real_ok_ratio: float = 0.0
    canonical_source_top: str = ""
    by_source: dict[str, int] = field(default_factory=dict)
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


def _canonical_from_bridge(bridged: dict[str, Any], candidate: dict[str, Any]) -> CanonicalRealRow:
    """Build a canonical row from a single bridged candidate."""
    symbol = str(bridged.get("symbol") or candidate.get("symbol") or "")
    ts = str(bridged.get("timestamp") or candidate.get("timestamp") or "")
    side = str(bridged.get("side") or candidate.get("side") or "LONG").upper()
    obs = bridged.get("observation_id") or candidate.get("observation_id")
    path_status = str(bridged.get("path_status") or "")

    # 1. SIGNAL_PATH_METRICS — highest authority.
    if path_status == PATH_FOUND:
        real_fr = _f(bridged.get("real_final_return_pct"))
        warning = WARN_NONE
        if bridged.get("proxy_mismatch_type") == "SIGN_MISMATCH":
            warning = WARN_SIGN_MISMATCH
        return CanonicalRealRow(
            observation_id=obs, symbol=symbol, timestamp=ts, side=side,
            canonical_source=SOURCE_SIGNAL_PATH_METRICS,
            canonical_is_real=True,
            canonical_net_pnl_est=real_fr,
            canonical_win=(real_fr > 0) if real_fr is not None else bridged.get("real_outcome_win"),
            canonical_mfe_pct=_f(bridged.get("real_max_favorable_pct")),
            canonical_mae_pct=_f(bridged.get("real_max_adverse_pct")),
            canonical_first_barrier_hit=str(bridged.get("real_first_barrier_hit") or ""),
            canonical_quality=QUALITY_REAL_PATH,
            canonical_warning=warning,
        )

    # 2. OHLCV_BARRIER_REPLAY — candidate carries a valid bar path.
    path = candidate.get("ohlcv_path")
    entry = candidate.get("entry_price")
    tp = candidate.get("take_profit_1") or candidate.get("tp_price")
    sl = candidate.get("stop_loss") or candidate.get("sl_price")
    if (
        side in {"LONG", "SHORT"}
        and _has_valid_path(path)
        and isinstance(entry, (int, float))
        and isinstance(tp, (int, float))
        and isinstance(sl, (int, float))
    ):
        if side == "LONG":
            replay = replay_long_baseline(float(entry), float(tp), float(sl), path)
        else:
            replay = replay_short_baseline(float(entry), float(tp), float(sl), path)
        net = _f(replay.get("net_pct"))
        if net is not None:
            return CanonicalRealRow(
                observation_id=obs, symbol=symbol, timestamp=ts, side=side,
                canonical_source=SOURCE_OHLCV_BARRIER_REPLAY,
                canonical_is_real=True,
                canonical_net_pnl_est=net,
                canonical_win=net > 0,
                canonical_mfe_pct=None,
                canonical_mae_pct=None,
                canonical_first_barrier_hit=str(replay.get("exit_reason") or ""),
                canonical_quality=QUALITY_REAL_OHLCV_REPLAY,
                canonical_warning=WARN_NONE,
            )

    # 3. BASELINE proxy — diagnostic only, NEVER edge validation.
    proxy = _f(candidate.get("net_pnl_est"))
    if proxy is None:
        proxy = _f(candidate.get("baseline_net_pnl_est"))
    if proxy is not None:
        warning = WARN_PROXY_ONLY
        if path_status == PATH_AMBIGUOUS_JOIN:
            warning = WARN_JOIN_AMBIGUOUS
        return CanonicalRealRow(
            observation_id=obs, symbol=symbol, timestamp=ts, side=side,
            canonical_source=SOURCE_BASELINE_PROXY,
            canonical_is_real=False,
            canonical_net_pnl_est=proxy,
            canonical_win=proxy > 0,
            canonical_mfe_pct=_f(candidate.get("mfe_pct_outcome")),
            canonical_mae_pct=_f(candidate.get("mae_pct_outcome")),
            canonical_first_barrier_hit=str(candidate.get("barrier_result_outcome") or ""),
            canonical_quality=QUALITY_PROXY_ONLY,
            canonical_warning=warning,
        )

    # 4. NEED_DATA.
    warning = WARN_PATH_MISSING
    if path_status == PATH_AMBIGUOUS_JOIN:
        warning = WARN_JOIN_AMBIGUOUS
    return CanonicalRealRow(
        observation_id=obs, symbol=symbol, timestamp=ts, side=side,
        canonical_source=SOURCE_NEED_DATA,
        canonical_is_real=False,
        canonical_net_pnl_est=None,
        canonical_win=None,
        canonical_mfe_pct=None,
        canonical_mae_pct=None,
        canonical_first_barrier_hit="",
        canonical_quality=QUALITY_NEED_DATA,
        canonical_warning=warning,
    )


def canonicalize_real(
    candidates: Iterable[dict[str, Any]] | None,
    path_rows: Iterable[dict[str, Any]] | None,
    *,
    hours: int = 168,
) -> CanonicalRealReport:
    """Compute canonical real outcomes for a candidate set, joining real
    path metrics first and falling back to OHLCV replay / proxy."""
    report = CanonicalRealReport(
        hours=int(hours),
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
    cand_list = list(candidates or [])
    report.rows_audited = len(cand_list)
    if not cand_list:
        return report
    bridge = bridge_candidates(cand_list, path_rows or [], hours=hours)
    bridged_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for b in bridge.rows:
        key = (
            str(b.get("observation_id") or ""),
            str(b.get("symbol") or ""),
            str(b.get("timestamp") or ""),
        )
        bridged_by_key.setdefault(key, b)
    by_source: dict[str, int] = {}
    for c in cand_list:
        key = (
            str(c.get("observation_id") or c.get("signal_id") or ""),
            str(c.get("symbol") or ""),
            str(c.get("timestamp") or ""),
        )
        bridged = bridged_by_key.get(key, {})
        row = _canonical_from_bridge(bridged, c)
        d = row.as_dict()
        report.rows.append(d)
        by_source[row.canonical_source] = by_source.get(row.canonical_source, 0) + 1
        if row.canonical_source == SOURCE_SIGNAL_PATH_METRICS:
            report.real_path_count += 1
        elif row.canonical_source == SOURCE_OHLCV_BARRIER_REPLAY:
            report.ohlcv_replay_count += 1
        elif row.canonical_source == SOURCE_BASELINE_PROXY:
            report.proxy_only_count += 1
        else:
            report.need_data_count += 1
    report.by_source = by_source
    real_ok = report.real_path_count + report.ohlcv_replay_count
    report.canonical_real_ok_ratio = real_ok / max(report.rows_audited, 1)
    if by_source:
        report.canonical_source_top = max(by_source.items(), key=lambda kv: kv[1])[0]
    report.rows = report.rows[:5000]
    report.status = STATUS_OK
    return report
