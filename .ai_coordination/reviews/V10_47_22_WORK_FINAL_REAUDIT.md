# V10.47.22 - WORK FINAL INDEPENDENT RE-AUDIT

Date: 2026-07-14

Scope: independent scientific, technical and adversarial re-audit of V10.47.22.
No productive code, dataset, tournament output, Codex report, manifest, seal,
dashboard, configuration or Git history was modified. The real holdout was not
opened. Only this report and the authorized Work research note were written at
the end of the audit.

## 1. Executive verdict

**GLOBAL VERDICT: FAIL**

V10.47.22 is materially stronger than V10.47.14 and V10.47.18. Validation now
short-circuits walk-forward, the current twelve tournaments are reproducible,
the holdout remained sealed during this audit, MTF aggregation is causal, ATR
stops and trailing are recorded in append-only ledgers, the certified test
evidence is internally coherent, and the operational safety posture remains
paper/research only.

However, the declared exact one-to-one baseline contract is false for a valid
adversarial input. `matched_random_paired()` prevents reuse of a baseline ID but
does not prevent reuse of a candidate ID. Twelve rows carrying the same
`candidate_trade_id` can be accepted as twelve independent compatible pairs and
can pass the production Bonferroni gate (`m_global=47`). This is a P1 scientific
certification blocker.

This defect does **not** create evidence of edge in the published V10.47.22
outputs: their four actual matched pairs contain unique candidate, baseline and
pair IDs, the remaining 307 requests are fail-closed, the minimum corrected
p-value is 1.0, validation admits zero candidates, and shadow candidates remain
zero. The conservative result remains valid:

* `NO_CONFIRMED_EDGE`
* `SHADOW_CANDIDATES=0`
* `HOLDOUT=SEALED`
* `FINAL_RECOMMENDATION=NO LIVE`

## 2. Repository identity and initial state

Reproduced before the authorized audit-note writes:

* branch: `local-v10-47-8-scientific-repair`
* HEAD: `b85eb871bd293dd0614b7ff71c9d257a81baa2e6`
* tree: `f8e5dc557edc6d328216f9de352ab4d8c8c972fc`
* origin/main: `adc7b9c47ed2390eddaf80436287172455bb32d8`
* upstream: absent
* relation to origin/main: 32 commits ahead, 0 behind
* tracked state: clean
* historical untracked files only:
  * `CODEX_RESULT.md`
  * `CODE_RESULT.md`
  * `docs/research/LOCAL_AI_RESEARCH_ASSISTANT_FEASIBILITY_V10_40.md`
* `.env`: absent

The current absence of an upstream/push is not treated as proof that no push was
ever attempted historically.

Protected Work audit hashes before this re-audit:

* `V10_47_14_WORK_FINAL_AUDIT.md`:
  `a8869b6fbdd7ad022f7bd2ba3848c51d7bc33001ebd2c758011154e3b54d7c15`
* `V10_47_18_WORK_REAUDIT.md`:
  `e0b6188048e95608704da76ba2c835d003874b621c29ae6533d425db34a8e36b`

## 3. Individual verdicts

| Block | Verdict | Basis |
|---|---|---|
| VALIDATION | PASS | Validation follows train; failed validation returns before the WF supplier is invoked. |
| HOLDOUT | PASS WITH LIMITATIONS | No real holdout rows were opened; tournament receives discovery partitions only. Isolation is procedural/filesystem-audited, not cryptographic data confidentiality. |
| BASELINE | **FAIL** | Duplicate candidate IDs are accepted as independent exact pairs and can pass the corrected gate. |
| MULTIPLE TESTING | PASS WITH LIMITATIONS | Bonferroni `m_global=47` is preregistered per tournament; there is no explicit outer correction across the 12-tournament campaign. |
| MTF | PASS | Exact closed 4h buckets, prefix publication and next-open entry are reproduced; outputs remain `INSUFFICIENT_DATA`. |
| ATR LEDGER | PASS | Causal ATR, immutable initial stop, next-bar trailing and append-only records reproduced. |
| REPRODUCIBILITY | PASS | 12/12 tournament outputs and 5/5 MTF outputs match byte-for-byte. |
| TWELVE TOURNAMENTS | PASS | Published aggregate counts were independently recalculated. |
| UNIQUE TESTS | PASS WITH LIMITATIONS | Collection proves 3040 unique nodeids; the execution log proves 3040 passes but does not record a per-node execution trace. |
| MANIFEST/SEAL | PASS WITH LIMITATIONS | Payload/seal and 146 non-holdout covered files were independently verified before the authorized Work write; real holdout bytes were not opened. |
| SAFETY | PASS | `SAFE_PAPER_ONLY`; no real-order path introduced. |
| HUB | PASS | `COHERENT`, one pending Work action, previous FAIL audits retained. |
| DASHBOARD | PASS WITH LIMITATIONS | Static semantic contract passes; no visual screenshot was performed. |

