"""Generate the V10.47.23 pairing/campaign evidence without reading holdout bars.

The twelve tournament processes are the only components that recompute market
results. This reporter reads their JSON outputs, the previous sealed evidence,
and certified test records. It never imports config, opens a DB, or accesses the
sealed holdout data files.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
REPORT_ROOT = ROOT / "reports" / "research" / "v10_47_23_exact_pairing"
PREVIOUS_ROOT = (
    ROOT / "reports" / "research" / "v10_47_22_real_state_certification"
)
PREVIOUS_LABEL = "work_reaudit_v10_47_22_final"
PREVIOUS_HEAD = "b85eb871bd293dd0614b7ff71c9d257a81baa2e6"
SYMBOLS = ("BTCUSDT", "ETHUSDT", "XRPUSDT", "DOGEUSDT")
TIMEFRAMES = ("1m", "5m", "15m")
FROZEN_MARKET_MODULES = (
    "app/labs/v10_46/causal_ledger.py",
    "app/labs/v10_46/discovery_dataset.py",
    "app/labs/v10_46/edge_search.py",
    "app/labs/v10_46/event_clock.py",
    "app/labs/v10_46/families.py",
    "app/labs/v10_46/sim_oms.py",
)


def git(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=ROOT, text=True, encoding="utf-8", errors="replace",
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if completed.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed: {completed.stderr.strip()}")
    return completed.stdout.strip()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def atomic_text(path: Path, value: str) -> None:
    temporary = path.with_name(path.name + f".tmp.{uuid.uuid4().hex}")
    temporary.write_text(value, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def atomic_json(path: Path, value: Any) -> None:
    atomic_text(path, json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")


def rel(path: Path) -> str:
    return path.resolve(strict=True).relative_to(ROOT.resolve(strict=True)).as_posix()


def safe_label(value: str) -> str:
    if not value or not value.replace("_", "").replace("-", "").isalnum():
        raise RuntimeError("unsafe evidence label")
    return value


def load_tournaments(label: str) -> tuple[Path, dict[str, dict]]:
    run_root = (REPORT_ROOT / "tournaments" / safe_label(label)).resolve(strict=True)
    run_root.relative_to(REPORT_ROOT.resolve(strict=True))
    head = git("rev-parse", "HEAD")
    tree = git("rev-parse", "HEAD^{tree}")
    values: dict[str, dict] = {}
    for symbol in SYMBOLS:
        for timeframe in TIMEFRAMES:
            key = f"{symbol}:{timeframe}"
            path = run_root / f"{symbol}_{timeframe}.json"
            value = json.loads(path.read_text(encoding="utf-8"))
            provenance = value.get("execution_provenance", {})
            if value.get("symbol") != symbol or value.get("timeframe") != timeframe:
                raise RuntimeError(f"tournament identity mismatch: {key}")
            if value.get("holdout", {}).get("state") != "SEALED":
                raise RuntimeError(f"holdout not sealed: {key}")
            if provenance.get("holdout_data_loaded") is not False:
                raise RuntimeError(f"holdout data entered process: {key}")
            if provenance.get("head") != head or provenance.get("tree") != tree:
                raise RuntimeError(f"tournament provenance mismatch: {key}")
            if value.get("final_recommendation") != "NO LIVE":
                raise RuntimeError(f"unsafe recommendation: {key}")
            values[key] = value
    return run_root, values


def _gate_rows(tournaments: dict[str, dict]) -> list[dict]:
    rows = []
    for tournament, value in sorted(tournaments.items()):
        for policy, result in sorted(value.get("results", {}).items()):
            gate = result.get("gate")
            if isinstance(gate, dict):
                rows.append({
                    "tournament": tournament,
                    "policy": policy,
                    "gate": gate,
                    "paired": gate["matched_random_paired"],
                })
    return rows


def pairing_campaign_audit(tournaments: dict[str, dict]) -> dict:
    gate_rows = _gate_rows(tournaments)
    campaign_shas = {
        value.get("campaign_registry", {}).get("campaign_registry_sha")
        for value in tournaments.values()
    }
    campaign_shas.discard(None)
    if len(campaign_shas) != 1:
        raise RuntimeError("the twelve tournaments do not share one campaign registry")
    first_campaign = next(iter(tournaments.values()))["campaign_registry"]
    contract = first_campaign["campaign_registry_contract"]
    if canonical_hash(contract) != first_campaign["campaign_registry_sha"]:
        raise RuntimeError("campaign registry SHA mismatch")

    requested = accepted = impossible = incompatible = 0
    invalid_blocks = duplicate_candidates = duplicate_baselines = duplicate_pairs = 0
    accepted_rows: list[dict] = []
    table_rows = []
    minimum_campaign_p = None
    for item in gate_rows:
        paired = item["paired"]
        requested += int(paired["pairs_requested"])
        accepted += int(paired["pairs_accepted"])
        impossible += int(paired["pairs_impossible"])
        incompatible += int(paired["pairs_incompatible"])
        duplicate_candidates += int(paired["duplicate_candidate_ids"])
        duplicate_baselines += int(paired["duplicate_baseline_ids"])
        duplicate_pairs += int(paired["duplicate_pair_ids"])
        invalid_blocks += paired["pairing_status"] != "VALID"
        p_campaign = paired.get("p_campaign_corrected")
        if p_campaign is not None:
            minimum_campaign_p = (
                float(p_campaign) if minimum_campaign_p is None
                else min(minimum_campaign_p, float(p_campaign))
            )
        ok_pairs = [row for row in paired.get("pairs", []) if row.get("match_status") == "OK"]
        candidate_ids = [row["candidate_trade_id"] for row in ok_pairs]
        baseline_ids = [row["baseline_trade_id"] for row in ok_pairs]
        pair_ids = [row["pair_id"] for row in ok_pairs]
        if not (
                len(candidate_ids) == len(set(candidate_ids))
                and len(baseline_ids) == len(set(baseline_ids))
                and len(pair_ids) == len(set(pair_ids))
                and len(ok_pairs) == int(paired["unique_pair_ids"])):
            raise RuntimeError(
                f"non-bijective accepted pairs: {item['tournament']}:{item['policy']}"
            )
        accepted_rows.extend({
            "tournament": item["tournament"], "policy": item["policy"], **row,
        } for row in ok_pairs)
        table_rows.append({
            "tournament": item["tournament"],
            "policy": item["policy"],
            "requested": paired["pairs_requested"],
            "accepted": paired["pairs_accepted"],
            "impossible": paired["pairs_impossible"],
            "incompatible": paired["pairs_incompatible"],
            "coverage": paired["coverage"],
            "pairing_status": paired["pairing_status"],
            "integrity_status": paired["integrity_status"],
            "p_raw": paired["p_raw"],
            "p_tournament_corrected": paired["p_tournament_corrected"],
            "p_campaign_corrected": paired["p_campaign_corrected"],
            "baseline_gate": paired["baseline_gate"],
        })

    reconciled = requested == accepted + impossible + incompatible
    if not reconciled or any((invalid_blocks, duplicate_candidates,
                              duplicate_baselines, duplicate_pairs)):
        raise RuntimeError("pairing integrity/reconciliation failed")
    global_candidate_ids = [row["candidate_trade_id"] for row in accepted_rows]
    global_baseline_ids = [row["baseline_trade_id"] for row in accepted_rows]
    global_pair_ids = [row["pair_id"] for row in accepted_rows]
    shadow = sum(len(value.get("shadow_candidates", [])) for value in tournaments.values())
    admitted = sum(len(value.get("validation_admitted_candidates", []))
                   for value in tournaments.values())
    return {
        "schema": "v10_47_23_pairing_campaign_audit",
        "pair_blocks": len(gate_rows),
        "pairs_requested": requested,
        "pairs_accepted": accepted,
        "pairs_impossible": impossible,
        "pairs_incompatible": incompatible,
        "pairs_invalid": 0,
        "reconciled": reconciled,
        "duplicate_candidate_ids_within_evaluation": duplicate_candidates,
        "duplicate_baseline_ids_within_evaluation": duplicate_baselines,
        "duplicate_pair_ids_within_evaluation": duplicate_pairs,
        "invalid_pairing_blocks": invalid_blocks,
        "accepted_rows": accepted_rows,
        "accepted_rows_global": len(accepted_rows),
        "accepted_unique_candidate_ids_global": len(set(global_candidate_ids)),
        "accepted_unique_baseline_ids_global": len(set(global_baseline_ids)),
        "accepted_unique_pair_ids_global": len(set(global_pair_ids)),
        "global_identity_scope_note": (
            "Bijection is enforced per policy evaluation. P11 and P11_SHORT may "
            "observe the same market trade; campaign m remains nominal to avoid "
            "anti-conservative semantic deduplication."
        ),
        "campaign_registry_sha": first_campaign["campaign_registry_sha"],
        "m_campaign_nominal": first_campaign["m_campaign_nominal"],
        "m_campaign_unique_hypotheses": first_campaign["m_campaign_unique_hypotheses"],
        "m_campaign_unique_results": first_campaign["m_campaign_unique_results"],
        "m_campaign_effective_for_gate": first_campaign["m_campaign_effective_for_gate"],
        "correction_method": first_campaign["correction_method"],
        "alpha": first_campaign["alpha"],
        "minimum_p_campaign_corrected": minimum_campaign_p,
        "validation_admitted": admitted,
        "shadow_candidates": shadow,
        "table_rows": table_rows,
        "research_only": True,
        "can_send_real_orders": False,
        "final_recommendation": "NO LIVE",
    }


def _market_payload(value: dict) -> dict:
    payload = {}
    for policy, result in sorted(value.get("results", {}).items()):
        row = {
            "metrics": result.get("metrics"),
            "ledger_integrity": result.get("ledger_integrity"),
        }
        gate = result.get("gate")
        if gate:
            row["candidate_evaluation_without_pairing"] = {
                key: gate.get(key) for key in (
                    "selection_metrics", "conservative_net_eur",
                    "validation_net_eur", "validation_trades",
                    "validation_metrics", "validation_gate",
                    "validation_rejection_reason", "walk_forward_called",
                    "walk_forward_metrics", "walk_forward_net_eur",
                )
            }
        payload[policy] = row
    return payload


def deterministic_reproduction_audit(tournaments: dict[str, dict], run_root: Path) -> dict:
    previous_tournaments = PREVIOUS_ROOT / "tournaments" / PREVIOUS_LABEL
    combinations = {}
    all_equal = True
    for key, current in sorted(tournaments.items()):
        symbol, timeframe = key.split(":")
        source_path = previous_tournaments / f"{symbol}_{timeframe}.json"
        current_path = run_root / f"{symbol}_{timeframe}.json"
        source = json.loads(source_path.read_text(encoding="utf-8"))
        source_hash = canonical_hash(_market_payload(source))
        current_hash = canonical_hash(_market_payload(current))
        equal = source_hash == current_hash
        all_equal &= equal
        combinations[key] = {
            "market_payload_equal": equal,
            "source_market_payload_sha256": source_hash,
            "current_market_payload_sha256": current_hash,
            "source_output_sha256": sha256(source_path),
            "current_output_sha256": sha256(current_path),
        }
    module_proofs = {}
    for path in FROZEN_MARKET_MODULES:
        source_blob = git("rev-parse", f"{PREVIOUS_HEAD}:{path}")
        current_blob = git("rev-parse", f"HEAD:{path}")
        module_proofs[path] = {
            "source_blob": source_blob,
            "current_blob": current_blob,
            "unchanged": source_blob == current_blob,
        }
        all_equal &= source_blob == current_blob
    return {
        "schema": "v10_47_23_deterministic_reproduction_audit",
        "source_head": PREVIOUS_HEAD,
        "current_head": git("rev-parse", "HEAD"),
        "combinations": combinations,
        "frozen_market_module_proofs": module_proofs,
        "market_payloads_equal_across_all_12": all_equal,
        "artifact_disposition": {
            "datasets": "REUSED_WITH_HASH_PROOF",
            "mtf_outputs": "REUSED_WITH_HASH_PROOF",
            "signal_trade_and_ledger_outputs": "RECOMPUTED",
            "signal_trade_and_ledger_reason": (
                "Archived tournament JSON did not retain every raw random-baseline "
                "trade required to independently recompute exact matching."
            ),
            "market_payload_comparison": "RECOMPUTED_AND_MATCHED_SOURCE",
            "old_pairing_and_promotion_gates": "INVALIDATED",
            "matching_pair_ids_lower_bounds_p_values_and_gates": "RECOMPUTED",
            "holdout": "NOT_APPLICABLE_SEALED_NOT_OPENED",
        },
        "research_only": True,
        "final_recommendation": "NO LIVE",
    }


def _previous_coverage() -> dict[str, list[dict]]:
    manifest = json.loads((
        PREVIOUS_ROOT / "manifests" / PREVIOUS_LABEL / "output_manifest.json"
    ).read_text(encoding="utf-8"))
    return manifest["payload"]["coverage"]


def _assert_previous_artifact(path: Path, expected_sha: str) -> None:
    if sha256(path) != expected_sha:
        raise RuntimeError(f"previous sealed artifact changed: {path}")


def _markdown_table(rows: list[dict]) -> str:
    lines = [
        "| Tournament | Policy | Requested | Accepted | Impossible | Incompatible | p campaign |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            f"| {row['tournament']} | {row['policy']} | {row['requested']} | "
            f"{row['accepted']} | {row['impossible']} | {row['incompatible']} | "
            f"{row['p_campaign_corrected']} |"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tournament-label", required=True)
    parser.add_argument("--evidence-label", required=True)
    parser.add_argument("--certified-test-dir", required=True)
    parser.add_argument("--security-audit-log", required=True)
    args = parser.parse_args(argv)
    if git("status", "--porcelain=v1", "--untracked-files=no"):
        raise RuntimeError("tracked worktree must be clean")
    run_root, tournaments = load_tournaments(args.tournament_label)
    audit = pairing_campaign_audit(tournaments)
    reproduction = deterministic_reproduction_audit(tournaments, run_root)
    if not reproduction["market_payloads_equal_across_all_12"]:
        raise RuntimeError("market payload deterministic comparison failed")

    evidence_root = (REPORT_ROOT / "evidence" / safe_label(args.evidence_label)).resolve()
    evidence_root.relative_to(REPORT_ROOT.resolve())
    if evidence_root.exists() or evidence_root.is_symlink():
        raise RuntimeError("evidence directory already exists")
    evidence_root.mkdir(parents=True, exist_ok=False)

    certified_root = Path(args.certified_test_dir)
    if not certified_root.is_absolute():
        certified_root = ROOT / certified_root
    certified_root = certified_root.resolve(strict=True)
    certified_root.relative_to(REPORT_ROOT.resolve(strict=True))
    execution_path = certified_root / "execution_record.json"
    execution = json.loads(execution_path.read_text(encoding="utf-8"))
    if (
            execution.get("head") != git("rev-parse", "HEAD")
            or execution.get("tree") != git("rev-parse", "HEAD^{tree}")
            or execution.get("exit_code") != 0
            or execution.get("failed") != 0
            or execution.get("collected") != execution.get("unique_nodeids")):
        raise RuntimeError("certified execution is not valid for current HEAD/tree")

    campaign = next(iter(tournaments.values()))["campaign_registry"]
    specs = {
        key: value["registry"]["baseline_policy_spec"]
        for key, value in sorted(tournaments.items())
    }
    registries = {
        "campaign": campaign,
        "tournaments": {
            key: value["registry"] for key, value in sorted(tournaments.items())
        },
    }
    ledgers = {
        key: {
            policy: result["ledger_integrity"]
            for policy, result in sorted(value["results"].items())
        }
        for key, value in sorted(tournaments.items())
    }
    paths = {
        "pairing_audit": evidence_root / "pairing_integrity_audit.json",
        "campaign": evidence_root / "campaign_registry.json",
        "reproduction": evidence_root / "deterministic_reproduction_audit.json",
        "reuse": evidence_root / "reuse_decisions.json",
        "spec": evidence_root / "spec_index.json",
        "registry": evidence_root / "registry_index.json",
        "ledger": evidence_root / "ledger_index.json",
        "pairing_report": evidence_root / "pairing_integrity_report.md",
        "campaign_report": evidence_root / "campaign_multiple_testing_report.md",
        "twelve_report": evidence_root / "twelve_tournament_recalculation.md",
        "final_report": evidence_root / "final_report.md",
        "legacy_final_report": evidence_root / "FINAL_REPORT.md",
        "test_report": evidence_root / "test_summary.md",
        "git_report": evidence_root / "git_summary.md",
        "dashboard": evidence_root / "status.html",
    }
    atomic_json(paths["pairing_audit"], audit)
    atomic_json(paths["campaign"], campaign)
    atomic_json(paths["reproduction"], reproduction)
    atomic_json(paths["reuse"], reproduction["artifact_disposition"])
    atomic_json(paths["spec"], {"schema": "v10_47_23_spec_index", "items": specs})
    atomic_json(paths["registry"], {"schema": "v10_47_23_registry_index", **registries})
    atomic_json(paths["ledger"], {"schema": "v10_47_23_ledger_index", "items": ledgers})

    table = _markdown_table(audit["table_rows"])
    pairing_text = f"""# V10.47.23 Pairing Integrity

