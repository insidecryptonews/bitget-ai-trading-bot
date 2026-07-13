"""V10.46 paired tournament + promotion controller (RESEARCH ONLY).

Every participant sees the SAME events, the SAME EventClock, the SAME SimOMS,
the SAME money scenarios and the SAME periods. Decisions are causal (decide at
bar close, enter at next open). Comparison is PAIRED by event_cluster_id so
policies are never compared on different opportunities. No participant can
place a real order; the Promotion Controller is deterministic and LIVE is
never an executable state.
"""

from __future__ import annotations

import math
import random
from typing import Any, Callable

from . import event_clock as EC
from . import features as F
from . import policy as POL
from . import sim_oms as S

WARMUP = 60


def _drive(bars: list[dict], *, symbol: str, venue: str, timeframe: str,
           data_generation_id: str | None, decide_fn: Callable,
           exit_params: dict, cooldown_clusters: int = 1,
           on_label: Callable | None = None,
           scenario_money: str = "5eur", scenario_cost: str = "observed"
           ) -> dict:
    """Run ONE participant over the bar series, causal + cooldown-gated.
    Returns per-cluster records and fill statistics."""
    per_cluster: dict[str, dict] = {}
    fills = {"FILLED": 0, "PARTIAL": 0, "NONFILL": 0}
    used_cluster_at: dict[str, int] = {}
    n_decisions = n_trades = 0
    interval = EC.BAR_MS
    time_exit = int(exit_params.get("time_exit", 20))
    for i in range(WARMUP, len(bars) - 1):
        dt = int(bars[i]["ts"]) + interval          # decision at bar i close
        cluster = EC.cluster_id(symbol, int(bars[i]["ts"]))
        # cooldown: one decision per cluster window
        if cluster in used_cluster_at and \
                (i - used_cluster_at[cluster]) < cooldown_clusters:
            continue
        feats = F.compute_features(bars[:i + 1], decision_time_ms=dt)
        event_id = f"{symbol}:{bars[i]['ts']}"
        n_decisions += 1
        decision = decide_fn(feats, event_id, dt, cluster)
        if decision.get("decision_action") != "TRADE":
            per_cluster.setdefault(cluster, {"net_eur": 0.0, "traded": False,
                                             "event_id": event_id})
            continue
        used_cluster_at[cluster] = i
        side = decision["side"]
        entry_bar = bars[i + 1]
        exit_bars = bars[i + 2: i + 2 + time_exit]
        res = S.simulate_trade(
            side=side, entry_bar=entry_bar, exit_bars=exit_bars,
            entry_ts_ms=int(entry_bar["ts"]),
            stop_frac=exit_params.get("stop_frac", 0.008),
            tp_frac=exit_params.get("tp_frac", 0.012),
            time_exit=time_exit, trailing_frac=exit_params.get("trailing_frac"),
            scenario_money=scenario_money, scenario_cost=scenario_cost)
        if res["status"] != "OK":
            per_cluster[cluster] = {"net_eur": 0.0, "traded": False,
                                    "event_id": event_id, "rejected": True}
            continue
        n_trades += 1
        fills["FILLED"] += 1
        label = 1 if res["net_pnl_eur"] > 0 else 0
        per_cluster[cluster] = {
            "net_eur": res["net_pnl_eur"], "gross_eur": res["gross_pnl_eur"],
            "traded": True, "side": side, "prob": decision["calibrated_probability"],
            "label": label, "event_id": event_id,
            "exit_reason": res["exit_reason"], "fee_eur": res["fee_eur"],
            "spread_eur": res["spread_eur"], "slippage_eur": res["slippage_eur"],
            "funding_eur": res["funding_eur"], "mfe": res["mfe_frac"],
            "mae": res["mae_frac"]}
        if on_label is not None:
            on_label(event_id, feats, label)          # matured label -> learn
    return {"per_cluster": per_cluster, "fills": fills,
            "n_decisions": n_decisions, "n_trades": n_trades}


