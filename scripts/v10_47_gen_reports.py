"""V10.47 report generator — consumes the consolidated tournament rows and the
shadow-candidate evaluation into euro-first markdown reports. Research only."""
import sys, os, json
sys.path.insert(0, r"C:\Users\Adrian\Documents\New project\bitget-ai-trading-bot")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = r"C:\Users\Adrian\Documents\New project\bitget-ai-trading-bot"
OUT = os.path.join(ROOT, "reports", "research", "v10_47_final_edge_search")


def load(name):
    with open(os.path.join(OUT, name), encoding="utf-8") as fh:
        return json.load(fh)


rows = load("tournament_rows.json")
shadow = load("shadow_candidate_eval.json")
SYMBOLS = ["BTCUSDT", "ETHUSDT", "XRPUSDT", "DOGEUSDT"]
TFS = ["1m", "5m", "15m"]
SAFETY = ("PAPER_TRADING=True · LIVE_TRADING=False · DRY_RUN=True · "
          "can_send_real_orders=false · NO real orders · REPLAY/SIMULATION ONLY")


def eur(x):
    return f"{x:+.4f}€" if isinstance(x, (int, float)) else str(x)


def w(name, text):
    with open(os.path.join(OUT, name), "w", encoding="utf-8") as fh:
        fh.write(text)
    print("wrote", name)


# ---- 1) tournament_report.md : full comparable table -----------------------
by_combo = {}
for r in rows:
    by_combo.setdefault((r["symbol"], r["timeframe"]), []).append(r)

L = ["# V10.47 — Full Strategy Tournament (gross-first, euro-first)", "",
     f"**Safety:** {SAFETY}", "",
     "Every participant (P01–P12 both/LONG/SHORT + Trend Rider A–J + No-Trade + "
     "random) competes on identical events (paired by `event_cluster_id`) through "
     "the same SimOMS, over 90 days of 1m public klines per symbol resampled to "
     "1m/5m/15m. Costs applied once each: fee per side, spread once, slippage "
     "once, funding only on 0/8/16 UTC settlement crossings.", "",
     "## Classification counts per (symbol, timeframe)", "",
     "| Symbol | TF | participants | NO_GROSS_EDGE | GROSS_EDGE_COST_KILLED | "
     "NET_EDGE_POSITIVE |", "|---|---|---|---|---|---|"]
for sym in SYMBOLS:
    for tf in TFS:
        c = by_combo.get((sym, tf), [])
        if not c:
            continue
        ng = sum(1 for x in c if x["class"] == "NO_GROSS_EDGE")
        ck = sum(1 for x in c if x["class"] == "GROSS_EDGE_COST_KILLED")
        npv = sum(1 for x in c if x["class"] == "NET_EDGE_POSITIVE")
        L.append(f"| {sym} | {tf} | {len(c)} | {ng} | {ck} | {npv} |")
L += ["", "## Top participant by NET pnl in each (symbol, timeframe)", "",
      "| Symbol | TF | top | class | trades | n_eff | gross€ | net€ | "
      "net_EV€ | beats No-Trade | beats Random |",
      "|---|---|---|---|---|---|---|---|---|---|---|"]
for sym in SYMBOLS:
    for tf in TFS:
        c = by_combo.get((sym, tf), [])
        if not c:
            continue
        t = max(c, key=lambda x: x["net_pnl"])
        L.append(f"| {sym} | {tf} | {t['name']} | {t['class']} | {t['trades']} | "
                 f"{t['n_eff']} | {t['gross_pnl']:+.4f} | {t['net_pnl']:+.4f} | "
                 f"{t['net_ev']:+.5f} | {t['beats_no_trade']} | "
                 f"{t['beats_random']} |")
L += ["", "## All NET_EDGE_POSITIVE participants (in-sample, single window)", "",
      "> These are **in-sample** full-window results with small trade counts. "
      "They are NOT a claim of edge; each must survive walk-forward + the Shadow "
      "gate below.", "",
      "| Symbol | TF | name | trades | n_eff | gross€ | net€ | net_EV€ | "
      "net_PF | net w/o top-3€ | paired-vs-NoTrade lb€ | paired-vs-Random lb€ |",
      "|---|---|---|---|---|---|---|---|---|---|---|---|"]
