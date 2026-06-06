"""V8.2.9.3 — Outcome Field Canonicalizer (research-only).

Defines the canonical research outcome for a LONG / SHORT row by
walking a strict priority cascade:

1. ``OHLCV_BARRIER_REPLAY`` — bar-by-bar replay over the row's
   ``ohlcv_path`` produces a deterministic exit. Highest authority.
2. ``BASELINE_NET_PNL`` — fallback to ``baseline_net_pnl_est`` when
   no path is available.
3. ``FUTURE_RETURN_DIAGNOSTIC_ONLY`` — surfaces when only forward
   returns (``ret_*_pct``) are present. Status is NEED_OHLCV_PATH and
   the canonical value is left None — forward returns NEVER become
   the canonical source.
4. ``NEED_DATA`` — nothing usable.

Sign suspect / field mismatch checks layer on top of the cascade and
override the status when triggered.

Hard contract:

- research-only;
- no DB writes, no production label changes;
- forward-return columns are only inspected to surface diagnostic
  signals — they are never promoted to the canonical source when a
  bar-by-bar path or a baseline value exists.
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


# Status values.
CANONICAL_STATUS_OK = "OK"
CANONICAL_STATUS_NEED_OHLCV_PATH = "NEED_OHLCV_PATH"
CANONICAL_STATUS_FIELD_MISMATCH = "FIELD_MISMATCH"
CANONICAL_STATUS_SIGN_SUSPECT = "SIGN_SUSPECT"
CANONICAL_STATUS_NEED_DATA = "NEED_DATA"

# Source values.
CANONICAL_SOURCE_OHLCV = "OHLCV_BARRIER_REPLAY"
CANONICAL_SOURCE_BASELINE = "BASELINE_NET_PNL"
CANONICAL_SOURCE_FUTURE_RETURN_DIAGNOSTIC = "FUTURE_RETURN_DIAGNOSTIC_ONLY"
CANONICAL_SOURCE_NEED_DATA = "NEED_DATA"

# Heuristic thresholds.
SIGN_SUSPECT_RET_THRESHOLD_PCT = 0.50
SIGN_SUSPECT_NET_THRESHOLD_PCT = 0.30
FIELD_MISMATCH_EPSILON = 0.01


@dataclass
class CanonicalRow:
    symbol: str
    timestamp: str
    side: str
    canonical_outcome_status: str
    canonical_net_pnl_est: float | None
    canonical_win: bool | None
    canonical_source: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CanonicalReport:
    hours: int
    generated_at: str
    rows_audited: int = 0
    ok_count: int = 0
    need_ohlcv_path_count: int = 0
    field_mismatch_count: int = 0
    sign_suspect_count: int = 0
    need_data_count: int = 0
    canonical_outcome_ok_ratio: float = 0.0
    canonical_outcome_source_top: str = ""
    by_source: dict[str, int] = field(default_factory=dict)
    rows: list[dict[str, Any]] = field(default_factory=list)
    status: str = STATUS_NEED_DATA
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _has_baseline(row: dict[str, Any]) -> bool:
    return isinstance(row.get("baseline_net_pnl_est"), (int, float))


def _has_future_return(row: dict[str, Any]) -> bool:
    for key in ("ret_1h_pct", "ret_4h_pct", "ret_24h_pct"):
        if isinstance(row.get(key), (int, float)):
            return True
    return False


def _sign_suspect_long(row: dict[str, Any], canonical_net: float | None) -> bool:
    """Detect LONG candidates whose canonical net disagrees with a clear
    positive forward return — purely diagnostic, never used as an
    input."""
    if str(row.get("side", "")).upper() != "LONG":
        return False
    if not isinstance(canonical_net, (int, float)):
        return False
    ret_4h = row.get("ret_4h_pct")
    if not isinstance(ret_4h, (int, float)):
        return False
    return (
        float(ret_4h) > SIGN_SUSPECT_RET_THRESHOLD_PCT
        and float(canonical_net) < -SIGN_SUSPECT_NET_THRESHOLD_PCT
    )


def _sign_suspect_short(row: dict[str, Any], canonical_net: float | None) -> bool:
    """Detect SHORT candidates whose canonical net disagrees with a clear
    negative forward return. Mirror of ``_sign_suspect_long`` — a SHORT
    should profit when ``ret_4h`` is strongly negative; if the canonical
    net is strongly negative too, the outcome label is suspect."""
    if str(row.get("side", "")).upper() != "SHORT":
        return False
    if not isinstance(canonical_net, (int, float)):
        return False
    ret_4h = row.get("ret_4h_pct")
    if not isinstance(ret_4h, (int, float)):
        return False
    return (
        float(ret_4h) < -SIGN_SUSPECT_RET_THRESHOLD_PCT
        and float(canonical_net) < -SIGN_SUSPECT_NET_THRESHOLD_PCT
    )


def _short_barriers_inverted(entry: float, tp: float, sl: float) -> bool:
    """SHORT requires ``tp < entry < sl``. Anything else is inverted."""
    return not (tp < entry < sl)


def _long_barriers_inverted(entry: float, tp: float, sl: float) -> bool:
    """LONG requires ``sl < entry < tp``. Anything else is inverted."""
    return not (sl < entry < tp)


def _apply_sign_suspect_override(
    row: dict[str, Any], canonical_net: float, side: str,
    status: str, reason: str,
) -> tuple[str, str]:
    """Side-aware sign-suspect override. Returns ``(status, reason)``."""
    if side == "LONG" and _sign_suspect_long(row, canonical_net):
        return CANONICAL_STATUS_SIGN_SUSPECT, (
            reason + ";sign_suspect_long_ret_4h_positive_but_canonical_negative"
        )
    if side == "SHORT" and _sign_suspect_short(row, canonical_net):
        return CANONICAL_STATUS_SIGN_SUSPECT, (
            reason + ";sign_suspect_short_ret_4h_negative_but_canonical_negative"
        )
    return status, reason


def canonicalize_row(row: dict[str, Any]) -> CanonicalRow:
    """Compute the canonical outcome for a single row. Supports LONG and
    SHORT under OHLCV bar-by-bar replay (V8.2.9.4)."""
    symbol = str(row.get("symbol") or "")
    timestamp = str(row.get("timestamp") or "")
    side = str(row.get("side", "LONG")).upper() or "LONG"

    # 1. OHLCV path replay — highest authority. Supports LONG and SHORT.
    path = row.get("ohlcv_path")
    entry = row.get("entry_price")
    tp = row.get("take_profit_1") or row.get("tp_price")
    sl = row.get("stop_loss") or row.get("sl_price")
    if (
        side in {"LONG", "SHORT"}
        and _has_valid_path(path)
        and isinstance(entry, (int, float))
        and isinstance(tp, (int, float))
        and isinstance(sl, (int, float))
    ):
        entry_f = float(entry)
        tp_f = float(tp)
        sl_f = float(sl)
        # Barrier orientation check before running the replay.
        if side == "LONG" and _long_barriers_inverted(entry_f, tp_f, sl_f):
            return CanonicalRow(
                symbol=symbol, timestamp=timestamp, side=side,
                canonical_outcome_status=CANONICAL_STATUS_FIELD_MISMATCH,
                canonical_net_pnl_est=None,
                canonical_win=None,
                canonical_source=CANONICAL_SOURCE_OHLCV,
                reason=f"long_barriers_inverted_sl={sl_f}_entry={entry_f}_tp={tp_f}",
            )
        if side == "SHORT" and _short_barriers_inverted(entry_f, tp_f, sl_f):
            return CanonicalRow(
                symbol=symbol, timestamp=timestamp, side=side,
                canonical_outcome_status=CANONICAL_STATUS_FIELD_MISMATCH,
                canonical_net_pnl_est=None,
                canonical_win=None,
                canonical_source=CANONICAL_SOURCE_OHLCV,
                reason=f"short_barriers_inverted_tp={tp_f}_entry={entry_f}_sl={sl_f}",
            )
        if side == "LONG":
            replay = replay_long_baseline(entry_f, tp_f, sl_f, path)
        else:
            replay = replay_short_baseline(entry_f, tp_f, sl_f, path)
        net = replay.get("net_pct")
        if isinstance(net, (int, float)):
            net_f = float(net)
            win = net_f > 0
            status = CANONICAL_STATUS_OK
            reason = f"bar_by_bar_exit={replay.get('exit_reason')}"
            status, reason = _apply_sign_suspect_override(
                row, net_f, side, status, reason,
            )
            return CanonicalRow(
                symbol=symbol, timestamp=timestamp, side=side,
                canonical_outcome_status=status,
                canonical_net_pnl_est=net_f,
                canonical_win=win,
                canonical_source=CANONICAL_SOURCE_OHLCV,
                reason=reason,
            )

    # 2. Baseline fallback.
    if _has_baseline(row):
        baseline_net = float(row.get("baseline_net_pnl_est"))
        candidate_net = row.get("net_pnl_est")
        status = CANONICAL_STATUS_OK
        reason = "baseline_net_pnl_est_only"
        # Field mismatch detection.
        if (
            isinstance(candidate_net, (int, float))
            and abs(float(candidate_net) - baseline_net) > FIELD_MISMATCH_EPSILON
        ):
            status = CANONICAL_STATUS_FIELD_MISMATCH
            reason = (
                f"baseline={baseline_net:.4f}_vs_candidate={float(candidate_net):.4f}"
            )
        status, reason = _apply_sign_suspect_override(
            row, baseline_net, side, status, reason,
        )
        return CanonicalRow(
            symbol=symbol, timestamp=timestamp, side=side,
            canonical_outcome_status=status,
            canonical_net_pnl_est=baseline_net,
            canonical_win=baseline_net > 0,
            canonical_source=CANONICAL_SOURCE_BASELINE,
            reason=reason,
        )

    # 3. Forward returns are diagnostic only — never canonical when no
    # path / baseline is available.
    if _has_future_return(row):
        return CanonicalRow(
            symbol=symbol, timestamp=timestamp, side=side,
            canonical_outcome_status=CANONICAL_STATUS_NEED_OHLCV_PATH,
            canonical_net_pnl_est=None,
            canonical_win=None,
            canonical_source=CANONICAL_SOURCE_FUTURE_RETURN_DIAGNOSTIC,
            reason="future_returns_present_but_not_canonical",
        )

    # 4. Nothing usable.
    return CanonicalRow(
        symbol=symbol, timestamp=timestamp, side=side,
        canonical_outcome_status=CANONICAL_STATUS_NEED_DATA,
        canonical_net_pnl_est=None,
        canonical_win=None,
        canonical_source=CANONICAL_SOURCE_NEED_DATA,
        reason="no_path_no_baseline_no_future_return",
    )


def canonicalize_rows(
    rows: Iterable[dict[str, Any]] | None = None,
    *,
    hours: int = 168,
) -> CanonicalReport:
    report = CanonicalReport(
        hours=int(hours),
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
    rows_list = list(rows or [])
    report.rows_audited = len(rows_list)
    by_source: dict[str, int] = {}
    for r in rows_list:
        canonical = canonicalize_row(r)
        d = canonical.as_dict()
        report.rows.append(d)
        by_source[canonical.canonical_source] = (
            by_source.get(canonical.canonical_source, 0) + 1
        )
        if canonical.canonical_outcome_status == CANONICAL_STATUS_OK:
            report.ok_count += 1
        elif canonical.canonical_outcome_status == CANONICAL_STATUS_NEED_OHLCV_PATH:
            report.need_ohlcv_path_count += 1
        elif canonical.canonical_outcome_status == CANONICAL_STATUS_FIELD_MISMATCH:
            report.field_mismatch_count += 1
        elif canonical.canonical_outcome_status == CANONICAL_STATUS_SIGN_SUSPECT:
            report.sign_suspect_count += 1
        elif canonical.canonical_outcome_status == CANONICAL_STATUS_NEED_DATA:
            report.need_data_count += 1
    report.by_source = by_source
    if report.rows_audited > 0:
        report.canonical_outcome_ok_ratio = report.ok_count / report.rows_audited
    if by_source:
        report.canonical_outcome_source_top = max(
            by_source.items(), key=lambda kv: kv[1],
        )[0]
    report.status = STATUS_OK if rows_list else STATUS_NEED_DATA
    # Cap rows to keep CSV bounded.
    report.rows = report.rows[:5000]
    return report
