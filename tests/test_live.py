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


class MaskTest(unittest.TestCase):
    def test_credentials_are_masked(self):
        M = A.LIVE_MASKED
        for secret in ("ghp_" + "a1B2" * 9, "github_pat_" + "x1" * 25, "sk-ant-api03-" + "Ab3" * 10, "AKIAIOSFODNN7EXAMPLE",
                       "hvs.CAESIJ" + "q9" * 12, "xoxb-1234-5678-abcdEFGH", "eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.SflKxwRJSMeKKF2QT4"):
            self.assertEqual(A.live_mask_text(f"use {secret} now"), f"use {M} now", secret)
        self.assertEqual(A.live_mask_text("export FORGEJO_TOKEN=3f9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d3e2f1a"),
                         f"export FORGEJO_TOKEN={M}")
        self.assertEqual(A.live_mask_text('{"password": "hunter2hunter2X9"}'), '{"password": "' + M + '"}')
        self.assertEqual(A.live_mask_text("Authorization: Bearer abcdef0123456789abcdef01"), f"Authorization: Bearer {M}")
        self.assertEqual(A.live_mask_text("git push https://pawel:s3cretPass@host/r.git"),
                         f"git push https://pawel:{M}@host/r.git")
        pem = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk\n-----END OPENSSH PRIVATE KEY-----"
        self.assertEqual(A.live_mask_text(f"key:\n{pem}\nok"), f"key:\n{M}\nok")

    def test_ordinary_text_is_left_alone(self):
        for s in ("commit 0f24259a8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d3e", "inputTokens: 207", "token budget exhausted",
                  "val token = readToken()", "password: $VAULT_PASSWORD", "the secret sauce is caching",
                  "/Users/pawel/.claude/projects/-Users-pawel-projects-movies/2bee16b9-f06c-4c34.jsonl",
                  "https://forgejo.example/o/r/actions/runs/3785"):
            self.assertEqual(A.live_mask_text(s), s)

    def test_json_values_under_secret_keys(self):
        got = A.live_mask({"input": {"command": "ls", "api_key": "abcdefgh", "auth_token": "x", "tokens": 3,
                                     "password": "$P"}, "list": ["ghp_" + "Z9" * 18]})
        self.assertEqual(got, {"input": {"command": "ls", "api_key": A.LIVE_MASKED, "auth_token": "x", "tokens": 3,
                                         "password": "$P"}, "list": [A.LIVE_MASKED]})


