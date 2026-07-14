# V10.47.23 Work final focused re-audit

Date: 2026-07-15

Role: independent scientific, statistical and adversarial auditor.

## 1. Executive verdict

**GLOBAL VERDICT: FAIL**

V10.47.23 repairs the P1 one-to-one pairing defect reported in V10.47.22.
Candidate, baseline and pair identities are enforced per evaluation; invalid
input fails closed; pair IDs are deterministic; and the published 311 requests
reconcile exactly.

Final scientific certification still fails because the campaign registry is
self-authorized by the caller. The statistical gate verifies a supplied
campaign contract against a supplied SHA, but does not verify that it is the
canonical preregistered 4-symbol x 3-timeframe x 47-participant family. A
rehashed reduced family can lower `m_campaign` from 564 to 47 and turn the same
synthetic evidence from rejected to promoted.

This blocker does not contaminate the current published conclusion. All 12
official outputs used `m_campaign=564`, have minimum campaign-corrected p=1.0,
admit no validation candidates and create no shadow candidates. The correct
state remains:

```
NO_CONFIRMED_EDGE
SHADOW_CANDIDATES=0
HOLDOUT=SEALED
FINAL_RECOMMENDATION=NO LIVE
```

## 2. Scope and provenance

Audited repository identity before any authorized audit write:

| Field | Value |
|---|---|
| Branch | `local-v10-47-8-scientific-repair` |
| HEAD | `c4d4247e6d2f5f3e57114931f44aa4e49d3c9cbf` |
| Tree | `62ee153f17365224247b41954150564d1777d98c` |
| origin/main | `adc7b9c47ed2390eddaf80436287172455bb32d8` |
| Current branch upstream | none |

The tracked worktree was clean before the audit. The only pre-existing
untracked files were:

* `CODEX_RESULT.md`
* `CODE_RESULT.md`
* `docs/research/LOCAL_AI_RESEARCH_ASSISTANT_FEASIBILITY_V10_40.md`

No source, dataset, output, manifest, seal, dashboard, configuration or Git
state was modified. The real holdout was not opened. After completing all
pre-write checks, only this report and `.ai_coordination/WORK_RESEARCH.md` were
written, as explicitly authorized.

## 3. Individual verdicts

| Area | Verdict | Evidence |
|---|---|---|
| Candidate uniqueness | PASS | Duplicate candidate IDs invalidate the entire evaluation before statistics. |
| Baseline uniqueness | PASS | Duplicate baseline IDs invalidate the entire evaluation before statistics. |
| Pair uniqueness | PASS | Duplicate pair IDs and cardinality violations fail closed. |
| Deterministic pair ID | PASS | Canonical JSON plus SHA-256 over all seven required identity fields. |
| Reconciliation of 311 | PASS | 4 accepted + 299 impossible + 8 incompatible; no overlap or unclassified rows. |
| Cross-hypothesis overlap | PASS WITH LIMITATIONS | No within-test inflation; nominal campaign m remains 564. Global dependency metadata is absent. |
| Campaign registry | FAIL | A reduced, rehashed, caller-supplied family is accepted. |
| Campaign FWER | FAIL | The formula is correct but its multiplicity authority is not anchored. |
| Twelve tournaments | PASS | All 12 are current, negative and holdout-sealed. |
| Tests | PASS WITH LIMITATIONS | Executed tests pass, but no test rejects a self-consistent reduced campaign registry. |
| Manifest and seal | PASS pre-write | Real-state verifier, independent hashes and mutation tests passed before authorized audit writes. |
| Safety | PASS | `SAFE_PAPER_ONLY`; real-order path unreachable in current configuration. |
| Coordination hub | PASS | `COHERENT`; one expected NEXT_ACTION; no prior scientific PASS. |

## 4. Findings by severity

### P0

None.

### P1 - Campaign registry is self-signed, not canonically anchored

Files and lines:

* `app/labs/v10_46/causal_stats.py:306-396`
* `app/labs/v10_46/causal_stats.py:427-459`
* `app/labs/v10_46/causal_stats.py:694-703`
* `app/labs/v10_46/causal_tournament.py:178-231`

`_campaign_registry_problems()` checks the supplied contract against the
supplied SHA and checks internal multiplicity, alpha and closure fields. It does
not compare that contract or SHA with the canonical result of
`preregister_campaign()`. It also does not require the exact four symbols,
three timeframes, twelve combinations or 47 participants per tournament.

The safe synthetic attack already reproduced during this audit used eleven
positive exact pairs:

| Contract | p_raw | p_campaign_corrected | promotion_allowed |
|---|---:|---:|---|
| Official family, m=564 | 0.0004882812 | 0.2753906250 | false |
| Rehashed reduced family, m=47 | 0.0004882812 | 0.0229492188 | true |

