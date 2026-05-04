from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import BotConfig, PROJECT_ROOT
from app.counterfactual_engine import CounterfactualEngine
from app.database import Database
from app.explainability_engine import ExplainabilityEngine
from app.feature_attribution import FeatureAttribution
from app.full_research_report import END_MARKER, START_MARKER, FullResearchReporter
from app.rule_miner import RuleMiner
from app.stop_loss_analyzer import StopLossAnalyzer
from app.walkforward_validator import WalkForwardValidator
from app.win_analyzer import WinAnalyzer


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
    db.sqlite_path = tmp_path / "phase2.db"
    db.initialize()
    return db


def row(**updates):
    base = {
        "id": 1,
        "observation_id": 1,
        "label": -1,
        "first_barrier_hit": "SL",
        "bars_to_outcome": 2,
        "symbol": "BTCUSDT",
        "side": "LONG",
        "strategy_type": "BREAKOUT",
        "market_regime": "CHOPPY_MARKET",
        "confidence_score": 78,
        "entry_price": 100.0,
        "stop_loss": 99.8,
        "take_profit_1": 103.0,
        "take_profit_2": 105.0,
        "risk_reward_ratio": 1.5,
        "volume_relative": 0.6,
        "spread_pct": 0.002,
        "rsi_14": 74,
        "normalized_atr": 0.01,
        "momentum_5": 0.001,
        "momentum_15": -0.001,
        "btc_momentum_5": -0.01,
        "btc_momentum_15": -0.01,
        "eth_momentum_5": -0.01,
        "market_risk_on": 0,
        "market_risk_off": 0,
        "number_of_symbols_bullish": 2,
        "number_of_symbols_bearish": 8,
        "max_favorable_excursion": 0.02,
        "max_adverse_excursion": -0.002,
        "realized_return_pct": -0.002,
        "timestamp": "2026-01-01T00:00:00+00:00",
    }
    base.update(updates)
    return base


def insert_labeled(db, index=0, **updates):
    data = row(**updates)
    ts = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=index)
    obs_id = db.record_signal_observation(
        {
            "timestamp": ts.isoformat(),
            "symbol": data["symbol"],
            "side": data["side"],
            "strategy_type": data["strategy_type"],
            "confidence_score": data["confidence_score"],
            "market_regime": data["market_regime"],
            "entry_price": data["entry_price"],
            "stop_loss": data["stop_loss"],
            "take_profit_1": data["take_profit_1"],
            "take_profit_2": data["take_profit_2"],
            "risk_reward_ratio": data["risk_reward_ratio"],
            "volume_relative": data["volume_relative"],
            "spread_pct": data["spread_pct"],
            "rsi_14": data["rsi_14"],
            "normalized_atr": data["normalized_atr"],
            "momentum_5": data["momentum_5"],
            "momentum_15": data["momentum_15"],
            "btc_momentum_5": data["btc_momentum_5"],
            "btc_momentum_15": data["btc_momentum_15"],
            "eth_momentum_5": data["eth_momentum_5"],
            "market_risk_on": data["market_risk_on"],
            "market_risk_off": data["market_risk_off"],
            "number_of_symbols_bullish": data["number_of_symbols_bullish"],
            "number_of_symbols_bearish": data["number_of_symbols_bearish"],
        }
    )
    db.record_signal_label(
        {
            "observation_id": obs_id,
            "label": data["label"],
            "first_barrier_hit": data["first_barrier_hit"],
            "bars_to_outcome": data["bars_to_outcome"],
            "max_favorable_excursion": data["max_favorable_excursion"],
            "max_adverse_excursion": data["max_adverse_excursion"],
            "realized_return_pct": data["realized_return_pct"],
            "simulated_pnl": data["realized_return_pct"] * 100,
            "would_have_won": int(data["label"] == 1),
        }
    )
    return obs_id


def test_explainability_generates_reason_codes():
    explanation = ExplainabilityEngine().explain_row(row())
    assert explanation["primary_reason"] == "CHOPPY_MARKET"
    assert "BTC_NOT_ALIGNED" in explanation["secondary_reasons_json"]


def test_explainability_works_without_labels(tmp_path):
    db = make_db(tmp_path)
    assert ExplainabilityEngine(db).generate() == []


def test_stop_loss_analyzer_detects_sl_dominant():
    result = StopLossAnalyzer().analyze_rows([row(), row(), row(label=1, first_barrier_hit="TP1", realized_return_pct=0.03)])
    assert result["total_sl"] == 2
    assert result["total_sl"] > result["total_tp"]


def test_stop_loss_analyzer_detects_stop_too_tight():
    result = StopLossAnalyzer().analyze_rows([row(stop_loss=99.8)])
    assert "STOP_TOO_TIGHT" in result["reason_counts"]


def test_stop_loss_analyzer_detects_choppy_market():
    result = StopLossAnalyzer().analyze_rows([row(market_regime="CHOPPY_MARKET")])
    assert result["reason_counts"]["CHOPPY_MARKET"] == 1


def test_win_analyzer_detects_winning_conditions():
    rows = [row(label=1, first_barrier_hit="TP1", realized_return_pct=0.03, volume_relative=2.0)]
    clusters = WinAnalyzer().analyze_rows(rows)
    assert clusters[0]["total_tp"] == 1
    assert "avg_volume_relative" in clusters[0]["common_features_json"]


