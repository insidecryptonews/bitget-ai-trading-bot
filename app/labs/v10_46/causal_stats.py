"""V10.47.8 conservative statistics for the causal ledger (RESEARCH ONLY).

Two things the V10.47 tournament got wrong and this module fixes:

  * n_eff was set equal to the trade count, ignoring that trades in the same
    event/cluster/session or with autocorrelated returns are NOT independent.
    `n_eff_estimate` reports every component and takes the conservative MINIMUM.

  * the random baseline was a free-running policy with a totally different trade
    count/exposure, so "beats random on total PnL" was meaningless.
    `matched_random_null` builds an EXPOSURE-MATCHED random baseline: same number
    of entries, same LONG/SHORT split, same clusters, same holding rule, same
    costs — only the precise intra-cluster timing and direction are randomised —
    and returns the null distribution so the gate compares like with like.

Also a moving-block bootstrap lower bound that respects serial dependence.
"""

from __future__ import annotations

import math
import random
from typing import Any

from . import event_clock as EC
from . import sim_oms as S


# --------------------------------------------------------------------------- #
# n_eff                                                                        #
# --------------------------------------------------------------------------- #
def _acf_neff(xs: list[float]) -> float:
    """Effective sample size from autocorrelation:
    n_eff = n / (1 + 2 * sum_{k>=1} rho_k), truncating at the first non-positive
    rho_k (initial-positive-sequence style). Conservative and bounded to [1, n]."""
    n = len(xs)
    if n <= 1:
        return float(n)
    mean = sum(xs) / n
    var = sum((x - mean) ** 2 for x in xs) / n
    if var <= 0:
        return float(n)                     # constant series: no serial info
    acc = 0.0
    for k in range(1, n // 2):
        cov = sum((xs[t] - mean) * (xs[t - k] - mean) for t in range(k, n)) / n
        rho = cov / var
        if rho <= 0:
            break
        acc += rho
    neff = n / (1.0 + 2.0 * acc)
    return max(1.0, min(float(n), neff))


def _non_overlapping_count(intervals: list[tuple[int, int]]) -> int:
    """Greedy count of mutually non-overlapping holding intervals."""
    if not intervals:
        return 0
    intervals = sorted(intervals, key=lambda p: p[1])
    count, last_end = 0, -math.inf
    for a, b in intervals:
        if a > last_end:
            count += 1
            last_end = b
    return count


def n_eff_estimate(trades: list[dict], *, timeframe: str) -> dict:
    """Report n_raw plus every dependence-aware estimate and the conservative
    final n_eff = min of the applicable estimators."""
    n = len(trades)
    if n == 0:
        return {"n_raw": 0, "n_executed": 0, "n_overlap": 0, "n_event": 0,
                "n_cluster": 0, "n_session": 0, "n_day": 0, "n_temporal": 0,
                "n_acf": 0.0, "n_eff_final": 0.0, "cluster_source": "cluster_id_tf",
                "fallback_used": False}
    clusters = {t["cluster"] for t in trades}
    sessions = {t.get("session") for t in trades}
    days = {t.get("day") for t in trades}
    events = {t["opportunity_bar"] for t in trades}
    intervals = [(t["entry_bar"], t["exit_index"]) for t in trades]
    n_overlap = _non_overlapping_count(intervals)
    n_acf = _acf_neff([float(t["net_eur"]) for t in trades])
    block_ms = EC.cluster_block_ms(timeframe)
    span = max(t["entry_ts"] for t in trades) - min(t["entry_ts"] for t in trades)
    n_temporal = max(1, min(n, int(span // block_ms) + 1))
    applicable = [len(events), n_overlap, len(clusters), len(sessions),
                  n_temporal, n_acf]
    n_eff_final = max(1.0, float(min(applicable)))
    return {"n_raw": n, "n_executed": n, "n_overlap": n_overlap,
            "n_event": len(events), "n_cluster": len(clusters),
            "n_session": len(sessions), "n_day": len(days),
            "n_temporal": n_temporal, "n_acf": round(n_acf, 4),
            "n_eff_final": round(n_eff_final, 4),
            "cluster_source": "cluster_id_tf", "fallback_used": False}


# --------------------------------------------------------------------------- #
# block bootstrap                                                              #
# --------------------------------------------------------------------------- #
def block_bootstrap_mean_lb(xs: list[float], *, block: int = 5, reps: int = 2000,
                            alpha: float = 0.05, seed: int = 12345) -> dict:
    """Moving-block bootstrap lower bound of the MEAN of a (possibly serially
    dependent) series. Returns per-item mean lower bound and the implied total."""
    n = len(xs)
    if n == 0:
        return {"mean": 0.0, "mean_lb": 0.0, "total_lb": 0.0, "reps": 0, "n": 0}
    block = max(1, min(block, n))
    rng = random.Random(seed)
    n_blocks = math.ceil(n / block)
    means = []
    for _ in range(reps):
        sample: list[float] = []
        for _b in range(n_blocks):
            start = rng.randint(0, n - block)
            sample.extend(xs[start:start + block])
        sample = sample[:n]
        means.append(sum(sample) / len(sample))
    means.sort()
    lb = means[max(0, int(alpha * reps) - 1)]
    mean = sum(xs) / n
    return {"mean": round(mean, 8), "mean_lb": round(lb, 8),
            "total_lb": round(lb * n, 8), "reps": reps, "n": n}


# --------------------------------------------------------------------------- #
# exposure-matched random baseline                                            #
# --------------------------------------------------------------------------- #
def matched_random_null(bars: list[dict], trades: list[dict], *, symbol: str,
                        timeframe: str, exit_params: dict,
                        scenario_money: str = "5eur",
                        scenario_cost: str = "observed", reps: int = 200,
                        seed: int = 20240714) -> dict:
    """Exposure-matched random baseline null distribution.

    For each replicate: for every executed trade, draw a RANDOM entry bar inside
    the SAME cluster the strategy entered, and assign a side by permuting the
    strategy's own LONG/SHORT multiset. Same count, same clusters, same sessions,
    same holding rule, same costs, same exposure — only the precise timing and
    direction are randomised. Returns the null net-total distribution + the
    empirical p-value of the strategy's net total (fraction of random >= strat)."""
    interval_ms = EC.interval_ms_for(timeframe)
    time_exit = int(exit_params.get("time_exit", 20))
    stop_frac = float(exit_params.get("stop_frac", 0.008))
    tp_frac = float(exit_params.get("tp_frac", 0.012))
    trailing_frac = exit_params.get("trailing_frac")
    n = len(trades)
    if n == 0:
        return {"reps": 0, "strategy_net": 0.0, "null_mean": 0.0,
                "null_p95": 0.0, "p_value": 1.0, "beats_matched_random": False}
    # candidate bars per cluster (exclude last two so entry+exit fit)
    cluster_bars: dict[str, list[int]] = {}
    for i in range(len(bars) - 2):
        c = EC.cluster_id_tf(symbol, int(bars[i]["ts"]), timeframe)
        cluster_bars.setdefault(c, []).append(i)
    sides = [t["side"] for t in trades]
    strat_net = float(sum(t["net_eur"] for t in trades))

    def _sim_at(idx: int, side: str) -> float:
        entry_bar = bars[idx + 1]
        exit_bars = bars[idx + 2: idx + 2 + time_exit]
        res = S.simulate_trade(
            side=side, entry_bar=entry_bar, exit_bars=exit_bars,
            entry_ts_ms=int(entry_bar["ts"]), stop_frac=stop_frac,
            tp_frac=tp_frac, time_exit=time_exit, scenario_money=scenario_money,
            scenario_cost=scenario_cost, trailing_frac=trailing_frac,
            interval_ms=interval_ms)
        return res["net_pnl_eur"] if res["status"] == "OK" else 0.0

    rng = random.Random(seed)
    null_totals: list[float] = []
    for _ in range(reps):
        perm = sides[:]
        rng.shuffle(perm)
        tot = 0.0
        for t, side in zip(trades, perm):
            cand = cluster_bars.get(t["cluster"]) or [t["opportunity_bar"]]
            idx = cand[rng.randrange(len(cand))]
            tot += _sim_at(idx, side)
        null_totals.append(tot)
    null_totals.sort()
    null_mean = sum(null_totals) / len(null_totals)
    p95 = null_totals[min(len(null_totals) - 1, int(0.95 * len(null_totals)))]
    ge = sum(1 for x in null_totals if x >= strat_net)
    p_value = (ge + 1) / (len(null_totals) + 1)
    return {"reps": reps, "strategy_net": round(strat_net, 6),
            "null_mean": round(null_mean, 6), "null_p95": round(p95, 6),
            "null_min": round(null_totals[0], 6),
            "null_max": round(null_totals[-1], 6),
            "p_value": round(p_value, 5),
            "beats_matched_random": bool(p_value < 0.05)}


def matched_random_paired(bars: list[dict], trades: list[dict], *, symbol: str,
                          timeframe: str, exit_params: dict,
                          scenario_money: str = "5eur",
                          scenario_cost: str = "observed", reps: int = 200,
                          seed: int = 20240714) -> dict:
    """EXACTLY paired exposure-matched baseline (Work audit P1.2).

    For each candidate trade we build an explicit matched baseline trade with the
    SAME symbol/timeframe/side/cluster/session and the SAME exit rule (holding cap,
    stop/tp, costs), entered at a RANDOM bar inside the same cluster — replacing
    only the precise entry timing. The baseline is driven as a SINGLE-POSITION
    sequence (a spill-over holding blocks the next cluster, exactly like the
    candidate), so realised holding and end-of-dataset censoring emerge from the
    same mechanics. We form explicit candidate↔baseline pairs, compute
    paired_delta_i = candidate_net_i − baseline_net_i, and report coverage. If any
    pair cannot be matched, match_status = BASELINE_MATCH_INCOMPLETE (GATE_FAIL);
    we never substitute a total-PnL comparison."""
    interval_ms = EC.interval_ms_for(timeframe)
    time_exit = int(exit_params.get("time_exit", 20))
    stop_frac = float(exit_params.get("stop_frac", 0.008))
    tp_frac = float(exit_params.get("tp_frac", 0.012))
    trailing_frac = exit_params.get("trailing_frac")
    n = len(trades)
    if n == 0:
        return {"pairs_requested": 0, "pairs_found": 0, "pairs_impossible": 0,
                "coverage": 1.0, "paired_mean_eur": 0.0, "paired_median_eur": 0.0,
                "paired_lower_bound_eur": 0.0, "block": 1,
                "match_status": "OK", "unmatched_reason": None,
                "beats_matched_random": False}
    cluster_bars: dict[str, list[int]] = {}
    for i in range(len(bars) - 2):
        c = EC.cluster_id_tf(symbol, int(bars[i]["ts"]), timeframe)
        cluster_bars.setdefault(c, []).append(i)

    def _sim_at(idx: int, side: str):
        entry_bar = bars[idx + 1]
        res = S.simulate_trade(
            side=side, entry_bar=entry_bar, exit_bars=bars[idx + 2:idx + 2 + time_exit],
            entry_ts_ms=int(entry_bar["ts"]), stop_frac=stop_frac, tp_frac=tp_frac,
            time_exit=time_exit, scenario_money=scenario_money,
            scenario_cost=scenario_cost, trailing_frac=trailing_frac,
            interval_ms=interval_ms)
        if res["status"] != "OK":
            return None, 0
        return res["net_pnl_eur"], int(res["bars_held"])

    rng = random.Random(seed)
    # per-trade baseline net averaged over reps where a match is possible
    acc = [[] for _ in trades]
    for _ in range(reps):
        busy_until = -1
        for j, t in enumerate(trades):
            choices = [b for b in cluster_bars.get(t["cluster"], []) if b > busy_until]
            if not choices:
                continue                                   # impossible this rep
            idx = choices[rng.randrange(len(choices))]
            net, held = _sim_at(idx, t["side"])
            if net is None:
                continue
            busy_until = idx + 1 + max(1, held)
            acc[j].append(net)
    baseline_net = [(sum(a) / len(a)) if a else None for a in acc]
    matched = [(t["net_eur"], bn) for t, bn in zip(trades, baseline_net)
               if bn is not None]
    pairs_found = len(matched)
    pairs_impossible = n - pairs_found
    coverage = pairs_found / n
    deltas = [c - b for c, b in matched]
    block = _bootstrap_block(trades, timeframe)
    bb = block_bootstrap_mean_lb(deltas, block=block) if deltas else \
        {"mean": 0.0, "mean_lb": 0.0}
    dsorted = sorted(deltas)
    median = dsorted[len(dsorted) // 2] if dsorted else 0.0
    status = "OK" if coverage >= 1.0 - 1e-9 else "BASELINE_MATCH_INCOMPLETE"
    return {"pairs_requested": n, "pairs_found": pairs_found,
            "pairs_impossible": pairs_impossible, "coverage": round(coverage, 4),
            "paired_mean_eur": round(bb["mean"], 6),
            "paired_median_eur": round(median, 6),
            "paired_lower_bound_eur": round(bb["mean_lb"], 6),
            "block": block, "match_status": status,
            "unmatched_reason": (None if status == "OK"
                                 else "no non-overlapping bar in candidate cluster"),
            "beats_matched_random": bool(status == "OK" and bb["mean_lb"] > 0)}


def _bootstrap_block(trades: list[dict], timeframe: str) -> int:
    """Justified block size for the block bootstrap: the median realised holding
    (in bars) of the candidate trades, clamped to [1, n//2]. This ties the block
    to the actual serial dependence (holding overlap) rather than a fixed 5."""
    n = len(trades)
    if n <= 1:
        return 1
    holds = sorted(int(t.get("bars_held", 1)) for t in trades)
    med = holds[len(holds) // 2]
    return max(1, min(med, n // 2))


def paired_delta_vs_zero(trades: list[dict]) -> dict:
    """Paired per-opportunity delta vs No-Trade (which nets 0 on every
    opportunity), with a block-bootstrap lower bound of the mean."""
    xs = [float(t["net_eur"]) for t in trades]
    bb = block_bootstrap_mean_lb(xs)
    return {"n": len(xs), "mean_diff_eur": bb["mean"],
            "mean_lb_eur": bb["mean_lb"], "total_lb_eur": bb["total_lb"],
            "beats_no_trade": bool(sum(xs) > 0)}
