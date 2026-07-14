"""V10.47.8 — regenerate all 12 tournaments with the REPAIRED causal engine
(first causal signal, single position, append-only ledger, real n_eff,
exposure-matched random baseline, closed registry + multiple testing, sealed
holdout). Fragmented per (symbol, timeframe) with progress + incremental save.
Research only, NO LIVE, NO orders."""
import sys, os, json, time
sys.path.insert(0, r"C:\Users\Adrian\Documents\New project\bitget-ai-trading-bot")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from app.labs import public_data_backfill_v10_45_1 as BF
from app.labs import edge_discovery_engine_v10_45_1 as ENG
from app.labs.v10_46 import causal_tournament as CT

ROOT = r"C:\Users\Adrian\Documents\New project\bitget-ai-trading-bot"
OUT = os.path.join(ROOT, "reports", "research", "v10_47_8_scientific_repair")
os.makedirs(os.path.join(OUT, "tournament"), exist_ok=True)
FACT = {"1m": 1, "5m": 5, "15m": 15}
SYMBOLS = sys.argv[1].split(",") if len(sys.argv) > 1 else \
    ["BTCUSDT", "ETHUSDT", "XRPUSDT", "DOGEUSDT"]
TFS = sys.argv[2].split(",") if len(sys.argv) > 2 else ["1m", "5m", "15m"]


def pick_venue(sym):
    for v in ("bitget", "bybit"):
        if BF.verify_dataset(v, sym).get("ok"):
            return v, BF.verify_dataset(v, sym)
    return None, None


summary = {}
sfile = os.path.join(OUT, "causal_tournament_summary.json")
if os.path.exists(sfile):
    summary = json.load(open(sfile, encoding="utf-8"))

for sym in SYMBOLS:
    venue, v = pick_venue(sym)
    if venue is None:
        print(f"{sym}: DATA_SOURCE_UNAVAILABLE", flush=True)
        summary.setdefault(sym, {})["status"] = "DATA_SOURCE_UNAVAILABLE"
        continue
    gen, as_of = v["generation_id"], v["as_of_ms"]
    bars1 = BF.load_klines(venue, sym)
    other = "bybit" if venue == "bitget" else "bitget"
    ref1 = BF.load_klines(other, sym) if BF.verify_dataset(other, sym).get("ok") else []
    print(f"\n### {sym} main={venue} gen={gen} bars1m={len(bars1)}", flush=True)
    for tf in TFS:
        f = FACT[tf]
        bars = ENG.resample_bars(bars1, f, as_of_ms=as_of) if f > 1 else list(bars1)
        ref = ENG.resample_bars(ref1, f, as_of_ms=as_of) if (ref1 and f > 1) else list(ref1)
        ref_by_ts = {int(b["ts"]): float(b["close"]) for b in ref} if ref else None
        t = time.time()
        out = CT.run_causal_tournament(bars, symbol=sym, venue=venue, timeframe=tf,
                                       gen=gen, ref_bars_by_ts=ref_by_ts,
                                       log=lambda *a: print("   ", *a, flush=True))
        dur = round(time.time() - t)
        fn = os.path.join(OUT, "tournament", f"{sym}_{tf}.json")
        json.dump(out, open(fn, "w", encoding="utf-8"), default=str)
        r = out["results"]
        cls = {}
        for x in r.values():
            cls[x["metrics"]["classification"]] = cls.get(
                x["metrics"]["classification"], 0) + 1
        print(f"  {sym} {tf} DONE {dur}s | {cls} | net_pos={out['n_net_positive']} "
              f"| SHADOW={out['shadow_candidates']} | m_unique="
              f"{out['registry']['m_unique_hypotheses']} | holdout_sealed="
              f"{not out['holdout_touched']}", flush=True)
        summary.setdefault(sym, {})[tf] = {
            "venue": venue, "n_bars": out["n_bars"], "classes": cls,
            "n_net_positive": out["n_net_positive"],
            "shadow_candidates": out["shadow_candidates"],
            "m_nominal": out["registry"]["m_nominal"],
            "m_unique": out["registry"]["m_unique_hypotheses"],
            "registry_hash": out["registry"]["registry_hash"],
            "holdout_touched": out["holdout_touched"]}
        json.dump(summary, open(sfile, "w", encoding="utf-8"), indent=2, default=str)

total_shadow = sum(len(tfd.get("shadow_candidates", []))
                   for s in summary.values() if isinstance(s, dict)
                   for tfd in s.values() if isinstance(tfd, dict))
print(f"\n=== ALL DONE === total SHADOW_CANDIDATES={total_shadow}", flush=True)
print("VERDICT:", "NO_CONFIRMED_EDGE SHADOW_CANDIDATES=0 HOLD"
      if total_shadow == 0 else f"REVIEW: {total_shadow} shadow candidates")