- Pair blocks: {audit['pair_blocks']}
- Requested: {audit['pairs_requested']}
- Accepted exact pairs: {audit['pairs_accepted']}
- Impossible: {audit['pairs_impossible']}
- Incompatible: {audit['pairs_incompatible']}
- Duplicate candidate IDs within an evaluation: {audit['duplicate_candidate_ids_within_evaluation']}
- Duplicate baseline IDs within an evaluation: {audit['duplicate_baseline_ids_within_evaluation']}
- Duplicate pair IDs within an evaluation: {audit['duplicate_pair_ids_within_evaluation']}
- Reconciled: {str(audit['reconciled']).lower()}

The bijection scope is one policy evaluation. Across the four accepted rows there
are {audit['accepted_unique_candidate_ids_global']} global candidate identities,
because P11 and P11_SHORT observe one shared ETH trade. This is not silently
deduplicated for campaign correction; the conservative nominal family is used.

{table}

`NO_CONFIRMED_EDGE` / `SHADOW_CANDIDATES=0` / `FINAL_RECOMMENDATION=NO LIVE`
"""
    campaign_text = f"""# V10.47.23 Campaign-Wide Multiple Testing

- Registry SHA: `{audit['campaign_registry_sha']}`
- Nominal hypotheses: {audit['m_campaign_nominal']}
- Unique hypotheses: {audit['m_campaign_unique_hypotheses']}
- Diagnostic unique results: {audit['m_campaign_unique_results']}
- Effective m for promotion gate: {audit['m_campaign_effective_for_gate']}
- Method: {audit['correction_method']}
- Alpha: {audit['alpha']}
- Minimum campaign-corrected p-value: {audit['minimum_p_campaign_corrected']}

