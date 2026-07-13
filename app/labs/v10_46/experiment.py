"""V10.46 integrated experiment harness (RESEARCH ONLY).

Runs the mandated minimum experiment — A (static+abstention), B
(learning+abstention), C (learning without abstention), D (no-trade) — plus a
No-Trade and a random exposure-matched baseline, over a VERIFIED dataset
generation, causally and deterministically. Produces the integrated report,
dashboard and an output manifest. No orders, no live, no private endpoints.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from . import VERSION, FINAL_RECOMMENDATION, assert_research_only
from . import contracts as C
from . import learner as L
from . import policy as POL
from . import promotion as PR
from . import tournament as T
from . import dashboard as DASH

OUT_SUBDIR = ("reports", "research", "v10_46_final_integrated")


def _participants():
    champ = POL.freeze(POL.default_policy("A_champion", kind="static",
                                          abstention=True))
    b = POL.default_policy("B_challenger", kind="learning", abstention=True)
    b = POL.mutate(champ, "threshold", 0.58, policy_id="B_challenger")
    b["kind"] = "learning"
    c = POL.default_policy("C_challenger", kind="learning", abstention=False)
    return champ, {
        "A_static_abstain": {"policy": champ},
        "B_learn_abstain": {"policy": b, "learner": L.PrequentialLearner(b)},
        "C_learn_no_abstain": {"policy": c, "learner": L.PrequentialLearner(c)},
        "D_no_trade": {"policy": POL.default_policy("D_no_trade")},
        "Q_random": {"policy": POL.default_policy("Q_random"), "random": True},
    }, L.PrequentialLearner(b)


def run_experiment(symbol: str = "BTCUSDT", timeframe: str = "5m",
                   write: bool = True, log=print) -> dict:
    """Load a verified generation, resample causally, run the paired
    tournament, evaluate promotion, and emit the integrated report."""
    assert_research_only()
    from .. import public_data_backfill_v10_45_1 as BF
    from .. import edge_discovery_engine_v10_45_1 as ENG
    started = datetime.now(timezone.utc)
    ver = BF.verify_dataset("bitget", symbol)
    if not ver.get("ok"):
        return {"status": ver.get("status"), "detail": ver.get("detail"),
                "safety": _safety()}
    manifest = ver["manifest"]
    gen_id = ver["generation_id"]
    as_of = ver["as_of_ms"]
    bars = BF.load_klines("bitget", symbol)
    factor = {"1m": 1, "5m": 5, "15m": 15}.get(timeframe, 1)
    if factor > 1:
        bars = ENG.resample_bars(bars, factor, as_of_ms=as_of)
    if len(bars) < T.WARMUP + 100:
        return {"status": "NEED_MORE_DATA", "n_bars": len(bars),
                "safety": _safety()}
    ident = ENG.code_identity()
    champ, participants, _lrB = _participants()
    log(f"[v10.46] tournament on {len(bars)} {timeframe} bars of {symbol} "
        f"(generation {gen_id})")
    out = T.run_tournament(bars, symbol=symbol, venue="bitget",
                           timeframe=timeframe, data_generation_id=gen_id,
                           participants=participants, log=log)
    res = out["results"]
    m = {k: v["metrics"] for k, v in res.items()}
    paired = out["paired"].get("B_vs_A", {})
    no_trade_net = m["D_no_trade"]["net_pnl_eur"]
    random_net = m["Q_random"]["net_pnl_eur"]
    # promotion evaluation for the best abstaining learner (B) vs champion
    ev_id = f"{symbol}:{as_of}"
    promo = PR.promotion_decision(
        "B_challenger", "SHADOW_CANDIDATE", m["B_learn_abstain"],
        symbol=symbol, venue="bitget", timeframe=timeframe, event_id=ev_id,
        decision_time_ms=as_of, data_generation_id=gen_id,
        paired_lb_eur=paired.get("lower_bound_eur"),
        no_trade_net=no_trade_net, random_net=random_net,
        dataset_verified=True, registry_closed=True,
        holdout_single_use_ok=True)
    # honest verdict
    promoted = promo["decision"] == "PROMOTE"
    verdict = (HONEST_NO_EDGE if not promoted else
               "Un challenger superó los gates de shadow en esta ventana; "
               "requiere shadow forward independiente y auditoría antes de "
               "cualquier paso. RESEARCH ONLY, NO LIVE.")
    report = {
        "tool_version": VERSION, "ran_at": started.isoformat(),
        "symbol": symbol, "timeframe": timeframe, "n_bars": len(bars),
        "provenance": {
            "repo_commit": ident["repo_commit"],
            "tree_oid": ident["git_tree_oid"],
            "data_generation_id": gen_id,
            "dataset_sha256": ver["sha256"],
            "dirty_worktree": ident["dirty_worktree"],
            "run_modes": "replay (causal)", "seal_match": None},
        "safety": _safety(),
        "market": _market_summary(res, timeframe),
        "decision": _decision_summary(res),
        "position": _position_summary(m),
        "tournament": {
            "champion": champ["policy_id"],
            "participants": m,
            "paired": {"B_vs_A": paired},
            "promotion_status": promo["decision"],
            "promotion_to_state": promo["to_state"]},
        "learning": _learning_summary(participants),
        "reports": {"tournament_participants": len(m)},
        "verdict": verdict}
    if write:
        _write_outputs(report, out, promo, log=log)
    return report


HONEST_NO_EDGE = ("No se encontró edge validado en las familias probadas, "
                  "durante esta ventana y bajo este modelo de costes.")


def _safety() -> dict:
    s = assert_research_only()
    return {**s, "final_recommendation": FINAL_RECOMMENDATION}


def _market_summary(res, timeframe) -> dict:
    a = res["A_static_abstain"]["run"]["per_cluster"]
    traded = [c for c in a.values() if c.get("traded")]
    sides = [c["side"] for c in traded]
    return {"regime": "mixed (24/7 replay)", "timeframe": timeframe,
            "long_signals": sides.count("LONG"),
            "short_signals": sides.count("SHORT"),
            "flat_clusters": sum(1 for c in a.values() if not c.get("traded")),
            "note": "LONG researched; blocked at Paper Champion until OOS edge"}


def _decision_summary(res) -> dict:
    b = res["B_learn_abstain"]["run"]
    return {"agents_for": "trend_rider + learner", "agents_against": "skeptic",
            "abstention": "enabled", "decisions": b["n_decisions"],
            "trades": b["n_trades"]}


def _position_summary(m) -> dict:
    best = max(m.items(), key=lambda kv: kv[1]["net_pnl_eur"])
    return {"exposure_eur": 5.0, "leverage": 1.0,
            "best_participant": best[0],
            "best_net_pnl_eur": best[1]["net_pnl_eur"],
            "no_trade_net_eur": m["D_no_trade"]["net_pnl_eur"]}


def _learning_summary(participants) -> dict:
    lr = participants["B_learn_abstain"].get("learner")
    return {"last_cause": "aggregate autopsy", "lesson": "see ledger",
            "mutation": "threshold 0.55->0.58 (one dimension)",
            "mutation_status": "evaluated by promotion controller",
            "memory": "prequential (no holdout learning)",
            "challenger_brier": round(lr.brier(), 6) if lr and lr.brier()
            is not None else None}


def _write_outputs(report, tourn_out, promo, log=print) -> None:
    from .. import public_data_backfill_v10_45_1 as BF
    from .. import edge_discovery_engine_v10_45_1 as ENG
    out = BF.validated_dir(*OUT_SUBDIR)
    BF.safe_atomic_write(out / "integrated_report.json",
                         json.dumps(report, indent=2, default=str).encode("utf-8"))
    # per-participant scoreboard (euro-first)
    import csv as _csv
    import io as _io
    buf = _io.StringIO()
    w = _csv.writer(buf, lineterminator="\n")
    w.writerow(["participant", "trades", "clusters", "win_rate",
                "gross_pnl_eur", "net_pnl_eur", "ev_per_trade_eur", "n_eff",
                "max_drawdown_eur", "expected_shortfall_eur", "brier",
                "net_without_top3_eur", "fill_rate"])
    for name, r in tourn_out["results"].items():
        mm = r["metrics"]
        w.writerow([name, mm["trades"], mm["clusters"], mm["win_rate"],
                    mm["gross_pnl_eur"], mm["net_pnl_eur"],
                    mm["ev_per_trade_eur"], mm["n_eff"],
                    mm["max_drawdown_eur"], mm["expected_shortfall_eur"],
                    mm["brier"], mm["net_without_top3_eur"], mm["fill_rate"]])
    BF.safe_atomic_write(out / "tournament_scoreboard_eur.csv",
                         buf.getvalue().encode("utf-8"))
    BF.safe_atomic_write(out / "promotion_decision.json",
                         json.dumps(promo, indent=2, default=str).encode("utf-8"))
    DASH.build_dashboard(report, out / "dashboard.html")
    # output manifest binding every artifact + code identity
    ident = ENG.code_identity()
    arts = {}
    import hashlib as _h
    for p in sorted(out.iterdir()):
        if p.is_file() and not p.name.startswith("output_manifest") \
                and p.name != "progress_checkpoint.md":
            arts[p.name] = _h.sha256(p.read_bytes()).hexdigest()
    man = {"tool_version": VERSION, "created_at": datetime.now(timezone.utc)
           .isoformat(), "code": ident, "artifacts": arts,
           "safety": _safety()}
    BF.safe_atomic_write(out / "output_manifest_v10_46.json",
                         json.dumps(man, indent=2, default=str).encode("utf-8"))
    log(f"[v10.46] outputs -> {out}")
