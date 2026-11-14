"""Plan builders and coordinator-local job handlers for the three required real workflows:
  1. no-root batch hashing of owner-provided files across phones, merged into one manifest/report;
  2. APK testing selected by live device capability (install/launch alone is never a PASS);
  3. dependent hand-off: one phone's output artifact becomes another phone's input via the coordinator.
Coordinator-local ops run inside the coordinator process (no device involved)."""
import hashlib, io, json, os, tarfile
from .protocol import new_id

MAX_STAGE_FILES = 5000
MAX_STAGE_BYTES = 1 << 30


def _put_artifact(coord, job_id, name, data: bytes, attempt=0):
    s = coord.s
    aid = "art-" + new_id()[:16]
    with open(os.path.join(s.dir, "artifacts", aid), "wb") as f:
        f.write(data); f.flush(); os.fsync(f.fileno())
    with s.tx() as db:
        db.execute("INSERT INTO artifacts(id,job_id,attempt,device_id,name,size,sha256,rel_path,created_ms,owner_job_final) VALUES(?,?,?,?,?,?,?,?,?,1)",
                   (aid, job_id, attempt, None, name, len(data), hashlib.sha256(data).hexdigest(), aid, coord.now()))
    return aid


def _read_artifact(coord, art):
    return open(os.path.join(coord.s.dir, "artifacts", art["rel_path"]), "rb").read()


def _inputs(coord, job):
    out = []
    for i in json.loads(job["inputs_json"]):
        a = coord.s.one("SELECT * FROM artifacts WHERE job_id=? AND name=? AND owner_job_final=1", (i["job"], i["name"]))
        if not a:
            raise RuntimeError(f"input artifact {i['name']} from {i['job']} is missing")
        out.append(a)
    return out


def partition(files, n):
    """Greedy longest-processing-time partition by size. files: [(path,size)]. Deterministic."""
    shards = [[] for _ in range(max(1, min(n, len(files))))]
    loads = [0] * len(shards)
    for p, sz in sorted(files, key=lambda x: (-x[1], x[0])):
        i = loads.index(min(loads))
        shards[i].append((p, sz)); loads[i] += sz
    return [s for s in shards if s]


def stage_files(coord, job):
    a = json.loads(job["args_json"])
    root = os.path.realpath(a["root"])
    n = int(a.get("shards", 1))
    files = []
    for rel in a["files"]:
        full = os.path.realpath(os.path.join(root, rel))
        if not full.startswith(root + os.sep) or not os.path.isfile(full):      # no traversal, no symlink escape, regular files only
            raise RuntimeError(f"refusing path outside the owner-provided root or not a regular file: {rel}")
        files.append((os.path.relpath(full, root), os.path.getsize(full)))
    if not files or len(files) > MAX_STAGE_FILES or sum(s for _, s in files) > MAX_STAGE_BYTES:
        raise RuntimeError("file set empty or over the staging bounds")
    index = {}
    for i, shard in enumerate(partition(files, n)):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as t:
            for rel, _ in shard:
                ti = t.gettarinfo(os.path.join(root, rel), arcname=rel); ti.uid = ti.gid = 0; ti.uname = ti.gname = ""; ti.mtime = 0
                with open(os.path.join(root, rel), "rb") as fh:
                    t.addfile(ti, fh)
        _put_artifact(coord, job["id"], f"shard-{i}.tar", buf.getvalue())
        index[f"shard-{i}.tar"] = [{"path": r, "size": s} for r, s in shard]
    _put_artifact(coord, job["id"], "index.json", json.dumps(index, sort_keys=True).encode())
    return {"shards": len(index), "files": len(files), "bytes": sum(s for _, s in files)}


def merge_manifests(coord, job):
    """Combine per-phone manifests; verify every staged file is covered exactly once with the expected size."""
    args = json.loads(job["args_json"])
    arts = {(a["job_id"], a["name"]): a for a in _inputs(coord, job)}      # same name from several phones must not collide
    idx_key = next(k for k in arts if k[1] == "index.json")
    index = json.loads(_read_artifact(coord, arts.pop(idx_key)))
    expected = {e["path"]: e["size"] for shard in index.values() for e in shard}
    combined, problems, by_device = {}, [], {}
    for name, a in sorted(arts.items()):
        m = json.loads(_read_artifact(coord, a))
        by_device[a["device_id"] or "unknown"] = by_device.get(a["device_id"] or "unknown", 0) + len(m.get("files", []))
        for e in m.get("files", []):
            if e["path"] in combined:
                problems.append(f"duplicate: {e['path']}")
            elif e["path"] not in expected:
                problems.append(f"unexpected: {e['path']}")
            elif e.get("size") != expected[e["path"]]:
                problems.append(f"size mismatch: {e['path']}")
            else:
                combined[e["path"]] = {"sha256": e["sha256"], "size": e["size"], "device": a["device_id"]}
    missing = sorted(set(expected) - set(combined))
    problems += [f"missing: {p}" for p in missing]
    report = {"algorithm": "sha256", "files": dict(sorted(combined.items())), "per_device_file_counts": by_device,
              "verified_complete": not problems, "problems": problems}
    data = json.dumps(report, indent=2, sort_keys=True).encode()
    _put_artifact(coord, job["id"], "combined-manifest.json", data)
    if problems:
        raise RuntimeError("combined manifest incomplete: " + "; ".join(problems[:5]))
    return {"files": len(combined), "devices": by_device, "combined_sha256": hashlib.sha256(data).hexdigest()}


