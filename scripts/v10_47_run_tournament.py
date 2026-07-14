"""V10.47 full gross-first tournament, fragmented per (symbol, timeframe),
with progress logging and INCREMENTAL per-combination save. Uses whichever
venue (bitget preferred, else bybit) is DATASET_VERIFIED for each symbol.
Research only, NO LIVE."""
import sys, os, json, time
sys.path.insert(0, r"C:\Users\Adrian\Documents\New project\bitget-ai-trading-bot")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from app.labs import public_data_backfill_v10_45_1 as BF
from app.labs import edge_discovery_engine_v10_45_1 as ENG
from app.labs.v10_46 import edge_search as ES

OUT = r"C:\Users\Adrian\Documents\New project\bitget-ai-trading-bot\reports\research\v10_47_final_edge_search"
os.makedirs(os.path.join(OUT, "tournament"), exist_ok=True)
FACT = {"1m": 1, "5m": 5, "15m": 15}
SYMBOLS = sys.argv[1].split(",") if len(sys.argv) > 1 else ["BTCUSDT","ETHUSDT","XRPUSDT","DOGEUSDT"]
TFS = sys.argv[2].split(",") if len(sys.argv) > 2 else ["1m","5m","15m"]

def pick_venue(sym):
    for v in ("bitget","bybit"):
        r = BF.verify_dataset(v, sym)
        if r.get("ok"):
            return v, r
    return None, None

def ref_for(sym, main_venue):
    other = "bybit" if main_venue == "bitget" else "bitget"
    r = BF.verify_dataset(other, sym)
    return (other, BF.load_klines(other, sym)) if r.get("ok") else (None, [])

summary = {}
for sym in SYMBOLS:
    venue, v = pick_venue(sym)
    if venue is None:
        print(f"{sym}: DATA_SOURCE_UNAVAILABLE (no verified venue)"); summary[sym]={"status":"DATA_SOURCE_UNAVAILABLE"}; continue
    gen = v["generation_id"]; as_of = v["as_of_ms"]
    bars1 = BF.load_klines(venue, sym)
    rvenue, ref1 = ref_for(sym, venue)
    print(f"\n### {sym} main={venue} gen={gen} ref={rvenue} bars1m={len(bars1)}", flush=True)
    for tf in TFS:
        f = FACT[tf]
        bars = ENG.resample_bars(bars1, f, as_of_ms=as_of) if f>1 else list(bars1)
        ref = ENG.resample_bars(ref1, f, as_of_ms=as_of) if (ref1 and f>1) else list(ref1)
        ref_by_ts = {int(b["ts"]):float(b["close"]) for b in ref} if ref else None
        t=time.time()
        out = ES.run_edge_search(bars, symbol=sym, venue=venue, timeframe=tf,
                                 data_generation_id=gen, ref_bars_by_ts=ref_by_ts,
                                 directions=(None,"LONG","SHORT"),
                                 log=lambda *a: print("   ", *a, flush=True))
        dur=round(time.time()-t)
        # incremental save per combination
        fn = os.path.join(OUT, "tournament", f"{sym}_{tf}.json")
        with open(fn,"w",encoding="utf-8") as fh: json.dump(out, fh, default=str)
        r = out["results"]
        best = sorted(r.items(), key=lambda kv: kv[1]["metrics"]["net_pnl_eur"], reverse=True)
        ge = [n for n,x in r.items() if x["metrics"]["classification"]!="NO_GROSS_EDGE"]
        ne = [n for n,x in r.items() if x["metrics"]["classification"]=="NET_EDGE_POSITIVE"]
        top = best[0]
        print(f"  {sym} {tf} DONE {dur}s | GROSS_EDGE={len(ge)} NET_POSITIVE={len(ne)} | top={top[0]} net={top[1]['metrics']['net_pnl_eur']}€ gross={top[1]['metrics']['gross_pnl_eur']}€ class={top[1]['metrics']['classification']}", flush=True)
        summary.setdefault(sym,{})[tf] = {"venue":venue,"n_bars":out["n_bars"],
            "gross_edge":ge,"net_positive":ne,
            "top":{"name":top[0],**{k:top[1]['metrics'][k] for k in ('classification','trades','gross_pnl_eur','net_pnl_eur','gross_ev_eur','net_ev_eur','n_eff')}}}
        with open(os.path.join(OUT,"tournament_summary.json"),"w",encoding="utf-8") as fh:
            json.dump(summary, fh, indent=2, default=str)
print("\n=== ALL DONE ===", flush=True)
print(json.dumps(summary, indent=2, default=str))
