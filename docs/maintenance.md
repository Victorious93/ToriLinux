# VictorLinux maintenance (developer documentation)

Original document by sasha0552, updated for the Victorious fork.

* Templates (`*.jinja2`) are rendered by `.ci/configure.py PLATFORM TYPE [--packs ...]`; rendered files are git-ignored.
* Add/modify a pack: edit `airootfs/root/customize_airootfs/scripts/*.jinja2` and the `PACKS` tuple in `.ci/configure.py`.
* llama.cpp backends: `airootfs/root/customize_airootfs/root-scripts/100-llamacpp.sh.jinja2`.
* Control Center, gateway, helpers: `airootfs/usr/local/lib/victorlinux/` and `airootfs/usr/local/bin/`.
* Bump versions deliberately: edit `.ci/options.ini` (tag **and** commit), run the tests, rebuild, update the test report.
* Checks: `python3 -m pytest tests`, `shellcheck`, `tests/integration_live.py MODEL.gguf` (real llama-server end to end).
* CI: `.github/workflows/`. Build instructions: [build.md](build.md).
