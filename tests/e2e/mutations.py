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
        "lib/agentctl/gitrepo.py",
        "    return [p for p in r.stdout.splitlines() if p] if r.returncode == 0 else None\n\n\ndef commit_message",
        "    if r.returncode != 0:\n        raise RuntimeError(r.stderr)\n    return [p for p in r.stdout.splitlines() if p]\n\n\ndef commit_message",
        "the red rule raises on a commit the clone lacks (the 2026-11-26 dead tick)"),
    "EscalationTest": (
        "lib/agentctl/conditions.py",
        "        if due:\n            _escalate_overdue(st, it, t, why)",
        "        if True:\n            _escalate_overdue(st, it, t, why)",
        "the server escalates an agent-routed condition to the owner while its agent works"),
    "VendorAutoSyncTest": (
        "lib/agentctl/vendordrift.py",
        '    if state == "idle":\n        auto.start()\n',
        '    if state == "idle":\n        pass\n',
        "a mechanical vendor drift is no longer synced by the script: it waits for an agent (or nobody)"),
    "SlowLinkTest": (
        "lib/agentctl/remotestore.py",
        'return json.loads(open_within(req, timeout or self.timeout) or b"null")',
        'with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:\n'
        '                return json.loads(r.read() or b"null")',
        "a store call bounded per socket read, not in all"),
    "RedFixerLandedTest": (
        "lib/agentctl/fixerlife.py",
        'rc = 0 if verdict.startswith("LANDED") else 1',
        "rc = 1",
        "a fixer that landed is judged a give-up: its worktree is archived and retired"),
    "RedFixerGaveUpTest": (
        "lib/agentctl/fixerlife.py",
        "end_fixer_tree(self.wt, bool(rc), verdict[:200])",
        "end_fixer_tree(self.wt, False, verdict[:200])",
        "a fixer that gave up leaves its worktree behind, not archived and retired"),
    "RedFixerUnlandedTest": (
        "lib/agentctl/worktreelife.py",
        "if state and not state.unmerged:",
        "if state:",
        "a give-up's worktree holding unlanded commits is retired with them"),
    "RedFixerContinuesTest": (
        "lib/agentctl/fixerland.py",
        "self.cont = None if resume else previous_fixer_tree(self.u)",
        "self.cont = None",
        "a fresh fixer starts off main instead of going on in the tree holding the previous fixer's unlanded commits"),
}


def stale_patterns(mutations=None, root=BIN):
    """Every mutation whose text does not match EXACTLY ONCE in its file, as (scenario, file, match count): a stale
    pattern would prove nothing, so the runner refuses to run while there is one."""
    stale = []
    for name, (rel, old, _new, _why) in (mutations or MUTATIONS).items():
        with open(os.path.join(root, rel)) as f:
            count = f.read().count(old)
        if count != 1:
            stale.append((name, rel, count))
    return stale


def mutated_tree(rel, old, new):
    """A copy of this tree (no .git) with `old` replaced by `new` in rel: its root."""
    root = tempfile.mkdtemp(prefix="agentctl-e2e-mut-")
    tree = os.path.join(root, "bin")
    shutil.copytree(BIN, tree, ignore=shutil.ignore_patterns(".git", "__pycache__"), symlinks=True)
    p = os.path.join(tree, rel)
    with open(p) as f:
        text = f.read()
    with open(p, "w") as f:
        f.write(text.replace(old, new))
    return root, tree


def main(argv):
    names = argv or list(MUTATIONS)
    stale = stale_patterns({n: MUTATIONS[n] for n in names})
    for name, rel, count in stale:
        print(f"STALE {name}: its text matches {count} times in {rel}, not once (update MUTATIONS)")
    if stale:
        return 1
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
