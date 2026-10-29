"""The status page's live agent output: which transcripts are known (fixer records, dispatch records, lease holders,
subagent globs), the path restrictions, the agent state."""
import json, os, sys, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from test_status_page import Base  # noqa: E402

SESSION = "2bee16b9-f06c-4c34-a2a0-382bf8d1f606"


def entry(typ, **k):
    return json.dumps({"type": typ, "timestamp": "2026-10-29T12:00:00Z", **k}) + "\n"


class LiveBase(Base):
    def setUp(self):
        super().setUp()
        self.projects = os.path.join(self.root, "projects")
        self.tasks = os.path.join(self.root, "tmp", "proj", "sess", "tasks")
        os.makedirs(self.projects)
        os.makedirs(self.tasks)
        for k in ("AGENTCTL_CLAUDE_PROJECTS", "AGENTCTL_LIVE_GLOBS", "AGENTCTL_LIVE_POLL"):
            self.saved.setdefault(k, os.environ.get(k))
        os.environ["AGENTCTL_CLAUDE_PROJECTS"] = self.projects
        os.environ["AGENTCTL_LIVE_GLOBS"] = os.path.join(self.root, "tmp", "*", "*", "tasks", "*.output")
        A._live_reg.update(at=0.0, reg=None)
        A._live_leased.clear()
        A._live_info.clear()

    def transcript(self, wt, session, lines=None):
        d = os.path.join(self.projects, A.live_slug(wt))
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, f"{session}.jsonl")
        with open(p, "w") as f:
            f.write("".join(lines if lines is not None else [entry("user", message={"content": "go"})]))
        return p

    def fixer(self, run, n, **rec):
        d = os.path.join(self.env["AGENTCTL_REDS_DIR"], str(run))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"fixer-{n}.json"), "w") as f:
            json.dump(rec, f)

    def dispatch(self, name, **rec):
        d = os.path.join(self.root, "state", "dispatches")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"{name}.json"), "w") as f:
            json.dump(rec, f)

    def subagent(self, aid, meta=None):
        sub = os.path.join(self.projects, "-Users-p-bitcashier", "sess", "subagents")
        os.makedirs(sub, exist_ok=True)
        real = os.path.join(sub, f"agent-{aid}.jsonl")
        with open(real, "w") as f:
            f.write(entry("user", message={"content": "do it"}))
        if meta is not None:
            with open(real[:-6] + ".meta.json", "w") as f:
                json.dump(meta, f)
        os.symlink(real, os.path.join(self.tasks, f"{aid}.output"))
        return real


class MappingTest(LiveBase):
    def test_fixers_dispatches_and_subagents_each_get_an_id(self):
        wt = "/Users/p/projects/movies-wt-red-2527-2"
        p = self.transcript(wt, SESSION)
        self.fixer(2527, 2, transcript=p, label="red fixer #2527", pid=4242, worktree=wt, session=SESSION)
        p2 = self.transcript("/Users/p/projects/movies-wt-flips", "aaaa-1111")
        self.dispatch("d1", session="aaaa-1111", worktree="/Users/p/projects/movies-wt-flips", purpose="flip measure",
                      epoch=time.time(), pid=77)
        self.dispatch("d2", session=SESSION, worktree=wt, epoch=time.time())  # the fixer's own session: one id
        real = self.subagent("a01e71d6262987b68", {"description": "Live output", "finished": True})
        with open(os.path.join(self.tasks, "bash1.output"), "w") as f:  # a background shell's output: not an agent
            f.write("plain text\n")
        reg = A.live_registry_read()
        self.assertEqual(sorted(reg), ["fixer-2527-2", "session-aaaa-1111", "task-a01e71d6262987b68"])
        self.assertEqual(reg["fixer-2527-2"]["transcript"], os.path.realpath(p))
        self.assertEqual(reg["session-aaaa-1111"]["transcript"], os.path.realpath(p2))
        self.assertEqual(reg["task-a01e71d6262987b68"]["transcript"], os.path.realpath(real))
        self.assertEqual(reg["task-a01e71d6262987b68"]["label"], "Live output")
        self.assertTrue(reg["task-a01e71d6262987b68"]["finished"])

    def test_a_fixer_log_that_is_a_jsonl_names_its_transcript(self):
        p = self.transcript("/w/x", "s-2")
        self.fixer(9, 1, log=p)
        self.fixer(9, 3, log=os.path.join(self.root, "state", "reds", "9", "claude-3.log"))
        self.assertEqual(sorted(A.live_registry_read()), ["fixer-9-1"])

    def test_lease_holders_become_known_through_the_agents_section(self):
        p = self.transcript("/w/y", "lease-sess")
        rows = A.live_annotate([{"pid": None, "holder": "laptop:claim:x", "kind": "claim", "transcript": p,
                                 "leases": [{"name": "red/k"}]}], time.time())
        self.assertEqual(rows[0]["live"], "session-lease-sess")
        self.assertEqual(A.live_lookup("session-lease-sess")["transcript"], os.path.realpath(p))

    def test_recent_sessions_and_subagents_get_rows_of_their_own(self):
        self.subagent("abc", {"description": "probe"})
        rows = A.live_annotate([], time.time())
        self.assertEqual([(r["live"], r["label"], r["kind"]) for r in rows], [("task-abc", "probe", "subagent")])
        old = time.time() - A.LIVE_RECENT - 60
        os.utime(A.live_registry_read()["task-abc"]["transcript"], (old, old))
        self.assertEqual(A.live_annotate([], time.time()), [])


