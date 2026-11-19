"""Real Social-Engineer Toolkit (SET) adapter for the VictorLinux security backend.

This replaces guessed, unverified menu navigation with a validated, version-aware one.
Findings below come from actually running `setoolkit` (TrustedSec's own source, cloned at
commit 5e0fbb50ca8b156ac93532a0a4d138900c6e0827 / installed version string "8.1.3 Maverick")
under pexpect in this backend's own development/test environment on 2026-11-19 - not from
reading documentation or guessing. See docs/security-backend.md#setoolkit-coverage for the
full transcript this was verified against.

Mechanical fact that makes any of this possible at all: SET has no official non-interactive
flag. `seautomate` exists upstream but is explicitly interactive-handoff (it feeds a file of
answers via pexpect and then calls `child.interact()`, handing the terminal back to a human)
- not a fire-and-forget automation entry point. This adapter drives `setoolkit` itself over a
pty with pexpect, the same mechanical trick seautomate uses, but owns the whole session and
terminates it itself instead of handing control to a human, which is what "unattended
execution" actually requires.

Only SUPPORTED_VECTORS are run. Everything else in SetAttackVector-equivalent space is
rejected up front by secprotocol.validate_job_submit with the SPECIFIC reason recorded in
secprotocol.UNSUPPORTED_SET_VECTOR_REASONS - never silently attempted, never reported
successful from a startup/import check alone.
"""
import os
import re
import signal
import time

try:
    import pexpect
except ImportError:  # pragma: no cover - exercised by test_secset.py's ImportError path
    pexpect = None

SETOOLKIT_BIN = os.environ.get("VICTOR_SETOOLKIT_BIN", "setoolkit")

# The only vector this backend will actually run unattended today. Adding another vector
# here requires the same thing QRCODE_GENERATOR got: an actual pexpect transcript against a
# real installed setoolkit, recorded in docs/security-backend.md, not a guess.
SUPPORTED_VECTORS = frozenset({"QRCODE_GENERATOR"})

# Verified navigation: top-level menu "1" (load SET's own attack-vector menu), then that
# menu's "8" (QRCode Generator Attack Vector). Confirmed live 2026-11-19 against v8.1.3.
_QRCODE_NAV = ["1", "8"]

_URL_PROMPT = re.compile(r"Enter the URL.*:\s*$")
_GENERATED = re.compile(r"QRCode has been generated under\s+(\S+)")
_RETURN_PROMPT = re.compile(r"Press[^\n]{0,20}<return>[^\n]{0,20}to continue", re.IGNORECASE)
_MSF_GATED = re.compile(r"Metasploit was not found|Metasploit's path", re.IGNORECASE)
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# SET colors its own "set> " prompt (e.g. "\x1b[4m\x1b[36mset\x1b[0m> "), so "set" and ">"
# are never contiguous on the wire. Tolerate any run of non-newline bytes between them
# rather than matching on a specific ANSI sequence that could differ by terminal detection.
_SET_PROMPT = re.compile(r"set[^\n]{0,20}>\s*$")


def strip_ansi(s):
    return _ANSI.sub("", s)


class SetAdapterError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def is_available():
    """Cheap, passive: is a `setoolkit` executable on PATH at all. Does not prove a workflow
    runs - see run_qrcode_generator's own startup checks for that."""
    from shutil import which
    return pexpect is not None and which(SETOOLKIT_BIN) is not None


def run_qrcode_generator(url, on_output=None, is_cancelled=None, timeout_s=60):
    """Runs SET's QRCode Generator Attack Vector for `url` against a real installed
    `setoolkit`. Returns the absolute path SET itself reported writing the PNG to.

    Deterministic, zero network/mail/listener side effects - SET's own gen_qrcode() just
    renders a QR code image to disk (see src/qrcode/qrgenerator.py upstream); this was
    chosen as the first automated vector specifically because it is safe to exercise for
    real, every run, including in CI, without touching any third-party system.

    Raises SetAdapterError with code "tool_not_installed" if setoolkit/pexpect is missing,
    "tool_version_unsupported" if this run's menu doesn't match what was verified (the
    QRCode item moved or a different prompt appeared), "capability_missing" if Metasploit's
    path is not configured (this version gates QRCode behind it even though the module
    itself does not call into Metasploit), "cancelled" if is_cancelled() became true, and
    "timeout" on exceeding timeout_s.
    """
    if pexpect is None:
        raise SetAdapterError("tool_not_installed", "pexpect is not installed")
    if not is_available():
        raise SetAdapterError("tool_not_installed", f"'{SETOOLKIT_BIN}' is not on PATH")

    url = url.strip()
    if not url or "\n" in url or "\r" in url:
        raise SetAdapterError("bad_request", "url must be a single non-empty line")

    def emit(line):
        if on_output:
            on_output(line)

    child = pexpect.spawn(SETOOLKIT_BIN, timeout=timeout_s, encoding="utf-8")
    try:
        deadline = time.monotonic() + timeout_s

        def remaining():
            r = deadline - time.monotonic()
            if r <= 0:
                raise SetAdapterError("timeout", f"setoolkit did not reach the expected prompt within {timeout_s}s")
            return r

        def check_cancel():
            if is_cancelled and is_cancelled():
                raise SetAdapterError("cancelled", "job cancelled while waiting on setoolkit")

        # First run only: license/terms prompt. Agreeing is required to use the tool at all
        # (upstream's own gate, not this backend's); the main menu never appears without it.
        idx = child.expect([r"Do you agree to the terms of service.*:", r"\d{1,2}\)\s*Exit the Social-Engineer Toolkit"],
                            timeout=remaining())
        if idx == 0:
            check_cancel()
            child.sendline("y")
            child.expect(r"\d{1,2}\)\s*Exit the Social-Engineer Toolkit", timeout=remaining())

        for digit in _QRCODE_NAV:
            check_cancel()
            child.expect(_SET_PROMPT, timeout=remaining())
            child.sendline(digit)

        check_cancel()
        idx = child.expect([_URL_PROMPT, _MSF_GATED], timeout=remaining())
        if idx == 1:
            raise SetAdapterError(
                "capability_missing",
                "SET gates the QRCode Generator behind a configured Metasploit path "
                "(/etc/setoolkit/set.config METASPLOIT_PATH), even though generating a QR "
                "code does not itself call Metasploit. Configure that path and retry.",
            )

        check_cancel()
        child.sendline(url)
        idx = child.expect([_GENERATED, r"This module requires PIL"], timeout=remaining())
        raw = strip_ansi(child.after if isinstance(child.after, str) else str(child.after))
        emit(raw)
        if idx == 1:
            raise SetAdapterError("internal", "setoolkit reports Pillow/qrcode is not installed in its environment")
        m = _GENERATED.search(raw)
        if not m:
            raise SetAdapterError("tool_version_unsupported",
                                   "setoolkit's QRCode output did not match the verified "
                                   "'QRCode has been generated under <path>' message")
        png_path = m.group(1)

        child.expect(_RETURN_PROMPT, timeout=remaining())
        child.sendline("")
        return png_path
    finally:
        _terminate(child)


def _terminate(child):
    try:
        if child.isalive():
            child.sendline("exit")
            child.expect(pexpect.EOF, timeout=5)
    except Exception:
        pass
    try:
        if child.isalive():
            child.kill(signal.SIGTERM)
            time.sleep(0.3)
        if child.isalive():
            child.kill(signal.SIGKILL)
    except Exception:
        pass
    try:
        child.close(force=True)
    except Exception:
        pass
