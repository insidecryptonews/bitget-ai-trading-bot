"""V10.43C strategy lab hardening: honest global verdict and taxonomy."""

from __future__ import annotations

from app.labs import strategy_lab_hardening_v10_43c as HARD


def test_global_verdict_all_needs_more_data():
    base = {"candidates_generated": 3,
            "verdict_counts": {"NEEDS_MORE_DATA": 3, "REJECTED": 0,
                               "WATCHLIST": 0, "INCUBATE": 0,
                               "SHADOW_FORWARD_CANDIDATE": 0}}
    assert HARD._global_verdict(base) == "ALL_NEEDS_MORE_DATA"


def test_global_verdict_rejected_when_sampled_candidates_fail():
    base = {"candidates_generated": 3,
            "verdict_counts": {"NEEDS_MORE_DATA": 1, "REJECTED": 2,
                               "WATCHLIST": 0, "INCUBATE": 0,
                               "SHADOW_FORWARD_CANDIDATE": 0}}
    assert HARD._global_verdict(base) == "NO_EDGE_ALL_REJECTED"


def test_rejection_taxonomy_maps_core_reasons():
    assert HARD._normalize_reason({"verdict": "NEEDS_MORE_DATA"}) == "INSUFFICIENT_SAMPLE"
    assert HARD._normalize_reason({"verdict": "REJECTED",
                                   "rejection_reason": "net_EV<=0"}) == "NEGATIVE_NET_EV"
    assert HARD._normalize_reason({"verdict": "WATCHLIST",
                                   "rejection_reason": "lower_bound<=0"}) == "LOWER_BOUND_NOT_POSITIVE"


def test_run_hardened_lab_wraps_base_without_promotion(monkeypatch, tmp_path):
    monkeypatch.setattr(HARD.CE, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(HARD.LAB, "run_lab", lambda *a, **k: {
        "effective_source": "ws_persistent", "n_bars": 100,
        "candidates_generated": 2,
        "verdict_counts": {"NEEDS_MORE_DATA": 2, "REJECTED": 0,
                           "WATCHLIST": 0, "INCUBATE": 0,
                           "SHADOW_FORWARD_CANDIDATE": 0},
        "watchlist_or_better": 0,
        "best": {"strategy_name": "x", "verdict": "NEEDS_MORE_DATA",
                 "net_EV": None, "net_EV_lower_bound": None},
        "best_net_EV_lower_bound": None,
        "candidates_detail": [
            {"strategy_name": "x", "verdict": "NEEDS_MORE_DATA"},
            {"strategy_name": "y", "verdict": "NEEDS_MORE_DATA"}],
    })
    monkeypatch.setattr(HARD, "_sensitivity", lambda *a, **k: {"status": "INSUFFICIENT_SAMPLE"})
    r = HARD.run_hardened_lab("BTCUSDT", write_reports=True)
    assert r["global_verdict"] == "ALL_NEEDS_MORE_DATA"
    assert r["watchlist_or_better"] == 0
    assert r["edge_validated"] is False
    assert (tmp_path.joinpath(*HARD.OUTPUT_SUBDIR) / "strategy_research_memo_v1043c.md").is_file()


def test_cli_registered():
    import app.research_lab as RL
    assert "autonomous-strategy-lab-v1043c" in RL.PUBLIC_RESEARCH_ONLY_COMMANDS
