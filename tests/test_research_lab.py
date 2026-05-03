import json
from datetime import datetime, timedelta, timezone

from app.config import BotConfig
from app.database import Database
from app.research_lab import (
    ResearchDatasetBuilder,
    ResearchLab,
    ResearchMetrics,
    ResearchRanker,
    make_basic_walkforward_splits,
    reverse_vs_normal_rows,
)


class DummyLogger:
    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


def make_db(tmp_path):
    db = Database(BotConfig(), DummyLogger())
    db.sqlite_path = tmp_path / "research_lab.db"
    db.initialize()
    return db


def insert_observation(db, *, index=0, label=None, ret=0.0, strategy="BREAKOUT", side="LONG", shadow=False, reverse=False):
    timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=index)
    observation_id = db.record_signal_observation(
        {
            "timestamp": timestamp.isoformat(),
            "symbol": "BTCUSDT",
            "side": side,
            "strategy_type": strategy,
            "confidence_score": 88,
            "market_regime": "TREND_UP",
            "entry_price": 100,
            "stop_loss": 98 if side == "LONG" else 102,
            "take_profit_1": 103 if side == "LONG" else 97,
            "take_profit_2": 105 if side == "LONG" else 95,
            "risk_reward_ratio": 1.5,
            "leverage_recommendation": 3,
            "spread_pct": 0.0005,
            "volume_24h_usdt": 100_000_000,
            "funding_rate": 0.0001,
            "open_interest": 10_000,
            "rsi_14": 55,
            "macd_hist": 0.1,
            "atr_14": 1.0,
            "normalized_atr": 0.01,
            "volume_relative": 1.5,
            "distance_to_ema_21": 0.01,
            "distance_to_ema_50": 0.02,
            "distance_to_ema_200": 0.05,
            "momentum_5": 0.01,
            "momentum_15": 0.02,
            "btc_regime": "TREND_UP",
            "btc_momentum_5": 0.01,
            "btc_momentum_15": 0.02,
            "btc_normalized_atr": 0.01,
            "eth_momentum_5": 0.01,
            "number_of_symbols_bullish": 7,
            "number_of_symbols_bearish": 3,
            "market_risk_on": 1,
            "market_risk_off": 0,
            "range_width_pct": 0.03,
            "body_pct": 0.01,
            "upper_wick_pct": 0.002,
            "lower_wick_pct": 0.003,
            "shadow_strategy": int(shadow),
            "variant_params_json": json.dumps({"reverse": True}) if reverse else json.dumps({}),
            "original_side": "SHORT" if reverse and side == "LONG" else "LONG" if reverse else side,
            "original_strategy_type": strategy,
        }
    )
    if label is not None:
        db.record_signal_label(
            {
                "observation_id": observation_id,
                "label": label,
                "first_barrier_hit": "TP1" if label == 1 else "SL" if label == -1 else "TIME",
                "bars_to_outcome": 4,
                "max_favorable_excursion": 0.03,
                "max_adverse_excursion": -0.01,
                "realized_return_pct": ret,
                "simulated_pnl": ret * 100,
                "would_have_won": int(label == 1),
            }
        )
    return observation_id


def test_dataset_builder_adds_features_without_future_outcome_columns(tmp_path):
    db = make_db(tmp_path)
    insert_observation(db, index=1, label=1, ret=0.03)
    dataset = ResearchDatasetBuilder(db).build()

    assert dataset[0]["stop_distance_pct"] == 0.02
    assert dataset[0]["tp1_to_sl_ratio"] == 1.5
    assert dataset[0]["is_btc_aligned"] == 1
    feature_columns = ResearchDatasetBuilder(db).feature_columns(dataset)
    assert "label" not in feature_columns
    assert "realized_return_pct" not in feature_columns
    assert "first_barrier_hit" not in feature_columns


def test_walkforward_split_is_temporal():
    rows = [
        {"timestamp": f"2026-01-01T00:{minute:02d}:00+00:00", "label": 1 if minute % 2 == 0 else -1}
        for minute in range(12)
    ]
    split = make_basic_walkforward_splits(rows)[0]
    assert split.train[-1]["timestamp"] < split.validation[0]["timestamp"]
    assert split.validation[-1]["timestamp"] < split.test[0]["timestamp"]


