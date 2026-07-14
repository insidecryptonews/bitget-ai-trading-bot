"""Build and verify the final V10.47.23 manifest without opening holdout bars."""

from __future__ import annotations

from pathlib import Path

import v10_47_22_build_real_state_manifest as builder


ROOT = Path(__file__).resolve().parents[1]
builder.REPORT_ROOT = (
    ROOT / "reports" / "research" / "v10_47_23_exact_pairing"
)


if __name__ == "__main__":
    raise SystemExit(builder.main())
