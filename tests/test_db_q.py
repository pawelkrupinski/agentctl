"""agentctl db-q: read-only Forgejo DB queries -- refusal of anything but one SELECT/WITH/PRAGMA read, -readonly, row cap,
one tunnel reopen on a refused connection."""
import contextlib, io, os, stat, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402

ALLOWED = [
    "select id, status from action_run order by id desc limit 5",
    "SELECT name FROM release",  # a table named like a keyword
    "with r as (select * from action_run) select count(*) from r",
    "select replace(title, 'a', 'b') from action_run",
    "select * from action_run where title = 'DROP TABLE x; DELETE FROM y'",
    "-- comment with ; delete from x\nselect 1;",
    "pragma table_info(action_run)",
]
REFUSED = {
    "delete from action_run": "only SELECT",
    "update action_run set status=1": "only SELECT",
    "with x as (select 1) delete from action_run": "write/side-effect",
    "with x as (select 1) insert into t select * from x": "write/side-effect",
    "select 1; drop table action_run": "one statement",
    "select 1;\n.shell rm -rf /": "dot-commands",
    ".tables": "dot-commands",
    "pragma journal_mode = wal": "PRAGMA assignments",
    "select writefile('/tmp/x', 'y')": "write/side-effect",
    "attach database '/tmp/x' as y": "only SELECT",
    "": "empty",
    "/* select */ vacuum": "only SELECT",
}


class SqlRefusalTest(unittest.TestCase):
    def test_allowed(self):
        for q in ALLOWED:
            self.assertIsNone(A.sql_refusal(q), q)

    def test_refused(self):
        for q, why in REFUSED.items():
            r = A.sql_refusal(q)
            self.assertIsNotNone(r, q)
            self.assertIn(why, r, q)

    def test_real_command_opens_readonly_and_safe(self):
        cmd = " ".join(A.DB_ENVS["forgejo"])
        self.assertIn("sqlite3 -readonly -safe", cmd)
        self.assertIn("BatchMode=yes", cmd)


class DbQTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fake = os.path.join(self.tmp.name, "fake-db")
        self.marker = os.path.join(self.tmp.name, "refused-once")
        with open(self.fake, "w") as f:
            f.write(f'#!/bin/sh\nsql=$(cat)\nif [ -n "$REFUSE_ONCE" ] && [ ! -f {self.marker} ]; then touch {self.marker}; '
                    'echo "ssh: connect to host 10.1.0.3 port 22: Connection refused" >&2; exit 255; fi\n'
                    'echo "id\tstatus"\ni=1; while [ $i -le ${ROWS:-3} ]; do printf "%s\\t1\\n" $i; i=$((i+1)); done\n')
        os.chmod(self.fake, os.stat(self.fake).st_mode | stat.S_IEXEC)
        self.saved_env, A.DB_ENVS = A.DB_ENVS, {"forgejo": [self.fake]}
        self.kicks = []
        self.saved_kick, A.kick_tunnel = A.kick_tunnel, lambda: self.kicks.append(1) or True

    def tearDown(self):
        A.DB_ENVS, A.kick_tunnel = self.saved_env, self.saved_kick
        for k in ("ROWS", "REFUSE_ONCE"):
            os.environ.pop(k, None)
        self.tmp.cleanup()

    def tool(self, *argv):
        out, errb = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(errb):
            rc = A.main(list(argv))
        return rc, out.getvalue(), errb.getvalue()

    def test_refused_query_never_reaches_the_db(self):
        rc, out, errs = self.tool("db-q", "delete from action_run")
        self.assertEqual(rc, 2)
        self.assertIn("REFUSED", errs)
        self.assertEqual(out, "")

    def test_rows_capped(self):
        os.environ["ROWS"] = "30"
        rc, out, _ = self.tool("db-q", "--max-rows", "10", "select id, status from action_run")
        self.assertEqual(rc, 0)
        self.assertEqual(out.splitlines()[0], "id\tstatus")
        self.assertEqual(len(out.splitlines()), 12)
        self.assertIn("20 more row(s) not shown", out)

    def test_refused_connection_reopens_the_tunnel_once(self):
        os.environ["REFUSE_ONCE"] = "1"
        rc, out, errs = self.tool("db-q", "select 1")
        self.assertEqual(rc, 0, errs)
        self.assertEqual(self.kicks, [1])
        self.assertIn("reopening the tunnel", errs)


class DbQMutationTest(unittest.TestCase):
    def test_refusal_tests_kill_a_permissive_mutant(self):
        real = A.sql_refusal
        A.sql_refusal = lambda sql: None
        try:
            res = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(
                unittest.TestSuite([SqlRefusalTest("test_refused"), DbQTest("test_refused_query_never_reaches_the_db")]))
        finally:
            A.sql_refusal = real
        self.assertEqual(len(res.failures) + len(res.errors), 2, "a mutant survived")


if __name__ == "__main__":
    unittest.main()
