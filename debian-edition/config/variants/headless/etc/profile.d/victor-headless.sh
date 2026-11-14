# Interactive console login of the owner (headless variant): run the text first-run once, then show how to operate.
[ "$(id -un)" = victor ] || return 0
[ -t 0 ] || return 0
case "$-" in *i*) ;; *) return 0;; esac
if [ "$(sudo -n /usr/local/lib/victorlinux/victor-priv owner-status 2>/dev/null)" = locked ]; then
  /usr/local/bin/victor-firstrun --text
fi
echo "VictorLinux (headless). Operate with: victor-ctl --help   |   status: victor-ctl status"