The local tournament correction (`m_tournament=47`) is diagnostic only. Promotion
uses `p_campaign_corrected=min(1,p_raw*564)`. Semantic equivalence across symbol/
timeframe evaluations is not proven, so the campaign never reduces to 47 or 540.

No baseline gate passed. No policy reached shadow. `FINAL_RECOMMENDATION=NO LIVE`.
"""
    twelve_lines = [
        "# V10.47.23 Twelve-Tournament Recalculation",
        "",
        "| Tournament | TRAIN positive | Gates | Validation admitted | Shadow |",
        "|---|---:|---:|---:|---:|",
    ]
    for key, value in sorted(tournaments.items()):
        twelve_lines.append(
            f"| {key} | {value.get('n_net_positive', 0)} | "
            f"{sum('gate' in row for row in value['results'].values())} | "
            f"{len(value.get('validation_admitted_candidates', []))} | "
            f"{len(value.get('shadow_candidates', []))} |"
        )
    twelve_lines.extend([
        "", "Artifact disposition is recorded in `reuse_decisions.json`.",
        "Signals/trades/ledgers were recomputed because archived outputs did not",
        "retain every raw baseline trade needed for an independent exact rematch.",
        "The recomputed non-pairing market payload matches the prior 12 outputs.",
        "", "HOLDOUT=SEALED", "FINAL_RECOMMENDATION=NO LIVE",
    ])
    twelve_text = "\n".join(twelve_lines) + "\n"
    final_text = f"""# V10.47.23 Final Builder Report

