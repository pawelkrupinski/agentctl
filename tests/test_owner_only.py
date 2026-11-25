"""ONLY WHAT NO AGENT CAN DO REACHES THE OWNER (the owner, 2026-11-19: "could any of these decision things could've been
handled automatically? fix structurally"). Each of that night's asks is replayed here: it either never reaches the
owner, or reaches him once, naming its owner-only category (ask.py OWNER_ONLY), and is retracted when it clears. The
lint at the end holds every source of an owner ask to a category."""
import glob, json, os, re, subprocess, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from test_vault_block import VaultBase  # noqa: E402

LIB = os.path.join(os.path.dirname(HERE), "lib", "agentctl")
CORE = "bitcashier/core"
RACE = ("DIAGNOSED: race in ReBroadcastMinedTransactionIntegrationTest.spendEverything (wallet_it, line 66): it spends "
        "the `getBalance` amount without naming which coins to spend, while BitcoinChainLookupIntegrationTest mines on "
        "the same regtest node")
LANES = ("DIAGNOSED: Stage NixOS closures' jobs ask for the plain `movies-measure` label, so ci-runner's scheduler ranks "
         "them as optional identity measurements")


class OwnerOnlyBase(VaultBase):
    def core_red(self, key="0c8eea91b712", **data):
        return self.red(key=key, workflow="Main Pipeline", job="build-and-test (it 1/2)", repo=CORE, gate=False,
                        last_run={"number": 1053, "databaseId": 44417242829}, **data)

    def owner_events(self):
        """What reached the owner: every escalation the projector makes an owner inbox entry of."""
        return [e for e in self.events() if A.alert_route({"verdict": (e.get("refs") or {}).get("verdict") or "",
                                                            "kind": A.inbox_kind((e.get("refs") or {}).get("verdict") or "",
                                                                                 True)})[0] == "owner"]

    def project(self, ev):
        """The laptop's projector: one escalation written to the inbox (alert_write) as its entry."""
        v = ev["refs"]["verdict"]
        return A.alert_write({"id": f"20261119T000000{ev['seq']:06d}-ev{ev['seq']}-red-ask", "at": self.t[0],
                              "command": "red", "cwd": "/", "final": True, "exit": 1, "verdict": v, "verdicts": [v],
                              "tail": [], "item_type": "red", "item_id": ev.get("item_id")}, A.CurrentStore(A.process_store))

    def unread_owner(self):
        return [e for e in A.inbox_entries() if not e["read"] and e.get("audience") == "owner"]


class HarnessFaultTest(OwnerOnlyBase):
    """Case 1, core #1053 23:23Z "BLOCKED ON OWNER: the fixers crash": a cross-host lease misjudgement and a re-counted
    lease end, agentctl's own bugs (852acad). A crash loop is the harness failing: an agentctl issue, never the owner."""

    def test_a_deep_crash_loop_files_one_agentctl_issue_and_never_asks_the_owner(self):
        it = self.core_red(tier="deep", attempts=3)
        for _ in range(3):
            it, _acts = self.launch(it, outcome="STOPPED: the red's lease was lost (ORPHANED: its wrapper is gone)")
        self.assertEqual(it["state"], "blocked-external")
        A.red_owner_reminders(self.st, self.t[0])
        self.assertEqual(self.owner_events(), [], "nothing reaches the owner")
        self.assertIn("HARNESS FAULT", self.view(it)[1])
        self.assertNotIn("BLOCKED", self.view(it)[1], "the status line never reads it as the owner's")
        issues = self.st.items("issue", ("open",))
        self.assertEqual(len(issues), 1)
        self.assertIn("~/bin-wt-", issues[0]["data"]["text"])
        # the block lapses, the red loops again: still ONE issue
        self.st.revoke(A.red_block_of(it, self.st)["name"], "lapsed")
        it = A.red_offer(self.st, self.st.item("red", it["id"]), "again", data={"crash_loop": None})
        it = self.st.item("red", it["id"])
        for _ in range(3):
            it, _acts = self.launch(it, outcome="STOPPED: the red's lease was lost (ORPHANED: its wrapper is gone)")
        self.assertEqual(len(self.st.items("issue", ("open",))), 1)


    def test_a_block_raised_before_the_categories_lapses_at_the_next_tick(self):
        """A crash-loop owner block, or a decision block with no category, live when this lands: lapsed, re-offered."""
        for key, meta in (("old-crash", {"crash_loop": True, "cause": A.CRASH_LOOP_CAUSE}),
                          ("old-decision", {"decision": True, "cause": A.DECISION_CAUSE, "decide": "x?"})):
            it = self.core_red(key=key)
            self.st.acquire(A.block_lease(key), "owner:x", A.BLOCK_TTL, A.BLOCK_MAX, progress="p",
                            meta={"kind": "block", "keys": [key], "text": "BLOCKED ON OWNER", **meta})
            it = A.red_move(self.st, it, "blocked-external", "blocked", data={"block": A.block_lease(key)})
            self.assertIsNotNone(A.block_invalid(self.st, it, A.red_block_of(it, self.st)["meta"], self.t[0]))
        out = A.red_renew_blocks(self.st, self.t[0])
        self.assertEqual(len([x for x in out if "lapsed" in x]), 2, out)


