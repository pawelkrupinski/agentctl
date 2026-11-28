"""THE POLICY REGISTRY (policy.py POLICIES; 2026-11-20, the owner: "fix agentctl issues structurally" -- 9 closed issues
were a rule nobody owned, the latest an invented "ask the agent, reject after 24 h"). A LINT over lib/agentctl/:

- every hold kind a change records (note_hold) is a row of THE HOLD TABLE, and every row's policy is registered;
- every queue reject (queue_verdict ... "rejected") and every gate wait (set_change ... "waiting") names its policy
  with a `# policy: <id>` line just above it (or on it);
- every unapproved rule that is not a milder form of a mechanism outcome is asked through policy_blocks("<id>", ...)
  at each site -- so it only WARNS until the owner approves it;
- no registry row is stale: each one is used.
And policy_blocks itself: an approved rule blocks; an unapproved one does not, and records its warning once."""
import os, re, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402

LIB = os.path.join(os.path.dirname(HERE), "lib", "agentctl")


def sources():
    for name in sorted(os.listdir(LIB)):
        if name.endswith(".py"):
            with open(os.path.join(LIB, name)) as f:
                yield name, f.read().splitlines()


def marker(lines, i):
    """The `# policy: <id>` of the call at line i: on it, or on one of the three lines above."""
    for j in range(i, max(-1, i - 4), -1):
        m = re.search(r"#\s*policy:\s*([a-z0-9-]+)", lines[j])
        if m:
            return m.group(1)
    return None


class PolicyLintTest(unittest.TestCase):
    def test_every_recorded_hold_kind_is_in_the_hold_table(self):
        bad = []
        for name, lines in sources():
            for i, line in enumerate(lines):
                for kind in re.findall(r'note_hold\(st, \w+, "([a-z-]+)"', line):
                    if kind not in A.HOLDS:
                        bad.append(f"{name}:{i + 1}: hold kind {kind!r}")
        for kind in ("regen", "retry", "governor", "batch", "starting"):  # the kinds built outside note_hold's literal
            self.assertIn(kind, A.HOLDS)
        self.assertEqual(bad, [], "register each in policy.py HOLDS (label, bound, recovery, policy)")

    def test_every_reject_and_gate_wait_names_a_registered_policy(self):
        bad, used = [], set()
        for name, lines in sources():
            for i, line in enumerate(lines):
                if re.search(r'queue_verdict\(d, \w+, "rejected"', line) or re.search(r'set_change\(d, \w+, "waiting"', line):
                    pid = marker(lines, i)
                    if pid is None:
                        bad.append(f"{name}:{i + 1}: no `# policy: <id>`")
                    elif pid not in A.POLICIES:
                        bad.append(f"{name}:{i + 1}: policy {pid!r} is not registered")
                    used.add(pid)
                m = re.search(r"#\s*policy:\s*([a-z0-9-]+)", line)
                if m:
                    used.add(m.group(1))
        self.assertEqual(bad, [], "every rule that rejects or holds a change is a row of policy.py POLICIES")
        self.used = used

    def test_an_unapproved_rule_is_asked_at_every_site_so_it_only_warns(self):
        asked = set()
        for name, lines in sources():
            for i, line in enumerate(lines):
                asked |= set(re.findall(r'policy_blocks\("([a-z0-9-]+)"', line))
                pid = re.search(r"#\s*policy:\s*([a-z0-9-]+)", line)
                if pid and pid.group(1) in A.POLICIES and not A.POLICIES[pid.group(1)].approved \
                        and not A.POLICIES[pid.group(1)].softens:
                    window = "\n".join(lines[max(0, i - 3):i + 3])
                    self.assertIn(f'policy_blocks("{pid.group(1)}"', window,
                                  f"{name}:{i + 1}: unapproved rule {pid.group(1)} acts without asking policy_blocks")
        for p in A.unapproved_policies():
            if not p.softens:
                self.assertIn(p.id, asked, f"unapproved rule {p.id} is never asked through policy_blocks")

    def test_no_registry_row_is_stale(self):
        text = "\n".join("\n".join(lines) for _n, lines in sources())
        for pid in A.POLICIES:
            hits = (re.search(rf"#\s*policy:\s*{re.escape(pid)}\b", text) or f'policy_blocks("{pid}"' in text
                    or any(h.policy == pid for h in A.HOLDS.values()))
            self.assertTrue(hits, f"policy {pid} is used nowhere: strike its row")


class OwnerDecisionsTest(unittest.TestCase):
    def test_the_owners_2026_11_20_decisions_are_recorded(self):
        """"1 approve, 2 drop the reject 3 approve and enforce for gate reds 4 confirm" (the owner, 2026-11-20)."""
        for pid, words in (("regen", "1 approve"), ("fix-replay", "3 approve and enforce for gate reds"),
                           ("older-batch", "4 confirm")):
            self.assertTrue(A.POLICIES[pid].approved, pid)
            self.assertIn("the owner, 2026-11-20", A.POLICIES[pid].decided, pid)
            self.assertIn(words, A.POLICIES[pid].decided, pid)
        self.assertIn("2 drop the reject", A.POLICIES["regen"].decided)
        self.assertNotIn("regen-expiry", A.POLICIES, "2: the 24-h reject is struck, not left unapproved")
        self.assertFalse(A.POLICIES["fix-replay-outside"].approved, "3: reds outside the gate stay warn-only")
        self.assertEqual(A.HOLDS["regen"].recovery, "recover", "2: an overdue regen hold recovers")


class CrashLoopHoldTest(unittest.TestCase):
    def test_the_crash_loop_relaunch_hold_is_a_registered_owner_approved_enforced_rule(self):
        """THE CRASH_LOOP relaunch hold (770c75a; the owner, 2026-11-27): in the registry, approved, so it blocks."""
        p = A.POLICIES["crash-loop-hold"]
        self.assertEqual(p.effect, "hold")
        self.assertTrue(p.approved)
        self.assertIn("the owner, 2026-11-27", p.decided)
        self.assertFalse(p.mechanism)
        st = A.Store(path=os.path.join(tempfile.mkdtemp(prefix="agentctl-policy-"), "store.db"), clock=lambda: 1000.0)
        self.assertTrue(A.policy_blocks("crash-loop-hold", st, said={}), "approved: enforced, never warn-only")


class PolicyBlocksTest(unittest.TestCase):
    def test_an_approved_rule_blocks_and_an_unapproved_one_warns_once(self):
        t = [1000.0]
        st = A.Store(path=os.path.join(tempfile.mkdtemp(prefix="agentctl-policy-"), "store.db"), clock=lambda: t[0])
        st.put_item("change", "c1", "queued", {"worktree": "/w"})
        said: dict = {}  # the warnings recorded (Caches.policy_said)
        self.assertTrue(A.policy_blocks("overlap", st, "c1", said=said))
        unapproved = next(p for p in A.unapproved_policies() if not p.softens)
        self.assertFalse(A.policy_blocks(unapproved.id, st, "c1", "a test", said=said))
        self.assertFalse(A.policy_blocks(unapproved.id, st, "c1", "a test", said=said))
        evs = [e for e in st.item_events("change", "c1") if e["kind"] == "policy.unapproved"]
        self.assertEqual(len(evs), 1, "its warning is recorded once per change")
        self.assertIn(f"POLICY UNAPPROVED {unapproved.id}", evs[0]["text"])
        with self.assertRaises(ValueError):
            A.policy_blocks("no-such-rule", st, "c1", said={})


if __name__ == "__main__":
    unittest.main()
