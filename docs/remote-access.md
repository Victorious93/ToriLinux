# PC inference server and remote access

The Control Center runs `llama-server` bound to `127.0.0.1` on a private port with a private key, and fronts it with
the **Tori gateway** (`gateway.py`):

* default: `http://127.0.0.1:8080/` - local only, no key needed
* remote: set a LAN/VPN bind address in *Server & Access*, create an API key; the gateway then **requires**
  `Authorization: Bearer <key>` for everything except `/tori/health`
* the key is shown once and stored only as a SHA-256 hash; rotating it revokes the old one; "Revoke all remote
  access" deletes it and returns to loopback

## Endpoints

| Path | Notes |
|---|---|
| `GET /tori/health` | `200 {"status":"ok"}` or `503 model_not_running`; no secrets |
| `GET /tori/capabilities` | Honest capability report (below) |
| `POST /v1/chat/completions`, `/v1/completions`, `GET /v1/models`, ... | OpenAI-compatible, proxied from llama.cpp, SSE streaming supported |

Only these inference paths are exposed. The gateway has no shell/file/tool-execution endpoints, and llama-server is
started **without** `--tools` and with the MCP proxy disabled: remote inference never grants tool execution on the PC.

## Capabilities are measured, not claimed

`/tori/capabilities` reports `streaming` and `tool_calling` as `unverified` until **`tori-capcheck`** (button in the
UI) has run real requests against the *currently loaded model file*: streaming must yield multiple SSE events ending
in `[DONE]`; tool calling must return a well-formed `tool_calls` response with valid JSON arguments. The result is
stored per model (path, size, mtime); loading a different model resets it to `unverified`.

## Encrypted transport

Plain HTTP over a LAN is readable by anyone on the network. Prefer:

1. **VPN (recommended):** Tailscale or self-hosted Headscale. Install the client yourself (it is not bundled and no
   VPN is required for local use), join the tailnet, then bind the gateway to the tailnet address (`tailscale ip -4`)
   and use that address from the phone. Traffic is end-to-end encrypted by WireGuard; keep the API key anyway.
2. **HTTPS from the gateway:** generate a self-signed certificate (Control Center button or
   `tori-tls-cert <hostname-or-ip>`), enable HTTPS, and import `cert.pem` into the client's trust store (or pin it).
3. A reverse proxy with a real certificate in front of the gateway.

## Connection details for clients (e.g. DroidCommand-AI)

* Base URL: `http(s)://<pc-address>:<port>/v1` (default port 8080)
* Auth header: `Authorization: Bearer <API key>`
* Health check: `GET /tori/health`; capabilities: `GET /tori/capabilities`
* Model name: `tori-local` (llama.cpp ignores it, but it is accepted)

This repository does not modify DroidCommand-AI and defines no pairing protocol; configure the URL and key manually.
