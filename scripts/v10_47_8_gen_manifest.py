"""V10.47.8 — output manifest + seal. Ties git identity + registry hashes +
m_global + SHA-256 of every output (json/md/html/log) into one manifest with a
non-None seal. Research only, NO LIVE."""
import sys, os, json, hashlib, subprocess, datetime
sys.path.insert(0, r"C:\Users\Adrian\Documents\New project\bitget-ai-trading-bot")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = r"C:\Users\Adrian\Documents\New project\bitget-ai-trading-bot"
OUT = os.path.join(ROOT, "reports", "research", "v10_47_8_scientific_repair")
MAN = os.path.join(OUT, "manifests")
os.makedirs(MAN, exist_ok=True)


def git(*a):
    try:
        return subprocess.check_output(["git", *a], cwd=ROOT).decode().strip()
    except Exception:
        return "?"


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# git identity + dirty on TRACKED files only (untracked historical files ignored)
tracked_dirty = bool(git("status", "--porcelain", "--untracked-files=no").strip())
head, tree = git("rev-parse", "HEAD"), git("rev-parse", "HEAD^{tree}")
branch = git("branch", "--show-current")

# registry hashes + m_global from the 12 combos
combos = {}
tdir = os.path.join(OUT, "tournament")
for f in sorted(os.listdir(tdir)):
    if f.endswith(".json"):
        o = json.load(open(os.path.join(tdir, f), encoding="utf-8"))
        combos[f[:-5]] = {"registry_hash": o["registry"]["registry_hash"],
                          "m_nominal": o["registry"]["m_nominal"],
                          "m_unique": o["registry"]["m_unique_hypotheses"],
                          "shadow_candidates": o["shadow_candidates"],
                          "holdout_touched": o["holdout_touched"]}
m_global = {"combos": len(combos),
            "participant_runs_nominal": sum(c["m_nominal"] for c in combos.values()),
            "m_unique_per_combo": sorted({c["m_unique"] for c in combos.values()}),
            "total_shadow_candidates": sum(len(c["shadow_candidates"])
                                           for c in combos.values())}

# SHA-256 of every output file (exclude the manifest dir itself)
files = {}
for dirpath, _dirs, fnames in os.walk(OUT):
    if os.path.abspath(dirpath).startswith(os.path.abspath(MAN)):
        continue
    for fn in fnames:
        if fn.lower().endswith((".json", ".md", ".html", ".log")):
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, OUT).replace("\\", "/")
            files[rel] = sha256(full)

# seal over the sorted path:sha lines
seal_src = "\n".join(f"{k}:{v}" for k, v in sorted(files.items()))
seal = hashlib.sha256(seal_src.encode()).hexdigest()

manifest = {
    "schema": "v10_47_8_output_manifest",
    "generated_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "git": {"branch": branch, "head": head, "tree": tree,
            "dirty_tracked": tracked_dirty, "origin_main": git("rev-parse", "origin/main")},
    "safety": {"paper_trading": True, "live_trading": False, "dry_run": True,
               "can_send_real_orders": False, "final_recommendation": "NO LIVE"},
    "verdict": "NO_CONFIRMED_EDGE", "shadow_candidates": m_global["total_shadow_candidates"],
    "m_global": m_global, "combos": combos,
    "n_output_files": len(files), "files_sha256": files,
    "seal": seal, "output_manifest_sha": seal}

json.dump(manifest, open(os.path.join(MAN, "output_manifest.json"), "w",
                         encoding="utf-8"), indent=2, sort_keys=True)
with open(os.path.join(MAN, "SEAL.txt"), "w", encoding="utf-8") as fh:
    fh.write(f"seal={seal}\nhead={head}\ntree={tree}\nfiles={len(files)}\n"
             f"shadow_candidates={m_global['total_shadow_candidates']}\n")
print(f"manifest: {len(files)} files, seal={seal[:16]}… "
      f"shadow={m_global['total_shadow_candidates']} dirty_tracked={tracked_dirty}")
assert seal and manifest["output_manifest_sha"], "seal must be non-None"
print("output_manifest_sha =", manifest["output_manifest_sha"][:32], "…  (non-None)")
