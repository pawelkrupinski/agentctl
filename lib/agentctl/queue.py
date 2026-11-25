# agentctl, part `queue`: the land queue and submission.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import re
import subprocess
import time
from typing import Any
from agentctl.procrunner import process_runner
from agentctl.core import env_num, err, fmt_age, git, git_ok, host_role
from agentctl.timefmt import iso_at
from agentctl.statefile import read_record
from agentctl.classify import credential_failure, redact_credentials
from agentctl.repos import precheck_hook, repo_of_waiting
from agentctl.waitingdir import waiting_entries, wlog
from agentctl.land import tool_env
from agentctl.queueslots import set_change
from agentctl.conditions import open_condition, run_probe
from agentctl.statemachines import StaleVersion, TerminalState, change_event


# a backlog batch (batch.py, THE BACKLOG BATCH) is up to BACKLOG_BATCH_MAX of them checked in ONE tree.
# 4, not 3 (2026-11-17): ci-runner's check runners were 3 in effect (a lane bug let borrowed checks hold the check
# lane: movies runner_scheduler.py), up to 2 lane + 1 CPU-admitted + 2 borrowed since; 4 is a first step, re-measure.
# A check the LAPTOP failed, not the change: sbt's server never came up or was lost, a script test cut off by its
# time bound, a stall. Queued again after INFRA_BACKOFF's delay, at most QUEUE_INFRA_RETRIES times, then judged as it is.
# THE CHECK'S ENVIRONMENT LACKED THE GITOPS MANIFESTS (2026-11-22, landing checks 44984605717 and 44998261508: the
# deploy specs failed "infra/kubernetes/... is missing because the GitOps manifests are no longer in this repository",
# the beside script tests having moved the fetched manifests aside; Identity takes #349's fix was REJECTED twice and the
# gate stayed red ~7 h). The manifests are another repository's, fetched by the check itself (infra/bin/fetch-gitops):
# their absence, or the fetch failing, is the check's environment, never the change. This is an INFRA RETRY of the
# check, NOT a rerun-to-green of a test: no test result is discarded, the check never ran the specs against their
# input. Re-queued like any infra failure (backed off, escalated from the third, a condition opened past
# QUEUE_INFRA_RETRIES), so a change that really breaks the fetch is still seen.
GITOPS_MISSING = (r"GitOps manifests are no longer in this repository|fetch-gitops could not fetch|"
                  r"GitOps manifests STALE")
INFRA_CHECK_FAILURE = re.compile(r"failed to connect to server|sbt server did not start within \d+ seconds|"
                                 r"sbt server disconnected|^TIMEOUT +\d+s|STALLED:|" + GITOPS_MISSING, re.M)
QUEUE_INFRA_RETRIES = 3
# A PUSH, FETCH OR TUNNEL BLIP IS NO VERDICT ON THE CHANGE (audit #6, 2026-10-28: entry 67 "the push failed (pre-push
# hook or remote)" was a tunnel error in its tail; a fixer GAVE UP on it): such a failure, a stalled push, a lock that
# never came free and main moving under every round re-queue the entry (retry_after, at most QUEUE_INFRA_RETRIES),
# its `attempts` reset (a checker restart after an infra re-queue is no failure of the change).
# TRANSIENT_PUSH: classify.py's PUSH_SIGNATURES (the network's rows and the push's own).


INFRA_BACKOFF = (300, 600, 1800, 3600)  # infra re-queues back off; from the third, each one escalates


