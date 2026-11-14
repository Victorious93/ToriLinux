# VictorLinux login: start the desktop on tty1 if installed, else print how to open the Control Center.
if [[ -z "$DISPLAY" && "$(tty)" == /dev/tty1 ]]; then
  if command -v startx >/dev/null 2>&1 && [[ -x /usr/bin/openbox ]]; then
    exec startx
  fi
  echo
  echo "VictorLinux - headless image"
  echo "  victor-open            open the Control Center (prints a local URL if no browser)"
  echo "  victor-diag            GPU / backend / storage diagnostics"
  echo "  victor-model-import F  validate and import a .gguf model"
  cat /run/victor/persist.json 2>/dev/null | python3 -c 'import sys,json; print("  storage:", json.load(sys.stdin).get("message"))' 2>/dev/null
fi
