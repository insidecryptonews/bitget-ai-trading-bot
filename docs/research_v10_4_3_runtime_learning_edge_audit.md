# ResearchOps V10.4.3 — Runtime Health Audit + Dashboard Truth Fix + Learning/Edge Diagnostic

**Status:** research-only · read-only · NO LIVE
**CLIs:** `runtime-health-audit-v104`, `learning-edge-diagnostic-v104`,
`runtime-efficiency-diagnostic-v104`
**Module:** `app/labs/runtime_audit_v10_4_3.py`

## 1. VPS state (snapshot 2026-06-10, commit 5b03d77)

- **Runtime OK:** bot in tmux, /health fast (~1.5ms), mode=paper,
  open_positions=0, circuit_breaker=false, worker heartbeat, memory ~130MB,
  cycles ok=21 error=0, api 429=0.
- **Dashboard worker lock was FALSE:** /health said `heartbeat/acquired`,
  the dashboard said `blocked_duplicate/YES`. Root cause: `_v104_safety`
  built a NEW `WorkerLockManager`, which gets its own instance_id and always
  loses against the real worker's lock. **Fixed in V10.4.3** — the dashboard
  now reuses the `worker_lock` payload the bot itself publishes in
  `state.payload()` (the exact same source as /health). No recompute.
- **Dashboard safety flags were None for external readers:** the payload only
  had lowercase keys. **Fixed** — `LIVE_TRADING`/`DRY_RUN`/`PAPER_TRADING`/
  `security`/`worker_acquired` aliases added; unknown lock → `unknown/UNKNOWN`,
  never invented.
- **DB light counts failed in the snapshot script** (`AttributeError`): the
  external script used a non-existent API. **Fixed** — `runtime-health-audit-v104`
  counts the 10 research tables through the repo's real `Database._connect()`
  (read-only, fixed whitelist, missing table → `missing`, never raises).
- **ERROR_count=7 in tmux tail:** the visible tail contains no `| ERROR |`
  log lines; the matches are metric strings like `error=0` / `errors=0`
  inside TRAINING PULSE / MFE_MAE blocks → **grep false positive**. Verify on
  VPS with `grep -c "| ERROR |"` (expected 0). No runtime change made — and
  none is justified without evidence.
- **CPU ~31.8%:** consistent with the accumulated CPU time of a continuous
  10-symbol scan every 30s plus MFE/MAE tracking (53min CPU over 167min
  uptime). Not a leak signal. Efficiency CLI reports it as
  `needs_vps_snapshot` from local context and proposes (without applying)
  log-volume reduction only.
- **No edge actionable (honest):** candidate-ranking `NO_VALID_CANDIDATES`;
  every bucket has net_EV<=0 after conservative costs; TIME-death dominates;
  samples below 150; paper filter disabled; heavy panels STALE_OR_PENDING by
  design (CLI-refresh only).
- **Learning alive but gated:** MFE/MAE coverage 100% (matured=148546,
  tracked=675), signals observed (SHORT=3, NO_TRADE=207), labels in window=0,
  top block low_rr. The system observes and learns; the gates correctly
  refuse to act on it.

## 2. What V10.4.3 adds

1. **Dashboard truth fixes** (worker lock single-source-of-truth, flag
   aliases, `worker_acquired`, `edge_focus` panel: "what is blocking edge" +
   "next best research action" — composed from cached dicts only, zero heavy
   work in the polling path).
2. **`runtime-health-audit-v104`** — "is the bot running well right now?":
   best-effort local /health (2s timeout), git commit, safety flags, dashboard
   contract, DB table counts via the real API, verdict
   `OK_RESEARCH_RUNTIME / OK_WITH_WARNINGS / NEEDS_ATTENTION / UNSAFE_STOP`
   (live or can_send_real_orders ⇒ UNSAFE_STOP). Log audit reports
   `NEEDS_RUNTIME_CONTEXT` when no portable log source exists — it never invents.
3. **`learning-edge-diagnostic-v104`** — "is the bot learning and what is
   missing for edge?": learning infra counts, candidate/net-edge status,
   reject-reason histogram, **false-hope warnings** (gross_PF≥2 with
   net_EV≤0; gross_PF=999 no-SL artifact; TIME≥90%; PF on samples<150),
   highest-value next steps and an explicit what-not-to-do list.
4. **`runtime-efficiency-diagnostic-v104`** — read-only findings and
   proposals; `auto_tuning_applied: false` always.

## 3. What NOT to touch yet

live · paper filter · leverage/sizing/slots · runtime signal strategy · new
operational strategies · paid APIs without human verification · OI buckets
(blocked by audit) · backtester operativo (needs 180d+ valid manifest).

## 4. Segments to research WITHOUT promoting (current evidence)

- SHORT in RISK_OFF/TREND_DOWN: signals appear (ETHUSDT score 75) but are
  blocked by low net R:R (1.33–1.34 < 1.40) and net_EV<=0 — investigate exit
  policy / TIME-death, do not relax the gate.
- RANGE regime: gross_PF 5.15 but TIME=99.9% and net_EV=-0.176 → cost-eaten
  illusion; never promote on gross PF.
- ADA/SOL/XRP SHORT buckets: samples exist (240–456) but net_EV negative and
  TIME 80–100% — exit policy research material only.
- gross_PF=999 entries are no-SL-in-sample artifacts, not edge.

## 5. Roadmap to profitability (quantitative, gated)

1. Human verification of Tardis.dev (fallback CoinGlass): pricing, Bitget perp
   180/365d sample, OI/funding/liquidations completeness, license.
2. Acquire 180/365d clean history through the V10.4 acquisition contract
   (staging → manifest + SHA256 + lineage + explicit human authorization →
   atomic promote).
3. Bar-by-bar replay backtester on validated data (no lookahead, worst-case
   same-bar SL/TP, real cost model x1/x2/x3).
4. Edge Hunter V10.5 against the frozen contract: ≥150 samples, net PF≥1.30,
   net EV>0, cost x2 pass, OOS pass, dominance/time-death caps; ceiling
   SHADOW_ONLY.
5. TIME-death reduction + exit-policy calibration on net EV.
6. Walk-forward (monthly + rolling) + anti-overfit score + stability matrix.
7. Shadow → paper (human-gated) → micro-live only after audit. Every step
   reversible; every promotion human-approved.

FINAL_RECOMMENDATION: **NO LIVE**
