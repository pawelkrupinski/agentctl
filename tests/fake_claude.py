#!/usr/bin/env python3
"""A stand-in for the `claude` CLI in agentctl's red-fixer tests, in the background-session shape agentctl uses.

  --help                      a help naming --bg and --permission-mode
  --bg <prompt> ... -n <name> records {argv, cwd, env} to $FAKE_CLAUDE_OUT/<id>.json, starts a detached session process
                              and prints `backgrounded · <id> · <name>`; `--resume <sid>` continues that session under
                              the same id -- or, when its process still runs (a spare), starts a COPY under a new id
  --bg with $FAKE_CLAUDE_BG_FAIL  prints it and exits 1 (a refused launch); with $FAKE_CLAUDE_NO_SESSION it backgrounds
                              a row (state `starting`) whose session never starts
  agents --json --all         the sessions ($FAKE_CLAUDE_OUT/agents/<id>.json rows: id, sessionId, name, cwd, state)
  stop <id>                   ends that session (its row stays, state done, stopped: true)
  _session <id>               (internal) the session: transcript at $AGENTCTL_CLAUDE_PROJECTS/fake/<sessionId>.jsonl,
                              sleeps $FAKE_CLAUDE_SLEEP s, answers $FAKE_CLAUDE_RESULT (default `LANDED abcdef1234`; for
                              an implementer briefed with a diagnosis, $FAKE_CLAUDE_RESULT_IMPLEMENT when set), done
                              (or $FAKE_CLAUDE_END_STATE: `blocked`, `stopped`). $FAKE_CLAUDE_LINGER: its process lives
                              on after the turn (`claude bg-spare`), its row `blocked` / `idle` with that live pid.
                              $FAKE_CLAUDE_STAY_WORKING: after its answer it closes the turn (a `turn_duration`
                              entry) yet its row stays `working`, its process alive (claude's job did so, 2026-11-21).
                              $FAKE_CLAUDE_BG_WAIT=<s>: it first backgrounds a task (`agentctl wait`), ends its turn
                              waiting on it (row `blocked`, live pid, no answer) for <s> s, then the task's notification
                              comes and it answers.
                              $FAKE_CLAUDE_CONTEXT=<n>: its answers carry usage figures, n tokens of context.
                              $FAKE_CLAUDE_SILENT_ONCE=<file>: while <file> exists, the next session consumes it and
                              fails before one message (a transcript of metadata only), its job's detail <file>'s text.
  --bg ... --resume <sid>     of a session whose transcript holds no conversation: claude's refusal -- a row under a
                              new id, `failed`, its job's detail `source session <sid> not found`
  jobs                        $AGENTCTL_CLAUDE_JOBS/<id>/state.json ({state, detail}), as ~/.claude/jobs holds them
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


def write_job(bid, state, detail):
    d = os.path.join(os.environ.get("AGENTCTL_CLAUDE_JOBS") or os.path.join(OUT, "jobs"), bid)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "state.json"), "w") as f:
        json.dump({"state": state, "detail": detail}, f)


def has_conversation(sid):
    try:
        return any(json.loads(l).get("type") in ("user", "assistant") for l in open(transcript(sid)) if l.strip())
    except OSError:
        return False


def say(path, role, text):
    m = {"role": role, "content": [{"type": "text", "text": text}]}
    if role == "assistant" and os.environ.get("FAKE_CLAUDE_CONTEXT"):  # the turn's usage: its context in tokens
        n = int(os.environ["FAKE_CLAUDE_CONTEXT"])
        m["usage"] = {"input_tokens": 1, "cache_creation_input_tokens": 9, "cache_read_input_tokens": n - 10,
                      "output_tokens": 50}
    with open(path, "a") as f:
        f.write(json.dumps({"type": role, "message": m}) + "\n")


try:  # claude refuses to start in a deleted directory
    os.getcwd()
except FileNotFoundError:
    print("The current directory no longer exists (it was deleted or moved). Start Claude Code from an existing "
          "directory.", file=sys.stderr)
    sys.exit(1)
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
    once = os.environ.get("FAKE_CLAUDE_SILENT_ONCE")
    if once and os.path.exists(once):  # it fails before its first message: a transcript of metadata only
        detail = open(once).read().strip()
        os.remove(once)
        with open(path, "a") as f:
            f.write(json.dumps({"type": "custom-title", "sessionId": r["sessionId"]}) + "\n")
        write_job(argv[1], "failed", detail)
        r.update(state="failed", pid=None)
        write_row(r)
        sys.exit(0)
    say(path, "user", r["prompt"])
    say(path, "assistant", "working")
    time.sleep(float(os.environ.get("FAKE_CLAUDE_SLEEP", "0")))
    if os.environ.get("FAKE_CLAUDE_BG_WAIT"):  # `agentctl wait` backgrounded, the turn ended waiting on it
        with open(path, "a") as f:
            f.write(json.dumps({"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "Command running in background with ID: "
                 "bwait1. Output is being written to: /tmp/x/bwait1.output"}]},
                "toolUseResult": {"backgroundTaskId": "bwait1"}}) + "\n")
        say(path, "assistant", "Waiting for my CI run.")
        r = read_row(argv[1])
        r.update(state="blocked", status="idle")
        write_row(r)
        time.sleep(float(os.environ["FAKE_CLAUDE_BG_WAIT"]))
        say(path, "user", "<task-notification>\n<task-id>bwait1</task-id>\n<status>completed</status>\n"
                          "</task-notification>")
        r = read_row(argv[1])
        r.update(state="working", status="busy")
        write_row(r)
    result = os.environ.get("FAKE_CLAUDE_RESULT", "LANDED abcdef1234")
    if os.environ.get("FAKE_CLAUDE_RESULT_IMPLEMENT") and "was DIAGNOSED" in r["prompt"]:  # an implementer's brief
        result = os.environ["FAKE_CLAUDE_RESULT_IMPLEMENT"]
    say(path, "assistant", "Done.\n" + result)
    if os.environ.get("FAKE_CLAUDE_STAY_WORKING"):  # the turn closed in the transcript, the row still `working`
        with open(path, "a") as f:
            f.write(json.dumps({"type": "system", "subtype": "turn_duration", "durationMs": 1000}) + "\n")
        time.sleep(3600)  # until `stop` ends its group
    r = read_row(argv[1])
    if r and not r.get("stopped") and os.environ.get("FAKE_CLAUDE_LINGER"):  # the turn over, the process lives on
        r.update(state="blocked", status="idle")
        write_row(r)
        time.sleep(3600)  # until `stop` ends its group
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
    was, copy = read_row(sid[:8]) if "--resume" in argv else None, False
    if was and was.get("pid") and not was.get("stopped"):  # the session still runs (a spare): claude starts a copy
        try:
            os.kill(was["pid"], 0)
            sid, copy = str(uuid.uuid4()), True
        except OSError:
            pass
    bid = sid[:8]
    os.makedirs(OUT, exist_ok=True)
    tmp = os.path.join(OUT, f".{bid}.json.{os.getpid()}")  # whole or not at all: a reader polls for <id>.json
    with open(tmp, "w") as f:
        json.dump({"argv": argv, "cwd": os.getcwd(), "env": dict(os.environ)}, f)
    os.replace(tmp, os.path.join(OUT, f"{bid}.json"))
    if "--resume" in argv and not has_conversation(argv[argv.index("--resume") + 1]):  # claude refuses it
        src, sid = argv[argv.index("--resume") + 1], str(uuid.uuid4())
        os.replace(os.path.join(OUT, f"{bid}.json"), os.path.join(OUT, f"{sid[:8]}.json"))
        write_row({"id": sid[:8], "sessionId": sid, "name": name, "cwd": os.getcwd(), "kind": "background",
                   "state": "failed", "prompt": prompt})
        write_job(sid[:8], "failed", f"source session {src} not found")
        print(f"backgrounded · {sid[:8]} · {name}")
        sys.exit(0)
    if os.environ.get("FAKE_CLAUDE_NO_SESSION"):  # backgrounded, and its session never starts (no id, no transcript)
        write_row({"id": bid, "name": name, "cwd": os.getcwd(), "kind": "background", "state": "starting",
                   "prompt": prompt})
        print(f"backgrounded · {bid} · {name}")
        sys.exit(0)
    write_row({"id": bid, "sessionId": sid, "name": name, "cwd": os.getcwd(), "kind": "background", "state": "working",
               "prompt": prompt})
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "_session", bid], start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"backgrounded · {bid} · {name}" + (f" (a copy of {was['id']}, which is still running)" if copy else ""))
    sys.exit(0)
print("fake claude: unsupported " + " ".join(argv), file=sys.stderr)
sys.exit(2)
