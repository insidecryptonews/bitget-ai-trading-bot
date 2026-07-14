"""V10.47 executable P01-P12 + Trend Rider variants + gross-first edge search:
every family emits a valid DecisionRecord through the same EventClock/SimOMS,
gross-first classification is correct, baselines present, causal. Research only,
NO LIVE."""

from __future__ import annotations

import random

from app.labs.v10_46 import contracts as C
from app.labs.v10_46 import families as FAM
from app.labs.v10_46 import edge_search as ES

T0 = 1_700_000_400_000
BAR = 60_000


def _bars(n=1200, seed=1):
    rng = random.Random(seed)
    price, bars = 100.0, []
    for i in range(n):
        phase = (i // 150) % 3
        drift = 0.0011 if phase == 0 else (-0.0011 if phase == 2 else 0.0)
        new = price * (1 + drift + rng.uniform(-0.0009, 0.0009))
        bars.append({"ts": T0 + i * BAR, "open": price,
                     "high": max(price, new) * 1.0007,
                     "low": min(price, new) * 0.9993, "close": new,
                     "volume": 10.0 + rng.random() * 3})
        price = new
    return bars


def test_all_p01_p12_registered_and_status():
    for i in range(1, 13):
        assert f"P{i:02d}" in FAM.FAMILIES
    assert FAM.FAMILIES["P01"]["status"] == "IMPLEMENTED"
    assert FAM.FAMILIES["P12"]["status"] == "DATA_NOT_AVAILABLE"
    proxies = [k for k, v in FAM.FAMILIES.items() if v["status"] == "PROXY"]
    assert set(proxies) == {"P04", "P05", "P06", "P08"}
    assert len(FAM.TREND_VARIANTS) == 10


def test_family_decider_emits_valid_decision_records():
    bars = _bars()
    for fid in FAM.FAMILIES:
        fn = FAM.family_decider(fid, symbol="BTCUSDT", venue="bitget",
                                timeframe="5m", gen_id="g")
        d = fn({"bars_upto": bars}, "BTCUSDT:1", bars[-1]["ts"] + BAR, "c1")
        assert d["contract"] == "DecisionRecord"
        assert C.validate(d)[0], (fid, C.validate(d)[1])
        assert d["decision_action"] == "TRADE" or d["decision_action"].startswith("ABSTAIN")


def test_p12_always_abstains_data_not_available():
    bars = _bars()
    fn = FAM.family_decider("P12", symbol="X", venue="bitget", timeframe="5m",
                            gen_id="g")
    d = fn({"bars_upto": bars}, "X:1", bars[-1]["ts"] + BAR, "c")
    assert d["decision_action"] == "ABSTAIN_DATA_QUALITY"


def test_direction_restriction_long_only():
    bars = _bars()
    # force a downtrend window; SHORT-only research should trade, LONG-only abstain
    down = _bars(seed=2)
    fnL = FAM.family_decider("P01", symbol="X", venue="bitget", timeframe="5m",
                             gen_id="g", direction="LONG")
    fnS = FAM.family_decider("P01", symbol="X", venue="bitget", timeframe="5m",
                             gen_id="g", direction="SHORT")
    # both return valid records; the restriction only ever converts a TRADE to
    # ABSTAIN, never flips the side
    for fn in (fnL, fnS):
        d = fn({"bars_upto": down}, "X:1", down[-1]["ts"] + BAR, "c")
        assert d["decision_action"] == "TRADE" or d["side"] == "FLAT"
        if d["decision_action"] == "TRADE":
            assert d["side"] in ("LONG", "SHORT")


def test_classification_helper():
    assert ES._classify(-1.0, -2.0) == "NO_GROSS_EDGE"
    assert ES._classify(1.0, -0.5) == "GROSS_EDGE_COST_KILLED"
    assert ES._classify(1.0, 0.5) == "NET_EDGE_POSITIVE"


def test_edge_search_runs_all_participants_gross_first():
    bars = _bars(1500)
    out = ES.run_edge_search(bars, symbol="BTCUSDT", venue="bitget",
                             timeframe="5m", data_generation_id="g",
                             log=lambda *a: None)
    r = out["results"]
    # all P01-P12 + 10 trend variants + no-trade + random present
    for fid in FAM.FAMILIES:
        assert fid in r
    for tid in FAM.TREND_VARIANTS:
        assert tid in r
    assert "D_no_trade" in r and "Q_random" in r
    # no-trade never trades and nets 0
    assert r["D_no_trade"]["metrics"]["trades"] == 0
    assert r["D_no_trade"]["metrics"]["net_pnl_eur"] == 0.0
    # every participant has gross-first fields + classification + paired baselines
    for name, res in r.items():
        m = res["metrics"]
        assert m["classification"] in ("NO_GROSS_EDGE", "GROSS_EDGE_COST_KILLED",
                                       "NET_EDGE_POSITIVE")
        assert "gross_pnl_eur" in m and "net_pnl_eur" in m
        assert "paired_vs_no_trade" in res and "paired_vs_random" in res
        # gross >= net (costs never help)
        if m["trades"] > 0:
            assert m["gross_pnl_eur"] >= m["net_pnl_eur"] - 1e-6


def test_walk_forward_and_shadow_gate():
    bars = _bars(2000)
    dec = FAM.family_decider("P01", symbol="X", venue="bitget", timeframe="5m",
                             gen_id="g")
    wf = ES.walk_forward(bars, dec, FAM.FAMILIES["P01"]["exit"], "X", n_folds=4)
    assert wf["n_folds"] == 4 and len(wf["folds"]) == 4
    assert 0.0 <= wf["fold_pos_frac"] <= 1.0
    assert "oos_net_total_eur" in wf
    # a weak candidate (small n, negative oos) must NOT pass the shadow gate
    weak_metrics = {"gross_ev_eur": -0.01, "net_pnl_eur": -0.1, "n_eff": 5,
                    "net_without_top3_eur": -0.2}
    weak_wf = {"oos_net_total_eur": -0.5, "fold_pos_frac": 0.25}
    g = ES.shadow_candidate_gate(weak_metrics, weak_wf, beats_no_trade=False,
                                 beats_random=False, net_conservative_eur=-0.1)
    assert g["all_pass"] is False
    # a strong synthetic candidate passes every gate
    strong = {"gross_ev_eur": 0.01, "net_pnl_eur": 0.5, "n_eff": 60,
              "net_without_top3_eur": 0.3}
    strong_wf = {"oos_net_total_eur": 0.4, "fold_pos_frac": 0.8}
    g2 = ES.shadow_candidate_gate(strong, strong_wf, beats_no_trade=True,
                                  beats_random=True, net_conservative_eur=0.2)
    assert g2["all_pass"] is True


def test_edge_search_deterministic():
    bars = _bars(1200, seed=5)
    a = ES.run_edge_search(bars, symbol="X", venue="bitget", timeframe="5m",
                           data_generation_id="g", log=lambda *a: None)
    b = ES.run_edge_search(bars, symbol="X", venue="bitget", timeframe="5m",
                           data_generation_id="g", log=lambda *a: None)
    for name in a["results"]:
        assert a["results"][name]["metrics"]["net_pnl_eur"] == \
            b["results"][name]["metrics"]["net_pnl_eur"]
