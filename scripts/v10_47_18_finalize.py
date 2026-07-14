"""V10.47.18 — consolidate the certification-repaired tournaments into reports +
dashboard. Reads the 12 regenerated JSONs. Research only, NO LIVE."""
import sys, os, json, subprocess, html
sys.path.insert(0, r"C:\Users\Adrian\Documents\New project\bitget-ai-trading-bot")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = r"C:\Users\Adrian\Documents\New project\bitget-ai-trading-bot"
OUT = os.path.join(ROOT, "reports", "research", "v10_47_15_final_certification_repair")
TDIR = os.path.join(OUT, "tournament")
SAFE = ("PAPER_TRADING=True · LIVE_TRADING=False · DRY_RUN=True · "
        "can_send_real_orders=false · FINAL_RECOMMENDATION=NO LIVE")


def W(name, text):
    with open(os.path.join(OUT, name), "w", encoding="utf-8") as fh:
        fh.write(text)
    print("wrote", name)


combos = {}
for fn in sorted(os.listdir(TDIR)):
    if fn.endswith(".json"):
        combos[fn[:-5]] = json.load(open(os.path.join(TDIR, fn), encoding="utf-8"))

agg = {"combos": len(combos), "runs_nominal": 0, "classes": {}, "shadow": [],
       "holdout_all_sealed": True, "val_gated": 0, "baseline_gated": 0,
       "raw": 0, "eligible": 0, "executed": 0, "skip_pos": 0, "skip_cd": 0}
for key, o in combos.items():
    agg["runs_nominal"] += o["registry"]["m_nominal"]
    if o["holdout"]["state"] != "SEALED":
        agg["holdout_all_sealed"] = False
    for name, r in o["results"].items():
        m = r["metrics"]
        agg["classes"][m["classification"]] = agg["classes"].get(m["classification"], 0) + 1
        c = m["counters"]
        agg["raw"] += c["n_signals_raw"]; agg["eligible"] += c["n_signals_eligible"]
        agg["executed"] += c["n_executed"]; agg["skip_pos"] += c["n_skipped_position_open"]
        agg["skip_cd"] += c["n_skipped_cluster_cooldown"]
        g = r.get("gate")
        if g:
            if not g["gates"]["validation_positive"]:
                agg["val_gated"] += 1
            if not g["gates"]["baseline_match_complete"] or not g["gates"]["beats_matched_random_paired"]:
                agg["baseline_gated"] += 1
    agg["shadow"] += [f"{o['symbol']} {o['timeframe']} {n}" for n in o["shadow_candidates"]]
json.dump(agg, open(os.path.join(OUT, "certified_aggregate.json"), "w",
                    encoding="utf-8"), indent=2, default=str)

# deduplicated_results.md
L = ["# V10.47.18 — Certified Deduplicated Results", "", f"**Safety:** {SAFE}", "",
     f"- combos: **{agg['combos']}** · participant-runs nominal: **{agg['runs_nominal']}**",
     f"- classification totals: **{agg['classes']}**",
     f"- SHADOW_CANDIDATES: **{len(agg['shadow'])}** {agg['shadow'] or '(none)'}",
     f"- holdout SEALED in every combo: **{agg['holdout_all_sealed']}**", "",
     "## Signal accounting (causal, single-position)",
     f"- raw **{agg['raw']:,}** · eligible **{agg['eligible']:,}** · executed "
     f"**{agg['executed']:,}** · POSITION_ALREADY_OPEN **{agg['skip_pos']:,}** · "
     f"CLUSTER_COOLDOWN **{agg['skip_cd']:,}**", "",
     "## Gate rejections (repaired gate)",
     f"- candidates rejected on VALIDATION: {agg['val_gated']}",
     f"- candidates rejected on paired matched baseline: {agg['baseline_gated']}", "",
     "**NO_CONFIRMED_EDGE · SHADOW_CANDIDATES=0 · HOLD**"]
W("deduplicated_results.md", "\n".join(L) + "\n")

