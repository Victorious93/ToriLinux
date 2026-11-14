#!/usr/bin/env bash
# Install the fleet coordinator into an image tree. Shared by BOTH editions so the scheduler is never duplicated.
#   fleet/install.sh DEST_TREE      e.g. debian-edition build:  config/includes.chroot_after_packages
#                                        arch edition build:    airootfs
# Copies only runtime files: no tests, no fixtures that need the test harness, no caches.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"; dest="${1:?destination tree}"
rm -rf "$dest/usr/local/lib/victorfleet"
install -d -m 0755 "$dest/usr/local/lib/victorfleet" "$dest/usr/local/bin"
cp -a "$here/victorfleet" "$dest/usr/local/lib/victorfleet/victorfleet"
install -d -m 0755 "$dest/usr/local/lib/victorfleet/fixtures"
cp -a "$here/fixtures/v1" "$dest/usr/local/lib/victorfleet/fixtures/v1"
find "$dest/usr/local/lib/victorfleet" -name __pycache__ -type d -prune -exec rm -rf {} +
install -m 0755 "$here/bin/victor-fleet" "$dest/usr/local/bin/victor-fleet"
python3 -m py_compile "$dest/usr/local/lib/victorfleet/victorfleet/"*.py && find "$dest/usr/local/lib/victorfleet" -name __pycache__ -type d -prune -exec rm -rf {} +
echo "victor-fleet installed into $dest"
