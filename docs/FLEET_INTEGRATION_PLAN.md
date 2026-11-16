# Fleet integration plan

This is a planning document only. Nothing here is implemented, built, or booted by this change.

Reviewed against `main` at `01fdd30` (merge of [PR #8](https://github.com/Victorious93/VictorLinux/pull/8), 12:46 AM ET on Nov 15, 2026). [Certain] There were no open pull requests on that review. Local work mentioned below lives on other branches and is not in this PR.

## Core rule

VictorLinux stays **standalone in both directions**.

- The PC works fully offline and alone: local chat, models, storage, and the Control Center do not need any phone, fleet service, discovery, or published ISO.
- Every fleet, remote, discovery, and companion-pack feature **defaults OFF**. It is not started at boot, does not advertise the PC, and does not open a non-loopback port until the owner turns it on.
- Turning a fleet feature off, or never turning it on, leaves local use unchanged.
- Phones and other apps remain useful without VictorLinux; this plan does not make those apps depend on a live PC.

Existing code already follows this for remote access and the fleet coordinator: `remote.enabled` defaults to `False` and binds `127.0.0.1:8080` ([`victorcommon.py`](../airootfs/usr/local/lib/victorlinux/victorcommon.py)); the coordinator is not started at boot and stays local-only until `victor-fleet config --enable-network` ([`fleet/docs/FLEET.md`](../fleet/docs/FLEET.md)). [Certain]

## Status labels used below

- **planned** — documented here, no feature branch yet
- **in progress on branch X** — local work exists; not necessarily a PR
- **needs Don's approval** — must not ship, publish, or land feature code until Don says yes

---

## 1. Release ISO with SHA-256 sums

**Priority / size:** High / L

**Status:** planned; **publishing needs Don's approval**. This item is plan-only. Do not publish a GitHub release or add a Flash catalog URL from this document.

**Goal:** Build a VictorLinux release ISO from current `main` (or a release tag), boot-test it, and only then publish it with SHA-256 sums so [Victorious Flash](https://github.com/Victorious93/victorious-flash)'s catalog and the fleet manifest can point at a real artifact.

**Default state:** No release is published today, and no ISO of this revision has been built or booted as a product image. [Certain] That is already stated in the README. Earlier ISO evidence in [docs/test-report.md](test-report.md) is historical and does not cover this revision. Publishing remains off until Don explicitly says yes.

**Design:**

1. Build the ISO with the existing image workflows and [docs/build.md](build.md). Record the exact commit, edition (Arch, Debian, or both), variant, and build log.
2. Compute SHA-256 of the ISO and publish the sum next to the file (a `SHA256SUMS` file, or the same hash in release notes and the Flash catalog). Do not invent a new Flash schema field; [docs/fleet-integration.md](fleet-integration.md) already describes the existing `kind: linux-iso` / `image_format: iso` contract.
3. Boot-test in this order: QEMU UEFI, then QEMU legacy BIOS, then real hardware (AMD/Intel/NVIDIA as available). A CI artifact or a rendered profile is not a boot test.
4. After a successful boot, record identity, boot path, free memory, and that inference still stays on the PC. Exercise a physical-drive flash and, when a phone is available, a phone-gadget boot of the *same* ISO hash.
5. Only after Don approves publishing: attach the ISO and checksums to a GitHub release, then add a Flash catalog entry and any fleet manifest pointer that uses those real HTTPS URLs. Until then, Flash keeps using its local image picker plus a manually supplied SHA-256. Do not add a placeholder download URL.

**Threat note:** A published ISO or checksum that does not match the bits people flash would silently distribute the wrong image. A catalog entry added before the file exists would make Flash less reliable. Mitigation: checksum the built file, never a placeholder; do not publish or catalog until Don says yes; Flash and fleet consumers must fail closed on a missing or mismatched hash.

**Tests to add:** Reproducible checksum of a built ISO (or a fixture-sized stand-in in CI if the full image is too large for every run); QEMU UEFI and BIOS boot of that image; a documented real-hardware checklist (not automated here) covering boot, persistence, and that a wrong checksum is refused by Flash's existing verify path. Do not treat a skipped QEMU test as a pass.

**Dependencies:** Existing build and USB tooling. Flash catalog and the fleet manifest depend on this item; they should not gain a VictorLinux download entry first.

---

## 2. Opt-in "Pair phone" QR (`victorpc://pair`)

**Priority / size:** High / M

**Status:** in progress on local branch `feat/gateway-keys-qr` (not yet a PR). This planning PR does not include that code.

**Goal:** Let the owner pair a phone to the inference gateway by scanning a QR that emits `victorpc://pair`, following VictorSuite's VICTORPC_PAIRING v1, including the gateway URL and TLS fingerprint. The long-lived API key is shown once on the PC and is stored only as a SHA-256 hash (already the case in [`gateway.py`](../airootfs/usr/local/lib/victorlinux/gateway.py)). [Certain]

**Default state:** Off. No QR, no pairing listener, and no pairing URI is emitted until the owner opens Control Center → Remote access (or an equivalent "Pair phone" action) and opts in. Today the repo has no pairing protocol: URL and key are copied by hand ([docs/remote-access.md](remote-access.md)). [Certain]

**Design:**

- The owner enables the gateway (non-loopback or remote access), optionally enables TLS, and starts pairing. The PC shows a QR and a manual fallback.
- The QR uses the `victorpc://pair` scheme with `v=1`. [Certain] The frozen VictorSuite v1 QR is SSH-oriented (profile id, SSH user and port, host keys, pairing port, single-use token `t`, expiry `x`). v1 says new optional keys are allowed and unknown keys must be ignored.
- [Likely] VictorLinux should reuse that scheme and version, and add optional fields for the gateway URL and the TLS certificate SHA-256 fingerprint (`transport.tls_fingerprint_sha256` already exists on `GET /victor/companion`). [Guessing] The exact optional key names — agree them with the VictorSuite spec in a short contract note before any feature PR, rather than inventing a private dialect.
- Never put the long-lived Bearer API key in the QR. If the QR carries a secret, it must be a single-use, short-lived token (v1 already has `t` and expiry `x`; the fleet coordinator's `victor-fleet pair` secret is the same idea, and is a separate protocol). The long-lived key is printed once on the PC screen, then only its hash is kept.
- TLS fingerprint is in the QR (and on screen) so the phone can pin it. Self-signed certs are expected; clients must not ignore certificate errors.
- Pairing is not the fleet coordinator's `POST /v1/enroll` flow. That flow stays for DroidCommand workers. This QR is for inference clients (VictorSuite, Victorious Browser, and any other app that speaks VICTORPC_PAIRING v1).
- Admin Control Center on `127.0.0.1:8787` stays local. The phone never receives that session token.

**Threat note:** Putting a long-lived key in a QR (or a screenshot of one) would leak gateway access to anyone who sees the image. A QR without a TLS fingerprint invites a LAN impersonator. A pairing listener left running would keep accepting enrollments. Mitigation: opt-in and short-lived; single-use token only; no long-lived key in the QR; show the fingerprint on both screens; expire the token (v1 default is ten minutes) and close the listener; never log the token or the key.

**Tests to add:** QR payload parses as `victorpc://pair` with `v=1`, gateway URL, and TLS fingerprint; long-lived keys never appear in the URI; a token in the QR is single-use and expires; a second scan or a replay fails; TLS fingerprint matches `victor-tls-cert` / `/victor/companion`; pairing stays off unless the owner starts it; no token in logs.

**Dependencies:** Existing gateway TLS and "show key once, store hash" behavior. Coordinate field names with VictorSuite's VICTORPC_PAIRING v1. Item 3 can make pairing easier but is not required for a displayed QR. Item 5 is independent.

---

## 3. Opt-in discovery toggle

**Priority / size:** Med / M

**Status:** planned

**Goal:** An owner-controlled way for a phone on the same LAN to find the PC, either by advertising `_victorlinux._tcp` via mDNS or by opening a one-shot pairing window. The PC is not discoverable unless the owner turns this on.

**Default state:** Off. No mDNS service is published, and no pairing window is open, unless the owner enables the toggle (or starts a one-shot window from "Pair phone"). [Certain] Nothing in the current tree advertises `_victorlinux._tcp`.

**Design:**

- Prefer a **one-shot pairing window** (minutes, same lifetime as a pairing token) over a continuous advertisement. Continuous mDNS is a separate, still-off-by-default toggle.
- If mDNS is used, the service type is `_victorlinux._tcp`. Advertise only what a phone needs to start pinning: display name, address, gateway port, and TLS fingerprint. Never advertise API keys, pairing tokens, or the Control Center port.
- Disable advertisement when the toggle is off, when the window expires, or when remote access is turned off. A reboot must not restart discovery unless the owner has clearly saved it as on — and even then, the safer default is "off until the next explicit Pair phone." Prefer the safer default if there is any doubt.
- Discovery is a hint. Identity remains the TLS fingerprint and, after pairing, the companion `pc.id`. A discovered host with the wrong fingerprint is rejected.

**Threat note:** Continuous mDNS tells the whole LAN that a VictorLinux PC is present and where it listens. A malicious advertisement could point a phone at the wrong host. Mitigation: off by default; prefer a short window; never put secrets in the record; phones must pin TLS (and later `pc.id`) and must not trust discovery alone; no wildcard bind as a side effect of turning discovery on.

**Tests to add:** Default install does not publish `_victorlinux._tcp`; enabling the toggle or window publishes the expected service without keys; expiry or disable removes it; a mismatched TLS fingerprint is rejected; remote access off stops advertisement.

**Dependencies:** Makes the most sense next to item 2. Does not require a published ISO.

---

## 4. Stale ToriLinux links, and a client matrix

**Priority / size:** Med / S

**Status:** The stale `Victorious93/ToriLinux` repo pointer in [docs/fleet-integration.md](fleet-integration.md) is **in progress on local branch `chore/cleanup-urls-wheels`** (pending PR, not in this change). [docs/clients.md](clients.md) is added in this PR. Historical mentions of the old name (for example the Nov 14 testing report, and attribution to [sasha0552/ToriLinux](https://github.com/sasha0552/ToriLinux) in LICENSE and NOTICE) should stay where they are history or required credit.

**Goal:** Point live "where the code lives" docs at `Victorious93/VictorLinux`, and give client authors one matrix of apps, endpoints, auth, and default state, based on what this repo actually exposes.

**Default state:** Not a runtime feature. Documentation only.

**Design:**

- On the cleanup branch: replace the ownership-table entry `Victorious93/ToriLinux` in `docs/fleet-integration.md` with `Victorious93/VictorLinux`. Keep original ToriLinux attribution.
- In this PR: add [docs/clients.md](clients.md) describing the gateway (`/v1/*`, `/victor/health`, `/victor/capabilities`, `/victor/companion`), the `/tori/` aliases, and the separate `fleet/` coordinator routes. Mark client-side behavior that is not verified from this repo as [Guessing].
- README documentation section links both this plan and the client matrix (one added line).

**Threat note:** A stale repo name in a "where the code lives" table can send people or automation to the wrong place, or to a repo that is no longer the product. Mitigation: fix the live pointer on the cleanup branch; keep historical test notes clearly historical.

**Tests to add:** None required beyond the usual markdown review. A light CI grep for `Victorious93/ToriLinux` in non-historical docs can land with the cleanup PR, with an allow-list for LICENSE, NOTICE, and dated reports.

**Dependencies:** Cleanup branch for the fleet-integration fix. `docs/clients.md` depends only on reading `gateway.py`, `companion.py`, and `fleet/`.

---

## 5. Version the `/victor/capabilities` schema

**Priority / size:** Med / S

**Status:** planned

**Goal:** Treat `GET /victor/capabilities` as a stable, versioned contract so clients can detect compatibility instead of guessing from loose JSON.

**Default state:** The endpoint **already exists** on the gateway and is aliased at `/tori/capabilities`. [Certain] It is served by `capabilities()` in [`gateway.py`](../airootfs/usr/local/lib/victorlinux/gateway.py). It is not a fleet feature, but remote clients only reach it after the owner enables the gateway; on loopback with remote access off, no key is required. [Certain]

The document today has no `schema` or version field. [Certain] Current keys are: `openai_compatible`, `endpoints`, `ollama_compat`, `llamacpp_native`, `streaming`, `tool_calling`, `tool_execution_on_pc`, `note`, and optionally `verified_at` / `verified_server` after `victor-capcheck`. `streaming` and `tool_calling` stay `"unverified"` until a real test of the loaded model file. `tool_execution_on_pc` is always `False`. Contrast with `GET /victor/companion`, which already sends `"schema": 1`. [Certain]

**Design:**

- Document today's payload as **capabilities schema 1**. Add an explicit version field (name it `schema`, to match the companion report, unless a published client already depends on a different key — none is known in this repo).
- Policy: additive optional fields are allowed in v1; a change that renames, removes, or changes the meaning of an existing field is schema 2. Clients must ignore unknown keys. `/tori/capabilities` returns the same document.
- Keep capability honesty: do not advertise verified streaming or tool calling without `victor-capcheck` for the current model file; never flip `tool_execution_on_pc` to true as a "fleet feature."
- Publish the contract in [docs/clients.md](clients.md) and, when the field is added, in [docs/remote-access.md](remote-access.md). This planning PR only documents the gap and the intended contract; it does not change `gateway.py`.

**Threat note:** Clients that assume tool calling or a stable shape could send tools, mis-parse a future field, or think the PC will execute tools. Mitigation: version the document; keep `tool_execution_on_pc: false`; treat missing version as "pre-schema" (today's body) rather than silently breaking; contract tests so a change cannot drop the version field.

**Tests to add:** When the field is implemented, unit tests that `/victor/capabilities` and `/tori/capabilities` include `schema: 1` and the current keys; a fixture for the v1 document; auth stays required whenever the gateway is not loopback-only; unverified / verified / failed values still depend on the current model file.

**Dependencies:** None. Should land before clients start depending on new fields. Update `docs/clients.md` when the field ships.

---

## 6. Optional RustDesk client pack (RustDeskV hand-off)

**Priority / size:** Low / M

**Status:** planned

**Goal:** Offer an optional, off-by-default RustDesk *client* pack on the PC so a phone-side RustDeskV hand-off has something local to talk to, without turning VictorLinux into a remote-desktop server by default.

**Default state:** Off. Not installed, not started at boot, not listening, and not required for local AI use. [Certain] This repo does not currently ship a RustDesk pack. [docs/MANUAL_FLEET.md](MANUAL_FLEET.md) describes a phone-side hand-off: DroidCommand builds `rustdesk://<peer-id>` to installed rustdeskV (`com.carriez.flutter_hbb`) after checking version and an owner-trusted signing certificate, and puts no password, key, or server setting in the link. That flow is documented as not fully built on Android yet. [Certain] rustdeskV itself is a separate repo; image build jobs for it were blocked in [docs/BUILD_ALL.md](BUILD_ALL.md).

**Design:**

- A pack the owner can install or enable, not a default package in the ISO. Enabling it must be explicit.
- Ship a client (or a clearly documented client-only configuration) for the existing RustDeskV hand-off. Do not auto-start a public unattended desktop server, do not open a router port, and do not put rustdesk passwords or keys into `victorpc://pair` or fleet envelopes.
- Keep rustdeskV's self-hosted settings on the owner-controlled client. VictorLinux should not override them and should not embed secrets in a URI, matching the MANUAL_FLEET rule.
- If the pack is later offered in a release ISO, it still defaults off. This pack is independent of inference; local chat works without it.

**Threat note:** Remote desktop is a full-control path. An auto-started or WAN-exposed server, or a password stuffed into a QR or log, would be a serious compromise. Mitigation: optional pack, off by default, no unattended listener unless the owner opts in with a clear warning; no secrets in hand-off URIs; no wildcard bind by default; document that this is not a substitute for the authenticated inference gateway.

**Tests to add:** Default image / default config does not start RustDesk and does not listen on rustdesk ports; enabling the pack is explicit and reversible; a packaging or unit check that hand-off helpers do not interpolate passwords or keys into `rustdesk://` URIs; when possible, a smoke test that the bundled client version is the owner-trusted one.

**Dependencies:** None for planning. A release ISO (item 1) would be needed only if the pack is distributed that way. Phone-side RustDeskV work stays in those apps, not in this repo.

---

## What this PR does not do

- No feature code, no ISO build, no boot test, no release, and no merge to `main`.
- No change to [docs/fleet-integration.md](fleet-integration.md) (that fix stays on `chore/cleanup-urls-wheels`).
- No secrets, keys, or tokens in these docs beyond already-public protocol field names.
