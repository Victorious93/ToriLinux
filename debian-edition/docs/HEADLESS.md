# Headless variant

`sudo ./build.sh --variant headless` builds a console-only image: **no Xorg, Xfce, LightDM, Firefox, Tk or GUI Wireshark** (Wireshark is replaced by `tshark`; same group-restricted
capture model). Everything else is identical to the `gui` variant: same kernel, llama.cpp build (Vulkan + CPU), `victor-ctl`, `victor-priv`, VICTORDATA/TORIDATA persistence,
authenticated TLS endpoint, optional rootless-podman Kali, firewall, no telemetry. The ISO is named `…-<profile>-headless-…iso` and the manifest records `"variant": "headless"`.

## First boot
1. The owner account `victor` is logged in on **tty1** automatically. Same trust model as the GUI variant's LightDM autologin: the password is **locked** (no shared default), the account is not in `sudo`,
   and privileged actions go through `victor-priv`.
2. While the owner password is locked, the login profile runs `victor-firstrun --text` (min. 8 characters, set through the same privileged helper; stored as a hash on the data partition).
3. Operate with `victor-ctl` (`victor-ctl --help`): `storage`, `model import/select`, `llama start/stop/status/config`, `api connection/capabilities/selftest`, `hw`, `diag`, `kali`.

## Remote access
SSH stays off until you enable it (key-only; the helper refuses to enable it without a public key):
`sudo /usr/local/lib/victorlinux/victor-priv ssh-enable <pubkey-file> <bind-address>`; revoke with `… victor-priv revoke`. The LLM endpoint is configured with
`victor-ctl llama config --set …` (see ENDPOINT.md). There is no TUI front end yet.

## Status
Built and exercised under QEMU only (see TEST-REPORT.md for exactly what ran). Not tested: real hardware, GPU acceleration, long-running unattended use. The inference service is a
*user* systemd unit, so it needs an active owner session (tty1 autologin or an SSH login); `loginctl enable-linger` is not set.
