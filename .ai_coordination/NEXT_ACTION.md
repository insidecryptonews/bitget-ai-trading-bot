# NEXT ACTION

There must be exactly ONE next action.

- [ ] NEXT: IMPLEMENT_V10_47_20_VALIDATION_AND_PHYSICAL_HOLDOUT_ISOLATION. Convert
  Work's focused falsifications into RED tests, then make VALIDATION admit the only
  candidates visible to WALK_FORWARD and replace the in-memory holdout wrapper with
  a separate fail-closed loader using synthetic fixtures only. Keep the real holdout
  SEALED and preserve NO_CONFIRMED_EDGE / SHADOW_CANDIDATES=0 / NO LIVE.