def apk_verdict(coord, job):
    """Install or launch alone is never a PASS. A PASS needs at least one behavioral check (accessibility
    walkthrough or instrumentation) that ran and reported success, with its evidence artifact preserved."""
    steps = {}
    for dep in json.loads(job["deps_json"]):
        r = coord.s.one("SELECT state,result_json FROM jobs WHERE id=?", (dep,))
        steps[dep.split(".", 1)[1]] = (r["state"], json.loads(r["result_json"] or "{}"))
    behavioural = [k for k, (st, res) in steps.items() if k.startswith(("a11y", "instr")) and st == "succeeded" and res.get("passed") is True]
    failed = [k for k, (st, res) in steps.items() if st != "succeeded" or res.get("passed") is False]
    if failed:
        verdict = "FAIL"
    elif behavioural:
        verdict = "PASS"
    else:
        verdict = "INCONCLUSIVE"
    out = {"verdict": verdict, "steps": {k: v[0] for k, v in steps.items()}, "behavioural_checks_passed": behavioural,
           "note": "installed/launched only; no behavioural check ran" if verdict == "INCONCLUSIVE" else ""}
    _put_artifact(coord, job["id"], "apk-test-report.json", json.dumps(out, indent=2).encode())
    if verdict != "PASS":
        raise RuntimeError(f"APK test verdict {verdict}: " + (out["note"] or f"failed steps: {failed}"))
    return out


def register(coord):
    coord.local_handlers["coordinator.stage_files"] = stage_files
    coord.local_handlers["coordinator.merge_manifests"] = merge_manifests
    coord.local_handlers["coordinator.apk_verdict"] = apk_verdict


# ------------------------------------------------------------------ plan builders
def batch_hash_plan(root, files, shards=4, device_pins=None):
    jobs = [{"key": "stage", "op": "coordinator.stage_files", "args": {"root": root, "files": files, "shards": shards}}]
    n = min(shards, len(files))
    for i in range(n):
        jobs.append({"key": f"hash{i}", "op": "hash_files", "args": {"shard": f"shard-{i}.tar", "algorithm": "sha256"},
                     "deps": ["stage"], "inputs": [{"job": "stage", "name": f"shard-{i}.tar"}], "idempotent": True, "max_attempts": 3,
                     **({"device": device_pins[i % len(device_pins)]} if device_pins else {})})
    jobs.append({"key": "merge", "op": "coordinator.merge_manifests", "args": {}, "deps": ["stage"] + [f"hash{i}" for i in range(n)],
                 "inputs": [{"job": "stage", "name": "index.json"}] + [{"job": f"hash{i}", "name": "manifest.json"} for i in range(n)]})
    return {"title": "batch-hash", "jobs": jobs}


def apk_test_plan(root, apk_rel, package, caps):
    """Steps chosen from the LIVE capabilities of the target phone (caps = its capability report)."""
    ops = caps.get("operations", {})
    have = lambda o: ops.get(o, {}).get("available") and ops.get(o, {}).get("granted", True)
    if not have("apk.install"):
        raise ValueError("target phone cannot install APKs (no available installer adapter); refusing to plan an APK test")
    jobs = [{"key": "stage", "op": "coordinator.stage_files", "args": {"root": root, "files": [apk_rel], "shards": 1}},
            {"key": "install", "op": "apk.install", "args": {"package": package, "artifact": "shard-0.tar"}, "deps": ["stage"],
             "inputs": [{"job": "stage", "name": "shard-0.tar"}], "ui_exclusive": True}]
    last = ["install"]
    checks = []
    if have("apk.launch"):
        jobs.append({"key": "launch", "op": "apk.launch", "args": {"package": package}, "deps": ["install"], "ui_exclusive": True}); last = ["launch"]
    if have("apk.a11y_walkthrough"):
        jobs.append({"key": "a11y", "op": "apk.a11y_walkthrough", "args": {"package": package}, "deps": last, "ui_exclusive": True}); checks.append("a11y")
    if have("apk.instrumentation"):
        jobs.append({"key": "instr", "op": "apk.instrumentation", "args": {"package": package}, "deps": last}); checks.append("instr")
    if have("apk.diagnostics"):
        jobs.append({"key": "diag", "op": "apk.diagnostics", "args": {"package": package}, "deps": last}); checks.append("diag")
    allkeys = [j["key"] for j in jobs if j["key"] != "stage"]
    jobs.append({"key": "verdict", "op": "coordinator.apk_verdict", "args": {}, "deps": allkeys, "inputs": []})
    for j in jobs:
        if j["key"] not in ("stage",) and not j["op"].startswith("coordinator."):
            j["device"] = caps.get("_device_id")      # all steps on the same phone; installation state is device-specific
    # verdict reads job results through deps; the inputs list is intentionally empty for result-only reads
    return {"title": f"apk-test {package}", "jobs": jobs}


def handoff_plan(op_a, args_a, op_b, args_b, artifact="output", device_a=None, device_b=None):
    a = {"key": "produce", "op": op_a, "args": args_a, "idempotent": False, **({"device": device_a} if device_a else {})}
    b = {"key": "consume", "op": op_b, "args": args_b, "deps": ["produce"], "inputs": [{"job": "produce", "name": artifact}],
         **({"device": device_b} if device_b else {})}
    return {"title": "handoff", "jobs": [a, b]}
