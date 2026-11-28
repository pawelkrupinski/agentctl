#!/usr/bin/env python3
"""A STUB `claude` CLI for the end-to-end escalation scenarios ($AGENTCTL_CLAUDE): never the real one. It answers what
agentctl asks of claude -- `--help`, `--bg <prompt> ...` (a background session), `agents --json --all`, `stop <id>` --
from files in $E2E_CLAUDE_DIR, and logs every call there (calls.jsonl: argv and cwd). A started session does its
scripted work at once: it copies $E2E_CLAUDE_FIX_FROM over $E2E_CLAUDE_FIX_TO (the scenario's fix) and stays `working`
until stopped, leaving an uncommitted agent-notes.txt in its cwd. $E2E_CLAUDE_BG_FAIL makes `--bg` refuse with that text (a launch that fails).

A RED FIXER's session (the red scenarios): $E2E_CLAUDE_VERDICT is the session's final answer (`GAVE UP: ...`, `LANDED
<sha>`): the stub writes a transcript for it (under $HOME/.claude/projects, where agentctl reads it) and the session is
`done` at once. $E2E_CLAUDE_COMMIT ("<file>=<text>") first makes a commit with that file in the session's cwd: an
unlanded fix the fixer did not land."""
import json, os, shutil, subprocess, sys

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


def write_transcript(row):
    """The session's transcript (a prompt, then its final answer) and its row `done`."""
    d = os.path.join(os.environ["HOME"], ".claude", "projects", "e2e")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, row["sessionId"] + ".jsonl"), "w") as f:
        f.write(json.dumps({"type": "user", "message": {"role": "user", "content": "fix the red"}}) + "\n")
        f.write(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": os.environ["E2E_CLAUDE_VERDICT"]}]}}) + "\n")
        f.write(json.dumps({"type": "system", "subtype": "turn_duration"}) + "\n")
    save([dict(r, state="done") if r["id"] == row["id"] else r for r in sessions()])


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
        if os.environ.get("E2E_CLAUDE_COMMIT"):
            rel, _, text = os.environ["E2E_CLAUDE_COMMIT"].partition("=")
            with open(os.path.join(os.getcwd(), rel), "w") as f:
                f.write(text + "\n")
            subprocess.run(["git", "add", rel], check=True)
            subprocess.run(["git", "-c", "user.name=e2e", "-c", "user.email=e2e@e2e", "commit", "-q", "-m", "wip: the fix"],
                           check=True)
        if os.environ.get("E2E_CLAUDE_VERDICT"):
            write_transcript(rows[-1])
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
