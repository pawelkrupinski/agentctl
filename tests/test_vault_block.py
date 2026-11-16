"""A BLOCK AGENTCTL CAN CHECK ITSELF (2026-11-16, Order independence #77 [ios]). Its fixer's fix waited on commit
signing while the `movies` vault was locked; the red sat "RED BLOCKED ON OWNER: WAITING ON YOU: run `! secrets unlock
movies` ..." from 03:49 as an OWNER DECISION. The owner unlocked the vault at 04:50Z, yet the block stayed open, and
in his status line, until he hand-ran `agentctl decide` at 05:01Z. The ask's only condition was one agentctl can
check: the secrets daemon logs every unlock. Now such a red waits on ONE vault lease per vault (block/cause/vault-<ns>)
whose probe is that unlock: the owner is told once for every red it holds, and the unlock resumes them all by itself --
offered again, the fixer briefed "the vault was unlocked at T" -- and its WAITING ON YOU alert closes."""
import json, os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from test_owner_ask import AskBase  # noqa: E402

DIAG = ("DIAGNOSED: the first app launch on CI's freshly booted simulator times out; the fix is in stash e94585c437a9 "
        "but can't be committed, because signing is refused while the vault is locked; decide: run `! secrets unlock "
        "movies`, or allow agents to make unsigned commits, so the fix can be committed with `Fixes-Red: 77` and landed")
BLOCKED = ("BLOCKED ON OWNER: `! secrets unlock movies` (or allow unsigned agent commits): commit signing is refused "
           "while the vault is locked")


class VaultBase(AskBase):
    def setUp(self):
        super().setUp()
        self.log = os.path.join(self.tmp.name, "secretsd.err.log")
        open(self.log, "w").close()
        self.saved_env["AGENTCTL_SECRETS_LOG"] = os.environ.get("AGENTCTL_SECRETS_LOG")
        os.environ["AGENTCTL_SECRETS_LOG"] = self.log
        saved = A.Store
        A.Store = lambda *a, **k: self.st
        self.addCleanup(setattr, A, "Store", saved)

    def ios_red(self, key="oi77", n=77):
        return self.red(key=key, workflow="Order independence", job="ios", kind="test",
                        last_run={"number": n, "databaseId": 9000 + n})

    def unlock(self, ns="movies+ssh", pid=None, at=None):
        stamp = A.iso_at(self.t[0] if at is None else at)
        with open(self.log, "a") as f:
            f.write(f"[{stamp}] unlocked {ns} for pid {pid or os.getpid()} (/x/claude/versions/2.1.325)\n")

    def tick_blocks(self):
        self.t[0] += A.BLOCK_PROBE_EVERY + 1
        return A.red_renew_blocks(self.st, self.t[0])

    def step(self, it):
        return A.red_step(self.st, self.st.item("red", it["id"]), None, None, {}, None, "pawelkrupinski/movies",
                          self.t[0])

    def owner_told(self):
        return [e for e in self.events() if "WAITING ON YOU" in str((e.get("refs") or {}).get("verdict"))]


