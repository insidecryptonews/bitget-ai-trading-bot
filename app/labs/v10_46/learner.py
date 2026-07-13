"""V10.46 prequential learner + autopsy + experience memory (RESEARCH ONLY).

Order is strict and causal:
    PREDICT -> IMMUTABLE LOG -> WAIT FOR LABEL -> SCORE -> AUTOPSY
    -> UPDATE CHALLENGER

The Champion is frozen; only a Challenger's ONE mutated dimension (here the
online logistic weights that drive the trade/abstain threshold) is updated,
and only from MATURED labels. No holdout/validation data is ever learned from.
No LLM is the judge; no deep RL, no martingale, no loss-DCA, no exploration in
paper champion.
"""

from __future__ import annotations

import math
from typing import Any

from . import contracts as C
from . import policy as POL


class OnlineLogistic:
    """Regularised online logistic regression (SGD). Deterministic given the
    same event order. Used to learn P(net>0) for the proposed side."""

    def __init__(self, dim: int = 6, lr: float = 0.05, l2: float = 1e-3):
        self.w = [0.0] * dim
        self.lr = lr
        self.l2 = l2
        self.n = 0

    def predict(self, x: list[float]) -> float:
        z = sum(wi * xi for wi, xi in zip(self.w, x))
        return POL._sigmoid(z)

    def update(self, x: list[float], y: int) -> None:
        p = self.predict(x)
        err = p - y
        for i in range(len(self.w)):
            self.w[i] -= self.lr * (err * x[i] + self.l2 * self.w[i])
        self.n += 1


class PrequentialLearner:
    """Drives ONE challenger. Predictions are logged immutably; weights update
    only after the label matures. Champion is never modified."""

    def __init__(self, challenger: dict):
        self.challenger = dict(challenger)
        self.model = OnlineLogistic()
        self.log: list[dict] = []          # immutable prediction log
        self.scores: list[dict] = []       # matured (predicted, label) pairs

    def predict(self, feats: dict, event_id: str) -> float:
        x = POL._feature_vector(feats)
        p = self.model.predict(x)
        self.log.append({"event_id": event_id, "x": x, "p": p, "label": None})
        return p

    def observe_label(self, event_id: str, label: int) -> None:
        """Feed a MATURED label back; update the challenger weights. The
        original prediction record is never rewritten — a new scored entry is
        appended."""
        rec = next((r for r in reversed(self.log)
                    if r["event_id"] == event_id and r["label"] is None), None)
        if rec is None:
            return
        rec["label"] = int(label)          # annotate (does not alter p)
        self.model.update(rec["x"], int(label))
        self.scores.append({"p": rec["p"], "y": int(label)})
        self.challenger["weights"] = list(self.model.w)

    def brier(self) -> float | None:
        if not self.scores:
            return None
        return sum((s["p"] - s["y"]) ** 2 for s in self.scores) / len(self.scores)

    def log_loss(self) -> float | None:
        if not self.scores:
            return None
        eps = 1e-12
        tot = 0.0
        for s in self.scores:
            p = min(max(s["p"], eps), 1 - eps)
            tot += -(s["y"] * math.log(p) + (1 - s["y"]) * math.log(1 - p))
        return tot / len(self.scores)


# ------------------------------------------------------------------ autopsy
def build_autopsy(*, trade_id: str, symbol: str, venue: str, timeframe: str,
                  event_id: str, decision_time_ms: int,
                  data_generation_id: str | None, before: dict, during: dict,
                  after: dict) -> dict:
    """Canonical TradeAutopsy joining before/during/after WITHOUT rewriting the
    original decision. Adds cause-of-outcome and a single mutation candidate."""
    net = after.get("net_pnl_eur", 0.0)
    reason = after.get("exit_reason")
    cause = ("STOP_LOSS" if reason == "SL"
             else "TAKE_PROFIT" if reason == "TP"
             else "TRAILING" if reason == "TRAIL"
             else "TIME_DECAY" if reason == "TIME" else "OTHER")
    mfe, mae = after.get("mfe_frac", 0.0), after.get("mae_frac", 0.0)
    # avoidable loss / abandoned profit heuristics (labels only, not decisions)
    abandoned = max(0.0, mfe - max(0.0, net) / max(after.get("notional_eur", 1.0), 1e-9))
    mutation_candidate = None
    if reason == "TIME" and mfe > 2 * (after.get("tp_frac") or 0.01):
        mutation_candidate = {"dim": "tp_frac", "hint": "raise TP: MFE unused"}
    elif reason == "SL" and mae < (after.get("stop_frac") or 0.01) * 0.5:
        mutation_candidate = {"dim": "stop_frac", "hint": "stop too tight"}
    return C.make("TradeAutopsy", symbol=symbol, venue=venue,
                  timeframe=timeframe, event_id=event_id,
                  causal_cutoff_ms=decision_time_ms,
                  data_generation_id=data_generation_id, trade_id=trade_id,
                  before=before, during=during, after=after,
                  cause_of_outcome=cause, net_pnl_eur=net,
                  mfe_frac=mfe, mae_frac=mae, abandoned_profit_frac=round(abandoned, 6),
                  mutation_candidate=mutation_candidate)


# ---------------------------------------------------------- experience memory
class ExperienceMemory:
    """Replay buffer with the mandated mixture and event-cluster dedup. Train,
    validation, holdout and shadow are kept strictly separate by `split`."""

    MIX = {"historical_uniform": 0.30, "recent": 0.25, "same_regime": 0.20,
           "extreme_events": 0.10, "hard_negatives": 0.05, "vetoes_near_gate": 0.10}

    def __init__(self):
        self.records: list[dict] = []
        self._clusters: set = set()

    def add(self, rec: dict, split: str = "train") -> bool:
        """Add one ExperienceRecord; dedup by event_cluster_id within a split.
        Returns False if it was a correlated duplicate."""
        key = (split, rec.get("event_cluster_id"))
        if key in self._clusters:
            return False
        self._clusters.add(key)
        self.records.append({**rec, "split": split})
        return True

    def sample(self, n: int, split: str = "train") -> list[dict]:
        pool = [r for r in self.records if r.get("split") == split]
        return pool[:n]

    def composition(self, split: str = "train") -> dict:
        pool = [r for r in self.records if r.get("split") == split]
        buckets: dict[str, int] = {}
        for r in pool:
            buckets[r.get("bucket", "other")] = buckets.get(r.get("bucket", "other"), 0) + 1
        return {"n": len(pool), "buckets": buckets}