## 4. Severity findings

### P0

None found.

### P1 - Exact-pair baseline accepts repeated candidate IDs

Files and lines:

* `app/labs/v10_46/causal_stats.py:251-260`
* `app/labs/v10_46/causal_stats.py:300-306`
* `app/labs/v10_46/causal_stats.py:332-350`

`consumed` tracks only `baseline_trade_id`. There is no consumed/seen set for
`candidate_trade_id`. Consequently, one logical candidate can be replicated
across multiple opportunities and each replica receives a different baseline.
Coverage, bootstrap, sign p-value and gate then count every replica.

Safe synthetic reproduction using only fictitious rows:

* 12 candidate rows
* one unique `candidate_trade_id`
* 12 unique opportunities and exact matching baseline rows
* 12 unique `baseline_trade_id` values
* paired deltas all `+1.0`
* `m_global=47`

Observed result:

```text
match_status=OK
pairs_requested=12
pairs_found=12
coverage=1.0
raw_p_value=0.0002441406
corrected_p_value=0.0114746094
paired_lower_bound_eur=1.0
beats_matched_random=true
unique_candidate_ids=1
unique_baseline_ids=12
unique_pair_ids=12
```

Impact: upstream duplicate candidate rows can inflate effective sample size,
paired lower bound and significance while violating the advertised one-to-one
contract. This can create a false positive promotion gate in a future run.

Current-output impact: none observed. Across the 21 actual pair blocks:

```text
pairs_requested=311
pairs_found=4
pairs_impossible=299
pairs_incompatible=8
duplicate OK candidate IDs=0
duplicate OK baseline IDs=0
duplicate OK pair IDs=0
```

The current `NO_CONFIRMED_EDGE` result therefore remains fail-closed.

### P2 - `m_global` is local to each tournament, not the complete campaign

Files and lines:

* `app/labs/v10_46/causal_tournament.py:133-168`
* `app/labs/v10_46/causal_tournament.py:273-277`

Each symbol/timeframe tournament preregisters and applies `m_global=47`. The
campaign runs 12 tournaments (564 participant results) and may select a winner
across them, but no explicit outer family correction is applied. This is not a
current-result defect because every corrected paired p-value is 1.0 and no
candidate reaches validation/WF promotion. It is a future selection-risk and the
label `global` is broader than the implemented scope.

### P2 - Holdout capability is intentionally non-operational after preparation

Files and lines:

* `scripts/v10_47_22_prepare_isolated_datasets.py:1-5`
* `scripts/v10_47_22_prepare_isolated_datasets.py:170-180`
* `app/labs/v10_46/holdout_loader.py:47-75`

The preparation process generates an authority secret, commits only its hash and
deliberately discards the secret. That makes self-authorization impossible, but
also means the generated real holdout cannot later be opened through the stated
HMAC authority unless the dataset is regenerated with an externally retained
secret. The behavior is disclosed in the module header, so this is not hidden
leakage. It does mean `SEALED` is an orchestration/process-isolation contract,
not encryption or an OS access-control boundary.

### P3 - Direct holdout loader relies on the outer isolation audit for hardlinks

Files and lines:

* `app/labs/v10_46/holdout_loader.py:119-135`
* `app/labs/v10_46/discovery_dataset.py:107-126`

The loader rejects traversal and symlinks, but a committed file that is a
hardlink to an external file is accepted if its content hash matches. The
standard orchestration separately detects shared file identities between
discovery and sealed roots and blocks them. This is defense-in-depth, not a
current twelve-tournament leak.

