from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .catalyst_classifier import CatalystClassifier
from .catalyst_registry import CatalystRegistry
from .config import BotConfig
from .database import Database
from .news_risk_gate import NewsRiskGate
from .paper_policy_lab import PaperPolicyLab
from .policy_backtest import PolicyBacktest
from .walk_forward_validation import WalkForwardValidation


START = "POLICY NEWS SMOKE TEST START"
END = "POLICY NEWS SMOKE TEST END"


class PolicyNewsSmokeTest:
    """End-to-end local smoke test. It writes research rows only."""

    def __init__(self, config: BotConfig, db: Database, logger=None) -> None:
        self.config = config
        self.db = db
        self.logger = logger

    def run(self) -> dict[str, Any]:
        before_open = self.db.get_paper_trade_summary().get("open", 0)
        registry = CatalystRegistry(self.config, self.db)
        bullish_id = "smoke_bullish_xrp_regulation"
        bearish_id = "smoke_bearish_global_hack"
        bullish = registry.add_manual(
            catalyst_id=bullish_id,
            title="Smoke bullish XRP regulatory clarity",
            symbols=["XRPUSDT"],
            category="regulation",
            direction="bullish",
            severity="high",
            confidence=0.85,
            hours_back=2,
            hours_forward=48,
        )
        bearish = registry.add_manual(
            catalyst_id=bearish_id,
            title="Smoke critical security exploit",
            symbols=["GLOBAL"],
            category="hack",
            direction="bearish",
            severity="critical",
            confidence=0.9,
            hours_back=2,
            hours_forward=6,
        )
        self._seed_labels()
        classifier = CatalystClassifier()
        classified_bullish = classifier.classify(title="Crypto market structure clarity bill advances for XRP")
        classified_bearish = classifier.classify(title="Major bridge exploit drains funds")
        news = NewsRiskGate(self.config, self.db).build(hours=24)
        policy = PaperPolicyLab(self.config, self.db).build(hours=24)
        walk = WalkForwardValidation(self.config, self.db).build(hours=24)
        backtest = PolicyBacktest(self.config, self.db).build(hours=24)
        after_open = self.db.get_paper_trade_summary().get("open", 0)
        pass_result = bool(
            bullish
            and bearish
            and classified_bullish.direction == "bullish"
            and classified_bearish.direction == "bearish"
            and news.get("global_decision")
            and isinstance(policy.get("candidate_policies"), list)
            and isinstance(walk.get("policies"), list)
            and "baseline" in backtest
            and after_open == before_open
            and not self.config.live_trading
            and self.config.dry_run
            and self.config.paper_trading
        )
        return {
            "bullish_catalyst_created": bool(bullish),
            "bearish_catalyst_created": bool(bearish),
            "classifier_checked": classified_bullish.direction == "bullish" and classified_bearish.direction == "bearish",
            "news_risk_gate_checked": bool(news.get("global_decision")),
            "policy_candidates": len(policy.get("candidate_policies", [])),
            "walk_forward_checked": isinstance(walk.get("policies"), list),
            "policy_backtest_checked": "baseline" in backtest,
            "opened_paper_trades": int(after_open) - int(before_open),
            "LIVE_TRADING": self.config.live_trading,
            "DRY_RUN": self.config.dry_run,
            "PAPER_TRADING": self.config.paper_trading,
            "result": "PASS" if pass_result else "FAIL",
        }

    def to_text(self) -> str:
        payload = self.run()
        lines = [
            START,
            f"bullish_catalyst_created: {str(payload['bullish_catalyst_created']).lower()}",
            f"bearish_catalyst_created: {str(payload['bearish_catalyst_created']).lower()}",
            f"classifier_checked: {str(payload['classifier_checked']).lower()}",
            f"news_risk_gate_checked: {str(payload['news_risk_gate_checked']).lower()}",
            f"policy_candidates: {payload['policy_candidates']}",
            f"walk_forward_checked: {str(payload['walk_forward_checked']).lower()}",
            f"policy_backtest_checked: {str(payload['policy_backtest_checked']).lower()}",
            f"opened_paper_trades: {payload['opened_paper_trades']}",
            f"LIVE_TRADING={str(payload['LIVE_TRADING']).lower()}",
            f"DRY_RUN={str(payload['DRY_RUN']).lower()}",
            f"PAPER_TRADING={str(payload['PAPER_TRADING']).lower()}",
            f"result: {payload['result']}",
            END,
        ]
        return "\n".join(lines)

    def _seed_labels(self) -> None:
        now = datetime.now(timezone.utc)
        for index in range(80):
            ts = (now - timedelta(minutes=120 - index)).isoformat()
            obs_id = self.db.record_signal_observation({
                "timestamp": ts,
                "symbol": "XRPUSDT",
                "side": "LONG",
                "strategy_type": "SMOKE_POLICY",
                "confidence_score": 86,
                "market_regime": "RISK_ON",
                "entry_price": 100.0,
                "score_bucket": "80-89",
            })
            win = index % 3 != 0
            self.db.record_signal_label({
                "timestamp": ts,
                "observation_id": obs_id,
                "label": 1 if win else -1,
                "first_barrier_hit": "TP1" if win else "SL",
                "bars_to_outcome": 10,
                "realized_return_pct": 1.0 if win else -0.5,
            })
        for index in range(30):
            ts = (now - timedelta(minutes=60 - index)).isoformat()
            obs_id = self.db.record_signal_observation({
                "timestamp": ts,
                "symbol": "BTCUSDT",
                "side": "SHORT",
                "strategy_type": "SMOKE_POLICY",
                "confidence_score": 90,
                "market_regime": "RISK_OFF",
                "entry_price": 100.0,
                "score_bucket": "90-100",
            })
            self.db.record_signal_label({
                "timestamp": ts,
                "observation_id": obs_id,
                "label": -1,
                "first_barrier_hit": "SL",
                "bars_to_outcome": 6,
                "realized_return_pct": -1.0,
            })