def test_counterfactual_reverse_converts_long_short_and_short_long():
    cf = CounterfactualEngine()
    reverse_long = [item for item in cf.simulate_row(row(side="LONG")) if item["scenario_name"] == "REVERSE_SIDE"][0]
    reverse_short = [item for item in cf.simulate_row(row(side="SHORT", stop_loss=100.2, take_profit_1=97)) if item["scenario_name"] == "REVERSE_SIDE"][0]
    assert reverse_long["simulated_side"] == "SHORT"
    assert reverse_short["simulated_side"] == "LONG"


def test_counterfactual_closer_tp_can_improve_loss():
    scenario = [item for item in CounterfactualEngine().simulate_row(row(max_favorable_excursion=0.02)) if item["scenario_name"] == "CLOSER_TP_0_5X"][0]
    assert scenario["simulated_first_barrier_hit"] == "TP1"
    assert scenario["improved_result"] == 1


def test_counterfactual_wider_sl_can_help():
    scenario = [item for item in CounterfactualEngine().simulate_row(row(stop_loss=98.0, max_adverse_excursion=-0.025)) if item["scenario_name"] == "WIDER_STOP_2X"][0]
    assert scenario["simulated_first_barrier_hit"] == "TIME"
    assert scenario["improved_result"] == 1


def test_no_trade_filter_avoids_bad_signals():
    scenario = [item for item in CounterfactualEngine().simulate_row(row(market_regime="CHOPPY_MARKET")) if item["scenario_name"] == "NO_TRADE_IF_CHOPPY"][0]
    assert scenario["would_trade"] == 0
    assert scenario["avoided_loss"] == 1


def test_feature_attribution_does_not_use_future_outcomes_as_features():
    result = FeatureAttribution().analyze_rows([row(), row(label=1, first_barrier_hit="TP1", realized_return_pct=0.03)])
    all_features = str(result)
    assert "realized_return_pct" not in all_features
    assert "first_barrier_hit" not in all_features


def test_rule_miner_does_not_recommend_rules_with_few_samples():
    rules = RuleMiner().mine_rows([row(label=1, first_barrier_hit="TP1", realized_return_pct=0.03)] * 10)
    assert rules
    assert all(rule["rule_type"] == "OBSERVE_ONLY" for rule in rules)


def test_rule_miner_blocks_low_pf_rules():
    rows = [row(label=-1, first_barrier_hit="SL", realized_return_pct=-0.02, timestamp=f"2026-01-01T00:{i:02d}:00+00:00") for i in range(120)]
    rules = RuleMiner().mine_rows(rows)
    assert any(rule["rule_type"] == "BLOCK" for rule in rules)


def test_walkforward_detects_overfitting():
    rows = []
    for i in range(150):
        good = i < 50
        rows.append(row(label=1 if good else -1, first_barrier_hit="TP1" if good else "SL", realized_return_pct=0.03 if good else -0.02, timestamp=f"2026-01-01T{i//60:02d}:{i%60:02d}:00+00:00"))
    result = WalkForwardValidator(min_samples=100).validate(rows)
    assert not result["stable"]
    assert result["overfit_risk"] > 0


def test_full_report_includes_sl_analysis(tmp_path):
    db = make_db(tmp_path)
    insert_labeled(db)
    report = FullResearchReporter(db, BotConfig(), DummyLogger(), reports_dir=tmp_path / "reports").build_report()
    assert "Stop Loss Analysis" in report


def test_full_report_includes_counterfactual_summary(tmp_path):
    db = make_db(tmp_path)
    insert_labeled(db)
    report = FullResearchReporter(db, BotConfig(), DummyLogger(), reports_dir=tmp_path / "reports").build_report()
    assert "Counterfactual Summary" in report


def test_full_report_keeps_start_end_markers(tmp_path):
    db = make_db(tmp_path)
    report = FullResearchReporter(db, BotConfig(), DummyLogger(), reports_dir=tmp_path / "reports").build_report()
    assert START_MARKER in report
    assert END_MARKER in report


def test_recommended_config_never_activates_live(tmp_path):
    db = make_db(tmp_path)
    reporter = FullResearchReporter(db, BotConfig(), DummyLogger(), reports_dir=tmp_path / "reports")
    reporter.build_report()
    text = (tmp_path / "reports" / "recommended_config.env").read_text(encoding="utf-8")
    assert "LIVE_TRADING=true" not in text
    assert "DRY_RUN=false" not in text


def test_live_trading_default_untouched():
    assert BotConfig().live_trading is False


def test_dry_run_default_untouched():
    assert BotConfig().dry_run is True


def test_risk_manager_not_coupled_to_research_phase2():
    text = (PROJECT_ROOT / "app" / "risk_manager.py").read_text(encoding="utf-8")
    assert "ExplainabilityEngine" not in text
    assert "CounterfactualEngine" not in text


def test_execution_engine_live_not_coupled_to_research_phase2():
    text = (PROJECT_ROOT / "app" / "execution_engine.py").read_text(encoding="utf-8")
    assert "research" not in text.lower()
    assert "counterfactual" not in text.lower()


def test_isolated_margin_default_untouched():
    assert BotConfig().margin_mode == "isolated"
    assert BotConfig().force_isolated_margin is True

