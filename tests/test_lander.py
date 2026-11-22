"""THE LANDER (lib/agentctl/lander.py): every `agentctl land` remembers who ran it -- the session, the subagent, the
transcript, the worktree, the branch -- and each commit's keys, so a commit on main (rebased: a new sha) maps back to
its change and lander."""
import json, os, sys, time, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_agentctl import A, Base, sh  # noqa: E402


class LanderRecordTest(Base):
    def setUp(self):
        super().setUp()
        self.projects = os.path.join(self.tmp.name, "claude-projects")
        os.environ["AGENTCTL_CLAUDE_PROJECTS"] = self.projects

    def tearDown(self):
        for k in ("AGENTCTL_CLAUDE_PROJECTS", "CLAUDE_CODE_SESSION_ID", "CLAUDE_PID"):
            os.environ.pop(k, None)
        super().tearDown()

    def transcript(self, rel, lines, age=0):
        p = os.path.join(self.projects, "-proj", rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            for x in lines:
                f.write(json.dumps(x) + "\n")
        t = time.time() - age
        os.utime(p, (t, t))
        return p

    def test_a_subagent_that_ran_land_is_the_lander_not_its_orchestrator(self):
        sid = "sess-1"
        main = self.transcript(f"{sid}.jsonl", [{"cwd": "/orchestrator", "type": "user"},
                                                {"type": "assistant", "message": "agentctl land"}])  # older, below
        os.utime(main, (time.time() - 30, time.time() - 30))
        sub = self.transcript(f"{sid}/subagents/agent-abc123.jsonl",
                              [{"cwd": "/wt", "agentId": "abc123"},
                               {"type": "assistant", "message": {"content": [{"type": "tool_use", "input": {
                                   "command": "~/bin/agentctl land"}}]}}])
        idle = self.transcript(f"{sid}/subagents/agent-idle.jsonl", [{"cwd": "/wt2"}])  # recent, but never landed
        self.assertTrue(os.path.exists(idle))
        self.assertEqual(A.lander_transcript(sid), (sub, "abc123"))
        os.environ["CLAUDE_CODE_SESSION_ID"] = sid
        rec = A.lander_record(self.r.path)
        self.assertEqual((rec["session"], rec["agent"], rec["transcript"], rec["session_cwd"]), (sid, "abc123", sub, "/wt"))
        self.assertEqual(rec["worktree"], self.r.path)
        self.assertEqual(rec["branch"], "main")

    def test_a_top_level_session_is_its_own_transcript_and_none_without_a_session(self):
        p = self.transcript("sess-2.jsonl", [{"cwd": "/w"}, {"m": "agentctl land"}])
        self.assertEqual(A.lander_transcript("sess-2"), (p, None))
        self.transcript("sess-3/subagents/agent-old.jsonl", [{"m": "agentctl land"}], age=3600)  # long ago: not it
        q = self.transcript("sess-3.jsonl", [{"cwd": "/w"}], age=3600)
        self.assertEqual(A.lander_transcript("sess-3"), (q, None))
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        rec = A.lander_record(self.r.path)
        self.assertIsNone(rec["session"])
        self.assertNotIn("transcript", rec)


    @staticmethod
    def bash_call(command, at="2026-11-17T08:49:00Z"):
        return {"type": "assistant", "timestamp": at, "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": command}}]}}

    def test_a_newer_subagent_that_only_read_about_land_is_not_the_lander(self):
        """2026-11-17: 20261117-094917-movies-dev-speed-a046a0's lander was recorded as a read-only analysis subagent
        of the same session, written last and quoting AGENTCTL.md's `agentctl land`; the one that ran it was older."""
        sid = "sess-4"
        self.transcript(f"{sid}.jsonl", [{"cwd": "/orchestrator"}], age=5)
        lander = self.transcript(f"{sid}/subagents/agent-lander.jsonl",
                                 [{"cwd": "/orchestrator"}, self.bash_call("cd /wt-dev && ~/bin/agentctl land")], age=60)
        self.transcript(f"{sid}/subagents/agent-reader.jsonl",
                        [{"cwd": "/orchestrator"}, {"type": "user", "message": {"content": [
                            {"type": "tool_result", "content": "agentctl land  # queues the change"}]}},
                         self.bash_call("grep -n land ~/bin/AGENTCTL.md")])
        self.assertEqual(A.lander_transcript(sid, worktree="/wt-dev", ancestry=[]), (lander, "lander"))

    def test_the_land_call_this_process_runs_under_wins_over_another_subagents_land(self):
        """Two subagents of a session both ran `agentctl land`: the one whose Bash command is this process's parent
        shell's (the calling Claude process's own call) is the lander, even when the other is newer."""
        sid = "sess-5"
        mine = self.transcript(f"{sid}/subagents/agent-mine.jsonl",
                               [{"cwd": "/o"}, self.bash_call("cd /wt-a && ~/bin/agentctl land 2>&1 | tail -3")], age=90)
        self.transcript(f"{sid}/subagents/agent-other.jsonl", [{"cwd": "/o"}, self.bash_call("cd /wt-b && ~/bin/agentctl land")])
        shell = "/bin/zsh -c source snap.sh && eval 'cd /wt-a && ~/bin/agentctl land 2>&1 | tail -3' < /dev/null"
        self.assertEqual(A.lander_transcript(sid, worktree="/elsewhere", ancestry=[shell]), (mine, "mine"))
        self.assertEqual(A.lander_transcript(sid, worktree="/wt-b", ancestry=[]),
                         (os.path.join(self.projects, "-proj", sid, "subagents", "agent-other.jsonl"), "other"))

    def test_land_status_is_not_a_land(self):
        sid = "sess-6"
        main = self.transcript(f"{sid}.jsonl", [{"cwd": "/o"}], age=5)
        self.transcript(f"{sid}/subagents/agent-watcher.jsonl", [{"cwd": "/o"}, self.bash_call("~/bin/agentctl land status")])
        self.assertEqual(A.lander_transcript(sid, worktree="/wt", ancestry=[]), (main, None))

    def test_ps_escapes_in_the_parent_shell_are_read_back(self):
        """ps prints a newline as \\012 and the eval wrapper quotes ' as '"'"'."""
        raw = "/bin/zsh -c eval 'cd /w && echo '\"'\"'x'\"'\"'\\012~/bin/agentctl land' < /dev/null"
        self.assertIn("cd /w && echo 'x'\n~/bin/agentctl land", A.shell_args_text(raw))


