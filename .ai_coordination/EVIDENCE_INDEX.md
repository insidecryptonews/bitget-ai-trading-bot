# EVIDENCE INDEX

## P11_SHORT continuous forward observer
- reviews/OPERATIONAL_ACTIVATION_REVIEW.md - revisión operativa que identificó el bloqueo.
- reviews/P11_SHORT_FORWARD_OBSERVER_IMPLEMENTATION.md - causa raíz, contrato, implementación y criterio de activación.
- app/labs/p11_short_forward_observer.py - fuente pública, lifecycle durable, persistencia, reconciliación y exports.
- app/labs/multi_symbol_opportunity_scanner_v10_28.py - conexión automática aislada al proceso continuo de research.
- app/research_lab.py - comandos públicos one-shot y continuos con dispatch temprano.
- app/labs/research_dashboard_v10_43c.py - proyección visible read-only y enlaces a exports atómicos.
- tests/test_p11_short_forward_observer.py - pruebas adversariales de lifecycle, recuperación, fencing y seguridad.
- tests/test_p11_forward_observer_integration.py - pruebas de CLI, hook, autowiring y ausencia de órdenes.
- tests/test_researchops_v10_43c_dashboard_watch.py - pruebas del panel, N/A, exports y publicación atómica.
- reports/research/p11_short_forward_observer/ - evidencia runtime ignorada: SQLite, ledger, outcomes, labels, reconciliación, status, resumen y captura del dashboard.

## V10.47.23 exact bijective pairing and campaign-wide FWER
- reviews/V10_47_22_WORK_FINAL_REAUDIT.md - protected independent Work FAIL.
- WORK_RESEARCH.md - protected Work research record.
- reports/research/v10_47_23_exact_pairing/logs/reproduction_before_fix.log - RED
  reproduction of repeated candidate identity and false paired-baseline success.
- reports/research/v10_47_23_exact_pairing/tournaments/work_reaudit_v10_47_23/ -
  twelve regenerated discovery-only tournament outputs and command logs.
- reports/research/v10_47_23_exact_pairing/evidence/work_reaudit_v10_47_23/ - exact
  pairing audit, campaign registry, deterministic comparison, reports and dashboard.
- reports/research/v10_47_23_exact_pairing/certified_tests/work_reaudit_v10_47_23/ -
  unique collection, nodeids, one full execution and hashes.
- reports/research/v10_47_23_exact_pairing/manifests/work_reaudit_v10_47_23/ -
  output_manifest.json, SEAL.txt and verification evidence.
- tests/test_researchops_v10_47_23_bijective_pairing_campaign.py - adversarial exact
  identity, bijection, campaign registry and correction contract.
- scripts/v10_47_23_regenerate_tournaments.py - bounded 12-combination regeneration.
- scripts/v10_47_23_run_one_tournament.py - isolated V10.47.23 output-root adapter.
- scripts/v10_47_23_generate_evidence.py - discovery-only evidence and dashboard.
- scripts/v10_47_23_certified_test_runner.py - one-run certified pytest evidence.
- scripts/v10_47_23_build_manifest.py - provenance-bound manifest and seal.

## V10.47.19-22 focused adversarial repair
- reviews/V10_47_18_WORK_REAUDIT.md - protected independent focused FAIL.
- WORK_RESEARCH.md - protected Work notes and research conclusions.
- logs/reproduction_before_fix.log - focused falsification categories reproduced RED.
- reports/research/v10_47_19_adversarial_certification_repair/progress_checkpoint.md
  - resumable builder checkpoint (ignored runtime evidence).
- reports/research/v10_47_22_real_state_certification/tournaments/
  work_reaudit_v10_47_22_final/ - twelve discovery-only tournament outputs and logs.
- reports/research/v10_47_22_real_state_certification/mtf/
  work_reaudit_v10_47_22_final/ - separate deterministic MTF technical experiment.
- reports/research/v10_47_22_real_state_certification/evidence/
  work_reaudit_v10_47_22_final/ - dataset/validation/baseline/ledger audits, report and dashboard.
- reports/research/v10_47_22_real_state_certification/certified_tests/
  work_reaudit_v10_47_22_final/ - unique collection, nodeids, one full execution and hashes.
- reports/research/v10_47_22_real_state_certification/manifests/
  work_reaudit_v10_47_22_final/ - output_manifest.json, SEAL.txt and independent verification.
- tests/test_researchops_v10_47_20_validation_holdout.py - validation and physical holdout.
- tests/test_researchops_v10_47_21_exact_baseline_mtf_atr.py - pairing, MTF and ATR ledger.
- tests/test_researchops_v10_47_22_real_state_manifest.py - real-state, mutation and runner contract.

## V10.47.8 scientific repair
- reproduction_flip.json — the sign flip (DOGE/XRP)
- invalidation_manifest.json / INVALIDATION.md — invalidation record
- reports/research/v10_47_8_scientific_repair/tournament/*.json (12)

## V10.47.15–18 certification repair
- reviews/V10_47_14_WORK_FINAL_AUDIT.md — Work's independent audit (FAIL)
- reports/research/v10_47_15_final_certification_repair/logs/reproduction_before_fix.log — findings reproduced RED
- reports/research/v10_47_15_final_certification_repair/tournament/*.json (12, repaired)
- reports/research/v10_47_15_final_certification_repair/certified_aggregate.json
- reports/research/v10_47_15_final_certification_repair/deduplicated_results.md
- reports/research/v10_47_15_final_certification_repair/paired_baseline_report.md
- reports/research/v10_47_15_final_certification_repair/split_validation_holdout_report.md
- reports/research/v10_47_15_final_certification_repair/registry_dedup_report.md
- reports/research/v10_47_15_final_certification_repair/reuse_decisions.md
- reports/research/v10_47_15_final_certification_repair/holdout/*.commitment.json (hash only, no bars)
- reports/research/v10_47_15_final_certification_repair/manifests/output_manifest.json — provenance-bound seal
- reports/research/v10_47_15_final_certification_repair/dashboard/dashboard_v10_47_18.html
- reports/research/v10_47_15_final_certification_repair/logs/ — regen, full_suite, collection
- tests/test_researchops_v10_47_15_certification.py — 16 falsification/certification tests