def requeue_infra(st, d, e, why, now, delay=None, reason=None, detail=None, repo=None):
    """Back to queued for a later look: infra NEVER rejects a change (design §5.1). The delay grows 5 -> 60 min; from
    QUEUE_INFRA_RETRIES on, each re-queue is an escalation (its event's severity). NEVER SILENTLY FOREVER (2026-10-29:
    a corrupt clone's fetch of main re-queued every change for hours, and nobody was told): re-queued for the same
    reason (reason: why without its details) QUEUE_INFRA_RETRIES times or for INFRA_SAME_MAX, it opens a condition
    with the exact error (detail), which its own probe clears."""
    n = e.get("infra_retries", 0) + 1
    wait = delay if delay is not None else INFRA_BACKOFF[min(n - 1, len(INFRA_BACKOFF) - 1)]
    sev = "escalate" if n >= QUEUE_INFRA_RETRIES else "info"
    t = now
    reason = reason or infra_reason_of(why)
    same = e.get("infra_reason") == reason
    n_same = int(e.get("infra_same") or 0) + 1 if same else 1
    since = float(e.get("infra_reason_since") or t) if same else t
    set_change(st, d, e, "queued", data={"infra_attempts": n},
               event=change_event(e, f"QUEUED AGAIN {e['id']}: infra ({why}); checked again in {int(wait // 60)} min",
                                  code=None, final=False, severity=sev),
               infra_retries=n, retry_after=t + wait, why=f"infra: {why}", waiting_on=None,
               infra_reason=reason, infra_same=n_same, infra_reason_since=since)
    wlog(d, f"{e['id']}: {why}: queued again in {int(wait // 60)} min (infra retry {n}"
            + (", escalated" if sev == "escalate" else "") + ")")
    requeue_condition(st, d, e, why, reason, n_same, t - since, detail, repo)


def requeue_condition(st: Any, d: str, e: dict[str, Any], why: str, reason: str, n_same: int, age: float, detail: str | None,
                      repo: str | None) -> None:
    """The condition a re-queue opens, if any. A push credential failure: ONE for the clone, at once (no retry brings it
    back). Any other reason: the infra one, once the same reason repeated QUEUE_INFRA_RETRIES times or for
    INFRA_SAME_MAX."""
    try:
        if credential_failure(why) and push_credential_condition(st, d, why, detail, repo):
            return
        if n_same >= QUEUE_INFRA_RETRIES or age >= env_num("AGENTCTL_INFRA_SAME_MAX", INFRA_SAME_MAX):
            infra_requeue_condition(st, d, e, why, reason, n_same, age, detail, repo)
    except (Exception, SystemExit) as x:
        err(f"{e['id']}: re-queued {n_same} times for {reason}, and its condition could not be opened ({str(x)[:160]})")


INFRA_SAME_MAX = 30 * 60
STALLED_REASON = "its checks stalled"
STALL_KEEP_PLACE = 2  # stalls in a row re-queued with no backoff (the change keeps its age-order place), then backed off
FETCH_MAIN_FAILED = "its fetch of main failed"


def infra_reason_of(why: str) -> str:
    """An infra re-queue's reason without its details (what differs between two re-queues for the same cause)."""
    return re.sub(r"\s*\(.*?\)", "", why).split(":")[0].strip()


def infra_requeue_condition(st: Any, d: str, e: dict[str, Any], why: str, reason: str, n: int, age: float, detail: str | None,
                            repo: str | None) -> Any:
    """The condition of a change re-queued n times for the same infra reason: main's fetch failing is the clone's
    (one per clone, cleared when it verifies and fetches main again); any other reason the change's own (cleared when
    it gets past it)."""
    text = (f"STALLED: {e['id']} re-queued {n} times in {fmt_age(age)} for the same infra reason ({reason}); it lands "
            f"only once that clears. Last: {why}")
    tail = (detail or why).strip().splitlines()[-15:]
    if reason == FETCH_MAIN_FAILED:
        repo = repo or repo_of_waiting(d)
        return open_condition("STALLED", f"{text}. Every change's check fetches main: the landing is blocked",
                              run_probe("clone_health", {"repo": repo}), [],
                              key=f"main-fetch-{os.path.basename(os.path.abspath(repo))}", command="land", tail=tail, store=st)
    return open_condition("STALLED", text, run_probe("infra_requeue", {"d": d, "eid": e["id"], "reason": reason}), [],
                          key=f"infra-requeue-{e['id']}", command="land", cwd=e.get("worktree"), tail=tail, store=st)


