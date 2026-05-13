from __future__ import annotations

from types import SimpleNamespace

from app.config import BotConfig, PROJECT_ROOT
from app.edge_guard import ALLOW_PAPER, BLOCK_PAPER, EdgeGuard
from app.tp_sl_horizon_lab import END as TP_END, START as TP_START, TpSlHorizonLab


class EdgeDb:
    def __init__(self, rows=None, labels=None):
        self.rows = rows or []
        self.labels = labels or {"total_labels": 1000, "time_count": 900, "sl_count": 90, "tp1_count": 10, "tp2_count": 0, "profit_factor": 0.5}

    def get_high_score_label_summary_since(self, *args, **kwargs):
        return self.labels

    def get_shadow_opportunity_group_summaries_since(self, *args, **kwargs):
        return list(self.rows)


def test_edge_guard_cli_exists():
    text = (PROJECT_ROOT / "app" / "research_lab.py").read_text(encoding="utf-8")
    assert '"edge-guard"' in text
    assert '"tp-sl-lab"' in text


def test_edge_guard_classifies_pf_below_one_as_block_or_shadow():
    row = {"group_value": "DOGEUSDT", "total_labels": 900, "profit_factor": 0.44, "tp_ratio": 0.02, "sl_ratio": 0.10, "time_ratio": 0.88}
    decision, reason = EdgeGuard(BotConfig(), EdgeDb()).classify_metrics(row)
    assert decision in {BLOCK_PAPER, "SHADOW_ONLY"}
    assert reason in {"pf_below_1", "edge_not_confirmed", "sl_high_tp_low"}


def test_edge_guard_classifies_good_sample_as_allow_paper():
    row = {"group_value": "ETHUSDT", "total_labels": 5704, "profit_factor": 1.93, "tp_ratio": 0.035, "sl_ratio": 0.018, "time_ratio": 0.947}
    decision, reason = EdgeGuard(BotConfig(), EdgeDb()).classify_metrics(row)
    assert decision == ALLOW_PAPER
    assert reason == "edge_thresholds_met"


def test_edge_guard_disabled_filter_allows_paper():
    signal = SimpleNamespace(symbol="DOGEUSDT", side="LONG", confidence_score=90)
    decision = EdgeGuard(BotConfig(enable_edge_guard_paper_filter=False), EdgeDb()).evaluate_signal(signal, "RANGE")
    assert decision.allows_paper is True
    assert decision.reason == "edge_guard_filter_disabled"


def test_tp_sl_lab_prints_markers():
    text = TpSlHorizonLab(BotConfig(), EdgeDb()).to_text(hours=24)
    assert TP_START in text
    assert TP_END in text
    assert "final_recommendation: NO LIVE" in text
