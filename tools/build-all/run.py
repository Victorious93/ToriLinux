#!/usr/bin/env python3
"""Coordinated build/validation runner for the Victorious set.

  tools/build-all/run.py --workspace /home/user --out OUT [--cpus N] [--only ID[,ID] [--with-deps]] [--list]
Full run: python3 tools/build-all/run.py --workspace <dir holding the checkouts> --out <dir>
Retry only what did not pass (plus what it needs):  python3 tools/build-all/run.py ... --retry-failed --out <same dir>

Honest by construction: a job is PASSED only if its command exited 0; FAILED if it ran and did not; BLOCKED if a needed tool/resource is
missing (reported, never faked); NOT RUN if a dependency did not pass or it was not selected. Exit code is non-zero if ANY required job is
not PASSED. Each job runs in the repo's own checkout with its own log; toolchain env is set per job only. Source state is recorded
(commit, branch, dirty-file hash) before running; artifacts are only what the job's directories actually contain afterwards (hashed).
Does not sign, publish, merge, or touch default branches.
"""
import argparse, concurrent.futures as cf, hashlib, json, os, shutil, subprocess, sys, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
ARTIFACT_GLOBS = ("*.apk", "*.aab", "*.deb", "*.AppImage", "*.iso", "*.so", "*.aar")


def probe(need, ws):
    if need == "root": return os.geteuid() == 0
    if need == "android-sdk": return bool(os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")) and os.path.isdir(os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT") or "")
    if need == "container-daemon":
        for rt in ("docker", "podman"):
            if shutil.which(rt) and subprocess.run([rt, "info"], capture_output=True, timeout=20).returncode == 0: return True
        return False
    if need.startswith("env:"): return bool(os.environ.get(need[4:]))
    if need.startswith("pkg:"): return shutil.which("pkg-config") is not None and subprocess.run(["pkg-config", "--exists", need[4:]]).returncode == 0
    if need.startswith("py:"): return subprocess.run([sys.executable, "-I", "-c", f"import {need[3:]}"], capture_output=True).returncode == 0
    return shutil.which(need) is not None


