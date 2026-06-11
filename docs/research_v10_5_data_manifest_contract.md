# ResearchOps V10.5 — Data Manifest Contract (schema v10.5)

**Status:** contract + offline validator · NO LIVE
**Module:** `app/labs/data_foundation_v10_5.py` (`evaluate_manifest_v105`)
**Builds on:** V10.4 acquisition contract (`external_data_acquisition_plan_v10_4.py`)

Every external dataset MUST ship with one manifest JSON. Without a valid
manifest there is no import. Schema v10.5 fields:

| field | rule |
|---|---|
| source_provider | required; unknown/unlisted providers are treated as paid |
| license_terms | required |
| authorization_reference | non-empty human approval reference |
| explicit_human_authorization | must be exactly `true` for promote |
| paid_download_authorized | exactly `true` for any non-free source |
| requested_range / actual_covered_range | required |
| clean_days | required; <180 ⇒ NEED_LONG_HISTORY |
| symbols / timeframes / data_types | required |
| rows_by_type | required |
| coverage_ratio | <0.80 ⇒ UNDERCOVERAGE_BLOCK (never replaces raw) |
| gap_count | gap ratio >0.05 ⇒ QUALITY_GATE_FAIL |
| duplicate_count | dup ratio >0.02 ⇒ QUALITY_GATE_FAIL |
| missing_oi_ratio / missing_oi_status | unknown/clustered/high or >0.10 ⇒ BLOCK_OI_BUCKETS |
| **missing_funding_ratio** (v10.5) | unknown/invalid or >0.10 ⇒ SERIES_COMPLETENESS_FAIL |
| **missing_liquidations_ratio** (v10.5) | unknown/invalid or >0.10 ⇒ SERIES_COMPLETENESS_FAIL |
| **timezone** (v10.5) | must be `UTC` |
| **timestamp_unit** (v10.5) | must be `unix_ms` or `unix_s` |
| checksums_sha256 | one SHA256 per file; missing ⇒ QUALITY_GATE_FAIL |
| **generated_at** (v10.5) | required |
| **schema_version** (v10.5) | must equal `v10.5` |
| **import_status** (v10.5) | managed by the gate chain; starts `BLOCKED` |
| promote_allowed | **false by default**, always |

## Gate chain (in order)

1. **Schema v10.5 completeness** — any missing field ⇒ `INVALID_MANIFEST_V105`.
2. **All V10.4 gates** (delegated): manifest validity, coverage ≥0.80,
   clean_days ≥180, gap/dup/checksum quality, conservative OI policy, and the
   **explicit human authorization gate** (V10.4.1): no promote without
   `explicit_human_authorization=true` + `license_terms_confirmed=true` +
   non-empty `authorization_reference`, plus `paid_download_authorized=true`
   for any non-free or unknown source.
3. **V10.5 series completeness**: funding/liquidations missing-ratios finite
   and ≤0.10, timezone UTC, timestamp unit unix_ms/unix_s, schema_version
   match.

Only after ALL of that: `status=PROMOTE_ALLOWED_RESEARCH_ONLY`,
`import_status=STAGED_READY_FOR_PROMOTE`, `do_not_replace_raw=false`.
Even then: research-only — never paper_ready, never live_ready.

## Hard rules (unchanged from V10.4.x)

- No manifest ⇒ no import.
- No explicit human authorization ⇒ no promote (even with perfect quality).
- No confirmed license ⇒ no promote.
- Paid/unknown source without `paid_download_authorized=true` ⇒ no promote.
- A free source can be **staged** for inspection, but never promoted without
  the human authorization fields.
- Never replace good raw data with insufficient staging.
- OI buckets stay blocked while the OI audit blocks them.

FINAL_RECOMMENDATION: **NO LIVE**
