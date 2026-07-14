"""V10.47.8 — build the repaired-state dashboard at the FINAL HEAD. Pure render
from the consolidated causal evidence. Shows NO_CONFIRMED_EDGE, SHADOW=0, the
V10.47 INVALIDATION, causal signal accounting, n_eff, matched baseline, P08 proxy,
data/cost truth, deterministic INSUFFICIENT_DATA and the hub NEXT_ACTION. It does
NOT show the old DOGE/XRP as active candidates. Research only, NO LIVE."""
import sys, os, json, subprocess, html
sys.path.insert(0, r"C:\Users\Adrian\Documents\New project\bitget-ai-trading-bot")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = r"C:\Users\Adrian\Documents\New project\bitget-ai-trading-bot"
OUT = os.path.join(ROOT, "reports", "research", "v10_47_8_scientific_repair")
os.makedirs(os.path.join(OUT, "dashboard"), exist_ok=True)


def git(*a):
    try:
        return subprocess.check_output(["git", *a], cwd=ROOT).decode().strip()
    except Exception:
        return "?"


agg = json.load(open(os.path.join(OUT, "causal_aggregate.json"), encoding="utf-8"))
rep = json.load(open(os.path.join(OUT, "reproduction_flip.json"), encoding="utf-8"))
det = json.load(open(os.path.join(OUT, "det_strategies_result.json"), encoding="utf-8"))
head, tree = git("rev-parse", "HEAD"), git("rev-parse", "HEAD^{tree}")
branch = git("branch", "--show-current")
na = ""
try:
    for l in open(os.path.join(ROOT, ".ai_coordination", "NEXT_ACTION.md"),
                  encoding="utf-8"):
        if "NEXT:" in l:
            na = l.strip("- []\n")
            break
except Exception:
    pass
E = html.escape


def kv(k, v):
    return f"<div class='kv'><span class='k'>{E(str(k))}</span><span class='v'>{E(str(v))}</span></div>"


def sec(t, body):
    return f"<section><h2>{E(t)}</h2>{body}</section>"


flips = "".join(
    f"<tr><td>{E(r['symbol'])} P08_LONG</td>"
    f"<td>+{r['flawed_last_signal_per_cluster']['net_eur']:.4f}€</td>"
    f"<td>{r['causal_first_signal_single_position']['net_eur']:.4f}€</td>"
    f"<td>{r['net_flipped_sign']}</td></tr>" for r in rep["reproduction"])

overview = (
    kv("Result", "SCIENTIFIC REPAIR COMPLETE — NO CONFIRMED EDGE") +
    kv("SHADOW_CANDIDATES", len(agg["shadow_candidates"])) +
    kv("Verdict", "NO_CONFIRMED_EDGE · HOLD") +
    kv("can_send_real_orders", "false") + kv("LIVE_TRADING", "False") +
    kv("FINAL_RECOMMENDATION", "NO LIVE") +
    kv("branch", branch) + kv("HEAD", head) + kv("tree", tree) +
    kv("manifest+seal", "manifests/output_manifest.json (SHA-256, dirty=false)"))

invalid = (
    "<div class='bad'>V10.47 DOGE/XRP 1m P08_LONG = INVALIDATED "
    "(LAST_SIGNAL_CLUSTER_OVERWRITE) — not active candidates.</div>"
    "<table><tr><th>candidate</th><th>flawed net</th><th>causal net</th>"
    "<th>flipped</th></tr>" + flips + "</table>")

tourn = (
    kv("combos", agg["combos"]) +
    kv("participant-runs nominal", agg["participant_runs_nominal"]) +
    kv("classes", agg["classes"]) +
    kv("SHADOW_CANDIDATES", len(agg["shadow_candidates"])) +
    kv("holdout sealed (all combos)", agg["holdout_all_sealed"]))

accounting = (
    kv("n_signals_raw", f"{agg['signals_raw']:,}") +
    kv("n_signals_eligible", f"{agg['signals_eligible']:,}") +
    kv("n_executed", f"{agg['executed']:,}") +
    kv("skipped POSITION_ALREADY_OPEN", f"{agg['skipped_position_open']:,}") +
    kv("skipped CLUSTER_COOLDOWN", f"{agg['skipped_cluster_cooldown']:,}") +
    kv("n_trades", f"{agg['trades']:,}"))

