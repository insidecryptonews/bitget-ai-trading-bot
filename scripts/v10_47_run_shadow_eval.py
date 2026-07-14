"""V10.47.6 — Walk-forward + Shadow Candidate evaluation over the completed
gross-first tournament. Reads all 12 per-combo JSONs, consolidates them, then
for every NET_EDGE_POSITIVE candidate rebuilds the exact decider, runs a 4-fold
walk-forward (observed cost), re-runs the full sample under CONSERVATIVE cost +
10eur/20eur money scenarios, and applies the deterministic SHADOW_CANDIDATE gate.
No candidate is fabricated. Research only, NO LIVE."""
import sys, os, json, time
sys.path.insert(0, r"C:\Users\Adrian\Documents\New project\bitget-ai-trading-bot")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from app.labs import public_data_backfill_v10_45_1 as BF
from app.labs import edge_discovery_engine_v10_45_1 as ENG
from app.labs.v10_46 import edge_search as ES
from app.labs.v10_46 import families as FAM

ROOT = r"C:\Users\Adrian\Documents\New project\bitget-ai-trading-bot"
OUT = os.path.join(ROOT, "reports", "research", "v10_47_final_edge_search")
TDIR = os.path.join(OUT, "tournament")
FACT = {"1m": 1, "5m": 5, "15m": 15}
SYMBOLS = ["BTCUSDT", "ETHUSDT", "XRPUSDT", "DOGEUSDT"]
TFS = ["1m", "5m", "15m"]


def log(*a):
    print(*a, flush=True)


# 1) consolidate all 12 combos
consolidated = {}
for sym in SYMBOLS:
    for tf in TFS:
        fn = os.path.join(TDIR, f"{sym}_{tf}.json")
        if not os.path.exists(fn):
            continue
        with open(fn, encoding="utf-8") as fh:
            consolidated[(sym, tf)] = json.load(fh)

with open(os.path.join(OUT, "tournament_full_consolidated.json"), "w",
          encoding="utf-8") as fh:
    json.dump({f"{s}|{t}": v for (s, t), v in consolidated.items()}, fh,
              default=str)

# 2) gather every classification, keyed for reports
rows = []       # (sym, tf, name, metrics, beats_no_trade, beats_random, paired)
for (sym, tf), out in consolidated.items():
    for name, r in out["results"].items():
        m = r["metrics"]
        rows.append({
            "symbol": sym, "timeframe": tf, "name": name,
            "class": m["classification"], "trades": m["trades"],
            "n_eff": m["n_eff"], "gross_pnl": m["gross_pnl_eur"],
            "net_pnl": m["net_pnl_eur"], "gross_ev": m["gross_ev_eur"],
            "net_ev": m["net_ev_eur"], "gross_pf": m["gross_pf"],
            "net_pf": m["net_pf"], "fee": m["fee_eur"], "spread": m["spread_eur"],
            "slippage": m["slippage_eur"], "funding": m["funding_eur"],
            "max_dd": m["max_drawdown_eur"],
            "net_wo_top3": m["net_without_top3_eur"],
            "beats_no_trade": r["beats_no_trade"],
            "beats_random": r["beats_random"],
            "paired_nt_lb": r["paired_vs_no_trade"].get("lower_bound_eur"),
            "paired_rnd_lb": r["paired_vs_random"].get("lower_bound_eur")})

net_pos = [r for r in rows if r["class"] == "NET_EDGE_POSITIVE"]
cost_killed = [r for r in rows if r["class"] == "GROSS_EDGE_COST_KILLED"]
log(f"consolidated {len(consolidated)} combos, {len(rows)} participant-runs")
log(f"NET_EDGE_POSITIVE={len(net_pos)} GROSS_EDGE_COST_KILLED={len(cost_killed)}")

# 3) build bar cache + decider builders per (sym, tf)
bar_cache, sig_cache, ref_cache = {}, {}, {}


def venue_for(sym):
    for v in ("bitget", "bybit"):
        if BF.verify_dataset(v, sym).get("ok"):
            return v
    return None


def load_combo(sym, tf):
    key = (sym, tf)
    if key in bar_cache:
        return
    venue = venue_for(sym)
    vr = BF.verify_dataset(venue, sym)
    as_of = vr["as_of_ms"]
    gen = vr["generation_id"]
    bars1 = BF.load_klines(venue, sym)
    f = FACT[tf]
    bars = ENG.resample_bars(bars1, f, as_of_ms=as_of) if f > 1 else list(bars1)
    other = "bybit" if venue == "bitget" else "bitget"
    ref = []
    if BF.verify_dataset(other, sym).get("ok"):
        ref1 = BF.load_klines(other, sym)
        ref = ENG.resample_bars(ref1, f, as_of_ms=as_of) if f > 1 else list(ref1)
    ref_by_ts = {int(b["ts"]): float(b["close"]) for b in ref} if ref else None
    t0 = time.time()
    sigs = ES.precompute_sigs(bars)
    bar_cache[key] = (bars, venue, gen)
    sig_cache[key] = sigs
    ref_cache[key] = ref_by_ts
    log(f"  loaded {sym} {tf}: {len(bars)} bars, sigs {round(time.time()-t0,1)}s")


