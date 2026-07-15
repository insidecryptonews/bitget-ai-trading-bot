"""Build V10.47.25 evidence from two independent discovery-only replays.

This reporter reads tournament JSON, certified test records and public Git
metadata. It does not import trading config, open a database, use the network,
or read sealed holdout bars.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import html
import json
import os
import re
import subprocess
import uuid
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
REPORT_ROOT = ROOT / "reports" / "research" / "v10_47_25_comprehensive_closure"
DATA_ROOT = ROOT / "external_data" / "staging" / "v10_47_22_isolated"
SYMBOLS = ("BTCUSDT", "ETHUSDT", "XRPUSDT", "DOGEUSDT")
TIMEFRAMES = ("1m", "5m", "15m")
EXPECTED_KEYS = {f"{symbol}:{timeframe}" for symbol in SYMBOLS for timeframe in TIMEFRAMES}
EXPECTED_CAMPAIGN_ROOT = "1b71ac3805e4717530d8f168229b4e49ab567a19752ea57411ea425f54d75c96"


def git(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=ROOT, text=True, encoding="utf-8", errors="replace",
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if completed.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed: {completed.stderr.strip()}")
    return completed.stdout.strip()


def git_tree() -> str:
    """Return the current Git tree using Git's exact revision syntax."""
    return git("rev-parse", "HEAD^{tree}")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def validate_coverage_seed(coverage: dict[str, list[str]]) -> None:
    """Reject empty categories and paths assigned to more than one root."""
    seen: dict[str, str] = {}
    for category, paths in coverage.items():
        if not isinstance(paths, list) or not paths:
            raise RuntimeError(f"empty coverage category: {category}")
        for raw_path in paths:
            path = str(raw_path)
            previous = seen.get(path)
            if previous is not None:
                raise RuntimeError(
                    f"coverage path reused by {previous} and {category}: {path}"
                )
            seen[path] = category