def push_credential_condition(st: Any, d: str, why: str, detail: str | None, repo: str | None) -> Any:
    """GitHub never got, or refused, this clone's push credential: ONE condition per clone, opened at the first such
    re-queue and naming every change it holds, cleared by the next push that succeeds (probe_push_credential) -- not an
    "infra failure" retried on a timer per change (2026-11-22: four changes held for hours by a dead helper)."""
    repo = repo or repo_of_waiting(d)
    held = credential_held(waiting_entries(d, open_only=False))
    text = (f"PUSH CREDENTIAL: GitHub refused {host_role()}'s push credential for {os.path.basename(repo)}; "
            f"{len(held)} change(s) wait on it ({', '.join(held[:6])}). Not the vault's and not the changes': "
            f"Its credential helper there must work again (the App helper on ci-runner, gh's login on the laptop). "
            f"Cleared by the next check-branch push that succeeds. Last: {redact_credentials(why)}")
    tail = redact_credentials(detail or why).strip().splitlines()[-15:]
    probe = run_probe("push_credential", {"d": d, "since": time.time(), "host": host_role()})
    return open_condition("STALLED", text, probe, [], key=f"push-credential-{os.path.basename(os.path.abspath(repo))}",
                          command="land", tail=tail, grace=0, store=st)


def credential_held(entries: list[dict[str, Any]]) -> list[str]:
    """The ids of the changes waiting on a push credential: queued or checking, re-queued for a credential failure."""
    return [str(x.get("id")) for x in entries if x.get("status") in ("queued", "checking")
            and credential_failure(x.get("why"))]


def probe_push_credential(a: dict[str, Any]) -> tuple[bool | None, str]:
    """A check-branch push of the clone's queue succeeded since the condition opened (save_remote's pushed_at), or no
    change waits on a credential any more. Read from the queue's own records ONLY: never a credential helper (the
    GitHub App's mints a token per call, 2026-11-22 review), and only on the host whose push failed (a["host"]):
    another host's clone and helper say nothing about it, so there it cannot tell."""
    if a.get("host") != host_role():
        return None, f"read on {a.get('host')} only, where the push failed"
    entries = waiting_entries(a["d"], open_only=False)
    pushed = max((float((x.get("remote") or {}).get("pushed_at") or 0) for x in entries), default=0.0)
    if pushed > float(a.get("since") or 0):
        return True, f"a check-branch push succeeded at {iso_at(pushed)}"
    held = credential_held(entries)
    return not held, f"{len(held)} change(s) wait on it" if held else "no change waits on it"


def probe_infra_requeue(a):
    """Change a['eid'] got past a['reason']: decided, checked, gone, or re-queued for another reason."""
    e = read_record(os.path.join(a["d"], "entries", f"{a['eid']}.json")) or {}
    stuck = e.get("status") in ("queued", "checking") and e.get("infra_reason") == a.get("reason")
    return not stuck, f"entry {a['eid']}: {e.get('status') or 'gone'}" + (f" ({e.get('why')})" if stuck else "")


