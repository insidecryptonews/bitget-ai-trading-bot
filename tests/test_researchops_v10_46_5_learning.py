"""V10.46.5 learner + champion/challenger + paired tournament + autopsy +
memory + promotion controller: frozen champion, one-mutation challenger,
prequential predict-before-label order, paired A/B/C/D over the same events,
and a deterministic promotion gate that never enables live. Research only,
NO LIVE."""

from __future__ import annotations

import random

import pytest

from app.labs.v10_46 import contracts as C
from app.labs.v10_46 import learner as L
from app.labs.v10_46 import policy as POL
from app.labs.v10_46 import promotion as PR
from app.labs.v10_46 import tournament as T

T0 = 1_700_000_400_000
BAR = 60_000


def _trend_bars(n=1200, seed=1):
    """A regime-switching series with real up/down trends and noise so the
    strategies have something to act on (and abstain within ranges)."""
    rng = random.Random(seed)
    price, bars = 100.0, []
    for i in range(n):
        phase = (i // 120) % 3               # trend up / range / trend down
        drift = 0.0011 if phase == 0 else (-0.0011 if phase == 2 else 0.0)
        step = drift + rng.uniform(-0.0009, 0.0009)
        new = price * (1 + step)
        bars.append({"ts": T0 + i * BAR, "open": price,
                     "high": max(price, new) * 1.0006,
                     "low": min(price, new) * 0.9994, "close": new,
                     "volume": 10.0 + rng.random()})
        price = new
    return bars


# ==========================================================================
# POLICY: frozen champion, one-mutation challenger
# ==========================================================================

def test_champion_frozen_and_one_mutation_challenger():
    champ = POL.freeze(POL.default_policy("champ", kind="static"))
    assert champ["frozen"] and champ["policy_hash"]
    chal = POL.mutate(champ, "threshold", 0.6, policy_id="chal1")
    assert chal["parent_policy_id"] == "champ"
    assert chal["mutation"]["dim"] == "threshold"
    assert chal["threshold"] == 0.6 and champ["threshold"] == 0.55
    # only ONE dimension changed
    diffs = [k for k in ("threshold", "stop_frac", "tp_frac", "time_exit",
                         "trailing_frac", "abstention", "slope_min")
             if chal[k] != champ[k]]
    assert diffs == ["threshold"]
    with pytest.raises(ValueError):
        POL.mutate(champ, "not_a_dim", 1, policy_id="x")


# ==========================================================================
# PREQUENTIAL LEARNER: predict before label; champion never touched
# ==========================================================================

def test_prequential_predicts_before_label_and_updates():
    chal = POL.default_policy("B", kind="learning")
    lr = L.PrequentialLearner(chal)
    feats = {"slope": 0.001, "up_fraction": 0.7, "vol_accel": 1.2,
             "move_consumed": 0.4, "volatility": 0.001}
    p0 = lr.predict(feats, "e1")
    assert 0.0 <= p0 <= 1.0
    # label logged as pending until it matures
    assert lr.log[-1]["label"] is None
    lr.observe_label("e1", 1)
    assert lr.log[-1]["label"] == 1
    assert lr.challenger["weights"] is not None       # challenger updated
    # feeding many positive labels moves the prediction up
    for i in range(200):
        lr.predict(feats, f"p{i}")
        lr.observe_label(f"p{i}", 1)
    assert lr.model.predict(POL._feature_vector(feats)) > p0
    assert lr.brier() is not None


def test_autopsy_does_not_rewrite_decision():
    before = {"decision": "TRADE", "side": "LONG"}
    after = {"net_pnl_eur": -0.02, "exit_reason": "SL", "mfe_frac": 0.001,
             "mae_frac": 0.004, "notional_eur": 5.0, "stop_frac": 0.008,
             "tp_frac": 0.012}
    a = L.build_autopsy(trade_id="t1", symbol="X", venue="bitget",
                        timeframe="1m", event_id="X:1", decision_time_ms=1000,
                        data_generation_id="g", before=before, during={},
                        after=after)
    assert a["before"] == before                      # original untouched
    assert a["cause_of_outcome"] == "STOP_LOSS"


def test_experience_memory_dedup_by_cluster():
    mem = L.ExperienceMemory()
    r = C.make("ExperienceRecord", symbol="X", venue="bitget", timeframe="1m",
               event_id="X:1", causal_cutoff_ms=1000, event_cluster_id="C1",
               data_generation_id="g", trade_id="t1", features={}, label=1,
               bucket="recent")
    assert mem.add(r, split="train") is True
    assert mem.add(r, split="train") is False         # correlated duplicate
    assert mem.add(r, split="validation") is True     # separate split
    assert mem.composition("train")["n"] == 1


# ==========================================================================
# PAIRED TOURNAMENT A/B/C/D
# ==========================================================================

def _participants():
    champ = POL.freeze(POL.default_policy("A", kind="static", abstention=True))
    b = POL.default_policy("B", kind="learning", abstention=True)
    c = POL.default_policy("C", kind="learning", abstention=False)
    lrB = L.PrequentialLearner(b)
    return {
        "A_static_abstain": {"policy": champ},
        "B_learn_abstain": {"policy": b, "learner": lrB},
        "C_learn_no_abstain": {"policy": c, "learner": L.PrequentialLearner(c)},
        "D_no_trade": {"policy": POL.default_policy("D")},
        "Q_random": {"policy": POL.default_policy("Q"), "random": True},
    }


def test_paired_tournament_same_events_and_pairing():
    bars = _trend_bars()
    out = T.run_tournament(bars, symbol="BTCUSDT", venue="bitget",
                           timeframe="1m", data_generation_id="g",
                           participants=_participants(), log=lambda *a: None)
    r = out["results"]
    assert set(r) == {"A_static_abstain", "B_learn_abstain",
                      "C_learn_no_abstain", "D_no_trade", "Q_random"}
    # No-Trade never trades and nets exactly zero
    assert r["D_no_trade"]["metrics"]["trades"] == 0
    assert r["D_no_trade"]["metrics"]["net_pnl_eur"] == 0.0
    # abstaining policies trade fewer clusters than the no-abstention one
    assert r["A_static_abstain"]["metrics"]["trades"] <= \
        r["C_learn_no_abstain"]["metrics"]["trades"]
    # paired B-vs-A is computed over shared clusters
    p = out["paired"]["B_vs_A"]
    assert "mean_diff_eur" in p and "lower_bound_eur" in p
    # every metric is finite and euro-denominated
    for name, res in r.items():
        m = res["metrics"]
        assert isinstance(m["net_pnl_eur"], float)
        assert m["n_eff"] <= m["n_raw"] or m["n_raw"] == 0


def test_determinism_of_tournament():
    bars = _trend_bars(seed=7)
    a = T.run_tournament(bars, symbol="X", venue="bitget", timeframe="1m",
                         data_generation_id="g", participants=_participants())
    b = T.run_tournament(bars, symbol="X", venue="bitget", timeframe="1m",
                         data_generation_id="g", participants=_participants())
    for name in a["results"]:
        assert a["results"][name]["metrics"]["net_pnl_eur"] == \
            b["results"][name]["metrics"]["net_pnl_eur"]


# ==========================================================================
# PROMOTION CONTROLLER (never enables live)
# ==========================================================================

def test_promotion_holds_without_edge_and_never_enables_live():
    weak = {"clusters": 5, "n_eff": 5, "net_pnl_eur": -0.1,
            "max_drawdown_eur": -2.0, "brier": 0.4, "net_without_top3_eur": -0.2}
    d = PR.promotion_decision(
        "B", "SHADOW_CANDIDATE", weak, symbol="X", venue="bitget",
        timeframe="1m", event_id="X:1", decision_time_ms=1000,
        data_generation_id="g", paired_lb_eur=-0.01, no_trade_net=0.0,
        random_net=0.0, dataset_verified=True, registry_closed=True,
        holdout_single_use_ok=True)
    assert d["decision"] == "HOLD"
    assert d["to_state"] == "SHADOW_CANDIDATE"        # no advance
    assert d["can_send_real_orders"] is False
    assert d["final_recommendation"] == "NO LIVE"


def test_promotion_to_live_readiness_requires_audit():
    strong = {"clusters": 50, "n_eff": 50, "net_pnl_eur": 0.5,
              "max_drawdown_eur": -0.3, "brier": 0.2,
              "net_without_top3_eur": 0.2}
    kw = dict(symbol="X", venue="bitget", timeframe="1m", event_id="X:1",
              decision_time_ms=1000, data_generation_id="g",
              paired_lb_eur=0.05, no_trade_net=0.0, random_net=0.0,
              dataset_verified=True, registry_closed=True,
              holdout_single_use_ok=True)
    no_audit = PR.promotion_decision("B", "PAPER_CHAMPION", strong, **kw)
    assert no_audit["decision"] == "HOLD"             # readiness needs an audit
    with_audit = PR.promotion_decision("B", "PAPER_CHAMPION", strong,
                                       independent_audit_ref="audit-1", **kw)
    assert with_audit["decision"] == "PROMOTE"
    assert with_audit["to_state"] == "LIVE_READINESS_ONLY"
    assert with_audit["can_send_real_orders"] is False   # still NOT live
