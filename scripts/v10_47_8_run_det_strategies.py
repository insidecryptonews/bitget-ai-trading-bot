"""V10.47.8 deterministic 1h/4h strategy evaluation, DATA-GATED.

The mandate requires >= 2 years of verified 1h/4h OHLCV (12m train / 4m val /
4m walk-forward / 4m sealed holdout). The verified public datasets are ~90 days
of 1m, so the honest status is INSUFFICIENT_DATA and NO validated result is
produced. A clearly-labelled causal SMOKE is still run on the 90d-resampled bars
to prove the strategies execute through the canonical ledger. Research only."""
import sys, os, json, time
sys.path.insert(0, r"C:\Users\Adrian\Documents\New project\bitget-ai-trading-bot")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from app.labs import public_data_backfill_v10_45_1 as BF
from app.labs import edge_discovery_engine_v10_45_1 as ENG
from app.labs.v10_46 import det_strategies as DET
from app.labs.v10_46 import causal_ledger as CL
from app.labs.v10_46 import causal_stats as CS

ROOT = r"C:\Users\Adrian\Documents\New project\bitget-ai-trading-bot"
OUT = os.path.join(ROOT, "reports", "research", "v10_47_8_scientific_repair")
SYMBOLS = ["BTCUSDT", "ETHUSDT", "XRPUSDT", "DOGEUSDT"]
FACT = {"1h": 60, "4h": 240}
MIN_DAYS = 730          # 2 years


def venue_for(sym):
    for v in ("bitget", "bybit"):
        if BF.verify_dataset(v, sym).get("ok"):
            return v
    return None


result = {"data_requirement_days": MIN_DAYS, "strategies": list(DET.DET_STRATEGIES),
          "symbols": {}, "smoke": []}
for sym in SYMBOLS:
    venue = venue_for(sym)
    if not venue:
        result["symbols"][sym] = {"status": "DATA_SOURCE_UNAVAILABLE"}
        continue
    vr = BF.verify_dataset(venue, sym)
    bars1 = BF.load_klines(venue, sym)
    span_days = (bars1[-1]["ts"] - bars1[0]["ts"]) / 86_400_000.0
    status = "OK" if span_days >= MIN_DAYS else "INSUFFICIENT_DATA"
    result["symbols"][sym] = {"venue": venue, "span_days": round(span_days, 1),
                              "status": status}
    # each strategy runs on its own entry timeframe with a real 4h regime (MTF)
    resampled = {tf: ENG.resample_bars(bars1, f, as_of_ms=vr["as_of_ms"])
                 for tf, f in FACT.items()}
    for name, spec in DET.DET_STRATEGIES.items():
        tf = spec["entry_tf"]
        bars = resampled[tf]
        sig = DET.precompute_det_sig_mtf(bars, entry_tf=spec["entry_tf"],
                                         regime_tf=spec["regime_tf"])
        dec = spec["decider"](symbol=sym, venue=venue, timeframe=tf,
                              gen=vr["generation_id"])
        if True:
            t = time.time()
            out = CL.drive_causal(bars, sig, dec, spec["exit"], symbol=sym,
                                  timeframe=tf)
            net = float(sum(x["net_eur"] for x in out["trades"]))
            neff = CS.n_eff_estimate(out["trades"], timeframe=tf)
            row = {"symbol": sym, "timeframe": tf, "strategy": name,
                   "data_status": status, "smoke_only": True,
                   "bars": len(bars), "n_executed": out["counters"]["n_executed"],
                   "n_signals_raw": out["counters"]["n_signals_raw"],
                   "net_eur": round(net, 6), "n_eff_final": neff["n_eff_final"],
                   "secs": round(time.time() - t, 1)}
            result["smoke"].append(row)
            print(f"  SMOKE {sym} {tf} {name}: status={status} exec={row['n_executed']} "
                  f"net={row['net_eur']}€ (labelled smoke, NOT validated)", flush=True)

any_ok = any(v.get("status") == "OK" for v in result["symbols"].values()
             if isinstance(v, dict))
result["verdict"] = ("EVALUATED" if any_ok else
                     "INSUFFICIENT_DATA — deterministic 1h/4h strategies "
                     "implemented + causally smoke-tested, but < 2y verified "
                     "OHLCV so NO validated result is produced (not invented)")
os.makedirs(OUT, exist_ok=True)
json.dump(result, open(os.path.join(OUT, "det_strategies_result.json"), "w",
                       encoding="utf-8"), indent=2, default=str)
print("\nVERDICT:", result["verdict"], flush=True)
