"""A VAULT ASK IS ONE OWNER ITEM PER VAULT (2026-11-16, Order independence #77 [ios]). Its fixer's fix waited on commit
signing while the `movies` vault was locked; it ended BLOCKED ON OWNER five turns in four minutes, then sat as an owner
decision until the owner hand-ran `agentctl decide` at 05:01Z. Now such a red waits on ONE lease per vault
(block/cause/vault-<ns>): the owner is told once for every red it holds, with the one line that resumes them all,
`agentctl decide vault-<ns> here`. No unlock resumes them by itself (review round 3): an unlock is pinned to ONE session
anchor and signing asks Touch ID per anchor, so the owner's unlock in his own session never lets a fixer sign; the
resumed fixer probes its own signing first, while the owner is at the machine."""
import json, os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from test_owner_ask import AskBase  # noqa: E402

DIAG = ("DIAGNOSED: the first app launch on CI's freshly booted simulator times out; the fix is in stash e94585c437a9 "
        "but its proof can't run: `secrets run movies TMDB_API_KEY -- scripts/ios-local-server` failed (locked); "
        "decide: run `! secrets unlock movies`, so the fix can be proved, committed with `Fixes-Red: 77` and landed")
BLOCKED = ("BLOCKED ON OWNER: `! secrets unlock movies`: `secrets run movies TMDB_API_KEY -- sbt itAll` failed while "
           "the vault is locked")


class VaultBase(AskBase):
    def setUp(self):
        super().setUp()
        saved = A.open_store
        A.open_store = lambda *a, **k: self.st
        self.addCleanup(setattr, A, "open_store", saved)

    def ios_red(self, key="oi77", n=77):
        return self.red(key=key, workflow="Order independence", job="ios", kind="test",
                        last_run={"number": n, "databaseId": 9000 + n})

    def tick_blocks(self):
        self.t[0] += A.BLOCK_PROBE_EVERY + 1
        return A.red_renew_blocks(self.st, self.t[0])

    def step(self, it):
        return A.red_step(self.st, self.st.item("red", it["id"]), None, None, {}, None, "pawelkrupinski/movies",
                          self.t[0])

    def owner_told(self):
        return [e for e in self.events() if "WAITING ON YOU" in str((e.get("refs") or {}).get("verdict"))]


