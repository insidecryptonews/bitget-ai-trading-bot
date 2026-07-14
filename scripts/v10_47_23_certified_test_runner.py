"""Run one certified full suite below the V10.47.23 evidence root."""

from __future__ import annotations

from pathlib import Path

import v10_47_22_certified_test_runner as runner


ROOT = Path(__file__).resolve().parents[1]
runner.ALLOWED_ROOT = (
    ROOT / "reports" / "research" / "v10_47_23_exact_pairing"
)


if __name__ == "__main__":
    raise SystemExit(runner.main())
