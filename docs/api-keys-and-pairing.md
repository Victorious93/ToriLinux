# API keys and phone pairing (Arch edition)

Everything here is **off by default**. VictorLinux works fully offline on its own: the gateway listens on `127.0.0.1` with no key needed until you turn on remote access in *Server & Access*.

## Named API keys

- Create one key per app or device. Each key can have an optional expiry (days) and an optional limit in requests per minute.
- A key is shown **once** when it's created. Only its SHA-256 hash is stored in `config/secrets.json` (`api_keys`, unchanged format), with its name, expiry and limit in `api_key_meta`.
- **Revoke** takes effect immediately, with no restart. The gateway re-reads the secrets file when it changes. Expired keys get `401` with an "expired" message. Over-limit requests get `429` and `Retry-After: 60`.
- *Rotate: replace all keys* swaps every key (named and phone keys too) for one new key. *Revoke all remote access* deletes all keys and returns to local-only.
- **Usage log** `logs/api-usage.jsonl` (0600, rotated at 1 MiB) records the time, key ID and name, method, path without the query, status and client address. It never stores a key, a token or request content. The Control Center shows per-key requests and errors over 24 hours, last use, and recent requests.

## Pair phone (opt-in)

*Server & Access → Pair phone* shows a QR code for 10 minutes. It only works after you have turned on remote access with HTTPS and a certificate, so the phone can pin this PC.

The QR is a `victorpc://pair` URI using the VICTORPC_PAIRING v1 encodings: `v=1`, a 12-character base32 `id`, base64url `t`, epoch-seconds `x`, percent-encoded values with `+` as `%2B`, duplicate keys refused and unknown keys ignored. It carries only these fields:

| Key | Meaning |
|---|---|
| `v` | `1` |
| `id` | this PC's pairing profile ID (12 base32 characters, random, stable) |
| `n` | display name (`VictorLinux <hostname>`) |
| `gw` | gateway URL, `https://<address>:<port>` (HTTPS only) |
| `gfp` | gateway TLS certificate fingerprint: `SHA256:` + base64 (no padding) of SHA-256 over the DER certificate. The UI also shows it as hex for comparison. |
| `t` | single-use pairing token, 16 random bytes, base64url (22 characters) |
| `x` | token expiry, Unix seconds (now + 600) |

Example:

```
victorpc://pair?v=1&id=7Q2M4KX9TB5N&n=VictorLinux%20victor&gw=https://192.168.1.42:8443&gfp=SHA256:gHC80Ray5GnftiotQW/JWkdaUZrT2zhJoAgvVkpna9w&t=AAECAwQFBgcICQoLDA0ODw&x=1762755600
```

**The QR never contains an API key** (not even a hash). The phone redeems the token over TLS, pinned to `gfp`:

```
POST https://<gw>/victor/pair   {"v":1,"t":"<token>","device":"Pixel 8"}
200 {"v":1,"ok":true,"api_key":"victor-…","key_id":"…","name":"Phone: Pixel 8"}
403 {"v":1,"ok":false,"err":"none|expired|bad_token|too_many_attempts|tls_required"}
```

- The token is consumed before the key is minted, so it is single use. Three wrong tokens close the window.
- The new key is returned only to the phone, once, and appears in the key list as `Phone: <device>`, where it can be revoked.
- The endpoint refuses plain HTTP unless the gateway is loopback-only.
- The QR is drawn locally by a built-in encoder (`qrsvg.py`). No web service is involved. It is shown only on the Control Center screen (`127.0.0.1`, session token).

**No LAN discovery.** VictorLinux does not advertise itself (no mDNS). The phone uses the address in the QR.

### Relation to the VictorSuite SSH profile

The frozen VICTORPC_PAIRING v1 spec describes SSH profiles (`u`, `h`, `p`, `hk`, `pp`, enrollment on a one-shot TCP listener). VictorLinux pairing is for the **inference gateway**, not SSH. It uses the v1 URI and encodings, but with the additive gateway keys `gw` and `gfp` instead of the SSH keys. A consumer that only understands SSH profiles will see missing required keys and must refuse. Supporting gateway profiles is a consumer-side change.

## Debian edition

Not ported. The Debian edition serves the API directly from llama-server with its own key file (`victor-ctl api rotate-key`), so it has no gateway for per-key limits, usage logs or pairing.
