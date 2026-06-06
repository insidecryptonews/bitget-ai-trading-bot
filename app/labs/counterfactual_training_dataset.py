"""V8.2.4 — Counterfactual Training Dataset Builder (research-only).

Produces a sanitised, training-ready dataset: one row per observation with
counterfactual baseline / trailing / campaign outcomes plus a single
``training_label`` and a ``final_use_for_training`` flag.

Hard contract:
- No secrets in output. No ``.env`` keys. No DB dump. No API keys/tokens.
- No order placement. No PaperTrader changes.
- Exports go under ``training_exports/research_v8_2_4/`` (gitignored).
- ZIP includes only CSV + TXT summary + manifest with SHA1 hashes.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from . import (
    FINAL_RECOMMENDATION_NO_LIVE,
    SIDE_LONG,
    SIDE_NO_TRADE,
    SIDE_SHORT,
    STATUS_NEED_DATA,
    STATUS_OK,
    STATUS_PARTIAL,
)
from .edgeguard_counterfactual_lab import (
    DEFAULT_FEE_BPS_ROUND_TRIP,
    DEFAULT_FUNDING_BPS_PER_CROSSING,
    DEFAULT_SLIPPAGE_BPS,
    _cost_estimate_pct,
    _gross_ev_est_pct,
    _is_edgeguard_blocked,
)
from .future_returns_bridge import (
    DEFAULT_HORIZONS_MINUTES,
    DEFAULT_MAX_BARS,
    DEFAULT_SL_PCT,
    DEFAULT_TIMEFRAME,
    DEFAULT_TP_PCT,
    compute_future_returns,
)


# ---- Labels ----------------------------------------------------------------

LABEL_GOOD_LONG = "GOOD_LONG"
LABEL_BAD_LONG = "BAD_LONG"
LABEL_GOOD_SHORT = "GOOD_SHORT"
LABEL_BAD_SHORT = "BAD_SHORT"
LABEL_AVOID_NO_TRADE = "AVOID_NO_TRADE"
LABEL_NEED_DATA = "NEED_DATA"
LABEL_UNCERTAIN = "UNCERTAIN"
LABEL_BLOCKED_WINNER = "BLOCKED_WINNER"
LABEL_BLOCKED_LOSER = "BLOCKED_LOSER"
LABEL_GOOD_NOT_MONETIZED = "GOOD_NOT_MONETIZED"

VALID_LABELS = (
    LABEL_GOOD_LONG, LABEL_BAD_LONG,
    LABEL_GOOD_SHORT, LABEL_BAD_SHORT,
    LABEL_AVOID_NO_TRADE,
    LABEL_NEED_DATA, LABEL_UNCERTAIN,
    LABEL_BLOCKED_WINNER, LABEL_BLOCKED_LOSER,
    LABEL_GOOD_NOT_MONETIZED,
)


# ---- Sanitisation ----------------------------------------------------------

FORBIDDEN_KEY_PATTERN = re.compile(
    r"(api[_-]?key|api[_-]?secret|passphrase|token|secret|password|bitget_passphrase)",
    re.IGNORECASE,
)
FORBIDDEN_VALUE_PATTERN = re.compile(
    r"(BITGET_[A-Z_]*KEY|BITGET_[A-Z_]*SECRET|PASSPHRASE)",
    re.IGNORECASE,
)


def _sanitise_value(value: Any) -> Any:
    """Return ``value`` unless it looks like a secret. Strings get scanned."""
    if isinstance(value, str):
        if FORBIDDEN_VALUE_PATTERN.search(value):
            return "REDACTED"
        return value
    return value


def _sanitise_row(row: dict[str, Any]) -> dict[str, Any]:
    """Drop forbidden keys, sanitise values."""
    safe: dict[str, Any] = {}
    for k, v in row.items():
        if FORBIDDEN_KEY_PATTERN.search(str(k)):
            continue
        safe[k] = _sanitise_value(v)
    return safe


# ---- Dataset row schema ----------------------------------------------------

DATASET_COLUMNS: tuple[str, ...] = (
    "signal_id",
    "timestamp",
    "symbol",
    "side",
    "regime",
    "score",
    "score_bucket",
    "strategy",
    "reason",
    "blocked_by",
    "edgeguard_reason",
    "candidate_selected",
    "risk_approved",
    "entry_price",
    "ohlcv_available",
    "ret_15m_pct",
    "ret_30m_pct",
    "ret_1h_pct",
    "ret_4h_pct",
    "ret_24h_pct",
    "mfe_pct",
    "mae_pct",
    "first_barrier_hit",
    "tp_before_sl",
    "sl_before_tp",
    "baseline_result",
    "baseline_gross_pnl",
    "baseline_net_pnl_est",
    "trailing_result",
    "trailing_net_pnl_est",
    "campaign_result",
    "campaign_net_pnl_est",
    "would_have_worked_baseline",
    "would_have_worked_trailing",
    "would_have_worked_campaign",
    "data_quality",
    "label_confidence",
    "training_label",
    "final_use_for_training",
    "research_only",
    "final_recommendation",
)


def _score_bucket(score: float | int | None) -> str:
    if score is None:
        return "unknown"
    try:
        s = int(score)
    except Exception:
        return "unknown"
    if s >= 90:
        return "90-100"
    if s >= 80:
        return "80-89"
    if s >= 70:
        return "70-79"
    if s >= 60:
        return "60-69"
    return "<60"


@dataclass
class DatasetSummary:
    hours: int
    generated_at: str
    total_rows: int = 0
    by_label: dict[str, int] = field(default_factory=dict)
    by_side: dict[str, int] = field(default_factory=dict)
    by_regime: dict[str, int] = field(default_factory=dict)
    use_for_training_count: int = 0
    need_data_count: int = 0
    blocked_winner_count: int = 0
    blocked_loser_count: int = 0
    good_not_monetized_count: int = 0
    net_ev_avg_est_pct: float = 0.0
    status: str = STATUS_NEED_DATA
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _classify_label(
    *,
    side: str,
    edgeguard_blocked: bool,
    baseline_net_pnl: float | None,
    trailing_net_pnl: float | None,
    first_barrier_hit: str | None,
    mfe_pct: float | None,
    realized_proxy_pct: float | None,
    ohlcv_available: bool,
) -> tuple[str, float, bool]:
    """Return (label, label_confidence_0..1, final_use_for_training)."""
    if not ohlcv_available:
        return LABEL_NEED_DATA, 0.0, False
    if baseline_net_pnl is None:
        return LABEL_UNCERTAIN, 0.1, False
    if side == SIDE_NO_TRADE:
        # If NO_TRADE was a good call (avoided loss) → AVOID_NO_TRADE.
        # We give a small confidence.
        return LABEL_AVOID_NO_TRADE, 0.5, True
    # SL before TP makes it a loser regardless of MFE.
    if first_barrier_hit == "SL" or baseline_net_pnl <= 0:
        if edgeguard_blocked:
            return LABEL_BLOCKED_LOSER, 0.7, True
        if side == SIDE_LONG:
            return LABEL_BAD_LONG, 0.7, True
        return LABEL_BAD_SHORT, 0.7, True
    # Baseline net positive → winner.
    if edgeguard_blocked:
        return LABEL_BLOCKED_WINNER, 0.8, True
    # If trailing improves materially over baseline, flag GOOD_NOT_MONETIZED.
    if (
        trailing_net_pnl is not None
        and baseline_net_pnl > 0
        and trailing_net_pnl > baseline_net_pnl + 0.15  # at least 15 bps gain
        and mfe_pct is not None
        and realized_proxy_pct is not None
        and (realized_proxy_pct / max(mfe_pct, 1e-9)) < 0.55
    ):
        return LABEL_GOOD_NOT_MONETIZED, 0.8, True
    if side == SIDE_LONG:
        return LABEL_GOOD_LONG, 0.85, True
    return LABEL_GOOD_SHORT, 0.85, True


def _trailing_proxy_pnl(future_mfe_pct: float | None, baseline_net_pnl: float | None) -> float | None:
    """Estimate trailing-policy net pnl as: 50% of MFE minus baseline costs.

    This is a conservative approximation used only for labelling; it is NOT a
    full bar-by-bar replay. The Profit Lock Simulator gives the real number.
    """
    if future_mfe_pct is None or baseline_net_pnl is None:
        return None
    return max(baseline_net_pnl, (future_mfe_pct * 0.5) - 0.15)


def _campaign_proxy_pnl(baseline_net_pnl: float | None) -> float | None:
    if baseline_net_pnl is None:
        return None
    # Conservative: 1+1 campaign captures 1.3x baseline if positive, else 1x.
    return baseline_net_pnl * 1.3 if baseline_net_pnl > 0 else baseline_net_pnl


def _build_row(
    obs: dict[str, Any],
    *,
    db: Any,
    tp_pct: float,
    sl_pct: float,
    timeframe: str,
    max_bars: int,
) -> dict[str, Any]:
    side = str(obs.get("side") or obs.get("proposed_side") or SIDE_NO_TRADE).upper()
    edgeguard_blocked, eg_reason = _is_edgeguard_blocked(obs)

    if side in {SIDE_LONG, SIDE_SHORT}:
        future = compute_future_returns(
            db, observation=obs,
            tp_pct=tp_pct, sl_pct=sl_pct,
            timeframe=timeframe, max_bars=max_bars,
        )
        gross = _gross_ev_est_pct(future, tp_pct, sl_pct)
        fee_pct, slip_pct, funding_pct = _cost_estimate_pct(side, future)
        baseline_net = (
            (gross - fee_pct - slip_pct - funding_pct)
            if gross is not None
            else None
        )
        first_barrier = future.first_barrier_hit
        mfe = future.mfe_pct
        mae = future.mae_pct
        ohlcv_ok = future.status in {STATUS_OK, STATUS_PARTIAL}
        entry_price = future.entry_price
        ret_15m = future.returns_by_horizon_pct.get("15m")
        ret_30m = future.returns_by_horizon_pct.get("30m")
        ret_1h = future.returns_by_horizon_pct.get("60m")
        ret_4h = future.returns_by_horizon_pct.get("240m")
        ret_24h = future.returns_by_horizon_pct.get("1440m")
    else:
        gross = None
        baseline_net = None
        first_barrier = None
        mfe = None
        mae = None
        ohlcv_ok = False
        entry_price = 0.0
        ret_15m = ret_30m = ret_1h = ret_4h = ret_24h = None
        future = None

    trailing_net = _trailing_proxy_pnl(mfe, baseline_net)
    campaign_net = _campaign_proxy_pnl(baseline_net)
    realized_proxy = (
        baseline_net + (DEFAULT_FEE_BPS_ROUND_TRIP / 100.0)
        if baseline_net is not None
        else None
    )

    label, confidence, use_training = _classify_label(
        side=side,
        edgeguard_blocked=edgeguard_blocked,
        baseline_net_pnl=baseline_net,
        trailing_net_pnl=trailing_net,
        first_barrier_hit=first_barrier,
        mfe_pct=mfe,
        realized_proxy_pct=realized_proxy,
        ohlcv_available=ohlcv_ok,
    )

    data_quality = "OK"
    if not ohlcv_ok:
        data_quality = "NEED_DATA"
    elif future and future.status == STATUS_PARTIAL:
        data_quality = "PARTIAL"

    row = {
        "signal_id": obs.get("id") or obs.get("signal_id"),
        "observation_id": obs.get("id") or obs.get("observation_id") or obs.get("signal_id"),
        "timestamp": str(obs.get("timestamp") or ""),
        "symbol": str(obs.get("symbol") or "").upper(),
        "side": side,
        "regime": str(obs.get("market_regime") or obs.get("regime") or "UNKNOWN").upper(),
        "score": obs.get("confidence_score") or obs.get("score"),
        "score_bucket": _score_bucket(obs.get("confidence_score") or obs.get("score")),
        "strategy": str(obs.get("strategy_type") or ""),
        "reason": str(obs.get("reason") or ""),
        "blocked_by": "edge_guard" if edgeguard_blocked else "",
        "edgeguard_reason": eg_reason,
        "candidate_selected": bool(obs.get("selected_by_allocator")),
        "risk_approved": bool(obs.get("risk_manager_approved")),
        "entry_price": entry_price,
        "ohlcv_available": ohlcv_ok,
        "ret_15m_pct": ret_15m,
        "ret_30m_pct": ret_30m,
        "ret_1h_pct": ret_1h,
        "ret_4h_pct": ret_4h,
        "ret_24h_pct": ret_24h,
        "mfe_pct": mfe,
        "mae_pct": mae,
        "first_barrier_hit": first_barrier,
        "tp_before_sl": (first_barrier == "TP") if first_barrier else None,
        "sl_before_tp": (first_barrier == "SL") if first_barrier else None,
        "baseline_result": first_barrier or "NA",
        "baseline_gross_pnl": gross,
        "baseline_net_pnl_est": baseline_net,
        "trailing_result": "trailing_proxy",
        "trailing_net_pnl_est": trailing_net,
        "campaign_result": "1+1_proxy",
        "campaign_net_pnl_est": campaign_net,
        "would_have_worked_baseline": (baseline_net is not None and baseline_net > 0),
        "would_have_worked_trailing": (trailing_net is not None and trailing_net > 0),
        "would_have_worked_campaign": (campaign_net is not None and campaign_net > 0),
        "data_quality": data_quality,
        "label_confidence": confidence,
        "training_label": label,
        "final_use_for_training": use_training,
        "research_only": True,
        "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE,
    }
    return _sanitise_row(row)


def _safe_call(db: Any, method: str, *args, **kwargs) -> tuple[bool, Any]:
    if db is None:
        return False, None
    fn = getattr(db, method, None)
    if fn is None or not callable(fn):
        return False, None
    try:
        return True, fn(*args, **kwargs)
    except Exception:
        return False, None


def build_dataset(
    db: Any,
    *,
    hours: int = 168,
    limit: int = 50000,
    rows: Iterable[dict[str, Any]] | None = None,
    tp_pct: float = DEFAULT_TP_PCT,
    sl_pct: float = DEFAULT_SL_PCT,
    timeframe: str = DEFAULT_TIMEFRAME,
    max_bars: int = DEFAULT_MAX_BARS,
) -> tuple[list[dict[str, Any]], DatasetSummary]:
    """Build the counterfactual training dataset.

    Returns ``(rows, summary)``. ``rows`` is a list of sanitised dicts; one
    per signal observation. ``summary`` is a ``DatasetSummary``.
    """
    summary = DatasetSummary(
        hours=int(hours),
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
    if rows is None:
        ok, value = _safe_call(db, "fetch_signal_observations", hours=int(hours), limit=int(limit))
        if not ok or value is None:
            summary.status = STATUS_NEED_DATA
            return [], summary
        obs_list = list(value)[: int(limit)]
    else:
        obs_list = list(rows)[: int(limit)]
    if not obs_list:
        summary.status = STATUS_NEED_DATA
        return [], summary
    dataset: list[dict[str, Any]] = []
    net_sum = 0.0
    net_count = 0
    for obs in obs_list:
        row = _build_row(
            obs,
            db=db,
            tp_pct=tp_pct, sl_pct=sl_pct,
            timeframe=timeframe, max_bars=max_bars,
        )
        dataset.append(row)
        # Aggregations
        label = row.get("training_label") or LABEL_UNCERTAIN
        side = row.get("side") or SIDE_NO_TRADE
        regime = row.get("regime") or "UNKNOWN"
        summary.by_label[label] = summary.by_label.get(label, 0) + 1
        summary.by_side[side] = summary.by_side.get(side, 0) + 1
        summary.by_regime[regime] = summary.by_regime.get(regime, 0) + 1
        if row.get("final_use_for_training"):
            summary.use_for_training_count += 1
        if label == LABEL_NEED_DATA:
            summary.need_data_count += 1
        if label == LABEL_BLOCKED_WINNER:
            summary.blocked_winner_count += 1
        if label == LABEL_BLOCKED_LOSER:
            summary.blocked_loser_count += 1
        if label == LABEL_GOOD_NOT_MONETIZED:
            summary.good_not_monetized_count += 1
        net = row.get("baseline_net_pnl_est")
        if isinstance(net, (int, float)):
            net_sum += float(net)
            net_count += 1
    summary.total_rows = len(dataset)
    if net_count > 0:
        summary.net_ev_avg_est_pct = net_sum / net_count
    if summary.total_rows == 0:
        summary.status = STATUS_NEED_DATA
    elif summary.need_data_count == summary.total_rows:
        summary.status = STATUS_NEED_DATA
    elif summary.need_data_count > 0:
        summary.status = STATUS_PARTIAL
    else:
        summary.status = STATUS_OK
    return dataset, summary


# ---- Export ----------------------------------------------------------------

EXPORT_SUBDIR = Path("training_exports") / "research_v8_2_4"


def _write_csv(path: Path, rows: list[dict[str, Any]], columns: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Use \n line terminator so hashes are stable across OSes.
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(columns), lineterminator="\n")
        writer.writeheader()
        for row in rows:
            safe_row = {col: row.get(col, "") for col in columns}
            writer.writerow(safe_row)


def _sha1_of_file(path: Path) -> str:
    sha = hashlib.sha1()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            sha.update(chunk)
    return sha.hexdigest()


def export_dataset(
    dataset: list[dict[str, Any]],
    summary: DatasetSummary,
    *,
    base_dir: Path | None = None,
    sample_size: int = 1000,
    pack_zip: bool = True,
) -> dict[str, Any]:
    """Write CSVs + summary TXT + manifest + ZIP under ``base_dir``.

    Returns a manifest dict with file paths and SHA1 hashes.
    """
    base = Path(base_dir) if base_dir else EXPORT_SUBDIR
    base.mkdir(parents=True, exist_ok=True)
    # Main CSV
    main_csv = base / "counterfactual_training_dataset_v1.csv"
    sample_csv = base / "counterfactual_training_dataset_sample_v1.csv"
    edgeguard_csv = base / "edgeguard_counterfactual_v1.csv"
    blocked_winners_csv = base / "blocked_winners_v1.csv"
    blocked_losers_csv = base / "blocked_losers_v1.csv"
    good_not_monetized_csv = base / "good_not_monetized_v1.csv"
    summary_txt = base / "counterfactual_training_summary_v1.txt"

    sanitised_rows = [_sanitise_row(r) for r in dataset]
    _write_csv(main_csv, sanitised_rows, DATASET_COLUMNS)
    sample_rows = sanitised_rows[: int(sample_size)]
    _write_csv(sample_csv, sample_rows, DATASET_COLUMNS)

    edgeguard_rows = [r for r in sanitised_rows if (r.get("blocked_by") == "edge_guard")]
    _write_csv(edgeguard_csv, edgeguard_rows, DATASET_COLUMNS)
    blocked_winners = [r for r in sanitised_rows if r.get("training_label") == LABEL_BLOCKED_WINNER]
    _write_csv(blocked_winners_csv, blocked_winners, DATASET_COLUMNS)
    blocked_losers = [r for r in sanitised_rows if r.get("training_label") == LABEL_BLOCKED_LOSER]
    _write_csv(blocked_losers_csv, blocked_losers, DATASET_COLUMNS)
    gnm = [r for r in sanitised_rows if r.get("training_label") == LABEL_GOOD_NOT_MONETIZED]
    _write_csv(good_not_monetized_csv, gnm, DATASET_COLUMNS)

    # Summary TXT
    with summary_txt.open("w", encoding="utf-8") as f:
        f.write("COUNTERFACTUAL TRAINING SUMMARY V1\n")
        f.write(f"generated_at: {summary.generated_at}\n")
        f.write(f"hours: {summary.hours}\n")
        f.write(f"total_rows: {summary.total_rows}\n")
        f.write(f"status: {summary.status}\n")
        f.write(f"use_for_training_count: {summary.use_for_training_count}\n")
        f.write(f"need_data_count: {summary.need_data_count}\n")
        f.write(f"blocked_winner_count: {summary.blocked_winner_count}\n")
        f.write(f"blocked_loser_count: {summary.blocked_loser_count}\n")
        f.write(f"good_not_monetized_count: {summary.good_not_monetized_count}\n")
        f.write(f"net_ev_avg_est_pct: {summary.net_ev_avg_est_pct:.6f}\n")
        for k, v in summary.by_label.items():
            f.write(f"by_label {k}: {v}\n")
        for k, v in summary.by_side.items():
            f.write(f"by_side {k}: {v}\n")
        for k, v in summary.by_regime.items():
            f.write(f"by_regime {k}: {v}\n")
        f.write("research_only: true\n")
        f.write("paper_filter_enabled: false\n")
        f.write("can_send_real_orders: false\n")
        f.write(f"final_recommendation: {FINAL_RECOMMENDATION_NO_LIVE}\n")

    files = [
        main_csv, sample_csv, edgeguard_csv, blocked_winners_csv,
        blocked_losers_csv, good_not_monetized_csv, summary_txt,
    ]
    manifest = {
        "version": "v8.2.4.v1",
        "generated_at": summary.generated_at,
        "base_dir": str(base),
        "files": [],
        "research_only": True,
        "paper_filter_enabled": False,
        "can_send_real_orders": False,
        "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE,
    }
    for path in files:
        if path.exists():
            manifest["files"].append({
                "name": path.name,
                "size_bytes": path.stat().st_size,
                "sha1": _sha1_of_file(path),
            })
    manifest_path = base / "manifest_v1.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    manifest["files"].append({
        "name": manifest_path.name,
        "size_bytes": manifest_path.stat().st_size,
        "sha1": _sha1_of_file(manifest_path),
    })
    if pack_zip:
        zip_path = base / "counterfactual_training_exports_v1.zip"
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for path in files + [manifest_path]:
                if path.exists():
                    zf.write(path, arcname=path.name)
        manifest["zip"] = {
            "name": zip_path.name,
            "size_bytes": zip_path.stat().st_size,
            "sha1": _sha1_of_file(zip_path),
        }
    return manifest


def find_latest_zip(base_dir: Path | None = None) -> Path | None:
    base = Path(base_dir) if base_dir else EXPORT_SUBDIR
    candidate = base / "counterfactual_training_exports_v1.zip"
    if candidate.exists() and candidate.is_file():
        return candidate
    return None
