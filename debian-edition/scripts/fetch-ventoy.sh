#!/usr/bin/env bash
# Download Ventoy (pinned version) and verify it against the project's published SHA-256 list before use.
# Usage: fetch-ventoy.sh [DEST=/opt/ventoy]   (needs network; build/prepare-time only)
set -Eeuo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"; . "$HERE/versions.env"
DEST="${1:-/opt/ventoy}"; v="$VENTOY_VERSION"; base="https://github.com/ventoy/Ventoy/releases/download/v$v"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
curl -fL --retry 3 -o "$tmp/ventoy.tar.gz" "$base/ventoy-$v-linux.tar.gz"
curl -fL --retry 3 -o "$tmp/sha256.txt" "$base/sha256.txt"
want="$(awk -v f="ventoy-$v-linux.tar.gz" '$2==f || $2=="*"f {print $1}' "$tmp/sha256.txt")"
[ -n "$want" ] || { echo "no checksum entry for ventoy-$v-linux.tar.gz in sha256.txt" >&2; exit 1; }
got="$(sha256sum "$tmp/ventoy.tar.gz" | cut -d' ' -f1)"
[ "$got" = "$want" ] || { echo "Ventoy checksum MISMATCH: $got != $want" >&2; exit 1; }
echo "Ventoy $v sha256 verified: $got"
mkdir -p "$tmp/x"; tar -xzf "$tmp/ventoy.tar.gz" -C "$tmp/x"
src="$(dirname "$(find "$tmp/x" -name Ventoy2Disk.sh -print -quit)")"; [ -f "$src/Ventoy2Disk.sh" ] || { echo "Ventoy2Disk.sh not found in archive" >&2; exit 1; }
rm -rf "$DEST"; mkdir -p "$(dirname "$DEST")"; cp -a "$src" "$DEST"
echo "installed to $DEST"
echo "NOTE: the checksum list comes from the same release page; for stronger assurance verify the signature/fingerprint per https://www.ventoy.net/en/doc_verify.html"
