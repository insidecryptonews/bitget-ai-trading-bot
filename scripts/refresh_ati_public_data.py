"""Incrementally refresh validated Bitget OHLCV generations for ATI research.

Public GET market data only. The script verifies the current generation before
merging, rejects conflicting overlap, and delegates atomic publication to the
V10.45.5 content-addressed dataset writer.
"""

from __future__ import annotations

import argparse
import json
import math
from typing import Any

from app.labs import public_data_backfill_v10_45_1 as backfill


def _as_row(bar: dict[str, Any]) -> list[float | int]:
    return [
        int(bar["ts"]), float(bar["open"]), float(bar["high"]),
        float(bar["low"]), float(bar["close"]), float(bar["volume"]),
        float(bar["turnover"]),
    ]


def _same_payload(left: list[Any], right: list[Any]) -> bool:
    if int(left[0]) != int(right[0]) or len(left) != 7 or len(right) != 7:
        return False
    return all(
        math.isfinite(float(a)) and math.isfinite(float(b))
        and math.isclose(float(a), float(b), rel_tol=1e-12, abs_tol=1e-12)
        for a, b in zip(left[1:], right[1:])
    )


def merge_verified_rows(existing: list[list[Any]], incoming: list[list[Any]],
                        *, start_ms: int, end_ms: int,
                        replace_conflicts_after_ms: int | None = None,
                        revised_tail_timestamps: list[int] | None = None) -> list[list[Any]]:
    merged: dict[int, list[Any]] = {}
    for row in existing:
        ts = int(row[0])
        if start_ms <= ts < end_ms:
            merged[ts] = list(row)
    for row in incoming:
        ts = int(row[0])
        if not start_ms <= ts < end_ms:
            continue
        current = merged.get(ts)
        if current is not None and not _same_payload(current, row):
            if replace_conflicts_after_ms is None or ts < replace_conflicts_after_ms:
                raise ValueError(f"ATI_PUBLIC_REFRESH_BAR_PAYLOAD_CONFLICT:{ts}")
            merged[ts] = list(row)
            if revised_tail_timestamps is not None:
                revised_tail_timestamps.append(ts)
            continue
        merged[ts] = list(current if current is not None else row)
    rows = [merged[key] for key in sorted(merged)]
    quality = backfill.raw_quality_report(rows)
    expected = (end_ms - start_ms) // backfill.BAR_MS
    if not quality.get("raw_quality_pass"):
        raise ValueError("ATI_PUBLIC_REFRESH_RAW_QUALITY_FAIL")
    if expected - len(rows) > backfill.COMPLETENESS_TOLERANCE_BARS:
        raise ValueError(f"ATI_PUBLIC_REFRESH_INCOMPLETE:{len(rows)}/{expected}")
    return rows


def refresh_symbols(symbols: list[str], *, days: int = 90, log=print) -> dict[str, Any]:
    safety_lag_bars = 2
    requested_end_ms = (
        (backfill._now_ms() // backfill.BAR_MS) * backfill.BAR_MS
        - safety_lag_bars * backfill.BAR_MS
    )
    requested_start_ms = requested_end_ms - int(days) * 86_400_000
    published: list[dict[str, Any]] = []
    for raw_symbol in symbols:
        symbol = backfill.validate_symbol(raw_symbol)
        verification = backfill.verify_dataset("bitget", symbol)
        if verification.get("status") != "DATASET_VERIFIED":
            raise ValueError(
                f"ATI_PUBLIC_REFRESH_CURRENT_NOT_VERIFIED:{symbol}:"
                f"{verification.get('status')}"
            )
        existing = [_as_row(row) for row in backfill.load_klines("bitget", symbol)]
        previous_end_ms = int((verification.get("manifest") or {}).get("requested_end_ms") or 0)
        replace_conflicts_after_ms = (
            previous_end_ms - backfill.COMPLETENESS_TOLERANCE_BARS * backfill.BAR_MS
        )
        last_ts = int(existing[-1][0]) if existing else requested_start_ms
        lag_days = max(0.0, (requested_end_ms - last_ts) / 86_400_000)
        fetch_days = min(int(days), max(2, int(math.ceil(lag_days)) + 1))
        log(f"refresh bitget {symbol}: public tail {fetch_days}d")
        incoming = backfill.fetch_bitget_1m(
            symbol, fetch_days, log=log, end_ms=requested_end_ms,
        )
        revised_tail_timestamps: list[int] = []
        rows = merge_verified_rows(
            existing, incoming,
            start_ms=requested_start_ms, end_ms=requested_end_ms,
            replace_conflicts_after_ms=replace_conflicts_after_ms,
            revised_tail_timestamps=revised_tail_timestamps,
        )
        if revised_tail_timestamps:
            log(
                f"  {symbol}: replaced {len(revised_tail_timestamps)} provisional "
                "tail bar(s) inside the strict 3-bar boundary window"
            )
        manifest = backfill.save_dataset(
            "bitget", symbol, rows, requested_days=int(days),
            requested_start_ms=requested_start_ms,
            requested_end_ms=requested_end_ms,
        )
        post = backfill.verify_dataset("bitget", symbol)
        if post.get("status") != "DATASET_VERIFIED":
            raise ValueError(f"ATI_PUBLIC_REFRESH_POST_VERIFY_FAIL:{symbol}:{post.get('status')}")
        published.append({
            "symbol": symbol,
            "generation_id": manifest["generation_id"],
            "sha256": manifest["sha256"],
            "n_bars": manifest["n_bars"],
            "last_bar_ts": manifest["actual_end"],
            "fetch_days": fetch_days,
            "safety_lag_bars": safety_lag_bars,
            "revised_provisional_tail_bars": len(revised_tail_timestamps),
        })
    return {
        "status": "REFRESHED_VERIFIED",
        "datasets": published,
        "research_only": True,
        "public_endpoints_only": True,
        "uses_api_keys": False,
        "paper_filter_enabled": False,
        "can_send_real_orders": False,
        "activation": "disabled",
        "final_recommendation": "NO LIVE",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh ATI public OHLCV snapshots")
    parser.add_argument("--symbols", default="BTCUSDT,ETHUSDT")
    parser.add_argument("--days", type=int, default=90)
    args = parser.parse_args()
    symbols = [item.strip().upper() for item in args.symbols.split(",") if item.strip()]
    result = refresh_symbols(symbols, days=max(1, int(args.days)))
    print(json.dumps(result, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
