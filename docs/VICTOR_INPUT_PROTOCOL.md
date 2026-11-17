# VictorLinux input protocol v1

This is the protocol the **Victorious Keyboard** Android app (`com.victorious.keyboard`, [Victorious93/Victorious-Keyboard](https://github.com/Victorious93/Victorious-Keyboard)) uses to send keyboard and mouse events to a VictorLinux PC over the LAN.

Status: **v1**. Additive optional keys are allowed. A change that would change the meaning of an existing field is v2. Words **MUST**, **MUST NOT**, **SHOULD** follow RFC 2119.

The receiver is the `victor-input` service. It is **off by default**. There is no LAN discovery (no mDNS). The phone uses the address in the pairing QR.

## 1. Transport

- **WebSocket over TLS (WSS)** on TCP port **8473** by default, on the address the owner chose.
- TLS 1.2 or newer. The certificate is generated locally on the PC (self-signed). The phone **MUST** pin the fingerprint from the QR (`ifp`) and **MUST NOT** accept a different certificate.
- Plain `ws://` is not used for pairing or input. The QR is refused unless `iw` is a `wss://` URL with a port.
- Maximum text frame: 4096 bytes. Binary frames are rejected.
- A localhost-only HTTP admin interface (default `127.0.0.1:8472`) starts and stops pairing and reports status. It is not reachable from the LAN.

### Firewall

If a firewall is active (the Debian edition drops inbound traffic by default), open **TCP 8473** (or the chosen port) only while the service is on, and only for the address you chose. Loopback needs no change. Close the port when you turn the service off. The Arch edition has no default-drop firewall in this tree; if you add one, apply the same rule.

## 2. Pairing (`victorpc://pair`, VICTORPC_PAIRING v1)

Pairing is **off** until the owner presses *Pair phone (Wi-Fi keyboard)* in the Control Center (or runs `victor-input pair-start` / `victor-ctl input pair-start`).

The QR is a `victorpc://pair` URI using the VICTORPC_PAIRING v1 encodings: `v=1`, a 12-character base32 `id`, base64url `t`, epoch-seconds `x`, percent-encoded values with `+` as `%2B`, duplicate keys refused, unknown keys ignored. It carries only these fields:

| Key | Required | Meaning |
|---|---|---|
| `v` | yes | `1` |
| `id` | yes | this PC's pairing profile ID (12 base32 characters, random, stable) |
| `n` | yes | display name (`VictorLinux <hostname>`), 1–64 characters |
| `iw` | yes | input WebSocket URL, `wss://<address>:<port>` (WSS only) |
| `ifp` | yes | TLS certificate fingerprint: `SHA256:` + standard base64 (no padding) of SHA-256 over the DER certificate |
| `t` | yes | single-use pairing token, 16 random bytes, base64url (22 characters) |
| `x` | yes | token expiry, Unix seconds (now + 600) |

**The QR MUST NOT contain a Bearer token** (not even a hash). The long-lived token is issued only when the phone redeems `t`.

Example (throwaway values):

```
victorpc://pair?v=1&id=7Q2M4KX9TB5N&n=VictorLinux%20victor&iw=wss://192.168.1.42:8473&ifp=SHA256:gHC80Ray5GnftiotQW/JWkdaUZrT2zhJoAgvVkpna9w&t=AAECAwQFBgcICQoLDA0ODw&x=1762755600
```

This is the same URI scheme and encodings as the inference-gateway pairing QR ([API keys and phone pairing](api-keys-and-pairing.md)), with additive keys `iw` and `ifp` instead of `gw` and `gfp`. A consumer that only understands gateway or SSH profiles will see missing required keys for those profiles and must refuse them; supporting this input profile is a consumer-side change. Unknown keys must still be ignored.

### 2.1 Redeem

The phone redeems the token over TLS, pinned to `ifp`:

```
POST https://<host>:<port>/victor/pair
{"v":1,"t":"<token>","device":"Pixel 8"}

200 {"v":1,"ok":true,"token":"victor-input-…","device_id":"…","name":"Phone: Pixel 8"}
403 {"v":1,"ok":false,"err":"none|expired|bad_token|too_many_attempts"}
400 {"v":1,"ok":false,"err":"bad_message"}
```

- The HTTP origin is the same host and port as `iw`, with `https://` instead of `wss://`.
- `t` is consumed before the Bearer token is minted (single use). A second redeem of the same `t` returns `403` / `none`.
- Three wrong tokens close the pairing window.
- The Bearer token is returned **once**, only to the phone. The PC stores only its SHA-256 hash. Clients MUST treat it as a secret and MUST NOT log it.
- The owner can revoke the device later; the hash remains marked revoked and the token stops working.

## 3. WebSocket session

Connect to `iw`. Pin `ifp`. Then authenticate in **one** of these ways:

1. HTTP header `Authorization: Bearer <token>` on the WebSocket upgrade. The server replies with a text frame `{ "v": 1, "type": "ok", "device_id": "…" }`.
2. First text frame:

```json
{"v":1,"type":"auth","token":"victor-input-…","device":"Pixel 8"}
```

The server replies `{ "v": 1, "type": "ok", "device_id": "…", "name": "Phone: Pixel 8" }` or `{ "v": 1, "type": "error", "err": "unauthorized" }` and closes.

Unauthenticated input messages are rejected with `err: "unauthorized"` and the connection is closed. There is no anonymous mode.

## 4. Input messages

Every message after auth is a single JSON object, UTF-8, maximum 4096 bytes.

Common fields (required on every input message except `auth`):

| Key | Type | Rules |
|---|---|---|
| `v` | int | `1` |
| `type` | string | `key`, `pointer`, or `ping` |
| `ts` | int | Unix time in **milliseconds**. Rejected if more than 30 seconds from the PC clock (`err: "replay"`). |
| `n` | string | Unique nonce, 16–64 characters `[A-Za-z0-9_-]`. A nonce already seen from this device is rejected (`err: "replay"`). |

The server replies `{ "v": 1, "type": "ack", "n": "<same n>" }` or `{ "v": 1, "type": "error", "err": "…" }`.

### 4.1 `key` (US layout first)

```json
{"v":1,"type":"key","ts":1730000000123,"n":"AbCdEfGhIjKlMnOp","down":["ShiftLeft","KeyA"],"up":["KeyA","ShiftLeft"]}
```

- `down` and `up` are arrays of UI Events `code` names from the US ANSI set shipped by VictorLinux (letters, digits, modifiers, arrows, function keys, numpad, and the usual punctuation). Unknown names are rejected.
- At most 16 key names in `down` and `up` combined.
- `down` is applied before `up`. An empty movement (both lists empty) is allowed and produces no evdev events.
- Layout is **US** in v1. Other layouts would be a later, additive revision.

### 4.2 `pointer` (relative mouse)

```json
{"v":1,"type":"pointer","ts":1730000000123,"n":"AbCdEfGhIjKlMnOq","dx":12,"dy":-3,"wheel":0,"hwheel":0,"buttons":{"left":1}}
```

| Key | Type | Rules |
|---|---|---|
| `dx`, `dy` | int | Relative movement, inclusive range −4096…4096. Omitted = 0. |
| `wheel`, `hwheel` | int | Vertical / horizontal scroll, inclusive range −16…16. Omitted = 0. |
| `buttons` | object | Optional. Keys `left`, `right`, `middle` only. Values `0` or `1` (button up / down). |

### 4.3 `ping`

```json
{"v":1,"type":"ping","ts":1730000000123,"n":"AbCdEfGhIjKlMnOr"}
```

Keeps the session alive. No uinput events.

## 5. Rate limits and errors

- At most **250 messages per second** per device (rolling one-second window). Over the limit: `err: "rate_limited"`.
- Error codes: `unauthorized`, `replay`, `rate_limited`, `bad_message`.
- The server does not log Bearer tokens, pairing tokens, or message bodies.

## 6. Versioning

- `v` in the URI and in every JSON object is `1`.
- New optional JSON keys and new optional QR keys MAY be added in v1 and MUST be ignored if unknown (URI) or MAY be ignored if unknown (JSON), except that an unknown `type` is `bad_message`.
- A change to the meaning of an existing field, a new required field, or a new `v` is a new protocol version.