class PathTest(LiveBase):
    def test_only_jsonl_files_of_this_user_under_the_projects_root(self):
        p = self.transcript("/w/z", "ok")
        self.assertEqual(A.live_transcript_ok(p), os.path.realpath(p))
        outside = os.path.join(self.root, "x.jsonl")
        open(outside, "w").close()
        self.assertIsNone(A.live_transcript_ok(outside))
        self.assertIsNone(A.live_transcript_ok(os.path.join(self.projects, "..", "x.jsonl")))
        link = os.path.join(self.projects, "evil.jsonl")
        os.symlink("/etc/hosts", link)
        self.assertIsNone(A.live_transcript_ok(link))
        txt = os.path.join(self.projects, "notes.txt")
        open(txt, "w").close()
        self.assertIsNone(A.live_transcript_ok(txt))
        self.assertIsNone(A.live_transcript_ok("relative.jsonl"))
        saved = os.getuid
        os.getuid = lambda: saved() + 1  # someone else's file
        try:
            self.assertIsNone(A.live_transcript_ok(p))
        finally:
            os.getuid = saved

    def test_records_naming_other_paths_are_not_streamed(self):
        self.fixer(5, 1, transcript="/etc/passwd")
        self.fixer(5, 2, transcript=os.path.join(self.projects, "..", "..", "x.jsonl"))
        self.dispatch("d", session="../../etc/passwd", worktree="/w")
        self.assertEqual(A.live_registry_read(), {})

    def test_lookup_takes_ids_only(self):
        p = self.transcript("/w/q", "s-q")
        self.fixer(7, 1, transcript=p)
        self.assertEqual(A.live_lookup("fixer-7-1")["transcript"], os.path.realpath(p))
        for bad in (p, "fixer-7-1/../x", "../fixer-7-1", "fixer-nope", "other-7-1", "", None, "fixer-" + "a" * 200):
            self.assertIsNone(A.live_lookup(bad), bad)
        os.remove(p)  # gone since the registry was read
        self.assertIsNone(A.live_lookup("fixer-7-1"))


class StateTest(LiveBase):
    def test_states(self):
        t = time.time()
        end = {"stop": "end_turn", "mtime": t}
        mid = {"stop": None, "mtime": t}
        self.assertEqual(A.live_state({"ended": "x", "rc": 0}, mid, t)[0], "done")
        self.assertEqual(A.live_state({"ended": "x", "rc": 1, "verdict": "STOPPED"}, end, t), ("failed", "STOPPED"))
        self.assertEqual(A.live_state({"pid": 4242}, mid, t)[0], "working")
        self.assertEqual(A.live_state({"pid": 4242}, end, t)[0], "waiting on background")
        self.assertEqual(A.live_state({"pid": 9}, end, t)[0], "done")
        self.assertEqual(A.live_state({"pid": 9}, mid, t)[0], "failed")
        self.assertEqual(A.live_state({}, end, t)[0], "done")
        self.assertEqual(A.live_state({"finished": True}, mid, t)[0], "done")
        self.assertEqual(A.live_state({}, mid, t)[0], "working")
        self.assertEqual(A.live_state({}, {"stop": None, "mtime": t - A.LIVE_STALL - 5}, t)[0], "failed")
        self.assertEqual(A.live_state({"pid": 4242}, {"api_error": True}, t)[0], "failed")

    def test_tail_info_follows_the_last_conversational_entry(self):
        p = self.transcript("/w/s", "s", [entry("assistant", message={"content": [], "stop_reason": "tool_use"}),
                                          entry("user", message={"content": [{"type": "tool_result"}]}),
                                          entry("assistant", message={"content": [], "stop_reason": "end_turn"}),
                                          entry("cost-state")])
        self.assertEqual(A.live_tail_info(p)["stop"], "end_turn")
        with open(p, "a") as f:
            f.write(entry("queue-operation", content="a message"))
        self.assertIsNone(A.live_tail_info(p)["stop"])


if __name__ == "__main__":
    unittest.main()
