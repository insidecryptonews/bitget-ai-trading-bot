"""Independent deterministic 1h/4h technical smoke on discovery data only."""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.labs import edge_discovery_engine_v10_45_1 as ENG  # noqa: E402
from app.labs.v10_46 import causal_ledger as CL  # noqa: E402
from app.labs.v10_46 import causal_tournament as CT  # noqa: E402
from app.labs.v10_46 import det_strategies as DET  # noqa: E402
from app.labs.v10_46.discovery_dataset import DiscoveryDatasetLoader  # noqa: E402


DATA_ROOT = ROOT / "external_data" / "staging" / "v10_47_22_isolated"
REPORT_ROOT = ROOT / "reports" / "research" / "v10_47_22_real_state_certification"
SYMBOLS = ("BTCUSDT", "ETHUSDT", "XRPUSDT", "DOGEUSDT")


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + f".tmp.{uuid.uuid4().hex}")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8", newline="\n",
    )
    os.replace(temporary, path)


def run_policy(bars: list[dict], signals: list[dict], factory, *,
               symbol: str, timeframe: str, direction: str) -> dict:
    decider = factory(
        symbol=symbol, venue="discovery_only", timeframe=timeframe,
        gen="v10_47_22_mtf", direction=direction,
    )
    result = CL.drive_causal(
        bars, signals, decider, DET.DET_EXIT_ATR,
        symbol=symbol, timeframe=timeframe,
    )
    return {
        "direction": direction,
        "executed_trades": len(result["trades"]),
        "ledger_integrity": CT._ledger_integrity(result["ledger"], result["trades"]),
        "technical_smoke_only": True,
        "edge_classification": "NOT_PERMITTED_IN_INSUFFICIENT_DATA_SMOKE",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-label", required=True)
    parser.add_argument("--symbols", default=",".join(SYMBOLS))
    args = parser.parse_args(argv)
    if not args.run_label.replace("_", "").replace("-", "").isalnum():
        raise RuntimeError("unsafe run label")
    symbols = tuple(item.strip().upper() for item in args.symbols.split(",") if item.strip())
    if any(symbol not in SYMBOLS for symbol in symbols):
        raise RuntimeError("unsupported symbol")
    output_root = (REPORT_ROOT / "mtf" / args.run_label).resolve()
    output_root.relative_to(REPORT_ROOT.resolve())
    if output_root.exists():
        raise RuntimeError("MTF run label already exists")
    output_root.mkdir(parents=True, exist_ok=False)
    outputs = {}
    for symbol in symbols:
        discovery = DiscoveryDatasetLoader(DATA_ROOT / symbol / "1m" / "discovery").load()
        train, validation, walk_forward = discovery.as_mutable()
        bars_1m = train + validation + walk_forward
        as_of_ms = int(bars_1m[-1]["ts"]) + 60_000
        bars_1h = ENG.resample_bars(bars_1m, 60, as_of_ms=as_of_ms)
        bars_4h = ENG.resample_bars(bars_1m, 240, as_of_ms=as_of_ms)
        signals_1h = DET.precompute_det_sig_mtf(
            bars_1h, entry_tf="1h", regime_tf="4h"
        )
        signals_4h = DET.precompute_det_sig(bars_4h)
        duration_days = (
            (int(bars_1m[-1]["ts"]) - int(bars_1m[0]["ts"])) / 86_400_000
        )
        policies = {
            "DET_EMA_ADX_PULLBACK_1H_4H_LONG": run_policy(
                bars_1h, signals_1h, DET.ema_adx_pullback_decider,
                symbol=symbol, timeframe="1h", direction="LONG",
            ),
            "DET_EMA_ADX_PULLBACK_1H_4H_SHORT": run_policy(
                bars_1h, signals_1h, DET.ema_adx_pullback_decider,
                symbol=symbol, timeframe="1h", direction="SHORT",
            ),
            "DET_DONCHIAN_BREAKOUT_4H_LONG": run_policy(
                bars_4h, signals_4h, DET.donchian_breakout_decider,
                symbol=symbol, timeframe="4h", direction="LONG",
            ),
            "DET_DONCHIAN_BREAKOUT_4H_SHORT": run_policy(
                bars_4h, signals_4h, DET.donchian_breakout_decider,
                symbol=symbol, timeframe="4h", direction="SHORT",
            ),
            "NO_TRADE": {"executed_trades": 0, "technical_smoke_only": True},
            "EXACT_MATCH_BASELINE": {
                "status": "NOT_EVALUATED_IN_INSUFFICIENT_DATA_SMOKE"
            },
            "TREND_RIDER_1H_4H": {
                "status": "NOT_EVALUATED_NO_COMPARABLE_PREREGISTERED_ADAPTER"
            },
        }
        value = {
            "schema": "v10_47_22_deterministic_mtf_smoke",
            "symbol": symbol,
            "experiment": DET.deterministic_mtf_experiment_registry(),
            "discovery_days": round(duration_days, 6),
            "bars_1h": len(bars_1h),
            "bars_4h": len(bars_4h),
            "scientific_evaluation": "INSUFFICIENT_DATA",
            "needs_2y_data": duration_days < 730,
            "policies": policies,
            "holdout_loaded": False,
            "edge_classification": "NOT_PERMITTED",
            "shadow_candidate": False,
            "research_only": True,
            "can_send_real_orders": False,
            "final_recommendation": "NO LIVE",
        }
        atomic_json(output_root / f"{symbol}_MTF_1H_4H.json", value)
        outputs[symbol] = value
        print(
            f"{symbol}: MTF IMPLEMENTED, SCIENTIFIC_EVALUATION=INSUFFICIENT_DATA",
            flush=True,
        )
    atomic_json(output_root / "mtf_summary.json", {
        "schema": "v10_47_22_mtf_summary",
        "symbols": list(symbols),
        "all_insufficient_data": all(
            value["scientific_evaluation"] == "INSUFFICIENT_DATA"
            for value in outputs.values()
        ),
        "shadow_candidates": 0,
        "research_only": True,
        "final_recommendation": "NO LIVE",
    })
    print("SHADOW_CANDIDATES=0")
    print("FINAL_RECOMMENDATION=NO LIVE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