class CommitKeysTest(Base):
    def change(self, name, files):
        wt = self.r.worktree(f"repo-wt-{name}", branch=name)
        for rel, text in files.items():
            self.r.write(rel, text, at=wt)
        sha = self.r.commit(f"{name}: change", at=wt)
        return wt, sha

    def test_a_rebased_commit_is_found_on_main_by_its_patch_id(self):
        wt, sha = self.change("a", {"a.txt": "a\n"})
        keys = A.commit_keys(wt, "origin/main", "HEAD")
        self.assertEqual([k["sha"] for k in keys], [sha])
        # another change lands first, then this one is rebased (a new sha) and lands
        self.r.write("other.txt", "o\n")
        self.r.commit("other")
        self.r.push()
        sh(wt, "git", "rebase", "-q", "origin/main")
        sh(wt, "git", "commit", "-q", "--amend", "--no-edit", "--date=@1700000000")  # new sha, new author time
        self.r.push(at=wt)
        landed = A.commit_keys(self.r.path, "origin/main~1", "origin/main")
        self.assertNotEqual(landed[0]["sha"], sha)
        idx = A.lander_index([{"change": "c1", "commit_keys": keys, "state": "landed", "lander": {"session": "s"}}])
        self.assertEqual(A.lander_of_commit(idx, landed[0])["change"], "c1")
        other = A.commit_keys(self.r.path, "origin/main~2", "origin/main~1")[0]
        self.assertIsNone(A.lander_of_commit(idx, other))

    def test_a_commit_whose_patch_changed_in_the_rebase_is_found_by_its_subject_and_author_time(self):
        k = {"sha": "1" * 40, "patch_id": "p-old", "subject": "x: do it", "at": 1700000000}
        idx = A.lander_index([{"change": "c2", "commit_keys": [k], "state": "landed"}])
        self.assertEqual(A.lander_of_commit(idx, {"sha": "2" * 40, "patch_id": "p-new", "subject": "x: do it",
                                                  "at": 1700000000})["change"], "c2")
        self.assertIsNone(A.lander_of_commit(idx, {"sha": "2" * 40, "patch_id": "p-new", "subject": "x: do it",
                                                   "at": 1700000001}))

    def test_the_change_item_carries_its_lander_and_keys_and_the_tick_notes_its_commits_on_main(self):
        wt, sha = self.change("b", {"b.txt": "b\n"})
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sess-b"
        try:
            fields = A.change_fields(wt, "refs/remotes/origin/main", sha, type("a", (), {})())
        finally:
            os.environ.pop("CLAUDE_CODE_SESSION_ID")
        self.assertEqual(fields["lander"]["session"], "sess-b")
        self.assertEqual(fields["lander"]["worktree"], wt)
        self.assertEqual([k["sha"] for k in fields["commit_keys"]], [sha])
        self.assertIn("lander", A.SUBMIT_FIELDS)
        self.assertIn("commit_keys", A.SUBMIT_FIELDS)
        st = A.open_store()
        e = {**fields, "id": "e-b", "status": "queued"}
        d = os.path.join(self.tmp.name, "wd")
        os.makedirs(os.path.join(d, "entries"))
        A.set_change(d, e, "checking", store=st)
        it = st.item("change", "e-b")
        self.assertEqual(it["data"]["lander"]["session"], "sess-b")  # the item, not only the entry file
        st.transition("change", "e-b", "checked", it["version"])
        st.transition("change", "e-b", "pushing", st.item("change", "e-b")["version"])
        st.transition("change", "e-b", "landed", st.item("change", "e-b")["version"])
        self.r.push(at=wt)
        acts = A.lander_note_landed(st, self.r.path, "pawelkrupinski/movies", time.time(), force=True)
        self.assertTrue(acts, acts)
        self.assertEqual(st.item("change", "e-b")["data"]["landed_commits"], {sha: fields["commit_keys"][0]["patch_id"]})
        recs = A.lander_records(st)
        self.assertEqual([(r["change"], r["lander"]["session"]) for r in recs], [("e-b", "sess-b")])

    def test_a_direct_land_writes_a_lander_item(self):
        wt, sha = self.change("c", {"c.txt": "c\n"})
        keys = A.commit_keys(wt, "origin/main", sha)
        cid = A.lander_note_direct(wt, sha, keys)
        it = A.open_store().item("lander", cid)
        self.assertEqual(it["state"], "recorded")
        self.assertEqual(it["data"]["landed_commits"], {sha: keys[0]["patch_id"]})
        self.assertEqual(it["data"]["lander"]["worktree"], wt)


if __name__ == "__main__":
    unittest.main()