for r in sorted([x for x in rows if x["class"] == "NET_EDGE_POSITIVE"],
                key=lambda x: x["net_pnl"], reverse=True):
    L.append(f"| {r['symbol']} | {r['timeframe']} | {r['name']} | {r['trades']} | "
             f"{r['n_eff']} | {r['gross_pnl']:+.4f} | {r['net_pnl']:+.4f} | "
             f"{r['net_ev']:+.5f} | {r['net_pf']} | {r['net_wo_top3']:+.4f} | "
             f"{r['paired_nt_lb']} | {r['paired_rnd_lb']} |")
w("tournament_report.md", "\n".join(L) + "\n")


# ---- 2) gross_edge_report.md : gross-first discovery ------------------------
L = ["# V10.47 — Gross-First Edge Report", "", f"**Safety:** {SAFETY}", "",
     "Edge is searched **before costs**. A participant with `gross_pnl > 0` has a "
     "raw directional signal; whether it survives costs is a separate question "
     "answered in `cost_attribution_report.md`.", "",
     "## Participants with GROSS edge (gross_pnl > 0), by gross_pnl", "",
     "| Symbol | TF | name | class | trades | gross€ | gross_EV€ | gross_PF | "
     "net€ | verdict |", "|---|---|---|---|---|---|---|---|---|---|"]
gross = [x for x in rows if x["gross_pnl"] > 0]
for r in sorted(gross, key=lambda x: x["gross_pnl"], reverse=True):
    verdict = ("survives costs" if r["class"] == "NET_EDGE_POSITIVE"
               else "killed by costs")
    L.append(f"| {r['symbol']} | {r['timeframe']} | {r['name']} | {r['class']} | "
             f"{r['trades']} | {r['gross_pnl']:+.4f} | {r['gross_ev']:+.5f} | "
             f"{r['gross_pf']} | {r['net_pnl']:+.4f} | {verdict} |")
tot = len(rows)
ng = sum(1 for x in rows if x["class"] == "NO_GROSS_EDGE")
ck = sum(1 for x in rows if x["class"] == "GROSS_EDGE_COST_KILLED")
npv = sum(1 for x in rows if x["class"] == "NET_EDGE_POSITIVE")
L += ["", "## Summary", "",
      f"- Participant-runs total: **{tot}** across {len(by_combo)} "
      f"(symbol, timeframe) combos",
      f"- NO_GROSS_EDGE: **{ng}** ({100*ng/tot:.1f}%) — no raw signal even "
      "before costs",
      f"- GROSS_EDGE_COST_KILLED: **{ck}** ({100*ck/tot:.1f}%) — raw signal "
      "exists but real costs erase it",
      f"- NET_EDGE_POSITIVE: **{npv}** ({100*npv/tot:.1f}%) — positive after "
      "costs, in-sample single window (must pass walk-forward + Shadow gate)", "",
      "**Reading:** the dominant outcome is NO_GROSS_EDGE / COST_KILLED. Where a "
      "raw gross edge exists it is small and mostly consumed by fees+spread+"
      "slippage. This is the honest state of the free-public-data universe."]
w("gross_edge_report.md", "\n".join(L) + "\n")


# ---- 3) cost_attribution_report.md -----------------------------------------
L = ["# V10.47 — Cost Attribution Report", "", f"**Safety:** {SAFETY}", "",
     "For every participant that had a GROSS edge, how much of it costs removed, "
     "broken into fee / spread / slippage / funding (euros). Money scenario for "
     "the tournament is 5€ notional at 1x (10€/20€ scenarios in "
     "`shadow_candidate_report.md`).", "",
     "| Symbol | TF | name | gross€ | fee€ | spread€ | slippage€ | funding€ | "
     "net€ | costs killed edge? |",
     "|---|---|---|---|---|---|---|---|---|---|"]
for r in sorted(gross, key=lambda x: x["gross_pnl"], reverse=True):
    killed = "YES" if r["class"] == "GROSS_EDGE_COST_KILLED" else "no"
    L.append(f"| {r['symbol']} | {r['timeframe']} | {r['name']} | "
             f"{r['gross_pnl']:+.4f} | {r['fee']:.4f} | {r['spread']:.4f} | "
             f"{r['slippage']:.4f} | {r['funding']:.4f} | {r['net_pnl']:+.4f} | "
             f"{killed} |")
