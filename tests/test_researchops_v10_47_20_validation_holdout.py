"""V10.47.20 adversarial contract: validation admission and holdout isolation.

All fixtures are synthetic.  No real holdout is opened by this suite.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
from pathlib import Path

import pytest


def _trade(net: float, *, neff: float = 40.0) -> dict:
    return {
        "net_eur": net,
        "gross_eur": net + 0.01,
        "fee_eur": 0.002,
        "spread_eur": 0.001,
        "slippage_eur": 0.001,
        "funding_eur": 0.0,
        "cluster": "X:C1",
        "session": "X:S1",
        "day": "X:D1",
        "opportunity_bar": 1,
        "entry_bar": 2,
        "exit_index": 3,
        "entry_ts": 60_000,
        "bars_held": 1,
        "side": "LONG",
        "_test_neff": neff,
    }


def _install_stage_driver(monkeypatch, *, validation_net: float | None,
                          validation_neff: float = 40.0,
                          walk_forward_net: float = 99.0):
    from app.labs.v10_46 import causal_tournament as CT

    calls: list[tuple[str, str]] = []

    def drive(bars, sigs, decide_fn, exit_params, **kwargs):
        stage = bars[0]["stage"]
        calls.append((stage, kwargs.get("scenario_cost", "observed")))
        if stage == "train":
            trades = [_trade(1.0)]
        elif stage == "validation":
            trades = [] if validation_net is None else [
                _trade(validation_net, neff=validation_neff)
            ]
        elif stage == "walk_forward":
            trades = [_trade(walk_forward_net)]
        else:
            raise AssertionError(stage)
        return {"trades": trades, "counters": {}}

    def metrics(trades, counters, timeframe):
        net = sum(t["net_eur"] for t in trades)
        neff = min((t.get("_test_neff", 40.0) for t in trades), default=0.0)
        return {
            "trades": len(trades),
            "gross_pnl_eur": net,
            "net_pnl_eur": net,
            "gross_ev_eur": net / len(trades) if trades else 0.0,
            "net_ev_eur": net / len(trades) if trades else 0.0,
            "fee_eur": 0.0,
            "spread_eur": 0.0,
            "slippage_eur": 0.0,
            "funding_eur": 0.0,
            "net_without_top3_eur": net,
            "n_eff_final": neff,
            "n_eff": {"n_eff_final": neff},
            "counters": counters,
            "classification": "NET_EDGE_POSITIVE" if net > 0 else "NO_GROSS_EDGE",
        }

    monkeypatch.setattr(CT.CL, "drive_causal", drive)
    monkeypatch.setattr(CT, "_metrics", metrics)
    monkeypatch.setattr(CT.CS, "matched_random_paired", lambda *a, **k: {
        "match_status": "OK",
        "beats_matched_random": True,
        "paired_lower_bound_eur": 0.1,
        "coverage": 1.0,
    })
    return CT, calls


def _evaluate(CT):
    stage = lambda name: [{"stage": name}]
    exit_params = {"stop_frac": 0.01, "tp_frac": 0.02, "time_exit": 2}
    decider = lambda *a, **k: {}
    result = CT.evaluate_candidate(
        stage("train"), [None], stage("validation"), [None],
        stage("walk_forward"), [None], decider, exit_params,
        symbol="X", timeframe="1m", m_unique=10,
    )
    return result, exit_params, decider


def test_validation_failure_short_circuits_walk_forward(monkeypatch):
    CT, calls = _install_stage_driver(monkeypatch, validation_net=-1.0)
    result, _, _ = _evaluate(CT)
    assert not any(stage == "walk_forward" for stage, _ in calls)
    assert result["validation_gate"] is False
    assert result["walk_forward_called"] is False
    assert result["walk_forward_metrics"] is None
    assert result["status"] == "REJECTED_AT_VALIDATION"
    assert result["next_stage"] == "NONE"


def test_validation_without_trades_is_rejected_before_walk_forward(monkeypatch):
    CT, calls = _install_stage_driver(monkeypatch, validation_net=None)
    result, _, _ = _evaluate(CT)
    assert result["status"] == "REJECTED_AT_VALIDATION"
    assert result["validation_rejection_reason"] == "NO_VALIDATION_TRADES"
    assert not any(stage == "walk_forward" for stage, _ in calls)


def test_validation_low_neff_is_rejected_before_walk_forward(monkeypatch):
    CT, calls = _install_stage_driver(
        monkeypatch, validation_net=1.0, validation_neff=1.0
    )
    result, _, _ = _evaluate(CT)
    assert result["status"] == "REJECTED_AT_VALIDATION"
    assert result["validation_rejection_reason"] == "VALIDATION_N_EFF_INSUFFICIENT"
    assert not any(stage == "walk_forward" for stage, _ in calls)


def test_positive_validation_calls_walk_forward_exactly_once(monkeypatch):
    CT, calls = _install_stage_driver(monkeypatch, validation_net=1.0)
    result, _, _ = _evaluate(CT)
    assert sum(stage == "walk_forward" for stage, _ in calls) == 1
    assert result["validation_gate"] is True
    assert result["walk_forward_called"] is True
    assert result["walk_forward_metrics"] is not None


def test_candidate_parameters_are_not_refit_after_validation(monkeypatch):
    CT, _ = _install_stage_driver(monkeypatch, validation_net=1.0)
    result, exit_params, decider = _evaluate(CT)
    assert exit_params == {"stop_frac": 0.01, "tp_frac": 0.02, "time_exit": 2}
    assert result["policy_identity"]["decider_object_id"] == id(decider)
    assert result["policy_identity"]["parameters_unchanged"] is True


def test_tournament_accepts_discovery_partitions_not_full_series():
    from app.labs.v10_46 import causal_tournament as CT

    params = inspect.signature(CT.run_causal_tournament).parameters
    assert "discovery_partitions" in params
    assert "bars" not in params
    src = inspect.getsource(CT.run_causal_tournament)
    assert "bars[hstart:]" not in src
    assert "SealedHoldout" not in src


def _make_isolated_tree(tmp_path: Path):
    discovery = tmp_path / "data_root" / "discovery"
    for name, value in (("train", 1), ("validation", 2), ("walk_forward", 3)):
        part = discovery / name
        part.mkdir(parents=True)
        (part / "bars.json").write_text(
            json.dumps([{"ts": value, "open": 1, "high": 1, "low": 1,
                         "close": 1, "volume": 1}]), encoding="utf-8"
        )
    sealed = tmp_path / "data_root" / "sealed_holdout"
    data = sealed / "encrypted_or_sealed_data"
    data.mkdir(parents=True)
    payload = b'[{"ts":4,"open":1,"high":1,"low":1,"close":1,"volume":1}]'
    (data / "bars.json.sealed").write_bytes(payload)
    secret = b"synthetic-external-authority-key"
    commitment = {
        "schema": "v10_47_20_holdout_commitment",
        "state": "SEALED",
        "data_file": "encrypted_or_sealed_data/bars.json.sealed",
        "commitment_sha256": hashlib.sha256(payload).hexdigest(),
        "authority_key_sha256": hashlib.sha256(secret).hexdigest(),
        "n_bars": 1,
    }
    (sealed / "commitment.json").write_text(
        json.dumps(commitment), encoding="utf-8"
    )
    return discovery, sealed, secret


def test_discovery_loader_only_knows_discovery_root(tmp_path):
    from app.labs.v10_46.discovery_dataset import DiscoveryDatasetLoader

    discovery, sealed, _ = _make_isolated_tree(tmp_path)
    loader = DiscoveryDatasetLoader(discovery)
    partitions = loader.load()
    assert [b[0]["ts"] for b in (
        partitions.train, partitions.validation, partitions.walk_forward
    )] == [1, 2, 3]
    assert not hasattr(loader, "holdout_root")
    assert str(sealed.resolve()) not in repr(loader)


def test_holdout_loader_is_separate_and_has_no_bar_attribute(tmp_path):
    from app.labs.v10_46 import holdout_loader as HL

    _, sealed, secret = _make_isolated_tree(tmp_path)
    authority = HL.ExternalHoldoutAuthority(sealed, secret=secret)
    assert not hasattr(authority, "_bars")
    assert "_bars" not in inspect.getsource(HL.ExternalHoldoutAuthority)


def test_arbitrary_string_cannot_authorize_holdout(tmp_path):
    from app.labs.v10_46 import holdout_loader as HL

    _, sealed, secret = _make_isolated_tree(tmp_path)
    authority = HL.ExternalHoldoutAuthority(sealed, secret=secret)
    with pytest.raises(HL.HoldoutAccessDenied):
        authority.load_once("ANY-NONEMPTY-STRING")


def test_neutral_wrapper_does_not_bypass_capability(tmp_path):
    from app.labs.v10_46 import holdout_loader as HL

    _, sealed, secret = _make_isolated_tree(tmp_path)
    authority = HL.ExternalHoldoutAuthority(sealed, secret=secret)

    def neutral_wrapper(value):
        return authority.load_once(value)

    with pytest.raises(HL.HoldoutAccessDenied):
        neutral_wrapper("forged")


@pytest.mark.parametrize("bad_path", [
    "child/../escaped.json",
    "../escaped.json",
])
def test_holdout_relative_traversal_is_rejected(tmp_path, bad_path):
    from app.labs.v10_46 import holdout_loader as HL

    _, sealed, secret = _make_isolated_tree(tmp_path)
    authority = HL.ExternalHoldoutAuthority(sealed, secret=secret)
    capability = authority.issue_capability(reason="synthetic", audit_ref="TEST")
    with pytest.raises(HL.HoldoutAccessDenied):
        authority.load_once(capability, relative_path=bad_path)


def test_holdout_absolute_external_path_is_rejected(tmp_path):
    from app.labs.v10_46 import holdout_loader as HL

    _, sealed, secret = _make_isolated_tree(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text("[]", encoding="utf-8")
    authority = HL.ExternalHoldoutAuthority(sealed, secret=secret)
    capability = authority.issue_capability(reason="synthetic", audit_ref="TEST")
    with pytest.raises(HL.HoldoutAccessDenied):
        authority.load_once(capability, relative_path=str(outside.resolve()))


def test_holdout_symlink_escape_is_rejected(tmp_path):
    from app.labs.v10_46 import holdout_loader as HL

    _, sealed, secret = _make_isolated_tree(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text("[]", encoding="utf-8")
    link = sealed / "encrypted_or_sealed_data" / "link.json"
    try:
        os.symlink(outside, link)
    except OSError as exc:
        pytest.skip(f"symlink unavailable on this platform: {exc}")
    authority = HL.ExternalHoldoutAuthority(sealed, secret=secret)
    capability = authority.issue_capability(reason="synthetic", audit_ref="TEST")
    with pytest.raises(HL.HoldoutAccessDenied):
        authority.load_once(
            capability, relative_path="encrypted_or_sealed_data/link.json"
        )


def test_external_capability_is_single_use_and_audited(tmp_path):
    from app.labs.v10_46 import holdout_loader as HL

    _, sealed, secret = _make_isolated_tree(tmp_path)
    authority = HL.ExternalHoldoutAuthority(sealed, secret=secret)
    capability = authority.issue_capability(reason="synthetic", audit_ref="TEST")
    bars = authority.load_once(capability)
    assert bars[0]["ts"] == 4
    with pytest.raises(HL.HoldoutAccessDenied):
        authority.load_once(capability)
    records = authority.access_log()
    assert [r["seq"] for r in records] == list(range(len(records)))
    assert records[-1]["kind"] == "denied_already_consumed"


def test_dataset_isolation_audit_detects_no_shared_paths(tmp_path):
    from app.labs.v10_46.discovery_dataset import audit_dataset_isolation

    discovery, sealed, _ = _make_isolated_tree(tmp_path)
    report = audit_dataset_isolation(discovery, sealed)
    assert report["ok"] is True
    assert report["shared_paths"] == []
    assert report["shared_file_ids"] == []
    assert report["holdout_state"] == "SEALED"
