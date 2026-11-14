"""Where fleet state lives, and whether it is actually durable.

Neither edition's live OS root survives reboot. State is kept only under the persistent data partition
(Debian: /run/victor/root -> VICTORDATA; Arch: the TORIDATA mount reported by /run/tori/persist.json).
If that is missing, read-only, tmpfs or the explicit temporary session, the coordinator can still run but
reports durable=false and says why; it never claims durability it does not have.
"""
import json, os, tempfile


def _fstype_of(path):
    best, fs = "", "unknown"
    try:
        real = os.path.realpath(path)
        for line in open("/proc/mounts"):
            dev, mp, t = line.split()[:3]
            mp = mp.replace("\\040", " ")
            if (real == mp or real.startswith(mp.rstrip("/") + "/")) and len(mp) >= len(best):
                best, fs = mp, t
    except OSError:
        pass
    return fs


def resolve(env=None):
    """Return dict(path, durable, reason, writable). Never raises."""
    env = os.environ if env is None else env
    explicit = env.get("VICTOR_FLEET_DIR")
    if explicit:
        base, why = explicit, "VICTOR_FLEET_DIR override"
        trusted = env.get("VICTOR_FLEET_DIR_DURABLE") == "1"   # an override is only durable if the owner says so
    else:
        trusted = False
        why = ""
        base = None
        victor_root = os.path.join(env.get("VICTOR_RUN", "/run/victor"), "root")
        tori_state = os.path.join(env.get("TORI_RUN_DIR", "/run/tori"), "persist.json")
        if os.path.islink(victor_root) or os.path.isdir(victor_root):
            target = os.path.realpath(victor_root)
            sess = os.path.realpath(os.path.join(env.get("VICTOR_RUN", "/run/victor"), "session"))
            base = os.path.join(victor_root, "fleet")
            if target == sess:
                why = "temporary session storage (RAM): nothing survives reboot"
            else:
                trusted = _fstype_of(target) not in ("tmpfs", "overlay", "ramfs", "squashfs", "unknown")
                why = "persistent data partition" if trusted else f"data root is on '{_fstype_of(target)}'"
        elif os.path.isfile(tori_state):
            try:
                st = json.load(open(tori_state))
            except (OSError, ValueError):
                st = {}
            mode, mp = st.get("mode"), st.get("data_root")
            if mode == "persistent" and mp:
                base = os.path.join(mp, "fleet")
                trusted = _fstype_of(mp) not in ("tmpfs", "overlay", "ramfs", "squashfs", "unknown")
                why = "TORIDATA partition" if trusted else f"data root is on '{_fstype_of(mp)}'"
            else:
                why = f"Arch edition storage mode is '{mode or 'unknown'}'" + (f": {st.get('message')}" if st.get("message") else "")
        if base is None:
            base = os.path.join(env.get("XDG_RUNTIME_DIR") or tempfile.gettempdir(), "victor-fleet-volatile")
            why = why or "no persistent data partition found"
    writable = False
    try:
        os.makedirs(base, mode=0o700, exist_ok=True)
        probe = os.path.join(base, ".write-probe")
        with open(probe, "w") as f:
            f.write("x"); f.flush(); os.fsync(f.fileno())
        os.unlink(probe)
        writable = True
    except OSError as e:
        why += f"; not writable ({e.strerror})"
    durable = bool(trusted and writable)
    return {"path": base, "durable": durable, "writable": writable,
            "reason": why if not durable else why + " (writable)"}