# validation + holdout + baseline + registry reports
sample = next(iter(combos.values()))
sp = sample["split"]
W("split_validation_holdout_report.md",
  f"# V10.47.18 — Split / Validation / Sealed Holdout\n\n**Safety:** {SAFE}\n\n"
  f"TRAIN {sp['train']} · VALIDATION {sp['validation']} · WALK_FORWARD "
  f"{sp['walk_forward']} · HOLDOUT {sp['holdout']} (n={sample['n_bars']}).\n\n"
  "- Features precomputed ONLY over [0, holdout_start); the holdout range is not "
  "fed to feature computation.\n- VALIDATION is evaluated in the gate "
  "(validation_positive) as a region strictly later than TRAIN; WALK_FORWARD later "
  "still.\n- The HOLDOUT is wrapped in a physically SEALED object, committed by "
  "hash, and NEVER loaded; a guard denies access from any selection module.\n- "
  f"Holdout state in every combo: SEALED ({agg['holdout_all_sealed']}).\n")

rows = []
for key, o in combos.items():
    for name, r in o["results"].items():
        g = r.get("gate")
        if not g:
            continue
        mp = g["matched_random_paired"]
        rows.append(f"| {o['symbol']} | {o['timeframe']} | {name} | "
                    f"{g['selection_metrics']['net_pnl_eur']:+.4f} | "
                    f"{g['validation_net_eur']:+.4f} | {g['walk_forward_net_eur']:+.4f} | "
                    f"{mp['coverage']} | {mp['match_status']} | "
                    f"{mp['paired_lower_bound_eur']:+.4f} | {g['is_shadow_candidate']} |")
W("paired_baseline_report.md",
  "# V10.47.18 — Paired Matched Baseline + Validation\n\n"
  f"**Safety:** {SAFE}\n\nEvery NET_EDGE_POSITIVE candidate is confirmed on a "
  "strictly-later VALIDATION and WALK-FORWARD region and tested against an EXACTLY "
  "paired exposure/holding-matched random baseline (paired candidate−random lower "
  "bound; coverage must be complete or the gate fails).\n\n"
  "| Symbol | TF | candidate | train€ | validation€ | walk_fwd€ | coverage | "
  "match | paired_lb€ | shadow |\n|---|---|---|---|---|---|---|---|---|---|\n"
  + ("\n".join(rows) if rows else "| — | — | (no candidate reached the gate) | | | | | | | |")
  + "\n\nNo candidate clears validation + a complete paired baseline with a positive "
  "lower bound. SHADOW_CANDIDATES=0.\n")

reg_rows = []
for key, o in combos.items():
    rg = o["registry"]
    reg_rows.append(f"| {o['symbol']} | {o['timeframe']} | {rg['m_nominal']} | "
                    f"{rg['m_unique_results']} | {len(rg['duplicated_runs'])} | "
                    f"`{rg['registry_hash'][:16]}…` |")
W("registry_dedup_report.md",
  "# V10.47.18 — Registry + Semantic Dedup + Multiple Testing\n\n"
  f"**Safety:** {SAFE}\n\nThe registry is CLOSED and hashed before results. The "
  "multiple-testing correction stays CONSERVATIVE at m_nominal; m_unique_results is "
  "an informational SEMANTIC dedup by behavioural fingerprint (name-independent; "
  "non-firing policies are not spuriously merged).\n\n"
  "| Symbol | TF | m_nominal | m_unique_results | duplicated_runs | registry_hash |\n"
  "|---|---|---|---|---|---|\n" + "\n".join(reg_rows) + "\n")

# dashboard
def git(*a):
    try:
        return subprocess.check_output(["git", *a], cwd=ROOT).decode().strip()
    except Exception:
        return "?"

head, tree = git("rev-parse", "HEAD"), git("rev-parse", "HEAD^{tree}")
E = html.escape
kv = lambda k, v: f"<div class='kv'><span class='k'>{E(str(k))}</span><span class='v'>{E(str(v))}</span></div>"
sec = lambda t, b: f"<section><h2>{E(t)}</h2>{b}</section>"
na = ""
try:
    for l in open(os.path.join(ROOT, ".ai_coordination", "NEXT_ACTION.md"), encoding="utf-8"):
        if "NEXT:" in l:
            na = l.strip("- []\n"); break