def git(repo, *a):
    r = subprocess.run(["git", "-C", repo, *a], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def snapshot(path):
    st = git(path, "status", "--porcelain=v1") or ""
    h = hashlib.sha256()
    for line in sorted(st.splitlines()):
        h.update(line.encode())
        f = os.path.join(path, line[3:].strip())
        if os.path.isfile(f):
            with open(f, "rb") as fh: h.update(hashlib.sha256(fh.read()).digest())
    subm = (git(path, "submodule", "status") or "").splitlines()
    return {"commit": git(path, "rev-parse", "HEAD"), "branch": git(path, "rev-parse", "--abbrev-ref", "HEAD"),
            "uncommitted_files": len(st.splitlines()), "uncommitted_state_sha256": h.hexdigest() if st else None, "submodules": subm}


def collect(cwd, since):
    out = []
    for root, dirs, files in os.walk(cwd):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", ".gradle", ".cargo")]
        for f in files:
            if any(__import__("fnmatch").fnmatch(f, g) for g in ARTIFACT_GLOBS):
                p = os.path.join(root, f)
                if os.path.getmtime(p) >= since:
                    with open(p, "rb") as fh: out.append({"path": p, "bytes": os.path.getsize(p), "sha256": hashlib.sha256(fh.read()).hexdigest()})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", default=os.path.dirname(os.path.dirname(os.path.dirname(HERE))), help="directory containing the repo checkouts")
    ap.add_argument("--out", default="build-all-out"); ap.add_argument("--cpus", type=int, default=os.cpu_count() or 1)
    ap.add_argument("--only"); ap.add_argument("--with-deps", action="store_true"); ap.add_argument("--retry-failed", action="store_true"); ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    spec = json.load(open(os.path.join(HERE, "jobs.json")))
    jobs = {j["id"]: j for j in spec["jobs"]}
    if a.list:
        for j in spec["jobs"]: print(j["id"], "-", j["cmd"][:90])
        return 0
    ws, out = os.path.abspath(a.workspace), os.path.abspath(a.out)
    os.makedirs(os.path.join(out, "logs"), exist_ok=True)
    prev = {}
    try: prev = {r["id"]: r for r in json.load(open(os.path.join(out, "results.json")))["jobs"]}
    except (OSError, ValueError, KeyError): pass
    sel = set(jobs)
    if a.only: sel = set(a.only.split(","))
    if a.retry_failed: sel = {i for i in jobs if prev.get(i, {}).get("status") != "PASSED"}
    if a.only or a.retry_failed:
        if a.with_deps or a.retry_failed:
            todo = list(sel)
            while todo:
                for d in jobs[todo.pop()].get("deps", []):
                    if d not in sel and prev.get(d, {}).get("status") != "PASSED": sel.add(d); todo.append(d)
    repodirs = {k: os.path.join(ws, v) for k, v in spec["repos"].items()}
    snaps = {k: snapshot(v) for k, v in repodirs.items() if os.path.isdir(os.path.join(v, ".git")) or git(v, "rev-parse", "HEAD")}
    res = {i: dict(prev[i]) if i in prev and i not in sel else {"id": i, "status": "NOT RUN", "reason": "not selected"} for i in jobs}
    lock, cpu_free = threading.Condition(), a.cpus
    def run_job(j):
        nonlocal cpu_free
        r = {"id": j["id"], "repo": j["repo"], "command": j["cmd"], "required": j.get("required", False), "note": j.get("note")}
        rd = repodirs[j["repo"]]
        if not os.path.isdir(rd): return dict(r, status="BLOCKED", reason=f"checkout missing: {rd}")
        miss = [n for n in j.get("needs", []) if not probe(n, ws)]
        if miss: return dict(r, status="BLOCKED", reason="missing: " + ", ".join(miss))
        w = min(j.get("weight", 1), a.cpus)
        with lock:
            lock.wait_for(lambda: cpu_free >= w); cpu_free -= w
        try:
            env = dict(os.environ)
            for k, v in (j.get("env") or {}).items(): env[k] = os.path.join(ws, v[1:]) if v.startswith("@") else v
            cwd = os.path.join(rd, j["cwd"]); t0 = time.time(); log = os.path.join(out, "logs", j["id"] + ".log")
            with open(log, "wb") as lf:
                p = subprocess.run(["bash", "-c", j["cmd"]], cwd=cwd, env=env, stdout=lf, stderr=subprocess.STDOUT)
            return dict(r, status="PASSED" if p.returncode == 0 else "FAILED", exit_code=p.returncode, seconds=round(time.time() - t0, 1), log=log, artifacts=collect(cwd, t0) if p.returncode == 0 else [])
        finally:
            with lock: cpu_free += w; lock.notify_all()
    done, pending = {}, {i for i in jobs if i in sel}
    with cf.ThreadPoolExecutor(max_workers=max(1, a.cpus)) as ex:
        futs = {}
        while pending or futs:
            for i in list(pending):
                deps = jobs[i].get("deps", [])
                if any(d in pending or d in futs.values() for d in deps): continue      # a selected dependency has not finished yet
                st = [(done.get(d) or res[d]).get("status") for d in deps]
                if any(s in ("FAILED", "BLOCKED", "NOT RUN") for s in st if s):
                    done[i] = dict(id=i, repo=jobs[i]["repo"], command=jobs[i]["cmd"], required=jobs[i].get("required", False), status="NOT RUN", reason="dependency not PASSED: " + ",".join(d for d, s in zip(deps, st) if s != "PASSED")); pending.discard(i)
                elif all(s == "PASSED" for s in st):
                    futs[ex.submit(run_job, jobs[i])] = i; pending.discard(i)
            if futs:
                d, _ = cf.wait(list(futs), return_when=cf.FIRST_COMPLETED)
                for f in d: done[futs.pop(f)] = f.result()
            elif pending:
                break
    res.update(done)
    ordered = [res[j["id"]] for j in spec["jobs"]]
    required_bad = [r["id"] for r in ordered if r.get("required") and r["status"] != "PASSED"]
    summary = {"generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "host": {"cpus": os.cpu_count(), "mem_kb": int([l for l in open("/proc/meminfo") if l.startswith("MemTotal")][0].split()[1])},
               "sources": snaps, "jobs": ordered, "complete": not required_bad, "required_not_passed": required_bad}
    json.dump(summary, open(os.path.join(out, "results.json"), "w"), indent=2)
    with open(os.path.join(out, "MATRIX.md"), "w") as m:
        m.write(f"# Build/validation matrix ({summary['generated_utc']})\n\n**Overall: {'COMPLETE' if summary['complete'] else 'NOT COMPLETE — ' + str(len(required_bad)) + ' required job(s) not PASSED'}**\n\n| Job | Repo | Required | Status | Detail |\n|---|---|---|---|---|\n")
        for r in ordered: m.write(f"| {r['id']} | {r.get('repo','')} | {'yes' if r.get('required') else 'no'} | **{r['status']}** | {(r.get('reason') or ('%ss' % r.get('seconds','')) or '').replace('|','/')} |\n")
    print(open(os.path.join(out, "MATRIX.md")).read())
    return 0 if summary["complete"] else 1


if __name__ == "__main__":
    sys.exit(main())
