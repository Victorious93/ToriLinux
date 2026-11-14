#!/usr/bin/env bash
# Render the Arch templates with the compiler cache OFF and ON (no image build), syntax-check them, and drive the web Control Center in headless Chromium.
set -euo pipefail
src="$(cd "$(dirname "$0")/../.." && pwd)"; t="$(mktemp -d)"; trap 'rm -rf "$t"' EXIT
( cd "$src" && git ls-files -z | xargs -0 -I{} cp --parents {} "$t/" )
for v in 0 1; do ( cd "$t" && VICTOR_SCCACHE=$v python3 .ci/configure.py amd gui >/dev/null && bash -n airootfs/root/customize_airootfs/root-scripts/100-llamacpp.sh && bash -n profiledef.sh ); echo "render sccache=$v OK"; done
cd "$src" && NODE_PATH="$(npm root -g 2>/dev/null):/opt/node22/lib/node_modules" node tests/browser_ui.js