- HEAD: `{git('rev-parse', 'HEAD')}`
- tree: `{git('rev-parse', 'HEAD^{tree}')}`
- IMPLEMENTATION_COMPLETE_FOR_WORK_REAUDIT
- CERTIFICATION=PENDING_WORK_REAUDIT
- NO_CONFIRMED_EDGE
- SHADOW_CANDIDATES={audit['shadow_candidates']}
- HOLDOUT=SEALED
- exact one-to-one pairing: PASS
- campaign-wide m: {audit['m_campaign_effective_for_gate']}
- 311 reconciliation: {audit['pairs_accepted']} accepted + {audit['pairs_impossible']} impossible + {audit['pairs_incompatible']} incompatible
- certified suite: {execution['passed']} passed, {execution['failed']} failed, exit {execution['exit_code']}

This is implementation evidence for independent Work re-audit, not self-certification.
No strategy is promoted and no holdout data was opened.

FINAL_RECOMMENDATION=NO LIVE
"""
    test_text = f"""# V10.47.23 Test Summary

- branch: `{execution['branch']}`
- HEAD: `{execution['head']}`
- tree: `{execution['tree']}`
- collected unique nodeids: {execution['unique_nodeids']}
- duplicate nodeids: {len(execution['duplicate_nodeids'])}
- passed: {execution['passed']}
- failed: {execution['failed']}
- skipped: {execution['skipped']}
- exit code: {execution['exit_code']}
- duration seconds: {execution['duration_seconds']}
- raw log SHA-256: `{execution['raw_log_sha256']}`