# Main moved between a change's check and its push: FULLY re-checked only when what landed meanwhile can change what the
# full check judged (the owner, 2026-10-27: memo-key -41 passed runs 3341 and 3350, then main moved and it was checked a
# third time). FILE GRANULARITY (the owner, 2026-11-16, "but always recompile and unit test": at module granularity
# nearly every change shared worker or scrapers with what landed, and changes looped 3-4 full rounds of 15-20 min, some
# over two test files in worker/src/test). THE RULE, judged against the base of the change's last FULL check:
#   - what landed changes the build or the hook (RECHECK_ALWAYS, scripts/ci/precommit-share.sh included: it picks what
#     each CI share of the check runs) -- but not the hook's own self-tests (HOOK_SELF_TEST: scripts/hooks/*-test.sh,
#     *-test-lib.sh, *_test.py; the owner, 2026-11-16): no landing check runs one but by its `scripttests` check, for
#     a change selecting it, like any script's test, so they get the verification round below -- or
#   - a path both sides change (added, modified or deleted; a rename counts as both of its paths), or
#   - a path one side deletes (a rename's old path included) whose file name a file the other side changes names
#     (`git grep -F <basename>` over those files at the rebased tip: a script, fixture or test reading it by name; a
#     Scala reference by class name the combined compile below catches)
# -> a new FULL round. Otherwise no full re-check, but ALWAYS the COMBINED-TREE VERIFICATION (VERIFY_MODE): the rebased
# tip checked once more, on the CI tier like any check, with the hook's `--verify-only <full base>..<tip>` -- every test
# config compiled, the lint and fast specs, and the unit tests of every module either side touches -- then pushed with
# the change's own checks skipped at the push (PREPUSH_SKIP: the hook's plan for its paths; the red gate still runs).
# A hook without --verify-only gets the full round, as before the rule. A rebase conflict is NOT LANDED, as ever.
# SELECTION-AWARE (the owner, 2026-11-17: 17 full rounds of ~40 min since 11-16 06:00, nearly all for a hook or registry
# change): when every forcing path that landed only steers WHAT the check selects (SELECTION_PATHS: the hook, its lint
# registry, local-tests.txt), the hook is asked what it selects for the change's paths at the tip the full check passed
# (full_tip) and at the rebased tip: its --plan, --compile-plan, --unit-plan and --lint-plan, and local-tests.txt's
# lines for every area the plan runs (`basic` with sbt). The verify round runs the NEW hook's compile plan (every test
# config), lint plan and unit plan over full_base..tip, a superset of the change's paths, so what those add is run there
# (the added selection, and only it, on top of the verification); a check --plan adds, or an area line local-tests.txt
# adds, it does not run: then the full round. Removals need nothing. An unreadable selection or no full_tip recorded:
# the full round. BUILD-AWARE (the owner, 2026-11-17: 6 full rounds of ~22 min that day for build.sbt / project/
# churn): what landed changes a root *.sbt or project/ -> the same selection test, AND the build as the change's
# modules see it (buildview.py's build_view_change: build.sbt's build-wide statements, the change's and its selection's module
# definitions with everything they name -- settings helpers, configurations, the projects they dependsOn, the
# project/ members -- at full_tip and at the rebased tip) unchanged, and no file of the change naming the changed build
# file (`git grep -F <basename>`: a script or test reading it) -> the verify round, which compiles every test config
# under the new build; anything changed, unreadable, or the change touching the build itself -> the full round.
# SHARE-AWARE (the owner, 2026-11-18, item 6: 1242 min of full rounds in a week for precommit-share.sh or the registry):
# scripts/ci/precommit-share.sh is a SELECTION_PATH too -- it only splits the hook's selection over the CI check's jobs
# -- and every selection read also holds the union of what its shares (SHARES) run for the change: a check one of
# them adds forces the full round, as a check --plan adds does. A file BOTH sides change stays a full round, these too.
# MODULE OVERLAP (the same decision): no shared file but an sbt module both sides touch -> the verify round plus the
# change's AREA tests (verify_areas: a check/verifyareas- branch; module_overlap), where the tree's precommit-share.sh
# runs that mode; the risk of a cross-change break the area tests miss is accepted (Main catches it).
# The other forcing paths (scripts/hooks/* beyond SELECTION_PATHS, hooks-path.sh, ...) stay a full round.
RECHECK_ALWAYS = re.compile(r"^([^/]+\.sbt|project/.*|scripts/hooks/.*|scripts/hooks-path\.sh"
                            r"|scripts/tool-conventions\.sh"
                            r"|scripts/ci/precommit-share\.sh)$")
HOOK_SELF_TEST = re.compile(r"^scripts/hooks/[^/]+(-test\.sh|-test-lib\.sh|_test\.py)$")
VERIFY_MODE = "--verify-only"
SHARE_SCRIPT = "scripts/ci/precommit-share.sh"
SELECTION_PATHS = frozenset({"scripts/hooks/pre-push", "scripts/hooks/lint-registry.tsv", "scripts/hooks/local-tests.txt",
                             SHARE_SCRIPT})
SELECTION_MODES = ("--plan", "--compile-plan", "--unit-plan", "--lint-plan")
# what each share of the CI check runs for the change (precommit-share.sh `checks <share>`, the hook's own PREPUSH_LIST
# under the share's environment), read on a check/<id> ref -- the change's own checks, its range the change since main.
# No `oldrefs` since 2026-11-21: the old-ref compile runs on Main, after landing, and the script refuses the name.
SHARES = ("rest", "identity", "plan")
SHARE_KEY = "shares"
# MODULE OVERLAP (the owner, 2026-11-18): a verify round whose two sides touch one sbt module also runs that change's
# AREA tests -- a check/verifyareas-<full base>-<id> branch, which a precommit-share.sh naming the mode runs with the
# area shares (identity) in --checks-only over <full base>..HEAD; one that does not name it gets the plain verify round
VERIFY_AREAS_MODE = "verifyareas"
VERIFY_RUNS_ADDED = frozenset({"--compile-plan", "--unit-plan", "--lint-plan"})  # the verify round runs what these add


