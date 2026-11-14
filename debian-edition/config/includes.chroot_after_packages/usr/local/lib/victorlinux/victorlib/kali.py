"""Optional isolated Kali environment (rootless podman; official image pinned by digest; no host repos)."""
import os
from .common import *

NAME = "victor-kali"


def _versions():
    d = {}
    for l in open("/usr/share/victorlinux/versions.env"):
        l = l.split("#")[0].strip()
        if "=" in l:
            k, v = l.split("=", 1); d[k] = v
    return d


def image_ref():
    v = _versions()
    return f"{v['KALI_IMAGE']}@{v['KALI_DIGEST']}"


def _pm(*a, **k):
    return run(["podman", *a], **k)


def home_dir():
    return data_path("kali")


def installed():
    return _pm("image", "exists", image_ref()).returncode == 0


def status():
    s = {"image": image_ref(), "image_present": installed(), "container": None}
    p = _pm("ps", "-a", "--filter", f"name=^{NAME}$", "--format", "{{.Status}}")
    if p.returncode == 0 and p.stdout.strip():
        s["container"] = p.stdout.strip()
    return s


def load_pack(tar):
    """Offline pack: `podman save` archive of the pinned image, supplied on external media."""
    if not os.path.isfile(tar):
        raise VictorError(f"{tar} not found")
    r = _pm("load", "-i", tar, timeout=1800)
    if r.returncode:
        raise VictorError("podman load failed: " + r.stderr.strip())
    if not installed():
        raise VictorError("The archive loaded, but does not contain the pinned Kali image digest "
                          f"({image_ref()}). Rebuild the pack with scripts/make-kali-pack.sh.")
    return {"loaded": True}


def download_pack(confirm=False):
    if not confirm:
        raise VictorError("This downloads the Kali image over the network (hundreds of MB). Re-run with --yes.")
    r = _pm("pull", image_ref(), timeout=3600)
    if r.returncode:
        raise VictorError("podman pull failed (network needed): " + r.stderr.strip()[-300:])
    return {"pulled": image_ref()}


def start(net="slirp4netns", caps=()):
    """Unprivileged by default. `caps` are explicit, per-launch additions (e.g. NET_RAW for raw sockets)."""
    if not installed():
        raise VictorError("Kali image is not installed. Use 'kali load-pack' (offline) or 'kali download --yes'. "
                          "Nothing is pulled automatically.")
    os.makedirs(home_dir(), exist_ok=True)
    if status()["container"]:
        return {"already": True}
    args = ["run", "-d", "--pull=never", "--name", NAME, "--hostname", "victor-kali", "--security-opt", "no-new-privileges",
            "--network", net, "-v", f"{home_dir()}:/root:Z"]
    for cap in caps:
        if cap not in ("NET_RAW", "NET_ADMIN"):
            raise VictorError(f"capability {cap} is not offered; only NET_RAW / NET_ADMIN can be added explicitly")
        args += ["--cap-add", cap]
    args += [image_ref(), "sleep", "infinity"]
    r = _pm(*args)
    if r.returncode:
        raise VictorError("podman run failed: " + r.stderr.strip()[-300:])
    return {"started": True, "caps": list(caps), "note": "Containers share the host kernel and host network drivers; this is not hardware or kernel isolation."}


def stop():
    _pm("rm", "-f", NAME)
    return {"stopped": True}
