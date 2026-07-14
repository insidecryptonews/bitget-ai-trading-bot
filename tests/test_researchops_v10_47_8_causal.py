"""V10.47.8 scientific repair — reproduce the flawed cluster-overwrite accounting
and prove the causal single-position ledger fixes it. Research only, NO LIVE."""

from __future__ import annotations

from app.labs.v10_46 import edge_search as ES
from app.labs.v10_46 import causal_ledger as CL
from app.labs.v10_46 import causal_stats as CS
from app.labs.v10_46 import event_clock as EC
from app.labs.v10_46 import families as FAM

BAR = 60_000


def _mk_bar(i, o, h, l, c):
    return {"ts": i * BAR, "open": o, "high": h, "low": l, "close": c,
            "volume": 10.0}


def _two_signal_cluster_bars():
    """After warmup (WARMUP=60): a DOWN leg for a trade entered at bar 71 (from
    the signal at bar 70) that LOSES, then later an UP leg for a trade entered
    at bar 86 (from the signal at bar 85) that WINS. Bars 70 and 85 are in the
    SAME timeframe-aware cluster (1m block = 1h = bars 60..119)."""
    bars = []
    price = 100.0
    for i in range(0, 140):
        o = price
        if 71 <= i <= 75:            # down leg -> trade entered at 71 loses
            c = o * 0.994
        elif 86 <= i <= 90:          # up leg   -> trade entered at 86 wins
            c = o * 1.006
        else:
            c = o
        h = max(o, c) * 1.0005
        l = min(o, c) * 0.9995
        bars.append(_mk_bar(i, o, h, l, c))
        price = c
    return bars


def _decider_trading_at(trade_bars, side="LONG"):
    tb = set(trade_bars)

    def fn(feats, event_id, dt, cluster):
        ts = feats["ts"]
        idx = ts // BAR
        if idx in tb:
            return FAM._mk("TRADE", side, 0.6, symbol="Z", venue="bitget",
                           timeframe="1m", event_id=event_id, dt=dt, gen_id="g",
                           reason="TEST")
        return FAM._mk("ABSTAIN_LOW_REWARD", "FLAT", 0.5, symbol="Z",
                       venue="bitget", timeframe="1m", event_id=event_id, dt=dt,
                       gen_id="g", reason="NO_TRADE")
    return fn


EXIT = {"stop_frac": 0.02, "tp_frac": 0.02, "time_exit": 3}


def test_same_cluster_signals_share_cluster():
    assert EC.cluster_id_tf("Z", 70 * BAR, "1m") == EC.cluster_id_tf("Z", 85 * BAR, "1m")


def test_reproduce_flawed_last_signal_overwrite():
    """FLAWED engine (_drive, per_cluster overwrite): both signals fall in one
    cluster; only the LAST (winning) trade is counted -> net POSITIVE. This
    documents the invalidating bug."""
    bars = _two_signal_cluster_bars()
    sigs = [None] * len(bars)
    # decider does not use _sig; provide a truthy placeholder so _drive proceeds
    for i in range(len(bars)):
        sigs[i] = {"ok": True}
    dec = _decider_trading_at([70, 85])
    pc = ES._drive(bars, sigs, dec, EXIT, "Z", cooldown_clusters=1)
    traded = [c for c in pc.values() if c.get("traded")]
    assert len(traded) == 1                      # only ONE trade survives (overwrite)
    assert traded[0]["net_eur"] > 0              # and it is the LATE winner


def test_causal_ledger_keeps_first_signal_only():
    """CAUSAL engine: the FIRST signal (bar 70, loser) is taken; the second
    (bar 85, same cluster) is skipped as CLUSTER_COOLDOWN -> net NEGATIVE.
    The flawed positive was an ex-post last-signal artifact."""
    bars = _two_signal_cluster_bars()
    sigs = [{"ok": True}] * len(bars)
    dec = _decider_trading_at([70, 85])
    out = CL.drive_causal(bars, sigs, dec, EXIT, symbol="Z", timeframe="1m")
    assert out["counters"]["n_executed"] == 1
    assert out["counters"]["n_skipped_cluster_cooldown"] == 1
    assert out["trades"][0]["opportunity_bar"] == 70      # FIRST signal
    assert out["trades"][0]["net_eur"] < 0                # the loser, honestly


