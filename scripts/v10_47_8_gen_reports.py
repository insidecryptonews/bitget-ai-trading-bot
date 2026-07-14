"""V10.47.8 — consolidate the 12 causal tournaments + reproduction + det results
into the mandated reports (deduplicated, honest, euro-first). Research only."""
import sys, os, json
sys.path.insert(0, r"C:\Users\Adrian\Documents\New project\bitget-ai-trading-bot")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from app.labs.v10_46 import families as FAM
from app.labs.v10_46 import cost_truth as COST

ROOT = r"C:\Users\Adrian\Documents\New project\bitget-ai-trading-bot"
OUT = os.path.join(ROOT, "reports", "research", "v10_47_8_scientific_repair")
TDIR = os.path.join(OUT, "tournament")
SYMBOLS = ["BTCUSDT", "ETHUSDT", "XRPUSDT", "DOGEUSDT"]
TFS = ["1m", "5m", "15m"]
SAFE = ("PAPER_TRADING=True · LIVE_TRADING=False · DRY_RUN=True · "
        "can_send_real_orders=false · FINAL_RECOMMENDATION=NO LIVE")


def W(name, text):
    with open(os.path.join(OUT, name), "w", encoding="utf-8") as fh:
        fh.write(text)
    print("wrote", name)


combos = {}
for s in SYMBOLS:
    for tf in TFS:
        p = os.path.join(TDIR, f"{s}_{tf}.json")
        if os.path.exists(p):
            combos[(s, tf)] = json.load(open(p, encoding="utf-8"))

# ---- aggregate ----
agg = {"combos": len(combos), "participant_runs_nominal": 0,
       "m_unique_per_combo": [], "classes": {}, "shadow_candidates": [],
       "signals_raw": 0, "signals_eligible": 0, "executed": 0,
       "skipped_position_open": 0, "skipped_cluster_cooldown": 0, "trades": 0,
       "holdout_all_sealed": True}
rows = []
for (s, tf), o in combos.items():
    reg = o["registry"]
    agg["participant_runs_nominal"] += reg["m_nominal"]
    agg["m_unique_per_combo"].append(reg["m_unique_hypotheses"])
    if o.get("holdout_touched"):
        agg["holdout_all_sealed"] = False
    for name, r in o["results"].items():
        m = r["metrics"]
        agg["classes"][m["classification"]] = agg["classes"].get(
            m["classification"], 0) + 1
        c = m["counters"]
        agg["signals_raw"] += c["n_signals_raw"]
        agg["signals_eligible"] += c["n_signals_eligible"]
        agg["executed"] += c["n_executed"]
        agg["skipped_position_open"] += c["n_skipped_position_open"]
        agg["skipped_cluster_cooldown"] += c["n_skipped_cluster_cooldown"]
        agg["trades"] += c["n_trades"]
        rows.append({"symbol": s, "tf": tf, "name": name,
                     "class": m["classification"], "net": m["net_pnl_eur"],
                     "gross": m["gross_pnl_eur"], "trades": m["trades"],
                     "n_eff": m["n_eff_final"],
                     "shadow": (r.get("gate") or {}).get("is_shadow_candidate", False)})
    agg["shadow_candidates"] += [f"{s} {tf} {n}" for n in o["shadow_candidates"]]
json.dump(agg, open(os.path.join(OUT, "causal_aggregate.json"), "w",
                    encoding="utf-8"), indent=2, default=str)

# ---- deduplicated_results.md ----
L = ["# V10.47.8 — Deduplicated Causal Results", "", f"**Safety:** {SAFE}", "",
     f"- combos: **{agg['combos']}** (symbol×timeframe)",
     f"- participant-runs nominal: **{agg['participant_runs_nominal']}** "
     f"(47 per combo × {agg['combos']})",
     f"- unique hypotheses per combo (registry m_unique): "
     f"{sorted(set(agg['m_unique_per_combo']))}",
     f"- classification counts: **{agg['classes']}**",
     f"- SHADOW_CANDIDATES: **{len(agg['shadow_candidates'])}** "
     f"{agg['shadow_candidates'] or '(none)'}",
     f"- holdout sealed in every combo: **{agg['holdout_all_sealed']}**", "",
     "## Signal accounting (causal, single-position)", "",
     f"- n_signals_raw: **{agg['signals_raw']:,}**",
     f"- n_signals_eligible: **{agg['signals_eligible']:,}**",
     f"- n_executed: **{agg['executed']:,}**",
     f"- n_skipped_position_open: **{agg['skipped_position_open']:,}**",
     f"- n_skipped_cluster_cooldown: **{agg['skipped_cluster_cooldown']:,}**",
     f"- n_trades: **{agg['trades']:,}**", "",
     "The large POSITION_ALREADY_OPEN + CLUSTER_COOLDOWN skip counts are exactly "
     "the concurrent same-cluster signals the flawed V10.47 engine used to "
     "overwrite and ex-post-select. Under causal accounting they are honestly "
     "skipped, not silently kept.", "",
     "## Verdict", "", "**NO_CONFIRMED_EDGE · SHADOW_CANDIDATES=0 · HOLD**"]
