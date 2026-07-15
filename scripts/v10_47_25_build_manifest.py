"""Build and verify the V10.47.25 comprehensive manifest and seal."""

from __future__ import annotations

from pathlib import Path

import v10_47_22_build_real_state_manifest as builder


ROOT = Path(__file__).resolve().parents[1]
builder.REPORT_ROOT = (
    ROOT / "reports" / "research" / "v10_47_25_comprehensive_closure"
)


if __name__ == "__main__":
    raise SystemExit(builder.main())