def range_paths(cwd, a, b, deleted=False):
    """The paths a..b changes (both sides of a rename); deleted: only those it deletes (a rename's old path too)."""
    flt = ["--diff-filter=D"] if deleted else []
    return [p for p in git(cwd, "diff", "--name-only", "--no-renames", *flt, a, b).splitlines() if p]


def hook_query(hook, tree, flag, paths):
    """The hook's dry-run answer for paths on stdin (`--plan`, `--compile-plan`), as words; None when unreadable."""
    try:
        r = process_runner().run([hook, flag], cwd=tree, input="".join(f"{p}\n" for p in paths), capture_output=True,
                           text=True, timeout=120, env=tool_env())
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.split() if r.returncode == 0 else None


def verify_supported(hook):
    """Whether the hook (a path) has the combined-tree verification mode."""
    try:
        with open(hook) as f:
            return VERIFY_MODE in f.read()
    except (OSError, TypeError):
        return False


def named_reader(tree, tip, paths, readers):
    """(path, reader) of a path (a deleted one, a changed build file) whose file name a reader names at tip; None when
    none does."""
    readers = [p for p in readers if git_ok(tree, "cat-file", "-e", f"{tip}:{p}")]
    if not paths or not readers:
        return None
    for p in sorted(paths):
        r = git(tree, "grep", "-l", "-F", "-e", os.path.basename(p), tip, "--", *readers, check=False)
        hits = sorted(line.split(":", 1)[1] for line in r.stdout.splitlines() if ":" in line)
        if hits:
            return p, hits[0]
    return None


def forces_full_recheck(path):
    """Whether a path landing meanwhile forces a FULL re-check whatever the change touches (THE RULE's first clause)."""
    return bool(RECHECK_ALWAYS.match(path)) and not HOOK_SELF_TEST.match(path)


def hook_selection(tree, paths):
    """What the tree's hook selects for paths (SELECTION-AWARE, above): {mode: words} for each dry-run mode the hook has,
    and local-tests.txt's lines for the areas its plan runs; None when a mode it has cannot be read."""
    out = hook_mode_selections(precheck_hook(tree), tree, paths)
    if out is None:
        return None
    shares = share_selection(tree)
    if shares is False:
        return None
    if shares is not None:
        out[SHARE_KEY] = shares
    out["local-tests"] = local_test_lines(tree, out.get("--plan", []))
    return out


def hook_mode_selections(hook, tree, paths):
    """{mode: words} of each dry-run mode the hook has, or None when the hook or a mode it has cannot be read."""
    try:
        with open(hook or "") as f:
            text = f.read()
    except OSError:
        return None
    out = {}
    for mode in SELECTION_MODES:
        if f"{mode})" not in text:  # never asked: an unknown flag would run the hook's real push checks
            continue
        out[mode] = hook_query(hook, tree, mode, paths)
        if out[mode] is None:
            return None
    return out


def local_test_lines(tree, plan):
    """local-tests.txt's lines for the areas the hook's plan runs (the basic tests with sbt in it)."""
    areas = {"basic"} if "sbt" in plan else set()
    areas |= set(plan)
    try:
        with open(os.path.join(tree, "scripts", "hooks", "local-tests.txt")) as f:
            return [line.rstrip("\n") for line in f if line.split()[:1] and line.split()[0] in areas]
    except OSError:
        return []


def share_selection(tree):
    """The union of what precommit-share.sh's shares run for the change at the tree's HEAD (sorted words); None when the
    tree has no such script, False when a share cannot be read. Read as a change's own check (GITHUB_REF_NAME check/<x>,
    --checks-only): its range is HEAD since its merge base with origin/main, which in a check tree is the change."""
    script = os.path.join(tree, SHARE_SCRIPT)
    if not os.access(script, os.X_OK):
        return None
    words = set()
    env = {**tool_env(), "GITHUB_REF_NAME": "check/selection"}
    for share in SHARES:
        try:
            r = process_runner().run([script, "checks", share], cwd=tree, capture_output=True, text=True, timeout=120,
                               env=env, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired):
            return False
        if r.returncode != 0:
            return False
        words |= set(r.stdout.split())
    return sorted(words)


def sbt_module_of(path):
    """The sbt module a path is in (`worker` for worker/src/...), or None."""
    parts = path.split("/")
    return parts[0] if len(parts) > 2 and parts[1] == "src" else None


