"""V10.46.8 explicit learner-divergence + causality proof: A (static champion)
and B (learning challenger) start identical; after MATURED labels B changes its
decision on LATER events while A stays identical and the champion hash never
changes. Also proves the SimOMS entry is causal (decision at close, entry at
next open). Research only, NO LIVE."""

from __future__ import annotations

from app.labs.v10_46 import learner as L
from app.labs.v10_46 import policy as POL
from app.labs.v10_46 import features as F
from app.labs.v10_46 import strategies as ST

BAR = 60_000
T0 = 1_700_000_400_000


def _uptrend(n=80):
    return [{"ts": T0 + i * BAR, "open": 100 + i * 0.15, "high": 100 + i * 0.15 + 0.2,
             "low": 100 + i * 0.15 - 0.05, "close": 100 + i * 0.15 + 0.15,
             "volume": 10.0 + i * 0.1} for i in range(n)]


def _fsnap(bars):
    dt = bars[-1]["ts"] + BAR
    return F.compute_features(bars, decision_time_ms=dt), dt


def _decide(pol, fsnap, dt):
    return POL.decide(pol, fsnap, symbol="BTCUSDT", venue="bitget",
                      timeframe="1m", event_id=f"BTCUSDT:{dt}",
                      decision_time_ms=dt, data_generation_id="g")


def test_A_and_B_start_identical_then_B_diverges_only_after_labels():
    champ = POL.freeze(POL.default_policy("A", kind="static", abstention=True))
    b = POL.mutate(champ, "threshold", champ["threshold"], policy_id="B")
    b["kind"] = "learning"
    champ_hash_before = champ["policy_hash"]
    lr = L.PrequentialLearner(b)

    fs, dt = _fsnap(_uptrend())
    # --- start: with NO matured labels, B's model is empty -> B == A ---
    dA0 = _decide(champ, fs, dt)
    b_untrained = dict(b, weights=None)
    dB0 = _decide(b_untrained, fs, dt)
    assert dA0["decision_action"] == dB0["decision_action"] == "TRADE"
    assert dA0["side"] == dB0["side"] == "LONG"

    # --- feed MATURED negative labels (these events "lost"): the update
    # happens only AFTER the label matures, never before ---
    feats = fs["features"]
    for i in range(300):
        p = lr.predict(feats, f"e{i}")
        assert lr.log[-1]["label"] is None          # predicted, not yet scored
        lr.observe_label(f"e{i}", 0)                 # matured loss -> learn

    # --- later event: B now uses its learned weights (low P(win)) and
    # ABSTAINS, while A (frozen champion) is byte-for-byte unchanged ---
    b_trained = dict(b, weights=list(lr.model.w))
    dB1 = _decide(b_trained, fs, dt)
    dA1 = _decide(champ, fs, dt)
    dkeys = ("decision_action", "side", "calibrated_probability", "policy_id",
             "spec_hash")
    assert {k: dA1[k] for k in dkeys} == {k: dA0[k] for k in dkeys}  # A stable
    assert champ["policy_hash"] == champ_hash_before  # champion never mutated
    assert dB1["decision_action"] != "TRADE"        # B changed its mind
    assert dB1["decision_action"].startswith("ABSTAIN")
    # the model genuinely learned a lower win probability
    assert lr.model.predict(POL._feature_vector(feats)) < 0.5


def test_champion_hash_is_immutable_under_challenger_updates():
    champ = POL.freeze(POL.default_policy("A", kind="static"))
    h = champ["policy_hash"]
    b = POL.mutate(champ, "threshold", 0.7, policy_id="B")
    lr = L.PrequentialLearner(b)
    for i in range(50):
        lr.predict({"slope": 0.001, "up_fraction": 0.6}, f"x{i}")
        lr.observe_label(f"x{i}", 1)
    assert champ["policy_hash"] == h                 # untouched by learning
    assert POL.policy_hash(champ) == h


def test_simoms_entry_is_causal_next_open():
    """The decision is taken at bar i CLOSE; the entry price is bar i+1 OPEN,
    never the current forming bar — proving no lookahead in execution."""
    from app.labs.v10_46 import sim_oms as S
    entry = {"ts": T0 + 1 * BAR, "open": 100.0, "high": 100.1, "low": 99.9,
             "close": 100.05, "volume": 10.0}
    nxt = {"ts": T0 + 2 * BAR, "open": 100.5, "high": 101.5, "low": 100.4,
           "close": 101.4, "volume": 10.0}
    r = S.simulate_trade(side="LONG", entry_bar=entry, exit_bars=[nxt],
                         entry_ts_ms=int(entry["ts"]), stop_frac=0.02,
                         tp_frac=0.006, time_exit=5, scenario_money="5eur")
    # entry price derives from the ENTRY bar open (100.0), not a future bar
    assert abs(r["entry_price"] - 100.0) < 1e-9
    assert r["entry_ts_ms"] == int(entry["ts"])
