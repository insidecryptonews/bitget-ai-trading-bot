from __future__ import annotations

from types import SimpleNamespace

from app.exit_policy_v2 import render_exit_policy_v2_text, run_exit_policy_v2


def _policy(name: str, trades: int, ev: float, pf: float, decision: str = "WATCH_ONLY"):
    return SimpleNamespace(
        policy_name=name,
        trades=trades,
        net_ev=ev,
        net_pf=pf,
        tp_pct=0.2,
        sl_pct=0.1,
        time_pct=0.7,
        delta_ev_vs_baseline=0.0,
        decision=decision,
    )


def test_exit_policy_v2_separates_per_symbol_and_aggregate_baselines(monkeypatch):
    def fake_dynamic(config, db, *, hours, timeframe, symbols):
        symbol = symbols[0]
        if symbol == "DOTUSDT":
            baseline = _policy("baseline_current_exit", 100, -0.03, 0.93, "BASELINE")
            policy = _policy("late_entry_block_plus_dynamic_hold", 80, 0.10, 1.25, "IMPROVES_BASELINE_RESEARCH_ONLY")
        else:
            baseline = _policy("baseline_current_exit", 100, -0.10, 0.75, "BASELINE")
            policy = _policy("late_entry_block_plus_dynamic_hold", 100, -0.01, 0.98, "WATCH_ONLY")
        return SimpleNamespace(policies=[baseline, policy])

    monkeypatch.setattr("app.exit_policy_v2.run_dynamic_hold_lab", fake_dynamic)
    monkeypatch.setattr("app.exit_policy_v2.run_exit_lab", lambda *a, **k: SimpleNamespace(comparisons=[]))

    report = run_exit_policy_v2(object(), object(), hours=720, symbols=["DOTUSDT", "LINKUSDT"])
    best = report.candidates[0]
    assert best.policy_name == "late_entry_block_plus_dynamic_hold"
    assert round(best.aggregate_baseline_net_ev, 6) == -0.065
    assert round(best.aggregate_policy_net_ev, 6) == 0.038889
    assert round(best.aggregate_delta_ev, 6) == 0.103889
    assert best.aggregate_baseline_net_ev != -0.03
    assert {row.symbol: round(row.delta_ev_vs_symbol_baseline, 6) for row in report.per_symbol_best_policy} == {
        "DOTUSDT": 0.13,
        "LINKUSDT": 0.09,
    }


def test_exit_policy_v2_text_declares_separated_aggregate_reporting(monkeypatch):
    monkeypatch.setattr(
        "app.exit_policy_v2.run_dynamic_hold_lab",
        lambda *a, **k: SimpleNamespace(policies=[
            _policy("baseline_current_exit", 10, -0.1, 0.8, "BASELINE"),
            _policy("late_entry_block_plus_dynamic_hold", 10, 0.1, 1.2, "IMPROVES_BASELINE_RESEARCH_ONLY"),
        ]),
    )
    monkeypatch.setattr("app.exit_policy_v2.run_exit_lab", lambda *a, **k: SimpleNamespace(comparisons=[]))
    text = render_exit_policy_v2_text(run_exit_policy_v2(object(), object(), symbols=["DOTUSDT", "LINKUSDT"]))
    assert "PER-SYMBOL BASELINE" in text
    assert "PER-SYMBOL BEST POLICY" in text
    assert "AGGREGATE BEST POLICY" in text
    assert "per_symbol_and_aggregate_baselines_are_separated" in text
    assert "research_only: true" in text
    assert "final_recommendation: NO LIVE" in text
