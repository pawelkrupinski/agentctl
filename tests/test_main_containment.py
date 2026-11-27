"""IS THIS COMMIT ON MAIN? -- characterised over a seeded matrix of clone and forge states.

Before MainContainment, four copies answered it: redfixes.red_sha_on_main (the clone: an ancestor of main, main fetched
at most once per RED_MAIN_REFETCH when it is not), red_sha_on_main_api (the forge's compare, for a repo with no clone
here), red_main_equivalent (the main commit CARRYING it: itself, or the commit with its patch) and
dispatches.commit_on_main (ancestor or patch-equivalent, True / False / None). MATRIX is what each answered before the
consolidation; the test holds the one class to it, but for the disagreements resolved deliberately (RESOLVED, each with
its own test below)."""
import os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, Base, sh  # noqa: E402

CLONES = ("clone", "no-repo", "no-main-ref")
SHAS = ("ancestor", "remote-only", "recut", "unlanded", "missing")
COMPARES = ("ahead", "identical", "behind", "diverged", "raises")


def kind_of(got, sha, twin):
    """red_main_equivalent's answer as a kind: the sha itself, its twin on main, None, or another commit."""
    return {sha: "self", twin: "twin", None: None}.get(got, "other")


class World:
    """A clone of origin with each kind of commit: on main and fetched, on main but not fetched yet, re-cut onto main
    (its patch on main under another sha), unlanded, and absent."""

    def __init__(self, test):
        r = test.r
        self.r = r
        wt = r.worktree("wt-a", branch="a")
        r.write("a.txt", "a\n", at=wt)
        self.shas = {"ancestor": r.commit("on main", at=wt)}
        r.push(at=wt, ref="HEAD:main")
        wt2 = r.worktree("wt-b", branch="b")
        r.write("b.txt", "b\n", at=wt2)
        self.shas["recut"] = r.commit("re-cut on landing", at=wt2)
        sh(r.path, "git", "checkout", "-q", "--detach", "origin/main")
        sh(r.path, "git", "cherry-pick", self.shas["recut"])
        sh(r.path, "git", "commit", "-q", "--amend", "-m", "re-cut on landing (landed)")
        self.twin = sh(r.path, "git", "rev-parse", "HEAD").strip()
        sh(r.path, "git", "push", "-q", "origin", "HEAD:main")
        sh(r.path, "git", "fetch", "-q", "origin")
        wt3 = r.worktree("wt-c", branch="c")
        r.write("c.txt", "c\n", at=wt3)
        self.shas["unlanded"] = r.commit("never landed", at=wt3)
        other = os.path.join(r.root, "other")  # another clone pushes; this one has not fetched it
        sh(r.root, "git", "clone", "-q", r.bare, other)
        with open(os.path.join(other, "d.txt"), "w") as f:
            f.write("d\n")
        sh(other, "git", "add", "-A")
        sh(other, "git", "commit", "-q", "-m", "pushed elsewhere")
        self.shas["remote-only"] = sh(other, "git", "rev-parse", "HEAD").strip()
        sh(other, "git", "push", "-q", "origin", "HEAD:main")
        self.shas["missing"] = "0123456789abcdef0123456789abcdef01234567"
        self.bare_clone = os.path.join(r.root, "nomain")
        sh(r.root, "git", "init", "-q", self.bare_clone)

    def repo(self, clone):
        return {"clone": self.r.path, "no-repo": None, "no-main-ref": self.bare_clone}[clone]


def clear_caches():
    for c in A.PROCESS_CACHES:
        c.clear()


def git_decisions(w, clone, kind):
    """(on main, carried by, landed) for one cell: red_sha_on_main, red_main_equivalent's kind, commit_on_main -- each
    asked of the clone as the World left it (main fetched before the other clone's push)."""
    repo, sha = w.repo(clone), w.shas[kind]
    out = []
    main = A.MainContainment(repo)
    for ask in (lambda: main.contains(sha), lambda: kind_of(main.carrying(sha), sha, w.twin),
                lambda: A.commit_on_main(repo, sha)):
        clear_caches()
        sh(w.r.path, "git", "update-ref", "refs/remotes/origin/main", w.twin)
        out.append(ask())
    return tuple(out)


def api_decision(test, compare):
    clear_caches()

    def gh_json(o_r, *args, **kw):
        if compare == "raises":
            raise RuntimeError("HTTP 502")
        return {"status": compare}
    test.addCleanup(setattr, A, "gh_json", A.gh_json)
    A.gh_json = gh_json
    return A.MainContainment(o_r="o/r").contains("a" * 40)


# (route, clone or compare, commit kind): before the consolidation -- git: (red_sha_on_main, red_main_equivalent's
# kind, commit_on_main); api: red_sha_on_main_api
MATRIX = {
    ('api', 'ahead'): True,
    ('api', 'behind'): False,
    ('api', 'diverged'): False,
    ('api', 'identical'): True,
    ('api', 'raises'): False,
    ('git', 'clone', 'ancestor'): (True, 'self', True),
    ('git', 'clone', 'missing'): (False, None, False),
    ('git', 'clone', 'recut'): (False, 'twin', True),
    ('git', 'clone', 'remote-only'): (True, 'self', False),
    ('git', 'clone', 'unlanded'): (False, None, False),
    ('git', 'no-main-ref', 'ancestor'): (False, 'self', False),
    ('git', 'no-main-ref', 'missing'): (False, 'self', False),
    ('git', 'no-main-ref', 'recut'): (False, 'self', False),
    ('git', 'no-main-ref', 'remote-only'): (False, 'self', False),
    ('git', 'no-main-ref', 'unlanded'): (False, 'self', False),
    ('git', 'no-repo', 'ancestor'): (False, 'self', None),
    ('git', 'no-repo', 'missing'): (False, 'self', None),
    ('git', 'no-repo', 'recut'): (False, 'self', None),
    ('git', 'no-repo', 'remote-only'): (False, 'self', None),
    ('git', 'no-repo', 'unlanded'): (False, 'self', None),
}

