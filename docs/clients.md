# Client apps and endpoints

This matrix is for people writing or pairing clients with a VictorLinux PC. It is based on what **this repository** actually exposes today (`main` at `01fdd30`). Client-side behavior that was not verified from this code or these docs is marked [Guessing].

Related docs: [remote access](remote-access.md), [companion API](companion-api.md), [fleet integration](fleet-integration.md), [fleet coordinator](../fleet/docs/FLEET.md), [fleet integration plan](FLEET_INTEGRATION_PLAN.md).

## Default state

[Certain] VictorLinux works without any of these clients.

| Surface | Default | Where that is set |
|---|---|---|
| Inference gateway | Bound to `127.0.0.1:8080`, `remote.enabled` is `False`, TLS off | `victorcommon.py` default settings |
| Gateway auth | Not required on loopback while remote access is off. Required if the bind is not loopback, or if remote access is enabled (including when a tunnel makes a remote client look like loopback) | `gateway.py` `build_config()` |
| API keys | None until the owner creates one. The plaintext key is shown once and stored only as SHA-256 | `gateway.py`, [remote-access.md](remote-access.md) |
| Admin Control Center | `127.0.0.1:8787` with a per-session token. Never published to the LAN. Phones must not receive this token | [companion-api.md](companion-api.md), [fleet-integration.md](fleet-integration.md) |
| Fleet coordinator | Off, not started at boot, listens on `127.0.0.1` until the owner runs `victor-fleet config --enable-network ADDR` | [fleet/docs/FLEET.md](../fleet/docs/FLEET.md) |
| Pairing QR, mDNS, RustDesk pack | Not present in this tree | See the [integration plan](FLEET_INTEGRATION_PLAN.md) |

The product name is VictorLinux (`Victorious93/VictorLinux`). `/tori/` health and capabilities paths are compatibility aliases only.

## Gateway endpoints (inference)

Implemented in [`airootfs/usr/local/lib/victorlinux/gateway.py`](../airootfs/usr/local/lib/victorlinux/gateway.py). llama-server itself stays on `127.0.0.1` with a private key; clients talk only to the gateway. [Certain] There is no shell, file, or tool-execution route on this port. `tool_execution_on_pc` is always `false`.

Auth column below is for the usual remote case (remote access on, or a non-loopback bind). Exception: `GET /victor/health` and `GET /tori/health` never require a key.