class VaultAskTest(VaultBase):
    def test_a_diagnosed_vault_ask_waits_on_the_vault_lease_not_on_a_decision(self):
        it, _acts = self.launch(self.ios_red(), outcome=DIAG)
        self.assertEqual(it["state"], "blocked-external")
        blk = A.red_block_of(it, self.st)
        self.assertEqual(blk["name"], "block/cause/vault-movies")
        self.assertFalse(blk["meta"].get("decision"), "a vault ask is no owner decision")
        self.assertEqual(blk["meta"]["probe"]["kind"], "vault_unlocked")
        _text, short = self.view(it)
        self.assertTrue(short.startswith("WAITING ON YOU: run `! secrets unlock movies`"), short)

    def test_a_blocked_on_owner_vault_verdict_waits_too_never_re_offered_every_turn(self):
        it, _acts = self.launch(self.ios_red(), outcome=BLOCKED)
        self.assertEqual(it["state"], "blocked-external", "#77 was offered back to its owner 4 times in 4 minutes")
        self.assertEqual(A.red_block_of(it, self.st)["name"], "block/cause/vault-movies")

    def test_still_locked_it_stays_blocked(self):
        it, _acts = self.launch(self.ios_red(), outcome=DIAG)
        self.unlock(ns="bitcashier+ssh")  # another vault's unlock is not this one
        self.unlock(at=self.t[0] - 3600)  # nor an unlock from before the block
        self.unlock(pid=2 ** 22 + 7)  # nor one whose session has ended
        self.assertEqual(self.tick_blocks(), [])
        self.step(it)
        it = self.st.item("red", it["id"])
        self.assertEqual(it["state"], "blocked-external")
        self.assertIsNotNone(A.red_block_of(it, self.st))

    def test_the_vault_unlocked_resumes_it_with_a_note_and_closes_its_alert(self):
        it, _acts = self.launch(self.ios_red(), outcome=DIAG)
        A.red_owner_reminders(self.st, self.t[0])
        told = self.owner_told()
        self.assertEqual(len(told), 1)
        line = told[0]["refs"]["verdict"]
        eid = A.alert_write({"id": "20261116T034801000000-ev1-red-ask", "at": self.t[0], "command": "red", "cwd": "/",
                             "final": True, "exit": 1, "verdict": line, "verdicts": [line], "tail": [],
                             "item_type": "red", "item_id": it["id"]})
        self.unlock()
        out = self.tick_blocks()
        self.assertTrue(any("RECOVERED" in x for x in out), out)
        self.step(it)
        it = self.st.item("red", it["id"])
        self.assertEqual(it["state"], "offered", "re-offered to a fixer, as a decide does")
        dec = it["data"]["decision"]
        self.assertEqual(dec["status"], "in-progress")
        self.assertIn("the movies vault was unlocked at", dec["answer"])
        self.assertEqual(dec["by"], "agentctl")
        self.assertIsNone(it["data"].get("ask"))
        u = {"workflow": "Order independence", "job": "ios", "kind": "test", "line": "",
             "run": {"number": 77, "databaseId": 9077}, "decision": dec}
        self.assertIn("the movies vault was unlocked at", A.fixer_prompt(u, "/w", "the brief"))
        self.assertNotIn("WAITING ON YOU", self.view(it)[1])
        A.reconcile_alerts(self.st, force=True)
        self.assertTrue(A.alert_acked(A.inbox_dir(), eid), "the owner's WAITING ON YOU alert closes with the block")

    def test_unlocked_before_the_verdict_arrives_it_is_offered_at_once(self):
        it = self.ios_red()
        self.unlock(at=self.t[0] + 60)  # the owner unlocked while the fixer was still ending its turn
        it, _acts = self.launch(it, outcome=DIAG, minutes=1)
        self.assertEqual(it["state"], "offered")
        self.assertIn("vault was unlocked", it["data"]["decision"]["answer"])


class OneItemTest(VaultBase):
    def test_n_reds_on_the_locked_vault_tell_the_owner_once_and_resume_together(self):
        reds = [self.launch(self.ios_red(key=f"r{i}", n=70 + i), outcome=DIAG, minutes=1)[0] for i in range(3)]
        names = {A.red_block_of(r, self.st)["name"] for r in reds}
        self.assertEqual(names, {"block/cause/vault-movies"}, "ONE lease for the vault")
        A.red_owner_reminders(self.st, self.t[0])
        told = self.owner_told()
        self.assertEqual(len(told), 1, [e["refs"]["verdict"] for e in told])
        for n in (70, 71, 72):
            self.assertIn(f"Order independence #{n}", told[0]["refs"]["verdict"])
        late = self.launch(self.ios_red(key="a0", n=69), outcome=BLOCKED, minutes=1)[0]  # joins later, sorting first
        A.red_owner_reminders(self.st, self.t[0])
        self.assertEqual(len(self.owner_told()), 1, "a red joining the vault's wait is no second telling")
        self.unlock()
        self.tick_blocks()
        for r in reds + [late]:
            self.step(r)
            self.assertEqual(self.st.item("red", r["id"])["state"], "offered")


class LaptopFactTest(VaultBase):
    """The reconciler ticks on ci-runner, which has no secrets daemon: the laptop's sync publishes what its log says,
    and the probe there reads that."""

    def test_the_probe_reads_the_laptops_word_where_there_is_no_log(self):
        it, _acts = self.launch(self.ios_red(), outcome=DIAG)
        args = A.red_block_of(it, self.st)["meta"]["probe"]["args"]
        self.unlock()
        A.vault_watch(self.st, now=self.t[0])  # the laptop's sync step
        os.environ["AGENTCTL_SECRETS_LOG"] = os.path.join(self.tmp.name, "absent.log")  # ci-runner
        r = A.run_probe("vault_unlocked", args)
        self.assertTrue(r.ok, r.detail)
        fact = json.loads(self.st.meta_get("vault:movies"))
        self.assertGreaterEqual(fact["unlocked_at"], args["since"])

    def test_no_word_from_the_laptop_cannot_tell(self):
        it, _acts = self.launch(self.ios_red(), outcome=DIAG)
        args = A.red_block_of(it, self.st)["meta"]["probe"]["args"]
        os.environ["AGENTCTL_SECRETS_LOG"] = os.path.join(self.tmp.name, "absent.log")
        self.assertIsNone(A.run_probe("vault_unlocked", args).ok)
        A.vault_watch(self.st, now=self.t[0])  # nothing published: no log on this host either
        self.assertIsNone(A.run_probe("vault_unlocked", args).ok)


if __name__ == "__main__":
    unittest.main()