The counterfeit contract was reported `pairing_status=VALID`. This falsifies
the claim that campaign multiplicity cannot be freely reduced by the caller.
The Bonferroni formula itself is correct; its family authority is not.

Impact: a future caller can obtain a false baseline promotion by presenting a
self-consistent reduced family. This blocks scientific certification and any
promotion status, even though the current published outputs remain negative.

### P2 - Campaign entries are not bound to actual tournament identities/specs

Files and lines:

* `app/labs/v10_46/causal_tournament.py:183-195`
* `app/labs/v10_46/causal_tournament.py:201-219`
* `app/labs/v10_46/causal_tournament.py:502-503`
* `app/labs/v10_46/causal_tournament.py:341-350`
* `app/labs/v10_46/causal_stats.py:460-469`

The campaign preregistration creates per-tournament registries with the
synthetic generation ID `v10_47_23_campaign_registry`; actual tournaments call
`preregister()` with their real generation ID. The `specs_hash` agrees in all
12 outputs, but the campaign entry `registry_hash` agrees with the actual
tournament registry in 0/12 outputs.

The statistical gate only format-checks `baseline_spec_hash` and
`registry_hash` as hashes. Safe tests with unrelated 64-hex strings remained
`pairing_status=VALID`. The campaign contract does not explicitly bind the
matching spec, baseline spec and tolerances used by each actual tournament.

Impact: even after fixing campaign size, provenance can be internally
well-formed without proving that the evaluated tournament is the preregistered
member of that campaign.

### P2 - Regression coverage misses the self-consistent reduction attack

File and lines:

* `tests/test_researchops_v10_47_23_bijective_pairing_campaign.py:254-420`

The tests correctly reject a mutation when the old SHA is retained, reject
missing SHA, reject `m_campaign < m_tournament`, and verify that 564 is more
conservative than a local family. They do not remove a symbol/timeframe or
participants, recompute the SHA and require rejection. The helper itself can
create arbitrary self-consistent campaign sizes.

### P3 - Cross-hypothesis dependency is conservative but under-described

Four accepted pair records contain three global candidate/baseline/pair
identities because the same ETH trade occurs in both P11 and P11_SHORT. It does
not appear twice inside one statistical test, does not increase any single
test's `n`, and does not reduce nominal `m_campaign=564`. It is therefore not a
current pseudoreplication failure.

Future evidence should carry `global_event_id` or `dependency_cluster_id` so
cross-hypothesis reuse is visible without reconstructing it after the fact.

### P3 - Two terminal generation logs are outside sealed coverage

`logs/evidence_generation.log` and `logs/manifest_build.log` are not among the
164 covered files. Core certified collection/execution, compile, security,
targeted-test, regeneration and audit evidence is covered, so this is not a
current claim-integrity blocker. The uncovered logs must not be cited as sealed
scientific evidence.

## 5. Pairing attacks and invariants

The following defensive synthetic attacks were executed or covered by the
targeted rerun:

* candidate ID C1 repeated twelve times with twelve distinct baselines;
* repeated baseline ID;
* repeated pair ID;
* identical repeated row;
* candidate reused across two baselines;
* baseline reused across two candidates;
* missing, empty, non-string, whitespace and ambiguous IDs;
* incompatible required matching field;
* input order reversal;
* caller-supplied pair ID with internally changed fields.

The former red attack now returns:

* `pairing_status=INVALID`;
* `DUPLICATE_CANDIDATE_TRADE_ID` count 11;
* `pairs_accepted=0`;
* `p_raw=None`;
* `baseline_gate=false`;
* `beats_matched_random=false`;
* `promotion_allowed=false`.

Reversing input rows preserves the invalid integrity result. A valid 4x4 input
accepts exactly four pairs with four candidate IDs, four baseline IDs and four
pair IDs. There is no silent deduplication or retrospective match selection.

Relevant implementation:

* `app/labs/v10_46/causal_stats.py:227-242` - deterministic pair identity;
* `app/labs/v10_46/causal_stats.py:244-281` - pairing registry;
* `app/labs/v10_46/causal_stats.py:283-304` - preflight identity inventory;
* `app/labs/v10_46/causal_stats.py:444-469` - fail-closed preflight;
* `app/labs/v10_46/causal_stats.py:684-714` - deltas and cardinality gate.

The independently recomputed canonical pair hash matched exactly:

`96a69cd814deea810aec83c3b52c6ea13adaf7f3d8b96d1606a234755eebc7c8`

Changing any required pair identity field changed the SHA-256.

## 6. Direct reconciliation of published records

The audit derived categories from every pairing record rather than accepting
the generated summary:

| Measure | Recomputed |
|---|---:|
| Tournament files | 12 |
| TRAIN-positive gate blocks | 21 |
| Pair requests | 311 |
| Accepted exact pairs | 4 |
| Impossible/no unique baseline | 299 |
| Incompatible | 8 |
| Invalid/duplicate IDs | 0 |
| Unclassified rows | 0 |
| Category overlaps | 0 |

Only the four accepted pairs enter paired deltas, bootstrap and the sign test.
The 299 impossible rows are not converted to zero deltas and do not increment
`n`; the eight incompatible rows do not enter statistics. Coverage uses
accepted/requested per preregistered gate block.

There are 311 requests because 21 TRAIN-positive policies emit all candidate
requests before exact baseline reconciliation. The single preregistered random
realization provides an exact compatible opportunity for only four of them.
All four accepted deltas are exactly 0.0.

## 7. Campaign and twelve-tournament evidence

The official generated campaign registry is internally coherent:

* symbols: BTCUSDT, DOGEUSDT, ETHUSDT, XRPUSDT;
* timeframes: 1m, 5m, 15m;
* combinations: 12;
* participants per tournament: 47;
* nominal hypotheses: 564;
* semantic hypotheses: 564;
* diagnostic unique results: 540;
* effective gate multiplicity: 564;
* method: Bonferroni;
* alpha: 0.05;
* closed and closed-before-metrics: true;
* campaign SHA: `a197f2558c7bacbe394afb747499bbaace07ec90315ae0d4a6d76ed3d261c481`.

The same campaign SHA appears in all twelve outputs. Ambiguous semantic overlap
does not reduce the gate multiplicity.

Direct output reconciliation confirms:

* 12/12 tournaments complete;
* TRAIN-positive policies: 21;
* validation admitted: 0;
* validation rejected: 21;
* shadow candidates: 0;
* walk-forward calls after validation failure: 0;
* baseline gates passed: 0;
* promotion allowed: 0;
* minimum reported campaign-corrected p: 1.0;
* `holdout_data_loaded=false` in all twelve outputs;
* HEAD/tree correspond to the audited commit;
* policy parameters were not relaxed to recover significance.

Thus the current negative result is supported. The defect only prevents a
future positive result from being certified until campaign authority is fixed.

## 8. Tests and certified execution evidence

### Rerun during this independent audit

1. V10.47.23 bijective pairing and campaign tests:
   `33 passed in 3.68s`, exit 0.
2. Real-state manifest/seal adversarial tests:
   `42 passed in 41.71s`, exit 0.

Both reruns used isolated temporary directories, disabled pytest cache and
created no repository artifacts.

### Certified full-suite evidence verified, not rerun

| Field | Value |
|---|---|
| collected | 3073 |
| unique nodeids | 3073 |
| duplicates | 0 |
| passed | 3073 |
| failed/skipped/xfailed/xpassed/deselected | 0/0/0/0/0 |
| pytest duration | 579.66s |
| wrapper duration | 582.442108s |
| exit code | 0 |
| execution log SHA | `ce13bc2c2dca0adc817ff3e417d6fb88fa5555d43d2cddea30f608b2d77dc9be` |
| nodeids SHA | `6a130d88aafaf06ceeeb4c69e42091066aa8b226b2e988bb6843c55c93d38bb6` |
| collection record SHA | `873f9e6d54c4d0066eca4740ec4b457f5e2fd948fd75da6580714cb93471016f` |
| execution record file SHA | `27b7e3d3d9e5251d9d7716f0b7126cda46e9326b75a79d00068da221e5f02428` |

The execution record binds the correct branch, HEAD and tree. The compileall
artifact was inspected and is sealed with SHA-256
`b8d94f564571596f3d97cbc8ab823dcbb08fc5473e191e03272f8ed9590f6f45`.

Green tests do not neutralize the P1 finding because the missing adversarial
case is precisely a reduced contract with a recomputed valid SHA.

## 9. Manifest, seal and dashboard

Before either authorized audit file was written, the real manifest and seal
verified successfully:

* covered categories: 15/15;
* covered files: 164;
* size/hash discrepancies: 0;
* payload SHA:
  `f34243a536662f66394d0174623e4bf1d1f3b49c680b220a511670e1ae4d95de`;
* seal SHA:
  `6acd95b93eae0f85428006080aa449e50adf974752ba70578d640e2789d20eb6`;
* verifier: `ok=true`, `payload_ok=true`, `seal_ok=true`, no problems;
* seal text: valid;
* Git HEAD/tree: exact.

The 42-test rerun confirms deterministic identical builds and fail-closed
behavior for all 15 categories, Git HEAD/tree, dirty tracked state, missing
categories, malformed payloads, test execution records, traversal, hardlinks,
symlinks and holdout metadata. Mutations were performed only in isolated test
directories.

