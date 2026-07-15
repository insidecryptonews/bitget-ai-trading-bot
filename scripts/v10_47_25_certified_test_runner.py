"""Run the V10.47.25 clean-tree certified collection and full suite."""

from __future__ import annotations

from pathlib import Path

import v10_47_22_certified_test_runner as runner


ROOT = Path(__file__).resolve().parents[1]
runner.ALLOWED_ROOT = (
    ROOT / "reports" / "research" / "v10_47_25_comprehensive_closure"
)


if __name__ == "__main__":
    raise SystemExit(runner.main())
