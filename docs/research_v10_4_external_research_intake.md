# ResearchOps V10.4 — External Research Intake

**Status:** research-only · backlog/shadow only · NO LIVE
**Module:** `app/labs/external_research_intake_v10_4.py`
**CLI:** `python -m app.research_lab external-research-intake-v104`

Structured intake for trading ideas coming from outside the validated
pipeline: Perplexity, papers, GitHub repos, humans, the dashboard, Codex/Code
or any other source. The intake **classifies** ideas into a research backlog;
it can never activate anything.

## Idea fields

`source_name, source_type, claim, market, symbols, side, timeframe,
required_features, known_risks, lookahead_risk, overfit_risk,
data_requirements, data_available, backtested, walk_forward_passed,
tradable_on_bitget, validation_plan, promotion_gate, final_status`

## Classification (conservative, rejections first)

| status | meaning |
|---|---|
| `REJECT_LOOKAHEAD_RISK` | the claim needs information not available at decision time |
| `REJECT_OVERFIT_RISK` | high overfit risk (tiny sample, tuned thresholds, story-driven) |
| `REJECT_UNTRADABLE` | not tradable on Bitget perps / no symbols / no side |
| `NEEDS_DATA` | data requirements not yet available (e.g. 180d clean OI) |
| `NEEDS_BACKTEST` | tradable + data available but never backtested |
| `NEEDS_WALK_FORWARD` | backtested but no OOS/walk-forward validation |
| `SHADOW_ELIGIBLE` | passed backtest + walk-forward → may run as shadow (research ceiling) |
| `IDEA_ONLY` | default backlog state |
| `PAPER_CANDIDATE_PENDING_VALIDATION` | backlog label only — still requires full V10.5+ gates |

## Hard rules

- **No external idea can enable the paper filter or live trading.** The
  classification ceiling is `SHADOW_ELIGIBLE`.
- `lookahead_risk`/`overfit_risk` marked high/severe/critical/yes → immediate
  reject; unknown risk does NOT pass as safe.
- The report always returns `paper_ready=false`, `live_ready=false`,
  `paper_filter_enabled=false`, `final_recommendation: NO LIVE`.
- The CLI loads no external ideas by default (no invented data); it prints the
  contract and an empty, auditable backlog.