### P3 - Certified execution is aggregate, not a per-node execution ledger

File and lines:

* `scripts/v10_47_22_certified_test_runner.py:142-155`
* `scripts/v10_47_22_certified_test_runner.py:177-205`

The runner records the unique collected nodeid list and then executes one normal
`pytest -q` process. Equal collection/pass counts strongly support a full run,
but the execution log does not cryptographically map each individual nodeid to
exactly one execution. The reported aggregate claim is supported; a stronger
per-node execution claim is not independently demonstrated.

## 5. Validation short-circuit

Files and lines:

* `app/labs/v10_46/causal_tournament.py:282-293`
* `app/labs/v10_46/causal_tournament.py:309-331`

Demonstrated with stateless synthetic deciders and a counted WF supplier:

* train-positive / validation-negative: stages `train, train, train, validation`,
  supplier calls `0`, `walk_forward_called=false`, WF metrics `null`, status
  `REJECTED_AT_VALIDATION`;
* no validation trades: rejected;
* insufficient validation `n_eff`: rejected;
* positive eligible validation: supplier invoked exactly once and WF evaluated;
* parameters and callable identity remain unchanged after validation.

The main tournament path does not precompute WF features for rejected
candidates. Directly calling lower-level helpers outside the orchestrator is not
a bypass of the tournament promotion state machine.

## 6. Holdout physical isolation

Files reviewed:

* `app/labs/v10_46/discovery_dataset.py`
* `app/labs/v10_46/holdout_loader.py`
* `app/labs/v10_46/holdout_contract.py`
* `app/labs/v10_46/sealed_holdout.py`
* `scripts/v10_47_8_run_causal_tournament.py`
* `scripts/v10_47_22_prepare_isolated_datasets.py`

Demonstrated without reading real holdout rows:

* tournament input is `DiscoveryPartitions(train, validation, walk_forward,
  source_root)` only;
* no holdout field or row reference reaches the tournament;
* tournament process does not import `holdout_loader`;
* all 12 discovery/sealed roots are distinct;
* filesystem identity audit found no shared paths or shared inodes;
* all 12 access logs are absent/empty and `physically_loaded=false`;
* synthetic capability signature alteration, second use, restart reuse,
  traversal, external absolute path, uncommitted path and symlink escape fail;
* a synthetic discovery/sealed hardlink is detected by
  `audit_dataset_isolation()`.

No real holdout row was parsed or exposed during this audit. The real holdout
was not opened. `HOLDOUT=SEALED` remains accurate for the audited execution.

## 7. Exact baseline and paired inference

The following parts pass independently of the P1 uniqueness defect:

* all 22 preregistered match fields were falsified one at a time and rejected;
* a required field missing on both sides is rejected;
* baseline IDs cannot be reused;
* incompatible and impossible pairs do not enter deltas, lower bound or gate;
* delta is candidate net minus baseline net;
* incomplete coverage blocks promotion;
* changing `m_global` changes the corrected p-value and gate;
* correction is applied before `beats_matched_random` is computed.

The global block still fails because candidate ID uniqueness is a necessary
part of one-to-one pairing.

## 8. Dependence-aware sample size

File and lines:

* `app/labs/v10_46/causal_stats.py:21-89`

`n_eff_final` is the minimum of event count, non-overlapping intervals,
clusters, sessions, days, temporal blocks and an ACF-based estimate. Degenerate
returns reduce ACF effective size to 1. Validation uses `n_eff_final`, not
`n_raw`, in its eligibility gate. No promotion path using raw trade count was
found in the audited tournament. This is conservative, although it is a custom
heuristic rather than a formal proof of independence.

## 9. MTF 4h to 1h

Files and lines:

* `app/labs/v10_46/det_strategies.py:132-280`
* `scripts/v10_47_22_run_mtf_experiment.py`

Reproduced:

* exactly four consecutive 1h bars form a 4h bucket;
* gap, duplicate, out-of-order and incomplete buckets are rejected;
* the closed 4h regime is published only after bucket close;
* a 00/01/02/03 bucket is available at 04;
* entry uses the next 1h open;
* future mutation does not alter past signals;
* LONG/SHORT EMA-ADX and Donchian variants are covered;
* day crossings and missing regimes do not introduce fallback lookahead.

