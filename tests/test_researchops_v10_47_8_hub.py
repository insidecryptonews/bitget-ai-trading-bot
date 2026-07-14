"""V10.47.8 — .ai_coordination hub coherence validator tests. No models/APIs."""

from __future__ import annotations

import importlib.util
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "ai_coordination_status",
    os.path.join(ROOT, "scripts", "ai_coordination_status.py"))
STATUS = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(STATUS)


def _mk_hub(base):
    for d in ("proposals", "reviews", "experiments"):
        os.makedirs(os.path.join(base, d), exist_ok=True)
    with open(os.path.join(base, "NEXT_ACTION.md"), "w", encoding="utf-8") as f:
        f.write("# NEXT ACTION\n- [ ] NEXT: do the one thing\n")
    with open(os.path.join(base, "DECISIONS.md"), "w", encoding="utf-8") as f:
        f.write("# DECISIONS\n### D001 - a decision\nbody\n")
    with open(os.path.join(base, "CURRENT_STATE.md"), "w", encoding="utf-8") as f:
        f.write("state content here")
    with open(os.path.join(base, "BLOCKERS.md"), "w", encoding="utf-8") as f:
        f.write("# BLOCKERS\n- BLK-001: something\n")
    with open(os.path.join(base, "EXPERIMENT_REGISTRY.md"), "w", encoding="utf-8") as f:
        f.write("# registry content long enough\n")
    with open(os.path.join(base, "DISAGREEMENTS.md"), "w", encoding="utf-8") as f:
        f.write("# DIS\n## DIS-001 x\n")
    with open(os.path.join(base, "REQUESTS.md"), "w", encoding="utf-8") as f:
        f.write("# REQ\n- REQ-001 (open): x\n")
    with open(os.path.join(base, "proposals", "PROP-X.md"), "w", encoding="utf-8") as f:
        f.write("# proposal x\nreview: reviews/REV-X.md\n")
    with open(os.path.join(base, "reviews", "REV-X.md"), "w", encoding="utf-8") as f:
        f.write("# review x\n")
    with open(os.path.join(base, "experiments", "EXP-X.md"), "w", encoding="utf-8") as f:
        f.write("# exp x\nevidence: NEXT_ACTION.md\n")   # exists rel to base


def test_clean_hub_is_coherent(tmp_path):
    hub = str(tmp_path / ".ai_coordination")
    os.makedirs(hub)
    _mk_hub(hub)
    r = STATUS.analyze(hub=hub, root=hub)
    assert r["coherent"], r["issues"]
    assert r["state"]["next_action_count"] == 1


def test_detects_multiple_next_actions(tmp_path):
    hub = str(tmp_path / ".ai_coordination")
    os.makedirs(hub)
    _mk_hub(hub)
    with open(os.path.join(hub, "NEXT_ACTION.md"), "w", encoding="utf-8") as f:
        f.write("- [ ] NEXT: one\n- [ ] NEXT: two\n")
    r = STATUS.analyze(hub=hub, root=hub)
    assert not r["coherent"]
    assert any("NEXT_ACTION must be exactly 1" in i for i in r["issues"])


def test_detects_proposal_without_review(tmp_path):
    hub = str(tmp_path / ".ai_coordination")
    os.makedirs(hub)
    _mk_hub(hub)
    os.remove(os.path.join(hub, "reviews", "REV-X.md"))
    r = STATUS.analyze(hub=hub, root=hub)
    assert any("no matching review" in i for i in r["issues"])


def test_detects_decision_without_id(tmp_path):
    hub = str(tmp_path / ".ai_coordination")
    os.makedirs(hub)
    _mk_hub(hub)
    with open(os.path.join(hub, "DECISIONS.md"), "w", encoding="utf-8") as f:
        f.write("# DECISIONS\n### a decision with no id\n")
    r = STATUS.analyze(hub=hub, root=hub)
    assert any("decision without ID" in i for i in r["issues"])


def test_detects_experiment_without_evidence(tmp_path):
    hub = str(tmp_path / ".ai_coordination")
    os.makedirs(hub)
    _mk_hub(hub)
    with open(os.path.join(hub, "experiments", "EXP-X.md"), "w", encoding="utf-8") as f:
        f.write("# exp x\nno evidence line here\n")
    r = STATUS.analyze(hub=hub, root=hub)
    assert any("no evidence" in i for i in r["issues"])


def test_real_hub_structure():
    r = STATUS.analyze()
    st = r["state"]
    assert st["next_action_count"] == 1
    assert len(st["proposals"]) == 2
    assert st["decisions"] >= 3
    # every real decision has an ID and every proposal has a review
    assert not any("decision without ID" in i for i in r["issues"])
    assert not any("no matching review" in i for i in r["issues"])
