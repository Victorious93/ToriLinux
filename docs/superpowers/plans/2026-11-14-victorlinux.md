# VictorLinux implementation plan

Goal: rename the existing distribution and its commands to VictorLinux, polish the existing interfaces, enforce dark appearance, and run reproducible bug checks.

Keep the Arch/Openbox/web and Debian/Xfce/Tk implementations. Use `victor-*` commands in both editions and `/usr/local/lib/victorlinux` for implementation files. Keep the real GitHub URL and original MIT attribution. New Arch USBs use `VICTORDATA` and a `victorlinux` directory; detect existing `TORIDATA` partitions and `tori` directories without moving or deleting owner data. Preserve legacy health/capability API routes for existing Android clients.

- [x] Establish the current test baseline and reproduce failures with isolated test state.
- [x] Rename operational paths, account, services, commands, environment variables, boot labels, and ISO identity; verify all build profiles render.
- [x] Fix demonstrated API/UI failures with regression coverage, including chat assets and unsaved remote settings.
- [x] Polish the web Control Center and desktop settings; enforce dark GTK, browser, terminal, Tk widgets and first-run dialogs. Remove the light toggle and ignore old light preferences.
- [x] Update READMEs, command migration notes, CI checks and a separate dated test report. Keep older build evidence explicitly historical.
- [x] Run Python, browser, syntax and profile checks and review the diff.
- [x] Package source snapshots, patches, reports, manual and one master Claude Code prompt. The user requested a ZIP handoff instead of publication.

Verification limits: source/browser tests do not establish a rebuilt ISO, firmware boot, USB persistence or RX 6800 XT acceleration. Record those separately and do not reuse older test claims for this revision.

Work attribution: Victorious / Victorious Framework.
