# Phone companion API (`/victor/companion`)

Victorious Browser (Android) can show a VictorLinux PC's real hardware, model fit and running model, and chat with its local model.

## Boundaries

* The administrative Control Center stays on `127.0.0.1:8787` with its per-session token. **It is never published to the LAN**, and the phone never receives that token.
* The phone talks to the **inference gateway** only (default port 8080, enable *Remote access* in the Control Center, create a key). Everything it can learn is in one read-only, authenticated report:

```
GET /victor/companion
Authorization: Bearer <key>
```

The report (schema 1) contains: a stable PC id and host name, RAM and GPU names/VRAM, backend and memory pool for this boot, the running model's *file name*, context, GPU layers and the result of the generation check, the top local models with their fit, and TLS status/fingerprint. It contains **no keys, paths or chat content** and the endpoint rejects every method except GET.
* `127.0.0.1` on a phone is the phone. Use the PC's LAN or VPN/mesh address (for example a Victorious Mesh address), or an explicit tunnel you run (`adb reverse`, SSH port forward) and mark it as a tunnel in the app.
* Turn on TLS for anything beyond a trusted private link: `victor-tls-cert <pc-address>`, then enable TLS. The cert is self-signed, so the app asks you to compare and pin its SHA-256 fingerprint (shown in `/victor/companion` → `transport.tls_fingerprint_sha256`, and in `victor-tls-cert`). The app never ignores certificate errors and never trusts a certificate that does not match both the pin **and** the address.

## Pairing and states

1. In the PC's Control Center → Remote access, enable the gateway and create a key.
2. In Victorious Browser → Tools → *VictorLinux PC* → Add: address, key, (self-signed) certificate fingerprint.
3. *Check PC*. The first successful check pins the PC id. Later checks tell you plainly: **offline / tunnel down**, **bad or rotated key**, **wrong host / certificate problem**, **a different PC answered** (id changed), **model changed**, **unsupported report**.

Rotating or revoking the key in the Control Center makes every phone show *bad key* immediately; there is no grace period.
