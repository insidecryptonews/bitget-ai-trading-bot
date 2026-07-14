"""V10.47.16 physically sealed holdout (RESEARCH ONLY, NO LIVE).

Work's audit showed the V10.47.14 holdout was only a hard-coded `holdout_touched=
False` literal while the whole series (including the nominal holdout range) was
precomputed. This module makes the seal REAL and auditable:

  * the holdout bars live ONLY inside a guarded object, never persisted next to
    selection data and never returned by default;
  * a content COMMITMENT hash is computed at seal time (before any authorization)
    and can be written to a separate commitment file that contains NO bar data;
  * access is DENIED by default; a single-use token must be minted with an
    explicit reason + independent audit reference;
  * every attempt (authorize / denied / consumed) is appended to an immutable
    access log;
  * a runtime guard refuses to serve the holdout if the call originates from a
    selection module (tournament / discovery / validation / walk-forward);
  * a deterministic state machine tracks
    UNAVAILABLE → SEALED → AUTHORIZED_ONCE → CONSUMED (or INVALIDATED).

In this certification repair the holdout is NEVER opened: it is constructed,
committed and left SEALED. Nothing here can send an order or enable live.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import secrets
from typing import Any

STATES = ("UNAVAILABLE", "SEALED", "AUTHORIZED_ONCE", "CONSUMED", "INVALIDATED")
# modules that must never be able to read the holdout
FORBIDDEN_CALLERS = ("causal_tournament", "edge_search", "causal_ledger",
                     "discovery", "validation", "walk_forward")


class HoldoutAccessDenied(RuntimeError):
    """Raised on any unauthorized / repeated / selection-originated access."""


def _commit(bars: list[dict]) -> str:
    payload = json.dumps(
        [[int(b["ts"]), float(b["open"]), float(b["high"]), float(b["low"]),
          float(b["close"]), float(b.get("volume", 0.0))] for b in bars],
        separators=(",", ":"), sort_keys=False)
    return hashlib.sha256(payload.encode()).hexdigest()


class SealedHoldout:
    __slots__ = ("symbol", "timeframe", "_bars", "_commitment", "state",
                 "_token", "_log", "n_bars", "index_range")

    def __init__(self, *, symbol: str, timeframe: str, holdout_bars: list[dict],
                 commitment: str | None = None, index_range: tuple | None = None):
        self.symbol = symbol
        self.timeframe = timeframe
        self._bars = list(holdout_bars)                  # private
        self.n_bars = len(self._bars)
        self.index_range = index_range
        self._commitment = _commit(self._bars)
        if commitment is not None and commitment != self._commitment:
            # a supplied commitment that does not match the data invalidates the seal
            self.state = "INVALIDATED"
        else:
            self.state = "SEALED" if self.n_bars > 0 else "UNAVAILABLE"
        self._token: str | None = None
        self._log: list[dict] = []
        self._append("seal", commitment=self._commitment, n_bars=self.n_bars)

    # -- introspection (no bar data) ------------------------------------------
    def commitment_hash(self) -> str:
        return self._commitment

    def access_log(self) -> list[dict]:
        return [dict(r) for r in self._log]

    def _append(self, kind: str, **f: Any) -> None:
        self._log.append({"seq": len(self._log), "kind": kind, "state": self.state,
                          **f})

    def _selection_caller(self) -> str | None:
        for fr in inspect.stack():
            mod = fr.frame.f_globals.get("__name__", "")
            if any(bad in mod for bad in FORBIDDEN_CALLERS):
                return mod
        return None

    # -- authorization + one-time load ----------------------------------------
    def authorize_once(self, *, reason: str, audit_ref: str) -> str:
        if self.state != "SEALED":
            self._append("authorize_denied", reason="not_sealed")
            raise HoldoutAccessDenied(f"cannot authorize from state {self.state}")
        if not audit_ref:
            self._append("authorize_denied", reason="no_audit_ref")
            raise HoldoutAccessDenied("authorization requires an audit_ref")
        self._token = secrets.token_hex(16)
        self.state = "AUTHORIZED_ONCE"
        self._append("authorized", reason=reason, audit_ref=audit_ref)
        return self._token

    def load(self, *, token: str | None = None) -> list[dict]:
        caller = self._selection_caller()
        if caller is not None:
            self._append("denied", reason="selection_caller", caller=caller)
            raise HoldoutAccessDenied(f"holdout not accessible from {caller}")
        if self.state != "AUTHORIZED_ONCE":
            self._append("denied", reason=f"state_{self.state}")
            raise HoldoutAccessDenied(
                f"holdout is {self.state}; mint a single-use token first")
        if not token or token != self._token:
            self._append("denied", reason="bad_token")
            raise HoldoutAccessDenied("invalid or missing one-time token")
        self.state = "CONSUMED"
        self._token = None
        self._append("consumed")
        return [dict(b) for b in self._bars]

    def write_commitment(self, path: str) -> str:
        """Write a commitment file with NO bar data (hash + metadata only)."""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        doc = {"symbol": self.symbol, "timeframe": self.timeframe,
               "state": self.state, "n_bars": self.n_bars,
               "index_range": self.index_range,
               "commitment_sha256": self._commitment}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2, sort_keys=True)
        return self._commitment


def assert_not_selecting_on_holdout(holdout_start_index: int,
                                    accessed_indices) -> None:
    """Fail closed if any selection index reaches into the holdout range."""
    for i in accessed_indices:
        if i >= holdout_start_index:
            raise HoldoutAccessDenied(
                f"selection index {i} enters holdout (start {holdout_start_index})")
