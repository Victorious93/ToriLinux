# Phone mirroring (scrcpy)

Optional tools to mirror and control a USB-connected Android phone from a
VictorLinux workstation, including audio and optional recording.

The mirroring engine is [scrcpy](https://github.com/Genymobile/scrcpy) by
Genymobile, licensed under Apache-2.0. Credit is also recorded in
[NOTICE](../NOTICE). VictorLinux does not download scrcpy, the scrcpy
server, or any other file at runtime. There is no telemetry. The UI is
English only.

This feature is **optional**. If scrcpy or adb is missing, the tools print
a clear message and exit. The image build and the rest of the system must
still work.

## What is included

| Tool | Where | Role |
|---|---|---|
| `victor-phone-gui` | GUI builds (Arch GUI profile and the Debian edition) | **VictorLinux Phone** — a Tk front end in the brand palette (background #0B0F1A, cyan #00E5FF, magenta #FF2BD6). Lists phones, starts and stops scrcpy, with audio, recording, stay-awake and turn-screen-off options. |
| `victor-phone-headless` | Every profile, including headless | CLI for adb and optional scrcpy: `status`, `devices`, `wait`, `screencap`, `record` (no window) and `mirror`. |
| `victor-phone` | Every profile | Original CLI wrapper that starts scrcpy directly. Still present for scripts. |

| Edition | Packages | Launcher |
|---|---|---|
| Arch (repository root, GUI profile) | `scrcpy` and `tk`, plus `android-tools` and `android-udev` on every profile | Openbox menu item **VictorLinux Phone**, and a `.desktop` file that runs `victor-phone-gui` |
| Arch headless profile | `android-tools` and `android-udev`. No `scrcpy` or `tk` | Use `victor-phone-headless` (adb always; scrcpy commands if you install scrcpy later) |
| Debian (`debian-edition/`) | `adb`, `android-sdk-platform-tools-common` (udev rules) and `python3-tk`. `scrcpy` is **not** in Debian 13 (trixie) itself; a build hook tries the configured suite and then `trixie-backports`, and **skips** if the package is not available | Xfce application **VictorLinux Phone** |

Both editions ship the same helpers: `/usr/local/bin/victor-phone-gui`,
`/usr/local/bin/victor-phone-headless` and `/usr/local/bin/victor-phone`.

No Control Center button was added. Starting a graphical scrcpy session
from the web or Tk admin UI is not a small change.

## Use the GUI (GUI builds)

1. On the phone, enable **Developer options** and **USB debugging**.
2. Connect the phone to the PC with a USB cable.
3. Unlock the phone and accept the "Allow USB debugging" prompt.
4. Open **VictorLinux Phone** from the menu, or run `victor-phone-gui`.
5. Select the phone, set audio or recording if you want, and press
   **Start mirroring**. **Stop** ends the session.

If Tk is missing, the GUI prints an English message and exits. Use
`victor-phone-headless` instead.

## Use the headless CLI

```sh
victor-phone-headless status
victor-phone-headless devices
victor-phone-headless wait
victor-phone-headless screencap "$HOME/phone.png"
victor-phone-headless record "$HOME/phone-session.mp4" --no-audio
victor-phone-headless mirror --serial 0123456789ABCDEF
victor-phone-headless record "$HOME/phone-session.mp4" --dry-run
```

`status` and `devices` accept `--json`. `screencap` uses adb only, so it
works on a headless image that has no scrcpy. `record` adds `--no-window`
so a session can be written to a file without a display. `mirror` starts
scrcpy and needs a display (or pass `--no-window` after the command).

`--dry-run` prints the scrcpy arguments (one per line) and does not need
a phone or a scrcpy install. Extra scrcpy flags can be passed after `--`.

## Use the original CLI

```sh
victor-phone
victor-phone --record "$HOME/phone-session.mp4"
victor-phone --no-audio
victor-phone --audio-source mic
victor-phone --serial 0123456789ABCDEF
victor-phone --dry-run
```

Default audio is whatever the installed scrcpy uses (device playback on
current releases). `--audio-source` and `--record` are passed through as
`--audio-source=...` and `--record=...`.

## Permissions

If `adb devices` shows `no permissions` or no device at all:

- Arch: the `android-udev` package provides udev rules and the `adbusers`
  group. The image customization adds the `victor` user to `adbusers`
  when that group exists.
- Debian: udev rules come from `android-sdk-platform-tools-common`
  (and its `android-udev-rules` dependency). The `victor` user is already
  in `plugdev`, and in `adbusers` when that group exists.

After a group change, log out and back in, or unplug and replug the
phone. A udev tag alone is not enough if the user session started
before the group was added.

## Debian and backports

[packages.debian.org](https://packages.debian.org/trixie/scrcpy) reports
that `scrcpy` is not in Debian 13 (trixie). A later version is in
`trixie-backports`. [Certain] as of 16 November 2026.

The optional hook `0250-optional-scrcpy.hook.chroot` may install scrcpy
from backports **at image build time only**, then removes that source so
the live system does not keep backports enabled. If the package or the
index cannot be fetched, the hook logs the skip and the build continues.
You can install scrcpy later yourself; nothing is auto-downloaded on the
running system.

## What was not tested

- No phone was connected. [Certain]
- No ISO of this change was built or booted. [Certain]
- The Tk GUI was unit-tested without opening a window (palette, argument
  building, missing-Tk message). It was not clicked through on a live
  desktop. [Certain]
- Whether a particular scrcpy version accepts every flag above depends
  on the package that actually landed in the image. [Likely] current
  Arch `scrcpy` and Debian backports 3.x accept `--record`, `--no-audio`,
  `--audio-source`, `--stay-awake`, `--turn-screen-off` and `--no-window`.

If a flag is rejected, run `scrcpy --help` on that image and pass the
working flags after `--`.