The experiment is correctly separate from the 12 intraday tournaments. All
four MTF strategy outputs report `SCIENTIFIC_EVALUATION=INSUFFICIENT_DATA`,
`NEEDS_2Y_DATA=true`, no edge classification and `NO LIVE`.

## 10. ATR, fills and append-only ledger

Files and lines:

* `app/labs/v10_46/det_strategies.py:284-291`
* `app/labs/v10_46/causal_ledger.py:142-244`
* `app/labs/v10_46/sim_oms.py:188-275`

Reproduced numerically:

* LONG entry 100, ATR 2, 2-ATR risk -> immutable stop 96;
* SHORT entry 100, ATR 2, 2-ATR risk -> immutable stop 104;
* same-bar stop/TP ambiguity -> stop wins;
* trailing activates after +1R from a completed bar and becomes effective on
  the next bar;
* trailing never widens the initial risk;
* future ATR mutation does not change the entry ATR or initial stop;
* deep copies prevent mutation of prior records.

Across 564 result ledgers: sequences are contiguous, close counts match trade
counts, executed trade IDs are unique, and required SIGNAL/ENTRY/POSITION/CLOSE
records are consistent. Duplicate ledger hashes occur for no-trade or
behaviorally equivalent policies; they are retained in `m_global`, which is
conservative for current inference.

## 11. Reproducibility and twelve-tournament aggregates

Independently reproduced:

* 12/12 canonical tournament JSON files equal their deterministic reproduction
  byte-for-byte;
* 5/5 MTF JSON files equal their reproduction byte-for-byte;
* no `decider_object_id` appears in scientific output;
* two independent preregistration processes returned the same registry hash:
  `d534a77911f43d72a9d3a9eba19cf1d9fe1b56b40cc60b67df7345a8e410efd4`;
* each registry has `m_global=47`, `m_unique_results=45`.

Recalculated from the 12 final tournament JSON files:

* symbols/timeframes: BTC, ETH, XRP, DOGE x 1m, 5m, 15m;
* train-positive candidates: 21;
* validation admitted: 0;
* validation rejected: 21;
* WF calls: 0;
* shadow candidates: 0;
* baseline requested/found/impossible/incompatible: 311/4/299/8;
* minimum corrected paired p-value: 1.0;
* result ledgers: 564;
* holdout physically loaded/imported: 0/12.

## 12. Certified tests

Safe targeted execution performed during this audit:

```text
tests/test_researchops_v10_47_20_validation_holdout.py
tests/test_researchops_v10_47_21_exact_baseline_mtf_atr.py
tests/test_researchops_v10_47_22_real_state_manifest.py
128 passed in 42.27s
exit code 0
```

`compileall` was run with bytecode redirected outside the repository and exited
0.

The full suite was not rerun. Its sealed certified evidence was independently
parsed and hash-checked:

* collected invocations: 3040;
* unique nodeids: 3040;
* duplicate nodeids: 0;
* passed: 3040;
* failures/skips/xfails/xpasses/deselected: 0;
* execution exit code: 0;
* raw pytest summary: `3040 passed in 628.17s`;
* execution record duration: `633.571209s`;
* collection raw SHA:
  `fbfec5...374a7` (full value verified from the sealed record);
* nodeid-list SHA:
  `4ae9dd...00d3` (full value verified from the sealed record);
* execution raw SHA:
  `d82ce7...04ce` (full value verified from the sealed record);
* collection record SHA:
  `acac3a...caed` (full value verified from the sealed record).

The runner executes one complete `pytest -q`; it does not produce a per-node
execution ledger. Therefore the exact claim supported is one clean full-suite
run with 3040 passes, not a cryptographic proof that every collected ID was
individually logged once.

## 13. Manifest and seal

Before the authorized audit-note writes, independent recomputation found:

* categories: 15;
* paths: 170, all unique;
* covered symlinks: 0;
* duplicate file identities: 0;
* all recorded sizes consistent;
* payload hash:
  `7f9e08d169380598bef873e519bdf6cb65b15b5e93d85bbb16367e67126edb9f`;