class DecidedActionFailedTest(OwnerOnlyBase):
    """Case 2, core #1053 02:49Z: the owner answered "852acad"; the fixer then DIAGNOSED a plain test race, and the
    owner was asked to "decide again". The standing rules decide a race (root cause, test-first): the fixer proceeds."""

    def decided(self, **data):
        it = self.core_red(decision={"answer": "852acad", "question": "the fixers crash", "status": "in-progress",
                                     "at": self.t[0]}, **data)
        return it

    def test_a_new_ordinary_diagnosis_proceeds_and_never_re_asks(self):
        it, acts = self.launch(self.decided(), outcome=RACE)
        self.assertEqual(it["state"], "offered", acts)
        self.assertIsNone(A.red_decision(it["data"]), "the owner's answer is spent: no 'THE OWNER DECIDED' brief")
        self.assertIn("spendEverything", it["data"]["diagnosis"])
        self.assertIn("NOT AN OWNER QUESTION", it["data"]["diagnosis"])
        A.red_owner_reminders(self.st, self.t[0])
        self.assertEqual(self.owner_events(), [])

    def test_a_new_diagnosis_naming_an_owner_only_category_asks_once_with_it(self):
        it, _acts = self.launch(self.decided(), outcome=RACE + "; decide[money-credentials]: rotate the regtest "
                                                             "faucet key in the provider dashboard")
        self.assertEqual(it["state"], "blocked-external")
        told = [e["refs"]["verdict"] for e in self.owner_events()]
        self.assertEqual(len(told), 1, told)
        self.assertTrue(told[0].startswith("RED BLOCKED ON OWNER [money-credentials]:"), told[0])
        self.assertIn("rotate the regtest faucet key", told[0])

    def test_a_failed_land_of_an_uncategorised_decision_goes_back_to_the_fixer(self):
        it, _acts = self.launch(self.decided(), outcome="NOT LANDED: the pre-push check failed: wallet_it")
        self.assertEqual(it["state"], "offered")
        self.assertEqual(self.owner_events(), [])


class LanePolicyTest(OwnerOnlyBase):
    """Case 3, Stage NixOS closures #102 ("where should a movies-stage lane rank") and Record scrape fixtures #198
    ("which lane order-independence rows take"): two DIAGNOSED in a row became owner decisions. The lane policy
    (movies scripts/ci/lane-policy.tsv) answers them: the fixer is pointed at it; only a question it has no rule for
    asks, proposing the row."""

    def stage_red(self):
        return self.red(key="abc567e5ae3b", workflow="Stage NixOS closures", job="stuck", kind="infra",
                        diagnosis=LANES, history=[{"outcome": LANES, "dispatched": True}],
                        last_run={"number": 102, "databaseId": 44420633319})

    def test_a_second_lane_diagnosis_is_routed_to_the_policy_never_the_owner(self):
        it, acts = self.launch(self.stage_red(), outcome=LANES + " (again)")
        self.assertEqual(it["state"], "offered", acts)
        self.assertIn("lane-policy.tsv", it["data"]["diagnosis"])
        self.assertEqual(self.owner_events(), [])

    def test_a_question_the_policy_has_no_rule_for_asks_once_proposing_the_row(self):
        it, _acts = self.launch(self.stage_red(), outcome=LANES + "; decide[policy-no-rule]: where a movies-stage lane "
                                "ranks; rule: scripts/ci/lane-policy.tsv: 3 movies-stage deploy-feeding")
        self.assertEqual(it["state"], "blocked-external")
        told = [e["refs"]["verdict"] for e in self.owner_events()]
        self.assertEqual(len(told), 1, told)
        self.assertIn("[policy-no-rule]", told[0])
        self.assertIn("rule: scripts/ci/lane-policy.tsv", told[0])