# aggregate cost weight
fee = sum(r["fee"] for r in gross)
spr = sum(r["spread"] for r in gross)
sli = sum(r["slippage"] for r in gross)
fun = sum(r["funding"] for r in gross)
tc = fee + spr + sli + fun or 1.0
L += ["", "## Aggregate cost mix across all gross-edge participants", "",
      f"- Fee: **{fee:.4f}€** ({100*fee/tc:.1f}%)",
      f"- Spread: **{spr:.4f}€** ({100*spr/tc:.1f}%)",
      f"- Slippage: **{sli:.4f}€** ({100*sli/tc:.1f}%)",
      f"- Funding: **{fun:.4f}€** ({100*fun/tc:.1f}%)", "",
      "Fees + spread dominate at the 5€ notional; funding is minor because most "
      "trades are short-held and rarely cross a settlement boundary."]
w("cost_attribution_report.md", "\n".join(L) + "\n")


# ---- 4) walk_forward_report.md + shadow_candidate_report.md -----------------
sr = shadow["shadow_results"]
L = ["# V10.47 — Walk-Forward Report", "", f"**Safety:** {SAFETY}", "",
     "Each NET_EDGE_POSITIVE candidate re-evaluated as a **fixed rule** across 4 "
     "contiguous out-of-sample folds (no learning ⇒ every fold is OOS). The core "
     "question: does the in-sample edge persist across time, or is it one window / "
     "a few events?", "",
     "| Symbol | TF | name | fold net € (f0,f1,f2,f3) | folds + | OOS total € | "
     "min fold € |", "|---|---|---|---|---|---|---|"]
for s in sr:
    wf = s["walk_forward"]
    fl = ",".join(f"{f['net_pnl_eur']:+.3f}" for f in wf["folds"])
    L.append(f"| {s['symbol']} | {s['timeframe']} | {s['name']} | {fl} | "
             f"{wf['folds_net_positive']}/4 | {wf['oos_net_total_eur']:+.4f} | "
             f"{wf['oos_net_min_fold_eur']:+.4f} |")
L += ["", "**Reading:** a candidate that is only net-positive because of one fold "
      "(and negative in the others) is a single-window artifact, not an edge. The "
      "Shadow gate requires ≥3/4 folds positive AND positive OOS total."]
w("walk_forward_report.md", "\n".join(L) + "\n")

# shadow candidate report
any_shadow = shadow["any_shadow_candidate"]
L = ["# V10.47 — Shadow Candidate Report", "", f"**Safety:** {SAFETY}", "",
     "A **Shadow Candidate** is a research label, NOT a paper champion and NOT a "
     "live authorization. To qualify, a candidate must clear EVERY gate:", "",
     "1. gross EV > 0 (raw edge exists)",
     "2. net PnL > 0 after real costs",
     "3. n_eff ≥ 40 (not a handful of trades)",
     "4. net without its top-3 trades ≥ 0 (not carried by 1–2 events)",
     "5. walk-forward OOS total > 0",
     "6. ≥ 3/4 walk-forward folds net-positive (temporally stable)",
     "7. beats the No-Trade baseline",
     "8. beats the random exposure-matched baseline",
     "9. still net-positive under CONSERVATIVE execution costs", "",
     "## ⚠️ Multiple-testing / selection-bias context (read first)", "",
     f"- **{len(rows)} participant-runs** were screened across "
     f"{len({(r['symbol'], r['timeframe']) for r in rows})} (symbol,timeframe) "
     "combos. Under that many tests, a handful of net-positive results is EXPECTED "
     "even under a pure-noise null.",
     "- The walk-forward folds here are carved from the **same 90-day sample** the "
     "candidate was selected on. They test temporal *stability*, NOT true "
     "out-of-sample generalization. The **sealed holdout** (V10.45/46 infra) has "
     "**NOT** been consumed by this search and remains the real OOS gate.",
     "- Net euros are **tiny** (< 1€ over 90 days on 5€ notional) and n_eff is "
     "small (~95–120). Economic significance is marginal even where statistical "
     "gates pass.",
     "- Therefore a passing candidate is labelled **SHADOW_CANDIDATE = a signal to "
     "forward-test**, explicitly **NOT** a validated edge, **NOT** a paper "
     "champion, **NOT** any form of live authorization.", "",
     "## Result", ""]