def _metrics(per_cluster: dict, fills: dict) -> dict:
    traded = [c for c in per_cluster.values() if c.get("traded")]
    nets = [c["net_eur"] for c in traded]
    gross = [c.get("gross_eur", 0.0) for c in traded]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x < 0]
    n = len(nets)
    clusters = len(traded)
    # drawdown / expected shortfall on the net-eur equity curve
    cur = peak = dd = 0.0
    for x in nets:
        cur += x
        peak = max(peak, cur)
        dd = min(dd, cur - peak)
    tail = sorted(nets)[:max(1, n // 20)] if n else []
    es = sum(tail) / len(tail) if tail else 0.0
    pf = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else \
        (999.0 if wins else 0.0)
    # calibration / Brier
    labelled = [(c["prob"], c["label"]) for c in traded if "prob" in c]
    brier = (sum((p - y) ** 2 for p, y in labelled) / len(labelled)
             if labelled else None)
    # top-event removal robustness
    net_total = float(sum(nets))
    without_top = float(sum(sorted(nets, reverse=True)[3:])) if n > 3 else net_total
    return {
        "trades": n, "clusters": clusters, "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / n, 4) if n else 0.0,
        "avg_win_eur": round(sum(wins) / len(wins), 6) if wins else 0.0,
        "avg_loss_eur": round(sum(losses) / len(losses), 6) if losses else 0.0,
        "gross_pnl_eur": round(sum(gross), 6),
        "net_pnl_eur": round(net_total, 6),
        "ev_per_trade_eur": round(net_total / n, 6) if n else 0.0,
        "profit_factor": round(pf, 4),
        "max_drawdown_eur": round(dd, 6),
        "expected_shortfall_eur": round(es, 6),
        "n_raw": n, "n_eff": clusters,
        "brier": round(brier, 6) if brier is not None else None,
        "net_without_top3_eur": round(without_top, 6),
        "fill_rate": round(fills["FILLED"] / max(1, sum(fills.values())), 4),
        "fills": fills}


def paired_ev(a: dict, b: dict) -> dict:
    """Paired B-vs-A EV by event_cluster_id: at each cluster both nets (0 when
    a participant abstained). Reports mean(B-A), a sign-test win fraction and a
    bootstrap-free lower bound."""
    clusters = set(a["per_cluster"]) | set(b["per_cluster"])
    diffs = []
    for c in clusters:
        na = a["per_cluster"].get(c, {}).get("net_eur", 0.0)
        nb = b["per_cluster"].get(c, {}).get("net_eur", 0.0)
        if a["per_cluster"].get(c, {}).get("traded") or \
                b["per_cluster"].get(c, {}).get("traded"):
            diffs.append(nb - na)
    n = len(diffs)
    if n == 0:
        return {"n_paired": 0, "mean_diff_eur": 0.0, "b_better_frac": 0.5,
                "lower_bound_eur": 0.0}
    mean = sum(diffs) / n
    sd = math.sqrt(sum((d - mean) ** 2 for d in diffs) / n) if n > 1 else 0.0
    lb = mean - 1.65 * sd / math.sqrt(n)
    better = sum(1 for d in diffs if d > 0) / n
    return {"n_paired": n, "mean_diff_eur": round(mean, 6),
            "b_better_frac": round(better, 4),
            "lower_bound_eur": round(lb, 6)}


def run_tournament(bars: list[dict], *, symbol: str, venue: str,
                   timeframe: str, data_generation_id: str | None,
                   participants: dict[str, dict], log=lambda *a: None) -> dict:
    """participants: {name: {"policy": pol, "learner": PrequentialLearner|None,
    "random": bool}}. Returns per-participant metrics + paired A/B/C/D."""
    results: dict[str, dict] = {}
    common = dict(symbol=symbol, venue=venue, timeframe=timeframe,
                  data_generation_id=data_generation_id)
    for name, spec in participants.items():
        pol = spec.get("policy")
        learner = spec.get("learner")
        rnd = spec.get("random")
        exit_params = {k: pol.get(k) for k in
                       ("stop_frac", "tp_frac", "time_exit", "trailing_frac")} \
            if pol else {"stop_frac": 0.008, "tp_frac": 0.012, "time_exit": 20}

        if name == "D_no_trade":
            def decide_fn(feats, eid, dt, cluster):
                return {"decision_action": "ABSTAIN_LOW_REWARD", "side": "FLAT",
                        "calibrated_probability": 0.5}
        elif rnd:
            rng = random.Random(4242)

            def decide_fn(feats, eid, dt, cluster, _rng=rng):
                if _rng.random() < 0.05:
                    return {"decision_action": "TRADE",
                            "side": "LONG" if _rng.random() < 0.5 else "SHORT",
                            "calibrated_probability": 0.5}
                return {"decision_action": "ABSTAIN_LOW_REWARD", "side": "FLAT",
                        "calibrated_probability": 0.5}
        else:
            def decide_fn(feats, eid, dt, cluster, _pol=pol, _lr=learner):
                use_pol = _pol
                if _lr is not None:
                    _lr.predict(feats.get("features") or {}, eid)  # log first
                    # PREQUENTIAL: decide with the model as trained on labels
                    # matured SO FAR (all earlier, causal). Champion is never
                    # touched — only this challenger's live weights are used.
                    if _lr.model.n > 0:
                        use_pol = {**_pol, "weights": list(_lr.model.w)}
                return POL.decide(use_pol, feats, event_id=eid,
                                  decision_time_ms=dt, **common)

        on_label = None
        if learner is not None:
            def on_label(eid, feats, label, _lr=learner):
                _lr.observe_label(eid, label)

        run = _drive(bars, decide_fn=decide_fn, exit_params=exit_params,
                     cooldown_clusters=(pol or {}).get("cooldown_clusters", 1),
                     on_label=on_label,
                     scenario_money=(pol or {}).get("scenario_money", "5eur"),
                     scenario_cost=(pol or {}).get("scenario_cost", "observed"),
                     **common)
        results[name] = {"metrics": _metrics(run["per_cluster"], run["fills"]),
                         "run": run}
        log(f"  {name}: trades={run['n_trades']} "
            f"net={results[name]['metrics']['net_pnl_eur']}eur")
    paired = {}
    if "A_static_abstain" in results and "B_learn_abstain" in results:
        paired["B_vs_A"] = paired_ev(results["A_static_abstain"]["run"],
                                     results["B_learn_abstain"]["run"])
    return {"results": results, "paired": paired}
