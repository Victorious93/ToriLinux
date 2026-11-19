# VictorLinux security-tool execution backend (SET / Metasploit)

Status as of 2026-11-19: **real, running, verified against actually-installed tools in the
environment that built it** (not mocks, not fixtures-only). This document is both the user
manual and the canonical protocol reference `docs/security-backend.md` that
`secprotocol.py` and DroidCommand AI's Kotlin client are both tested against.

## 1. What runs where

| Component | Runs in | Starts when |
|---|---|---|
| `gateway.py` | VictorLinux, inference only | Opt-in, spawned by Control Center |
| `control_center.py` | VictorLinux, local admin UI | `victor-control-center` |
| **`secbackend.py`** | VictorLinux, **tool execution only** | Opt-in, `victor-secbackend serve` |
| DroidCommand AI (phone-local) | Android, inside VictorSuite's Kali proot | `com.termux.RUN_COMMAND` |
| DroidCommand AI (Linux-backend client) | Android | talks to `secbackend.py` over HTTP |

`secbackend.py` is a **separate process and a separate authenticated service** from both
`gateway.py` and `control_center.py` - never the same port, never the same API-key
namespace. `gateway.py`'s own docstring says "there is no tool execution ... on this port";
this backend is that missing piece, kept isolated on purpose. Nothing starts it at boot. It
binds to `127.0.0.1` by default and **always** requires an API key, even on loopback
(stricter than the inference gateway, because running a real exploit/SET module is a
materially different risk than serving a chat completion).

Two independent execution paths exist and are both real:

- **Linux backend** (this document): Metasploit over its documented MessagePack RPC
  (`msfrpcd`), SET over a real pexpect-driven terminal session. Needs a VictorLinux
  install (or any Ubuntu dev host - see §2) reachable from the phone, or used standalone
  via the CLI client (§5) without a phone at all.
- **Phone-local** (VictorSuite): unchanged, still routes through
  `com.termux.RUN_COMMAND` into VictorSuite's Kali proot. Works with no Linux PC. See
  DroidCommand AI's `docs/AUDIT_2026-09-05.md` for its own status.

Either works without the other. DroidCommand AI's Settings lets the owner choose which
backend a job runs on; it never silently switches mid-task (see that repo's own docs for
the client side).

## 2. Installing the Linux backend

### 2.1 Metasploit Framework

This backend talks to a real `msfrpcd`, Metasploit's own documented RPC daemon. It does
**not** vendor or fork Metasploit - install it through one of Metasploit's own official
channels and point this backend at it:

- **Kali/Debian/Ubuntu with the Rapid7 apt repo configured**: `apt install metasploit-framework`,
  then run `msfrpcd` directly from the installed `bin/`.
