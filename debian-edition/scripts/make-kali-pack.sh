#!/usr/bin/env bash
# Build an OFFLINE Kali pack (tar of the pinned official image) to copy onto external media. Needs network + podman.
# Optional packages can be baked in with --with "pkg1 pkg2" (creates a derived image tagged victor-kali:custom; the
# derived image is NOT the pinned digest, so victor-ctl will not accept it unless you re-tag; default is the pinned image).
set -Eeuo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"; . "$HERE/versions.env"
OUT="${1:-./kali-pack.tar}"
ref="$KALI_IMAGE@$KALI_DIGEST"
podman pull "$ref"
podman save -o "$OUT" "$ref"
sha256sum "$OUT" | tee "$OUT.sha256"
echo "Pack for $ref written to $OUT ($(du -h "$OUT" | cut -f1)). Load on VictorLinux: Control Center > Kali > Load offline pack."
