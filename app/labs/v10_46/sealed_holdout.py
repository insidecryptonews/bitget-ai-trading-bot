"""Holdout commitment metadata helpers (research only, no live).

This module deliberately contains no holdout rows and no authorization API.
Actual sealed data access lives in ``holdout_loader`` and requires an external
single-use capability.  Discovery code never imports that loader.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from .holdout_loader import HoldoutAccessDenied


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
        "authority_key_sha256", "n_bars",
    }
    if not required.issubset(document) or document["state"] != "SEALED":
        raise HoldoutAccessDenied("incomplete or unsealed holdout commitment")
    if len(str(document["commitment_sha256"])) != 64:
        raise HoldoutAccessDenied("invalid commitment hash")
    return copy.deepcopy(document)


def commitment_document(*, symbol: str, timeframe: str, data_file: str,
                        data_sha256: str, authority_key_sha256: str,
                        n_bars: int, index_range: tuple[int, int]) -> dict:
    """Build metadata only.  This function never receives or stores bar rows."""
    payload = {
        "schema": "v10_47_20_holdout_commitment",
        "symbol": symbol,
        "timeframe": timeframe,
        "state": "SEALED",
        "data_file": data_file,
        "commitment_sha256": data_sha256,
        "authority_key_sha256": authority_key_sha256,
        "n_bars": int(n_bars),
        "index_range": list(index_range),
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