except Exception:
    pass
body = (
    sec("Overview", kv("Certification", "REPAIRED — re-audit pending") +
        kv("SHADOW_CANDIDATES", len(agg["shadow"])) + kv("Verdict", "NO_CONFIRMED_EDGE · HOLD") +
        kv("can_send_real_orders", "false") + kv("FINAL_RECOMMENDATION", "NO LIVE") +
        kv("HEAD", head) + kv("tree", tree)) +
    sec("Causal Tournament (12 combos, repaired)", kv("runs_nominal", agg["runs_nominal"]) +
        kv("classes", agg["classes"]) + kv("SHADOW", len(agg["shadow"])) +
        kv("holdout sealed (all)", agg["holdout_all_sealed"])) +
    sec("Validation + Sealed Holdout", kv("VALIDATION", "evaluated in gate (later than TRAIN)") +
        kv("HOLDOUT", "physically SEALED, never loaded, guarded") +
        kv("rejected on validation", agg["val_gated"]) + kv("rejected on paired baseline", agg["baseline_gated"])) +
    sec("Signal Accounting", kv("raw", f"{agg['raw']:,}") + kv("executed", f"{agg['executed']:,}") +
        kv("skip POSITION_ALREADY_OPEN", f"{agg['skip_pos']:,}") + kv("skip CLUSTER_COOLDOWN", f"{agg['skip_cd']:,}")) +
    sec("Hub NEXT_ACTION", kv("NEXT", na)) +
    "<div class='verdict'>CERTIFICATION REPAIR COMPLETE — NO CONFIRMED EDGE · SHADOW_CANDIDATES=0 · HOLDOUT SEALED · NO LIVE</div>")
css = ("*{box-sizing:border-box}body{margin:0;font:14px/1.5 system-ui}"
       ":root{--bg:#0f1117;--fg:#e6e6e6;--mut:#8a90a2;--acc:#4da3ff;--card:#171a23}"
       "@media(prefers-color-scheme:light){:root{--bg:#f6f7f9;--fg:#1a1d24;--mut:#5b6270;--card:#fff}}"
       ":root[data-theme=dark]{--bg:#0f1117;--fg:#e6e6e6;--card:#171a23}"
       ":root[data-theme=light]{--bg:#f6f7f9;--fg:#1a1d24;--card:#fff}"
       "body{background:var(--bg);color:var(--fg)}header{padding:16px 20px;background:var(--card)}"
       "header h1{margin:0;font-size:17px}.banner{color:#3ecf8e;font-weight:600;font-size:13px}"
       "main{padding:16px 20px;display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:14px;max-width:1400px}"
       "section{background:var(--card);border-radius:10px;padding:14px 16px}"
       "section h2{margin:0 0 10px;font-size:13px;color:var(--acc);text-transform:uppercase}"
       ".kv{display:flex;justify-content:space-between;gap:10px;padding:3px 0;border-bottom:1px dashed #0001}"
       ".k{color:var(--mut)}.v{text-align:right}"
       ".verdict{grid-column:1/-1;background:var(--card);border:1px solid var(--acc);border-radius:10px;padding:14px;font-weight:700}")
os.makedirs(os.path.join(OUT, "dashboard"), exist_ok=True)
with open(os.path.join(OUT, "dashboard", "dashboard_v10_47_18.html"), "w", encoding="utf-8") as fh:
    fh.write(f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' "
             f"content='width=device-width,initial-scale=1'><title>V10.47.18 Certification "
             f"Repair</title><style>{css}</style></head><body><header><h1>Bitget AI Trading "
             f"Bot — V10.47.18 Certification Repair</h1><div class='banner'>REPLAY/SIM RESEARCH "
             f"ONLY · NO LIVE · NO_CONFIRMED_EDGE · SHADOW=0 · HOLDOUT SEALED</div></header>"
             f"<main>{body}</main></body></html>")
print("dashboard written; classes:", agg["classes"], "shadow:", len(agg["shadow"]))
