from app.cost_model import explain_cost_breakdown, should_apply_funding


def test_funding_applies_only_when_timestamp_crosses():
    assert should_apply_funding("2026-05-19T07:59:00+00:00", "2026-05-19T08:01:00+00:00") is True
    assert should_apply_funding("2026-05-19T08:01:00+00:00", "2026-05-19T09:01:00+00:00") is False


def test_positive_funding_is_cost_for_long_income_for_short():
    long = explain_cost_breakdown(side="LONG", entry_time="2026-05-19T07:59:00+00:00", exit_time="2026-05-19T08:01:00+00:00", funding_rate=0.0045)
    short = explain_cost_breakdown(side="SHORT", entry_time="2026-05-19T07:59:00+00:00", exit_time="2026-05-19T08:01:00+00:00", funding_rate=0.0045)

    assert long.funding_component_bps > 0
    assert short.funding_component_bps < 0
    assert long.funding_model_status == "OK"
