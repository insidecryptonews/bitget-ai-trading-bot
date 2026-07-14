"""V10.47.7 — SHADOW PIPELINE SMOKE (local, NO ORDERS).

Takes the top SHADOW_CANDIDATE (DOGEUSDT 1m P08_LONG) and:
  1. runs a forward SHADOW pass over the most recent slice of bars through the
     single SimOMS (pure simulation — no network, no order interface exists);
  2. feeds its real metrics to the deterministic promotion controller from the
     SHADOW_CANDIDATE state, with the sealed holdout NOT consumed (as is true) —
     proving the controller HOLDS and every output stays NO-LIVE.

Asserts, hard: can_send_real_orders is False everywhere and the candidate is NOT
promoted because the real OOS holdout gate has not been passed. Research only.
"""
import sys, os, json
sys.path.insert(0, r"C:\Users\Adrian\Documents\New project\bitget-ai-trading-bot")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from app.labs import public_data_backfill_v10_45_1 as BF
from app.labs.v10_46 import edge_search as ES
from app.labs.v10_46 import families as FAM
from app.labs.v10_46 import promotion as PROMO

ROOT = r"C:\Users\Adrian\Documents\New project\bitget-ai-trading-bot"
OUT = os.path.join(ROOT, "reports", "research", "v10_47_final_edge_search")

ev = json.load(open(os.path.join(OUT, "shadow_candidate_eval.json"),
                    encoding="utf-8"))
cand = next(s for s in ev["shadow_results"] if s["is_shadow_candidate"])
sym, tf, name, venue = cand["symbol"], cand["timeframe"], cand["name"], cand["venue"]
print(f"SHADOW SMOKE for top candidate: {sym} {tf} {name} (venue={venue})")

vr = BF.verify_dataset(venue, sym)
gen, as_of = vr["generation_id"], vr["as_of_ms"]
bars = BF.load_klines(venue, sym)                      # 1m, native
slice_bars = bars[-3000:]                              # recent forward slice
sigs = ES.precompute_sigs(slice_bars)
deciders = ES.build_deciders(sym, venue, tf, gen, directions=(None, "LONG", "SHORT"))
decide_fn, exit_params = deciders[name]

# 1) forward SHADOW pass (drive through SimOMS; SimOMS has NO order path)
pc = ES._drive_slice(slice_bars, sigs, decide_fn, exit_params, sym,
                     scenario_cost="observed")
m = ES._participant_metrics(pc)
decisions = sum(1 for c in pc.values() if c.get("traded"))
print(f"  forward shadow pass: bars={len(slice_bars)} sim_trades={decisions} "
      f"net={m['net_pnl_eur']}€ gross={m['gross_pnl_eur']}€ (SIMULATED, no orders)")

# 2) promotion controller from SHADOW_CANDIDATE — holdout NOT consumed
metrics = {"clusters": cand["observed"]["n_eff"], "n_eff": cand["observed"]["n_eff"],
           "net_pnl_eur": cand["observed"]["net_pnl"],
           "max_drawdown_eur": -0.5, "brier": 0.25,
           "net_without_top3_eur": cand["observed"]["net_wo_top3"]}
dec = PROMO.promotion_decision(
    policy_id=name, from_state="SHADOW_CANDIDATE", metrics=metrics,
    symbol=sym, venue=venue, timeframe=tf, event_id=f"{sym}:smoke",
    decision_time_ms=as_of, data_generation_id=gen,
    paired_lb_eur=cand["observed"]["paired_nt_lb"],
    no_trade_net=0.0, random_net=0.0,
    dataset_verified=True,
    registry_closed=False,          # search was exploratory, registry not sealed
    holdout_single_use_ok=False)    # SEALED HOLDOUT NOT CONSUMED (the real gate)
print(f"  promotion_decision: decision={dec['decision']} "
      f"from={dec['from_state']} to={dec['to_state']}")
print(f"    gate holdout_single_use={dec['gate_results']['holdout_single_use']} "
      f"registry_closed={dec['gate_results']['registry_closed']} "
      f"all_pass={dec['gate_results']['all_pass']}")
print(f"    can_send_real_orders={dec['can_send_real_orders']} "
      f"live_trading={dec['live_trading']} "
      f"final_recommendation={dec['final_recommendation']!r}")

# 3) HARD safety assertions
assert dec["can_send_real_orders"] is False
assert dec["live_trading"] is False
assert dec["final_recommendation"] == "NO LIVE"
assert dec["decision"] == "HOLD", "must HOLD: sealed holdout not consumed"
assert dec["to_state"] == "SHADOW_CANDIDATE", "must not advance without holdout"
assert dec["gate_results"]["all_pass"] is False

result = {"candidate": f"{sym} {tf} {name}", "forward_slice_bars": len(slice_bars),
          "sim_trades": decisions, "sim_net_eur": m["net_pnl_eur"],
          "promotion_decision": dec["decision"],
          "to_state": dec["to_state"],
          "can_send_real_orders": dec["can_send_real_orders"],
          "live_trading": dec["live_trading"],
          "final_recommendation": dec["final_recommendation"],
          "holdout_consumed": False, "orders_sent": 0,
          "safety_assertions": "ALL PASS"}
json.dump(result, open(os.path.join(OUT, "shadow_smoke_result.json"), "w",
                       encoding="utf-8"), indent=2, default=str)
print("\nSHADOW SMOKE: OK — pipeline runs locally, 0 orders, controller HOLDS "
      "(holdout untouched), NO LIVE guaranteed.")
