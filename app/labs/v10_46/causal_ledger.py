"""V10.47.8 causal, immutable, single-position ledger (RESEARCH ONLY, NO LIVE).

Replaces the scientifically-invalid accounting used by the V10.47 tournament,
where `per_cluster[cluster] = latest_result` let a LATER signal in the same
temporal cluster overwrite — and therefore ex-post SELECT — an earlier one. On
the real DOGE/XRP 1m P08_LONG deciders that overwrite kept the last (often
winning) trade of each cluster and dropped the earlier losers, manufacturing a
positive net that does not survive causal accounting.

The repaired rules (resolution: FIRST_CAUSAL_SIGNAL_SINGLE_POSITION):
  * only the FIRST causal eligible signal in a cluster may open a position;
  * a policy/symbol may hold at most ONE simulated position at a time;
  * a later signal while a position is open  -> POSITION_ALREADY_OPEN (skipped);
  * a later signal in an already-entered cluster (cooldown) -> CLUSTER_COOLDOWN;
  * every ledger record is appended once and NEVER mutated (append-only);
  * there is NO ex-post selection: the trade that is accounted is exactly the
    trade the causal rule opened, decided only on information available at entry.

`drive_causal` returns the immutable ledger, the executed trades (append-only,
one per opened position), the eligible opportunities (for exposure-matched
baselines), and the full skip/execution counters.
"""

from __future__ import annotations

from typing import Any, Callable

from . import event_clock as EC
from . import sim_oms as S

WARMUP = 60


class ImmutableLedger:
    """Append-only ledger. Records can be appended and read but never mutated;
    each returned record is a shallow copy so callers cannot rewrite history."""

    __slots__ = ("_records", "_seq")

    def __init__(self) -> None:
        self._records: list[dict] = []
        self._seq = 0

    def append(self, kind: str, **fields: Any) -> int:
        rec = {"seq": self._seq, "kind": kind, **fields}
        self._records.append(rec)
        self._seq += 1
        return rec["seq"]

    def __len__(self) -> int:
        return len(self._records)

    def records(self) -> list[dict]:
        return [dict(r) for r in self._records]           # defensive copies

    def by_kind(self, kind: str) -> list[dict]:
        return [dict(r) for r in self._records if r["kind"] == kind]


def _cluster(symbol: str, ts: int, timeframe: str) -> str:
    return EC.cluster_id_tf(symbol, ts, timeframe)