class TailTest(LiveBase):
    def lines(self, tail):
        out, reset, _more = tail.poll()
        return [(o, x if isinstance(x, int) else x.decode()) for o, x in out], reset

    def test_growth_partial_lines_truncation_and_rotation(self):
        p = os.path.join(self.root, "t.jsonl")
        with open(p, "wb") as f:
            f.write(b"one\ntw")
        t = A.LiveTail(p)
        self.assertEqual(self.lines(t), ([(4, "one")], None))
        self.assertEqual(self.lines(t), ([], None))  # half a line is held back
        with open(p, "ab") as f:
            f.write(b"o\nthree\n")
        self.assertEqual(self.lines(t), ([(8, "two"), (14, "three")], None))
        with open(p, "wb") as f:  # truncated in place
            f.write(b"x\n")
        self.assertEqual(self.lines(t), ([(2, "x")], "truncated"))
        os.rename(p, p + ".1")  # rotated: a new file at the path
        with open(p, "wb") as f:
            f.write(b"new\n")
        self.assertEqual(self.lines(t), ([(4, "new")], "rotated"))
        t.close()
        t = A.LiveTail(p, offset=4)  # resuming past the history
        with open(p, "ab") as f:
            f.write(b"more\n")
        self.assertEqual(self.lines(t), ([(9, "more")], None))
        t.close()
        t = A.LiveTail(p, offset=999)
        self.assertEqual(self.lines(t), ([(4, "new"), (9, "more")], "truncated"))
        t.close()

    def test_bounded_reads_and_lines(self):
        p = os.path.join(self.root, "big.jsonl")
        with open(p, "wb") as f:
            f.write(b"a" * 50 + b"\n" + b"b" * 10 + b"\n" + b"c" * 30 + b"\nd\n")
        t = A.LiveTail(p, chunk=16, line_max=20)
        got, more = [], True
        while more:
            out, _r, more = t.poll()
            got += out
        t.close()
        self.assertEqual(got, [(51, 51), (62, b"b" * 10), (93, 31), (95, b"d")])  # over line_max: its size only

    def test_entries_skip_bookkeeping_and_mask(self):
        self.assertIsNone(A.live_entry(entry("api-request-blob", message={"content": "x"}).encode()))
        self.assertIsNone(A.live_entry(b"   "))
        e = A.live_entry(entry("assistant", parentUuid="p", message={
            "role": "assistant", "usage": {"x": 1}, "stop_reason": "tool_use", "content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": "echo ghp_" + "Q1" * 18}}]}).encode())
        self.assertEqual(e["message"], {"role": "assistant", "stop_reason": "tool_use", "content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "echo " + A.LIVE_MASKED}}]})
        self.assertNotIn("parentUuid", e)
        self.assertEqual(A.live_entry(b"not json sk-ant-" + b"x1" * 12), {"type": "raw", "text": "not json " + A.LIVE_MASKED})
        self.assertEqual(A.live_entry(entry("new-kind", extra=1).encode())["extra"], 1)  # unknown: all of it

    def test_sse_framing(self):
        self.assertEqual(A.sse_frame("entry", {"a": "x\ny"}, 12), b'id: 12\nevent: entry\ndata: {"a": "x\\ny"}\n\n')
        self.assertEqual(A.sse_frame("x", "l1\nl2"), b"event: x\ndata: l1\ndata: l2\n\n")


def sse_events(raw):
    out = []
    for block in raw.split("\n\n"):
        ev = {"data": []}
        for ln in block.split("\n"):
            if ln.startswith(":") or not ln:
                continue
            k, _, v = ln.partition(": ")
            if k == "data":
                ev["data"].append(v)
            else:
                ev[k] = v
        if "event" in ev:
            ev["data"] = json.loads("\n".join(ev["data"]))
            out.append(ev)
    return out


class StreamServerTest(LiveBase):
    def setUp(self):
        super().setUp()
        os.environ["AGENTCTL_LIVE_POLL"] = "0.05"
        self.saved_cached = A.status_cached
        A.status_cached = lambda: {"at": 0, "agents": []}
        self.srv = A.status_page_server("127.0.0.1", 0)
        self.port = self.srv.server_address[1]
        import threading
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.p = self.transcript("/w/live", "s-live", [
            entry("user", message={"role": "user", "content": "fix the red"}),
            entry("api-request", params={"model": "m"}),
            entry("assistant", message={"role": "assistant", "content": [{"type": "text", "text": "on it"}],
                                        "stop_reason": "tool_use"})])
        self.fixer(31, 1, transcript=self.p, pid=4242)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        A.status_cached = self.saved_cached
        super().tearDown()

    def open(self, path, host="agentctl.localhost", headers=None):
        import socket
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        hd = "".join(f"{k}: {v}\r\n" for k, v in (headers or {}).items())
        s.sendall(f"GET {path} HTTP/1.1\r\nHost: {host}\r\n{hd}\r\n".encode())
        return s

    def read_until(self, s, pred, secs=5):
        """(the response head, every event so far on s) once pred(events) holds."""
        bufs = self.__dict__.setdefault("bufs", {})
        buf, end = bufs.get(id(s), b""), time.time() + secs
        if buf:
            text = buf.decode("utf-8", "replace")
            if pred(sse_events(text.split("\r\n\r\n", 1)[1])):
                return text.split("\r\n\r\n", 1)[0], sse_events(text.split("\r\n\r\n", 1)[1])
        while time.time() < end:
            try:
                b = s.recv(65536)
            except OSError:
                break
            if not b:
                break
            buf += b
            bufs[id(s)] = buf
            text = buf.decode("utf-8", "replace")
            body = text.split("\r\n\r\n", 1)[1] if "\r\n\r\n" in text else ""
            if pred(sse_events(body)):
                return text.split("\r\n\r\n", 1)[0], sse_events(body)
        self.fail(f"not seen in {secs} s: {buf[-2000:]!r}")

    def test_history_then_growth_then_truncation(self):
        s = self.open("/stream?agent=fixer-31-1")
        head, evs = self.read_until(s, lambda es: any(e["event"] == "state" for e in es))
        self.assertIn("200", head.split("\r\n")[0])
        self.assertIn("text/event-stream", head)
        self.assertEqual([e["event"] for e in evs], ["meta", "entry", "entry", "synced", "state"])
        self.assertEqual(evs[1]["data"]["message"]["content"], "fix the red")
        self.assertEqual(evs[2]["data"]["message"]["content"][0]["text"], "on it")
        self.assertEqual(int(evs[2]["id"]), os.path.getsize(self.p))
        self.assertEqual(evs[4]["data"]["state"], "working")
        with open(self.p, "a") as f:
            f.write(entry("assistant", message={"content": [{"type": "text", "text": "done, token ghp_" + "R2" * 18}],
                                                "stop_reason": "end_turn"}))
        _h, evs = self.read_until(s, lambda es: sum(e["event"] == "state" for e in es) >= 2)
        said = [e for e in evs if e["event"] == "entry"][-1]["data"]["message"]["content"][0]["text"]
        self.assertEqual(said, "done, token " + A.LIVE_MASKED)
        self.assertEqual([e for e in evs if e["event"] == "state"][-1]["data"]["state"], "waiting on background")
        with open(self.p, "w") as f:
            f.write(entry("user", message={"content": "fresh"}))
        _h, evs = self.read_until(s, lambda es: any(e["event"] == "state" and e["data"]["state"] == "working"
                                                    for e in es[es.index(next(x for x in es if x["event"] == "reset")):])
                                  if any(x["event"] == "reset" for x in es) else False)
        after = evs[[e["event"] for e in evs].index("reset"):]
        self.assertEqual(after[0]["id"], "0")
        self.assertEqual([e["data"]["message"]["content"] for e in after if e["event"] == "entry"], ["fresh"])
        s.close()

    def test_resume_from_an_offset_or_last_event_id(self):
        size = os.path.getsize(self.p)
        with open(self.p, "a") as f:
            f.write(entry("user", message={"content": "later"}))
        for hd, q in (({}, f"&from={size}"), ({"Last-Event-ID": str(size)}, "&from=0")):
            s = self.open("/stream?agent=fixer-31-1" + q, headers=hd)
            _h, evs = self.read_until(s, lambda es: any(e["event"] == "synced" for e in es))
            s.close()
            self.assertEqual([e["data"]["message"]["content"] for e in evs if e["event"] == "entry"], ["later"])

    def test_unknown_agents_paths_hosts_and_writes_are_refused(self):
        import urllib.request, urllib.error

        def get(path, host="agentctl.localhost", method="GET"):
            req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", headers={"Host": host}, method=method)
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status
            except urllib.error.HTTPError as e:
                e.close()
                return e.code
        self.assertEqual(get("/stream?agent=fixer-99-1"), 404)
        self.assertEqual(get("/stream?agent=" + urllib.request.quote(self.p)), 404)
        self.assertEqual(get("/stream?path=" + urllib.request.quote(self.p)), 404)
        self.assertEqual(get("/stream?agent=fixer-31-1&from=x"), 400)
        self.assertEqual(get("/stream?agent=fixer-31-1", host="evil.example"), 403)
        self.assertEqual(get("/stream?agent=fixer-31-1", method="POST"), 405)

    def test_streams_are_capped(self):
        saved = A._live_streams
        A._live_streams = __import__("threading").BoundedSemaphore(1)
        try:
            s = self.open("/stream?agent=fixer-31-1")
            self.read_until(s, lambda es: any(e["event"] == "synced" for e in es))
            s2 = self.open("/stream?agent=fixer-31-1")
            self.assertIn(b" 503 ", s2.recv(200))
            s2.close()
            s.close()
        finally:
            A._live_streams = saved

    def test_pages(self):
        import urllib.request, urllib.error
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/agent/fixer-31-1", headers={"Host": "agentctl.localhost"})
        with urllib.request.urlopen(req, timeout=5) as r:
            body = r.read().decode()
        self.assertIn("livePane(ID,true)", body)
        self.assertIn("prefers-color-scheme:dark", body)
        self.assertNotIn("/*", body.split("<script>")[1].split("lvBlock")[0])  # every placeholder filled
        for bad in ("/agent/fixer-99-1", "/agent/" + urllib.request.quote(self.p, safe=""), "/agent/../etc/passwd"):
            req = urllib.request.Request(f"http://127.0.0.1:{self.port}{bad}", headers={"Host": "agentctl.localhost"})
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(req, timeout=5)
            cm.exception.close()
            self.assertEqual(cm.exception.code, 404, bad)
        for frag in ("data-live-btn", "new EventSource", "follow", "pause", ".livehost"):
            self.assertIn(frag, A.STATUS_PAGE_HTML)
        self.assertNotIn("/*LIVE", A.STATUS_PAGE_HTML)

    @unittest.skipUnless(__import__("shutil").which("node"), "node is not installed")
    def test_the_scripts_parse(self):
        import subprocess, tempfile
        for html in (A.STATUS_PAGE_HTML, A.AGENT_PAGE_HTML):
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
                f.write(html.split("<script>")[1].split("</script>")[0])
            r = subprocess.run(["node", "--check", f.name], capture_output=True, text=True, timeout=60)
            os.remove(f.name)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_a_slow_client_is_dropped(self):
        writes = []

        def write(b):
            writes.append(b)
            if len(writes) > 1:
                raise TimeoutError("timed out")  # what a full socket buffer past LIVE_SEND_TIMEOUT raises
        e = A.live_lookup("fixer-31-1")
        with self.assertRaises(OSError):
            A.live_stream(e, 0, write, poll=0.01, max_secs=5)
        self.assertEqual(len(writes), 2)


if __name__ == "__main__":
    unittest.main()