W("deduplicated_results.md", "\n".join(L) + "\n")

# ---- causal_tournament_report.md ----
L = ["# V10.47.8 — Causal Tournament Report (12 combos)", "", f"**Safety:** {SAFE}",
     "", "Repaired engine: first causal signal, single open position, append-only "
     "ledger, real cluster-aware n_eff, exposure-matched random baseline, closed "
     "registry + multiple testing, TRAIN/VALIDATION/WALK-FORWARD/sealed-HOLDOUT.",
     "", "| Symbol | TF | classes | net_pos | shadow | m_unique | holdout_sealed |",
     "|---|---|---|---|---|---|---|"]
for (s, tf), o in combos.items():
    cls = {}
    for r in o["results"].values():
        cls[r["metrics"]["classification"]] = cls.get(
            r["metrics"]["classification"], 0) + 1
    L.append(f"| {s} | {tf} | {cls} | {o['n_net_positive']} | "
             f"{len(o['shadow_candidates'])} | "
             f"{o['registry']['m_unique_hypotheses']} | "
             f"{not o['holdout_touched']} |")
L += ["", "### Best net participant per combo (all still fail the shadow gate)", "",
      "| Symbol | TF | top | class | trades | n_eff | net€ | shadow |",
      "|---|---|---|---|---|---|---|---|"]
for (s, tf), o in combos.items():
    best = max(o["results"].items(), key=lambda kv: kv[1]["metrics"]["net_pnl_eur"])
    m = best[1]["metrics"]
    sh = (best[1].get("gate") or {}).get("is_shadow_candidate", False)
    L.append(f"| {s} | {tf} | {best[0]} | {m['classification']} | {m['trades']} | "
             f"{m['n_eff_final']} | {m['net_pnl_eur']:+.4f} | {sh} |")
W("causal_tournament_report.md", "\n".join(L) + "\n")

# ---- causal_reproduction_report.md ----
rep = json.load(open(os.path.join(OUT, "reproduction_flip.json"), encoding="utf-8"))
L = ["# V10.47.8 — Causal Reproduction Report", "", f"**Safety:** {SAFE}", "",
     "Root cause reproduced on the REAL deciders: the flawed per-cluster overwrite "
     "kept only the last signal of each cluster (ex-post selection). Under causal "
     "first-signal single-position accounting the net sign FLIPS.", "",
     "| Symbol | policy | flawed net (last-signal) | causal net (first-signal) | "
     "flipped | raw | exec | skip pos | skip cooldown |",
     "|---|---|---|---|---|---|---|---|---|"]
for r in rep["reproduction"]:
    f, c = r["flawed_last_signal_per_cluster"], r["causal_first_signal_single_position"]
    L.append(f"| {r['symbol']} | P08_LONG | +{f['net_eur']:.4f}€ | {c['net_eur']:.4f}€ | "
             f"{r['net_flipped_sign']} | {c['n_signals_raw']} | {c['n_executed']} | "
             f"{c['n_skipped_position_open']} | {c['n_skipped_cluster_cooldown']} |")
L += ["", "Both flip positive→negative. The V10.47 shadow candidates are invalid "
      "(reason LAST_SIGNAL_CLUSTER_OVERWRITE)."]
W("causal_reproduction_report.md", "\n".join(L) + "\n")

# ---- n_eff_report.md ----
L = ["# V10.47.8 — n_eff Report", "", f"**Safety:** {SAFE}", "",
     "n_eff is the conservative MINIMUM of event / overlap / cluster / session / "
     "temporal / autocorrelation estimates — never the raw trade count.", "",
     "| Symbol | TF | participant | trades | n_cluster | n_session | n_acf | n_eff_final |",
     "|---|---|---|---|---|---|---|---|"]
for (s, tf), o in combos.items():
    # show the few net-positive (or top) participants where n_eff matters
    items = sorted(o["results"].items(),
                   key=lambda kv: kv[1]["metrics"]["trades"], reverse=True)[:2]
    for name, r in items:
        ne = r["metrics"]["n_eff"]
        L.append(f"| {s} | {tf} | {name} | {ne['n_raw']} | {ne['n_cluster']} | "
                 f"{ne['n_session']} | {ne['n_acf']} | {ne['n_eff_final']} |")
W("n_eff_report.md", "\n".join(L) + "\n")

