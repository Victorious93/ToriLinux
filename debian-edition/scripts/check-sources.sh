#!/usr/bin/env bash
# Syntax-check every shell/python file in a config tree and enforce exec bits on scripts.
set -Eeuo pipefail
root="${1:?config dir}"; fail=0
while IFS= read -r -d '' f; do
  first="$(head -c 64 "$f" | head -1)"
  case "$first" in
    '#!'*bash*|'#!'*/sh*|'#!'*env\ sh*) bash -n "$f" || { echo "syntax error: $f"; fail=1; } ;;
    '#!'*python3*) python3 -I -m py_compile "$f" || { echo "python error: $f"; fail=1; } ;;
    *) continue ;;
  esac
  case "$f" in */etc/skel/*|*/usr/share/*) continue;; esac
  [ -x "$f" ] || { chmod +x "$f"; echo "fixed exec bit: $f"; }
done < <(find "$root" -type f -print0)
exit $fail
