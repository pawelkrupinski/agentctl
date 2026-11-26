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
    for ask in (lambda: A.red_sha_on_main(repo, sha), lambda: kind_of(A.red_main_equivalent(repo, sha), sha, w.twin),
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
    return A.red_sha_on_main_api("o/r", "a" * 40)


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

RESOLVED = {}


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


if __name__ == "__main__":
    unittest.main()
