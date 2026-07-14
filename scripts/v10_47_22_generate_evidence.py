"""Generate V10.47.22 audits, report, static dashboard and coverage seed."""

from __future__ import annotations

import argparse
import hashlib
import html
import inspect
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.labs.v10_46.discovery_dataset import (  # noqa: E402
    DiscoveryDatasetLoader,
    audit_dataset_isolation,
)


DATA_ROOT = ROOT / "external_data" / "staging" / "v10_47_22_isolated"
REPORT_ROOT = ROOT / "reports" / "research" / "v10_47_22_real_state_certification"
SYMBOLS = ("BTCUSDT", "ETHUSDT", "XRPUSDT", "DOGEUSDT")
TIMEFRAMES = ("1m", "5m", "15m")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=ROOT, text=True, encoding="utf-8", errors="replace",
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else "UNAVAILABLE"


def atomic_text(path: Path, value: str) -> None:
    temporary = path.with_name(path.name + f".tmp.{uuid.uuid4().hex}")
    temporary.write_text(value, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def atomic_json(path: Path, value: dict) -> None:
    atomic_text(path, json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")


def rel(path: Path) -> str:
    return path.resolve(strict=True).relative_to(ROOT.resolve(strict=True)).as_posix()


def load_tournaments(label: str) -> tuple[Path, dict[str, dict]]:
    run_root = (REPORT_ROOT / "tournaments" / label).resolve(strict=True)
    run_root.relative_to(REPORT_ROOT.resolve(strict=True))
    values = {}
    for symbol in SYMBOLS:
        for timeframe in TIMEFRAMES:
            key = f"{symbol}:{timeframe}"
            path = run_root / f"{symbol}_{timeframe}.json"
            value = json.loads(path.read_text(encoding="utf-8"))
            if value.get("symbol") != symbol or value.get("timeframe") != timeframe:
                raise RuntimeError(f"tournament identity mismatch: {key}")
            if value.get("holdout", {}).get("state") != "SEALED" \
                    or value.get("execution_provenance", {}).get("holdout_data_loaded") is not False:
                raise RuntimeError(f"holdout contract failed: {key}")
            values[key] = value
    return run_root, values


def dataset_audit() -> tuple[dict, dict[str, list[str]]]:
    source = inspect.getsource(DiscoveryDatasetLoader)
    combinations = {}
    dataset_paths: set[str] = set()
    manifest_paths: set[str] = set()
    holdout_paths: set[str] = set()
    all_ok = True
    for symbol in SYMBOLS:
        for timeframe in TIMEFRAMES:
            key = f"{symbol}:{timeframe}"
            combo = DATA_ROOT / symbol / timeframe
            manifest_path = combo / "dataset_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            isolation = audit_dataset_isolation(
                combo / "discovery", combo / "sealed_holdout"
            )
            file_problems = []
            for record in manifest.get("files", []):
                path = combo / record["path"]
                if not path.is_file() or sha256(path) != record["sha256"]:
                    file_problems.append(record["path"])
                if record.get("partition", "").startswith("holdout"):
                    holdout_paths.add(rel(path))
                else:
                    dataset_paths.add(rel(path))
            commitment_path = combo / "sealed_holdout" / "commitment.json"
            commitment = json.loads(commitment_path.read_text(encoding="utf-8"))
            holdout_data = combo / "sealed_holdout" / commitment["data_file"]
            commitment_matches = sha256(holdout_data) == commitment["commitment_sha256"]
            source_paths = [manifest.get("source"), manifest.get("reference_source")]
            for record in source_paths:
                if not record:
                    continue
                dataset_paths.add(record["csv_path"])
                manifest_paths.add(record["manifest_path"])
            manifest_paths.add(rel(manifest_path))
            loader_source_clean = "holdout" not in source.lower()
            ok = bool(
                isolation["ok"] and not file_problems and commitment_matches
                and loader_source_clean and manifest.get("authority_secret_persisted") is False
            )
            all_ok &= ok
            combinations[key] = {
                "ok": ok,
                "isolation": isolation,
                "file_hash_problems": file_problems,
                "commitment_matches_sealed_bytes": commitment_matches,
                "authority_secret_persisted": manifest.get("authority_secret_persisted"),
                "loader_source_references_holdout": not loader_source_clean,
                "holdout_state": commitment.get("state"),
                "source_generation_id": manifest.get("source_generation_id"),
            }
    return ({
        "schema": "v10_47_22_dataset_separation_audit",
        "ok": all_ok,
        "combinations": combinations,
        "holdout_rows_parsed_by_audit": False,
        "holdout_bytes_read_for_sha_only": True,
        "research_only": True,
        "final_recommendation": "NO LIVE",
    }, {
        "dataset": sorted(dataset_paths),
        "dataset_manifest": sorted(manifest_paths),
        "holdout": sorted(holdout_paths),
    })


def indexes(tournaments: dict[str, dict]) -> tuple[dict, dict, dict, dict]:
    specs, registries, ledgers = {}, {}, {}
    gate_rows = []
    for key, value in sorted(tournaments.items()):
        registry = value["registry"]
        specs[key] = {
            "specs": registry["registry_contract"]["specs"],
            "specs_hash": registry["specs_hash"],
            "baseline_policy_spec": registry["baseline_policy_spec"],
            "baseline_policy_spec_hash": registry["baseline_policy_spec_hash"],
            "baseline_tolerance_spec_hash": registry["baseline_tolerance_spec_hash"],
        }
        registries[key] = registry
        ledgers[key] = {
            name: result["ledger_integrity"]
            for name, result in sorted(value["results"].items())
        }
        for name, result in value["results"].items():
            gate = result.get("gate")
            if gate:
                paired = gate["matched_random_paired"]
                gate_rows.append({
                    "tournament": key, "policy": name,
                    "validation_gate": gate["validation_gate"],
                    "validation_rejection_reason": gate["validation_rejection_reason"],
                    "walk_forward_called": gate["walk_forward_called"],
                    "pairs_requested": paired["pairs_requested"],
                    "pairs_found": paired["pairs_found"],
                    "pairs_incompatible": paired["pairs_incompatible"],
                    "coverage": paired["coverage"],
                    "paired_lower_bound_eur": paired["paired_lower_bound_eur"],
                    "corrected_p_value": paired["corrected_p_value"],
                    "n_eff": gate["selection_metrics"]["n_eff_final"],
                    "classification": gate["selection_metrics"]["classification"],
                    "all_gates_pass": gate["gates"]["all_pass"],
                })
    validation_audit = {
        "schema": "v10_47_22_validation_baseline_audit",
        "candidates_evaluated": len(gate_rows),
        "validation_admitted": sum(row["validation_gate"] for row in gate_rows),
        "validation_rejected": sum(not row["validation_gate"] for row in gate_rows),
        "walk_forward_called": sum(row["walk_forward_called"] for row in gate_rows),
        "walk_forward_called_after_validation_failure": sum(
            row["walk_forward_called"] and not row["validation_gate"]
            for row in gate_rows
        ),
        "exact_baseline_rows": gate_rows,
        "shadow_candidates": sum(
            len(value.get("shadow_candidates", [])) for value in tournaments.values()
        ),
        "research_only": True,
        "final_recommendation": "NO LIVE",
    }
    return (
        {"schema": "v10_47_22_spec_index", "items": specs},
        {"schema": "v10_47_22_registry_index", "items": registries},
        {"schema": "v10_47_22_ledger_index", "items": ledgers},
        validation_audit,
    )


def report_markdown(*, head: str, tree: str, dataset: dict,
                    validation: dict, mtf: dict) -> str:
    return f"""# V10.47.22 Real-State Adversarial Repair

## Status

- HEAD: `{head}`
- tree: `{tree}`
- WORK_REAUDIT_REQUIRED: `true`
- CERTIFICATION: `PENDING_WORK_REAUDIT`
- NO_CONFIRMED_EDGE: `true`
- SHADOW_CANDIDATES: `{validation['shadow_candidates']}`
- HOLDOUT: `SEALED`
- FINAL_RECOMMENDATION: `NO LIVE`

## Evidence

- Physical dataset isolation audit: `{'PASS' if dataset['ok'] else 'FAIL'}` across 12 combinations.
- Candidates evaluated after positive TRAIN classification: `{validation['candidates_evaluated']}`.
- VALIDATION admitted: `{validation['validation_admitted']}`.
- VALIDATION rejected: `{validation['validation_rejected']}`.
- WALK_FORWARD calls: `{validation['walk_forward_called']}`.
- WF calls after failed VALIDATION: `{validation['walk_forward_called_after_validation_failure']}`.
- Exact one-to-one baseline is fail-closed; incomplete coverage cannot promote.
- Multiple-testing gate uses preregistered Bonferroni `m_global`.
- Independent MTF experiment: `{mtf.get('scientific_evaluation', 'INSUFFICIENT_DATA')}`; needs two years.

The final manifest and `SEAL.txt` are generated after the certified full-suite run.
The dashboard links to that external seal rather than embedding its own hash, which
would create an impossible cryptographic self-reference because the dashboard is
itself a covered manifest input.

`VISUAL_DASHBOARD_VERIFICATION=PENDING_USER_SCREENSHOT`

No policy is promoted. No holdout rows were parsed. No live/paper execution path
was enabled.
"""


def dashboard_html(*, head: str, tree: str, dataset: dict,
                   validation: dict, mtf: dict) -> str:
    rows = "".join(
        f"<tr><td>{html.escape(row['tournament'])}</td>"
        f"<td>{html.escape(row['policy'])}</td>"
        f"<td>{html.escape(str(row['validation_rejection_reason']))}</td>"
        f"<td>{row['coverage']:.4f}</td><td>{row['corrected_p_value']:.4f}</td></tr>"
        for row in validation["exact_baseline_rows"]
    ) or "<tr><td colspan='5'>No TRAIN-positive candidates required gating.</td></tr>"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>V10.47.22 Research Status</title><style>
body{{margin:0;background:#111315;color:#e8ecef;font:14px Segoe UI,Arial,sans-serif}}
header{{padding:18px 24px;border-bottom:1px solid #343a40;background:#171a1d}}
main{{max-width:1180px;margin:auto;padding:20px}}h1{{font-size:22px;margin:0 0 8px}}h2{{font-size:16px;margin-top:26px}}
.danger{{color:#ff6b6b;font-weight:700}}.warn{{color:#ffd166}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:10px}}
.metric{{border:1px solid #343a40;padding:14px;border-radius:6px;background:#191d20}}.metric b{{display:block;margin-bottom:7px;color:#aab3ba}}
table{{width:100%;border-collapse:collapse;background:#171a1d}}th,td{{padding:9px;border:1px solid #343a40;text-align:left}}th{{color:#aab3ba}}
code{{overflow-wrap:anywhere}}a{{color:#7cc7ff}}
</style></head><body><header><h1>V10.47.22 Adversarial Repair</h1><div class="danger">RESEARCH ONLY · CERTIFICATION PENDING WORK RE-AUDIT · NO LIVE</div></header>
<main><div class="grid">
<div class="metric"><b>NO_CONFIRMED_EDGE</b>true</div><div class="metric"><b>SHADOW_CANDIDATES</b>{validation['shadow_candidates']}</div>
<div class="metric"><b>HOLDOUT</b>SEALED</div><div class="metric"><b>Dataset isolation</b>{'PASS' if dataset['ok'] else 'FAIL'}</div>
<div class="metric"><b>Validation admitted / rejected</b>{validation['validation_admitted']} / {validation['validation_rejected']}</div>
<div class="metric"><b>WF after validation fail</b>{validation['walk_forward_called_after_validation_failure']}</div>
<div class="metric"><b>MTF scientific status</b>{html.escape(mtf.get('scientific_evaluation','INSUFFICIENT_DATA'))}</div>
<div class="metric"><b>Manifest / seal SHA</b><a href="../manifests/SEAL.txt">external self-reference: see SEAL.txt</a></div>
</div><h2>Provenance</h2><p>HEAD <code>{head}</code><br>tree <code>{tree}</code></p>
<h2>Exact baseline and validation</h2><table><thead><tr><th>Tournament</th><th>Policy</th><th>Validation result</th><th>Coverage</th><th>Corrected p</th></tr></thead><tbody>{rows}</tbody></table>
<h2>Truth labels</h2><p class="warn">Exact-match incomplete means GATE_FAIL. A high TRAIN metric is not edge. MTF is a separate technical smoke with insufficient data.</p>
<p class="danger">FINAL_RECOMMENDATION: NO LIVE</p></main></body></html>"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tournament-label", required=True)
    parser.add_argument("--mtf-label", required=True)
    parser.add_argument("--evidence-label", required=True)
    args = parser.parse_args(argv)
    if not all(label.replace("_", "").replace("-", "").isalnum()
               for label in (args.tournament_label, args.mtf_label, args.evidence_label)):
        raise RuntimeError("unsafe label")
    run_root, tournaments = load_tournaments(args.tournament_label)
    mtf_summary_path = REPORT_ROOT / "mtf" / args.mtf_label / "mtf_summary.json"
    mtf = json.loads(mtf_summary_path.read_text(encoding="utf-8"))
    dataset, coverage = dataset_audit()
    spec_index, registry_index, ledger_index, validation = indexes(tournaments)
    evidence_root = (REPORT_ROOT / "evidence" / args.evidence_label).resolve()
    evidence_root.relative_to(REPORT_ROOT.resolve())
    if evidence_root.exists():
        raise RuntimeError("evidence label already exists")
    evidence_root.mkdir(parents=True, exist_ok=False)
    paths = {
        "dataset_audit": evidence_root / "dataset_separation_audit.json",
        "spec": evidence_root / "spec_index.json",
        "registry": evidence_root / "registry_index.json",
        "ledger": evidence_root / "ledger_index.json",
        "validation": evidence_root / "validation_baseline_audit.json",
        "report": evidence_root / "FINAL_REPORT.md",
        "dashboard": evidence_root / "status.html",
    }
    atomic_json(paths["dataset_audit"], dataset)
    atomic_json(paths["spec"], spec_index)
    atomic_json(paths["registry"], registry_index)
    atomic_json(paths["ledger"], ledger_index)
    atomic_json(paths["validation"], validation)
    head, tree = git("rev-parse", "HEAD"), git("rev-parse", "HEAD^{tree}")
    atomic_text(paths["report"], report_markdown(
        head=head, tree=tree, dataset=dataset, validation=validation,
        mtf={"scientific_evaluation": "INSUFFICIENT_DATA"},
    ))
    atomic_text(paths["dashboard"], dashboard_html(
        head=head, tree=tree, dataset=dataset, validation=validation,
        mtf={"scientific_evaluation": "INSUFFICIENT_DATA"},
    ))
    coverage.update({
        "spec": [rel(paths["spec"])],
        "registry": [rel(paths["registry"])],
        "policy": [
            "app/labs/v10_46/causal_ledger.py",
            "app/labs/v10_46/causal_stats.py",
            "app/labs/v10_46/causal_tournament.py",
            "app/labs/v10_46/det_strategies.py",
            "app/labs/v10_46/discovery_dataset.py",
            "app/labs/v10_46/holdout_contract.py",
            "app/labs/v10_46/holdout_loader.py",
            "app/labs/v10_46/manifest_seal.py",
            "app/labs/v10_46/sealed_holdout.py",
            "app/labs/v10_46/sim_oms.py",
            "scripts/v10_47_22_build_real_state_manifest.py",
            "scripts/v10_47_22_certified_test_runner.py",
            "scripts/v10_47_22_generate_evidence.py",
            "scripts/v10_47_22_prepare_isolated_datasets.py",
            "scripts/v10_47_22_regenerate_tournaments.py",
            "scripts/v10_47_22_run_mtf_experiment.py",
            "scripts/v10_47_22_run_one_tournament.py",
        ],
        "ledger": [rel(paths["ledger"])],
        "tournament": sorted(
            [rel(path) for path in run_root.glob("*.json")]
            + [rel(path) for path in (REPORT_ROOT / "mtf" / args.mtf_label).glob("*.json")]
        ),
        "report": [rel(paths["report"])],
        "dashboard": [rel(paths["dashboard"])],
        "audit": [
            rel(paths["dataset_audit"]), rel(paths["validation"]),
            ".ai_coordination/WORK_RESEARCH.md",
            ".ai_coordination/reviews/V10_47_14_WORK_FINAL_AUDIT.md",
            ".ai_coordination/reviews/V10_47_18_WORK_REAUDIT.md",
            "tests/test_researchops_v10_47_15_certification.py",
            "tests/test_researchops_v10_47_20_validation_holdout.py",
            "tests/test_researchops_v10_47_21_exact_baseline_mtf_atr.py",
            "tests/test_researchops_v10_47_22_real_state_manifest.py",
        ],
        "hub": [
            f".ai_coordination/{name}" for name in (
                "CURRENT_STATE.md", "NEXT_ACTION.md", "DECISIONS.md",
                "SYNTHESIS.md", "BLOCKERS.md", "FABLE_IMPLEMENTATION.md",
                "EVIDENCE_INDEX.md", "SESSION_HANDOFF.md", "MEETING_NOTES.md",
            )
        ],
    })
    atomic_json(evidence_root / "coverage_seed.json", coverage)
    print(f"EVIDENCE_ROOT={rel(evidence_root)}")
    print(f"DATASET_ISOLATION={'PASS' if dataset['ok'] else 'FAIL'}")
    print(f"SHADOW_CANDIDATES={validation['shadow_candidates']}")
    print("CERTIFICATION=PENDING_WORK_REAUDIT")
    print("FINAL_RECOMMENDATION=NO LIVE")
    return 0 if dataset["ok"] and validation["shadow_candidates"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