class VaultAskTest(VaultBase):
    def test_the_decide_vault_line_itself_is_a_vault_ask(self):
        """2026-11-22, Order independence #105: turns 69-71 ended `BLOCKED ON OWNER: ~/bin/agentctl decide vault-movies
        "here"` -- the line the owner is told -- and, unread as a vault ask, each was re-offered ~2 min later."""
        it, _acts = self.launch(self.ios_red(), outcome='BLOCKED ON OWNER: ~/bin/agentctl decide vault-movies "here" '
                                                        '(`secrets run movies OMDB_API_KEY -- sbt it` failed: locked)')
        self.assertEqual(it["state"], "blocked-external")
        self.assertEqual(A.red_block_of(it, self.st)["name"], "block/cause/vault-movies")

    def test_a_vault_ask_reading_no_secret_in_an_unsigned_repo_goes_back_to_its_fixer(self):
        """2026-11-22, Tool self-tests #480 / Alert backtest #20: `BLOCKED ON OWNER: ! secrets unlock movies` after a
        land's push failed (DNS) and `env --check` said the vault was locked -- no movies secret in the fix. Released
        three times, each back on the vault within minutes. movies' main is unsigned: such an ask is no owner's."""
        for outcome in ("BLOCKED ON OWNER: ! secrets unlock movies",
                        "BLOCKED ON OWNER: secrets unlock movies: `agentctl env --check` says vault movies locked",
                        'BLOCKED ON OWNER: ~/bin/agentctl decide vault-movies "here"'):
            it, _acts = self.launch(self.ios_red(key=f"k{abs(hash(outcome)) % 9999}"), outcome=outcome)
            self.assertEqual(it["state"], "offered", outcome)
            self.assertIsNone(A.red_block_of(it, self.st), outcome)
            self.assertIn("no movies secret was read", it["data"]["diagnosis"])
            self.assertIn("secrets run movies <KEY>", it["data"]["diagnosis"])
        self.assertFalse(self.owner_told(), "nothing reaches the owner")

    def test_a_vault_ask_in_a_repo_that_signs_still_waits_on_the_vault(self):
        saved = A.repo_signs
        A.repo_signs = lambda o_r: True
        self.addCleanup(setattr, A, "repo_signs", saved)
        it, _acts = self.launch(self.ios_red(), outcome="BLOCKED ON OWNER: ! secrets unlock movies: signing refused")
        self.assertEqual(it["state"], "blocked-external")

    def test_a_diagnosed_vault_ask_waits_on_the_vault_lease_not_on_a_decision(self):
        it, _acts = self.launch(self.ios_red(), outcome=DIAG)
        self.assertEqual(it["state"], "blocked-external")
        blk = A.red_block_of(it, self.st)
        self.assertEqual(blk["name"], "block/cause/vault-movies")
        self.assertFalse(blk["meta"].get("decision"), "a vault ask is no per-red owner decision")
        self.assertIsNone(blk["meta"].get("probe"), "no unlock in another session resumes it by itself")
        ask = A.red_ask(it, blk)
        self.assertIn('decide vault-movies "here"', ask["how"])
        self.assertIn("WAITING ON YOU", self.view(it)[1])

    def test_a_blocked_on_owner_vault_verdict_waits_too_never_re_offered_every_turn(self):
        it, _acts = self.launch(self.ios_red(), outcome=BLOCKED)
        self.assertEqual(it["state"], "blocked-external", "#77 was offered back to its owner 4 times in 4 minutes")
        self.assertEqual(A.red_block_of(it, self.st)["name"], "block/cause/vault-movies")

    def test_it_stays_blocked_until_the_owner_says_he_is_at_the_machine(self):
        it, _acts = self.launch(self.ios_red(), outcome=DIAG)
        for _ in range(3):
            self.tick_blocks()
            self.step(it)
        it = self.st.item("red", it["id"])
        self.assertEqual(it["state"], "blocked-external")
        self.assertIsNotNone(A.red_block_of(it, self.st))

    def test_decide_vault_resumes_it_with_the_signing_probe_first_and_closes_its_alert(self):
        it, _acts = self.launch(self.ios_red(), outcome=DIAG)
        A.red_owner_reminders(self.st, self.t[0])
        told = self.owner_told()
        self.assertEqual(len(told), 1)
        line = told[0]["refs"]["verdict"]
        self.assertIn('decide vault-movies "here"', line, "the one line to run is in the telling")
        eid = A.alert_write({"id": "20261116T034801000000-ev1-red-ask", "at": self.t[0], "command": "red", "cwd": "/",
                             "final": True, "exit": 1, "verdict": line, "verdicts": [line], "tail": [],
                             "item_type": "red", "item_id": it["id"]})
        rc, out = A.red_decide(self.st, "pawelkrupinski/movies", "vault-movies", "here")
        self.assertEqual(rc, 0, out)
        self.assertIn("DECIDED", out[0])
        it = self.st.item("red", it["id"])
        self.assertEqual(it["state"], "offered", "re-offered to a fixer, as a decide does")
        self.assertIsNone(A.red_block_of(it, self.st))
        dec = it["data"]["decision"]
        self.assertEqual(dec["status"], "in-progress")
        self.assertIsNone(it["data"].get("ask"))
        u = {"workflow": "Order independence", "job": "ios", "kind": "test", "line": "",
             "run": {"number": 77, "databaseId": 9077}, "decision": dec}
        prompt = A.fixer_prompt(u, "/w", "the brief")
        # an unlock is pinned to ONE Claude Code session (secrets' session anchor), never the fixer's own: its first
        # use raises its own Touch ID prompt, probed first while the owner is there -- never by signing in movies,
        # whose fixers commit unsigned (fixer_env): the probe is the command that needed the vault
        self.assertNotIn("commit-tree -S", prompt)
        self.assertIn("run again the one command that needed the vault", prompt)
        # Alert backtest #20, 2026-11-22 15:52Z / 15:54Z: released twice, re-blocked within 3 min each time -- the
        # session took `agentctl env --check` for "the command that needed the vault", which only reads `secrets
        # status` and never raises a Touch ID prompt: no release could ever clear it
        self.assertIn("never `agentctl env --check`", prompt)
        self.assertIn("name the command that failed and its error", prompt)
        self.assertIn("BLOCKED ON OWNER: ! secrets unlock movies", dec["answer"])
        self.assertNotIn("WAITING ON YOU", self.view(it)[1])
        A.reconcile_alerts(self.st, force=True)
        self.assertTrue(A.alert_acked(A.inbox_dir(), eid), "the owner's WAITING ON YOU alert closes with the block")

    def test_decide_vault_with_nothing_waiting_on_it_says_so(self):
        rc, out = A.red_decide(self.st, "pawelkrupinski/movies", "vault-movies", "here")
        self.assertEqual(rc, 1)
        self.assertIn("no red waits on the movies vault", out[0])


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
        rc, out = A.red_decide(self.st, "pawelkrupinski/movies", "vault-movies", "here")
        self.assertEqual((rc, len(out)), (0, 4), out)
        for r in reds + [late]:
            self.assertEqual(self.st.item("red", r["id"])["state"], "offered")


if __name__ == "__main__":
    unittest.main()
