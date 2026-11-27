#!/usr/bin/env python3
"""THE E2E LAYER'S MUTATION PROOF: each scenario FAILS when the behaviour it holds is broken (the owner, 2026-11-27).

  python3 tests/e2e/mutations.py [SCENARIO...]

For each scenario, a copy of this tree with ONE mutation applied (an exact text replacement in one file, which must
match once) runs that scenario through tests/e2e/run.py; the proof holds when it fails. The unmutated tree passing is
the layer's own run. Exit 0 when every mutation was caught. Slow (one scenario run each): run it when a scenario or the
code a mutation names changes, never as a push step."""
import os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.dirname(os.path.dirname(HERE))

# scenario: (file, the text, its mutation, what it breaks)
MUTATIONS = {
    "HappyPathTest": (
        "lib/agentctl/store.py",
        'found(*a, event={"actor": self._actor, **(event or {})}, **k)',
        "found(*a, event=event, **k)",
        "the process's actor is no longer stamped on the moves it writes"),
    "RedGateTest": (
        "lib/agentctl/queuediff.py",
        "kr = None if landing.gate_fix or landing.tools_only_known_red else known",
        "kr = None if landing.gate_fix else known",
        "known-red-tools off: a tools-only change failing as main fails waits behind the red"),
    "StoreOutageTest": (
        "lib/agentctl/syncsteps.py",
        "        try:\n            st.submit(e)\n        except StoreUnavailable:\n            break",
        "        try:\n            continue\n        except StoreUnavailable:\n            break",
        "the sync never re-submits the outbox"),
    "SkewOldClientTest": (
        "lib/agentctl/changesubmit.py",
        'missing = [k for k in ("worktree", "head", "commits") if not body.get(k)]',
        'missing = [k for k in ("worktree", "head", "commits", "submitted_by") if not body.get(k)]',
        "the server refuses a submission an older client makes (a field only a newer one would send)"),
    "SkewOldServerTest": (
        "lib/agentctl/eventreader.py",
        "    except StoreLacksCall:  # a store before the call: its whole feed and backfill, merged here",
        "    except KeyError:  # a store before the call: its whole feed and backfill, merged here",
        "history no longer falls back for a store that lacks trace_events"),
    "MissingCommitTest": (
        "lib/agentctl/redland.py",
        "        if r.returncode != 0:\n            return None\n        _production[c]",
        "        if r.returncode != 0:\n            raise RuntimeError(r.stderr)\n        _production[c]",
        "the red rule raises on a commit the clone lacks (the 2026-11-26 dead tick)"),
    "SlowLinkTest": (
        "lib/agentctl/remotestore.py",
        'return json.loads(open_within(req, timeout or self.timeout) or b"null")',
        'with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:\n'
        '                return json.loads(r.read() or b"null")',
        "a store call bounded per socket read, not in all"),
}


def mutated_tree(rel, old, new):
    """A copy of this tree (no .git) with `old` replaced by `new` in rel: its root."""
    root = tempfile.mkdtemp(prefix="agentctl-e2e-mut-")
    tree = os.path.join(root, "bin")
    shutil.copytree(BIN, tree, ignore=shutil.ignore_patterns(".git", "__pycache__"), symlinks=True)
    p = os.path.join(tree, rel)
    with open(p) as f:
        text = f.read()
    if text.count(old) != 1:
        raise SystemExit(f"mutation of {rel}: its text matches {text.count(old)} times, not once (update MUTATIONS)")
    with open(p, "w") as f:
        f.write(text.replace(old, new))
    return root, tree


def main(argv):
    names = argv or list(MUTATIONS)
    caught = {}
    for name in names:
        rel, old, new, why = MUTATIONS[name]
        root, tree = mutated_tree(rel, old, new)
        try:
            env = dict(os.environ, AGENTCTL_E2E_HISTORY=BIN)  # the old revisions come from this checkout's history
            r = subprocess.run([sys.executable, os.path.join(tree, "tests", "e2e", "run.py"), name], cwd=tree,
                               env=env, capture_output=True, text=True, timeout=400)
        finally:
            shutil.rmtree(root, ignore_errors=True)
        caught[name] = r.returncode != 0
        print(f"{'CAUGHT' if caught[name] else 'MISSED'} {name}: {why} ({rel})", flush=True)
        if not caught[name]:
            print(r.stdout[-3000:])
    missed = [n for n, c in caught.items() if not c]
    print("every mutation caught" if not missed else f"MISSED: {', '.join(missed)}")
    return 1 if missed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
