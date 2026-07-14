"""V10.47.13 — .ai_coordination status + coherence validator (no models, no APIs).

Prints the hub state (branch/HEAD/tree, the single NEXT_ACTION, open ideas,
pending reviews, experiments, requests, disagreements, blockers, evidence) and
DETECTS incoherences: more than one NEXT_ACTION, broken file links, experiments
without evidence, proposals without a review, decisions without an ID, and empty
required sections. Exit code is non-zero if any incoherence is found."""
from __future__ import annotations

import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HUB = os.path.join(ROOT, ".ai_coordination")

_PATH_RE = re.compile(
    r"(?:reports|proposals|reviews|experiments|scripts|app|tests|manifests"
    r"|\.ai_coordination)/[\w./\-]+\.(?:md|json|py|html|log)")


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def _git(*args: str) -> str:
    try:
        return subprocess.check_output(["git", *args], cwd=ROOT).decode().strip()
    except Exception:
        return "?"


def analyze(hub: str = HUB, root: str = ROOT) -> dict:
    issues: list[str] = []
    state: dict = {}

    # NEXT_ACTION — exactly one
    na = _read(os.path.join(hub, "NEXT_ACTION.md"))
    next_actions = re.findall(r"^- \[[ xX]\] NEXT:", na, flags=re.M)
    state["next_action_count"] = len(next_actions)
    state["next_action"] = next((l.strip() for l in na.splitlines()
                                 if "NEXT:" in l), None)
    if len(next_actions) != 1:
        issues.append(f"NEXT_ACTION must be exactly 1, found {len(next_actions)}")

    # proposals must each have a review
    prop_dir = os.path.join(hub, "proposals")
    rev_dir = os.path.join(hub, "reviews")
    props = sorted(f for f in os.listdir(prop_dir)) if os.path.isdir(prop_dir) else []
    for p in props:
        slug = p.replace("PROP-", "").replace(".md", "")
        if not os.path.exists(os.path.join(rev_dir, f"REV-{slug}.md")):
            issues.append(f"proposal {p} has no matching review REV-{slug}.md")
    state["proposals"] = props

    # experiments must cite existing evidence (or be NEEDS_DATA with smoke evidence)
    exp_dir = os.path.join(hub, "experiments")
    exps = sorted(f for f in os.listdir(exp_dir)) if os.path.isdir(exp_dir) else []
    for e in exps:
        body = _read(os.path.join(exp_dir, e))
        m = re.search(r"evidence:\s*(\S+)", body)
        if not m:
            issues.append(f"experiment {e} has no evidence: line")
            continue
        ev = m.group(1)
        if not os.path.exists(os.path.join(root, ev)):
            issues.append(f"experiment {e} evidence missing on disk: {ev}")
    state["experiments"] = exps

    # decisions must all have an ID (### Dxxx)
    dec = _read(os.path.join(hub, "DECISIONS.md"))
    headers = re.findall(r"^###\s+(.*)$", dec, flags=re.M)
    state["decisions"] = len(headers)
    for h in headers:
        if not re.match(r"^D\d+\b", h.strip()):
            issues.append(f"decision without ID: '{h.strip()}'")

    # broken links across all hub markdown
    broken: list[str] = []
    for dirpath, _dirs, files in os.walk(hub):
        for f in files:
            if not f.endswith(".md"):
                continue
            for ref in _PATH_RE.findall(_read(os.path.join(dirpath, f))):
                # hub-relative refs (proposals/ reviews/ experiments/) resolve in hub
                base = hub if ref.split("/")[0] in ("proposals", "reviews",
                                                    "experiments") else root
                if not os.path.exists(os.path.join(base, ref)):
                    broken.append(f"{f} -> {ref}")
    # de-dup
    broken = sorted(set(broken))
    if broken:
        issues.extend(f"broken link: {b}" for b in broken)
    state["broken_links"] = broken

    # required non-empty files
    for req in ("CURRENT_STATE.md", "DECISIONS.md", "BLOCKERS.md",
                "EXPERIMENT_REGISTRY.md"):
        if len(_read(os.path.join(hub, req)).strip()) < 10:
            issues.append(f"required file empty/missing: {req}")

    state["blockers"] = [l for l in _read(os.path.join(hub, "BLOCKERS.md")).splitlines()
                         if l.strip().startswith("- BLK")]
    state["open_requests"] = [l for l in _read(os.path.join(hub, "REQUESTS.md")).splitlines()
                              if "(open)" in l]
    state["disagreements"] = re.findall(r"^##\s+DIS-\S+", _read(
        os.path.join(hub, "DISAGREEMENTS.md")), flags=re.M)
    state["git"] = {"branch": _git("branch", "--show-current"),
                    "head": _git("rev-parse", "HEAD"),
                    "tree": _git("rev-parse", "HEAD^{tree}")}
    return {"state": state, "issues": issues, "coherent": not issues}


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    r = analyze()
    s, g = r["state"], r["state"]["git"]
    print("=== .ai_coordination status ===")
    print(f"branch={g['branch']} HEAD={g['head'][:10]} tree={g['tree'][:10]}")
    print(f"NEXT_ACTION ({s['next_action_count']}): {s['next_action']}")
    print(f"proposals={len(s['proposals'])} experiments={len(s['experiments'])} "
          f"decisions={s['decisions']} disagreements={len(s['disagreements'])}")
    print(f"open_requests={len(s['open_requests'])} blockers={len(s['blockers'])} "
          f"broken_links={len(s['broken_links'])}")
    if r["coherent"]:
        print("COHERENT: no incoherences detected")
        return 0
    print("INCOHERENCES:")
    for i in r["issues"]:
        print("  -", i)
    return 1


if __name__ == "__main__":
    sys.exit(main())