- **From source** (what this document's own verification used, since no apt package exists
  for bare Ubuntu 24.04 without Rapid7's repo configured): clone
  `https://github.com/rapid7/metasploit-framework` at a pinned release tag, `bundle install`
  (needs `build-essential libpq-dev libpcap-dev libssl-dev zlib1g-dev` - see §7 for the
  exact package list used), then run `bundle exec ruby msfrpcd ...` from that checkout.

Start it bound to loopback only, with a strong, unique password - it is never exposed
beyond `secbackend.py` itself:

```sh
msfrpcd -U <rpc-user> -P '<strong-password>' -a 127.0.0.1 -p 55553 -S
```

(`-S` disables SSL on the RPC listener itself; that is fine because it only has to be
reachable from `secbackend.py` on the same host - never expose port 55553 beyond
loopback.) Then tell the backend about it:

```sh
python3 -c "
import victorcommon as vc
sec = vc.load_secrets()
sec['security_msf_rpc_user'] = '<rpc-user>'
sec['security_msf_rpc_pass'] = '<strong-password>'
vc.save_secrets(sec)
"
```

### 2.2 Social-Engineer Toolkit (SET)

```sh
git clone https://github.com/trustedsec/social-engineer-toolkit
cd social-engineer-toolkit
pip install -r requirements.txt
python3 setup.py           # installs the real 'setoolkit' launcher to /usr/local/bin
```

SET must run as root (its own, upstream requirement - this backend does not add that).
`secbackend.py serve` therefore needs to run as a user that can either run `setoolkit`
directly as root, or via a narrowly-scoped sudo rule limited to that one binary - choose
whichever fits your own host's privilege model; this backend does not ship a sudo policy
of its own (see §6, "run with the minimum privileges required").

### 2.3 The backend itself

No separate install step: `secbackend.py`, `secmsf.py`, `secset.py`, `secjobs.py` and
`secprotocol.py` ship in every VictorLinux image/checkout under
`/usr/local/lib/victorlinux/`, next to `gateway.py` and `control_center.py`. Only
`pexpect` (for SET) and `msgpack` (for Metasploit RPC) are extra Python dependencies -
install them with `pip install pexpect msgpack` if the image does not already carry them.

## 3. First run

```sh
victor-secbackend create-key "my phone"      # -> prints a victor-... key ONCE
victor-secbackend serve                       # loopback:18099 by default
```

From another terminal on the same host (or from DroidCommand AI once paired - see that
repo's docs for the phone side):

```sh
export VICTOR_SECURITY_KEY=victor-...
victor-secbackend capabilities
```

A healthy, fully-installed response looks like:

```json
{
  "protocol": {"major": 1, "minor": 0},
  "operations": {
    "metasploit.module": {"available": true, "reason": null, "version": {"version": "6.5.12...", ...}},
    "setoolkit.attack": {"available": true, "reason": null, "supported_vectors": ["QRCODE_GENERATOR"], ...}
  }
}
```

If Metasploit RPC credentials are not configured yet, `metasploit.module.available` is
`false` with `reason: "not configured"` - a distinct, actionable message from "msfrpcd is
unreachable" (wrong host/port/credentials) or "setoolkit is not on PATH" (SET missing
entirely). These are deliberately different facts; see §8.

## 4. Running a job and reading results

```sh
victor-secbackend submit setoolkit.attack \
  '{"attack_vector":"QRCODE_GENERATOR","target_host":"https://example.com"}' \
  --idempotency-key my-first-qr
# -> {"job_id": "job_...", "state": "running", ...}

victor-secbackend status job_...
# -> {"state": "completed", "result": {"summary": "QR code generated", "artifact_path": "/root/.set/reports/qrcode_attack.png"}, ...}
```

```sh
victor-secbackend submit metasploit.module \
  '{"module_type":"auxiliary","module_path":"scanner/portscan/tcp","target_host":"127.0.0.1","options":{"PORTS":"1-100"}}' \
  --idempotency-key my-first-scan
victor-secbackend status job_...
```

`status --since <cursor>` resumes exactly where you left off (the response's
`next_cursor` is the value to pass next time) - no duplicated or dropped output lines
across reconnects. `cancel job_...` requests cancellation; poll `status` again to see
whether it reached `cancelled` (confirmed stopped) or the job had already finished
(`completed`/`failed` - cancelling a finished job is a no-op, not an error).

Submitting the **same** `--idempotency-key` with the **same** parameters again returns the
original job untouched (never runs twice); the same key with **different** parameters is
rejected with `idempotency_conflict` rather than silently running the new parameters.

## 5. SET coverage: what is supported today and why

Only **QRCode Generator Attack Vector** runs unattended through this backend. Every other
vector is reported with a specific reason via `capabilities`, not silently blocked:

| Vector | Status | Why |
|---|---|---|
| `QRCODE_GENERATOR` | **Supported, verified live** | Single prompt (a URL), writes a deterministic PNG, zero network/mail side effects. See §7 for the exact verified transcript. |
| `SMS_SPOOFING` | Unsupported - **removed upstream** | Verified against `trustedsec/social-engineer-toolkit@5e0fbb5` (installed version string "8.1.3 Maverick"): the main attack-vector menu now has exactly 10 entries ending in "Third Party Modules" - there is no SMS entry at all. An earlier guessed digit mapping in DroidCommand AI's own `SetMenuPath` assumed digit 10 was SMS Spoofing; it is not, in this version. |
| `SPEAR_PHISHING` | Unsupported - not yet validated | Submenu branches too far (template/attachment/SMTP vs sendmail/recipient list) for one digit sequence; can send real email. |
| `WEBSITE_ATTACK` | Unsupported - not yet validated | Starts a long-lived local listener; sub-menu not yet covered. |
| `INFECTIOUS_MEDIA_GENERATOR` | Unsupported - not yet validated | Payload/listener sub-menu not yet covered. |
| `MASS_MAILER` | Unsupported - deliberately excluded | Sends real email as its only effect; not automated unattended regardless of mapping accuracy. |
| Powershell Attack Vectors | Not wired into the protocol at all yet | Requires a configured Metasploit path (same gate QRCode hit) and generates a reverse-shell payload file; the exact prompt sequence (submenu digit, IP, port) was read from source but **not yet run live** - a concrete next step, not a guess shipped as a feature. |

Everything else in this table is a real finding from reading `social-engineer-toolkit`'s
actual installed source (not documentation, not memory) on 2026-11-19 - see §7 for the
exact commit and version.

## 6. Access control and transport

- Separate secrets namespace: `security_api_keys`/`security_api_key_meta` in
  `secrets.json`, distinct from `gateway.py`'s `api_keys`. A chat/model API key grants
  nothing here, and a security key grants nothing on the inference gateway.
- Loopback by default; reaching this from a LAN/VPN needs an explicit `--bind` plus your
  own reverse-proxy/VPN trust boundary - this backend does not add TLS of its own (same
  posture `gateway.py` documents for its own remote mode). Never put raw Metasploit RPC
  (port 55553 by default) on anything but loopback.
- Every job is owned by the API key that submitted it; `status`/`cancel`/output access by
  a different key gets `403 not_job_owner`, never silent cross-tenant access.
- `secbackend.py` itself never runs as root. SET's own root requirement (§2.2) is
  Metasploit/SET's requirement, not this backend's - scope it as narrowly as your host
  allows; do not run the whole backend as root to work around it.

## 7. Verification record (2026-11-19)

Exact environment: Ubuntu 24.04 container, Ruby 3.3.6, Python 3.13.18, root. Packages
installed for the build: `build-essential libpq-dev postgresql postgresql-contrib
libffi-dev libyaml-dev pkg-config libpcap-dev`.

**Metasploit**: `git clone --depth 1 https://github.com/rapid7/metasploit-framework`,
`bundle install --jobs 4` (249 gems, ~15 min), then:

```
bundle exec ruby msfrpcd -U victor -P <redacted> -a 127.0.0.1 -p 55553 -S
```

Verified over real MessagePack RPC: `auth.login` -> real token; `core.version` -> real
`6.5.12-dev-a5d29613`; `module.exploits` -> 2726 real module names; a real
`auxiliary/scanner/portscan/tcp` run via `console.create`/`write`/`read`/`destroy` against
`127.0.0.1` returned real captured output (`"[*] Auxiliary module execution completed"`);
cancellation mid-scan (`\x03` into the console) stopped a real 65000-port scan in under two
seconds; `job.info` on an already-finished job's id returned a structured
`{"error": true, "error_message": "Invalid Job", "error_code": 500}` - note it is HTTP 500
carrying a valid msgpack body, not an HTTP failure to treat as a bare transport error (this
was an actual bug caught and fixed in `secmsf.py` during this verification - see its own
git history / the `tests/test_secmsf.py::test_live_job_info_...` test for the regression
check).

**SET**: `git clone https://github.com/trustedsec/social-engineer-toolkit` @
`5e0fbb50ca8b156ac93532a0a4d138900c6e0827`, `pip install -r requirements.txt`,
`python3 setup.py` (real `/usr/local/bin/setoolkit` launcher). Running version string:
"8.1.3 'Maverick'". Found live: the QRCode Generator Attack Vector is gated behind a
configured Metasploit path even though it does not itself call Metasploit (a real
installed-version quirk, not a design choice of this backend) - worked around by exposing
`msfconsole` on `PATH` pointing at the same checkout. Full verified pexpect transcript
(abbreviated, ANSI stripped):

```
Select from the menu:
   1) Social-Engineering Attacks
   ...
set> 1
   1) Spear-Phishing Attack Vectors
   2) Website Attack Vectors
   3) Infectious Media Generator
   4) Create a Payload and Listener
   5) Mass Mailer Attack
   6) Arduino-Based Attack Vector
   7) Wireless Access Point Attack Vector
   8) QRCode Generator Attack Vector
   9) Powershell Attack Vectors
  10) Third Party Modules
set> 8
Enter the URL you want the QRCode to go to (99 to exit): http://example.invalid/dca-verification
[*] QRCode has been generated under /root/.set/reports/qrcode_attack.png
      Press <return> to continue
```

The generated PNG was decoded with `pyzbar` and found to encode exactly the requested URL.
No orphan `setoolkit` process remained after the adapter's own termination sequence
(SIGTERM then SIGKILL fallback). Repeated against a different URL via the full
`secbackend.py` HTTP service (not the bare module) with identical results.

**Full service, end to end** (the exact commands any operator can re-run):
submit/status/list/cancel were run against the real, running `secbackend.py serve` on
`127.0.0.1:18099`, with a real created API key, for: a completed Metasploit scan, a
completed SET QR-code job, an idempotent retry (same job returned, not re-run), an
idempotency conflict (409), a cancelled long-running scan (confirmed stopped, real output
captured up to the cancel point), a wrong API key (401), an unknown job id (404), and a
hard-killed-and-restarted backend process whose in-flight job came back as `interrupted`
with an honest "outcome unknown" message rather than being silently re-run or reported
successful.

**Not verified**: any of this running inside VictorLinux's actual ISO/live environment
(systemd, the real `victor-priv` privilege-drop path, the real persistent-storage
mountpoints) rather than this standalone container; any SET vector besides QRCode; a real
`exploit`-type (as opposed to `auxiliary`) Metasploit module against a real vulnerable
target (the portscan run above is `auxiliary`, chosen specifically because it is safe to
run against localhost with zero side effects); DroidCommand AI's phone-side client against
a real running instance of this backend (see that repo's own report for its side of the
verification, which used a fake HTTP server, not this real one, for its JVM tests - a real
phone-to-this-backend run is a concrete next step, not yet done).

## 8. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `metasploit.module.available: false, reason: "not configured"` | No RPC credentials saved | §2.1's `save_secrets` step |
| `reason` starts with `auth_failed:` | Wrong RPC user/password | Re-check `-U`/`-P` given to `msfrpcd` match what was saved |
| `reason` starts with `transport_error:` | `msfrpcd` not running, or wrong host/port | Confirm `msfrpcd` is up on the host/port `secbackend.py --msf-host/--msf-port` was given |
| `setoolkit.attack.available: false` | `setoolkit`/`pexpect` missing | §2.2 |
| Job fails with `capability_missing: ... Metasploit path` | SET itself gates a vector behind its own `METASPLOIT_PATH` config | Put `msfconsole` on PATH or set `/etc/setoolkit/set.config`'s `METASPLOIT_PATH` |
| Job stuck `running` past its timeout | Real tool hung on an unexpected prompt | `cancel` it; file the exact transcript so the adapter can be fixed for that case |
| A job shows `interrupted` after a restart | The backend process died or was restarted mid-job | Its real tool-side outcome is genuinely unknown - do not assume success or failure; re-submit as a new job if appropriate |
| `401 unauthorized` | Missing/wrong/revoked key, or using a gateway key here | Security keys and inference keys are different namespaces (§6) - create one with `create-key` |

## 9. Uninstall / rollback

Stop the running `victor-secbackend serve` process (Ctrl-C or kill the PID; it is not a
systemd-managed service, so there is nothing to disable). Job history and output live
under `<persistent-storage>/security/jobs/*.json` - delete that directory to clear it, or
leave it; it is never touched by anything else in VictorLinux. Revoking a security API key
does not delete its past jobs' records, only its ability to submit new ones or read
others. Uninstalling Metasploit/SET themselves is whatever their own upstream projects
document; this backend has no uninstaller of its own to run first.