class OwnerAsksSnapshotTest(OwnerOnlyBase):
    """THE OWNER'S PENDING DECISIONS, structured (ask.py owner_asks): state.json `asks` carries each told owner ask --
    what the movies owner-decisions hook reads, never inbox text. 2026-11-19: an open agentctl issue QUOTING a red's
    "WAITING ON YOU: either design and measure..." was put to the owner as a pending decision."""

    def test_a_told_owner_ask_is_in_the_snapshot_with_its_category_and_command(self):
        it, _acts = self.launch(self.core_red(), outcome="BLOCKED ON OWNER: ! secrets unlock bitcashier")
        A.red_owner_reminders(self.st, self.t[0])
        A.write_snapshot({}, self.st)
        asks = A.state_read()["asks"]
        self.assertEqual(len(asks), 1, asks)
        self.assertEqual((asks[0]["category"], asks[0]["keys"]), ("vault", [it["data"]["key"]]))
        self.assertIn("decide vault-bitcashier", asks[0]["how"])
        self.st.revoke("block/cause/vault-bitcashier", "LIFTED")
        A.write_snapshot({}, self.st)
        self.assertEqual(A.state_read()["asks"], [], "a lapsed block is no pending decision")

    def test_an_issue_quoting_waiting_on_you_and_an_uncategorised_block_are_no_asks(self):
        A.issue_open(self.st, "a red's verdict: WAITING ON YOU: either design and measure X -- "
                              "`! ~/bin/agentctl decide c494fec65b9d \"<your answer>\"`", "test")
        it = self.core_red(key="old-decision")
        self.st.acquire(A.block_lease("old-decision"), "owner:x", A.BLOCK_TTL, A.BLOCK_MAX, progress="p",
                        meta={"kind": "block", "keys": ["old-decision"], "text": "BLOCKED ON OWNER",
                              "decision": True, "cause": A.DECISION_CAUSE, "decide": "x?"})
        A.red_move(self.st, it, "blocked-external", "blocked", data={"block": A.block_lease("old-decision"),
                                                                     "ask": {"q": "x?", "n": 1}})
        A.write_snapshot({}, self.st)
        self.assertEqual(A.state_read()["asks"], [])

    def test_a_held_question_is_not_the_owners_yet(self):
        it = self.core_red(key="held")
        self.st.acquire(A.block_lease("held"), "owner:x", A.BLOCK_TTL, A.BLOCK_MAX, progress="p",
                        meta={"kind": "block", "keys": ["held"], "text": "BLOCKED ON OWNER", "decision": True,
                              "cause": A.DECISION_CAUSE, "category": "irreversible",
                              "decide": "force-push main?"})
        it = A.red_move(self.st, it, "blocked-external", "blocked", data={"block": A.block_lease("held")})
        A.red_ask_mark(self.st, self.st.item("red", it["id"]), self.t[0], told=False)
        A.write_snapshot({}, self.st)
        self.assertEqual(A.state_read()["asks"], [])
        A.red_ask_mark(self.st, self.st.item("red", it["id"]), self.t[0], told=True)
        A.write_snapshot({}, self.st)
        self.assertEqual([a["category"] for a in A.state_read()["asks"]], ["irreversible"])


