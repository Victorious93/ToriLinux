# VictorLinux, Victorious Flash and Victorious Browser

Work attribution: **Victorious / Victorious Framework**. This guide describes the actual repositories and distinguishes implemented paths from release work still required.

## Ownership of each job

| Component | Job | Where the code lives |
|---|---|---|
| VictorLinux | Boot the PC; detect and use the PC's CPU/GPU/RAM; run local models; persist owner work | `Victorious93/ToriLinux` |
| Victorious Flash desktop | Verify and write an image to a physical removable drive | `Victorious93/victorious-flash` (Rust/Tauri/React) |
| Victorious Flash Android | Expose a verified image through a supported rooted phone's USB gadget, or write it to a USB stick over OTG | Same repo, `android/` |
| Victorious Browser | Android companion: saved fleet panels and authenticated PC inference/chat | `Victorious93/victorious-browser` |
| DroidCommand-AI / VictorSuite | Existing inference clients; they are not modified by this ZIP | Their own repositories |

The phone is the **boot medium** when exposing an ISO. The PC executes VictorLinux and supplies its GPU. A 20 GiB PC GPU does not become the phone's GPU. Phone gadget mode and phone USB-host/OTG mode are different USB roles; do not promise both simultaneously through the same port.

## Two supported design paths

1. Physical drive: build ISO → obtain/check its SHA-256 → flash drive → verify read-back → boot the PC → detect PC hardware → select/import a model → run inference.
2. Phone medium: build the same ISO → copy it and its expected checksum to the phone → Flash verifies it → phone exposes read-only USB disk/CD-ROM → boot the PC → detect PC hardware → select separate writable data storage → run inference.

The revised ISO has not been built or booted in this work. Existing Flash gadget code and OTG write code are present; real phone/firmware compatibility still needs testing. Flash's experimental Ubuntu/casper HTTP/EFI path does not automatically support the Arch image.

## Persistence boundaries

Raw flashing writes an image. It does not create a `VICTORDATA` partition unless the provisioning path explicitly implements and verifies that step. VictorLinux's `scripts/make-usb.sh` provides its own layout/provisioning logic. The Android raw writer does not yet have equivalent VictorLinux data-partition provisioning.

A phone exposing a read-only ISO does not provide persistent writable storage by itself. Use a separate USB/data drive. A future private ext4 data-image LUN is optional work: it must have exclusive ownership while exposed, explicit detach/flush, free-space checks and recovery. Never share an Android-mounted writable filesystem with the PC at the same time.

Arch recognizes new `VICTORDATA` and old `TORIDATA` labels and reuses an old `tori` directory without moving it. Debian has its own root-level data layout. A shared partition label does not imply interchangeable settings or chat histories.

## Browser connection boundary

The Android browser already has `PanelStore`, editable fleet panels and mesh reachability checks. Use them for the **real PC address** over the configured LAN/VPN. `localhost` on Android is Android, not VictorLinux.

VictorLinux's administrative web interface binds to loopback (`127.0.0.1:8787`) and uses a per-session token. Do not turn it into a public web administration service just to integrate the browser. Remote inference uses the separately configured API listener and bearer authentication. A future browser status view should use a small authenticated read-only API, or an explicit trusted tunnel/bridge, not the local admin token in a URL or a public JavaScript store.

On Android, protect imported credentials with Keystore-backed storage, show remote/local identity, and send an inference key only to its saved origin. Do not bypass certificate errors globally or inject a key into arbitrary WebView requests. Keep privacy-policy choices in force: remote/cloud inference and data uploads remain opt-in.

The separate `Victorious93/browser` repo is a headless Lightpanda fork. It has no graphical rendering engine. It is not the Android Victorious Browser app. If ever packaged as an optional PC automation tool, pin/test it and disable its documented telemetry; do not replace Firefox with it.

## Release metadata contract

The Flash catalog currently requires real HTTPS ISO/checksum URLs and an exact filename; `sha256` is optional but should be populated from the actual built artifact. Its existing `kind: linux-iso` and `image_format: iso` suffice. Do not invent a new Rust enum variant or Kotlin schema field without updating all consumers.

Only add a downloadable VictorLinux entry after the artifact and checksum actually exist and the URLs have been checked. Until then, use Flash's local image picker plus a manually supplied expected SHA-256. Adding a placeholder download URL would make the app less reliable.

## Remaining release checks

- Build the renamed ISO and examine the actual identity, executable permissions, boot paths and bundled driver versions.
- Boot UEFI and legacy BIOS in QEMU, then real AMD/Intel/NVIDIA machines as available. A rendered profile is not a boot test.
- Measure free memory and actual offloaded layers; run a small known GGUF and record GPU/CPU, context, model hash and throughput. Test larger/smaller PCs and the 20 GiB case without hardcoded GPU names.
- Exercise physical-drive and phone-gadget boot using the same ISO hash; verify phone detach/recovery behavior.
- Verify persistence over reboot and that neither flasher overwrites the system disk or reports an unverified write as successful.
- Build/test Flash's Rust components and Android APK; test cancellation, detach, short transfers, corrupt images and missing kernel functions.
- Test Browser companion flows on Android; keep native Linux browser packaging separate.

The ZIP's master Claude Code prompt carries the full implementation and test sequence. Reconcile its patches with the live checkouts before applying them; do not replace newer files wholesale.