def make_decider(sym, tf, name):
    """Rebuild the exact decider for a participant name (family[_DIR] or TRxx)."""
    bars, venue, gen = bar_cache[(sym, tf)]
    ref_by_ts = ref_cache[(sym, tf)]
    deciders = ES.build_deciders(sym, venue, tf, gen, ref_bars_by_ts=ref_by_ts,
                                 directions=(None, "LONG", "SHORT"))
    if name in deciders:
        return deciders[name]
    # strip direction suffix fallback
    return deciders.get(name)


# 4) evaluate each NET_EDGE_POSITIVE candidate: walk-forward + conservative
shadow_results = []
for r in sorted(net_pos, key=lambda x: x["net_pnl"], reverse=True):
    sym, tf, name = r["symbol"], r["timeframe"], r["name"]
    load_combo(sym, tf)
    bars, venue, gen = bar_cache[(sym, tf)]
    sigs = sig_cache[(sym, tf)]
    dec = make_decider(sym, tf, name)
    if dec is None:
        log(f"  !! no decider for {name}; skip")
        continue
    decide_fn, exit_params = dec
    t0 = time.time()
    wf = ES.walk_forward(bars, decide_fn, exit_params, sym, n_folds=4,
                         scenario_cost="observed", sigs=sigs)
    # conservative-cost full-sample re-run (survives conservative execution?)
    pc_cons = ES._drive_slice(bars, sigs, decide_fn, exit_params, sym,
                              scenario_cost="conservative")
    m_cons = ES._participant_metrics(pc_cons)
    # money scenarios 10/20 eur (5 already in the tournament observed run)
    pc10 = ES._drive_slice(bars, sigs, decide_fn, exit_params, sym,
                           scenario_money="10eur", scenario_cost="observed")
    pc20 = ES._drive_slice(bars, sigs, decide_fn, exit_params, sym,
                           scenario_money="20eur", scenario_cost="observed")
    m10, m20 = ES._participant_metrics(pc10), ES._participant_metrics(pc20)
    gate = ES.shadow_candidate_gate(
        {"gross_ev_eur": r["gross_ev"], "net_pnl_eur": r["net_pnl"],
         "n_eff": r["n_eff"], "net_without_top3_eur": r["net_wo_top3"]},
        wf, beats_no_trade=r["beats_no_trade"],
        beats_random=r["beats_random"],
        net_conservative_eur=m_cons["net_pnl_eur"])
    shadow_results.append({
        "symbol": sym, "timeframe": tf, "name": name, "venue": venue,
        "observed": {k: r[k] for k in ("trades", "n_eff", "gross_pnl",
                     "net_pnl", "gross_ev", "net_ev", "net_wo_top3",
                     "beats_no_trade", "beats_random", "paired_nt_lb",
                     "paired_rnd_lb")},
        "walk_forward": wf,
        "conservative_net_eur": m_cons["net_pnl_eur"],
        "money_10eur_net_eur": m10["net_pnl_eur"],
        "money_20eur_net_eur": m20["net_pnl_eur"],
        "gate": gate, "is_shadow_candidate": gate["all_pass"]})
    log(f"  {sym} {tf} {name}: WF folds+={wf['folds_net_positive']}/4 "
        f"oos={wf['oos_net_total_eur']}€ cons={m_cons['net_pnl_eur']}€ "
        f"SHADOW={gate['all_pass']} ({round(time.time()-t0,1)}s)")

with open(os.path.join(OUT, "shadow_candidate_eval.json"), "w",
          encoding="utf-8") as fh:
    json.dump({"net_positive_count": len(net_pos),
               "shadow_results": shadow_results,
               "any_shadow_candidate": any(s["is_shadow_candidate"]
                                           for s in shadow_results)},
              fh, indent=2, default=str)

# also dump the flat rows for the report generator
with open(os.path.join(OUT, "tournament_rows.json"), "w",
          encoding="utf-8") as fh:
    json.dump(rows, fh, default=str)

log("\n=== SHADOW EVAL DONE ===")
log(f"NET_EDGE_POSITIVE candidates evaluated: {len(shadow_results)}")
log(f"SHADOW_CANDIDATES found: "
    f"{sum(1 for s in shadow_results if s['is_shadow_candidate'])}")
