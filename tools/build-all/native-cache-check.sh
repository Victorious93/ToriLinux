#!/usr/bin/env bash
# Build llama.cpp (CPU profile) with the compiler cache off or on, from LLAMA_SRC (a checkout of the pinned commit). Needs minutes; not part of the default quick run.
set -euo pipefail
src="$(cd "$(dirname "$0")/../.." && pwd)"; : "${LLAMA_SRC:?set LLAMA_SRC to a llama.cpp checkout at the pinned commit}"
res="${RESULT_DIR:-$src/out/native-cache}"; mkdir -p "$res"
if [ "$1" = off ]; then "$src/scripts/cache-bench.sh" "$LLAMA_SRC" "$res" --label ci-off
else VICTOR_TOOLS_DIR="$src/out/tools" "$src/scripts/sccache-stage.sh" fetch; t="$src/out/tools/sccache"; c="$res/cache"; rm -rf "$c"
  "$src/scripts/cache-bench.sh" "$LLAMA_SRC" "$res" --label ci-cold --sccache "$t" --cache-dir "$c"; "$src/scripts/cache-bench.sh" "$LLAMA_SRC" "$res" --label ci-warm --sccache "$t" --cache-dir "$c"; fi
