# Phone mirroring (scrcpy)

Optional helper to mirror and control a USB-connected Android phone from a
VictorLinux workstation, including audio and optional recording.

The tool is [scrcpy](https://github.com/Genymobile/scrcpy) by Genymobile,
licensed under Apache-2.0. Credit is also recorded in [NOTICE](../NOTICE).
VictorLinux does not download scrcpy, the scrcpy server, or any other file
at runtime. There is no telemetry.

This feature is **optional**. If scrcpy or adb is missing, `victor-phone`
prints a clear message and exits. The image build and the rest of the
system must still work.

## What is included

| Edition | Packages | Launcher |
|---|---|---|
| Arch (repository root, GUI profile) | `scrcpy`, plus `android-tools` and `android-udev` on every profile | Openbox menu item **Phone mirroring**, and a `.desktop` file |
| Debian (`debian-edition/`) | `adb` and `android-sdk-platform-tools-common` (udev rules). `scrcpy` is **not** in Debian 13 (trixie) itself; a build hook tries the configured suite and then `trixie-backports`, and **skips** if the package is not available | Xfce application **Phone mirroring** |

Both editions ship the same helper: `/usr/local/bin/victor-phone`.

No Control Center button was added. Starting a graphical scrcpy session
from the web or Tk admin UI is not a small change.

## Use

1. On the phone, enable **Developer options** and **USB debugging**.
2. Connect the phone to the PC with a USB cable.
3. Unlock the phone and accept the "Allow USB debugging" prompt.
4. Run **Phone mirroring** from the menu, or:

```sh
victor-phone
victor-phone --record "$HOME/phone-session.mp4"
victor-phone --no-audio
victor-phone --audio-source mic
victor-phone --serial 0123456789ABCDEF
victor-phone --dry-run
```

`--dry-run` prints the scrcpy arguments (one per line) and does not need
a phone or a scrcpy install. Extra scrcpy flags can be passed after `--`.

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
- Whether a particular scrcpy version accepts every flag above depends
  on the package that actually landed in the image. [Likely] current
  Arch `scrcpy` and Debian backports 3.x accept `--record`, `--no-audio`
  and `--audio-source`.

If a flag is rejected, run `scrcpy --help` on that image and pass the
working flags after `--`.
