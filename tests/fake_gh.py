#!/usr/bin/env python3
"""A stand-in for the GitHub CLI (`gh`) in agentctl's tests, served from files in $FAKE_GH_DIR:
  run view <id> --json ...   -> run-<id>.json            run view <id> --log-failed -> run-<id>.log
  run view <id> --log --job <j> -> run-<id>-job-<j>.log
  run download <id> -n <a> -D <dir> -> art-<id>-<a>.txt copied to <dir>/verdict.txt
  run list [--workflow W] [--branch B] ... -> runs.json filtered (W matches workflowName or the workflow file);
                               runs-<owner>_<repo>.json instead when it exists for -R <owner>/<repo>;
                               a W listed in workflows-absent (one per line) -> GitHub's 404 for a workflow file
                               not on the default branch
  api [-X M] [-H h] [-i] <path>   GET -> api-<path with /?&= as _>.json (GitHub's REST shapes); missing -> `HTTP 404`;
                             -i: headers first, an ETag per body, and a 304 for a matching If-None-Match;
                             any other method -> recorded (its --input body as `<pid> BODY <json>`), answers nothing
  api ... -R <repo>          refused, as gh does: `gh api` takes no -R (its path names the repo)
  release download <tag> -p <name> -O -  -> release-<tag>-<name>
  run cancel <id>, run rerun ..., workflow run ... -> recorded only
Every call is appended to calls.log as `<caller pid> <argv>`."""
import json, os, re, shutil, sys

d = os.environ["FAKE_GH_DIR"]
argv = sys.argv[1:]


def log(line):
    with open(os.path.join(d, "calls.log"), "a") as f:
        f.write(f"{os.getppid()} {line}\n")


log(" ".join(argv))


def opt(name):
    return argv[argv.index(name) + 1] if name in argv else None


def emit(path):
    if not os.path.exists(path):
        sys.stderr.write(f"fake gh: no {os.path.basename(path)}\n")
        sys.exit(1)
    sys.stdout.write(open(path).read())


if argv[:1] == ["api"]:
    if "-R" in argv or "--repo" in argv:
        sys.stderr.write("unknown shorthand flag: 'R' in -R\n")
        sys.exit(1)
    method, rest, i, headers, include = "GET", [], 1, [], False
    while i < len(argv):
        if argv[i] in ("-i", "--include"):
            include, i = True, i + 1
            continue
        if argv[i] == "--allow-escape-sequences":
            i += 1
            continue
        if argv[i] in ("-X", "--method", "-H", "--header", "--input", "-f", "-F"):
            if argv[i] in ("-X", "--method"):
                method = argv[i + 1]
            if argv[i] in ("-H", "--header"):
                headers.append(argv[i + 1])
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
    body = open(f).read()
    if "\x1b" in body and "--allow-escape-sequences" not in argv:  # gh 2.8x+: a job log's ANSI colours are refused
        sys.stderr.write("the response contains terminal escape sequences; pass --allow-escape-sequences to output it "
                         "anyway\n")
        sys.exit(1)
    if include:  # GitHub's conditional GET: an ETag per body; a matching If-None-Match is a 304 with no body
        import hashlib
        etag = 'W/"%s"' % hashlib.sha1(body.encode()).hexdigest()
        if any(h.lower().startswith("if-none-match:") and h.split(":", 1)[1].strip() == etag for h in headers):
            sys.stdout.write("HTTP/2.0 304 Not Modified\r\nEtag: %s\r\n\r\n" % etag)
            sys.stderr.write("gh: HTTP 304\n")
            sys.exit(1)
        sys.stdout.write("HTTP/2.0 200 OK\r\nEtag: %s\r\n\r\n" % etag + body)
        sys.exit(0)
    sys.stdout.write(body)
    sys.exit(0)
repo = None
if "-R" in argv:
    i = argv.index("-R")
    repo = argv[i + 1] if i + 1 < len(argv) else None
    argv = argv[:i] + argv[i + 2:]
if argv[:2] == ["release", "download"]:
    f = os.path.join(d, f"release-{argv[2]}-{opt('-p')}")
    if not os.path.exists(f):
        sys.stderr.write("release not found\n")
        sys.exit(1)
    sys.stdout.write(open(f).read())
elif argv[:2] == ["run", "view"]:
    rid = argv[2]
    if "--job" in argv:
        emit(os.path.join(d, f"run-{rid}-job-{opt('--job')}.log"))
    else:
        emit(os.path.join(d, f"run-{rid}.log" if "--log-failed" in argv else f"run-{rid}.json"))
elif argv[:2] == ["run", "download"]:
    rid, name, dest = argv[2], opt("-n"), opt("-D")
    os.makedirs(dest, exist_ok=True)
    shutil.copy(os.path.join(d, f"art-{rid}-{name}.txt"), os.path.join(dest, "verdict.txt"))
elif argv[:2] == ["run", "list"]:
    per_repo = os.path.join(d, f"runs-{repo.replace('/', '_')}.json") if repo else None
    runs = json.load(open(per_repo if per_repo and os.path.exists(per_repo) else os.path.join(d, "runs.json")))
    w, b = opt("--workflow"), opt("--branch")
    absent = os.path.join(d, "workflows-absent")
    if w and os.path.exists(absent) and w in open(absent).read().split():
        sys.stderr.write(f"HTTP 404: workflow {w} not found on the default branch "
                         f"(https://api.github.com/repos/{repo or 'o/r'}/actions/workflows/{w})\n")
        sys.exit(1)
    runs = [r for r in runs if (not w or w in (r.get("workflowName"), r.get("workflowFile"))) and (not b or r.get("headBranch") == b)]
    print(json.dumps(runs[: int(opt("--limit") or 20)]))
elif argv[:2] == ["workflow", "run"]:
    pass  # a dispatch: recorded in calls.log
elif argv[:2] == ["run", "cancel"]:
    print(f"Request to cancel workflow {argv[2]} submitted.")
elif argv[:2] == ["run", "rerun"]:
    pass
else:
    sys.stderr.write(f"fake gh: unsupported {argv}\n")