The dashboard SHA is
`f13906d931d929993bedc847e1dddfd01d6bd39026d1bb2922fc95881e89c66a`.
It is static HTML, contains no script/fetch/external URL/secret patterns, says
`PENDING WORK RE-AUDIT`, reports zero shadow candidates and ends `NO LIVE`.

The manifest's `certification_ready=true` means the evidence bundle is
structurally ready for Work review. It is not a scientific PASS:
`work_certification=PENDING_WORK_REAUDIT` and
`scientifically_certified=false` are explicit.

The authorized append to `WORK_RESEARCH.md` and creation of this report occur
after the successful pre-write verification and therefore intentionally make
the old manifest stale. That expected post-audit state is not retroactively
reported as a pre-audit manifest failure.

## 10. Safety and coordination hub

The rerun of `python -B -m app.research_lab security-audit` returned
`SAFE_PAPER_ONLY` and `NO LIVE`.

Verified flags:

* `PAPER_TRADING=true`;
* `LIVE_TRADING=false`;
* `DRY_RUN=true`;
* `ENABLE_PAPER_POLICY_FILTER=false`;
* `can_send_real_orders=false`;
* credentials absent in the audit;
* no real order was sent;
* no VPS or `.env` was touched;
* no push, commit or stage was performed.

The live execution path exists in the repository but is unreachable under the
current three-gate configuration. This audit does not certify profitability or
live readiness.

`python -B scripts/ai_coordination_status.py` returned `COHERENT`, no broken
links and exactly one NEXT_ACTION:

`WORK_REAUDIT_V10_47_23_EXACT_PAIRING_AND_CAMPAIGN_FWER`

No prior Codex record claims Work PASS or scientific certification.

## 11. Claims confirmed

* The old repeated-candidate P1 is fixed.
* Pairing is bijective and fail-closed per evaluation.
* Pair IDs are deterministic and field-bound.
* The 311-case reconciliation is exact.
* Campaign correction uses `min(1, p_raw * m_campaign)`.
* Official outputs use conservative nominal `m_campaign=564`.
* The 12 published tournaments remain negative and do not access holdout data.
* Full-suite collection and execution artifacts are coherent and hash-bound.
* Manifest/seal are deterministic and fail closed for covered mutations.
* Operational state is paper/research only.

## 12. Claims falsified

* The campaign family cannot be reduced by a caller: false.
* `m_campaign=47` necessarily fails closed: false when accompanied by a
  rehashed reduced contract.
* Campaign registry SHA proves membership in the official 12-tournament
  campaign: false; it proves only self-consistency of the supplied contract.
* Per-tournament registry/baseline/matching identities are fully bound to the
  campaign entry: false.
* V10.47.23 is ready for final scientific certification: false.

## 13. Remaining limitations

1. No real holdout was opened; correctly, no holdout performance claim exists.
2. Four accepted pair records map to three global economic events across two
   separate hypotheses; current multiplicity is conservative, but dependency
   metadata should be explicit.
3. The exact-match baseline has extremely low coverage (4/311), so it supplies
   essentially no evidence of superiority. Impossible matches are correctly
   excluded rather than treated as losses or zeros.
4. Two terminal generation logs are not sealed and cannot support certified
   claims.
5. All published results remain research evidence, not edge validation.

## 14. Minimal closed correction set

Next action:

`V10_47_24_ANCHOR_CAMPAIGN_REGISTRY_AND_FWER`

The correction set is deliberately narrow:

1. Make campaign authority internal or require equality with one canonical
   preregistered campaign SHA; do not trust a caller-rehashed replacement.
2. Validate the exact 4 symbols, 3 timeframes, 12 combinations, 47 participants
   per tournament and full participant IDs before statistics.
3. Bind each actual tournament's registry, strategy specs, baseline policy,
   matching/tolerance spec, alpha and correction method to its campaign entry.
4. Reject a reduced/rehashed symbol, timeframe, participant or tournament set;
   reject unrelated but well-formed registry/baseline hashes.
5. Add regressions where the same evidence passes at 47 and fails at 564, and
   require the counterfeit 47 contract to return INVALID with no p-value or
   promotion.
6. Regenerate all 12 gates, reports, certified test records, manifest and seal;
   request a fresh independent Work re-audit.

Do not open holdout, change strategies, tune parameters, enable shadow/paper,
or pursue edge recovery as part of this fix.

## 15. Final recommendation

Do not grant Work PASS or scientific certification. Preserve the negative
published result, implement only the campaign-authority correction above, and
repeat the focused audit.

```
NO_CONFIRMED_EDGE
SHADOW_CANDIDATES=0
HOLDOUT=SEALED
FINAL_RECOMMENDATION=NO LIVE
```
