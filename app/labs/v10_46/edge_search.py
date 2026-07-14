"""V10.47 gross-first edge search over the existing V10.46 lab (RESEARCH ONLY).

Runs the FULL strategy tournament — P01–P12, Trend Rider variants A–J, plus
No-Trade and random exposure-matched baselines — over a verified dataset
generation, causally and deterministically, through the SAME SimOMS. Signals
are computed ONCE per decision bar and shared across every participant, so all
compete on identical events (paired by event_cluster_id).

Discovery is GROSS-FIRST: gross PnL/EV/PF are reported before real costs, and
each participant is classified NO_GROSS_EDGE / GROSS_EDGE_COST_KILLED /
NET_EDGE_POSITIVE. Everything is euro-first. No orders, no live.
"""

from __future__ import annotations

from typing import Any

from . import families as FAM
from . import event_clock as EC
from . import sim_oms as S

WARMUP = 60


def _classify(gross: float, net: float) -> str:
    if gross <= 0:
        return "NO_GROSS_EDGE"
    if net <= 0:
        return "GROSS_EDGE_COST_KILLED"
    return "NET_EDGE_POSITIVE"


def _participant_metrics(per_cluster: dict) -> dict:
    traded = [c for c in per_cluster.values() if c.get("traded")]
    nets = [c["net_eur"] for c in traded]
    gross = [c["gross_eur"] for c in traded]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x < 0]
    n = len(nets)
    cur = peak = dd = 0.0
    for x in nets:
        cur += x
        peak = max(peak, cur)
        dd = min(dd, cur - peak)
    curg = peakg = ddg = 0.0
    for x in gross:
        curg += x
        peakg = max(peakg, curg)
        ddg = min(ddg, curg - peakg)
    tail = sorted(nets)[:max(1, n // 20)] if n else []
    es = sum(tail) / len(tail) if tail else 0.0
    pf = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else \
        (999.0 if wins else 0.0)
    gwins = [x for x in gross if x > 0]
    glosses = [x for x in gross if x < 0]
    gpf = (sum(gwins) / abs(sum(glosses))) if glosses and sum(glosses) != 0 else \
        (999.0 if gwins else 0.0)
    net_total, gross_total = float(sum(nets)), float(sum(gross))
    fee = float(sum(c.get("fee_eur", 0.0) for c in traded))
    spread = float(sum(c.get("spread_eur", 0.0) for c in traded))
    slip = float(sum(c.get("slippage_eur", 0.0) for c in traded))
    funding = float(sum(c.get("funding_eur", 0.0) for c in traded))
    without_top = float(sum(sorted(nets, reverse=True)[3:])) if n > 3 else net_total
    labelled = [(c["prob"], c["label"]) for c in traded if "prob" in c]
    brier = (sum((p - y) ** 2 for p, y in labelled) / len(labelled)
             if labelled else None)
    return {
        "trades": n, "wins": len(wins), "losses": len(losses),
        "win_rate": round(len(wins) / n, 4) if n else 0.0,
        "avg_win_eur": round(sum(wins) / len(wins), 6) if wins else 0.0,
        "avg_loss_eur": round(sum(losses) / len(losses), 6) if losses else 0.0,
        "gross_pnl_eur": round(gross_total, 6),
        "net_pnl_eur": round(net_total, 6),
        "gross_ev_eur": round(gross_total / n, 6) if n else 0.0,
        "net_ev_eur": round(net_total / n, 6) if n else 0.0,
        "gross_pf": round(gpf, 4), "net_pf": round(pf, 4),
        "gross_drawdown_eur": round(ddg, 6), "max_drawdown_eur": round(dd, 6),
        "expected_shortfall_eur": round(es, 6),
        "fee_eur": round(fee, 6), "spread_eur": round(spread, 6),
        "slippage_eur": round(slip, 6), "funding_eur": round(funding, 6),
        "n_raw": n, "n_eff": len(traded),
        "brier": round(brier, 6) if brier is not None else None,
        "net_without_top3_eur": round(without_top, 6),
        "classification": _classify(gross_total, net_total)}


def _drive(bars, sigs, decide_fn, exit_params, symbol, scenario_money="5eur",
           scenario_cost="observed", cooldown_clusters=1):
    per_cluster: dict = {}
    used: dict = {}
    time_exit = int(exit_params.get("time_exit", 20))
    for i in range(WARMUP, len(bars) - 1):
        ts_i = int(bars[i]["ts"])
        dt = ts_i + EC.BAR_MS
        cluster = EC.cluster_id(symbol, ts_i)
        if cluster in used and (i - used[cluster]) < cooldown_clusters:
            continue
        s = sigs[i]
        event_id = f"{symbol}:{ts_i}"
        # share the precomputed signal + ts by REFERENCE (no per-bar history
        # copy) — this is what keeps the full tournament O(participants * n)
        feats = {"_sig": s, "ts": ts_i}
        d = decide_fn(feats, event_id, dt, cluster)
        if d.get("decision_action") != "TRADE":
            per_cluster.setdefault(cluster, {"net_eur": 0.0, "traded": False})
            continue
        used[cluster] = i
        entry_bar = bars[i + 1]
        exit_bars = bars[i + 2: i + 2 + time_exit]
        res = S.simulate_trade(
            side=d["side"], entry_bar=entry_bar, exit_bars=exit_bars,
            entry_ts_ms=int(entry_bar["ts"]),
            stop_frac=exit_params.get("stop_frac", 0.008),
            tp_frac=exit_params.get("tp_frac", 0.012), time_exit=time_exit,
            scenario_money=scenario_money, scenario_cost=scenario_cost)
        if res["status"] != "OK":
            per_cluster[cluster] = {"net_eur": 0.0, "traded": False}
            continue
        per_cluster[cluster] = {
            "net_eur": res["net_pnl_eur"], "gross_eur": res["gross_pnl_eur"],
            "traded": True, "side": d["side"],
            "prob": d["calibrated_probability"],
            "label": 1 if res["net_pnl_eur"] > 0 else 0,
            "fee_eur": res["fee_eur"], "spread_eur": res["spread_eur"],
            "slippage_eur": res["slippage_eur"], "funding_eur": res["funding_eur"]}
    return per_cluster


def _paired(a: dict, b: dict) -> dict:
    import math
    clusters = set(a) | set(b)
    diffs = []
    for c in clusters:
        if a.get(c, {}).get("traded") or b.get(c, {}).get("traded"):
            diffs.append(b.get(c, {}).get("net_eur", 0.0)
                         - a.get(c, {}).get("net_eur", 0.0))
    n = len(diffs)
    if not n:
        return {"n_paired": 0, "mean_diff_eur": 0.0, "lower_bound_eur": 0.0}
    mean = sum(diffs) / n
    sd = math.sqrt(sum((d - mean) ** 2 for d in diffs) / n) if n > 1 else 0.0
    return {"n_paired": n, "mean_diff_eur": round(mean, 6),
            "lower_bound_eur": round(mean - 1.65 * sd / math.sqrt(n), 6),
            "b_better_frac": round(sum(1 for d in diffs if d > 0) / n, 4)}


def run_edge_search(bars: list[dict], *, symbol: str, venue: str,
                    timeframe: str, data_generation_id: str | None,
                    ref_bars_by_ts: dict | None = None,
                    directions: tuple = (None,), scenario_money: str = "5eur",
                    scenario_cost: str = "observed", log=lambda *a: None) -> dict:
    """Run the full gross-first tournament. `directions` may include None
    (both), 'LONG', 'SHORT' to add LONG-only / SHORT-only research variants."""
    import random as _r
    import time as _t
    # precompute the shared causal signal ONCE per bar using a BOUNDED window
    # (O(SIG_LOOKBACK) each) instead of re-scanning the whole history
    t0 = _t.time()
    lb = FAM.SIG_LOOKBACK
    sigs = [None] * len(bars)
    for i in range(WARMUP, len(bars)):
        sigs[i] = FAM._sig(bars[max(0, i - lb):i + 1])
    log(f"  [sigs] {len(bars)} bars precomputed in {round(_t.time()-t0,1)}s")
    results: dict = {}

    def _add(name, decide_fn, exit_params):
        ta = _t.time()
        pc = _drive(bars, sigs, decide_fn, exit_params, symbol,
                    scenario_money=scenario_money, scenario_cost=scenario_cost)
        results[name] = {"metrics": _participant_metrics(pc), "per_cluster": pc}
        m = results[name]["metrics"]
        log(f"  {name}: {m['classification']} trades={m['trades']} "
            f"net={m['net_pnl_eur']}€ gross={m['gross_pnl_eur']}€ "
            f"({round(_t.time()-ta,1)}s)")

    # P01–P12 (both-direction) + optional LONG-only / SHORT-only
    for fid, fam in FAM.FAMILIES.items():
        for d in directions:
            suffix = "" if d is None else f"_{d}"
            _add(f"{fid}{suffix}", FAM.family_decider(
                fid, symbol=symbol, venue=venue, timeframe=timeframe,
                gen_id=data_generation_id, direction=d,
                ref_bars_by_ts=ref_bars_by_ts), fam["exit"])
    # Trend Rider variants A–J
    for tid, tv in FAM.TREND_VARIANTS.items():
        def mkfn(_tv=tv, _tid=tid):
            spec_hash = FAM.C.canonical_hash({"variant": _tid})

            def decide_fn(feats, event_id, dt, cluster):
                s = feats["_sig"]
                if not s.get("ok"):
                    return FAM._mk("ABSTAIN_DATA_QUALITY", "FLAT", 0.5,
                                   symbol=symbol, venue=venue,
                                   timeframe=timeframe, event_id=event_id,
                                   dt=dt, gen_id=data_generation_id,
                                   reason="ABSTAIN_DATA_QUALITY",
                                   spec_hash=spec_hash, policy_id=_tid)
                ctx = {}
                if ref_bars_by_ts is not None:
                    ref = ref_bars_by_ts.get(int(feats.get("ts", 0)))
                    if ref is not None and s["last"]:
                        ctx["xv_gap"] = (s["last"] - float(ref)) / s["last"]
                a, sd, pb = _tv["fn"](s, ctx)
                return FAM._mk(a, sd, pb, symbol=symbol, venue=venue,
                               timeframe=timeframe, event_id=event_id, dt=dt,
                               gen_id=data_generation_id, reason=a,
                               spec_hash=spec_hash, policy_id=_tid)
            return decide_fn
        _add(tid, mkfn(), FAM.TREND_EXIT)
    # baselines: No-Trade + random exposure-matched
    _add("D_no_trade", lambda f, e, dt, c: FAM._mk(
        "ABSTAIN_LOW_REWARD", "FLAT", 0.5, symbol=symbol, venue=venue,
        timeframe=timeframe, event_id=e, dt=dt, gen_id=data_generation_id,
        reason="NO_TRADE"), FAM.TREND_EXIT)
    rng = _r.Random(4242)

    def _rand(f, e, dt, c):
        if rng.random() < 0.05:
            return FAM._mk("TRADE", "LONG" if rng.random() < 0.5 else "SHORT",
                           0.5, symbol=symbol, venue=venue, timeframe=timeframe,
                           event_id=e, dt=dt, gen_id=data_generation_id,
                           reason="RANDOM")
        return FAM._mk("ABSTAIN_LOW_REWARD", "FLAT", 0.5, symbol=symbol,
                       venue=venue, timeframe=timeframe, event_id=e, dt=dt,
                       gen_id=data_generation_id, reason="NO_TRADE")
    _add("Q_random", _rand, FAM.TREND_EXIT)

    nt = results["D_no_trade"]["per_cluster"]
    rnd_pc = results["Q_random"]["per_cluster"]
    for name, r in results.items():
        r["paired_vs_no_trade"] = _paired(nt, r["per_cluster"])
        r["paired_vs_random"] = _paired(rnd_pc, r["per_cluster"])
        m = r["metrics"]
        r["beats_no_trade"] = m["net_pnl_eur"] > results["D_no_trade"]["metrics"]["net_pnl_eur"]
        r["beats_random"] = m["net_pnl_eur"] > results["Q_random"]["metrics"]["net_pnl_eur"]
        del r["per_cluster"]                     # keep the report compact
    return {"symbol": symbol, "timeframe": timeframe,
            "data_generation_id": data_generation_id, "n_bars": len(bars),
            "results": results}
