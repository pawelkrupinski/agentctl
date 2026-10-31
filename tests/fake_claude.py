#!/usr/bin/env python3
"""A stand-in for the `claude` CLI in agentctl's red-fixer tests, in the background-session shape agentctl uses.

  --help                      a help naming --bg and --permission-mode
  --bg <prompt> ... -n <name> records {argv, cwd, env} to $FAKE_CLAUDE_OUT/<id>.json, starts a detached session process
                              and prints `backgrounded · <id> · <name>`
  --bg with $FAKE_CLAUDE_BG_FAIL  prints it and exits 1 (a refused launch); with $FAKE_CLAUDE_NO_SESSION it backgrounds
                              a row (state `starting`) whose session never starts
  agents --json --all         the sessions ($FAKE_CLAUDE_OUT/agents/<id>.json rows: id, sessionId, name, cwd, state)
  stop <id>                   ends that session (its row stays, state done, stopped: true)
  _session <id>               (internal) the session: transcript at $AGENTCTL_CLAUDE_PROJECTS/fake/<sessionId>.jsonl,
                              sleeps $FAKE_CLAUDE_SLEEP s, answers $FAKE_CLAUDE_RESULT (default `LANDED abcdef1234`; for
                              an implementer briefed with a diagnosis, $FAKE_CLAUDE_RESULT_IMPLEMENT when set), done
                              (or $FAKE_CLAUDE_END_STATE: `blocked`, `stopped`).
"""
import json, os, signal, subprocess, sys, time, uuid

OUT = os.environ.get("FAKE_CLAUDE_OUT") or "/tmp/fake-claude"
AGENTS = os.path.join(OUT, "agents")


def row_path(bid):
    return os.path.join(AGENTS, f"{bid}.json")


def read_row(bid):
    try:
        with open(row_path(bid)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_row(row):
    os.makedirs(AGENTS, exist_ok=True)
    tmp = row_path(row["id"]) + f".{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(row, f)
    os.replace(tmp, row_path(row["id"]))


def transcript(sid):
    d = os.path.join(os.environ.get("AGENTCTL_CLAUDE_PROJECTS") or os.path.join(OUT, "projects"), "fake")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{sid}.jsonl")


def say(path, role, text):
    with open(path, "a") as f:
        f.write(json.dumps({"type": role, "message": {"role": role, "content": [{"type": "text", "text": text}]}}) + "\n")


argv = sys.argv[1:]
if "--help" in argv:
    print("Usage: claude [options]\n  --bg, --background\n  --permission-mode <mode>\n  --permission-prompts <target>")
    sys.exit(0)
if argv[:1] == ["agents"]:
    rows = []
    for n in sorted(os.listdir(AGENTS)) if os.path.isdir(AGENTS) else []:
        r = read_row(n[:-5]) if n.endswith(".json") else None
        if r:
            rows.append(r)
    print(json.dumps(rows))
    sys.exit(0)
if argv[:1] in (["stop"], ["kill"]):
    r = read_row(argv[1])
    if r:
        try:
            os.killpg(r["pid"], signal.SIGTERM)
        except (OSError, KeyError, TypeError):
            pass
        r.update(state="done", stopped=True, pid=None)
        write_row(r)
        print(f"stopped {argv[1]}")
    sys.exit(0)
if argv[:1] == ["_session"]:
    r = read_row(argv[1])
    if r.get("stopped"):
        sys.exit(0)
    r["pid"] = os.getpid()  # the leader of its own session: `stop` ends its group
    write_row(r)
    path = transcript(r["sessionId"])
    say(path, "user", r["prompt"])
    say(path, "assistant", "working")
    time.sleep(float(os.environ.get("FAKE_CLAUDE_SLEEP", "0")))
    result = os.environ.get("FAKE_CLAUDE_RESULT", "LANDED abcdef1234")
    if os.environ.get("FAKE_CLAUDE_RESULT_IMPLEMENT") and "was DIAGNOSED" in r["prompt"]:  # an implementer's brief
        result = os.environ["FAKE_CLAUDE_RESULT_IMPLEMENT"]
    say(path, "assistant", "Done.\n" + result)
    r = read_row(argv[1])
    if r and not r.get("stopped"):  # $FAKE_CLAUDE_END_STATE: how its turn ends (`blocked`: waiting for input, no pid)
        r.update(state=os.environ.get("FAKE_CLAUDE_END_STATE") or "done", pid=None)
        write_row(r)
    sys.exit(0)
if "--bg" in argv and os.environ.get("FAKE_CLAUDE_BG_FAIL"):  # the launch refused (e.g. an untrusted workspace)
    print(os.environ["FAKE_CLAUDE_BG_FAIL"], file=sys.stderr)
    sys.exit(1)
if "--bg" in argv:
    prompt = argv[argv.index("--bg") + 1]
    name = argv[argv.index("-n") + 1] if "-n" in argv else "session"
    sid = argv[argv.index("--resume") + 1] if "--resume" in argv else str(uuid.uuid4())
    bid = sid[:8]
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, f"{bid}.json"), "w") as f:
        json.dump({"argv": argv, "cwd": os.getcwd(), "env": dict(os.environ)}, f)
    if os.environ.get("FAKE_CLAUDE_NO_SESSION"):  # backgrounded, and its session never starts (no id, no transcript)
        write_row({"id": bid, "name": name, "cwd": os.getcwd(), "kind": "background", "state": "starting",
                   "prompt": prompt})
        print(f"backgrounded · {bid} · {name}")
        sys.exit(0)
    write_row({"id": bid, "sessionId": sid, "name": name, "cwd": os.getcwd(), "kind": "background", "state": "working",
               "prompt": prompt})
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "_session", bid], start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"backgrounded · {bid} · {name}")
    sys.exit(0)
print("fake claude: unsupported " + " ".join(argv), file=sys.stderr)
sys.exit(2)