The pre-fix RED log remains separately preserved. Certification remains pending Work.
"""
    status = git("status", "--porcelain=v1")
    git_text = f"""# V10.47.23 Git Summary

- branch: `{git('branch', '--show-current')}`
- HEAD: `{git('rev-parse', 'HEAD')}`
- tree: `{git('rev-parse', 'HEAD^{tree}')}`
- origin/main: `{git('rev-parse', 'origin/main')}`
- tracked clean: `{str(not bool(git('status', '--porcelain=v1', '--untracked-files=no'))).lower()}`

Untracked exclusions (not committed):

```text
{status or '(none)'}
```

NO PUSH was performed by this evidence generator.
"""
    dashboard_rows = "".join(
        "<tr>" + "".join(
            f"<td>{html.escape(str(row[field]))}</td>" for field in (
                "tournament", "policy", "requested", "accepted",
                "impossible", "incompatible", "p_campaign_corrected",
            )
        ) + "</tr>" for row in audit["table_rows"]
    )
    dashboard = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>V10.47.23 Pairing Status</title><style>
body{{margin:0;background:#111417;color:#e7ebee;font:14px Segoe UI,Arial,sans-serif}}
header,main{{padding:18px 24px}}header{{border-bottom:1px solid #374047}}h1{{font-size:22px}}
.danger{{color:#ff7474;font-weight:700}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:10px}}
.card{{border:1px solid #374047;background:#181d21;padding:13px;border-radius:6px}}table{{margin-top:20px;width:100%;border-collapse:collapse}}
th,td{{border:1px solid #374047;padding:7px;text-align:left}}th{{color:#aeb8bf}}
</style></head><body><header><h1>V10.47.23 Exact Pairing + Campaign FWER</h1>
<div class="danger">PENDING WORK RE-AUDIT | NO CONFIRMED EDGE | NO LIVE</div></header><main>
<div class="grid"><div class="card"><b>Exact one-to-one</b><br>PASS per evaluation</div>
<div class="card"><b>Duplicate candidates / baselines / pairs</b><br>0 / 0 / 0</div>
<div class="card"><b>Campaign m</b><br>{audit['m_campaign_effective_for_gate']}</div>
<div class="card"><b>Campaign min p</b><br>{audit['minimum_p_campaign_corrected']}</div>
<div class="card"><b>Reconciliation</b><br>{audit['pairs_accepted']} + {audit['pairs_impossible']} + {audit['pairs_incompatible']} = {audit['pairs_requested']}</div>
<div class="card"><b>Shadow candidates</b><br>{audit['shadow_candidates']}</div></div>
<table><thead><tr><th>Tournament</th><th>Policy</th><th>Requested</th><th>Accepted</th><th>Impossible</th><th>Incompatible</th><th>p campaign</th></tr></thead>
<tbody>{dashboard_rows}</tbody></table><p class="danger">FINAL_RECOMMENDATION: NO LIVE</p></main></body></html>"""
    for path, text_value in (
        (paths["pairing_report"], pairing_text),
        (paths["campaign_report"], campaign_text),
        (paths["twelve_report"], twelve_text),
        (paths["final_report"], final_text),
        (paths["legacy_final_report"], final_text),
        (paths["test_report"], test_text),
        (paths["git_report"], git_text),
        (paths["dashboard"], dashboard),
    ):
        atomic_text(path, text_value)

    previous = _previous_coverage()
    previous_dataset_audit = (
        PREVIOUS_ROOT / "evidence" / PREVIOUS_LABEL / "dataset_separation_audit.json"
    )
    previous_mtf = PREVIOUS_ROOT / "mtf" / PREVIOUS_LABEL / "mtf_summary.json"
    for prior_path in (previous_dataset_audit, previous_mtf):
        records = [
            row for rows in previous.values() for row in rows
            if row["path"] == rel(prior_path)
        ]
        if len(records) != 1:
            raise RuntimeError(f"prior artifact absent from sealed manifest: {prior_path}")
        _assert_previous_artifact(prior_path, records[0]["sha256"])

    security_log = Path(args.security_audit_log)
    if not security_log.is_absolute():
        security_log = ROOT / security_log
    security_log = security_log.resolve(strict=True)
    security_log.relative_to(REPORT_ROOT.resolve(strict=True))
    log_paths = sorted((REPORT_ROOT / "logs").glob("*"))
    report_paths = sorted({
        path for name, path in paths.items() if name.endswith("report")
    })
    mtf_paths = [
        ROOT / row["path"] for row in previous["tournament"]
        if f"/mtf/{PREVIOUS_LABEL}/" in row["path"]
    ]
    coverage = {
        "dataset": [row["path"] for row in previous["dataset"]],
        "dataset_manifest": [row["path"] for row in previous["dataset_manifest"]],
        # Commitment metadata is covered; sealed bars are deliberately not opened.
        "holdout": [
            row["path"] for row in previous["holdout"]
            if row["path"].endswith("/commitment.json")
        ],
        "spec": [rel(paths["spec"])],
        "registry": [rel(paths["registry"]), rel(paths["campaign"])],
        "policy": [
            "app/labs/v10_46/causal_stats.py",
            "app/labs/v10_46/causal_tournament.py",
            "app/labs/v10_46/manifest_seal.py",
            "scripts/v10_47_22_build_real_state_manifest.py",
            "scripts/v10_47_22_certified_test_runner.py",
            "scripts/v10_47_22_regenerate_tournaments.py",
            "scripts/v10_47_22_run_one_tournament.py",
            "scripts/v10_47_23_build_manifest.py",
            "scripts/v10_47_23_certified_test_runner.py",
            "scripts/v10_47_23_generate_evidence.py",
            "scripts/v10_47_23_regenerate_tournaments.py",
            "scripts/v10_47_23_run_one_tournament.py",
        ],
        "ledger": [rel(paths["ledger"])],
        "tournament": sorted(
            [rel(path) for path in run_root.glob("*.json")]
            + [rel(path) for path in mtf_paths]
        ),
        "report": sorted(rel(path) for path in report_paths),
        "dashboard": [rel(paths["dashboard"])],
        "audit": sorted({
            rel(paths["pairing_audit"]), rel(paths["reproduction"]),
            rel(paths["reuse"]), rel(previous_dataset_audit), rel(security_log),
            ".ai_coordination/WORK_RESEARCH.md",
            ".ai_coordination/reviews/V10_47_22_WORK_FINAL_REAUDIT.md",
            "tests/test_researchops_v10_47_15_certification.py",
            "tests/test_researchops_v10_47_20_validation_holdout.py",
            "tests/test_researchops_v10_47_21_exact_baseline_mtf_atr.py",
            "tests/test_researchops_v10_47_23_bijective_pairing_campaign.py",
            *(rel(path) for path in log_paths if path.is_file()),
        }),
        "hub": [
            f".ai_coordination/{name}" for name in (
                "CURRENT_STATE.md", "NEXT_ACTION.md", "DECISIONS.md",
                "SYNTHESIS.md", "BLOCKERS.md", "SESSION_HANDOFF.md",
                "EVIDENCE_INDEX.md", "MEETING_NOTES.md",
            )
        ],
        "collection_log": [
            rel(certified_root / "pytest_collection.log"),
            rel(certified_root / "collection_record.json"),
        ],
        "execution_log": [
            rel(certified_root / "pytest_execution.log"), rel(execution_path),
        ],
        "test_nodeids": [rel(certified_root / "pytest_nodeids.txt")],
    }
    if not coverage["holdout"]:
        raise RuntimeError("sealed holdout commitments are not covered")
    atomic_json(evidence_root / "coverage_seed.json", coverage)
    print(f"EVIDENCE_ROOT={rel(evidence_root)}")
    print(f"PAIRING_RECONCILED={audit['pairs_requested']}")
    print(f"M_CAMPAIGN={audit['m_campaign_effective_for_gate']}")
    print(f"SHADOW_CANDIDATES={audit['shadow_candidates']}")
    print("CERTIFICATION=PENDING_WORK_REAUDIT")
    print("FINAL_RECOMMENDATION=NO LIVE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
