# OpenAI-compatible endpoint (for DroidCommand-AI)

Served by `llama-server` from llama.cpp (pinned commit). `victor-ctl api connection` prints base URL, auth mode, TLS certificate SHA-256 and (with `--reveal-key`) the API key.

| Setting | Default | Remote |
|---|---|---|
| Bind | `127.0.0.1:8080` | explicit address you choose (e.g. LAN or VPN IP) — never `0.0.0.0` automatically |
| Auth | none (loopback only) | **mandatory** Bearer key (`secrets/api.key`, 256-bit random per drive) |
| Transport | HTTP on loopback | **HTTPS** with a per-drive self-signed P-256 certificate (pin its SHA-256), or plain HTTP only inside a private VPN with an explicit acknowledgement (`remote.vpn_plain_http_ack`) |
| Firewall | nftables default-drop inbound | the port is opened only while remote mode is enabled; *Revoke* closes it, disables SSH, removes keys |

Endpoints (llama.cpp): `GET /health`, `GET /v1/models`, `POST /v1/chat/completions` (streaming via SSE), `GET /props`. Capabilities are **measured**, not assumed: `victor-ctl api selftest [--tools]` performs real
requests (health, 401-without-key when remote, models, chat, SSE stream, optional tool call) and stores the result keyed by model file + chat-template hash + llama.cpp build. `victor-ctl api capabilities`
shows it; `tool_calling` stays `untested`/`failed` unless that exact combination passed (requires the *jinja* option). The tiny test model used here correctly fails the tool test.

**Tool calls never grant execution.** The server is started without `--tools`, `--webui-mcp-proxy`, `--mcp-servers` (these flags are refused by the launcher); nothing in VictorLinux executes a model-emitted tool call. Any
tool-running logic belongs to the client, which must do its own authorization.

## DroidCommand-AI assumptions
DroidCommand-AI is not modified and no pairing protocol is invented. Assumed: it can talk to an OpenAI-compatible HTTPS base URL with a Bearer key. For the self-signed certificate the client must trust/pin it
(SHA-256 shown); if it cannot, use the VPN path. I could not test against DroidCommand-AI itself.

## Optional Tailscale / Headscale
Not included in the image (not in Debian main archives). If you choose to install the Tailscale client yourself (needs network; follow Tailscale's own instructions; for Headscale use the client's `--login-server`
option), set *Listen address* to the machine's tailnet IP. Traffic is then WireGuard-encrypted; keep TLS on anyway unless you accept `vpn_plain_http_ack`. Revoke via Control Center → Remote access.
A VPN is not needed for local use.
