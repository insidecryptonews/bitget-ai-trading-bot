# ResearchOps V10.4 — Safe Data Acquisition Plan + Importer Contract

**Status:** research-only · contract/stub only · NO LIVE
**Module:** `app/labs/external_data_acquisition_plan_v10_4.py`
**CLI:** `python -m app.research_lab external-data-acquisition-plan-v104`

This plan defines how 180d/365d historical data will be acquired and imported
**in the future**, safely. V10.4 implements only the contract and a pure
manifest evaluator: it downloads nothing, calls no APIs, needs no keys and
writes nothing.

## Directory layout

| role | path | rule |
|---|---|---|
| staging | `external_data/staging` | new downloads land here, never directly in raw |
| raw immutable | `external_data/raw` | only replaced by an atomic promote that passed ALL gates |
| processed | `external_data/processed` | normalized intermediate output |
| manifests | `external_data/manifests` | one manifest JSON per import (lineage) |
| archive | `external_data/archive` | previous raw snapshot for rollback |

## Manifest (required fields)

`source_provider, license_terms, requested_range, actual_covered_range,
symbols, timeframes, data_types, rows_by_type, missing_oi_ratio,
missing_oi_status, gap_count, duplicate_count, coverage_ratio, clean_days,
checksums_sha256`

Data types covered: OHLCV, open_interest, funding, liquidations,
long_short_ratio (when applicable).

## Importer contract (future, testable now)

- Expected inputs: `perp_market_state.csv|ndjson`, `perp_liquidations.csv|ndjson`.
- Minimum columns per file are defined in `build_importer_contract()`.
- Validations: UTC unix-ms timestamps, Bitget symbol normalization,
  contract/instrument normalization, provider-specific mapping, NaN/Inf
  rejection, duplicate detection, gap detection, missing-OI audit, SHA256 per
  file, coverage vs requested range.
- **Blocks import:** missing columns, invalid/missing manifest,
  `coverage_ratio < 0.80`, checksum mismatch, no paid-download authorization.
- **Research-only allowed:** intermediate history for diagnostics, staging
  inspection without publish.
- Atomic promote: staging → validate → manifest+checksums → archive current
  raw → move processed into raw (only if every gate passes).
- Rollback: restore the archived raw snapshot.
- **Never:** replace good raw with insufficient staging, paid download without
  explicit authorization, DB writes to runtime tables, mutate `.env`/secrets.

## Quality gates (evaluate_acquisition_manifest)

| condition | result |
|---|---|
| missing manifest fields | `INVALID_MANIFEST` — promote blocked |
| `coverage_ratio < 0.80` | `UNDERCOVERAGE_BLOCK` — promote blocked, never replaces raw |
| `clean_days < 180` | `NEED_LONG_HISTORY` — promote blocked |
| gap ratio > 0.05, dup ratio > 0.02, or missing checksums | `QUALITY_GATE_FAIL` |
| all gates pass, `180 ≤ clean_days < 365` | `PROMOTE_ALLOWED_RESEARCH_ONLY` + `INITIAL_VALIDATION_READY` class |
| all gates pass, `clean_days ≥ 365` | `PROMOTE_ALLOWED_RESEARCH_ONLY` + `STRONGER_RESEARCH_READY` class |

OI policy (same conservative rule as V10.3.1): OI status unknown / no audit /
`NEED_MORE_DATA` / clustered / high / moderate, or ratio > 0.10 →
`BLOCK_OI_BUCKETS`. Only audited + low missing OI → `ALLOW_OI_BUCKETS_WITH_CARE`.

No operational backtester without a valid manifest + quality gates. No
paper/live readiness can ever come from this layer.

## How a future API key will be stored (when a provider is chosen)

- **Outside the repo**, in `~/.config/bitget-bot/<provider>.env`.
- `chmod 600` (owner read/write only).
- **Never** in the project `.env`, never committed to GitHub, never printed
  to logs or CLI output, never pasted in chats.
- The future importer will read it via environment loading at runtime only;
  V10.4 code does not read any key from anywhere.