truth = (
    kv("P08 executed", "P08_FUNDING_HOUR_RETURN_REVERSAL_PROXY (PROXY)") +
    kv("uses_real_oi / funding", "False / False") +
    kv("costs", "MODELLED (fixed bps); OI & L2 book UNAVAILABLE") +
    kv("n_eff", "conservative min(event/cluster/session/temporal/acf)") +
    kv("random baseline", "exposure-matched + corrected p-value + bootstrap LB"))

det_rows = "".join(
    f"<tr><td>{E(r['symbol'])}</td><td>{E(r['timeframe'])}</td>"
    f"<td>{E(r['strategy'])}</td><td>{E(r['data_status'])}</td>"
    f"<td>{r['n_executed']}</td><td>{r['net_eur']:+.4f}€</td></tr>"
    for r in det["smoke"])
detsec = ("<div>IMPLEMENTATION_STATUS=COMPLETE · "
          "SCIENTIFIC_EVALUATION=INSUFFICIENT_DATA (~90d < 2y). Smoke only.</div>"
          "<table><tr><th>sym</th><th>tf</th><th>strategy</th><th>data</th>"
          "<th>exec</th><th>net</th></tr>" + det_rows + "</table>")

body = (sec("Overview", overview) + sec("V10.47 Invalidation", invalid) +
        sec("Causal Tournament (12 combos)", tourn) +
        sec("Signal Accounting (causal, single-position)", accounting) +
        sec("Data / Cost / P08 Truth", truth) +
        sec("Deterministic 1h/4h", detsec) +
        sec("Hub NEXT_ACTION", kv("NEXT", na)) +
        f"<div class='verdict'>SCIENTIFIC REPAIR COMPLETE — NO CONFIRMED EDGE · "
        f"SHADOW_CANDIDATES=0 · NO LIVE</div>")

css = """*{box-sizing:border-box}body{margin:0;font:14px/1.5 system-ui,sans-serif}
:root{--bg:#0f1117;--fg:#e6e6e6;--mut:#8a90a2;--acc:#4da3ff;--card:#171a23;--bad:#ff6b6b}
@media(prefers-color-scheme:light){:root{--bg:#f6f7f9;--fg:#1a1d24;--mut:#5b6270;--card:#fff}}
:root[data-theme=dark]{--bg:#0f1117;--fg:#e6e6e6;--card:#171a23}
:root[data-theme=light]{--bg:#f6f7f9;--fg:#1a1d24;--card:#fff}
body{background:var(--bg);color:var(--fg)}
header{padding:16px 20px;background:var(--card);border-bottom:1px solid #0003}
header h1{margin:0;font-size:17px}.banner{color:#3ecf8e;font-weight:600;font-size:13px;margin-top:6px}
main{padding:16px 20px;display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:14px;max-width:1400px}
section{background:var(--card);border:1px solid #0002;border-radius:10px;padding:14px 16px;overflow-x:auto}
section h2{margin:0 0 10px;font-size:13px;color:var(--acc);text-transform:uppercase;letter-spacing:.05em}
.kv{display:flex;justify-content:space-between;gap:10px;padding:3px 0;border-bottom:1px dashed #0001}
.k{color:var(--mut)}.v{text-align:right;font-variant-numeric:tabular-nums}
.bad{color:var(--bad);font-weight:600;margin-bottom:8px}
table{width:100%;border-collapse:collapse;font-size:12px}th,td{padding:4px 6px;text-align:right;border-bottom:1px solid #0001}
th:first-child,td:first-child{text-align:left}
.verdict{grid-column:1/-1;background:var(--card);border:1px solid var(--acc);border-radius:10px;padding:14px;font-weight:700}"""

htmlout = (f"<!doctype html><html><head><meta charset='utf-8'>"
           f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
           f"<title>V10.47.8 Scientific Repair Dashboard</title><style>{css}</style>"
           f"</head><body><header><h1>Bitget AI Trading Bot — V10.47.8 Scientific "
           f"Repair</h1><div class='banner'>REPLAY/SIMULATION RESEARCH ONLY · NO "
           f"LIVE · can_send_real_orders=false · NO_CONFIRMED_EDGE · "
           f"SHADOW_CANDIDATES=0</div></header><main>{body}</main></body></html>")
p = os.path.join(OUT, "dashboard", "dashboard_v10_47_8.html")
with open(p, "w", encoding="utf-8") as fh:
    fh.write(htmlout)
print("dashboard written:", p, f"({len(htmlout)} bytes)")
