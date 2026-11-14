#!/usr/bin/env bash
# Fetch pinned third-party sources BEFORE the image build (build-time internet needed here, not at boot).
# Verifies the llama.cpp tag still resolves to the pinned commit and records the SHA for the manifest.
set -euo pipefail
cd "$(dirname "$0")/.."

opt() { python3 - "$1" "$2" <<'PY'
import configparser, sys
c = configparser.ConfigParser(); c.read(".ci/options.ini"); print(c[sys.argv[1]][sys.argv[2]])
PY
}

dest=airootfs/root/sources
rm -rf "$dest"; mkdir -p "$dest"
repo=$(opt repositories llamacpp); tag=$(opt revisions llamacpp); want=$(opt revisions llamacpp_commit)

echo "==> cloning llama.cpp $tag"
git clone --quiet --depth 1 --branch "$tag" "$repo.git" "$dest/llama.cpp"
got=$(git -C "$dest/llama.cpp" rev-parse HEAD)
if [ "$got" != "$want" ]; then
  echo "ERROR: tag $tag now resolves to $got but .ci/options.ini pins $want (tag moved?). Refusing to build." >&2
  exit 1
fi
echo "$got" > "$dest/llama.cpp.rev"
rm -rf "$dest/llama.cpp/.git"
echo "llama.cpp $tag = $got"
