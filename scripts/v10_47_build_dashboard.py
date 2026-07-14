"""V10.47 dashboard — pure render of the completed edge-search into the V10.46
integrated dashboard shell. No live, no orders, no tournament re-run here."""
import sys, os, json, subprocess
sys.path.insert(0, r"C:\Users\Adrian\Documents\New project\bitget-ai-trading-bot")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from app.labs.v10_46 import dashboard as DASH

ROOT = r"C:\Users\Adrian\Documents\New project\bitget-ai-trading-bot"
OUT = os.path.join(ROOT, "reports", "research", "v10_47_final_edge_search")

rows = json.load(open(os.path.join(OUT, "tournament_rows.json"), encoding="utf-8"))
ev = json.load(open(os.path.join(OUT, "shadow_candidate_eval.json"), encoding="utf-8"))
smoke = json.load(open(os.path.join(OUT, "shadow_smoke_result.json"), encoding="utf-8"))
commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT).decode().strip()
tree = subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=ROOT).decode().strip()

shadows = [s for s in ev["shadow_results"] if s["is_shadow_candidate"]]
# top participants for the tournament table: the 2 shadows + best non-shadow net
top = sorted(rows, key=lambda r: r["net_pnl"], reverse=True)[:12]
participants = {}
for r in top:
    tag = " [SHADOW]" if any(s["symbol"] == r["symbol"] and s["timeframe"] == r["timeframe"]
                             and s["name"] == r["name"] for s in shadows) else ""
    participants[f"{r['symbol']} {r['timeframe']} {r['name']}{tag}"] = {
        "trades": r["trades"], "net_pnl_eur": round(r["net_pnl"], 4),
        "ev_per_trade_eur": round(r["net_ev"], 5), "n_eff": r["n_eff"],
        "max_drawdown_eur": round(r["max_dd"], 4), "brier": None}

ng = sum(1 for r in rows if r["class"] == "NO_GROSS_EDGE")
ck = sum(1 for r in rows if r["class"] == "GROSS_EDGE_COST_KILLED")
npv = sum(1 for r in rows if r["class"] == "NET_EDGE_POSITIVE")

verdict = (
    "V10.47 EDGE SEARCH — NO CONFIRMED EDGE. "
    f"{len(rows)} participant-runs (12 symbol/timeframe combos): "
    f"{ng} NO_GROSS_EDGE, {ck} GROSS_EDGE_COST_KILLED, {npv} in-sample "
    f"NET_EDGE_POSITIVE. {len(shadows)} SHADOW_CANDIDATE(s) cleared all 9 "
    "pre-registered gates (DOGEUSDT/XRPUSDT 1m P08_LONG, mean-reversion) — but "
    "with 576 screened runs, tiny euros, small n, PROXY signals and an UNTOUCHED "
    "sealed holdout these are forward-test leads, NOT validated edges. "
    "SHADOW_CANDIDATE != PAPER_CHAMPION != LIVE. Shadow smoke: pipeline runs "
    "locally, 0 orders, promotion controller HOLDS, NO LIVE.")

report = {
    "provenance": {"repo_commit": commit, "tree_oid": tree,
                   "data_generation_id": "per-symbol verified generations "
                   "(bitget/bybit 90d 1m klines)",
                   "collectors": "public klines generations (V10.45.1 backfill)",
                   "run_modes": "replay + walk-forward + shadow (no live)",
                   "seal_match": "sealed holdout NOT consumed by this search"},
    "safety": {"mode": "REPLAY / SIMULATION / SHADOW RESEARCH ONLY",
               "can_send_real_orders": False},
    "market": {"regime": "historical replay; SHORT/RISK_OFF/TREND_DOWN priority",
               "cross_venue": "bitget primary, bybit reference where verified"},
    "decision": {"abstention": "meta-abstention active; P12 DATA_NOT_AVAILABLE"},
    "position": {"exposure_eur": 5.0, "leverage": "1x (no added margin/DCA)",
                 "notional_eur": 5.0, "reason": "money scenarios 5/10/20 EUR"},
    "tournament": {
        "champion": "NONE PROMOTED (no validated edge)",
        "participants": participants,
        "paired": {"B_vs_A": {"mean_diff_eur": None, "lower_bound_eur": None}},
        "promotion_status": f"{len(shadows)} SHADOW_CANDIDATE (forward-test only); "
                            "0 promoted; holdout untouched"},
    "learning": {"last_cause": "costs dominate small gross edges at 5 EUR notional",
                 "lesson": "net edge concentrates on P08 mean-reversion 1m but is "
                           "fragile out-of-sample",
                 "mutation": "n/a (final edge-search sprint)"},
    "reports": {
        "tournament": "reports/research/v10_47_final_edge_search/tournament_report.md",
        "gross_edge": "reports/research/v10_47_final_edge_search/gross_edge_report.md",
        "cost_attribution": "reports/research/v10_47_final_edge_search/cost_attribution_report.md",
        "walk_forward": "reports/research/v10_47_final_edge_search/walk_forward_report.md",
        "shadow_candidate": "reports/research/v10_47_final_edge_search/shadow_candidate_report.md",
        "shadow_smoke": f"orders_sent={smoke['orders_sent']}, "
                        f"decision={smoke['promotion_decision']}, "
                        f"final={smoke['final_recommendation']}"},
    "verdict": verdict}

path = os.path.join(OUT, "dashboard_v10_47.html")
DASH.build_dashboard(report, path)
json.dump(report, open(os.path.join(OUT, "dashboard_v10_47_report.json"), "w",
                       encoding="utf-8"), indent=2, default=str)
print("dashboard written:", path)
print("participants in table:", len(participants), "| shadows:", len(shadows))
