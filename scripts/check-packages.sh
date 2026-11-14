#!/usr/bin/env bash
# Verify every package in the rendered packages.x86_64 exists in the pinned snapshot (run in Arch, as root).
set -euo pipefail
cd "$(dirname "$0")/.."
db=$(mktemp -d)
trap 'rm -rf "$db"' EXIT
pacman --config pacman.conf --dbpath "$db" -Sy >/dev/null
missing=0
while read -r pkg; do
  [[ -z "$pkg" || "$pkg" == \#* ]] && continue
  if ! pacman --config pacman.conf --dbpath "$db" -Si "$pkg" >/dev/null 2>&1; then
    echo "MISSING in snapshot: $pkg"; missing=1
  fi
done < packages.x86_64
[ "$missing" -eq 0 ] && echo "all packages resolvable"
exit "$missing"
