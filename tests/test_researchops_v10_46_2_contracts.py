"""V10.46.2 canonical contracts + EventClock: provenance block, enum-only
decisions, epoch-ms timestamps, no ambiguous IDs, causal validation, and a
deterministic no-lookahead EventClock over adapted dataset generations.
Research only, NO LIVE."""

from __future__ import annotations

import pytest

from app.labs.v10_46 import contracts as C
from app.labs.v10_46 import event_clock as EC

CM = dict(symbol="BTCUSDT", venue="bitget", timeframe="1m",
          event_id="BTCUSDT:1000", causal_cutoff_ms=2000,
          data_generation_id="gen1", repo_commit="abc")


def test_common_block_and_epoch_ms():
    rec = C.make("MarketEvent", event_type="BAR", ts_ms=1000,
                 available_time_ms=2000, payload={"close": 100.0}, **CM)
    for f in ("schema_version", "created_at_ms", "event_id",
              "event_cluster_id", "symbol", "venue", "timeframe",
              "data_generation_id", "repo_commit", "policy_version",
              "spec_hash", "causal_cutoff_ms"):
        assert f in rec
    assert isinstance(rec["created_at_ms"], int)
    assert rec["schema_version"] == C.SCHEMA_VERSION


def test_missing_id_is_explicit_not_ambiguous():
    rec = C.make("MarketSnapshot", ts_ms=1000, mid=100.0, bid=99.9, ask=100.1,
                 **{**CM, "spec_hash": None, "data_generation_id": None})
    assert rec["spec_hash_status"] == "NOT_APPLICABLE"
    assert rec["data_generation_status"] == "NOT_APPLICABLE"


def test_agent_proposal_rejects_uncalibrated_and_free_text():
    good = C.make("AgentProposal", agent="TrendAgent", action="PROPOSE",
                  side="LONG", calibrated_probability=0.62,
                  expected_win_pct=0.8, expected_loss_pct=0.5,
                  expected_duration_ms=600000, fill_probability=0.9,
                  entry_zone={}, invalidation={"price": 99.0},
                  target={"price": 101.0}, cost_estimate_eur=0.02,
                  evidence_ids=["BTCUSDT:1000"], regime="TREND_UP",
                  reason_codes=["TREND_CONFIRMED"], expiry_ms=3000,
                  model_version="m1", **CM)
    assert C.validate(good)[0]
    # uncalibrated probability rejected
    bad = dict(good)
    bad["calibrated_probability"] = 1.7
    ok, reasons = C.validate(bad)
    assert not ok and "PROB_NOT_CALIBRATED" in reasons
    # non-finite rejected
    nf = dict(good)
    nf["expected_win_pct"] = float("inf")
    assert not C.validate(nf)[0]


def test_decision_record_is_enum_only():
    with pytest.raises(ValueError):
        C.make("DecisionRecord", decision_action="just do it", side="LONG",
               reason_codes=[], proposals_for=1, proposals_against=0,
               calibrated_probability=0.6, **CM)
    ok = C.make("DecisionRecord", decision_action="ABSTAIN_COST", side="FLAT",
                reason_codes=["ABSTAIN_COST"], proposals_for=0,
                proposals_against=2, calibrated_probability=0.5, **CM)
    assert ok["decision_action"] == "ABSTAIN_COST"


def test_promotion_live_readiness_needs_audit():
    with pytest.raises(ValueError):
        C.make("PromotionDecision", policy_id="p1",
               from_state="PAPER_CHAMPION", to_state="LIVE_READINESS_ONLY",
               decision="PROMOTE", gate_results={}, **CM)
    ok = C.make("PromotionDecision", policy_id="p1",
                from_state="PAPER_CHAMPION", to_state="LIVE_READINESS_ONLY",
                decision="PROMOTE", gate_results={},
                independent_audit_ref="audit-123", **CM)
    assert ok["to_state"] == "LIVE_READINESS_ONLY"


def test_causal_validation_rejects_future_evidence():
    rec = C.make("AgentProposal", agent="A", action="PROPOSE", side="LONG",
                 calibrated_probability=0.6, expected_win_pct=0.5,
                 expected_loss_pct=0.5, expected_duration_ms=60000,
                 fill_probability=0.9, entry_zone={}, invalidation={},
                 target={}, cost_estimate_eur=0.01,
                 evidence_ids=["past", "future"], regime="ANY",
                 reason_codes=["X"], expiry_ms=3000, model_version="m",
                 **{**CM, "causal_cutoff_ms": 2000})
    ok, bad = C.validate_causal(rec, {"past": 1500, "future": 2500})
    assert not ok and "future" in bad and "past" not in bad


# ==========================================================================
# EventClock
# ==========================================================================

def _bars(n, t0=1_700_000_000_000):
    return [{"ts": t0 + i * EC.BAR_MS, "open": 100.0 + i, "high": 101.0 + i,
             "low": 99.0 + i, "close": 100.5 + i, "volume": 10.0}
            for i in range(n)]


def test_event_clock_is_causal_and_no_lookahead():
    evs = EC.bars_to_events(_bars(10), symbol="BTCUSDT", venue="bitget",
                            timeframe="1m", data_generation_id="g")
    clk = EC.EventClock(evs)
    t0 = 1_700_000_000_000
    # at the close of bar 3 (available at t0+4*BAR) exactly 4 bars are visible
    vis = clk.visible_at(t0 + 4 * EC.BAR_MS)
    assert len(vis) == 4
    assert all(e["available_time_ms"] <= t0 + 4 * EC.BAR_MS for e in vis)
    # the forming bar (open at t0+4*BAR, closes later) is NOT visible yet
    assert all(e["ts_ms"] < t0 + 4 * EC.BAR_MS for e in vis)


def test_event_clock_rejects_backward_time():
    evs = EC.bars_to_events(_bars(5), symbol="X", venue="bitget",
                            timeframe="1m", data_generation_id="g")
    clk = EC.EventClock(evs)
    clk.visible_at(1_700_000_000_000 + 3 * EC.BAR_MS)
    with pytest.raises(ValueError, match="backwards"):
        clk.visible_at(1_700_000_000_000)


def test_event_clock_stream_is_deterministic():
    evs = EC.bars_to_events(_bars(8), symbol="X", venue="bitget",
                            timeframe="1m", data_generation_id="g")
    clk = EC.EventClock(evs)
    a = [(t, len(v)) for t, v in clk.stream(warmup=2)]
    b = [(t, len(v)) for t, v in clk.stream(warmup=2)]
    assert a == b and len(a) == 6                        # 8 bars - 2 warmup
    # visible count is monotonic non-decreasing
    assert all(a[i][1] <= a[i + 1][1] for i in range(len(a) - 1))
