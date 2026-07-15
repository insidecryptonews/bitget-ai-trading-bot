"""Regenerate the twelve V10.47.25 canonical tournaments."""

from __future__ import annotations

from pathlib import Path

import v10_47_22_regenerate_tournaments as runner


ROOT = Path(__file__).resolve().parents[1]
runner.REPORT_ROOT = (
    ROOT / "reports" / "research" / "v10_47_25_comprehensive_closure"
)
runner.RUN_ONE_SCRIPT = ROOT / "scripts" / "v10_47_25_run_one_tournament.py"


if __name__ == "__main__":
    raise SystemExit(runner.main())
