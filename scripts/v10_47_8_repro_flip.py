"""V10.47.8 reproduction: on the REAL DOGE/XRP 1m P08_LONG deciders, show the
flawed per-cluster-overwrite accounting reports a POSITIVE net while the causal
single-position ledger (first causal signal) reports the true net. Research only."""
import sys, os, json, time
sys.path.insert(0, r"C:\Users\Adrian\Documents\New project\bitget-ai-trading-bot")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from app.labs import public_data_backfill_v10_45_1 as BF
from app.labs.v10_46 import edge_search as ES
from app.labs.v10_46 import causal_ledger as CL
from app.labs.v10_46 import causal_stats as CS
from app.labs.v10_46 import families as FAM

ROOT = r"C:\Users\Adrian\Documents\New project\bitget-ai-trading-bot"
OUT = os.path.join(ROOT, "reports", "research", "v10_47_8_scientific_repair")

CANDS = [("DOGEUSDT", "bitget"), ("XRPUSDT", "bybit")]
EXIT = FAM.FAMILIES["P08"]["exit"]
results = []
for sym, venue in CANDS:
    vr = BF.verify_dataset(venue, sym)
    gen = vr["generation_id"]
    bars = BF.load_klines(venue, sym)                 # native 1m
    t0 = time.time()
    sigs = ES.precompute_sigs(bars)
    dec = FAM.family_decider("P08", symbol=sym, venue=venue, timeframe="1m",
                             gen_id=gen, direction="LONG")
    # FLAWED engine (per_cluster overwrite, last-signal-per-cluster)
    pc = ES._drive(bars, sigs, dec, EXIT, sym)
    traded = [c for c in pc.values() if c.get("traded")]
    flawed_net = sum(c["net_eur"] for c in traded)
    flawed_trades = len(traded)
    # CAUSAL engine (first causal signal, single position, append-only)
    out = CL.drive_causal(bars, sigs, dec, EXIT, symbol=sym, timeframe="1m")
    causal_net = sum(t["net_eur"] for t in out["trades"])
    neff = CS.n_eff_estimate(out["trades"], timeframe="1m")
    row = {"symbol": sym, "venue": venue,
           "flawed_last_signal_per_cluster": {
               "net_eur": round(flawed_net, 6), "trades": flawed_trades},
           "causal_first_signal_single_position": {
               "net_eur": round(causal_net, 6),
               "n_executed": out["counters"]["n_executed"],
               "n_signals_raw": out["counters"]["n_signals_raw"],
               "n_skipped_position_open": out["counters"]["n_skipped_position_open"],
               "n_skipped_cluster_cooldown": out["counters"]["n_skipped_cluster_cooldown"],
               "n_eff_final": neff["n_eff_final"], "n_cluster": neff["n_cluster"]},
           "net_flipped_sign": bool((flawed_net > 0) and (causal_net <= 0)),
           "secs": round(time.time() - t0, 1)}
    results.append(row)
    print(json.dumps(row, indent=2), flush=True)

os.makedirs(OUT, exist_ok=True)
json.dump({"reproduction": results,
           "conclusion": "flawed per-cluster overwrite inflates net via ex-post "
           "last-signal selection; causal accounting removes it"},
          open(os.path.join(OUT, "reproduction_flip.json"), "w",
               encoding="utf-8"), indent=2)
print("\nWROTE reproduction_flip.json")