def drive_causal(bars: list[dict], sigs: list, decide_fn: Callable,
                 exit_params: dict, *, symbol: str, timeframe: str,
                 scenario_money: str = "5eur", scenario_cost: str = "observed",
                 cooldown_clusters: int = 1, warmup: int = WARMUP) -> dict:
    """Drive a decider causally with single-position, first-causal-signal,
    append-only accounting. See module docstring for the rules."""
    interval_ms = EC.interval_ms_for(timeframe)
    time_exit = int(exit_params.get("time_exit", 20))
    stop_frac = float(exit_params.get("stop_frac", 0.008))
    tp_frac = float(exit_params.get("tp_frac", 0.012))
    trailing_frac = exit_params.get("trailing_frac")

    ledger = ImmutableLedger()
    trades: list[dict] = []
    opportunities: list[dict] = []           # eligible entries (for baselines)
    entered_clusters: dict[str, int] = {}     # cluster -> bar index of entry
    busy_until_index = -1                     # last bar occupied by open position

    n_raw = n_eligible = n_exec = 0
    n_skip_pos = n_skip_cd = 0
    clusters_seen: set[str] = set()
    sessions_seen: set[str] = set()
    days_seen: set[str] = set()

    for i in range(warmup, len(bars) - 1):
        ts_i = int(bars[i]["ts"])
        cluster = _cluster(symbol, ts_i, timeframe)
        clusters_seen.add(cluster)
        sessions_seen.add(EC.session_id(symbol, ts_i))
        days_seen.add(EC.day_id(symbol, ts_i))
        s = sigs[i]
        event_id = f"{symbol}:{ts_i}"
        dt = ts_i + interval_ms
        d = decide_fn({"_sig": s, "ts": ts_i}, event_id, dt, cluster)
        action = d.get("decision_action")
        ledger.append("raw_signal", bar=i, ts=ts_i, cluster=cluster,
                      action=action, side=d.get("side"))
        if action != "TRADE":
            continue
        n_raw += 1
        # ---- causal eligibility gates (order matters, all causal) ----
        if i <= busy_until_index:
            n_skip_pos += 1
            ledger.append("skip", bar=i, ts=ts_i, cluster=cluster,
                          reason="POSITION_ALREADY_OPEN",
                          busy_until_index=busy_until_index)
            continue
        cd_hit = False
        for c_prev, i_prev in entered_clusters.items():
            # block re-entry in the same cluster, and the next
            # (cooldown_clusters-1) contiguous clusters after an entry
            if c_prev == cluster:
                cd_hit = True
                break
        if not cd_hit and cooldown_clusters > 1 and entered_clusters:
            last_entry_bar = max(entered_clusters.values())
            block_ms = EC.cluster_block_ms(timeframe)
            spanned = (ts_i - int(bars[last_entry_bar]["ts"])) // block_ms
            if 0 <= spanned < cooldown_clusters:
                cd_hit = True
        if cd_hit:
            n_skip_cd += 1
            ledger.append("skip", bar=i, ts=ts_i, cluster=cluster,
                          reason="CLUSTER_COOLDOWN")
            continue
        # ---- eligible: this is the FIRST causal signal we may act on ----
        n_eligible += 1
        opportunities.append({"bar": i, "ts": ts_i, "cluster": cluster,
                              "side": d["side"], "prob": d.get(
                                  "calibrated_probability", 0.5)})
        ledger.append("decision", bar=i, ts=ts_i, cluster=cluster,
                      side=d["side"], prob=d.get("calibrated_probability", 0.5),
                      immutable=True)
        entry_bar = bars[i + 1]
        exit_bars = bars[i + 2: i + 2 + time_exit]
        res = S.simulate_trade(
            side=d["side"], entry_bar=entry_bar, exit_bars=exit_bars,
            entry_ts_ms=int(entry_bar["ts"]), stop_frac=stop_frac,
            tp_frac=tp_frac, time_exit=time_exit, scenario_money=scenario_money,
            scenario_cost=scenario_cost, trailing_frac=trailing_frac,
            interval_ms=interval_ms)
        if res["status"] != "OK":
            ledger.append("skip", bar=i, ts=ts_i, cluster=cluster,
                          reason=f"SIM_{res['status']}")
            # a rejected entry does NOT occupy the book and does NOT consume the
            # cluster (nothing was opened) — but we DID evaluate it as eligible
            continue
        entry_index = i + 1
        bars_held = int(res["bars_held"])
        exit_index = entry_index + max(1, bars_held)
        busy_until_index = exit_index
        entered_clusters[cluster] = i
        n_exec += 1
        ledger.append("order", bar=entry_index, ts=int(entry_bar["ts"]),
                      cluster=cluster, side=d["side"])
        ledger.append("entry", bar=entry_index, ts=int(entry_bar["ts"]),
                      cluster=cluster, side=d["side"],
                      entry_price=res["entry_price"])
        ledger.append("position", entry_bar=entry_index, exit_index=exit_index,
                      cluster=cluster, side=d["side"], bars_held=bars_held)
        ledger.append("exit", bar=exit_index, ts=int(res["exit_ts_ms"]),
                      cluster=cluster, reason=res["exit_reason"],
                      exit_price=res["exit_price"])
        trade = {
            "opportunity_bar": i, "entry_bar": entry_index,
            "exit_index": exit_index, "entry_ts": int(entry_bar["ts"]),
            "exit_ts": int(res["exit_ts_ms"]), "cluster": cluster,
            "session": EC.session_id(symbol, ts_i),
            "day": EC.day_id(symbol, ts_i), "side": d["side"],
            "net_eur": res["net_pnl_eur"], "gross_eur": res["gross_pnl_eur"],
            "fee_eur": res["fee_eur"], "spread_eur": res["spread_eur"],
            "slippage_eur": res["slippage_eur"], "funding_eur": res["funding_eur"],
            "bars_held": bars_held, "exit_reason": res["exit_reason"],
            "prob": d.get("calibrated_probability", 0.5),
            "label": 1 if res["net_pnl_eur"] > 0 else 0}
        trades.append(trade)
        ledger.append("trade", bar=entry_index, cluster=cluster,
                      net_eur=res["net_pnl_eur"], gross_eur=res["gross_pnl_eur"])

    counters = {
        "n_signals_raw": n_raw, "n_signals_eligible": n_eligible,
        "n_executed": n_exec, "n_skipped_position_open": n_skip_pos,
        "n_skipped_cluster_cooldown": n_skip_cd,
        "n_clusters": len(clusters_seen), "n_sessions": len(sessions_seen),
        "n_days": len(days_seen), "n_positions": n_exec, "n_trades": len(trades)}
    return {"symbol": symbol, "timeframe": timeframe, "trades": trades,
            "opportunities": opportunities, "counters": counters,
            "ledger": ledger, "interval_ms": interval_ms}
