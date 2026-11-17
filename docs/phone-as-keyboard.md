# Use a phone as a keyboard and mouse

VictorLinux can be controlled from the [Victorious Keyboard](https://github.com/Victorious93/Victorious-Keyboard) Android app (`com.victorious.keyboard`) in three ways. Bluetooth and Wi-Fi input are **off by default**. USB needs no software on the PC.

Nothing here is downloaded automatically. You choose the app and the model files on the phone yourself.

## USB (no PC software)

Victorious Keyboard can present itself as a standard USB keyboard and mouse over a USB data cable. On the phone this uses a temporary USB gadget (ConfigFS HID, report ID 1 = keyboard, 2 = mouse) and needs **root**. It is US ASCII only. The PC sees a normal USB input device, so **no VictorLinux software is required** and no pairing step runs on the PC.

This USB path is documented from the keyboard app's own behaviour ([`docs/USB_KEYBOARD_MOUSE.md`](https://github.com/Victorious93/Victorious-Keyboard/blob/main/docs/USB_KEYBOARD_MOUSE.md) in that repo). It has **not been tried on a real VictorLinux boot**, so treat “it works with any PC, including VictorLinux” as **[Likely]**.

Use a data cable, not charge-only. Unplug when you are finished.

## Bluetooth keyboard and mouse

The phone acts as a standard Bluetooth HID keyboard and mouse through Android's `BluetoothHidDevice`. The PC uses BlueZ (`bluez` and, on Arch, `bluez-utils`). GUI builds also include `blueman`, but it does not auto-start, and **`bluetooth.service` is not enabled on boot**.

To pair, from the Control Center (Server & Access on the Arch edition, *Phone keyboard* on the Debian edition) or from a terminal:

1. Run **Pair phone (Bluetooth)** (`victor-bt-pair start` or `victor-ctl bluetooth start`). This starts `bluetoothd` for this session only, powers the adapter on, and makes the PC discoverable and pairable for **120 seconds**.
2. On the phone, pair with this PC. Compare the passkey shown on the PC with the phone.
3. Press **Confirm passkey** (`victor-bt-pair confirm`) only if they match. The PC **trusts the device only after that confirmation**.
4. After 120 seconds, or if you press Cancel, the PC is no longer discoverable.

`victor-bt-pair list` shows paired devices. `victor-bt-pair unpair AA:BB:CC:DD:EE:FF` removes one. This helper never runs `systemctl enable bluetooth`.

This path has not been tried with a real phone or adapter on a VictorLinux boot. The command plan and the confirm-before-trust rule are covered by tests. Hardware behaviour is **untested**.

## Wi-Fi input (WebSocket)

`victor-input` is an optional service that accepts keyboard and mouse events over **WSS** on the LAN. It is installed but **not enabled**. A localhost-only admin socket is used to open a pairing window.

1. Turn **Wi-Fi input** on and choose the listen address and port (default 8473).
2. Enter the address the phone should use and press **Pair phone (Wi-Fi keyboard)**. A `victorpc://pair` QR is shown for 10 minutes. It carries the WSS URL and TLS fingerprint and a single-use token, **never a Bearer token**.
3. The phone redeems the token over TLS and receives a per-device Bearer token once. The PC stores only the SHA-256 hash.
4. The app then sends keyed and pointer events as defined in [VICTOR_INPUT_PROTOCOL.md](VICTOR_INPUT_PROTOCOL.md). Unauthenticated, replayed, or over-limit messages are rejected.
5. Revoke a device to stop it immediately. Turn the service off when you do not need it.

**Firewall:** the Debian edition drops inbound traffic by default. Turning the service on opens only the input TCP port; turning it off closes that port. Do not leave the port open if the service is stopped. The Arch edition has no default-drop firewall in this tree; if you add one, open the same port only while the service is on.

This path is tested for protocol parsing, auth, replay, rate limits, mocked uinput mapping, and a live pairing redeem against a local WSS server. It has **not** been tried with a real phone, a real `/dev/uinput` device, or a booted VictorLinux image.

## Control Center

Both editions use the brand palette (background `#0B0F1A`, cyan `#00E5FF`, magenta `#FF2BD6`), English only, and no white backgrounds. On Arch the controls are in **Server & Access**. On Debian they are in the **Phone keyboard** tab (`victor-ctl input …` and `victor-ctl bluetooth …`).

## Related

- [Phone mirroring (scrcpy)](phone-mirroring.md) is a separate optional tool for seeing the phone's screen.
- [API keys and phone pairing](api-keys-and-pairing.md) is the inference-gateway QR, which uses the same URI scheme with different additive keys.
