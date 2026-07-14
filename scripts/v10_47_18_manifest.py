"""V10.47.18 — build/verify the provenance-bound output manifest + seal.

Usage:
  python scripts/v10_47_18_manifest.py build
  python scripts/v10_47_18_manifest.py verify     # independent verification
Research only, NO LIVE."""
import sys, os, json, hashlib
sys.path.insert(0, r"C:\Users\Adrian\Documents\New project\bitget-ai-trading-bot")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from app.labs import public_data_backfill_v10_45_1 as BF
from app.labs.v10_46 import manifest_seal as MZ
from app.labs.v10_46 import causal_tournament as CT

ROOT = r"C:\Users\Adrian\Documents\New project\bitget-ai-trading-bot"
OUT = os.path.join(ROOT, "reports", "research", "v10_47_15_final_certification_repair")
TDIR = os.path.join(OUT, "tournament")
MANP = os.path.join(OUT, "manifests", "output_manifest.json")


def _dataset_hashes():
    out = {}
    for sym in ("BTCUSDT", "ETHUSDT", "XRPUSDT", "DOGEUSDT"):
        for v in ("bitget", "bybit"):
            r = BF.verify_dataset(v, sym)
            if r.get("ok"):
                bars = BF.load_klines(v, sym)
                h = hashlib.sha256(json.dumps(
                    [[int(b["ts"]), b["open"], b["high"], b["low"], b["close"]]
                     for b in bars], separators=(",", ":")).encode()).hexdigest()
                out[f"{sym}:{v}"] = f"{r['generation_id']}:{h}"
                break
    return out


def _spec_registry_holdout():
    specs, regs, holds = {}, {}, {}
    for fn in sorted(os.listdir(TDIR)):
        if not fn.endswith(".json"):
            continue
        o = json.load(open(os.path.join(TDIR, fn), encoding="utf-8"))
        key = f"{o['symbol']}:{o['timeframe']}"
        regs[key] = o["registry"]["registry_hash"]
        holds[key] = o["holdout"]["commitment_sha256"]
        specs[key] = o["registry"]["registry_hash"]     # closed registry = spec set
    reg_root = MZ._root_hash(regs)
    hold_root = MZ._root_hash(holds)
    return specs, reg_root, hold_root


def build():
    specs, reg_root, hold_root = _spec_registry_holdout()
    split_spec_hash = MZ._sha_str(json.dumps(CT.split_indices(129600), sort_keys=True))
    m = MZ.build_manifest(root=ROOT, out_dir=OUT, dataset_hashes=_dataset_hashes(),
                          spec_hashes=specs, registry_hash=reg_root,
                          split_spec_hash=split_spec_hash,
                          holdout_commitment_hash=hold_root,
                          extra_provenance={"sprint": "v10_47_15_final_certification_repair",
                                            "verdict": "NO_CONFIRMED_EDGE",
                                            "shadow_candidates": 0})
    os.makedirs(os.path.dirname(MANP), exist_ok=True)
    json.dump(m, open(MANP, "w", encoding="utf-8"), indent=2, sort_keys=True)
    v = MZ.verify_manifest(m, root=ROOT)
    print(f"built manifest: files={len(m['files_sha256'])} "
          f"payload={m['manifest_payload_sha256'][:16]}… seal={m['seal_sha256'][:16]}… "
          f"dirty={m['git']['dirty_tracked']}")
    print(f"self-verify: ok={v['ok']} payload_ok={v['payload_ok']} seal_ok={v['seal_ok']} "
          f"problems={v['problems'][:3]}")
    assert m["seal_sha256"] and m["manifest_payload_sha256"], "seal non-None"
    return v["ok"]


def verify():
    m = json.load(open(MANP, encoding="utf-8"))
    v = MZ.verify_manifest(m, root=ROOT)
    print(f"VERIFY ok={v['ok']} payload_ok={v['payload_ok']} seal_ok={v['seal_ok']}")
    if not v["ok"]:
        print("problems:", v["problems"][:20])
    return v["ok"]


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "build"
    ok = build() if cmd == "build" else verify()
    sys.exit(0 if ok else 1)