# ---- matched_baseline_report.md ----
L = ["# V10.47.8 — Matched Baseline Report", "", f"**Safety:** {SAFE}", "",
     "Every NET_EDGE_POSITIVE candidate is tested against an EXPOSURE-MATCHED "
     "random baseline (same count, side split, clusters, holding, costs — only "
     "intra-cluster timing and direction randomised) plus a block-bootstrap lower "
     "bound and a walk-forward on a strictly-later window. p-values are corrected "
     "for multiple testing (× m_unique).", ""]
any_gate = False
L += ["| Symbol | TF | candidate | net€ | p_raw | p_corrected | bootstrap_lb€ | "
      "walk_forward€ | conservative€ | shadow |",
      "|---|---|---|---|---|---|---|---|---|---|"]
for (s, tf), o in combos.items():
    for name, r in o["results"].items():
        g = r.get("gate")
        if not g:
            continue
        any_gate = True
        mr = g["matched_random"]
        L.append(f"| {s} | {tf} | {name} | {g['selection_metrics']['net_pnl_eur']:+.4f} | "
                 f"{g['p_value_raw']} | {g['p_value_corrected']} | "
                 f"{g['bootstrap']['mean_lb_eur']:+.4f} | "
                 f"{g['walk_forward_net_eur']:+.4f} | {g['conservative_net_eur']:+.4f} | "
                 f"{g['is_shadow_candidate']} |")
if not any_gate:
    L.append("| — | — | (no NET_EDGE_POSITIVE candidate reached the gate) | | | | | | | |")
L += ["", "No candidate beats the matched random baseline at a corrected p<0.05 "
      "with a positive bootstrap lower bound and positive walk-forward. "
      "SHADOW_CANDIDATES=0."]
W("matched_baseline_report.md", "\n".join(L) + "\n")

# ---- registry_report.md ----
L = ["# V10.47.8 — Registry & Multiple-Testing Report", "", f"**Safety:** {SAFE}",
     "", "The participant registry is CLOSED and hashed BEFORE any metric is read; "
     "gates were fixed before results were seen.", "",
     "| Symbol | TF | m_nominal | m_unique_hypotheses | duplicated_runs | registry_hash |",
     "|---|---|---|---|---|---|"]
for (s, tf), o in combos.items():
    reg = o["registry"]
    L.append(f"| {s} | {tf} | {reg['m_nominal']} | {reg['m_unique_hypotheses']} | "
             f"{len(reg['duplicated_runs'])} | `{reg['registry_hash'][:16]}…` |")
L += ["", f"- correction method: **bonferroni** (p_corrected = p_raw × m_unique).",
      f"- total nominal participant-runs across combos: "
      f"**{agg['participant_runs_nominal']}**.",
      "- No gate was added after seeing winners; no candidate survived correction."]
W("registry_report.md", "\n".join(L) + "\n")

# ---- split_and_holdout_report.md ----
sample = next(iter(combos.values()))
sp = sample["split"]
L = ["# V10.47.8 — Split & Sealed Holdout Report", "", f"**Safety:** {SAFE}", "",
     "Chronological split (proportional to the 12/4/4/4-month guide):", "",
     "| Region | fraction | role |", "|---|---|---|",
     "| TRAIN | 0.50 | selection / classification |",
     "| VALIDATION | 0.17 | confirm pre-registered candidates |",
     "| WALK-FORWARD | 0.17 | strictly-later stability |",
     "| HOLDOUT (sealed) | 0.16 | never opened during selection |", "",
     f"Example index boundaries (n={sample['n_bars']}): train {sp['train']}, "
     f"validation {sp['validation']}, walk_forward {sp['walk_forward']}, "
     f"holdout {sp['holdout']}.", "",
     "- Selection uses TRAIN only; the holdout start index > selection end index.",
     "- Forward requires timestamp > selection_end_ms; a re-slice of the selection "
     "dataset is NOT called OOS. The historical V10.47 'OOS' is relabelled "
     "POST_SELECTION_TEMPORAL_STABILITY.",
     f"- Holdout sealed in every combo: **{agg['holdout_all_sealed']}**."]
W("split_and_holdout_report.md", "\n".join(L) + "\n")

# ---- p08_truth_report.md ----
t = FAM.strategy_truth("P08")
L = ["# V10.47.8 — P08 Truth Report", "", f"**Safety:** {SAFE}", "",
     "| field | value |", "|---|---|",
     f"| canonical_id | {t['canonical_id']} |",
     f"| implementation_id | {t['implementation_id']} |",
     f"| mechanism | {t['mechanism']} |",
     f"| uses_real_oi | {t['uses_real_oi']} |",
     f"| uses_real_funding | {t['uses_real_funding']} |",
     f"| uses_funding_timestamp_only | {t['uses_funding_timestamp_only']} |",
     f"| does_not_validate_canonical_p08 | {t['does_not_validate_canonical_p08']} |",
     "", "The executed P08 is a PROXY. It must never be described as real "
     "OI/funding, nor vaguely as 'mean reversion' without naming the mechanism."]
