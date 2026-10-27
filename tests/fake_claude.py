#!/usr/bin/env python3
"""A stand-in for the `claude` CLI in agentctl's red-fixer tests. `-p --help` prints a help naming --permission-mode.
Otherwise it records {argv, cwd, env} to $FAKE_CLAUDE_OUT/<pid>.json, sleeps $FAKE_CLAUDE_SLEEP seconds, and prints
stream-json lines whose final `result` is $FAKE_CLAUDE_RESULT (default `LANDED abcdef1234`), or for an implementer
briefed with a diagnosis, $FAKE_CLAUDE_RESULT_IMPLEMENT when set."""
import json, os, sys, time

if "--help" in sys.argv:
    print("Usage: claude [options]\n  --permission-mode <mode>\n  --permission-prompts <target>")
    sys.exit(0)
out = os.environ.get("FAKE_CLAUDE_OUT")
if out:
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, f"{os.getpid()}.json"), "w") as f:
        json.dump({"argv": sys.argv[1:], "cwd": os.getcwd(), "env": dict(os.environ)}, f)
time.sleep(float(os.environ.get("FAKE_CLAUDE_SLEEP", "0")))
print(json.dumps({"type": "system", "subtype": "init"}))
print(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "working"}]}}))
result = os.environ.get("FAKE_CLAUDE_RESULT", "LANDED abcdef1234")
if os.environ.get("FAKE_CLAUDE_RESULT_IMPLEMENT") and "was DIAGNOSED" in " ".join(sys.argv):  # an implementer's brief
    result = os.environ["FAKE_CLAUDE_RESULT_IMPLEMENT"]
print(json.dumps({"type": "result", "result": "Done.\n" + result}))
