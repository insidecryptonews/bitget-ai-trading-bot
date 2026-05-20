# Migration Notes

No files were moved or deleted in this pass. This note documents canonical modules and legacy candidates for later human review.

## Backtesting

| Module | Proposed Status | Notes |
| --- | --- | --- |
| `app/real_strategy_backtester.py` | KEEP / canonical for real strategy replay | Calls `SignalEngine.generate_signal()` candle by candle and uses next-open entry. |
| `app/backtester.py` | LEGACY_CANDIDATE | Simple EMA/breakout baseline. Useful as a benchmark, not evidence for the live bot logic. |

## Walk Forward

| Module | Proposed Status | Notes |
| --- | --- | --- |
| `app/walk_forward_validator.py` | KEEP / Fase 7 rolling validator | Research/shadow rolling OOS validation. |
| `app/walk_forward_validation.py` | LEGACY_CANDIDATE | Existing policy-level validator used by older gates; review imports before merging. |

## Exit Policy

| Module | Proposed Status | Notes |
| --- | --- | --- |
| `app/exit_policy_v3.py` | KEEP | Dynamic exits require bar path; MFE summary is marked `NEED_BAR_PATH`. |
| `app/exit_policy_v3_backtest.py` | KEEP | Group comparator for V3 policies. |
| `app/exit_policy_backtest.py` | LEGACY_CANDIDATE | Older exit policy report; keep until dashboard/report dependencies are reviewed. |

## Smoke Tests In `app/`

Several command-facing smoke modules remain under `app/` because `research_lab` imports them directly. Recommended future target is `tests/smoke/`, but moving them should be done only after command compatibility is preserved.

## Recommendation

Do not delete or move modules automatically. First add import tracing, update `research_lab` command routing, and run the full test suite.

Final recommendation: `NO LIVE`.
