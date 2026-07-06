"""V10.38 Policy registry: auditable, versioned, live-flags always blocked."""

from __future__ import annotations

import json

import pytest

from app.labs import continuous_edge_factory_v10_38 as CE


def _policy(pid="BTCUSDT_burst_v1", status="RESEARCH_ONLY", version=1):
    return {"policy_id": pid, "version": version, "status": status,
            "source_data": "bybit_forward_v10_32", "features": ["burst_score"],
            "labels": ["triple_barrier"], "allowed_symbols": ["BTCUSDT"],
            "allowed_sides": ["long"], "allowed_regimes": ["all"]}


def test_register_writes_blocked_audited_doc(tmp_path):
    reg = CE.PolicyRegistry(base=tmp_path)
    path = reg.register(_policy())
    doc = json.loads((tmp_path / "BTCUSDT_burst_v1.json").read_text(encoding="utf-8"))
    assert path.endswith("BTCUSDT_burst_v1.json")
    assert doc["blocked_live_flags"] == {"actual_live_ready": False,
                                         "can_send_real_orders": False,
                                         "human_promotion_required": True}
    assert doc["can_send_real_orders"] is False
    assert doc["final_recommendation"] == "NO LIVE"
    assert len(doc["audit_trail"]) == 1


def test_reregister_appends_audit_trail(tmp_path):
    reg = CE.PolicyRegistry(base=tmp_path)
    reg.register(_policy(version=1))
    reg.register(_policy(status="SHADOW_ONLY", version=2))
    reg.register(_policy(status="PAUSED", version=3))
    doc = json.loads((tmp_path / "BTCUSDT_burst_v1.json").read_text(encoding="utf-8"))
    assert [e["version"] for e in doc["audit_trail"]] == [1, 2, 3]
    assert [e["status"] for e in doc["audit_trail"]][-1] == "PAUSED"


@pytest.mark.parametrize("bad", sorted(CE.FORBIDDEN_STATES))
def test_forbidden_policy_status_rejected(tmp_path, bad):
    reg = CE.PolicyRegistry(base=tmp_path)
    with pytest.raises(ValueError):
        reg.register(_policy(status=bad))


def test_unknown_policy_status_rejected(tmp_path):
    reg = CE.PolicyRegistry(base=tmp_path)
    with pytest.raises(ValueError):
        reg.register(_policy(status="LIVE_ISH"))


def test_registry_states_exclude_live():
    assert not (CE.REGISTRY_STATES & CE.FORBIDDEN_STATES)
