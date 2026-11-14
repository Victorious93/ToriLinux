#!/usr/bin/env python3
"""Render the *.jinja2 build templates for a chosen profile.

usage: .ci/configure.py PLATFORM TYPE [--packs a,b,c]

  PLATFORM  amd    AMD GPUs: llama.cpp Vulkan (default path) + CPU fallback
            cuda   NVIDIA GPUs: llama.cpp CUDA + Vulkan + CPU
            rocm   alias for `amd` with the experimental `hip` pack
  TYPE      headless (alias: normal)  no desktop; control center reachable via browser/victor-open
            gui                       Openbox desktop that autostarts the control center
            empty                     drivers/OS only: no llama.cpp, no packs (as upstream "empty")
  PACKS     optional extras, all off by default (lean image):
            hip comfyui automatic invokeai sillytavern vllm(cuda only)

Modified by Victorious / Victorious Framework from upstream ToriLinux (sasha0552, MIT).
"""
import configparser
import glob
import json
import sys

import jinja2

PLATFORMS = ("amd", "cuda", "universal", "rocm")
TYPES = ("headless", "normal", "gui", "empty")
PACKS = ("hip", "comfyui", "automatic", "invokeai", "sillytavern", "vllm")


def render_template(filepath, **options):
  if not filepath.endswith(".jinja2"):
    return
  with open(filepath, "r") as file:
    template = jinja2.Template(file.read(), lstrip_blocks=True, trim_blocks=True,
                               undefined=jinja2.StrictUndefined, keep_trailing_newline=True)
  rendered = template.render(**options)
  with open(filepath[:-7], "w") as file:
    file.write(rendered)


def parse(argv):
  args, packs = [], []
  it = iter(argv)
  for a in it:
    if a == "--packs":
      packs = [p for p in next(it, "").split(",") if p]
    elif a.startswith("--packs="):
      packs = [p for p in a.split("=", 1)[1].split(",") if p]
    else:
      args.append(a)
  platform = args[0] if len(args) > 0 else "amd"
  type_ = args[1] if len(args) > 1 else "headless"
  if platform not in PLATFORMS:
    sys.exit(f"unknown platform '{platform}' (use: {', '.join(PLATFORMS)})")
  if type_ not in TYPES:
    sys.exit(f"unknown type '{type_}' (use: {', '.join(TYPES)})")
  for p in packs:
    if p not in PACKS:
      sys.exit(f"unknown pack '{p}' (use: {', '.join(PACKS)})")
  if platform == "universal":
    platform = "cuda"  # same portable Vulkan/CPU builds plus the NVIDIA driver/CUDA stack
  if platform == "rocm":
    platform = "amd"
    packs = sorted(set(packs) | {"hip"})
  if type_ == "normal":
    type_ = "headless"
  if type_ == "empty":
    packs = []
  if "vllm" in packs and platform != "cuda":
    sys.exit("pack 'vllm' is only available on the cuda platform")
  if "invokeai" in packs and platform != "cuda":
    sys.exit("pack 'invokeai' is only available on the cuda platform (as upstream)")
  return platform, type_, sorted(set(packs))


def main():
  platform, type_, packs = parse(sys.argv[1:])
  config = configparser.ConfigParser()
  config.read(".ci/options.ini")
  options = {s: dict(config.items(s)) for s in ("repositories", "revisions", "files", "build")}

  def gh_release(name, index=None):
    fn = options["files"][name]
    if index is not None:
      fn = json.loads(fn)[index]
    return f"{options['repositories'][name]}/releases/download/{options['revisions'][name]}/{fn}"

  rendered = []
  for filepath in sorted(glob.glob("**/*.jinja2", recursive=True)) + [".gitignore.jinja2"]:
    rendered.sort()
    render_template(filepath, platform=platform, type=type_, packs=packs, rendered=rendered,
                    gh_release=gh_release, **options)
    rendered.append(filepath[:-7].replace("\\", "/"))
    print(f"File '{filepath}' rendered successfully")
  print(f"PROFILE platform={platform} type={type_} packs={','.join(packs) or '-'}")


if __name__ == "__main__":
  main()
