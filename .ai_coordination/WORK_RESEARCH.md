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

## Independent focused re-audit of V10.47.23 (2026-07-15)

Verdict: **FAIL** for final scientific certification. The prior one-to-one
pairing defect is repaired: candidate, baseline and pair identities are
bijective per evaluation; invalid identities fail closed; pair IDs are
deterministic; and the 311 published requests reconcile exactly as 4 accepted,
299 impossible and 8 incompatible. The 12 published tournaments remain safely
negative: validation admits zero candidates, shadow has zero candidates,
minimum campaign-corrected p is 1.0, and the holdout remained sealed.

The remaining P1 blocker is campaign-family authority. `matched_random_paired()`
accepts a caller-supplied campaign contract and only checks that contract against
its caller-supplied SHA and internal multiplicities. It does not anchor the
contract to the canonical 4-symbol x 3-timeframe x 47-participant family. A
rehashed one-tournament contract with `m_campaign=47` was therefore accepted and
promoted the same synthetic evidence that correctly fails under the official
`m_campaign=564` contract. The campaign registry also does not bind the supplied
tournament/baseline/matching hashes to the actual tournament entry.

Required next action: `V10_47_24_ANCHOR_CAMPAIGN_REGISTRY_AND_FWER`. Require the
canonical closed campaign identity inside the statistical gate, validate the
exact universe and participant set, bind every tournament/spec/tolerance hash,
reject rehashed reduced families and unrelated hashes, then regenerate all
gates, reports, manifest and seal for another independent Work audit. Full
evidence: `reviews/V10_47_23_WORK_FINAL_REAUDIT.md`.

The operational conclusion is unchanged:
`NO_CONFIRMED_EDGE / SHADOW_CANDIDATES=0 / HOLDOUT=SEALED / NO LIVE`.

## Independent final comprehensive re-audit of V10.47.25 (2026-07-15)

Verdict: **PASS WITH LIMITATIONS** for final scientific closure. No P0, P1 or
material P2 was reproduced. The canonical authority is now anchored to
`V10_47_OFFICIAL_4X3X47`: 4 symbols x 3 timeframes x 47 participants,
`m_campaign=564`, alpha 0.05 and Bonferroni. All 12 real manifests, commitments,
venues and registry/spec/matching hashes authorize; reduced, incomplete,
duplicated, alternate-venue/registry and self-consistent non-canonical campaigns
fail closed.

The 12 primary and 12 replay JSONs at HEAD
`81d8b0b07c93b13a28cca75c220b4def79ac68b1` and tree
`6c0775620c45e28939c23692593a558dbe9f0e16` are byte-identical by pair. Direct
record reconciliation gives 564 hypotheses: 399 without gross edge, 146 killed
by costs and 19 TRAIN net-positive blocks; their 172 requested baseline pairs
reconcile as 3 accepted, 163 impossible and 6 incompatible. Zero hypotheses
complete the baseline gate, zero enter validation/WF and zero reach shadow.
P11/P11_SHORT dependency reuse does not inflate `n_eff` or reduce campaign FWER.

The holdout was not opened and remains physically unloaded in 24/24 outputs.
The certified suite is coherent at 3107 collected, 3107 unique and 3107 passed;
234 focused tests also passed. Independent manifest/seal verification succeeds
with payload `a86e4663d48fbbf4da3a9887b9c4642b6369e559b284af19c19fa6b47e1430aa`
and canonical seal
`de93a0c1d733ed2d2a0e153c9c177a7eedae5164ae951338f765ea13ea92341d`.
Safety remains `SAFE_PAPER_ONLY` with `can_send_real_orders=false`.

Non-blocking limitations: final evidence is local and ignored by Git pending an
external read-only archive; no real dashboard screenshot was taken; full ledger
events are represented by integrity summaries/hashes; two pairing identities
are inferable rather than explicitly serialized; validation faults abort closed
rather than emit a structured rejection. Full evidence and P3 analysis:
`reviews/V10_47_25_WORK_FINAL_COMPREHENSIVE_REAUDIT.md`.

Final closure state:
`NO_CONFIRMED_EDGE / SHADOW_CANDIDATES=0 / HOLDOUT=SEALED / NO LIVE`.

## Revisión de activación operativa (2026-07-15)

Decisión: **C — REPAIR_ONE_SPECIFIC_BLOCKER**. El bloqueo no es un gate de
promoción ni un flag paper demasiado estricto: ninguna política V10.47 está
seleccionada y conectada a un lifecycle forward continuo. El escáner V10.28
activo es deliberadamente no accionable y emite snapshots repetidos sin abrir,
cerrar ni etiquetar posiciones virtuales; el runtime local `app.main` no está
activo. El `PaperTrader` existente tampoco reproduce los exits ni todos los
costes de P11.

El historial paper continuo de dos o tres meses no existe localmente. La
evidencia auditable es: un episodio paper parcial en seis reinicios del 1–2 de
mayo (1.800 evaluaciones, 17 ciclos seleccionados, 11 bloqueos de riesgo, seis
aperturas LONG y cero cierres/labels); 12,52 días transcurridos de escáner estable
en julio (9.337 scans y 27.013 snapshots candidatos repetidos, cero
ejecuciones/outcomes); y readiness V10.29 diferenciado de 12,66 días de trades,
13,62 de orderbook, 15,35 de OI y cero días/frames de liquidaciones. No forman un
único reloj de cobertura densa. Operaciones cerradas, labels y `n_eff` forward de
una política válida: cero. Fixtures, labels smoke de vault, replays y torneos
post hoc se excluyeron del rendimiento forward.

El mejor lead existente es BTCUSDT Bitget 15m P11_SHORT. En TRAIN tuvo 101
señales raw, 35 trades SimOMS en 46 días, `n_eff=20`, gross EV €0,013404, coste
medio €0,008714 y net EV €0,004689. La validation diagnóstica tuvo nueve trades,
neto +€0,0494 y `n_eff=6,1723`, pero neto sin top-3 −€0,1046. Su cobertura de
baseline exacto fue sólo 1/35. Sólo justifica un observer forward-shadow congelado
inmediato, no posiciones paper ni promoción.

Próxima acción requerida: implementar un único observer append-only
`P11_SHORT_BTC_15M_FORWARD_SHADOW` con el decider P11, causal ledger, SimOMS y el
contrato público oficial Bitget 1m→15m; registrar entradas next-open, lifecycle
TP/SL/TIME, MFE/MAE, costes base/conservador y controles preregistrados
no-trade/integridad/placebo bajo un contrato de matching nuevo y certificado;
mostrar su funnel en dashboard; nunca llamar PaperTrader/ExecutionEngine ni
enviar una orden. Cuando la reparación pase sus gates de integridad, el estado
operativo cambia inmediatamente a B/START_FORWARD_SHADOW_NOW; no promociona edge
ni autoriza paper. Evidencia completa en 20 partes, política, hipótesis Warren
MTF, gates, tiempo basado en eventos y criterios de aceptación:
`reviews/OPERATIONAL_ACTIVATION_REVIEW.md`.
