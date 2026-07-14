# MEETING NOTES

## 2026-07-14 — Scientific repair review
- Accepted Work's audit in full; reproduced before fixing.
- Adopted FIRST_CAUSAL_SIGNAL_SINGLE_POSITION (D001).
- Invalidated V10.47 shadow candidates (D002).
- Deterministic strategies gated on data (D003).
- Verdict: NO CONFIRMED EDGE. Next real step is data.

## 2026-07-14 — Certification FAIL + repair
- Work's independent audit of V10.47.14 = CERTIFICATION=FAIL (D004); accepted.
- Deterministic strategies need implementation repair before data (D005).
- Reproduced the 6 material findings with failing tests, then fixed each.
- Physically sealed the holdout; evaluated VALIDATION; exact paired baseline; real
  4h→1h + 2-ATR risk; provenance-bound manifest/seal; unique test ids.
- Regenerated the 12 tournaments without opening the holdout → SHADOW_CANDIDATES=0,
  holdout SEALED everywhere. Verdict: CERTIFICATION REPAIR COMPLETE — NO CONFIRMED EDGE.
- NEXT_ACTION: route back to Work for re-audit; hold the holdout SEALED.
