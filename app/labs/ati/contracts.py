"""Versioned ATI policy loading and fail-closed contract validation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from . import FEATURE_VERSION, POLICY_VERSION, safety_envelope

REPO_ROOT = Path(__file__).resolve().parents[3]
POLICY_DIR = REPO_ROOT / "config" / "ati"
POLICY_PATH = POLICY_DIR / "ATI_SHADOW_POLICY_V2.json"
PRIORS_PATH = POLICY_DIR / "ATI_STATISTICAL_PRIORS_V2.json"
RULE_MATRIX_PATH = POLICY_DIR / "ATI_RULE_MATRIX_V2.csv"
EXPECTED_RULES = ("SHORT_R1", "SHORT_S1", "LONG_R1", "LONG_S1")


class AtiContractError(ValueError):
    """Raised when a frozen ATI input is missing or unsafe."""


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AtiContractError(f"ATI_CONTRACT_UNREADABLE:{path.name}") from exc
    if not isinstance(value, dict):
        raise AtiContractError(f"ATI_CONTRACT_NOT_OBJECT:{path.name}")
    return value


def load_policy() -> dict[str, Any]:
    policy = _read_json(POLICY_PATH)
    if policy.get("policy_name") != POLICY_VERSION:
        raise AtiContractError("ATI_POLICY_VERSION_MISMATCH")
    if policy.get("mode") != "SHADOW_RESEARCH_ONLY":
        raise AtiContractError("ATI_POLICY_NOT_SHADOW_ONLY")
    if policy.get("can_send_real_orders") is not False:
        raise AtiContractError("ATI_POLICY_REAL_ORDERS_NOT_FALSE")
    if policy.get("paper_filter_enabled") is not False:
        raise AtiContractError("ATI_POLICY_PAPER_FILTER_NOT_FALSE")
    rules = policy.get("rules")
    if not isinstance(rules, list):
        raise AtiContractError("ATI_POLICY_RULES_MISSING")
    identifiers = tuple(str(rule.get("rule_id")) for rule in rules if isinstance(rule, dict))
    if identifiers != EXPECTED_RULES or len(set(identifiers)) != len(EXPECTED_RULES):
        raise AtiContractError("ATI_POLICY_RULE_SET_MISMATCH")
    promotion = policy.get("promotion_gate") or {}
    if promotion.get("current_decision") != "NO_LIVE":
        raise AtiContractError("ATI_POLICY_PROMOTION_NOT_BLOCKED")
    return policy


def load_priors() -> dict[str, Any]:
    priors = _read_json(PRIORS_PATH)
    if priors.get("status") != "research_prior_only":
        raise AtiContractError("ATI_PRIORS_NOT_RESEARCH_ONLY")
    if priors.get("live_decision") != "NO_LIVE":
        raise AtiContractError("ATI_PRIORS_LIVE_NOT_BLOCKED")
    return priors


def contract_receipt() -> dict[str, Any]:
    policy = load_policy()
    priors = load_priors()
    return {
        "policy_version": POLICY_VERSION,
        "feature_version": FEATURE_VERSION,
        "policy_sha256": file_sha256(POLICY_PATH),
        "priors_sha256": file_sha256(PRIORS_PATH),
        "rule_matrix_sha256": file_sha256(RULE_MATRIX_PATH),
        "rules": [rule["rule_id"] for rule in policy["rules"]],
        "priors_source": priors.get("source"),
        **safety_envelope(),
    }
