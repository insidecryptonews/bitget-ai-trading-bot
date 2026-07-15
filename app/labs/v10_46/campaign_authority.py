"""Tracked canonical authority for the V10.47 research campaign.

This module is the only source for campaign multiplicity, alpha, correction,
participants and tournament commitments. Callers identify a campaign and a
tournament; they cannot provide an alternative authority definition.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any


CAMPAIGN_ID = "V10_47_OFFICIAL_4X3X47"
AUTHORITY_FILENAME = "campaign_authority_v10_47_25.json"
EXPECTED_ROOT_ANCHOR = "2355b07492797f2065f016c4a1160a8cd933b6efe1ddafc6cbe8230d8a4aa439"
EXPECTED_SYMBOLS = ("BTCUSDT", "ETHUSDT", "XRPUSDT", "DOGEUSDT")
EXPECTED_TIMEFRAMES = ("1m", "5m", "15m")
_FULL_CONTEXT_CAPABILITY = object()


class CampaignAuthorityError(RuntimeError):
    """Fail-closed canonical authority violation."""


@dataclass(frozen=True)
class TournamentAuthorization:
    campaign_id: str
    campaign_version: str
    symbol: str
    timeframe: str
    entry: dict[str, Any]
    root_anchor_sha256: str
    m_tournament: int
    m_campaign: int
    alpha: float
    correction_method: str
    full_context_verified: bool
    _full_context_capability: object | None = field(
        default=None, repr=False, compare=False,
    )


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _is_sha256(value: Any) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and value == value.lower()
        and all(char in "0123456789abcdef" for char in value)
    )


def _entry_spec_hash(entry: dict[str, Any]) -> str:
    payload = {
        "schema": "v10_47_25_tournament_authority_entry",
        **{key: value for key, value in entry.items() if key != "tournament_spec_hash"},
    }
    return _canonical_hash(payload)


def _validate_authority(authority: dict[str, Any]) -> None:
    problems: list[str] = []
    root = authority.get("root_anchor_sha256")
    body = {key: value for key, value in authority.items() if key != "root_anchor_sha256"}
    if root != EXPECTED_ROOT_ANCHOR or _canonical_hash(body) != EXPECTED_ROOT_ANCHOR:
        problems.append("AUTHORITY_ROOT_ANCHOR_MISMATCH")
    expected_scalars = {
        "schema": "v10_47_25_campaign_authority",
        "campaign_id": CAMPAIGN_ID,
        "campaign_version": "10.47.25",
        "tournament_combinations": 12,
        "participants_per_tournament": 47,
        "m_campaign": 564,
        "alpha": 0.05,
        "correction_method": "bonferroni",
        "closed": True,
        "closed_before_metrics": True,
        "research_only": True,
        "final_recommendation": "NO LIVE",
    }
    for key, expected in expected_scalars.items():
        if authority.get(key) != expected:
            problems.append(f"AUTHORITY_{key.upper()}_MISMATCH")
    if tuple(authority.get("symbols", ())) != EXPECTED_SYMBOLS:
        problems.append("AUTHORITY_SYMBOLS_MISMATCH")
    if tuple(authority.get("timeframes", ())) != EXPECTED_TIMEFRAMES:
        problems.append("AUTHORITY_TIMEFRAMES_MISMATCH")
    participants = authority.get("participant_spec_hashes")
    if not isinstance(participants, dict) or len(participants) != 47:
        problems.append("AUTHORITY_PARTICIPANT_COUNT_MISMATCH")
    else:
        if any(type(name) is not str or not name.isascii() or not _is_sha256(value)
               for name, value in participants.items()):
            problems.append("AUTHORITY_PARTICIPANT_HASH_INVALID")
        if _canonical_hash(participants) != authority.get("participant_spec_hashes_sha256"):
            problems.append("AUTHORITY_PARTICIPANT_ROOT_MISMATCH")
    for key in ("baseline_spec_hash", "matching_spec_hash", "tolerance_spec_hash"):
        if not _is_sha256(authority.get(key)):
            problems.append(f"AUTHORITY_{key.upper()}_INVALID")
    entries = authority.get("entries")
    if not isinstance(entries, list) or len(entries) != 12:
        problems.append("AUTHORITY_ENTRY_COUNT_MISMATCH")
        entries = []
    expected_keys = {
        f"{symbol}:{timeframe}"
        for symbol in EXPECTED_SYMBOLS for timeframe in EXPECTED_TIMEFRAMES
    }
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            problems.append("AUTHORITY_ENTRY_INVALID")
            continue
        key = entry.get("key")
        if key in seen:
            problems.append("AUTHORITY_ENTRY_DUPLICATE")
        seen.add(key)
        if key != f"{entry.get('symbol')}:{entry.get('timeframe')}":
            problems.append("AUTHORITY_ENTRY_KEY_MISMATCH")
        if entry.get("symbol") not in EXPECTED_SYMBOLS \
                or entry.get("timeframe") not in EXPECTED_TIMEFRAMES:
            problems.append("AUTHORITY_ENTRY_SCOPE_MISMATCH")
        if entry.get("venue") != "bitget" or entry.get("participant_count") != 47:
            problems.append("AUTHORITY_ENTRY_CONTRACT_MISMATCH")
        for hash_key in (
            "participant_specs_hash", "tournament_registry_hash",
            "baseline_spec_hash", "matching_spec_hash", "tolerance_spec_hash",
            "dataset_manifest_sha256", "holdout_commitment_sha256",
            "tournament_spec_hash",
        ):
            if not _is_sha256(entry.get(hash_key)):
                problems.append(f"AUTHORITY_ENTRY_{hash_key.upper()}_INVALID")
        if entry.get("participant_specs_hash") != authority.get(
                "participant_spec_hashes_sha256"):
            problems.append("AUTHORITY_ENTRY_PARTICIPANTS_MISMATCH")
        if entry.get("baseline_spec_hash") != authority.get("baseline_spec_hash"):
            problems.append("AUTHORITY_ENTRY_BASELINE_MISMATCH")
        if entry.get("matching_spec_hash") != authority.get("matching_spec_hash") \
                or entry.get("tolerance_spec_hash") != authority.get(
                    "tolerance_spec_hash"):
            problems.append("AUTHORITY_ENTRY_MATCHING_MISMATCH")
        if _entry_spec_hash(entry) != entry.get("tournament_spec_hash"):
            problems.append("AUTHORITY_ENTRY_SPEC_HASH_MISMATCH")
    if seen != expected_keys:
        problems.append("AUTHORITY_ENTRY_SET_MISMATCH")
    if problems:
        raise CampaignAuthorityError(";".join(sorted(set(problems))))


@lru_cache(maxsize=1)
def _load_cached() -> dict[str, Any]:
    module_dir = Path(__file__).resolve(strict=True).parent
    supplied = Path(__file__).with_name(AUTHORITY_FILENAME)
    if supplied.is_symlink():
        raise CampaignAuthorityError("AUTHORITY_SYMLINK_FORBIDDEN")
    path = supplied.resolve(strict=True)
    if path.parent != module_dir:
        raise CampaignAuthorityError("AUTHORITY_PATH_OUTSIDE_MODULE")
    try:
        authority = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CampaignAuthorityError(f"AUTHORITY_UNREADABLE:{exc}") from exc
    if not isinstance(authority, dict):
        raise CampaignAuthorityError("AUTHORITY_NOT_OBJECT")
    _validate_authority(authority)
    return authority


def load_campaign_authority(campaign_id: str = CAMPAIGN_ID) -> dict[str, Any]:
    if campaign_id != CAMPAIGN_ID:
        raise CampaignAuthorityError("UNAUTHORIZED_CAMPAIGN_ID")
    return copy.deepcopy(_load_cached())


def authorize_pairing(*, campaign_id: str, symbol: str,
                      timeframe: str) -> TournamentAuthorization:
    authority = load_campaign_authority(campaign_id)
    matches = [
        entry for entry in authority["entries"]
        if entry["symbol"] == symbol and entry["timeframe"] == timeframe
    ]
    if len(matches) != 1:
        raise CampaignAuthorityError("UNAUTHORIZED_TOURNAMENT_KEY")
    return TournamentAuthorization(
        campaign_id=authority["campaign_id"],
        campaign_version=authority["campaign_version"],
        symbol=symbol,
        timeframe=timeframe,
        entry=copy.deepcopy(matches[0]),
        root_anchor_sha256=authority["root_anchor_sha256"],
        m_tournament=authority["participants_per_tournament"],
        m_campaign=authority["m_campaign"],
        alpha=authority["alpha"],
        correction_method=authority["correction_method"],
        full_context_verified=False,
    )


def authorize_tournament(*, campaign_id: str, symbol: str, timeframe: str,
                         venue: str, registry: dict[str, Any],
                         dataset_manifest_path: str | Path,
                         source_generation_id: str,
                         holdout_commitment_sha256: str) -> TournamentAuthorization:
    context = authorize_pairing(
        campaign_id=campaign_id, symbol=symbol, timeframe=timeframe,
    )
    entry = context.entry
    problems: list[str] = []
    if venue != entry["venue"]:
        problems.append("TOURNAMENT_VENUE_MISMATCH")
    expected_registry = {
        "registry_hash": entry["tournament_registry_hash"],
        "specs_hash": entry["participant_specs_hash"],
        "baseline_policy_spec_hash": entry["baseline_spec_hash"],
        "baseline_tolerance_spec_hash": entry["tolerance_spec_hash"],
        "m_nominal": context.m_tournament,
        "m_unique_hypotheses": context.m_tournament,
        "m_global": context.m_tournament,
    }
    for key, expected in expected_registry.items():
        if registry.get(key) != expected:
            problems.append(f"TOURNAMENT_{key.upper()}_MISMATCH")
    specs = registry.get("specs")
    authority = load_campaign_authority(campaign_id)
    if specs != authority["participant_spec_hashes"]:
        problems.append("TOURNAMENT_PARTICIPANTS_MISMATCH")
    manifest_path = Path(dataset_manifest_path)
    if manifest_path.is_symlink():
        problems.append("DATASET_MANIFEST_SYMLINK_FORBIDDEN")
    else:
        try:
            manifest_raw = manifest_path.resolve(strict=True).read_bytes()
            manifest = json.loads(manifest_raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            manifest_raw, manifest = b"", {}
            problems.append("DATASET_MANIFEST_UNREADABLE")
        if hashlib.sha256(manifest_raw).hexdigest() != entry["dataset_manifest_sha256"]:
            problems.append("DATASET_MANIFEST_SHA_MISMATCH")
        if manifest.get("symbol") != symbol or manifest.get("timeframe") != timeframe:
            problems.append("DATASET_MANIFEST_SCOPE_MISMATCH")
        if manifest.get("source_generation_id") != source_generation_id:
            problems.append("DATASET_MANIFEST_GENERATION_MISMATCH")
        if manifest.get("holdout_state") != "SEALED":
            problems.append("DATASET_HOLDOUT_NOT_SEALED")
    if source_generation_id != entry["dataset_source_generation_id"]:
        problems.append("DATASET_SOURCE_GENERATION_MISMATCH")
    if holdout_commitment_sha256 != entry["holdout_commitment_sha256"]:
        problems.append("HOLDOUT_COMMITMENT_MISMATCH")
    if problems:
        raise CampaignAuthorityError(";".join(sorted(set(problems))))
    return TournamentAuthorization(
        **{
            **context.__dict__,
            "full_context_verified": True,
            "_full_context_capability": _FULL_CONTEXT_CAPABILITY,
        },
    )


def validate_full_authorization(
        authorization: TournamentAuthorization) -> TournamentAuthorization:
    """Validate a full canonical authorization without accepting overrides."""
    if not isinstance(authorization, TournamentAuthorization):
        raise CampaignAuthorityError("TOURNAMENT_AUTHORIZATION_TYPE_INVALID")
    if authorization._full_context_capability is not _FULL_CONTEXT_CAPABILITY:
        raise CampaignAuthorityError("TOURNAMENT_AUTHORIZATION_NOT_FACTORY_ISSUED")
    canonical = authorize_pairing(
        campaign_id=authorization.campaign_id,
        symbol=authorization.symbol,
        timeframe=authorization.timeframe,
    )
    expected = TournamentAuthorization(
        **{
            **canonical.__dict__,
            "full_context_verified": True,
            "_full_context_capability": _FULL_CONTEXT_CAPABILITY,
        },
    )
    if authorization != expected:
        raise CampaignAuthorityError("TOURNAMENT_AUTHORIZATION_CONTEXT_INVALID")
    return expected


def public_campaign_contract(campaign_id: str = CAMPAIGN_ID) -> dict[str, Any]:
    authority = load_campaign_authority(campaign_id)
    tournaments = [
        {
            "key": entry["key"],
            "symbol": entry["symbol"],
            "timeframe": entry["timeframe"],
            "participants": authority["participants_per_tournament"],
            "participant_specs_hash": entry["participant_specs_hash"],
            "tournament_registry_hash": entry["tournament_registry_hash"],
            "tournament_spec_hash": entry["tournament_spec_hash"],
            "dataset_manifest_sha256": entry["dataset_manifest_sha256"],
        }
        for entry in authority["entries"]
    ]
    contract = {
        "schema": authority["schema"],
        "campaign_id": authority["campaign_id"],
        "campaign_version": authority["campaign_version"],
        "symbols": list(authority["symbols"]),
        "timeframes": list(authority["timeframes"]),
        "tournament_combinations": authority["tournament_combinations"],
        "participants_per_tournament": authority["participants_per_tournament"],
        "tournaments": tournaments,
        "m_campaign_nominal": authority["m_campaign"],
        "m_campaign_unique_hypotheses": authority["m_campaign"],
        "m_campaign_unique_results": authority["m_campaign"],
        "m_campaign_effective_for_gate": authority["m_campaign"],
        "deduplication_status": "CANONICAL_NOMINAL_REQUIRED",
        "correction_method": authority["correction_method"],
        "alpha": authority["alpha"],
        "root_anchor_sha256": authority["root_anchor_sha256"],
        "closed": True,
        "closed_before_metrics": True,
    }
    return {
        "campaign_registry_contract": contract,
        "campaign_registry_sha": authority["root_anchor_sha256"],
        "campaign_authority_root": authority["root_anchor_sha256"],
        "campaign_id": authority["campaign_id"],
        "campaign_version": authority["campaign_version"],
        "m_campaign_nominal": authority["m_campaign"],
        "m_campaign_unique_hypotheses": authority["m_campaign"],
        "m_campaign_unique_results": authority["m_campaign"],
        "m_campaign_effective_for_gate": authority["m_campaign"],
        "correction_method": authority["correction_method"],
        "alpha": authority["alpha"],
        "closed": True,
        "closed_before_metrics": True,
        "authority_status": "CANONICAL_AUTHORITY_VALID",
        "research_only": True,
        "final_recommendation": "NO LIVE",
    }