class StaleVaultAskTest(OwnerOnlyBase):
    """Case 4. 00:09Z "decide vault-movies here", run later: "NOT DECIDED: no red waits on the movies vault". 03:01Z
    "decide vault-bitcashier here": "NOT DECIDED: ... held no red still waiting on it (released)" -- the decide revoked
    the lease, the tick's invariant re-offered the red bare in the same second, and the decide found nothing blocked."""

    def vault_blocked(self):
        it, _acts = self.launch(self.core_red(), outcome="BLOCKED ON OWNER: ! secrets unlock bitcashier")
        self.assertEqual(A.red_block_of(it, self.st)["name"], "block/cause/vault-bitcashier")
        return it

    def test_the_ask_is_retracted_the_pass_its_block_lapses_while_the_red_still_reads_blocked(self):
        it = self.vault_blocked()
        A.red_owner_reminders(self.st, self.t[0])
        told = [e for e in self.owner_events() if "WAITING ON YOU" in e["refs"]["verdict"]]
        self.assertEqual(len(told), 1)
        self.assertIn("RED BLOCKED ON OWNER [vault]", told[0]["refs"]["verdict"])
        eid = self.project(told[0])
        self.assertEqual(len(self.unread_owner()), 1)
        self.st.revoke("block/cause/vault-bitcashier", "LIFTED: its fix is on its way")
        self.assertEqual(self.st.item("red", it["id"])["state"], "blocked-external", "the red has not moved yet")
        A.reconcile_alerts(self.st, force=True)
        self.assertTrue(A.alert_acked(A.inbox_dir(), eid), "the inbox and the owner-decisions hook drop it now")
        self.assertNotIn("BLOCKED ON OWNER", " ".join(self.view(self.st.item("red", it["id"]))))

    def test_decide_vault_racing_the_invariant_still_resumes_the_red_with_the_signing_brief(self):
        it = self.vault_blocked()
        real = self.st.revoke

        def revoke_then_tick(name, *a, **k):  # the tick's invariant, in the same second as the decide's revoke
            r = real(name, *a, **k)
            if name == "block/cause/vault-bitcashier":
                A.red_enforce(self.st, None, self.t[0])
            return r
        self.st.revoke = revoke_then_tick
        rc, out = A.red_decide(self.st, "pawelkrupinski/movies", "vault-bitcashier", "here")
        self.assertEqual(rc, 0, out)
        it = self.st.item("red", it["id"])
        self.assertEqual(it["state"], "offered")
        self.assertIn("commit-tree -S", it["data"]["decision"]["answer"])

    def test_decide_vault_after_the_invariant_re_offered_it_resumes_it_too(self):
        it = self.vault_blocked()
        A.red_offer(self.st, self.st.item("red", it["id"]), "INVARIANT: re-offered")  # the lease still live
        rc, out = A.red_decide(self.st, "pawelkrupinski/movies", "vault-bitcashier", "here")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.st.item("red", it["id"])["data"]["decision"]["category"], "vault")