* seal:
  `ef22fc6d7d9c9b3028500a544b2568532d3a16ae140ad53bbb7db102fbf3f72b`;
* `SEAL.txt`, aliases, branch, HEAD and tree consistent;
* 146 non-holdout covered files rehashed with zero mismatch.

The 24 real holdout files were not opened or independently rehashed because the
audit instruction explicitly forbids opening the real holdout. Only metadata,
size and file identity were inspected. The existing manifest verifier was not
run against those bytes for the same reason.

Targeted tests cover mutations of all 15 categories, Git identity/dirty state,
traversal, absolute path, symlink/hardlink coverage, aliases, malformed payload,
seal text and certified execution records.

`certification_ready=true` in this manifest means integrity/test bundle ready;
the same payload explicitly says `work_certification=PENDING_WORK_REAUDIT` and
`scientifically_certified=false`. It is not an edge or live certification.

The authorized append to `WORK_RESEARCH.md` necessarily changes a covered Work
file after the sealed snapshot. Consequently, the original seal remains valid
for the pre-audit snapshot and must not be described as sealing the final dirty
workspace unless a new post-audit manifest is deliberately built.

## 14. Operational safety

Reproduced security audit:

```text
SAFE_PAPER_ONLY
PAPER_TRADING=true
LIVE_TRADING=false
DRY_RUN=true
can_send_real_orders=false
paper_filter_enabled=false
FINAL_RECOMMENDATION=NO LIVE
```

Relevant defaults:

* `app/config.py:355-356`
* `app/config.py:376-378`
* `app/config.py:541-542`

AST/text/import scans of V10.47.22 productive modules and scripts found no new
Bitget private endpoint, order placement, execution-engine call, paper position
opening, leverage/margin mutation, live enablement or environment write. No
VPS action, commit or push was performed.

## 15. Hub and dashboard

`python scripts/ai_coordination_status.py` returned `COHERENT` with one action:
`WORK_REAUDIT_V10_47_22`. Historical FAIL audits remain present and unchanged;
Codex did not self-certify.

The final dashboard was inspected statically:

* static HTML, 4551 bytes;
* no script tags, fetch calls or external URLs;
* displays `NO_CONFIRMED_EDGE`, `SHADOW_CANDIDATES=0`, `HOLDOUT SEALED`,
  `PENDING WORK RE-AUDIT`, MTF `INSUFFICIENT_DATA`, and `NO LIVE`.

No visual screenshot was taken, so only the static semantic contract passes.

## 16. Claims reproduced

* Validation rejects before WF and does not precompute rejected WF features.
* The audited tournament processes received no holdout rows and imported no
  holdout loader.
* The real holdout stayed sealed and unopened during this audit.
* Required baseline fields are exact/fail-closed when missing or mismatched.
* Incomplete baseline coverage blocks all current candidates.
* MTF aggregation and publication are causal and complete-bucket only.
* ATR stop/trailing state is causal, side-symmetric and ledgered.
* 12/12 tournaments and 5/5 MTF outputs reproduce byte-for-byte.
* Published twelve-tournament counts recalculate.
* Certified collection/execution records and hashes are internally coherent.
* Manifest/seal recompute for the pre-audit snapshot.
* Safety remains paper/research only.
* No edge and no shadow candidate are present.

## 17. Claims falsified or overstated

* **Falsified:** “Exact one-to-one baseline” for arbitrary candidate input.
  Candidate IDs are not unique-enforced.
* **Overstated:** `m_global` as campaign-global; it is tournament-local.
* **Limited:** external authority capability exists as code but the real
  preparation secret is intentionally destroyed, so future authorized opening
  is not operational for the generated artifacts.
* **Not fully demonstrated:** exact once-per-node test execution; only unique
  collection plus one full aggregate execution is evidenced.
* **Not assessed visually:** dashboard rendering.

## 18. Files reviewed

Principal code:

* `app/labs/v10_46/causal_stats.py`
* `app/labs/v10_46/causal_tournament.py`
* `app/labs/v10_46/causal_ledger.py`
* `app/labs/v10_46/sim_oms.py`
* `app/labs/v10_46/det_strategies.py`
* `app/labs/v10_46/discovery_dataset.py`
* `app/labs/v10_46/holdout_loader.py`
* `app/labs/v10_46/holdout_contract.py`
* `app/labs/v10_46/manifest_seal.py`
* `scripts/v10_47_8_run_causal_tournament.py`
* `scripts/v10_47_22_prepare_isolated_datasets.py`
* `scripts/v10_47_22_run_mtf_experiment.py`
* `scripts/v10_47_22_certified_test_runner.py`
* `app/config.py`

Coordination/evidence:

* all required `.ai_coordination` truth files;
* V10.47.14 and V10.47.18 Work audits;
* final V10.47.22 report, manifest and seal;
* 12 tournament outputs and deterministic reproductions;
* five MTF outputs and reproductions;
* registries, specs, commitments and isolation audits;
* ledger integrity summaries;
* certified collection and execution logs/records;
* static final dashboard;
* three targeted V10.47.20-22 test modules.

## 19. Minimal closed correction set

Next action: **V10.47.23 scientific baseline uniqueness hotfix**, not strategy
research and not live/paper activation.

Required to remove the blocking P1:

1. Reject duplicate or empty `candidate_trade_id` before pairing; maintain an
   explicit consumed-candidate set in addition to consumed baselines.
2. Require uniqueness of all compatible `candidate_trade_id`,
   `baseline_trade_id` and `pair_id` values before coverage/statistics.
3. Return a fail-closed reason such as `DUPLICATE_CANDIDATE_TRADE_ID` and force
   `beats_matched_random=false` for any uniqueness violation.
4. Add adversarial tests with duplicate candidate IDs that would otherwise pass
   under the real `m_global=47`, plus duplicate baseline and duplicate pair-ID
   invariant tests.
5. Define the multiple-testing family explicitly. If promotion can select among
   all 12 tournaments, preregister and apply an outer correction or a valid
   hierarchical procedure before any future shadow classification.

Recommended non-blocking hardening:

6. Make the holdout authority lifecycle explicit: either retain the authority
   secret outside the repository under independent control, or document the
   generated holdout as intentionally irreversible and regenerate for an
   authorized opening.
7. Add hardlink/file-identity enforcement inside the loader, not only in the
   outer isolation audit.
8. If exact per-node execution provenance is required, emit a per-test execution
   ledger rather than infer it from aggregate counts.

After the P1 fix, rerun only the affected baseline tests first, then the three
V10.47.20-23 targeted suites, regenerate the 12 tournaments, recalculate all
pair invariants, and only then rebuild a new manifest/seal for a new Work audit.

## 20. Final recommendation

Do not certify V10.47.22 as scientifically complete and do not push it as a
final scientific repair while the exact-pair baseline invariant is false.

The negative result remains trustworthy enough to keep the system stopped:

```text
CERTIFICATION=FAIL
NO_CONFIRMED_EDGE
SHADOW_CANDIDATES=0
HOLDOUT=SEALED
FINAL_RECOMMENDATION=NO LIVE
```

## 21. Final repository state

Final `git status --short` after the two authorized audit writes:

```text
 M .ai_coordination/WORK_RESEARCH.md
?? .ai_coordination/reviews/V10_47_22_WORK_FINAL_REAUDIT.md
?? CODEX_RESULT.md
?? CODE_RESULT.md
?? docs/research/LOCAL_AI_RESEARCH_ASSISTANT_FEASIBILITY_V10_40.md
```

There are no productive-code, dataset, output, manifest, seal, dashboard or
configuration changes. `git diff --check` exited 0. No file was staged, no
commit was created and no push was performed.

Protected hashes after the audit are unchanged from the before values:

* `V10_47_14_WORK_FINAL_AUDIT.md`:
  `a8869b6fbdd7ad022f7bd2ba3848c51d7bc33001ebd2c758011154e3b54d7c15`
* `V10_47_18_WORK_REAUDIT.md`:
  `e0b6188048e95608704da76ba2c835d003874b621c29ae6533d425db34a8e36b`

The coordination status remains `COHERENT` with exactly one pending action,
`WORK_REAUDIT_V10_47_22`; this report supplies the independent FAIL verdict but
does not silently rewrite the action tracker or erase historical failures.
