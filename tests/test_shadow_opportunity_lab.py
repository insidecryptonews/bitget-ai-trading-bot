from __future__ import annotations

from app.config import BotConfig, PROJECT_ROOT
from app.shadow_opportunity_lab import END, START, ShadowOpportunityLab
from app.training_summary import TrainingSummary


class PoorEdgeDb:
    def get_signal_label_summary_since(self, since):
        return {
            "total_labels": 100,
            "time_count": 70,
            "sl_count": 28,
            "tp1_count": 2,
            "tp2_count": 0,
            "profit_factor": 0.18,
        }

    def get_high_score_label_summary_since(self, *args, **kwargs):
        return {
            "total_labels": 80,
            "time_count": 60,
            "sl_count": 19,
            "tp1_count": 1,
            "tp2_count": 0,
            "profit_factor": 0.12,
        }

    def get_training_observation_summary_since(self, *args, **kwargs):
        return {
            "total": 200,
            "long_count": 50,
            "short_count": 70,
            "no_trade_count": 80,
            "high_score_count": 120,
            "regimes": [{"key": "CHOPPY_MARKET", "count": 90}],
            "top_symbols": [{"key": "SOLUSDT", "count": 50, "max_score": 100}],
        }

    def get_paper_trade_summary(self):
        return {"open": 1, "closed": 28}

    def get_event_type_counts_since(self, since):
        return {"training_slot_block": 100, "training_high_score_missed": 20}

    def get_shadow_opportunity_group_summaries_since(self, *args, **kwargs):
        return [
            {
                "group_value": "SOLUSDT",
                "total_labels": 80,
                "time_count": 60,
                "sl_count": 19,
                "tp1_count": 1,
                "tp2_count": 0,
                "profit_factor": 0.12,
                "time_ratio": 0.75,
                "sl_ratio": 0.2375,
                "tp_ratio": 0.0125,
            }
        ]

    def get_missed_high_score_summary_since(self, *args, **kwargs):
        return {"total": 20, "by_reason": [{"reason": "Sin slots", "count": 20}]}


def test_shadow_opportunity_cli_exists():
    text = (PROJECT_ROOT / "app" / "research_lab.py").read_text(encoding="utf-8")
    assert '"shadow-opportunity"' in text


def test_shadow_opportunity_prints_markers():
    text = ShadowOpportunityLab(BotConfig(), PoorEdgeDb()).to_text(hours=24)
    assert START in text
    assert END in text
    assert "DO NOT EXPAND SLOTS" in text


def test_training_summary_prioritizes_poor_edge_over_slots():
    text = TrainingSummary(BotConfig(), PoorEdgeDb()).build(hours=6)
    assert "recommendation: NEED_RESEARCH_POOR_EDGE" in text
    assert "PF=0.18" in text


def test_acceleration_plan_prioritizes_poor_edge_over_slots():
    text = TrainingSummary(BotConfig(), PoorEdgeDb()).acceleration_plan(hours=24)
    assert "biggest_problem: poor_edge" in text
    assert "no ampliar slots" in text