class TrustAskTest(OwnerOnlyBase):
    """Case 5: "claude does not trust ~/bitcashier/core" reached the owner 21 minutes into core #1053. It is asked when
    the repo is registered (the sync's pass over red-repos.json), once, and retracted once trusted. Case 6: that line
    was written to the inbox five times on 11-18, once per flap of the offer's reason."""

    def setUp(self):
        super().setUp()
        self.clone = os.path.join(self.tmp.name, "core")
        subprocess.run(["git", "init", "-q", self.clone], check=True)
        repos = os.path.join(self.tmp.name, "red-repos.json")
        with open(repos, "w") as f:
            json.dump({"repos": [{"repo": CORE, "clone": self.clone, "gate": [["Main Pipeline", "m.yml"]],
                                  "family": "core-repo"}]}, f)
        cfg = os.path.join(self.tmp.name, "claude")
        os.makedirs(cfg)
        self.cfg = os.path.join(cfg, ".claude.json")
        self.trust(False)
        for k, v in (("AGENTCTL_RED_REPOS", repos), ("CLAUDE_CONFIG_DIR", cfg)):
            self.saved_env[k] = os.environ.get(k)
            os.environ[k] = v

    def trust(self, ok):
        with open(self.cfg, "w") as f:
            json.dump({"projects": {os.path.realpath(self.clone): {"hasTrustDialogAccepted": ok}}}, f)

    def test_an_untrusted_clone_is_asked_once_when_registered_and_retracted_once_trusted(self):
        out = A.trust_asks()
        self.assertEqual(len(out), 1, out)
        for _ in range(5):  # every later pass, and every flap of a red's offer reason: no new entry
            self.assertEqual(A.trust_asks(), [])
        asks = self.unread_owner()
        self.assertEqual(len(asks), 1, [e["verdict"] for e in asks])
        self.assertTrue(asks[0]["verdict"].startswith("RED BLOCKED ON OWNER [tty-trust]: WAITING ON YOU:"))
        self.assertIn(f"cd {self.clone} && claude", asks[0]["verdict"])
        self.trust(True)
        A.reconcile_alerts(self.st, force=True)
        self.assertEqual(self.unread_owner(), [])

    def test_the_same_ask_under_another_item_is_one_entry(self):
        line = ("RED BLOCKED ON OWNER [vault]: WAITING ON YOU: core Main Pipeline #1053 [it]: agents' work waits on the "
                "bitcashier vault -- at the machine: `! ~/bin/agentctl decide vault-bitcashier \"here\"`")
        for n, item in ((1, "red:0c8eea91b712.12"), (2, "red:0c8eea91b712.13")):
            A.alert_write({"id": f"20261119T03010{n}000000-ev{n}-red-ask", "at": self.t[0] + n, "command": "red",
                           "cwd": "/", "final": True, "exit": 1, "verdict": line, "verdicts": [line], "tail": [],
                           "alert_item": item}, A.CurrentStore(A.process_store))
        asks = self.unread_owner()
        self.assertEqual(len(asks), 1)
        self.assertEqual(asks[0]["count"], 2)


class OwnerOnlyLintTest(unittest.TestCase):
    """THE LINT: no source of an owner ask outside the categories."""

    def sources(self):
        for p in sorted(glob.glob(os.path.join(LIB, "*.py"))):
            with open(p) as f:
                yield os.path.basename(p), f.read()

    def test_every_owner_cause_is_an_owner_only_category(self):
        self.assertEqual(set(A.OWNER_CAUSES), set(A.OWNER_CAUSE_CATEGORY))
        self.assertLessEqual(set(A.OWNER_CAUSE_CATEGORY.values()), set(A.OWNER_ONLY))

    def test_every_owner_block_lease_carries_its_category(self):
        """Every `acquire(..., "owner:...")` (a block lease the owner is asked about) names `category` in its meta."""
        found = 0
        for name, src in self.sources():
            for m in re.finditer(r"\.acquire\([^,]+,\s*f?\"owner:", src):
                call = src[m.start():src.find("\n\n", m.start())]
                found += 1
                self.assertIn('"category"', call, f"{name}: an owner block without its category: {call[:200]}")
        self.assertGreaterEqual(found, 3)

    def test_every_owner_ask_line_names_its_category(self):
        """Every `RED BLOCKED ON OWNER` line agentctl writes is `RED BLOCKED ON OWNER [<category>]: ...`."""
        for name, src in self.sources():
            for m in re.finditer(r"(?<!\w)f?\"RED BLOCKED ON OWNER([^\"]{0,3})", src):
                self.assertTrue(m.group(1).startswith(" ["), f"{name}: {src[m.start():m.start() + 80]!r}")

    def test_an_ask_outside_the_list_is_refused(self):
        self.assertIsNotNone(A.ask_refusal(None, "anything"))
        self.assertIsNotNone(A.ask_refusal("design", "anything"))
        self.assertIsNotNone(A.ask_refusal("policy-no-rule", "which lane?"))
        self.assertIsNone(A.ask_refusal("policy-no-rule", "which lane? rule: lane-policy.tsv: 3 movies-stage"))
        for cat in A.OWNER_ONLY:
            if cat != "policy-no-rule":
                self.assertIsNone(A.ask_refusal(cat, "q"))


if __name__ == "__main__":
    unittest.main()
