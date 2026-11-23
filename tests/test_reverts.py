"""A REVERTED CHANGE GETS BACK TO THE AGENT THAT WROTE IT (lib/agentctl/reverts.py; the owner, 2026-11-21: 604134d1a was
reverted by a red fixer as 7380cf9d7 and nobody told its lander). A revert on main opens a notice told to its lander
(author) and its orchestrator (owner audience); an ack closes it; an unacked one escalates once past its deadline; a
re-land with `Rework-Of:` (or a revert of the revert) closes it. A real git clone, a real store, the real projector."""
import contextlib, io, os, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402

ANALYSIS = ("Main #2806-#2808 (worker-identity-2): its sameOriginal change added a new A -> B -> A take; the parent "
            "passes the slice.")


def g(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True, text=True).stdout.strip()


class RevertNoticeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="agentctl-reverts-")
        for k in ("AGENTCTL_INBOX_DIR", "CLAUDE_CODE_SESSION_ID"):
            self.addCleanup(lambda k=k, v=os.environ.get(k): os.environ.__setitem__(k, v) if v is not None
                            else os.environ.pop(k, None))
        os.environ["AGENTCTL_INBOX_DIR"] = os.path.join(self.tmp, "inbox")
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        self.t = [1_000_000.0]
        self.st = A.Store(path=os.path.join(self.tmp, "store.db"), clock=lambda: self.t[0])
        self.repo = os.path.join(self.tmp, "movies")
        subprocess.run(["git", "init", "-q", "-b", "main", self.repo], check=True)
        self.commit("base", {"a.txt": "a\n"})
        self.publish()
        A.tick_reverts(self.repo, st=self.st, now_=self.t[0])  # the cursor: main as it was
        self.wt = os.path.join(self.tmp, "movies-wt-feature")
        os.makedirs(self.wt)

    def commit(self, msg, files):
        for rel, text in files.items():
            with open(os.path.join(self.repo, rel), "w") as f:
                f.write(text)
        g(self.repo, "add", "-A")
        g(self.repo, "commit", "-q", "-m", msg)
        return g(self.repo, "rev-parse", "HEAD")

    def publish(self):
        g(self.repo, "update-ref", f"refs/remotes/{A.remote()}/main", "HEAD")

    def land(self, agent="sub-1"):
        """The feature, landed by `agentctl land` from a subagent of session sess-orch (its change item)."""
        base = g(self.repo, "rev-parse", "HEAD")
        sha = self.commit("Feature: an original title links only to the other title", {"b.txt": "b\n"})
        self.st.put_item("change", "ch-feature", "landed", {
            "worktree": self.wt, "branch": "feature", "commit_keys": A.commit_keys(self.repo, base, sha),
            "lander": {"session": "sess-orch", "agent": agent, "worktree": self.wt, "branch": "feature",
                       "repo": A.RED_PRIMARY}})
        self.publish()
        return sha

    def revert(self, sha):
        g(self.repo, "revert", "--no-edit", sha)
        body = g(self.repo, "log", "-1", "--format=%B")
        g(self.repo, "commit", "-q", "--amend", "-m",
          f"{body}\n\n{ANALYSIS}\n\nFixes-Red: 2808\nCo-Authored-By: Claude <noreply@anthropic.com>")
        self.publish()
        return g(self.repo, "rev-parse", "HEAD")

    def tick(self, after=60):
        self.t[0] += after
        out = A.tick_reverts(self.repo, st=self.st, now_=self.t[0])
        A.project_events(self.st)
        return out

    def entries(self):
        return [e for e in A.inbox_entries() if str(e.get("command")) == "revert"]

    def only(self):
        [it] = self.st.items("revert")
        return it

    def test_a_revert_of_a_landed_commit_tells_its_lander_and_its_orchestrator(self):
        sha = self.land()
        rv = self.revert(sha)
        out = self.tick()
        self.assertTrue(any("told subagent sub-1 of session sess-orc" in x for x in out), out)
        it = self.only()
        self.assertEqual((it["state"], it["data"]["change"], it["data"]["fixes"]), ("open", "ch-feature", ["2808"]))
        self.assertEqual(it["data"]["reverted"][0]["sha"], sha)
        by = {A.alert_audience(e): e for e in self.entries()}
        self.assertEqual(set(by), {"author", "owner"})
        lander, orch = by["author"], by["owner"]
        self.assertEqual((lander["kind"], lander["owner"], lander["cwd"]), ("REVERTED", "sess-orch", self.wt))
        self.assertIn(sha[:10], lander["verdict"])
        self.assertIn("Feature: an original title", lander["verdict"])
        self.assertIn(rv[:10], lander["verdict"])
        self.assertIn("Fixes-Red: 2808", lander["verdict"])
        self.assertIn(f"agentctl revert ack {it['id']} --rework|--drop", lander["verdict"])
        self.assertTrue(any("A -> B -> A" in line for line in lander["tail"]), "the revert's analysis rides along")
        self.assertEqual((orch["kind"], orch["owner"]), ("REVERT_NOTICE", "sess-orch"))  # the session that spawned it
        self.assertIn("subagent sub-1", orch["verdict"])
        self.assertEqual(self.tick(), [], "a second scan opens nothing again")
        self.assertEqual(len(self.entries()), 2)

    def test_a_top_level_lander_s_orchestrator_is_the_owner_and_an_unrecorded_commit_tells_only_the_owner(self):
        sha = self.land(agent=None)
        self.revert(sha)
        self.tick()
        by = {A.alert_audience(e): e for e in self.entries()}
        self.assertEqual(by["owner"]["owner"], "?")  # no spawner recorded: the owner's inbox
        hand = self.commit("the owner's own change", {"c.txt": "c\n"})
        self.publish()
        self.revert(hand)
        self.tick()
        mine = [e for e in self.entries() if hand[:10] in e["verdict"]]
        self.assertEqual([A.alert_audience(e) for e in mine], ["owner"])
        self.assertIn("nobody's landing", mine[0]["verdict"])

    def test_an_ack_closes_it_and_its_alerts(self):
        self.revert(self.land())
        self.tick()
        it = self.only()
        got, msg = A.revert_ack(self.st, it["id"][:8], "rework", "re-landing with a monotone link", "sub-1")
        self.assertEqual(got["state"], "acked", msg)
        self.assertEqual(got["data"]["how"], "rework")
        A.reconcile_alerts(self.st, force=True)
        self.assertTrue(all(e["read"] for e in self.entries()), "both alerts close with their notice")
        self.t[0] += A.REVERT_ACK_MAX * 2
        self.assertEqual(A.revert_deadlines(self.st, self.t[0]), [], "an acked notice never escalates")

    def cli(self, *argv):
        saved = A.open_store
        A.open_store = lambda *a, **k: self.st
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = A.main(list(argv))
            return code, buf.getvalue()
        finally:
            A.open_store = saved

    def test_a_second_ack_of_a_closed_notice_says_who_acked_it_and_keeps_its_note(self):
        """2026-11-22: the orchestrator acked 7380cf9d775c-604134d1a582, then its authoring agent's own ack said "no
        open notice with that id"."""
        self.revert(self.land())
        self.tick()
        iid = self.only()["id"]
        self.assertEqual(self.cli("revert", "ack", iid, "--rework", "--note", "handed back to its lane")[0], 0)
        code, out = self.cli("revert", "ack", iid, "--rework", "--note", "re-landing with a monotone link")
        self.assertEqual(code, 0, out)
        self.assertIn("already acked -- acknowledged by", out)
        self.assertIn("(rework): handed back to its lane", out)
        it = self.only()
        self.assertEqual(it["state"], "acked")
        self.assertEqual([r["note"] for r in it["data"]["replies"]], ["re-landing with a monotone link"])
        self.assertEqual(it["data"]["note"], "handed back to its lane", "the first ack stays the ack")

    def test_revert_all_lists_closed_notices_with_their_ack_whichever_way_it_is_written(self):
        self.revert(self.land())
        self.tick()
        iid = self.only()["id"]
        A.revert_ack(self.st, iid, "drop", "it stays reverted", "session orch")
        self.assertIn("reverts: none open", self.cli("revert")[1])
        for argv in (("revert", "--all"), ("revert", "--all", "list"), ("revert", "list", "--all")):
            code, out = self.cli(*argv)
            self.assertEqual(code, 0)
            self.assertIn(f"{iid}  acked", out, argv)
            self.assertIn("acknowledged by session orch at", out, argv)
            self.assertIn("(drop): it stays reverted", out, argv)

    def test_unacked_past_its_deadline_it_escalates_once_to_the_owner(self):
        self.revert(self.land())
        self.tick()
        self.assertEqual(self.tick(A.REVERT_ACK_MAX - 120), [])
        out = self.tick(180)
        self.assertEqual(len(out), 1, out)
        self.assertIn("escalated to the owner", out[0])
        it = self.only()
        self.assertEqual(it["state"], "escalated")
        ev = [e for e in self.st.events() if e["item_type"] == "revert" and e["severity"] == "escalate"]
        self.assertEqual(len(ev), 1)
        esc = [e for e in self.entries() if e["verdict"].startswith("REVERT UNACKED")]
        self.assertEqual([(e["kind"], A.alert_audience(e)) for e in esc], [("REVERT_NOTICE", "owner")])
        self.assertEqual(self.tick(3600), [], "escalated ONCE")
        self.t[0] += A.ARCHIVE + 60
        self.assertEqual(A.revert_deadlines(self.st, self.t[0]), [f"revert {it['id']}: expired"])

    def test_a_reland_with_rework_of_closes_it(self):
        sha = self.land()
        self.revert(sha)
        self.tick()
        self.commit(f"Feature again, the link monotone\n\nRework-Of: {sha[:10]}\nCo-Authored-By: C <c@x>", {"b.txt": "b2\n"})
        self.publish()
        out = self.tick()
        self.assertTrue(any("reworked (Rework-Of:" in x for x in out), out)
        self.assertEqual(self.only()["state"], "reworked")

    def test_a_rework_of_line_outside_the_final_trailer_block_is_prose(self):
        sha = self.land()
        self.revert(sha)
        self.tick()
        self.commit(f"Feature again\n\nRework-Of: {sha[:10]}\n\nCo-Authored-By: C <c@x>", {"b.txt": "b2\n"})
        self.publish()
        self.tick()
        self.assertEqual(self.only()["state"], "open")

    def test_a_revert_of_the_revert_relands_it_and_opens_no_notice_of_its_own(self):
        rv = self.revert(self.land())
        self.tick()
        g(self.repo, "revert", "--no-edit", rv)
        self.publish()
        self.tick()
        self.assertEqual([i["state"] for i in self.st.items("revert")], ["reworked"])

    def test_the_lander_s_worktree_gets_its_note_once_kept_out_of_commits(self):
        subprocess.run(["git", "-C", self.repo, "worktree", "add", "-q", "-b", "feature-wt", self.wt + "-git"], check=True)
        self.wt = self.wt + "-git"
        self.revert(self.land())
        self.tick()
        self.assertEqual(len(A.revert_notes(self.st)), 1)
        self.assertEqual(A.revert_notes(self.st), [], "once")
        with open(os.path.join(self.wt, A.REVERT_NOTE)) as f:
            self.assertIn("REVERTED: your", f.read())
        self.assertEqual(g(self.wt, "status", "--porcelain"), "", "the note never shows in git status")

    def test_status_and_red_status_show_the_open_notices(self):
        self.revert(self.land())
        self.tick()
        rows = [A.revert_brief(i) for i in self.st.items("revert", A.REVERT_OPEN)]
        h = {"gate_text": "gate green", "tick_at": self.t[0], "store_url": None, "store_unreachable": False,
             "waiting": [], "queued": [], "reverts": rows}
        self.assertIn("REVERTED unacked: 1", A.render_status_line(h, self.t[0]))
        text = A.render_red_status({"reds": [], "reverts": rows, "families": [], "owners": [], "dispatcher": {"error": "-"}})
        self.assertIn("reverts (1 unacked", text)
        self.assertIn(rows[0]["id"], text)

    def test_the_machine_is_ticked_and_no_policy(self):
        self.assertEqual(A.DEADLINE_TICKS["revert"], "tick_reverts")
        self.assertIn("reverts", [n for n, _ in A.server_steps("/r", tempfile.mkdtemp(), lambda f: f, A.Clock())])
        self.assertNotIn("revert", A.POLICIES)  # a notification: it holds and rejects nothing


if __name__ == "__main__":
    unittest.main()
