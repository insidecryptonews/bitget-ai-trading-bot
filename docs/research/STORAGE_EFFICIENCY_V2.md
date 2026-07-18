# Storage Efficiency V2

Storage Efficiency V2 reduces local research-data growth without weakening the
append-only audit source. It is local, offline with respect to exchanges, and
has no execution capability.

## Layers

- **HOT:** one append-only `current.jsonl` per venue plus raw audit frames. The
  normalized stream rotates at a configured size only after its consumer has
  acknowledged every byte. Raw audit files rotate separately and are retained.
- **WARM:** a low-priority worker compresses new closed normalized segments with
  Zstandard level 1 (selected by the 64/128/256/512 MB local benchmark) and
  validates row count and logical SHA-256 before replacing the derived
  uncompressed segment. Existing verified gzip segments remain readable. Closed raw JSONL may use transparent NTFS
  compression, which does not change logical bytes or paths.
- **ANALYTICS:** verified warm segments become partitioned Parquet with Zstandard.
  `source_json` and `source_row_index` preserve exact reconstruction and order.
- **FEATURE STORE:** causal aggregates are materialized incrementally from verified
  Parquet. Every row records its source partition, dataset hash, feature version,
  event interval and causal cutoff.

## Safety contract

The default and only accepted mode is `COMPRESSION_ONLY_NO_DELETE`.

- `NO_DELETE_WITHOUT_VERIFIED_REMOTE_BACKUP=true`.
- `r2_verified=false` means raw deletion remains blocked.
- No exchange connection, API key, database, order path, policy, sizing, margin,
  leverage or slot is used or changed.
- Failed compression retains the source and records an error for bounded retry.
- Derived writes stop below the configured free-disk guard; collectors keep priority.

## Commands

```powershell
python -m app.research_lab storage-efficiency-status-v2
python -m app.research_lab storage-efficiency-cycle-v2
python -m app.research_lab storage-efficiency-cycle-v2 --apply
python -m app.research_lab storage-efficiency-benchmark-v2 --source-file <closed.jsonl>
```

The five-minute scheduler is `scripts/run_storage_edge_scheduler.ps1`. It runs
compression before analytics, and runs the Challenger only when data, health,
disk, interval and heavy-job guards all pass.

## Recovery and validation

Rollover journals make interrupted normalized/raw rotations recoverable. Manifests
carry state, hashes, counts and source paths. Verification requires complete JSONL
lines, exact gzip round-trip, Parquet semantic fingerprints and immutable source
retention. No generated raw, Parquet, feature, runtime or report artifact belongs
in Git.

`PAPER_TRADING=True`, `LIVE_TRADING=False`, `DRY_RUN=True`, paper filter disabled,
`can_send_real_orders=false`, `FINAL_RECOMMENDATION=NO LIVE`.
