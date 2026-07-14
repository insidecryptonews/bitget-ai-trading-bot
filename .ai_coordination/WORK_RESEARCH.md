# WORK — RESEARCH NOTES

Findings that drove this repair (all reproduced before fixing):
1. Fixed 1-minute EventClock step for all timeframes.
2. per_cluster overwrite → last signal per cluster selected ex post.
3. Multiple concurrent signals per cluster silently executed.
4. Unmatched random baseline (count/exposure mismatch).
5. Post-selection "OOS" mislabeled as out-of-sample.
6. n_eff == trade count (ignored dependence).
7. Ambiguous intrabar trailing sequence.
8. P08 proxy mislabeled as OI/Funding.
9. "observed" costs were fixed bps tables.

Falsification for any future candidate: it must fail if it cannot beat an
exposure-matched random baseline on a strictly-later validation window.

## Independent final audit of V10.47.14 (2026-07-14)

Verdict: **FAIL** for the claim `SCIENTIFIC REPAIR COMPLETE`; the conservative
operational conclusion remains **SHADOW_CANDIDATES=0 / NO_CONFIRMED_EDGE / NO
LIVE**. The DOGE/XRP sign flip, causal ledger accounting and 12 tournament
totals reproduce, but the final certification has material gaps:

1. `VALIDATION` is defined but never evaluated. The runner precomputes signals
   over the full dataset (including the nominal holdout) and returns the
   hard-coded flag `holdout_touched=False`; this is not a sealed holdout.
2. The matched random null preserves count, aggregate side mix, cluster,
   exposure and cost parameters, but not realised holding/censoring or the
   single-position path. Its lower bound is versus zero, not a paired
   candidate-minus-random lower bound.
3. The deterministic implementations do not implement the preregistered 4h
   regime + 1h pullback linkage, dynamic 2-ATR stop, or trailing from 1R;
   `DET_EXIT` uses fixed 2%/6%/2% fractions.
4. The output manifest is stale (`progress_checkpoint.md` SHA-256 mismatch), so
   the actual-files seal does not match the declared seal. The seal covers only
   output path/hash pairs and does not bind commit/tree/dataset/spec provenance.
5. The suite log is real and records 2896 passing invocations, but collection
   has 2895 unique nodeids: `%2E%2E` and `%2e%2e` collide under the same pytest
   nodeid. The 29 new tests independently pass.
6. `bars_to_events()` still defaults to a 1-minute close step even when its
   `timeframe` argument is 5m/15m/1h/4h unless the caller supplies an interval.

Full evidence, severity, matrix, reproductions and required next action:
`reviews/V10_47_14_WORK_FINAL_AUDIT.md`.

## Focused re-audit of V10.47.15–18 (2026-07-14)

Verdict: **FAIL**. The repair adds real improvements, but does not close the
certification contract under adversarial execution:

1. VALIDATION has its own metrics and rejects candidates, but WALK_FORWARD is
   still executed for validation failures instead of receiving admitted
   candidates only.
2. `SealedHoldout` is an in-memory wrapper over `bars[hstart:]`, not a separate
   physical loader/path. `_bars` is directly readable, any neutral caller can
   self-authorize with an arbitrary string, and commitment paths allow traversal.
3. `matched_random_paired` accepts `match_status=OK` despite deliberately
   mismatched holding, session, day, censoring, notional, funding and regime. It
   returns no candidate/baseline trade IDs or explicit pairs, and applies no
   multiple-testing correction.
4. The regular 4h→1h mapping is causal, but an incomplete 4h candle is published
   as ready and the twelve-tournament registry contains no deterministic MTF
   participant.
5. LONG/SHORT 2-ATR stops are numerically exact and reach SimOMS; ATR and the
   initial stop are not stored in the append-only ledger records.
6. The final manifest is stale again (`progress_checkpoint.md` changed after
   sealing). Its verifier does not compare current Git or re-hash external
   datasets/specs/registry, excludes collection `.txt`, audits and hub, and two
   identical rebuilds produce different payload/seal due to `generated_utc`.
7. The unique collection itself is repaired: 2912 invocations, 2912 unique
   nodeids, zero duplicates. The execution log lacks HEAD/tree and its manifest
   certification is invalid because the manifest does not verify.

The twelve regenerated outputs still support the conservative result
`SHADOW_CANDIDATES=0 / NO_CONFIRMED_EDGE / NO LIVE`, but they do not pass final
certification. Full evidence: `reviews/V10_47_18_WORK_REAUDIT.md`.

## Independent final re-audit of V10.47.22 (2026-07-14)

Verdict: **FAIL** for final scientific certification. Most of the repair now
passes: validation short-circuits WF, the holdout remained sealed/unopened,
MTF and ATR/ledger behavior are causal, 12/12 tournaments and 5/5 MTF outputs
reproduce, the certified 3040-test evidence is coherent, and operational safety
remains `SAFE_PAPER_ONLY`.

One P1 blocker was independently reproduced in
`app/labs/v10_46/causal_stats.py`: `matched_random_paired()` consumes baseline
IDs but does not enforce unique candidate IDs. Twelve rows with the same
`candidate_trade_id` were accepted as twelve exact pairs and passed the real
Bonferroni gate (`m_global=47`, corrected p=0.0114746094,
`beats_matched_random=true`). The declared exact one-to-one baseline contract is
therefore false for adversarial input.

The published outputs are not contaminated by that defect: their four actual
OK pairs have unique IDs, 299 requests are impossible, eight are incompatible,
the minimum corrected p-value is 1.0, validation admits zero candidates, and
there are zero shadow candidates. The conservative conclusion remains:
`NO_CONFIRMED_EDGE / SHADOW_CANDIDATES=0 / HOLDOUT=SEALED / NO LIVE`.

Required next action: V10.47.23 must enforce candidate/baseline/pair ID
uniqueness before statistics, fail closed on duplicates, add a production-scale
`m_global=47` adversarial regression, and define the multiple-testing family
across the 12-tournament campaign before any future promotion. Full evidence:
`reviews/V10_47_22_WORK_FINAL_REAUDIT.md`.
