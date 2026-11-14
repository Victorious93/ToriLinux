#!/usr/bin/env bash
# Build-only compiler-cache plumbing shared by both editions. Disabled unless VICTOR_SCCACHE=1; the ordinary uncached
# build path is the default and is never altered by this file.
#
#   sccache-stage.sh fetch                 download + verify the pinned static sccache into out/tools/ (cached)
#   sccache-stage.sh stage  DEST_TREE      copy wrapper + bounded cache INTO the image-build tree (before the chroot build)
#   sccache-stage.sh harvest SRC_TREE      copy the (bounded) cache back OUT of the build tree (after build, success or failure)
#   sccache-stage.sh check-clean ROOT      fail if wrapper/cache/daemon state is present in a finished image tree
#   sccache-stage.sh purge                 delete the host-side cache
#
# Why copy-in/copy-out and not a bind mount: neither mkarchiso nor live-build lets a profile bind-mount host paths into
# the chroot where the compiler really runs, and the chroot is deleted after the build. The cache therefore travels as plain
# files, capped by VICTOR_SCCACHE_MAX_MB (default 2048): an over-cap cache is trimmed oldest-first before staging.
# Keys/credentials: sccache is used with its LOCAL disk backend only; no remote backend, no tokens, nothing secret exists.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"; root="$(cd "$here/.." && pwd)"
. "$here/sccache.pin"
tools="${VICTOR_TOOLS_DIR:-$root/out/tools}"
cache="${VICTOR_SCCACHE_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/victorlinux/sccache}"
max_mb="${VICTOR_SCCACHE_MAX_MB:-2048}"
# paths INSIDE the image tree; chosen so one rule removes everything (see check-clean)
in_bin="usr/local/lib/victor-build"; in_cache="var/cache/victor-sccache"

cmd="${1:-}"; shift || true
case "$cmd" in
  fetch)
    mkdir -p "$tools"
    if [ -x "$tools/sccache" ] && "$tools/sccache" --version | grep -q "${SCCACHE_VERSION#v}"; then echo "$tools/sccache ($SCCACHE_VERSION) already present"; exit 0; fi
    tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
    curl -fsSL -o "$tmp/s.tgz" "$SCCACHE_URL"
    got="$(sha256sum "$tmp/s.tgz" | awk '{print $1}')"
    [ "$got" = "$SCCACHE_SHA256" ] || { echo "sccache checksum mismatch: got $got want $SCCACHE_SHA256 (refusing; building uncached)" >&2; exit 3; }
    tar -xzf "$tmp/s.tgz" -C "$tmp"; install -m 0755 "$tmp"/sccache-*/sccache "$tools/sccache"
    echo "installed $("$tools/sccache" --version) -> $tools/sccache";;
  stage)
    dest="${1:?dest tree}"
    [ "${VICTOR_SCCACHE:-0}" = 1 ] || { echo "sccache disabled (VICTOR_SCCACHE != 1): ordinary uncached build"; exit 0; }
    if ! "$here/sccache-stage.sh" fetch >/dev/null 2>&1; then
      # decided BEFORE compilation, not by retrying a failed compile: no wrapper -> uncached build, said out loud
      echo "WARNING: pinned sccache could not be fetched/verified; continuing with an UNCACHED build" >&2; exit 0
    fi
    install -d -m 0755 "$dest/$in_bin" "$dest/$in_cache"
    install -m 0755 "$tools/sccache" "$dest/$in_bin/sccache"
    mkdir -p "$cache"
    size_mb=$(du -sm "$cache" | awk '{print $1}')
    if [ "$size_mb" -gt "$max_mb" ]; then
      echo "cache is ${size_mb} MiB > cap ${max_mb} MiB: trimming oldest entries first"
      find "$cache" -type f -printf '%T@ %p\n' | sort -n | while read -r _ f; do
        [ "$(du -sm "$cache" | awk '{print $1}')" -le "$max_mb" ] && break; rm -f "$f"; done
    fi
    cp -a "$cache/." "$dest/$in_cache/" 2>/dev/null || true
    echo "staged sccache $SCCACHE_VERSION + $(du -sm "$dest/$in_cache" | awk '{print $1}') MiB cache into $dest";;
  harvest)
    src="${1:?source tree}"
    [ "${VICTOR_SCCACHE:-0}" = 1 ] && [ -d "$src/$in_cache" ] || exit 0
    mkdir -p "$cache"; cp -a "$src/$in_cache/." "$cache/"
    echo "harvested cache: $(du -sm "$cache" | awk '{print $1}') MiB (cap ${max_mb} MiB, trimmed on next stage)";;
  check-clean)
    r="${1:?image tree root}"; bad=0
    for p in "$in_bin" "$in_cache" root/.config/sccache tmp/sccache.sock; do
      if [ -e "$r/$p" ]; then echo "LEAK: $p present in the image" >&2; bad=1; fi
    done
    if find "$r" -xdev \( -name 'sccache*' -o -name '.sccache*' \) -not -path "$r/proc/*" 2>/dev/null | grep -q .; then
      echo "LEAK: sccache files found in the image:" >&2; find "$r" -xdev -name 'sccache*' 2>/dev/null | head -5 >&2; bad=1; fi
    [ "$bad" = 0 ] && echo "image tree is clean of compiler-cache material";  exit "$bad";;
  purge)
    rm -rf "$cache"; echo "purged $cache";;
  *) sed -n 2,14p "$0"; exit 2;;
esac