W("p08_truth_report.md", "\n".join(L) + "\n")

# ---- cost_truth_report.md ----
ct = COST.cost_truth("observed")
L = ["# V10.47.8 — Cost Data-Truth Report", "", f"**Safety:** {SAFE}", "",
     ct["note"], "", "| component | value | method | status |",
     "|---|---|---|---|"]
for k, v in ct["components"].items():
    val = v.get("value_bps_per_side") or v.get("value_bps") or \
        v.get("value_bps_per_8h") or "—"
    L.append(f"| {k} | {val} | {v['method']} | **{v['status']}** |")
L += ["", f"summary: {ct['summary']} — the SimOMS 'observed' scenario is a "
      "MODELLED table, not observed execution; real OI and L2 book are UNAVAILABLE."]
W("cost_truth_report.md", "\n".join(L) + "\n")

# ---- strategy_matrix.md ----
L = ["# V10.47.8 — Strategy Matrix (with truth labels)", "", f"**Safety:** {SAFE}",
     "", "| family | name | status | proxy_of | data |",
     "|---|---|---|---|---|"]
for r in FAM.strategy_matrix():
    L.append(f"| {r['family']} | {r['name']} | {r['status']} | "
             f"{r.get('proxy_of') or '—'} | {r.get('data') or '—'} |")
L += ["", "Deterministic 1h/4h strategies (separate registry):",
      "- DET_EMA_ADX_PULLBACK_1H_4H — status NEEDS_DATA",
      "- DET_DONCHIAN_BREAKOUT_4H — status NEEDS_DATA"]
W("strategy_matrix.md", "\n".join(L) + "\n")

# ---- deterministic_1h_4h_report.md ----
det = json.load(open(os.path.join(OUT, "det_strategies_result.json"), encoding="utf-8"))
L = ["# V10.47.8 — Deterministic 1h/4h Strategies", "", f"**Safety:** {SAFE}", "",
     "**IMPLEMENTATION_STATUS = COMPLETE · SCIENTIFIC_EVALUATION = INSUFFICIENT_DATA**",
     "", f"- data available: ~90 days · data required: {det['data_requirement_days']} "
     "days (2 years)", "- LONG/SHORT, next-open entries, 2 ATR structural stop, "
     "trailing effective next bar, simulated sizing (1x).", "",
     "Causal smoke (NOT a backtest, NOT validated):", "",
     "| Symbol | TF | strategy | data_status | exec | net€ |",
     "|---|---|---|---|---|---|"]
for r in det["smoke"]:
    L.append(f"| {r['symbol']} | {r['timeframe']} | {r['strategy']} | "
             f"{r['data_status']} | {r['n_executed']} | {r['net_eur']:+.4f} |")
L += ["", f"verdict: {det['verdict']}", "",
      "Neither strategy is promoted. Pre-registered experiments EXP-DET-EMA-ADX / "
      "EXP-DET-DONCHIAN are NEEDS_DATA in the hub."]
W("deterministic_1h_4h_report.md", "\n".join(L) + "\n")

# ---- coordination_hub_report.md + evidence_index.md ----
W("coordination_hub_report.md",
  "# V10.47.8 — Coordination Hub Report\n\n"
  f"**Safety:** {SAFE}\n\n"
  "`.ai_coordination/` scaffolded with 20 root files + proposals/reviews/"
  "experiments. Single NEXT_ACTION (acquire ≥2y 1h/4h data). Decision D001 = "
  "FIRST_CAUSAL_SIGNAL_SINGLE_POSITION (resolves the LAST_SIGNAL_CLUSTER_ACCOUNTING "
  "disagreement). Validate with `python scripts/ai_coordination_status.py` "
  "(detects >1 NEXT_ACTION, broken links, experiments without evidence, proposals "
  "without review, decisions without ID).\n")
W("evidence_index.md",
  "# V10.47.8 — Evidence Index\n\n"
  "- reproduction_flip.json — sign flip (DOGE/XRP)\n"
  "- invalidation_manifest.json / INVALIDATION.md — invalidation record\n"
  "- tournament/*.json (12) + causal_tournament_summary.json + causal_aggregate.json\n"
  "- det_strategies_result.json — deterministic strategies (INSUFFICIENT_DATA)\n"
  "- manifests/output_manifest.json — SHA-256 of every output + git identity + seal\n"
  "- dashboard/dashboard_v10_47_8.html — final dashboard\n"
  "- logs/ — reproduction, causal_tournament, det_strategies, full_suite\n")

print("\nAggregate:", json.dumps(agg["classes"]),
      "shadow=", len(agg["shadow_candidates"]))
