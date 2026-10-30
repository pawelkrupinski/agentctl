#!/usr/bin/env python3
"""A stand-in for the REAL GitHub CLI (`gh`, forge = github) in agentctl's tests, served from files in $FAKE_GH_DIR.
It answers like gh and github.com, not like the forgejo-gh shim:
  api [-X M] [-H h] <path>   GET -> api-<path with /?&= as _>.json (GitHub's REST shapes); missing -> `HTTP 404`;
                             any other method -> recorded (its --input body as `<pid> BODY <json>`), answers nothing
  api ... -R <repo>          refused, as gh does: `gh api` takes no -R (its path names the repo)
  release download <tag> -p <name> -O -  -> release-<tag>-<name>
Everything else (run list / view / download / cancel / rerun, workflow run) is the shim fake's (fake_forgejo_gh.py:
gh's own flags, JSON fields and --log-failed shape are the shim's). Every call is appended to calls.log as
`<caller pid> <argv>`, and to gh-calls.log (so a test tells which CLI answered)."""
import os, re, runpy, sys

d = os.environ["FAKE_GH_DIR"]
argv = sys.argv[1:]
HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(d, "gh-calls.log"), "a") as _f:  # which fake answered: this one (the real gh's stand-in)
    _f.write(" ".join(argv) + "\n")


def log(line):
    with open(os.path.join(d, "calls.log"), "a") as f:
        f.write(f"{os.getppid()} {line}\n")


def opt(name):
    return argv[argv.index(name) + 1] if name in argv else None


if argv[:1] == ["api"]:
    log(" ".join(argv))
    if "-R" in argv or "--repo" in argv:
        sys.stderr.write("unknown shorthand flag: 'R' in -R\n")
        sys.exit(1)
    method, rest, i = "GET", [], 1
    while i < len(argv):
        if argv[i] in ("-X", "--method", "-H", "--header", "--input", "-f", "-F"):
            if argv[i] in ("-X", "--method"):
                method = argv[i + 1]
            i += 2
            continue
        rest.append(argv[i])
        i += 1
    path = rest[0]
    if "--input" in argv:
        log("BODY " + sys.stdin.read())
    if method != "GET":
        sys.exit(0)
    f = os.path.join(d, "api-" + re.sub(r"[/?&=]", "_", path) + ".json")
    if not os.path.exists(f):
        sys.stderr.write("gh: Not Found (HTTP 404)\n")
        sys.exit(1)
    sys.stdout.write(open(f).read())
elif argv[:2] == ["release", "download"]:
    log(" ".join(argv))
    f = os.path.join(d, f"release-{argv[2]}-{opt('-p')}")
    if not os.path.exists(f):
        sys.stderr.write("release not found\n")
        sys.exit(1)
    sys.stdout.write(open(f).read())
else:
    runpy.run_path(os.path.join(HERE, "fake_forgejo_gh.py"), run_name="__main__")
