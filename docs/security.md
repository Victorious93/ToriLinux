# Access and security model

* **No shared password.** `tori` and `root` are locked. The *physical console* auto-logs in `tori` (anyone with the
  USB and the PC is the owner by assumption). Nothing can log in over the network.
* **SSH is off.** Enable it in the Control Center after pasting your **public key**; key-only, `AllowUsers tori`,
  `PermitRootLogin no`, listens only on the address you choose. Password SSH is not supported.
* **Least privilege.** AI applications run as `tori`. sudo is limited to one validated helper,
  `/usr/local/lib/tori/tori-priv` (mount storage, SSH key/enable/reset, reboot/poweroff). There is no `NOPASSWD: ALL`.
* **Network exposure.** Services bind to `127.0.0.1`; the inference gateway opens a LAN/VPN address only when you
  set it and have an API key. mDNS advertising is disabled.
* **Reset / revoke.** Control Center: *Revoke all remote access* (API key + bind), *Remove all keys & disable* (SSH).
  Rebooting the live image resets everything that is not on `TORIDATA`; delete `TORIDATA/tori/config/secrets.json`
  to drop stored keys.
* **Local UI.** The Control Center listens on loopback only, requires a per-session token, and rejects foreign
  `Host` headers (DNS rebinding).
* **Offline by design.** No telemetry, no auto-update, no cloud fallback. Downloading models is a manual act.

Known limits: a self-signed certificate protects against passive snooping only if the client pins it; the RAM cap
is estimate-based, not a kernel limit; Secure Boot is not supported by this ISO.