# THE DISAGREEMENTS, resolved (maincontain.py's header says why; each has its own test below):
#  - a commit main has but the clone has not fetched: commit_on_main said "not on main" -- now it fetches, as the
#    contains route did: landed;
#  - UNKNOWN IS NOT "NO": no clone, no main ref, a failed compare answered False -- now None (each caller says what
#    unknown means: every red reader takes only True as "on main").
RESOLVED = {
    ("git", "clone", "remote-only"): (True, "self", True),
    **{("git", "no-main-ref", k): (None, "self", None) for k in SHAS},
    **{("git", "no-repo", k): (None, "self", None) for k in SHAS},
    ("api", "raises"): None,
}


class MainContainmentMatrixTest(Base):
    def test_the_one_class_answers_as_every_copy_did_but_for_the_resolved_disagreements(self):
        w = World(self)
        got = {}
        for clone in CLONES:
            for kind in SHAS:
                got[("git", clone, kind)] = git_decisions(w, clone, kind)
        for compare in COMPARES:
            got[("api", compare)] = api_decision(self, compare)
        want = {k: RESOLVED.get(k, v) for k, v in MATRIX.items()}
        diff = {k: (want.get(k), v) for k, v in got.items() if want.get(k) != v}
        self.assertEqual(diff, {}, "\n" + "\n".join(f"    {k!r}: {v!r}," for k, v in sorted(got.items(), key=str)))

    def test_the_ancestry_primitives_every_caller_shares(self):
        """Review 2026-11-26 (A D6): fourteen raw `merge-base --is-ancestor` and two dozen `cat-file -e` calls became
        ancestry.is_ancestor / commit_known: an unknown commit is not an ancestor and not known."""
        w = World(self)
        repo = w.r.path
        tip = sh(repo, "git", "rev-parse", "HEAD")
        parent = sh(repo, "git", "rev-parse", "HEAD~1")
        self.assertTrue(A.is_ancestor(repo, parent, tip))
        self.assertTrue(A.is_ancestor(repo, tip, tip))
        self.assertFalse(A.is_ancestor(repo, tip, parent))
        self.assertFalse(A.is_ancestor(repo, "0" * 40, tip))
        self.assertTrue(A.commit_known(repo, tip))
        self.assertFalse(A.commit_known(repo, "0" * 40))

    def test_a_commit_the_clone_has_not_fetched_is_landed_once_main_is_fetched(self):
        """Before: commit_on_main never fetched -- a branch red built on a commit another clone had just landed was
        "not on main" here (an orphan, its owner gone)."""
        w = World(self)
        sh(w.r.path, "git", "update-ref", "refs/remotes/origin/main", w.twin)
        self.assertIs(A.commit_on_main(w.r.path, w.shas["remote-only"]), True)

    def test_no_main_ref_is_unknown_never_not_on_main(self):
        w = World(self)
        self.assertIsNone(A.MainContainment(w.bare_clone).contains(w.shas["ancestor"]))
        self.assertIsNone(A.commit_on_main(w.bare_clone, w.shas["ancestor"]), "an unknown answer is never an orphan")

    def test_a_failed_compare_is_unknown_and_asked_again(self):
        """Before: the forge's error was remembered as "not on main" for RED_MAIN_REFETCH."""
        answers = [RuntimeError("HTTP 502"), {"status": "ahead"}]

        def gh_json(o_r, *args, **kw):
            got = answers.pop(0)
            if isinstance(got, Exception):
                raise got
            return got
        self.addCleanup(setattr, A, "gh_json", A.gh_json)
        A.gh_json = gh_json
        main = A.MainContainment(o_r="o/r")
        self.assertIsNone(main.contains("b" * 40))
        self.assertIs(main.contains("b" * 40), True, "asked again at once: the error was not remembered")

    def test_a_run_head_the_clone_lacks_fetches_main_once_per_window(self):
        """Review 2026-11-26 (A B1): ancestry.contains fetched main on EVERY call for a run head the clone did not have
        (a force-pushed-away head refetched each land-watch tick), unlike MainContainment's once per RED_MAIN_REFETCH."""
        w = World(self)
        os.utime(os.path.join(w.r.path, sh(w.r.path, "git", "rev-parse", "--git-path", "FETCH_HEAD").strip()), (0, 0))
        fetches = []
        real = A.git

        def git(repo, *a, **k):
            if a[:1] == ("fetch",):
                fetches.append(a)
            return real(repo, *a, **k)
        A.git = git
        try:
            gone = "fedcba9876543210fedcba9876543210fedcba98"
            runs = [{"databaseId": 7, "headSha": gone, "conclusion": "success"}]
            for _ in range(3):  # three land-watch ticks
                self.assertIsNone(A.pick_main_run(w.r.path, w.shas["ancestor"], runs))
            self.assertEqual(len(fetches), 1, fetches)
        finally:
            A.git = real


if __name__ == "__main__":
    unittest.main()
