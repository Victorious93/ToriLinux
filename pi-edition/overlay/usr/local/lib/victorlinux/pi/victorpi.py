"""VictorLinux Raspberry Pi edition: pure helpers shared by the boot services, the first-boot screens and victor-pi.

Everything here is side-effect free so it can be unit-tested on any machine (pi-edition/tests)."""
import re

# Board classes. LLM serving needs real RAM: below ~1.8 GiB the device is a node (gateway/CLI/mesh/bridge only).
MIN_LLM_MEM_KIB = 1_800_000
COUNTRY_RE = re.compile(r"^[A-Z]{2}$")
# ISO 3166-1 alpha-2 codes offered first in the setup screens (any valid 2-letter code is accepted).
COMMON_COUNTRIES = ["US", "GB", "CA", "AU", "NZ", "IE", "DE", "FR", "ES", "IT", "NL", "BE", "SE", "NO", "DK", "FI",
                    "PL", "PT", "CH", "AT", "JP", "KR", "IN", "BR", "MX", "ZA", "SG", "AE"]
FIRSTBOOT_KEYS = {"FULL_NAME", "PASSWORD_HASH", "WIFI_COUNTRY", "WIFI_SSID", "WIFI_PSK", "SSH_AUTHORIZED_KEY", "SSH_ENABLE"}


def classify(model, mem_kib):
    """model: /proc/device-tree/model text; mem_kib: MemTotal. Returns a dict describing what this board may run."""
    m = (model or "").replace("\x00", "").strip()
    if "Raspberry Pi 5" in m or "Compute Module 5" in m or "Raspberry Pi 500" in m:
        board = "pi5"
    elif "Raspberry Pi 4" in m or "Compute Module 4" in m or "Raspberry Pi 400" in m:
        board = "pi4"
    elif "Zero 2" in m:
        board = "zero2w"
    elif m.startswith("Raspberry Pi"):
        board = "other-pi"
    else:
        board = "unknown"
    llm, reason = True, "enough RAM for small CPU models"
    if board == "zero2w":
        llm, reason = False, "Raspberry Pi Zero 2 W (512 MB RAM): LLM serving is disabled; this device runs as a node."
    elif board in ("other-pi", "unknown"):
        llm, reason = False, f"Unsupported board ({m or 'no device-tree model'}): LLM serving is disabled."
    elif mem_kib < MIN_LLM_MEM_KIB:
        llm, reason = False, f"Only {mem_kib // 1024} MiB RAM: LLM serving is disabled; this device runs as a node."
    role = "ai-box" if llm else "node"
    return {"board": board, "model": m, "mem_kib": int(mem_kib), "llm_allowed": llm, "role": role, "reason": reason,
            "recommended_model": recommend_model(board, mem_kib) if llm else None}


def recommend_model(board, mem_kib):
    """A small default suggestion only; nothing is downloaded. Sizes are approximate Q4_K_M GGUF file sizes."""
    gib = mem_kib / (1024 * 1024)
    if board == "pi5" and gib >= 7:
        return {"name": "Llama 3.2 3B Instruct Q4_K_M", "approx_size": "2.0 GB", "ctx": 4096}
    if gib >= 3.5:
        return {"name": "Qwen2.5 1.5B Instruct Q4_K_M", "approx_size": "1.0 GB", "ctx": 4096}
    return {"name": "Qwen2.5 0.5B Instruct Q4_K_M", "approx_size": "0.4 GB", "ctx": 2048}


def valid_country(cc):
    return bool(COUNTRY_RE.match(cc or "")) and cc != "00"


def set_cmdline_regdom(cmdline, cc):
    """Return cmdline.txt content with cfg80211.ieee80211_regdom=CC (replacing any previous value). One line."""
    if not valid_country(cc):
        raise ValueError(f"invalid Wi-Fi country code: {cc!r}")
    parts = [p for p in cmdline.replace("\n", " ").split(" ") if p and not p.startswith("cfg80211.ieee80211_regdom=")]
    parts.append(f"cfg80211.ieee80211_regdom={cc}")
    return " ".join(parts) + "\n"


