"""Holdout commitment metadata helpers (research only, no live).

This module deliberately contains no holdout rows and no authorization API.
Actual sealed data access lives in ``holdout_loader`` and requires an external
single-use capability.  Discovery code never imports that loader.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path, PurePath

from .holdout_contract import HoldoutAccessDenied


def load_commitment(path: str | Path) -> dict:
    candidate = Path(path)
    if candidate.is_symlink():
        raise HoldoutAccessDenied("commitment symlink is forbidden")
    try:
        document = json.loads(candidate.resolve(strict=True).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HoldoutAccessDenied(f"invalid holdout commitment: {exc}") from exc
    required = {
        "schema", "state", "data_file", "commitment_sha256",
        "authority_key_sha256", "n_bars", "index_range", "metadata_sha256",
    }
    if not required.issubset(document) or document["state"] != "SEALED":
        raise HoldoutAccessDenied("incomplete or unsealed holdout commitment")
    for field in ("commitment_sha256", "authority_key_sha256", "metadata_sha256"):
        value = str(document[field])
        if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value.lower()):
            raise HoldoutAccessDenied(f"invalid {field}")
    data_file = PurePath(str(document["data_file"]))
    if data_file.is_absolute() or ".." in data_file.parts or not data_file.parts:
        raise HoldoutAccessDenied("unsafe committed data file")
    index_range = document["index_range"]
    if not isinstance(index_range, list) or len(index_range) != 2:
        raise HoldoutAccessDenied("invalid holdout index range")
    try:
        start, end = (int(index_range[0]), int(index_range[1]))
        n_bars = int(document["n_bars"])
    except (TypeError, ValueError) as exc:
        raise HoldoutAccessDenied("invalid holdout row count/index range") from exc
    if start < 0 or end <= start or n_bars != end - start:
        raise HoldoutAccessDenied("holdout row count/index range mismatch")
    metadata = {key: value for key, value in document.items() if key != "metadata_sha256"}
    actual_metadata_sha = hashlib.sha256(
        json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if actual_metadata_sha != document["metadata_sha256"]:
        raise HoldoutAccessDenied("holdout commitment metadata hash mismatch")
    return copy.deepcopy(document)


def commitment_document(*, symbol: str, timeframe: str, data_file: str,
                        data_sha256: str, authority_key_sha256: str,
                        n_bars: int, index_range: tuple[int, int]) -> dict:
    """Build metadata only.  This function never receives or stores bar rows."""
    pure_data_file = PurePath(str(data_file))
    if pure_data_file.is_absolute() or ".." in pure_data_file.parts \
            or not pure_data_file.parts:
        raise HoldoutAccessDenied("unsafe committed data file")
    for name, value in (("data_sha256", data_sha256),
                        ("authority_key_sha256", authority_key_sha256)):
        if len(str(value)) != 64 \
                or any(ch not in "0123456789abcdef" for ch in str(value).lower()):
            raise HoldoutAccessDenied(f"invalid {name}")
    start, end = int(index_range[0]), int(index_range[1])
    if start < 0 or end <= start or int(n_bars) != end - start:
        raise HoldoutAccessDenied("holdout row count/index range mismatch")
    payload = {
        "schema": "v10_47_20_holdout_commitment",
        "symbol": symbol,
        "timeframe": timeframe,
        "state": "SEALED",
        "data_file": data_file,
        "commitment_sha256": data_sha256,
        "authority_key_sha256": authority_key_sha256,
        "n_bars": int(n_bars),
        "index_range": [start, end],
        "research_only": True,
        "final_recommendation": "NO LIVE",
    }
    payload["metadata_sha256"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return payload


def assert_not_selecting_on_holdout(holdout_start_index: int,
                                    accessed_indices) -> None:
    for index in accessed_indices:
        if index >= holdout_start_index:
            raise HoldoutAccessDenied(
                f"selection index {index} enters holdout (start {holdout_start_index})"
            )
