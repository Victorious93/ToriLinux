#!/usr/bin/env bash
# Measure native-build compiler caching for llama.cpp with FRESH, disposable outputs (a no-op rebuild proves nothing).
#   scripts/cache-bench.sh SRC_DIR RESULT_DIR [--sccache BIN --cache-dir DIR] [--label NAME] [--jobs N] [-- extra cmake args]
# Records: exact commands, versions, wall seconds, sccache hit/miss/uncacheable counts, cache size. Measured note: sccache keys come from the
# preprocessed source + arguments; on this llama.cpp revision a DIFFERENT build dir still hit 9/9 (see docs/caching.md), a changed build type or flag missed 9/9.
set -euo pipefail
src="${1:?llama.cpp source}"; res="${2:?result dir}"; shift 2
sccache=""; cache_dir=""; label="run"; jobs="$(nproc)"; extra=()
while [ $# -gt 0 ]; do case "$1" in
  --sccache) sccache="$2"; shift 2;; --cache-dir) cache_dir="$2"; shift 2;; --label) label="$2"; shift 2;;
  --jobs) jobs="$2"; shift 2;; --) shift; extra=("$@"); break;; *) echo "bad arg $1" >&2; exit 2;; esac; done
mkdir -p "$res"; build="${BUILD_DIR:-/var/tmp/cache-bench-build}"
rm -rf "$build"
launcher=()
if [ -n "$sccache" ]; then
  export SCCACHE_DIR="$cache_dir" SCCACHE_CACHE_SIZE="${SCCACHE_CACHE_SIZE:-5G}" SCCACHE_IDLE_TIMEOUT=0
  "$sccache" --stop-server >/dev/null 2>&1 || true
  "$sccache" --start-server >/dev/null; "$sccache" --zero-stats >/dev/null
  launcher=(-DCMAKE_C_COMPILER_LAUNCHER="$sccache" -DCMAKE_CXX_COMPILER_LAUNCHER="$sccache")
fi
cfg=(cmake -S "$src" -B "$build" -G Ninja -DCMAKE_BUILD_TYPE=Release "${launcher[@]}"
  -DBUILD_SHARED_LIBS=ON -DGGML_BACKEND_DL=ON -DGGML_CPU_ALL_VARIANTS=ON -DGGML_NATIVE=OFF
  -DLLAMA_OPENSSL=OFF -DLLAMA_CURL=OFF -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF -DLLAMA_BUILD_SERVER=ON "${extra[@]}")
bld=(cmake --build "$build" -j"$jobs" --target "${TARGET:-llama-server}")
{ echo "label=$label"; echo "date=$(date -u +%FT%TZ)"; echo "cpu=$(grep -m1 'model name' /proc/cpuinfo | cut -d: -f2)"; echo "cores=$(nproc) jobs=$jobs"
  echo "mem_kb=$(awk '/MemTotal/{print $2}' /proc/meminfo)"; echo "gxx=$(g++ --version | head -1)"; echo "cmake=$(cmake --version | head -1)"
  echo "src_commit=$(git -C "$src" rev-parse HEAD 2>/dev/null || echo unknown)"; [ -n "$sccache" ] && echo "sccache=$("$sccache" --version)"
  echo "configure: ${cfg[*]}"; echo "build: ${bld[*]}"; } > "$res/$label.env"
"${cfg[@]}" > "$res/$label.configure.log" 2>&1
t0=$(date +%s.%N)
set +e; "${bld[@]}" > "$res/$label.build.log" 2>&1; rc=$?; set -e
t1=$(date +%s.%N)
python3 - "$res/$label.json" "$label" "$rc" "$t0" "$t1" "$build" "${sccache:-}" <<'PY'
import json, subprocess, sys, os
out, label, rc, t0, t1, build, sc = sys.argv[1:8]
d = {"label": label, "exit_code": int(rc), "wall_seconds": round(float(t1) - float(t0), 1)}
bin_ = os.path.join(build, "bin", "llama-server"); d["llama_server_built"] = os.path.exists(bin_)
open(out, "w").write(json.dumps(d, indent=2))      # timing is on disk even if the stats call below fails
if sc:
    s = json.loads(subprocess.run([sc, "--show-stats", "--stats-format", "json"], capture_output=True, text=True).stdout)["stats"]
    hits = sum(s["cache_hits"]["counts"].values()); miss = sum(s["cache_misses"]["counts"].values())
    d["sccache"] = {"hits": hits, "misses": miss, "requests_executed": s["requests_executed"], "requests_not_cacheable": s["requests_not_cacheable"],
                    "non_cacheable_compilations": s["non_cacheable_compilations"], "compile_requests": s["compile_requests"], "compile_fails": s["compile_fails"],
                    "cache_write_errors": s["cache_write_errors"], "cache_read_errors": s["cache_read_errors"],
                    "hit_rate": round(hits / (hits + miss), 4) if hits + miss else None}
print(json.dumps(d)); open(out, "w").write(json.dumps(d, indent=2))
PY
if [ -n "$sccache" ]; then du -sk "$cache_dir" | awk '{print "cache_size_kb=" $1}' | tee -a "$res/$label.env"; "$sccache" --stop-server >/dev/null 2>&1 || true; fi
[ "$rc" -eq 0 ] || { echo "BUILD FAILED rc=$rc (see $res/$label.build.log)" >&2; exit "$rc"; }
