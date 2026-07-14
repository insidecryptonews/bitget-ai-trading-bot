"""Run one V10.47.23 tournament below its isolated evidence root."""

from __future__ import annotations

from pathlib import Path

import v10_47_22_run_one_tournament as runner


ROOT = Path(__file__).resolve().parents[1]
runner.REPORT_ROOT = (
    ROOT / "reports" / "research" / "v10_47_23_exact_pairing"
)


if __name__ == "__main__":
    raise SystemExit(runner.main())