| Path | Methods of interest | Auth | What it does |
|---|---|---|---|
| `/victor/health`, `/tori/health` | GET | None | `200 {"status":"ok","gateway":"ok","auth_required":...}` or `503` with `"status":"model_not_running"`. No secrets. |
| `/victor/capabilities`, `/tori/capabilities` | GET | Bearer when auth is required | Honest capability report. **No schema version field today.** See [Capabilities contract](#capabilities-contract-today). |
| `/victor/companion` | GET only (other methods 405) | Bearer when auth is required | Read-only phone report, `"schema": 1`. See [Companion report](#companion-report). |
| Other `/victor/*` | any | Bearer when auth is required | `404` unknown victor endpoint |
| `/v1/models`, `/v1/chat/completions`, `/v1/completions` | GET / POST as in OpenAI | Bearer when auth is required | OpenAI-compatible, proxied to llama-server. SSE streaming is supported. |
| `/v1/embeddings` | POST | Bearer when auth is required | Listed in the capabilities `endpoints` array as "if model supports it" |
| `/api/version`, `/api/tags`, `/api/generate`, `/api/chat` | GET / POST | Bearer when auth is required | Ollama-compatible subset. `/api/version` returns `victor-compat (not Ollama)`, not a real Ollama version. `tools` are dropped. No pull, push, create, or delete. |
| `/completion`, `/props`, `/tokenize`, `/detokenize` | as in llama.cpp | Bearer when auth is required | Native llama.cpp API, proxied |
| `/slots`, `/health`, `/metrics`, and other `/v1/*` prefixes allowed by the proxy | as upstream | Bearer when auth is required | Allowed through the gateway's prefix list to llama-server. Prefer the documented OpenAI, Ollama, or victor routes above. |

Clients may use the same base address and the same Bearer key with OpenAI, Ollama, or llama.cpp protocols. Typical remote setup, from [remote-access.md](remote-access.md):

- Base URL: `http(s)://<host>:8080/v1` (default port 8080)
- Header: `Authorization: Bearer <key>`
- Model name: `victor-local` (accepted; llama.cpp ignores it)

Prefer TLS or a VPN for anything beyond a trusted private link. The gateway certificate is self-signed unless the owner puts a reverse proxy in front. Pin the SHA-256 fingerprint; do not ignore certificate errors.

## Capabilities contract (today)

`GET /victor/capabilities` exists and returns JSON from `capabilities()` in `gateway.py`. [Certain] It is **not versioned** yet. A schema field is planned in [FLEET_INTEGRATION_PLAN.md](FLEET_INTEGRATION_PLAN.md) item 5; this file should be updated when that lands.

Current body (schema is implicit and should be treated as "pre-v1"):

| Field | Meaning |
|---|---|
| `openai_compatible` | Always `true` |
| `endpoints` | OpenAI-style paths the gateway documents |
| `ollama_compat` | Ollama-subset paths |
| `llamacpp_native` | Native llama.cpp paths |
| `streaming` | `"unverified"`, `"verified"`, or `"failed"` after `victor-capcheck` for the *current* model file |
| `tool_calling` | `"unverified"`, `"verified"`, or `"failed-or-unsupported"` for that same file |
| `tool_execution_on_pc` | Always `false` |
| `note` | Human-readable warning about tools and capcheck |
| `verified_at`, `verified_server` | Present only when a matching capcheck record exists |

A previous capcheck for a different model file is ignored and a note is appended. Clients must not treat `"unverified"` as supported. Intended stable policy once versioned: add a `schema` field (`1` for this shape, matching the companion report); additive optional keys are allowed; breaking changes become schema 2; unknown keys are ignored.

## Companion report

`GET /victor/companion` is implemented in [`companion.py`](../airootfs/usr/local/lib/victorlinux/companion.py). [Certain] `SCHEMA = 1`. It contains no keys, no file paths, and no chat content. Measured hardware comes from `/run` for this boot only.

Top-level keys: `schema`, `pc` (`id`, `name`), `generated_at`, `hardware` (RAM, GPUs, backend, memory pool/budget, default threads), `runtime` (whether a model is running, model *file name* only, backend, context, GPU layers, generation check), `models` (up to eight local models by name/fit/ctx/verified/size), `transport` (`tls`, `tls_fingerprint_sha256`), `privacy` (a fixed notice).

The phone should pin `pc.id` on first success and treat a different id at the same address as a different PC.

## Fleet coordinator endpoints

Implemented under `fleet/`, documented in [fleet/docs/FLEET.md](../fleet/docs/FLEET.md). This is a separate service from the inference gateway, with its own TLS certificate and pairing secret. Protocol major version is 1. [Certain]

| Path | Auth | Purpose |
|---|---|---|
| `POST /v1/enroll` | Single-use pairing secret + device-key signature | Create the device credential |
| `POST /v1/exchange` | Device bearer credential | Send envelopes, ack, long-poll for work |
| `PUT /v1/artifacts/<job>/<attempt>/<name>?sha256=` | Device credential, current attempt only | Upload a result |
| `GET /v1/artifacts/<id>` | Device credential, with access rules | Download an artifact |
| `POST /v1/rotate` | Device credential | Rotate the credential; the old one dies at once |

Do not confuse these `/v1/*` routes with the gateway's OpenAI `/v1/*` routes. They are different listeners and different credentials. `victor-fleet pair` is also not the planned `victorpc://pair` inference QR.

## Client matrix

| Client | Endpoints it is documented to use | Auth | Default state | Confidence |
|---|---|---|---|---|
| **DroidCommand-AI** | Inference: OpenAI-compatible `/v1` on the gateway ([remote-access.md](remote-access.md)). Fleet worker: coordinator `/v1/enroll`, `/v1/exchange`, artifacts, `/v1/rotate` when the owner has enabled the coordinator and paired the phone ([fleet/docs/FLEET.md](../fleet/docs/FLEET.md)). Desktop hand-off to rustdeskV is a `rustdesk://<peer-id>` URI on the phone, not a VictorLinux HTTP route ([MANUAL_FLEET.md](MANUAL_FLEET.md)). | Inference: Bearer API key, configured manually today (no pairing protocol in this repo). Fleet: single-use pairing secret, then device credential. | Gateway remote off; fleet coordinator off. The PC does not modify this app. | Server routes [Certain]. How the current Android app is wired, which screens call which URL, and whether Ollama paths are used: [Guessing]. |
| **VictorSuite** | Named in the README as an inference client of the optional API. No VictorSuite code lives in this repo. A planned `victorpc://pair` (VICTORPC_PAIRING v1) QR would carry the gateway URL and TLS fingerprint; that QR is not in this tree yet. | Bearer API key once remote access is on, same as other inference clients. | Remote access off; no pairing listener. | That the README lists it as a client: [Certain]. Exact paths, libraries, or screens the app uses: [Guessing]. |
| **Victorious Browser** | Android companion: `GET /victor/companion` for the hardware/model report, then inference against the same gateway ([companion-api.md](companion-api.md)). Tools → VictorLinux PC → Add address, key, and certificate fingerprint, then Check PC. It must not use the admin UI or its session token. | Bearer API key. Pin the self-signed TLS fingerprint and the `pc.id`. | Remote access off; the owner adds the PC by hand. | Server contract and the flow documented here: [Certain]. Anything about the Android app not stated in this repo: [Guessing]. |
| **Victorious Flash** | Does **not** call the inference gateway or the fleet coordinator. It verifies and writes (or gadget-exposes) an ISO, using a catalog entry or a local file plus SHA-256 ([fleet-integration.md](fleet-integration.md)). | Not a gateway client. Catalog entries need real HTTPS ISO and checksum URLs once a release exists. | No VictorLinux catalog entry until an ISO and checksum have been published (and Don has approved publishing). Until then, use the local image picker and a manual SHA-256. | Catalog contract as documented here: [Certain]. Flash UI or Android internals beyond these docs: [Guessing]. |
| **RustDeskV** | Not an inference or fleet-HTTP client. Phone-side hand-off uses `rustdesk://<peer-id>` after a version and signing-certificate check. No password, key, or server setting is put in that link ([MANUAL_FLEET.md](MANUAL_FLEET.md)). | RustDesk's own settings, kept in rustdeskV. | No RustDesk pack is shipped or started by VictorLinux today. The Android hand-off UI is documented as not built yet. | What this repo documents: [Certain]. Live rustdeskV app behavior and ports: [Guessing]. |

## What clients must not do

- Do not send the Control Center session token to a phone, or put it in a URL or QR.
- Do not expect tool execution on the PC. The gateway drops Ollama `tools` and does not start llama-server with `--tools`.
- Do not treat `/api/version` as proof that Ollama is installed.
- Do not use `localhost` on a phone and assume it is the PC.
- Do not bypass TLS errors or skip fingerprint comparison on a self-signed gateway certificate.
