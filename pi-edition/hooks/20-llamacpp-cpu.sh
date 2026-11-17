#!/bin/bash
# llama.cpp for arm64 (NEON) CPU, same pinned commit as the Debian edition. BUILD-time network only.
# GGML_NATIVE=OFF so one image runs on Cortex-A76 (Pi 5), A72 (Pi 4) and A53 (Zero 2 W). With GGML_CPU_ALL_VARIANTS
# the best ARM variant (dotprod/fp16 on Pi 5) is picked at runtime; if this commit cannot configure that on arm64,
# fall back to a plain armv8-a NEON build.
set -Eeuo pipefail
. /usr/share/victorlinux/versions.env
[ -f /usr/share/victorlinux/build-env ] && . /usr/share/victorlinux/build-env
export DEBIAN_FRONTEND=noninteractive
BUILD_DEPS="build-essential cmake ninja-build git ca-certificates pkg-config libssl-dev"
dpkg-query -W -f='${Package}\n' | sort > /tmp/pkgs.before
apt-get update
apt-get install -y --no-install-recommends $BUILD_DEPS
src=/tmp/llama.cpp
rm -rf "$src"; git init -q "$src"; cd "$src"
git remote add origin "$LLAMACPP_REPO"
git fetch -q --depth 1 origin "$LLAMACPP_COMMIT"
git checkout -q FETCH_HEAD
got="$(git rev-parse HEAD)"
[ "$got" = "$LLAMACPP_COMMIT" ] || { echo "llama.cpp commit mismatch: $got != $LLAMACPP_COMMIT" >&2; exit 1; }
common=(-G Ninja -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=ON -DGGML_NATIVE=OFF -DLLAMA_OPENSSL=ON -DLLAMA_CURL=OFF
        -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF -DLLAMA_BUILD_SERVER=ON
        -DCMAKE_INSTALL_PREFIX=/opt/victorlinux/llama.cpp "-DCMAKE_INSTALL_RPATH=\$ORIGIN/../lib")
variant="cpu(arm all-variants)"
if ! cmake -S . -B build "${common[@]}" -DGGML_BACKEND_DL=ON -DGGML_CPU_ALL_VARIANTS=ON; then
  echo "llama.cpp: ARM all-variants not available at this commit; building baseline armv8-a NEON" >&2
  rm -rf build; variant="cpu(armv8-a neon)"
  cmake -S . -B build "${common[@]}" -DGGML_CPU_ARM_ARCH=armv8-a
fi
jobs="$(nproc)"; [ "$jobs" -gt 4 ] && jobs=4     # bounded RAM under qemu / small runners
cmake --build build -j"$jobs"
cmake --install build
{
  echo "llama.cpp_repo=$LLAMACPP_REPO"; echo "llama.cpp_tag=$LLAMACPP_TAG"; echo "llama.cpp_commit=$got"
  echo "backends=$variant"; echo "arch=arm64"; echo "openssl=on"; echo "curl_download_in_server=off"
} > /usr/share/victorlinux/llamacpp.build-info
find /opt/victorlinux/llama.cpp -type f \( -name '*.so*' -o -perm -u+x \) -exec strip --strip-unneeded {} + 2>/dev/null || true
/opt/victorlinux/llama.cpp/bin/llama-server --version 2>&1 | head -3 || true
cd /; rm -rf "$src"
dpkg-query -W -f='${Package}\n' | sort > /tmp/pkgs.after
to_purge="$(comm -13 /tmp/pkgs.before /tmp/pkgs.after | grep -x -F -f <(echo $BUILD_DEPS | tr ' ' '\n') || true)"
[ -z "$to_purge" ] || apt-get purge -y $to_purge
apt-get autoremove -y --purge
apt-get clean
for must in git libgomp1 libssl3t64 python3; do dpkg -s "$must" >/dev/null 2>&1 || { echo "required package $must was removed" >&2; exit 1; }; done
test -x /opt/victorlinux/llama.cpp/bin/llama-server