def test_single_position_blocks_overlapping_signal():
    """A signal fired while a position is open is POSITION_ALREADY_OPEN."""
    bars = _two_signal_cluster_bars()
    sigs = [{"ok": True}] * len(bars)
    # two signals 1 bar apart, long holding -> second overlaps the first
    dec = _decider_trading_at([70, 71])
    out = CL.drive_causal(bars, sigs, dec, {"stop_frac": 0.05, "tp_frac": 0.05,
                          "time_exit": 20}, symbol="Z", timeframe="1m")
    assert out["counters"]["n_executed"] == 1
    assert out["counters"]["n_skipped_position_open"] == 1


def test_later_winner_cannot_replace_earlier_loser():
    """Explicit: a later winning signal must NOT retrospectively replace an
    earlier losing one (the exact V10.47 defect)."""
    bars = _two_signal_cluster_bars()
    sigs = [{"ok": True}] * len(bars)
    out = CL.drive_causal(bars, sigs, _decider_trading_at([70, 85]), EXIT,
                          symbol="Z", timeframe="1m")
    nets = [t["net_eur"] for t in out["trades"]]
    assert len(nets) == 1 and nets[0] < 0


def test_ledger_is_append_only_and_immutable():
    bars = _two_signal_cluster_bars()
    sigs = [{"ok": True}] * len(bars)
    out = CL.drive_causal(bars, sigs, _decider_trading_at([70, 85]), EXIT,
                          symbol="Z", timeframe="1m")
    led = out["ledger"]
    recs = led.records()
    recs[0]["kind"] = "TAMPERED"                 # mutate the copy
    assert led.records()[0]["kind"] != "TAMPERED"   # original unchanged
    seqs = [r["seq"] for r in led.records()]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)  # monotone unique
    # a decision record is present and marked immutable
    assert any(r["kind"] == "decision" and r.get("immutable") for r in led.records())


def test_counters_partition_raw_signals():
    bars = _two_signal_cluster_bars()
    sigs = [{"ok": True}] * len(bars)
    out = CL.drive_causal(bars, sigs, _decider_trading_at([70, 85]), EXIT,
                          symbol="Z", timeframe="1m")
    c = out["counters"]
    assert c["n_signals_raw"] == c["n_signals_eligible"] + c["n_skipped_position_open"] \
        + c["n_skipped_cluster_cooldown"]
    assert c["n_executed"] <= c["n_signals_eligible"]


def test_n_eff_same_event_collapses():
    """20 trades in the same cluster/session collapse n_eff toward 1; 20 in
    distinct clusters do not."""
    same = [{"opportunity_bar": i, "entry_bar": i, "exit_index": i + 1,
             "entry_ts": 1000 + i, "cluster": "Z:0", "session": "Z:S0",
             "day": "Z:D0", "net_eur": 0.01, "side": "LONG"} for i in range(20)]
    r_same = CS.n_eff_estimate(same, timeframe="1m")
    assert r_same["n_cluster"] == 1 and r_same["n_eff_final"] <= 2.0
    diff = [{"opportunity_bar": i, "entry_bar": i * 100, "exit_index": i * 100 + 1,
             "entry_ts": i * CS.EC.cluster_block_ms("1m") + 1,
             "cluster": f"Z:{i}", "session": f"Z:S{i}", "day": f"Z:D{i}",
             "net_eur": (1 if i % 2 else -1) * 0.01, "side": "LONG"}
            for i in range(20)]
    r_diff = CS.n_eff_estimate(diff, timeframe="1m")
    assert r_diff["n_cluster"] == 20 and r_diff["n_eff_final"] > r_same["n_eff_final"]


def test_matched_random_baseline_matches_count_and_side():
    bars = _two_signal_cluster_bars()
    trades = [{"opportunity_bar": 31, "entry_bar": 32, "exit_index": 35,
               "entry_ts": 31 * BAR, "cluster": EC.cluster_id_tf("Z", 31 * BAR, "1m"),
               "session": "Z:S0", "day": "Z:D0", "side": "LONG", "net_eur": -0.1,
               "gross_eur": -0.05, "prob": 0.6, "label": 0, "bars_held": 3,
               "exit_reason": "SL"}]
    r = CS.matched_random_null(bars, trades, symbol="Z", timeframe="1m",
                               exit_params=EXIT, reps=50)
    assert r["reps"] == 50
    assert "beats_matched_random" in r and isinstance(r["p_value"], float)


def test_timeframe_intervals_distinct():
    assert EC.interval_ms_for("15m") == 900_000
    assert EC.interval_ms_for("1h") == 3_600_000
    assert EC.interval_ms_for("4h") == 14_400_000
    import pytest
    with pytest.raises(ValueError):
        EC.interval_ms_for("2m")
