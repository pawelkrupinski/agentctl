#!/usr/bin/env python3
"""The quality tools' venv, installed from quality/tools.lock, outside the tree.

  python3 quality/tools.py            prints the venv's python, installing it first when missing (exit 1 + the fix)
  python3 quality/tools.py --relock   rewrites tools.lock's hashes from PyPI for the versions it pins

The venv lives at ~/.cache/agentctl-quality/tools-<lock digest>/ ($AGENTCTL_QUALITY_HOME), so a changed lock is a new
venv and two clones on one lock share one. Nothing is ever skipped: a tool that cannot be installed blocks the push.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request

def default_lock():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools.lock")


def quality_home():
    return os.environ.get("AGENTCTL_QUALITY_HOME") or os.path.expanduser("~/.cache/agentctl-quality")


def venv_dir(lock):
    digest = hashlib.sha256(open(lock, "rb").read()).hexdigest()[:12]
    return os.path.join(quality_home(), f"tools-{digest}-py{sys.version_info[0]}{sys.version_info[1]}")


def ensure(lock, run=subprocess.run):
    """The venv's python; installs the venv when it is missing. Raises SystemExit naming the fix when it cannot."""
    venv = venv_dir(lock)
    python = os.path.join(venv, "bin", "python")
    if os.path.exists(os.path.join(venv, ".installed")):
        return python
    steps = ([sys.executable, "-m", "venv", "--clear", venv],
             [python, "-m", "pip", "install", "-q", "--disable-pip-version-check", "--require-hashes", "--no-deps",
              "-r", lock])
    for cmd in steps:
        r = run(cmd, capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            raise SystemExit(f"quality tools: `{' '.join(cmd)}` failed:\n{(r.stderr or r.stdout)[-1500:]}\n"
                             f"fix: the network or PyPI, then push again (the hook installs them itself); "
                             f"a lock that no longer installs: python3 quality/tools.py --relock")
    open(os.path.join(venv, ".installed"), "w").write("ok\n")
    return python


def relock(lock):
    pins = re.findall(r"^([A-Za-z0-9_.-]+)==(\S+)", open(lock).read(), re.M)
    head = [line for line in open(lock).read().splitlines() if line.startswith("#")]
    out = head[:]
    for name, version in pins:
        data = json.load(urllib.request.urlopen(f"https://pypi.org/pypi/{name}/{version}/json", timeout=60))
        hashes = sorted({u["digests"]["sha256"] for u in data["urls"]})
        out.append(f"{name}=={version} \\")
        out += [f"    --hash=sha256:{h} \\" for h in hashes[:-1]] + [f"    --hash=sha256:{hashes[-1]}"]
    open(lock, "w").write("\n".join(out) + "\n")


if __name__ == "__main__":
    if "--relock" in sys.argv:
        relock(default_lock())
    else:
        print(ensure(default_lock()))