def test_metrics_profit_factor_and_expectancy_are_correct():
    metrics = ResearchMetrics.calculate(
        [
            {"label": 1, "realized_return_pct": 0.03, "first_barrier_hit": "TP1"},
            {"label": -1, "realized_return_pct": -0.02, "first_barrier_hit": "SL"},
            {"label": 0, "realized_return_pct": 0.0, "first_barrier_hit": "TIME"},
        ]
    )
    assert round(metrics["profit_factor"], 2) == 1.50
    assert round(metrics["expectancy"], 5) == 0.00333


def test_ranking_rejects_less_than_100_labels():
    rows = [
        {"timestamp": f"2026-01-01T00:{idx:02d}:00+00:00", "label": 1, "realized_return_pct": 0.02, "strategy_type": "BREAKOUT"}
        for idx in range(99)
    ]
    accepted, rejected = ResearchRanker().rank(rows)
    assert accepted == []
    assert any(item["status"] == "REJECTED_TOO_FEW_SAMPLES" for item in rejected)


def test_ranking_rejects_profit_factor_below_1_2():
    rows = []
    for idx in range(120):
        win = idx % 3 == 0
        rows.append(
            {
                "timestamp": f"2026-01-01T{idx // 60:02d}:{idx % 60:02d}:00+00:00",
                "label": 1 if win else -1,
                "realized_return_pct": 0.01 if win else -0.01,
                "strategy_type": "BREAKOUT",
            }
        )
    _, rejected = ResearchRanker().rank(rows)
    global_rejection = [item for item in rejected if item["name"] == "all_labeled"][0]
    assert global_rejection["status"] == "REJECTED_NO_EDGE"


def test_reverse_vs_normal_comparison_works():
    rows = [
        {
            "symbol": "BTCUSDT",
            "market_regime": "TREND_UP",
            "score_bucket": "85-89",
            "strategy_type": "BREAKOUT",
            "original_strategy_type": "BREAKOUT",
            "label": 1,
            "realized_return_pct": 0.02,
            "shadow_strategy": 0,
            "variant_params_json": "{}",
        },
        {
            "symbol": "BTCUSDT",
            "market_regime": "TREND_UP",
            "score_bucket": "85-89",
            "strategy_type": "REVERSE_BREAKOUT",
            "original_strategy_type": "BREAKOUT",
            "label": -1,
            "realized_return_pct": -0.01,
            "shadow_strategy": 1,
            "variant_params_json": json.dumps({"reverse": True}),
        },
    ]
    comparison = reverse_vs_normal_rows(rows)
    assert comparison[0]["normal_labels"] == 1
    assert comparison[0]["reverse_labels"] == 1


def test_recommended_config_never_activates_live(tmp_path):
    db = make_db(tmp_path)
    insert_observation(db, label=1, ret=0.03)
    lab = ResearchLab(db, BotConfig(), DummyLogger(), reports_dir=tmp_path / "reports")
    path = lab.recommend_config()
    text = path.read_text(encoding="utf-8")
    assert "LIVE_TRADING=true" not in text
    assert "DRY_RUN=false" not in text
    assert "LIVE_TRADING=false" in text
    assert "# NO_LIVE_RECOMMENDED=true" in text


def test_report_generates_without_labels_or_shadow_labels(tmp_path):
    db = make_db(tmp_path)
    insert_observation(db, label=None)
    lab = ResearchLab(db, BotConfig(), DummyLogger(), reports_dir=tmp_path / "reports")
    report = lab.build_markdown_report()
    assert "Research Lab Report" in report
    assert "NO ACTIVAR LIVE" in report
    assert "0 reverse/shadow labels" in report


def test_export_creates_expected_files(tmp_path):
    db = make_db(tmp_path)
    insert_observation(db, label=1, ret=0.03)
    lab = ResearchLab(db, BotConfig(), DummyLogger(), reports_dir=tmp_path / "reports")
    path = lab.export()
    expected = {
        "research_lab_report.md",
        "best_strategies.json",
        "rejected_strategies.json",
        "recommended_config.env",
        "feature_importance.csv",
        "walkforward_summary.csv",
        "reverse_vs_normal.csv",
        "tp_sl_optimizer.csv",
    }
    assert expected.issubset({item.name for item in path.iterdir()})
