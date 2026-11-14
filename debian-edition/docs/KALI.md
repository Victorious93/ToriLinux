# Optional Kali environment

* Official image `docker.io/kalilinux/kali-rolling` **pinned by digest** (see `versions.env`), run with **rootless Podman** — separate filesystem and package repositories; the Debian host never gets Kali repos.
* Not privileged by default: no `--privileged`, `no-new-privileges`, podman's default rootless capability set, user-mode (slirp4netns) networking. `victor-ctl kali start --cap NET_RAW|NET_ADMIN` adds one explicit capability per launch.
* User work persists separately in `kali/` on VICTORDATA (mounted at `/root` in the container).
* Nothing is pulled silently: `kali start` fails with an explanation if the image is not installed. Install with `kali load-pack TAR` (offline; build the tar with `scripts/make-kali-pack.sh` on a networked machine) or
  `kali download --yes` (explicit network). Installing extra tools (`apt install kali-tools-…`) inside the container is your explicit, network-requiring choice; no tool metapackages are pre-installed.
* Limits you should know: a container shares the **host kernel**, host drivers (no separate Wi-Fi driver/kernel) and the host's hardware; USB/Wi-Fi monitor-mode work depends on the host chipset/driver, and
  raw-socket tools need `NET_RAW` and often a bridged/host network, which weakens isolation. This is not a hardware or kernel isolation boundary.
* A full Kali-based ISO is a possible later profile (see ROADMAP).
