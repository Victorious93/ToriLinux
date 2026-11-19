"""secset.py tests. Pure-logic tests always run. The LIVE test is a REAL integration test
against a real installed `setoolkit` - not a mock - and is skipped (not faked) when one is
not on PATH. This is exactly the QRCode Generator Attack Vector workflow verified live on
2026-11-19 (see docs/security-backend.md#setoolkit-coverage for the full transcript and the
install steps used to get `setoolkit` and `pexpect` onto PATH)."""
import os

import pytest

import secprotocol as proto
import secset


def test_is_available_false_without_binary(monkeypatch):
    monkeypatch.setattr(secset, "SETOOLKIT_BIN", "definitely-not-a-real-binary-xyz")
    assert secset.is_available() is False


def test_run_rejects_multiline_url():
    with pytest.raises(secset.SetAdapterError):
        secset.run_qrcode_generator("https://example.com\nextra", timeout_s=1)


def test_sms_spoofing_is_not_in_supported_vectors():
    """The real, load-bearing finding from reading current upstream SET: this vector was
    removed from the main attack-vector menu entirely (10 entries now, ending in Third
    Party Modules - none of them SMS). Guard against a future edit silently re-adding it
    to SUPPORTED_VECTORS without a fresh verified transcript."""
    assert "SMS_SPOOFING" not in secset.SUPPORTED_VECTORS
    assert "SMS_SPOOFING" in proto.UNSUPPORTED_SET_VECTOR_REASONS


def test_strip_ansi():
    assert secset.strip_ansi("\x1b[4m\x1b[36mset\x1b[0m> ") == "set> "


live = pytest.mark.skipif(not secset.is_available(),
                           reason="setoolkit (and pexpect) must be installed and on PATH to run this")


@live
def test_live_qrcode_generation_round_trip(tmp_path):
    url = "http://example.invalid/dca-backend-verification"
    output_lines = []
    png_path = secset.run_qrcode_generator(url, on_output=output_lines.append, timeout_s=60)
    assert os.path.isfile(png_path)
    assert os.path.getsize(png_path) > 0
    assert any("QRCode has been generated" in line for line in output_lines)
    # Decode the real PNG and confirm it encodes exactly the requested URL - not just that a
    # file of nonzero size appeared, which would prove nothing about correctness.
    try:
        from PIL import Image
        from pyzbar.pyzbar import decode
    except ImportError:
        pytest.skip("Pillow/pyzbar not installed in this test environment; file presence was still checked")
    decoded = decode(Image.open(png_path))
    assert [d.data.decode() for d in decoded] == [url]


@live
def test_live_cancellation_before_any_prompt_is_sent():
    with pytest.raises(secset.SetAdapterError) as e:
        secset.run_qrcode_generator("https://example.com", is_cancelled=lambda: True, timeout_s=30)
    assert e.value.code == "cancelled"
