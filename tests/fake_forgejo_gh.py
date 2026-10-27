#!/usr/bin/env python3
"""A stand-in for infra/bin/forgejo-gh in agentctl's tests, served from files in $FAKE_GH_DIR:
  run view <id> --json ...   -> run-<id>.json            run view <id> --log-failed -> run-<id>.log
  run view <id> --log --job <j> -> run-<id>-job-<j>.log
  run download <id> -n <a> -D <dir> -> art-<id>-<a>.txt copied to <dir>/verdict.txt
  run list [--workflow W] [--branch B] ... -> runs.json filtered (W matches workflowName or the workflow file)
  api <path>                 -> api-<path with / as _>.json
  run cancel <id>, workflow run ..., api -X <METHOD> <path> -> recorded only
Every call is appended to calls.log as `<caller pid> <argv>`."""
import json, os, re, shutil, sys

d = os.environ["FAKE_GH_DIR"]
argv = sys.argv[1:]
with open(os.path.join(d, "calls.log"), "a") as f:
    f.write(f"{os.getppid()} {' '.join(argv)}\n")
if "-R" in argv:
    i = argv.index("-R")
    argv = argv[:i] + argv[i + 2:]


def opt(name):
    return argv[argv.index(name) + 1] if name in argv else None


def emit(path):
    if not os.path.exists(path):
        sys.stderr.write(f"fake gh: no {os.path.basename(path)}\n")
        sys.exit(1)
    sys.stdout.write(open(path).read())


if argv[:2] == ["run", "view"]:
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
    runs = json.load(open(os.path.join(d, "runs.json")))
    w, b = opt("--workflow"), opt("--branch")
    runs = [r for r in runs if (not w or w in (r.get("workflowName"), r.get("workflowFile"))) and (not b or r.get("headBranch") == b)]
    print(json.dumps(runs[: int(opt("--limit") or 20)]))
elif argv[:2] == ["workflow", "run"]:
    pass  # a dispatch: recorded in calls.log
elif argv[:2] == ["run", "cancel"]:
    print(f"Request to cancel workflow {argv[2]} submitted.")
elif argv[:1] == ["api"] and "-X" in argv:
    pass  # a write (e.g. DELETE a branch): recorded in calls.log, answers nothing
elif argv[:1] == ["api"]:
    emit(os.path.join(d, "api-" + re.sub(r"[/?&=]", "_", argv[1]) + ".json"))
else:
    sys.stderr.write(f"fake gh: unsupported {argv}\n")
    sys.exit(2)
