# Continuous Edge Research Challenger

The Challenger is an independent, bounded research process over verified causal
Storage Efficiency V2 features. It cannot edit ATI, P11, Cross-Venue, paper
accounts, active policies or execution settings.

## Research contract

- Inputs are SHA-verified feature Parquet files only.
- Signals use data available by each row's causal cutoff; entry is the next
  bucket and exits are later buckets.
- Gapped paths are skipped.
- Outcome, label, MFE/MAE, future-return, PnL and barrier fields are prohibited
  as features.
- The chronological split is 60% train, 20% validation and a sealed 20% holdout.
  Outcomes cannot cross a boundary and an embargo bucket separates train and
  validation.
- The holdout has no evaluation path: access count remains zero.
- Search is preregistered and bounded to five families, 80 trials and 30 minutes.
- Costs are tested at 14.5, 15.5 and 18 bps. Effective sample size accounts for
  overlap and lag-1 dependence. Moving-block bootstrap, Bonferroni correction,
  fixed-spec rolling stability and exposure-matched controls are reported.

## Families

The initial families cover extreme flow, temporal venue consensus, order-flow
precursors, leader persistence and isolated SHORT trend-down research. This is
not permission to change any active strategy.

## States and promotion ceiling

Automatic output is limited to `REJECTED`, `NEED_MORE_DATA` or `WATCH_ONLY`.
`WATCH_ONLY` is not a paper candidate and does not unlock the holdout. Promotion,
paper-filter activation and policy replacement are always disabled. Reports may
be produced only after enough new verified partitions exist and the scheduler's
resource/health guards pass.

```powershell
python -m app.research_lab continuous-edge-challenger-v2 --symbols BTCUSDT
```

The dashboard and HTTP API read only the latest status artifact; they never start
the Challenger or any heavy job.

No result is an edge claim. Current strategy verdict remains
`NINGUN EDGE NUEVO VALIDADO` until strict out-of-sample evidence says otherwise.

`PAPER_TRADING=True`, `LIVE_TRADING=False`, `DRY_RUN=True`, paper filter disabled,
`can_send_real_orders=false`, `FINAL_RECOMMENDATION=NO LIVE`.
