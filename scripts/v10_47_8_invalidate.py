"""V10.47.8 — formally INVALIDATE the flawed V10.47 shadow-candidate evidence.

Conserves all prior outputs as historical (never deletes); adds an explicit
status=INVALIDATED, invalidated_reason=LAST_SIGNAL_CLUSTER_OVERWRITE to the
machine-readable evidence, and prepends a visible banner to the historical
reports. Emits SHADOW_CANDIDATES=0 / NO_CONFIRMED_EDGE / HOLD. Research only."""
import sys, os, json, hashlib, datetime
sys.path.insert(0, r"C:\Users\Adrian\Documents\New project\bitget-ai-trading-bot")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = r"C:\Users\Adrian\Documents\New project\bitget-ai-trading-bot"
V47 = os.path.join(ROOT, "reports", "research", "v10_47_final_edge_search")
V478 = os.path.join(ROOT, "reports", "research", "v10_47_8_scientific_repair")
os.makedirs(V478, exist_ok=True)
UTC = datetime.datetime.now(datetime.timezone.utc).isoformat()
REASON = "LAST_SIGNAL_CLUSTER_OVERWRITE"

INVALIDATED_ARTIFACTS = [
    "shadow_candidate_report.md", "walk_forward_report.md",
    "shadow_candidate_eval.json", "shadow_smoke_result.json",
    "final_report.md", "dashboard_v10_47.html",
    "dashboard_v10_47_report.json", "tournament_summary.json",
]
INVALID_CANDIDATES = [
    {"symbol": "DOGEUSDT", "timeframe": "1m", "policy": "P08_LONG",
     "flawed_net_eur": 0.672646, "causal_net_eur": -0.730039, "flipped": True},
    {"symbol": "XRPUSDT", "timeframe": "1m", "policy": "P08_LONG",
     "flawed_net_eur": 0.310297, "causal_net_eur": -0.557404, "flipped": True},
]


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


banner = (
    f"<!-- ============================================================\n"
    f"STATUS: INVALIDATED  ({UTC})\n"
    f"invalidated_reason: {REASON}\n"
    f"By: V10.47.8 scientific repair. The per-cluster overwrite accounting kept\n"
    f"only the LAST signal of each temporal cluster (ex-post selection). Under\n"
    f"causal first-signal single-position accounting DOGEUSDT 1m and XRPUSDT 1m\n"
    f"P08_LONG both flip from net-positive to net-NEGATIVE. These are NOT shadow\n"
    f"candidates. SHADOW_CANDIDATES=0 · NO_CONFIRMED_EDGE · HOLD. Kept as history.\n"
    f"See reports/research/v10_47_8_scientific_repair/INVALIDATION.md\n"
    f"============================================================ -->\n\n")

manifest = {"status": "INVALIDATED", "invalidated_reason": REASON,
            "timestamp_utc": UTC, "shadow_candidates": 0,
            "verdict": "NO_CONFIRMED_EDGE", "recommendation": "HOLD",
            "invalid_candidates": INVALID_CANDIDATES, "artifacts": {}}

for name in INVALIDATED_ARTIFACTS:
    p = os.path.join(V47, name)
    if not os.path.exists(p):
        manifest["artifacts"][name] = "ABSENT"
        continue
    pre = sha256(p)
    if name.endswith(".json"):
        try:
            data = json.load(open(p, encoding="utf-8"))
        except Exception:
            data = None
        if isinstance(data, dict):
            data["status"] = "INVALIDATED"
            data["invalidated_reason"] = REASON
            data["invalidated_at_utc"] = UTC
            data["shadow_candidates_after_repair"] = 0
            json.dump(data, open(p, "w", encoding="utf-8"), indent=2, default=str)
    elif name.endswith(".md"):
        body = open(p, encoding="utf-8").read()
        if "STATUS: INVALIDATED" not in body:
            open(p, "w", encoding="utf-8").write(banner.replace("<!--", "").
                                                 replace("-->", "") + body)
    elif name.endswith(".html"):
        body = open(p, encoding="utf-8").read()
        if "INVALIDATED" not in body:
            note = ("<div style='background:#a00;color:#fff;padding:10px;"
                    "font-weight:700'>STATUS: INVALIDATED (" + REASON +
                    ") — SHADOW_CANDIDATES=0 · NO_CONFIRMED_EDGE · HOLD. "
                    "See v10_47_8_scientific_repair/INVALIDATION.md</div>")
            body = body.replace("<main>", note + "<main>", 1)
            open(p, "w", encoding="utf-8").write(body)
    manifest["artifacts"][name] = {"sha256_before": pre, "sha256_after": sha256(p)}

json.dump(manifest, open(os.path.join(V478, "invalidation_manifest.json"), "w",
                         encoding="utf-8"), indent=2)

md = [f"# V10.47 RESULTS — FORMAL INVALIDATION ({REASON})", "",
      f"**Timestamp (UTC):** {UTC}", "",
      "## Verdict", "",
      "- **SHADOW_CANDIDATES = 0**", "- **NO_CONFIRMED_EDGE**",
      "- **recommendation = HOLD**", "- can_send_real_orders = false · NO LIVE",
      "", "## Why the V10.47 shadow candidates are invalid", "",
      "The V10.47 tournament accounted trades with `per_cluster[cluster] = "
      "latest_result`. Because a temporal cluster spans many bars, MANY signals "
      "fell in one cluster and each overwrote the previous, so only the LAST "
      "signal per cluster survived — an ex-post selection that systematically "
      "kept the cluster's final (often winning) trade and dropped earlier "
      "losers.", "",
      "Reproduced numerically on the real deciders (see `reproduction_flip.json`, "
      "`logs/reproduction_flip.log`):", "",
      "| Symbol | TF | policy | flawed net (last-signal) | causal net "
      "(first-signal, single position) | flipped |",
      "|---|---|---|---|---|---|"]
for c in INVALID_CANDIDATES:
    md.append(f"| {c['symbol']} | {c['timeframe']} | {c['policy']} | "
              f"+{c['flawed_net_eur']:.4f}€ | {c['causal_net_eur']:.4f}€ | "
              f"{'YES' if c['flipped'] else 'no'} |")
md += ["", "Both candidates flip from net-positive to net-**negative**. They are "
       "NOT shadow candidates.", "",
       "## Scope of invalidation (kept as historical, not deleted)", ""]
for name in INVALIDATED_ARTIFACTS:
    md.append(f"- `reports/research/v10_47_final_edge_search/{name}` — "
              f"{manifest['artifacts'].get(name, 'ABSENT') if isinstance(manifest['artifacts'].get(name), str) else 'marked INVALIDATED'}")
md += ["", "## Reason code", "", f"`invalidated_reason = {REASON}`", "",
       "Related defects repaired in V10.47.8+: timeframe-aware EventClock, causal "
       "trailing, real cluster-aware n_eff, exposure-matched random baseline, "
       "closed registry + multiple-testing correction, proper train/validation/"
       "walk-forward/sealed-holdout separation, P08 truth relabelling, cost "
       "data-truth status. See the v10_47_8 checkpoint and final report."]
open(os.path.join(V478, "INVALIDATION.md"), "w", encoding="utf-8").write(
    "\n".join(md) + "\n")

print("INVALIDATION complete. SHADOW_CANDIDATES=0 NO_CONFIRMED_EDGE HOLD")
print("artifacts:", json.dumps({k: (v if isinstance(v, str) else "INVALIDATED")
                                for k, v in manifest["artifacts"].items()}, indent=2))