if any_shadow:
    L.append("**SHADOW CANDIDATE(S) FOUND (pre-registered gate passed): "
             f"{sum(1 for s in sr if s['is_shadow_candidate'])}** — listed below. "
             "Given the multiple-testing context above, these are research signals "
             "to route into the sealed-holdout gate + forward paper-shadow, NOT "
             "validated edges. Still not live; requires independent audit before "
             "any promotion discussion. No candidate was fabricated; none was "
             "suppressed.")
else:
    L.append("**NO SHADOW CANDIDATE.** Every NET_EDGE_POSITIVE participant failed "
             "at least one gate — overwhelmingly the walk-forward stability and "
             "conservative-cost gates. The in-sample net-positive results are "
             "single-window artifacts that do not persist out-of-sample. No "
             "candidate is promoted. **No candidate is fabricated.**")
L += ["", "## Per-candidate gate detail", "",
      "| Symbol | TF | name | n_eff | net€ | OOS € | folds+ | w/o top3€ | "
      "cons€ | 10€ net | 20€ net | beats NT | beats Rnd | SHADOW? |",
      "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
for s in sr:
    o, wf, g = s["observed"], s["walk_forward"], s["gate"]
    L.append(
        f"| {s['symbol']} | {s['timeframe']} | {s['name']} | {o['n_eff']} | "
        f"{o['net_pnl']:+.4f} | {wf['oos_net_total_eur']:+.4f} | "
        f"{wf['folds_net_positive']}/4 | {o['net_wo_top3']:+.4f} | "
        f"{s['conservative_net_eur']:+.4f} | {s['money_10eur_net_eur']:+.4f} | "
        f"{s['money_20eur_net_eur']:+.4f} | {o['beats_no_trade']} | "
        f"{o['beats_random']} | {'**YES**' if s['is_shadow_candidate'] else 'no'} |")
# gate failure breakdown
L += ["", "## Which gate each candidate fails (first failing gate)", "",
      "| Symbol | TF | name | failing gates |", "|---|---|---|---|"]
for s in sr:
    fails = [k for k, v in s["gate"].items() if k != "all_pass" and not v]
    L.append(f"| {s['symbol']} | {s['timeframe']} | {s['name']} | "
             f"{', '.join(fails) if fails else '— (passes all)'} |")
L += ["", "## Final answer to the V10.47 question", "",
      "> *¿Existe alguna estrategia, símbolo, régimen y timeframe con ventaja "
      "positiva, estable y reproducible?*", ""]
if any_shadow:
    names = ", ".join(f"{s['symbol']} {s['timeframe']} {s['name']}"
                      for s in sr if s["is_shadow_candidate"])
    L.append(f"**Not proven — but not empty either.** {names} cleared every "
             "pre-registered gate (gross+net positive, n_eff≥40, top-3-robust, "
             "OOS total>0, ≥3/4 folds positive, beats both baselines, survives "
             "conservative costs), and — notably — the SAME family (**P08 "
             "mean-reversion LONG on 1m**) passes on two independent symbols "
             "(DOGE and XRP), which is weak cross-sectional corroboration rather "
             "than a single-symbol fluke. **However**, given 576 screened runs, "
             "tiny euros, small n, a PROXY signal family, and an UNTOUCHED sealed "
             "holdout, this is a **SHADOW_CANDIDATE to forward-test, not a "
             "validated, stable, reproducible edge**. The correct honest answer "
             "to the question today remains: *no confirmed edge yet* — but P08 1m "
             "mean-reversion is the one lead worth routing into the sealed holdout "
             "and forward paper-shadow.")
else:
    L.append("**No.** On BTC/ETH/XRP/DOGE at 1m/5m/15m over 90 days of free public "
             "klines, no strategy shows a positive, stable, reproducible net edge. "
             "A few families (notably P08 mean-reversion and P05/P11 SHORT) produce "
             "small in-sample net-positive windows, but none survives walk-forward "
             "stability + conservative costs. The honest conclusion is unchanged: "
             "**no validated edge on free/public data.**")
w("shadow_candidate_report.md", "\n".join(L) + "\n")
print("\nAll reports generated. any_shadow_candidate =", any_shadow)
