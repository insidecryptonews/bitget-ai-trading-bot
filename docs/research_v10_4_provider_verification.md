# ResearchOps V10.4 — Provider Manual Verification Layer

**Status:** research-only · read-only · NO LIVE
**Module:** `app/labs/external_provider_verification_v10_4.py`
**CLI:** `python -m app.research_lab external-provider-verification-v104`
**Endpoint:** `GET /api/researchops/v104/provider-verification` (read-only)

## Why this layer exists (post V10.3.1 state)

- Current provider **Coinalyze** delivers only ~63 clean days at 1h (intraday
  retention cap ~1500–2000 datapoints). That is **insufficient** for the 180d
  minimum and the 365d stronger-research target.
- Missing OI is 24.67% and **clustered** → `BLOCK_OI_BUCKETS` (V10.3.1 rule:
  *if OI is not audited, OI buckets stay blocked*).
- V10.3 recommended `tardis_dev` (primary) and `coinglass` (fallback), but
  **neither has been manually verified**. This layer converts that
  recommendation into an auditable checklist so nobody pays for data on a
  guess.

## Provider matrix (verification snapshot)

| provider | status | recommendation | bitget perps | 180d | 365d | cost |
|---|---|---|---|---|---|---|
| coinalyze | CURRENT | current | yes | **no** | **no** | freemium |
| tardis_dev | CANDIDATE | **primary** | yes | yes* | yes* | paid_subscription* |
| coinglass | CANDIDATE | **fallback** | yes | yes* | yes* | NEEDS_MANUAL_VERIFICATION |
| coinapi | NEEDS_MANUAL_VERIFICATION | needs_manual_verification | ? | ? | ? | NEEDS_MANUAL_VERIFICATION |
| kaiko | ENTERPRISE_ONLY | enterprise_gated | ? | yes* | yes* | enterprise |
| ccdata_cryptocompare | NEEDS_MANUAL_VERIFICATION | needs_manual_verification | ? | ? | ? | NEEDS_MANUAL_VERIFICATION |
| bitget_official | CANDIDATE | cross_check | yes | ? | ? | free |
| binance_okx_proxy | PROXY_ONLY | proxy_only | **no** (proxy) | yes | yes | free |

`*` = claimed by vendor docs/marketing, **not yet verified by us**. `?` =
unknown → `NEEDS_MANUAL_VERIFICATION`. We never invent pricing, limits, exact
history depth or coverage.

## Manual checklist before paying for ANY provider

For every provider with pending verification the report lists these checks:

1. `verify_pricing` — real price for the tier we need, not the marketing page.
2. `verify_rate_limits` — requests/min and monthly caps for bulk backfill.
3. `verify_bitget_perp_history_depth_180d` — ask for a sample covering 180d of
   **Bitget** perps (not Binance) at 1h.
4. `verify_bitget_perp_history_depth_365d` — same for 365d.
5. `verify_oi_completeness` — missing-OI ratio on the sample must be auditable.
6. `verify_funding_completeness` — funding series without gaps.
7. `verify_liquidations_completeness` — liquidation events coverage.
8. `verify_license_and_terms` — redistribution/retention terms, research use.
9. `verify_data_model_compatibility` — maps onto `perp_market_state` /
   `perp_liquidations` minimum columns.
10. `verify_vendor_lock_in_risk` — bulk export available? exit cost?

## Hard rules

- `paid_download_authorized` is **always false** in this layer. Only an
  explicit human authorization (outside this code) can change the plan.
- Provider verification alone can never produce `paper_ready` or
  `live_ready`. Output ends with `final_recommendation: NO LIVE`.
- No API keys are requested, read, stored or printed by V10.4. See
  `research_v10_4_data_acquisition_plan.md` for the future safe key storage
  procedure.