def module_overlap(landed_paths, change_paths):
    """The sbt modules both sides touch (MODULE OVERLAP), sorted."""
    landed = {sbt_module_of(p) for p in landed_paths} - {None}
    return sorted(landed & ({sbt_module_of(p) for p in change_paths} - {None}))


def verify_areas_supported(tree):
    """Whether the tree's precommit-share.sh runs the area tests of a check/verifyareas- branch."""
    try:
        with open(os.path.join(tree, SHARE_SCRIPT)) as f:
            return f"{VERIFY_AREAS_MODE}-" in f.read()
    except OSError:
        return False


def selection_at(tree, rev, tip, paths):
    """hook_selection at rev, read in tree (checked out at tip, and back at tip after); None when rev is unknown."""
    if not rev or not git_ok(tree, "cat-file", "-e", f"{rev}^{{commit}}"):
        return None
    if rev == tip:
        return hook_selection(tree, paths)
    git(tree, "checkout", "-q", "--force", "--detach", rev)
    try:
        return hook_selection(tree, paths)
    finally:
        git(tree, "checkout", "-q", "--force", "--detach", tip)


def selection_change(tree, full_tip, tip, paths, new=None):
    """None when the hook selects nothing at tip for paths that the verify round leaves unrun and the full check at
    full_tip did not run (SELECTION-AWARE, above); else what it adds, in words. new: the selection at tip, when read."""
    old, new = selection_at(tree, full_tip, tip, paths), new or selection_at(tree, tip, tip, paths)
    if old is None or new is None:
        return "its selection at the full check cannot be read" if old is None else "its selection cannot be read"
    for key in sorted(set(old) | set(new)):
        added = selection_added(key, old, new)
        if added is not None:
            return selection_added_words(key, added)
    return None


def selection_added_words(key: Any, added: Any) -> str:
    """`its <key> for the change adds a, b, c (+n more)`: the first three of what the selection adds, or a mode."""
    return f"its {key.lstrip('-')} for the change adds {', '.join(added[:3]) or 'a mode'}" \
        + (f" (+{len(added) - 3} more)" if len(added) > 3 else "")


def selection_added(key: Any, old: Any, new: Any) -> list[str] | None:
    """What the selection `new` adds under key to `old`'s (a list: empty for a mode that was not selected at all), or None
    when it adds nothing the verify round leaves unrun."""
    if key in VERIFY_RUNS_ADDED:
        return None
    added = [w for w in (new.get(key) or []) if w not in set(old.get(key) or [])]
    return added if added or (key not in old and key in new) else None


def land_holds(st):
    return st.items("hold", ["held"])


def land_hold(st: Any, now_: float | None = None, expire: bool = True) -> dict[str, Any] | None:
    """The land hold in force, or None. A hold past its deadline is moved to `expired` here (an event says so): every
    reader -- the tick, `land status`, the snapshot -- lifts a forgotten hold the moment it is due. expire=False: only
    read (the status page never writes)."""
    t = now_ if now_ is not None else time.time()
    live = None
    for it in land_holds(st):
        if it["deadline"] <= t:
            if expire:
                expire_land_hold(st, it)
        elif live is None or it["deadline"] > live["deadline"]:
            live = it
    return live


def expire_land_hold(st: Any, it: dict[str, Any]) -> None:
    """A hold past its deadline moved to `expired`, an event saying who held it and why (one already moved: left)."""
    try:
        st.transition("hold", it["id"], "expired", it["version"],
                      event={"text": f"land hold {it['id']} expired (held by {it['data'].get('by')}: "
                                     f"{it['data'].get('why') or 'no reason given'})"})
    except (StaleVersion, TerminalState):
        pass


def hold_text(h: Any, t: float | None = None) -> str:
    """`HELD until HH:MM (by, why)` for a hold row ({deadline, data} or the snapshot's {until, by, why})."""
    until = h.get("until") or h.get("deadline")
    d = h.get("data") or h
    return (f"HELD until {time.strftime('%H:%M', time.localtime(float(until)))} (`agentctl land release`; "
            f"by {d.get('by') or '?'}" + (f": {d['why']}" if d.get("why") else "") + ")")


def hold_row(it):
    return {"id": it["id"], "until": it["deadline"], "by": it["data"].get("by"), "why": it["data"].get("why")} if it else None
