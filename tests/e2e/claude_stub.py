#!/usr/bin/env python3
"""A STUB `claude` CLI for the end-to-end escalation scenarios ($AGENTCTL_CLAUDE): never the real one. It answers what
agentctl asks of claude -- `--help`, `--bg <prompt> ...` (a background session), `agents --json --all`, `stop <id>` --
from files in $E2E_CLAUDE_DIR, and logs every call there (calls.jsonl: argv and cwd). A started session does its
scripted work at once: it copies $E2E_CLAUDE_FIX_FROM over $E2E_CLAUDE_FIX_TO (the scenario's fix) and stays `working`
until stopped, leaving an uncommitted agent-notes.txt in its cwd. $E2E_CLAUDE_BG_FAIL makes `--bg` refuse with that text (a launch that fails)."""
import json, os, shutil, sys

DIR = os.environ["E2E_CLAUDE_DIR"]
SESSIONS = os.path.join(DIR, "sessions.json")


def sessions():
    try:
        with open(SESSIONS) as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def save(rows):
    with open(SESSIONS + ".tmp", "w") as f:
        json.dump(rows, f)
    os.replace(SESSIONS + ".tmp", SESSIONS)


def main(argv):
    os.makedirs(DIR, exist_ok=True)
    with open(os.path.join(DIR, "calls.jsonl"), "a") as f:
        f.write(json.dumps({"argv": argv, "cwd": os.getcwd()}) + "\n")
    if argv[:1] == ["--help"]:
        print("Usage: claude [options]\n  --bg  run in the background\n  --permission-mode <mode>")
        return 0
    if argv[:1] == ["--bg"]:
        if os.environ.get("E2E_CLAUDE_BG_FAIL"):
            print(os.environ["E2E_CLAUDE_BG_FAIL"], file=sys.stderr)
            return 1
        rows = sessions()
        sid = f"{0xe2e000 + len(rows) + 1:08x}"
        name = argv[argv.index("-n") + 1] if "-n" in argv else ""
        rows.append({"id": sid, "sessionId": f"{sid}-0000", "name": name, "state": "working", "cwd": os.getcwd()})
        save(rows)
        with open(os.path.join(os.getcwd(), "agent-notes.txt"), "w") as f:  # the work in progress an agent leaves
            f.write("uncommitted notes\n")
        if os.environ.get("E2E_CLAUDE_FIX_FROM"):
            shutil.copyfile(os.environ["E2E_CLAUDE_FIX_FROM"], os.environ["E2E_CLAUDE_FIX_TO"])
        print(f"backgrounded {sid}")
        return 0
    if argv[:1] == ["agents"]:
        print(json.dumps(sessions()))
        return 0
    if argv[:1] == ["stop"] and len(argv) > 1:
        save([dict(r, state="stopped") if r["id"] == argv[1] else r for r in sessions()])
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
