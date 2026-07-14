"""Run the twelve V10.47.23 tournaments with the audited V10.47.22 runner."""

from __future__ import annotations

from pathlib import Path

import v10_47_22_regenerate_tournaments as runner


ROOT = Path(__file__).resolve().parents[1]
runner.REPORT_ROOT = (
    ROOT / "reports" / "research" / "v10_47_23_exact_pairing"
)
runner.RUN_ONE_SCRIPT = ROOT / "scripts" / "v10_47_23_run_one_tournament.py"


if __name__ == "__main__":
    raise SystemExit(runner.main())
