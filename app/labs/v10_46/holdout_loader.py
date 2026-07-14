"""External, single-use loader for a physically separate sealed holdout.

Discovery code does not import this module.  A capability can only be issued by
an externally constructed authority whose secret matches the public key hash in
the commitment.  The real holdout is not opened by V10.47.20-22.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any


class HoldoutAccessDenied(RuntimeError):
    """Fail-closed holdout access violation."""


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


@dataclass
class HoldoutCapability:
    capability_id: str
    root: str
    commitment_sha256: str
    audit_ref: str
    reason: str
    signature: str
    consumed: bool = False


class ExternalHoldoutAuthority:
    """Authority supplied by an external audit context, never by discovery."""

    __slots__ = ("root", "_secret", "_commitment", "_log_path")

    def __init__(self, sealed_root: str | os.PathLike[str], *, secret: bytes):
        supplied = Path(sealed_root)
        if supplied.is_symlink():
            raise HoldoutAccessDenied("sealed root symlink is forbidden")
        root = supplied.resolve(strict=True)
        if not root.is_dir():
            raise HoldoutAccessDenied("sealed root is not a directory")
        commitment_path = root / "commitment.json"
        if commitment_path.is_symlink():
            raise HoldoutAccessDenied("commitment symlink is forbidden")
        try:
            commitment = json.loads(commitment_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HoldoutAccessDenied(f"invalid commitment: {exc}") from exc
        if commitment.get("state") != "SEALED":
            raise HoldoutAccessDenied("holdout commitment is not SEALED")
        if not isinstance(secret, bytes) or not secret:
            raise HoldoutAccessDenied("external authority secret is required")
        if _sha_bytes(secret) != commitment.get("authority_key_sha256"):
            raise HoldoutAccessDenied("external authority does not match commitment")
        self.root = root
        self._secret = bytes(secret)
        self._commitment = copy.deepcopy(commitment)
        self._log_path = root / "access.log"

    def _capability_payload(self, capability_id: str, audit_ref: str,
                            reason: str) -> dict:
        return {
            "capability_id": capability_id,
            "root": str(self.root),
            "commitment_sha256": self._commitment["commitment_sha256"],
            "audit_ref": audit_ref,
            "reason": reason,
        }

    def _sign(self, payload: dict) -> str:
        return hmac.new(self._secret, _canonical(payload), hashlib.sha256).hexdigest()

    def _append(self, kind: str, **fields: Any) -> None:
        records = self.access_log()
        previous = records[-1]["record_hash"] if records else "0" * 64
        record = {
            "seq": len(records), "kind": kind, "prev_hash": previous,
            **copy.deepcopy(fields),
        }
        record["record_hash"] = _sha_bytes(_canonical(record))
        line = _canonical(record) + b"\n"
        fd = os.open(self._log_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(fd, line)
            os.fsync(fd)
        finally:
            os.close(fd)

    def access_log(self) -> list[dict]:
        if not self._log_path.exists():
            return []
        records: list[dict] = []
        previous = "0" * 64
        for raw in self._log_path.read_text(encoding="utf-8").splitlines():
            record = json.loads(raw)
            claimed = record.pop("record_hash")
            actual = _sha_bytes(_canonical(record))
            record["record_hash"] = claimed
            if claimed != actual or record.get("prev_hash") != previous:
                raise HoldoutAccessDenied("append-only access log integrity failure")
            previous = claimed
            records.append(record)
        return copy.deepcopy(records)

    def issue_capability(self, *, reason: str, audit_ref: str) -> HoldoutCapability:
        if not reason or not audit_ref:
            raise HoldoutAccessDenied("reason and independent audit_ref are required")
        capability_id = secrets.token_hex(16)
        payload = self._capability_payload(capability_id, audit_ref, reason)
        capability = HoldoutCapability(**payload, signature=self._sign(payload))
        self._append("capability_issued", capability_id=capability_id,
                     audit_ref=audit_ref, reason=reason)
        return capability

    def _resolve_data_path(self, relative_path: str | os.PathLike[str] | None) -> Path:
        raw = str(relative_path or self._commitment.get("data_file", ""))
        pure = PurePath(raw)
        if pure.is_absolute() or ".." in pure.parts:
            raise HoldoutAccessDenied("absolute paths and traversal are forbidden")
        if raw.replace("\\", "/") != self._commitment.get("data_file"):
            raise HoldoutAccessDenied("capability is limited to the committed data file")
        candidate = self.root.joinpath(*pure.parts)
        current = candidate
        while current != self.root:
            if current.is_symlink():
                raise HoldoutAccessDenied("symlink escape is forbidden")
            current = current.parent
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(self.root):
            raise HoldoutAccessDenied("resolved path escapes sealed root")
        return resolved

    def load_once(self, capability: HoldoutCapability,
                  relative_path: str | os.PathLike[str] | None = None) -> list[dict]:
        if not isinstance(capability, HoldoutCapability):
            self._append("denied_invalid_capability_type")
            raise HoldoutAccessDenied("an external HoldoutCapability is required")
        if capability.consumed:
            self._append("denied_already_consumed",
                         capability_id=capability.capability_id)
            raise HoldoutAccessDenied("capability already consumed")
        payload = self._capability_payload(
            capability.capability_id, capability.audit_ref, capability.reason
        )
        if capability.root != str(self.root) \
                or capability.commitment_sha256 != self._commitment["commitment_sha256"] \
                or not hmac.compare_digest(capability.signature, self._sign(payload)):
            self._append("denied_bad_signature",
                         capability_id=capability.capability_id)
            raise HoldoutAccessDenied("invalid external capability")
        path = self._resolve_data_path(relative_path)
        capability.consumed = True
        self._append("capability_consumed", capability_id=capability.capability_id,
                     audit_ref=capability.audit_ref)
        payload_bytes = path.read_bytes()
        if _sha_bytes(payload_bytes) != self._commitment["commitment_sha256"]:
            raise HoldoutAccessDenied("sealed data hash does not match commitment")
        try:
            rows = json.loads(payload_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HoldoutAccessDenied(f"sealed data is not valid JSON: {exc}") from exc
        if not isinstance(rows, list):
            raise HoldoutAccessDenied("sealed data must be a list")
        return copy.deepcopy(rows)