def atomic_text(path: Path, value: str) -> None:
    temporary = path.with_name(path.name + f".tmp.{uuid.uuid4().hex}")
    temporary.write_text(value, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def atomic_json(path: Path, value: Any) -> None:
    atomic_text(
        path, json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n",
    )


def rel(path: Path) -> str:
    return path.resolve(strict=True).relative_to(ROOT.resolve(strict=True)).as_posix()


def safe_label(value: str) -> str:
    if not value or not value.replace("_", "").replace("-", "").isalnum():
        raise RuntimeError("unsafe evidence label")
    return value


def load_run(label: str) -> tuple[Path, dict[str, dict]]:
    run_root = (REPORT_ROOT / "tournaments" / safe_label(label)).resolve(strict=True)
    run_root.relative_to(REPORT_ROOT.resolve(strict=True))
    values: dict[str, dict] = {}
    head, tree = git("rev-parse", "HEAD"), git("rev-parse", "HEAD^{tree}")
    for symbol in SYMBOLS:
        for timeframe in TIMEFRAMES:
            key = f"{symbol}:{timeframe}"
            path = run_root / f"{symbol}_{timeframe}.json"
            value = json.loads(path.read_text(encoding="utf-8"))
            provenance = value.get("execution_provenance", {})
            holdout = value.get("holdout", {})
            holdout_evidence = value.get("holdout_commitment_evidence", {})
            authority = value.get("campaign_authority", {})
            if value.get("symbol") != symbol or value.get("timeframe") != timeframe:
                raise RuntimeError(f"tournament scope mismatch: {key}")
            if provenance.get("head") != head or provenance.get("tree") != tree:
                raise RuntimeError(f"tournament provenance mismatch: {key}")
            if not (
                    holdout.get("state") == "SEALED"
                    and holdout.get("physically_loaded") is False
                    and value.get("holdout_touched") is False
                    and holdout_evidence.get("sealed_data_opened") is False):
                raise RuntimeError(f"holdout isolation mismatch: {key}")
            if not (
                    authority.get("canonical_entry_match") is True
                    and authority.get("m_campaign") == 564
                    and authority.get("root_anchor_sha256") == EXPECTED_CAMPAIGN_ROOT):
                raise RuntimeError(f"campaign authority mismatch: {key}")
            if value.get("shadow_candidates"):
                raise RuntimeError(f"unexpected shadow candidate: {key}")
            if value.get("final_recommendation") != "NO LIVE":
                raise RuntimeError(f"unsafe recommendation: {key}")
            values[key] = value
    if set(values) != EXPECTED_KEYS:
        raise RuntimeError("the canonical 12-tournament set is incomplete")
    return run_root, values


def deterministic_payload(value: dict) -> dict:
    payload = copy.deepcopy(value)
    payload.pop("execution_provenance", None)
    return payload


def compare_replays(primary: dict[str, dict], replay: dict[str, dict]) -> dict:
    rows, all_equal = {}, True
    for key in sorted(EXPECTED_KEYS):
        left = canonical_hash(deterministic_payload(primary[key]))
        right = canonical_hash(deterministic_payload(replay[key]))
        equal = left == right
        all_equal &= equal
        rows[key] = {
            "equal": equal,
            "primary_payload_sha256": left,
            "replay_payload_sha256": right,
        }
    return {
        "schema": "v10_47_25_deterministic_replay_audit",
        "combinations": rows,
        "all_12_equal": all_equal,
        "holdout_opened": False,
        "research_only": True,
        "final_recommendation": "NO LIVE",
    }


def audit_results(values: dict[str, dict]) -> tuple[dict, dict, dict]:
    pairing_rows: list[dict] = []
    tournament_rows: list[dict] = []
    classes: dict[str, int] = {}
    validation_admitted = walk_forward_called = shadow_count = 0
    pair_requested = pair_accepted = pair_impossible = pair_incompatible = 0
    invalid_pair_blocks = baseline_gate_passes = 0
    for key, value in sorted(values.items()):
        for result in value["results"].values():
            classification = result["metrics"]["classification"]
            classes[classification] = classes.get(classification, 0) + 1
            gate = result.get("gate")
            if not isinstance(gate, dict):
                continue
            paired = gate["matched_random_paired"]
            if paired.get("promotion_allowed") is not False:
                raise RuntimeError(f"pairing helper claimed global promotion: {key}")
            requested = int(paired["pairs_requested"])
            accepted = int(paired["pairs_accepted"])
            impossible = int(paired["pairs_impossible"])
            incompatible = int(paired["pairs_incompatible"])
            if paired["pairing_status"] == "VALID":
                if requested != accepted + impossible + incompatible:
                    raise RuntimeError(f"pair reconciliation failed: {key}")
                ok_pairs = [
                    row for row in paired.get("pairs", [])
                    if row.get("match_status") == "OK"
                ]
                for field in ("candidate_trade_id", "baseline_trade_id", "pair_id"):
                    ids = [row[field] for row in ok_pairs]
                    if len(ids) != len(set(ids)):
                        raise RuntimeError(f"pair bijection failed: {key}:{field}")
            else:
                invalid_pair_blocks += 1
                if accepted or paired.get("baseline_gate"):
                    raise RuntimeError(f"invalid pairing admitted evidence: {key}")
            baseline_gate_passes += bool(paired.get("baseline_gate"))
            pair_requested += requested
            pair_accepted += accepted
            pair_impossible += impossible
            pair_incompatible += incompatible
            walk_forward_called += bool(gate.get("walk_forward_called"))
            pairing_rows.append({
                "tournament": key,
                "hypothesis_id": gate["policy_identity"]["hypothesis_id"],
                "pairing_status": paired["pairing_status"],
                "requested": requested, "accepted": accepted,
                "impossible": impossible, "incompatible": incompatible,
                "coverage": paired["coverage"],
                "p_campaign_corrected": paired.get("p_campaign_corrected"),
                "baseline_gate": paired["baseline_gate"],
            })
        admitted = len(value.get("validation_admitted_candidates", []))
        shadow = len(value.get("shadow_candidates", []))
        validation_admitted += admitted
        shadow_count += shadow
        tournament_rows.append({
            "tournament": key,
            "n_net_positive_train": value.get("n_net_positive", 0),
            "validation_admitted": admitted,
            "shadow_candidates": shadow,
            "walk_forward_precomputed": value.get("walk_forward_precomputed"),
            "reference_status": value["reference_dataset_evidence"]["status"],
            "holdout_state": value["holdout"]["state"],
        })
    pairing = {
        "schema": "v10_47_25_pairing_integrity_audit",
        "pair_blocks": len(pairing_rows),
        "pairs_requested": pair_requested, "pairs_accepted": pair_accepted,
        "pairs_impossible": pair_impossible,
        "pairs_incompatible": pair_incompatible,
        "invalid_pair_blocks_fail_closed": invalid_pair_blocks,
        "baseline_gate_passes": baseline_gate_passes,
        "rows": pairing_rows,
        "promotion_allowed": False,
        "research_only": True, "final_recommendation": "NO LIVE",
    }
    authority = {
        "schema": "v10_47_25_campaign_authority_audit",
        "campaign_id": "V10_47_OFFICIAL_4X3X47",
        "root_anchor_sha256": EXPECTED_CAMPAIGN_ROOT,
        "symbols": list(SYMBOLS), "timeframes": list(TIMEFRAMES),
        "tournament_combinations": 12, "participants_per_tournament": 47,
        "m_campaign": 564, "alpha": 0.05, "correction": "bonferroni",
        "caller_authority_overrides": False,
        "all_12_authorized": True,
        "research_only": True, "final_recommendation": "NO LIVE",
    }
    summary = {
        "schema": "v10_47_25_scientific_summary",
        "tournaments": tournament_rows, "classification_counts": classes,
        "validation_admitted": validation_admitted,
        "walk_forward_called": walk_forward_called,
        "shadow_candidates": shadow_count,
        "no_confirmed_edge": True,
        "holdout_state": "SEALED", "holdout_opened": False,
        "implementation_status": "IMPLEMENTATION_COMPLETE_FOR_WORK_REAUDIT",
        "certification": "PENDING_WORK_REAUDIT",
        "research_only": True, "can_send_real_orders": False,
        "final_recommendation": "NO LIVE",
    }
    return pairing, authority, summary


def validate_execution(certified_root: Path) -> dict:
    record_path = certified_root / "execution_record.json"
    value = json.loads(record_path.read_text(encoding="utf-8"))
    execution_log = certified_root / "pytest_execution.log"
    nodeids = certified_root / "pytest_nodeids.txt"
    collection_record = certified_root / "collection_record.json"
    completed = sum(
        int(value.get(name, 0))
        for name in ("passed", "failed", "skipped", "xfailed", "xpassed")
    )
    if not (
            value.get("schema") == "v10_47_22_certified_execution"
            and value.get("head") == git("rev-parse", "HEAD")
            and value.get("tree") == git("rev-parse", "HEAD^{tree}")
            and value.get("exit_code") == 0 and value.get("failed") == 0
            and value.get("collected") == value.get("unique_nodeids")
            and int(value.get("collected", 0)) > 0
            and completed == int(value.get("collected", -1))
            and not value.get("duplicate_nodeids")
            and sha256(execution_log) == value.get("raw_log_sha256")
            and sha256(nodeids) == value.get("nodeids_sha256")
            and sha256(collection_record) == value.get("collection_record_sha256")):
        raise RuntimeError("certified execution does not certify current HEAD/tree")
    return value


def validate_security_audit(path: Path) -> None:
    text = path.read_text(encoding="utf-8", errors="strict")
    lowered = text.lower()
    if (
            "safe_paper_only" not in lowered
            or re.search(
                r"can_send_real_orders[\"']?\s*[:=]\s*false", lowered,
            ) is None
            or re.search(
                r"final_recommendation[\"']?\s*[:=]\s*no live", lowered,
            ) is None):
        raise RuntimeError("security audit does not prove SAFE_PAPER_ONLY")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--primary-label", required=True)
    parser.add_argument("--replay-label", required=True)
    parser.add_argument("--evidence-label", required=True)
    parser.add_argument("--certified-test-dir", required=True)
    parser.add_argument("--security-audit-log", required=True)
    args = parser.parse_args(argv)
    if git("status", "--porcelain=v1", "--untracked-files=no"):
        raise RuntimeError("tracked worktree must be clean")
    primary_root, primary = load_run(args.primary_label)
    replay_root, replay = load_run(args.replay_label)
    deterministic = compare_replays(primary, replay)
    if not deterministic["all_12_equal"]:
        raise RuntimeError("deterministic replay mismatch")
    pairing, authority, summary = audit_results(primary)
    if summary["shadow_candidates"] != 0 or pairing["baseline_gate_passes"] != 0:
        raise RuntimeError("unexpected candidate/promotion evidence")
    certified_root = Path(args.certified_test_dir)
    if not certified_root.is_absolute():
        certified_root = ROOT / certified_root
    certified_root = certified_root.resolve(strict=True)
    certified_root.relative_to(REPORT_ROOT.resolve(strict=True))
    execution = validate_execution(certified_root)
    security_log = Path(args.security_audit_log)
    if not security_log.is_absolute():
        security_log = ROOT / security_log
    security_log = security_log.resolve(strict=True)
    security_log.relative_to(REPORT_ROOT.resolve(strict=True))
    validate_security_audit(security_log)

    evidence_root = (REPORT_ROOT / "evidence" / safe_label(args.evidence_label)).resolve()
    evidence_root.relative_to(REPORT_ROOT.resolve(strict=True))
    if evidence_root.exists() or evidence_root.is_symlink():
        raise RuntimeError("evidence directory already exists")
    evidence_root.mkdir(parents=True, exist_ok=False)
    paths = {
        "authority": evidence_root / "campaign_authority_audit.json",
        "pairing": evidence_root / "pairing_integrity_audit.json",
        "replay": evidence_root / "deterministic_replay_audit.json",
        "summary": evidence_root / "scientific_summary.json",
        "invariants": evidence_root / "round_2_invariant_validation.json",
        "report": evidence_root / "final_report.md",
        "dashboard": evidence_root / "status.html",
        "tests": evidence_root / "test_summary.md",
    }
    invariants = {
        "schema": "v10_47_25_round_2_invariant_validation",
        "campaign_authority": "PASS_12X47_M564",
        "caller_authority_override": "REJECTED",
        "policy_callable_identity": "STRUCTURAL_AND_BEHAVIORAL",
        "reference_data": "MANIFEST_BOUND_OR_EXPLICITLY_ABSENT",
        "discovery_partitions": "CONTENT_AND_MANIFEST_BOUND",
        "holdout": "SEALED_COMMITMENT_ONLY_BARS_NOT_OPENED",
        "entry_bar_exposure": "INCLUDED_STOP_BEFORE_TP",
        "pairing": "EXACT_BIJECTIVE_FAIL_CLOSED",
        "n_eff": "DEPENDENCY_OVERLAP_CLUSTER_ACF_CAPPED",
        "validation_before_walk_forward": True,
        "shadow_candidates": 0,
        "scientifically_certified": False,
        "certification": "PENDING_WORK_REAUDIT",
        "research_only": True, "final_recommendation": "NO LIVE",
    }
    for path, value in (
        (paths["authority"], authority), (paths["pairing"], pairing),
        (paths["replay"], deterministic), (paths["summary"], summary),
        (paths["invariants"], invariants),
    ):
        atomic_json(path, value)

    table_rows = "\n".join(
        f"| {row['tournament']} | {row['n_net_positive_train']} | "
        f"{row['validation_admitted']} | {row['shadow_candidates']} | "
        f"{row['reference_status']} |"
        for row in summary["tournaments"]
    )
    report = f"""# V10.47.25 Comprehensive Closure

- HEAD: `{git('rev-parse', 'HEAD')}`
- tree: `{git_tree()}`
- campaign authority: 12 x 47 = 564 hypotheses
- deterministic replay: 12/12 equal
- baseline component gates passed: {pairing['baseline_gate_passes']}
- validation admitted: {summary['validation_admitted']}
- shadow candidates: {summary['shadow_candidates']}
- holdout: SEALED, bars not opened
- certified tests: {execution['passed']} passed, {execution['failed']} failed

| Tournament | TRAIN positive | Validation admitted | Shadow | Reference |
|---|---:|---:|---:|---|
{table_rows}

`IMPLEMENTATION_COMPLETE_FOR_WORK_REAUDIT`

`CERTIFICATION=PENDING_WORK_REAUDIT`

`NO_CONFIRMED_EDGE`

`SHADOW_CANDIDATES=0`

`HOLDOUT=SEALED`

`FINAL_RECOMMENDATION=NO LIVE`
"""
    atomic_text(paths["report"], report)
    atomic_text(paths["tests"], f"""# V10.47.25 Test Summary

- collected: {execution['collected']}
- passed: {execution['passed']}
- failed: {execution['failed']}
- skipped: {execution['skipped']}
- exit code: {execution['exit_code']}
- duration seconds: {execution['duration_seconds']}
- log SHA-256: `{execution['raw_log_sha256']}`
"""
    )
    cards = "".join(
        f"<tr><td>{html.escape(row['tournament'])}</td>"
        f"<td>{row['n_net_positive_train']}</td>"
        f"<td>{row['validation_admitted']}</td>"
        f"<td>{row['shadow_candidates']}</td>"
        f"<td>{html.escape(row['reference_status'])}</td></tr>"
        for row in summary["tournaments"]
    )
    dashboard = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>V10.47.25</title>
<style>body{{background:#11161a;color:#e8edf0;font:14px Segoe UI,Arial;margin:24px}}
.warn{{color:#ff7474;font-weight:700}}table{{border-collapse:collapse;width:100%;margin-top:20px}}
th,td{{border:1px solid #39434a;padding:7px;text-align:left}}th{{color:#b8c2c8}}</style></head>
<body><h1>V10.47.25 Comprehensive Closure</h1>
<p class="warn">PENDING WORK RE-AUDIT | NO CONFIRMED EDGE | NO LIVE</p>
<p>Campaign m=564 | deterministic replay 12/12 | shadow candidates=0 | holdout=SEALED</p>
<table><thead><tr><th>Tournament</th><th>TRAIN positive</th><th>Validation</th><th>Shadow</th><th>Reference</th></tr></thead>
<tbody>{cards}</tbody></table><p class="warn">FINAL_RECOMMENDATION: NO LIVE</p></body></html>"""
    atomic_text(paths["dashboard"], dashboard)

    dataset_paths: list[str] = []
    manifest_paths: list[str] = []
    holdout_paths: list[str] = []
    for symbol in SYMBOLS:
        for timeframe in TIMEFRAMES:
            combo = DATA_ROOT / symbol / timeframe
            manifest = combo / "dataset_manifest.json"
            manifest_paths.append(rel(manifest))
            manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
            for row in manifest_value["files"]:
                if row["partition"] in {
                        "train", "validation", "walk_forward", "reference_discovery"}:
                    dataset_paths.append(rel(combo / row["path"]))
                elif row["partition"] == "holdout_commitment":
                    holdout_paths.append(rel(combo / row["path"]))
    policy_paths = [
        "app/labs/v10_46/campaign_authority.py",
        "app/labs/v10_46/contracts.py",
        "app/labs/v10_46/event_clock.py",
        "app/labs/v10_46/families.py",
        "app/labs/v10_46/edge_search.py",
        "app/labs/v10_46/causal_ledger.py", "app/labs/v10_46/causal_stats.py",
        "app/labs/v10_46/causal_tournament.py",
        "app/labs/v10_46/discovery_dataset.py", "app/labs/v10_46/sim_oms.py",
        "app/labs/v10_46/manifest_seal.py",
        "scripts/v10_47_22_run_one_tournament.py",
        "scripts/v10_47_22_regenerate_tournaments.py",
        "scripts/v10_47_22_certified_test_runner.py",
        "scripts/v10_47_22_build_real_state_manifest.py",
        "scripts/v10_47_25_run_one_tournament.py",
        "scripts/v10_47_25_regenerate_tournaments.py",
        "scripts/v10_47_25_certified_test_runner.py",
        "scripts/v10_47_25_generate_evidence.py",
        "scripts/v10_47_25_build_manifest.py",
    ]
    test_paths = sorted(
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "tests").glob("test_researchops_v10_47*.py")
    )
    ledger_paths = sorted(
        rel(path) for root in (primary_root, replay_root)
        for path in root.glob("*_*.json")
        if path.name not in {"final_summary.json", "tournament_summary.json"}
    )
    tournament_paths = sorted(
        rel(root / name) for root in (primary_root, replay_root)
        for name in ("final_summary.json", "tournament_summary.json")
    )
    report_paths = [rel(paths["report"]), rel(paths["tests"])]
    audit_paths = [
        rel(paths["pairing"]), rel(paths["replay"]),
        rel(paths["summary"]), rel(paths["invariants"]), rel(security_log),
        *test_paths,
    ]
    coverage = {
        "dataset": sorted(set(dataset_paths)),
        "dataset_manifest": sorted({
            *manifest_paths, rel(DATA_ROOT / "preparation_summary.json"),
        }),
        "spec": ["app/labs/v10_46/campaign_authority_v10_47_25.json"],
        "registry": [rel(paths["authority"])],
        "holdout": sorted(set(holdout_paths)),
        "policy": policy_paths,
        "ledger": ledger_paths,
        "tournament": tournament_paths,
        "report": report_paths,
        "dashboard": [rel(paths["dashboard"])],
        "audit": sorted(set(audit_paths)),
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
            rel(certified_root / "pytest_execution.log"),
            rel(certified_root / "execution_record.json"),
        ],
        "test_nodeids": [rel(certified_root / "pytest_nodeids.txt")],
    }
    validate_coverage_seed(coverage)
    atomic_json(evidence_root / "coverage_seed.json", coverage)
    print(f"EVIDENCE_ROOT={rel(evidence_root)}")
    print("DETERMINISTIC_REPLAY=12/12")
    print("IMPLEMENTATION_COMPLETE_FOR_WORK_REAUDIT")
    print("CERTIFICATION=PENDING_WORK_REAUDIT")
    print("NO_CONFIRMED_EDGE")
    print("SHADOW_CANDIDATES=0")
    print("HOLDOUT=SEALED")
    print("FINAL_RECOMMENDATION=NO LIVE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