def parse_firstboot(text):
    """Parse /boot/firmware/victor-firstboot.conf (KEY=VALUE lines). Returns (config, errors).

    Only a password HASH is accepted (openssl passwd -6 / mkpasswd -m yescrypt), never a plain password."""
    cfg, errs = {"SSH_AUTHORIZED_KEY": []}, []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            errs.append(f"line {n}: expected KEY=VALUE"); continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        if k == "PASSWORD":
            errs.append(f"line {n}: plain PASSWORD is not accepted; use PASSWORD_HASH (openssl passwd -6)"); continue
        if k not in FIRSTBOOT_KEYS:
            errs.append(f"line {n}: unknown key {k}"); continue
        if k == "SSH_AUTHORIZED_KEY":
            cfg[k].append(v)
        else:
            cfg[k] = v
    h = cfg.get("PASSWORD_HASH")
    if h is not None and not (h.startswith("$6$") or h.startswith("$y$")):
        errs.append("PASSWORD_HASH must be a SHA-512 ($6$) or yescrypt ($y$) crypt hash")
    if "WIFI_COUNTRY" in cfg and not valid_country(cfg["WIFI_COUNTRY"]):
        errs.append("WIFI_COUNTRY must be a 2-letter ISO country code, e.g. US")
    if "WIFI_SSID" in cfg and not (1 <= len(cfg["WIFI_SSID"].encode()) <= 32):
        errs.append("WIFI_SSID must be 1-32 bytes")
    if "WIFI_PSK" in cfg and not (8 <= len(cfg["WIFI_PSK"]) <= 63):
        errs.append("WIFI_PSK must be 8-63 characters")
    if "WIFI_SSID" in cfg and "WIFI_COUNTRY" not in cfg:
        errs.append("WIFI_COUNTRY is required when WIFI_SSID is set (the radio stays blocked without it)")
    if cfg.get("SSH_ENABLE", "no").lower() not in ("yes", "no"):
        errs.append("SSH_ENABLE must be yes or no")
    if cfg.get("SSH_ENABLE", "no").lower() == "yes" and not cfg["SSH_AUTHORIZED_KEY"]:
        errs.append("SSH_ENABLE=yes needs at least one SSH_AUTHORIZED_KEY (key-only login)")
    if "FULL_NAME" in cfg and (len(cfg["FULL_NAME"]) > 64 or any(c in cfg["FULL_NAME"] for c in ":,=\n")):
        errs.append("FULL_NAME must be at most 64 characters without : , =")
    return cfg, errs


def nm_wifi_keyfile(ssid, psk):
    """NetworkManager keyfile for a WPA2/WPA3-personal network (written 0600 by root)."""
    if not (1 <= len(ssid.encode()) <= 32) or not (8 <= len(psk) <= 63) or "\n" in ssid + psk:
        raise ValueError("invalid SSID or passphrase")
    return ("[connection]\nid=victor-wifi\ntype=wifi\nautoconnect=true\n\n"
            f"[wifi]\nmode=infrastructure\nssid={ssid}\n\n"
            f"[wifi-security]\nkey-mgmt=wpa-psk\npsk={psk}\n\n"
            "[ipv4]\nmethod=auto\n\n[ipv6]\nmethod=auto\naddr-gen-mode=stable-privacy\n")


# Optional integrations. None runs by default. 'script' is the implementation reused from main when it exists.
HOOKS = {
    "scrcpy": {"desc": "Phone mirroring/recording (scrcpy + adb)", "script": "/usr/local/bin/victor-phone", "todo": None},
    "phone-keyboard-bt": {"desc": "Victorious Keyboard as a Bluetooth keyboard/mouse", "script": None,
                          "todo": "VictorLinux-side Bluetooth pairing is not on main yet (phone side not built either)."},
    "hotspot": {"desc": "Wi-Fi hotspot for phone pairing", "script": None, "todo": "No hotspot tool on main yet."},
    "mesh": {"desc": "Victorious Mesh (tailscale against your Headscale)", "script": None,
             "todo": "No mesh tool on main yet; tailscale is not in the image."},
}
