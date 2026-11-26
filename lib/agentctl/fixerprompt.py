# agentctl, part `fixerprompt`: the red fixer's and the lander's prompts: the brief, its tasks and replay notes
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import re
from typing import Any
from agentctl.selfcode import TOOL_PATH
from agentctl.redknobs import knob_brief_src
from agentctl.fixerknobs import FIXER_UNLANDED_NUDGE
from agentctl.core import err, remote, tilde
from agentctl.repos import RED_PRIMARY, red_repo_clone, red_repo_of
from agentctl.workflows import LOCAL_FLOW
from agentctl.classify import is_regression
from agentctl.redrepos import red_owner_only
from agentctl.redrecord import RED_RECURRING
from agentctl.fixerland import LAND_BRIEF
from agentctl.redfixes import red_refuted
from agentctl.redland import FIX_REPLAY_NONE_MIN, red_replay_command


# THE FIXER BRIEF (the owner, 2026-10-30): the rules every dispatched fixer and family owner gets -- fast reproducer
# first, mechanism-only identity fixes, no allow-list or baseline edits, land as you go, `agentctl issue` for defects,
# bounded commands -- live in ONE tracked file, docs/fixer-brief.md beside the entry, and go into the prompt word for
# word. The code adds only what is particular to the red (its run, job, line, lease) and to the moment (the nudge time).
FIXER_BRIEF = os.path.join(os.path.dirname(TOOL_PATH), "docs", "fixer-brief.md")


REPO_SECTION = re.compile(r"^### REPO (\S+)\s*$", re.M)


def fixer_brief_for(text, o_r):
    """The brief for a red of o_r: everything but the `### REPO <owner/name>` sections of OTHER repos (each section
    runs to the next `#`/`##`/`### REPO` heading)."""
    out, pos = [], 0
    for m in REPO_SECTION.finditer(text):
        out.append(text[pos:m.start()])
        nxt = re.compile(r"^(#{1,2} |### REPO )", re.M).search(text, m.end())
        end = nxt.start() if nxt else len(text)
        if m.group(1) == o_r:
            out.append(text[m.start():end])
        pos = end
    out.append(text[pos:])
    return re.sub(r"\n{3,}", "\n\n", "".join(out)).strip()


def fixer_brief(o_r=None):
    """(path, text) of the fixer brief: $AGENTCTL_BRIEF_SRC when it names a readable file (an override, for trying a
    wording out), else the tracked docs/fixer-brief.md -- with only o_r's per-repo section (default the primary). Never
    raises: a missing brief is said in the prompt."""
    for p in (knob_brief_src(), FIXER_BRIEF):
        if p:
            try:
                with open(p) as f:
                    return p, fixer_brief_for(f.read().strip(), o_r or RED_PRIMARY)
            except OSError as e:
                err(f"fixer brief unreadable ({str(e)[:160]})")
    return FIXER_BRIEF, (f"(THE FIXER BRIEF IS MISSING at {FIXER_BRIEF}: file it with `~/bin/agentctl issue`, then go on "
                         "under ~/bin/AGENTCTL.md's rules.)")


# OWNER-ONLY REPOS (red-repos.json `"push": "owner-only"`; infra: a push to main is a deploy). Their fixers never land,
# push or deploy: the prompt says so (red_owner_rules, after every task text that says "land it"), the session denies
# the commands (OWNER_ONLY_DENIED), `agentctl land` refuses inside such a fixer, and `agentctl red owner-push` files the
# prepared branch with the owner as a one-liner.
OWNER_ONLY_DENIED = ("Bash(*agentctl land*)", "Bash(*bin/deploy*)", "Bash(*nixos-rebuild*)",
                     "Bash(*switch-to-configuration*)", "Bash(*git*push*)")


def red_owner_rules(o_r, num):
    return (f" OWNER-ONLY REPO ({o_r}): this overrides every `land` above. NEVER `agentctl land`, never push, never "
            "deploy: a push to its main IS a deploy, and it is the owner's. Prepare the fix on your worktree's branch "
            f"(rebased on {remote()}/main, one commit per step, a `Fixes-Red: {num}` trailer, the repo's own checks run), "
            f"then `~/bin/agentctl red owner-push {num} --note \"<what it fixes; which units a deploy restarts>\"` from "
            "your worktree, and end with `BLOCKED ON OWNER: <the one-liner it printed>`.")


def fixer_prompt(u, wt, brief, resume_note=None, owner=None):
    """The fixer's prompt for red u; owner: a family owner's turn ({family, notebook, reds}), whose leases its wrapper
    holds (no claim) and whose session lives on across reds."""
    num = u["run"].get("number")
    o_r = red_repo_of(u)
    common = fixer_preamble(u, wt, brief, owner, o_r)
    task = fixer_task(u, wt)
    if u.get("decision"):  # THE OWNER ANSWERED this red's question (`agentctl decide`): act on that answer
        task = owner_decided_note(u["decision"]) + task
    task += WAITS_ON_BRIEF + LAND_BRIEF + fixer_task_context(u, o_r, num) + fixer_task_verdicts(u)
    return common + task + early_brief_of(u) + (f" {resume_note}" if resume_note else "")


def fixer_preamble(u: Any, wt: Any, brief: Any, owner: Any, o_r: str) -> str:
    """What every fixer prompt opens with: who it is, the brief, its worktree, and the replays its land will demand."""
    num = u["run"].get("number")
    # WHO YOU ARE comes in the prompt, never the environment: a `claude --bg` session inherits the claude daemon's
    # environment -- whichever fixer's wrapper spawned it (2026-10-29: every background fixer was 'red-fixer-2473')
    fx = f" --fixer {u['record']}" if u.get("record") else ""
    first = "" if owner else f"FIRST run `~/bin/agentctl red claim {num}{fx} --note \"<one line: what you are doing>\"`. "
    who = (owner_intro(owner) if owner else
           "agentctl revived you (tier lander, blame.py), a background session the owner sees in `claude agents`. "
           if u.get("lander") else
           "You are an unattended red-build fixer dispatched by agentctl (nobody else was fixing this red), a background "
           "session the owner sees in `claude agents`. ")
    bpath, btext = fixer_brief(o_r)
    if o_r != RED_PRIMARY:  # the movies preamble is movies'; the repo's own CLAUDE.md is the first read here
        brief = f"{o_r}'s CLAUDE.md in your worktree"
    common = (who + (f"YOUR RED IS IN {o_r} (its per-repo rules are in the brief below). " if o_r != RED_PRIMARY else "")
              + f"Read {brief} and ~/bin/AGENTCTL.md first. Your worktree is {wt} (branch off {remote()}/main, already "
              "created): work only there. " + first + "Commits unlanded for "
              f"{FIXER_UNLANDED_NUDGE // 60} min get a RED-NUDGE.md in your worktree (read it, land, delete it), then an "
              f"escalation to the owner.\n\n=== THE FIXER BRIEF ({tilde(bpath)}) ===\n{btext}\n"
              "=== END OF THE FIXER BRIEF ===\n\n")
    common += fixer_replay_notes(u, owner, o_r)
    if u.get("orphan"):  # an unlanded change whose author ended (orphan_duty)
        common = common.replace(first, "").replace(f"(branch off {remote()}/main, already created)", "(the ended author's own)")
    elif u.get("lander"):  # THE LANDER REVIVED (blame.py): its change reverted, it fixes and re-lands it
        common = common.replace(f"(branch off {remote()}/main, already created)", "(yours)")
    return common


def fixer_replay_notes(u: Any, owner: Any, o_r: str) -> str:
    """The replays the fixer's land will demand (redreplay.py) and the warning when fixes of this red already failed."""
    common = ""
    replay = red_replay_command(red_repo_clone(o_r), u) if not owner else None
    if replay:  # FIX REPLAY (redland.py): its land is refused without the replay's base=FAIL head=PASS
        common += (f"REPLAY BEFORE LANDING: this red's check replays cheaply: `{replay}`. Run it at {remote()}/main "
                   "(base) and at your fix (head) and land only when it says base=FAIL head=PASS, with the trailer "
                   f"`Fix-Replay: {replay} base=FAIL head=PASS` beside Fixes-Red. ")
    if u.get("gate") and not owner:  # FIX REPLAY FOR GATE REDS (redland.py; the owner, 2026-11-20): enforced
        common += ("REPLAY YOUR GATE FIX: beside Fixes-Red, add the trailer `Fix-Replay: <command>` -- the narrowest "
                   "command that fails where this red fails (a `testOnly`, a script's self-test) -- and agentctl's "
                   f"land runs it at {remote()}/main and at your fix: the change counts as the fix only on base=FAIL "
                   "head=PASS. A red that genuinely cannot replay (infra, a CI-only race): `Fix-Replay: none — <why>`, "
                   f"a specific reason of {FIX_REPLAY_NONE_MIN}+ characters (no n/a, flaky or can't), which the owner "
                   "reads weekly. land refuses a gate fix with neither. ")
    if len(red_refuted(u)) >= RED_RECURRING:
        common += (f"RECURRING: this failure outlived {len(red_refuted(u))} landed fixes ({', '.join(x[:10] for x in red_refuted(u))}): "
                   "each treated a symptom. Read them (`git show`), find the ROOT CAUSE they all missed, and fix that; "
                   "another symptom fix is not a fix. ")
    return common


def fixer_task(u: Any, wt: Any) -> Any:
    """What the fixer is asked to do, by the kind of red it was dispatched for."""
    num, rid = u["run"].get("number"), u["run"].get("databaseId")
    if u.get("orphan"):
        return orphan_task(u)
    if u.get("lander"):
        return lander_prompt(u, wt)
    if u.get("diagnosis"):  # a diagnose-only fixer found the cause: this one makes the durable fix
        return diagnosed_task(u, num, rid)
    if u["workflow"] == LOCAL_FLOW:
        return local_check_task(u)
    if u["kind"] in ("infra", "unclear"):  # the group's primary: no test failure in the run at all
        return infra_task(u, num, rid)
    return red_run_task(u, num, rid)


def orphan_task(u: Any) -> str:
    """An unlanded change whose author ended (orphan_duty): make it land."""
    return (f"This worktree holds commits that are NOT on {remote()}/main, and the agent that wrote them has ended. Its last "
            f"verdict: {str(u.get('line') or 'none')[:400]!r}. Make it land: `git fetch {remote()} && git rebase "
            f"{remote()}/main` (never squash), fix what the checks reject (read the failing checks' output; never loosen a "
            "test, never skip a check), then `~/bin/agentctl land`. If the change is wrong or "
            "already on main in another form, say so. End at `LANDED <sha>`, or `GAVE UP: <why>`.")


def diagnosed_task(u: Any, num: Any, rid: Any) -> str:
    """A diagnose-only fixer found the cause: the durable fix is this fixer's."""
    return (f"Red run {u['workflow']} #{num} (run id {rid}), job `{u['job']}`, was DIAGNOSED by a diagnose-only fixer: "
            f"{u['diagnosis'][:600]!r}. The diagnosis is not the end, and nor is a 'one-off' verdict: make the DURABLE "
            "fix. Remove the cause (code, CI config, the test harness) if it is in the repo. If it really lies outside "
            "the code (a crashed browser, a runner, a transient), land instead the means to diagnose it next time: keep "
            "the failing process's stderr and exit status in the job log or its artifacts (e.g. Chrome's stderr, which "
            "CdpDriver discards), a retry-free guard that names the cause. `~/bin/agentctl ci-why " + str(rid) + "` "
            "explains the run. Never rerun to green, never loosen a test. Commit with a `Fixes-Red: " + str(num) + "` "
            "trailer and land it with `~/bin/agentctl land`. End at `LANDED <sha>`; if you cannot, "
            "`GAVE UP: <why>`; if the fix needs the owner's decision (a design change the red rules do not let you "
            "make, in an owner-only category: the brief's ASK THE OWNER ONLY WHAT NO AGENT CAN DO), `DIAGNOSED: <cause>; decide[<category>]: <the decision>` (the red waits on the owner with it); if it needs the owner, `~/bin/agentctl red block " + str(num) + " --owner-cause <kind> --cmd \"<command>\"` (only for a cause outside the code that only the owner can act on: never a code red) "
            "and end `BLOCKED ON OWNER: <command>`.")


def local_check_task(u: Any) -> str:
    """A pre-push check that fails on plain main."""
    return (f"The pre-push check `{u['job']}` FAILS ON PLAIN {remote()}/main {str(u['run'].get('headSha'))[:10]} (no "
            f"change needed to see it; first failure line: {u.get('line', '')[:200]!r}). scripts/hooks/pre-push "
            "documents each check; reproduce it in your worktree, root-cause and fix it (never loosen the check), "
            "commit with a `Fixes-Red: local` trailer and land with `~/bin/agentctl land`. End "
            "at `LANDED <sha>`, or `GAVE UP: <why>`.")


def infra_task(u: Any, num: Any, rid: Any) -> str:
    """An infra-shaped or unclear red: diagnose only, no code change."""
    return (f"Red run {u['workflow']} #{num} (run id {rid}) failed in job `{u['job']}`, and ci-why calls it "
            f"{'INFRA-SHAPED' if u['kind'] == 'infra' else 'UNCLEAR (no FAILED/error line: runner lost? log truncated?)'}. "
            f"DIAGNOSE ONLY: `~/bin/agentctl ci-why {rid}`, the CI runners' health (~/bin/ci-runners-page, the "
            "ci-runner host's systemd units over ssh, read-only), disk, docker, the tunnel. Make NO code change and "
            "do not rerun anything to get green. If it needs a deploy, a restart or any owner action, run "
            f"`~/bin/agentctl red block {num} --owner-cause <kind> --cmd \"<the exact command the owner must run>\"` and end with a line "
            "`BLOCKED ON OWNER: <command>`; otherwise end with `DIAGNOSED: <cause>; durable fix: <what to change, or "
            "what to log so the next occurrence names its cause>` -- an implementer is dispatched with it at once.")


def red_run_task(u: Any, num: Any, rid: Any) -> str:
    """An ordinary red run: root-cause it and land the fix."""
    return (f"Fix red run {u['workflow']} #{num} (run id {rid}), failing in "
            + (f"jobs {', '.join(u['jobs'])} (first: `{u['job']}`)" if len(u.get("jobs") or []) > 1 else f"job `{u['job']}`")
            + (f" with: {u['line'][:200]!r}" if u.get("line") else "") +
            f". `~/bin/agentctl ci-why {rid}` explains it. Root-cause it (never rerun to green, never loosen a test "
            "or a budget to pass), commit the fix with a `Fixes-Red: " + str(num) + "` trailer, and land it with "
            "`~/bin/agentctl land`. End at `LANDED <sha>`. If you cannot fix it, end with a line "
            "`GAVE UP: <why>`; if it needs the owner, `~/bin/agentctl red block " + str(num) + " --owner-cause <kind> --cmd \"<command>\"` (only for a cause outside the code that only the owner can act on: never a code red) "
            "and end `BLOCKED ON OWNER: <command>`.")


def owner_decided_note(dec: Any) -> str:
    """The opening of a red's task once the owner answered its question (`agentctl decide`): act on that answer."""
    return (f"THE OWNER DECIDED this red: asked {str(dec.get('question'))[:400]!r}, the owner answered "
            f"{str(dec.get('answer'))[:1500]!r}. Act on exactly that answer (it settles what the fixers could not "
            "decide) and land the result. If acting on it fails (the land is NOT LANDED, a check rejects it, it "
            "cannot be done), end `NOT LANDED: <why>` or `GAVE UP: <why>`: the owner is told at once. ")


def fixer_task_context(u: Any, o_r: str, num: Any) -> str:
    """What a red's task gets appended about where it stands: the owner-only repo's rules, an old newest run, the work a
    previous fixer left, the branch it is on."""
    task = ""
    if red_owner_only(o_r):  # every task above says "land it": not in an owner-only repo
        task += red_owner_rules(o_r, num)
    if u.get("repo_note"):  # the repo's own rules for its fixer (red-repos.json `note`), over every `land` above
        task += f" REPO RULES ({o_r}), overriding any `land` above: {u['repo_note']}"
    if u.get("old_note"):
        task += (f" This red's newest run is old and {u['old_note']}: decide whether it still fails (fix it), or why it "
                 "cannot recur (land the change that makes that so, e.g. its workflow fixed or removed), and say which.")
    pw = u.get("prior_work") or {}
    if pw.get("commits"):
        task += (f" A PREVIOUS FIXER of this red left commits not on {remote()}/main in {pw['worktree']} "
                 f"({', '.join(pw['commits'])}): read them first (`git -C {pw['worktree']} log -p {remote()}/main..HEAD`). If "
                 "they are (part of) the fix, cherry-pick them into your worktree (never squash; keep their messages, add "
                 f"the `Fixes-Red: {num}` trailer) and land them; never leave them behind unexamined.")
    if u.get("predicts"):
        task += (f" This red is on branch {u['predicts']}, not main yet: a run of commits that have since landed on main, "
                 "with main's inputs, so main will fail the same way next. Fix it on main before it does.")
    return task


def fixer_task_verdicts(u: Any) -> str:
    """What the red's own failure line and blame add to its task: a regression or an order dependence, a flake, the blame's note."""
    task = ""
    if is_regression(u.get("line")):  # THE ORDER VERDICT (classify.py): the control pass failed in default order too
        task += (" REGRESSION, NOT AN ORDER DEPENDENCE: its control pass ran the failing tests again in DEFAULT order at "
                 "the same commit and they failed there too, so the order they ran in is not the cause and no ordering "
                 "fix is one. Go after the COMMIT THAT BROKE THEM: the range from this workflow's newest green run on "
                 f"{remote()}/main to this run's head {str(u['run'].get('headSha') or '')[:10]} (`git log --oneline "
                 "<green head>..<red head> -- <the tests' and their code's paths>`); reproduce the failure with a plain "
                 "`testOnly` of the named tests, then fix that change forward (or revert it) -- and say which commit it "
                 "was in the commit message.")
    elif str(u.get("line") or "").startswith("ORDER DEPENDENCE (or a flake):"):
        task += (" ORDER DEPENDENCE (or a flake): its control pass ran the failing tests in DEFAULT order at the same "
                 "commit and they PASSED: find the state one test leaves for another (a singleton, a file, the clock, a "
                 "cache) with the replay the line names, and make each test set up its own.")
    if u.get("flake"):
        task += (" This red PASSED on the next run: it is a FLAKE. Make it deterministic (find the race, the clock, "
                 "the ordering); do not just retry it.")
    if u.get("blame_note"):  # the red's blame (blame.py): the offending change, its revert, what to do with it
        task += u["blame_note"]
    return task


def early_brief_of(u: dict[str, Any]) -> str:
    """An EARLY red's words for its fixer (redearly.py writes them into its `early`: the run still goes on), else ""."""
    return str(dict(u.get("early") or {}).get("brief") or "")


FIXER_IDENTITY_ENV = ("AGENTCTL_AGENT_NAME", "AGENTCTL_RED_FIXER", "AGENTCTL_FIXER_RECORD", "AGENTCTL_CLAIM_PID",
                      "AGENTCTL_LEASE_SECRET", "AGENTCTL_LEASE_SECRETS")


WAITS_ON_BRIEF = (" If this red's fix is ANOTHER red's commit that is not on main yet, land nothing and end "
                  "`WAITS ON <sha> (red <its key>): <why>`: agentctl waits for that commit (or its rebased copy) to reach "
                  "main and verifies this red against the next run containing it -- never a NOT LANDED for such a hold.")


def owner_intro(owner):
    fam = owner["family"]
    reds = "; ".join(owner.get("reds") or [])
    return (f"You are the LONG-LIVED OWNER of the `{fam}` red family: one background session (the owner sees it in "
            "`claude agents`) that keeps every red of the family, across reds and restarts, in ONE worktree. Your "
            f"NOTEBOOK is {owner['notebook']}: read it FIRST, and keep it current after every step (the reproducer you "
            "built and how to run it, each hypothesis and how it ended, what is left) -- a restart of you continues "
            "from it and this same worktree. Your reds' leases are held for you by agentctl while you work (do not "
            "`red claim` them); new reds of your family arrive in RED-INBOX.md and as your next turn. "
            + (f"This turn's reds: {reds}. " if reds else "")
            + "End each turn with ONE verdict line for the turn's reds (LANDED <sha> / WAITS ON <sha> (red <key>): / "
              "GAVE UP: / DIAGNOSED: / BLOCKED ON OWNER:). When the durable fix needs the OWNER'S DECISION in an owner-only "
              "category (the brief's ASK THE OWNER ONLY WHAT NO AGENT CAN DO), end `DIAGNOSED: <cause>; "
              "decide[<category>]: <the decision only the owner can make>`: the red then waits on the owner with it. "
              "Any other question is yours: proceed under the repo's standing rules and checked-in policies. ")


def _lander_continuation(l: Any, name: Any) -> str:
    """The prompt's opening for a lander revived from its own transcript, else ''."""
    if l.get("mode") != "transcript":
        return ""
    return (f"YOU ARE THE AGENT THAT LANDED {name}, continued from your transcript {l.get('transcript')} (subagent "
            f"{l.get('agent')} of session {str(l.get('session'))[:8]}): read its last part first, it is your own context. ")


def lander_prompt(u, wt):
    """The revived lander's task (fixer_prompt's, for tier `lander`)."""
    l = u["lander"]
    num, rid = u["run"].get("number"), u["run"].get("databaseId")
    name = l.get("change")
    head = _lander_continuation(l, name)
    moved = (f"Your old worktree {l['moved_from']} is gone: work in {wt} (off {remote()}/main). " if l.get("moved_from")
             else "")
    why = f"\n\n`agentctl ci-why {rid}` says:\n{l['ci_why']}\n\n" if l.get("ci_why") else " "
    if l.get("reverted") is False:  # its revert did not apply: fix forward, as a gate fix
        return (head + f"agentctl REVIVED YOU: your change {name} ({', '.join(l.get('shas') or [])}) turned main red -- "
                f"{u['workflow']} #{num} (run id {rid}) failed in job `{u.get('job')}` with "
                f"{str(u.get('line') or '')[:300]!r}; its parent passes the same check, its end fails. It COULD NOT BE "
                f"REVERTED ({l.get('revert')}).{why}" + moved + f"FIX IT FORWARD in your worktree {wt}: `git fetch "
                f"{remote()} && git rebase {remote()}/main`, fix the failure (never loosen the test), commit it with a "
                f"`Fixes-Red: {num}` trailer and land it with `~/bin/agentctl land` (a gate fix: it lands while main is "
                "red). End at `LANDED <sha>`, or `GAVE UP: <why>`.")
    return (head + f"agentctl REVIVED YOU: your change {name} ({', '.join(l.get('shas') or [])}: "
            f"{str(l.get('subject') or '')[:80]}) turned main red -- {u['workflow']} #{num} (run id {rid}) failed in job "
            f"`{u.get('job')}` with {str(u.get('line') or '')[:300]!r}; its parent passes the same check, its end fails "
            f"({str(l.get('evidence') or '')[:200]}). IT WAS REVERTED ({l.get('revert')}).{why}" + moved +
            f"FIX IT in your worktree {wt}: `git fetch {remote()} && git rebase {remote()}/main` (your original commits "
            "drop out as already upstream), re-apply your change with `git revert <the revert's sha>` once the revert is "
            f"on {remote()}/main (`git log {remote()}/main --grep 'Reverts-Change: {name}'`), add the fix, reproduce the "
            "failure and see it pass (the failing test, fast; never loosen it), then land again with "
            "`~/bin/agentctl land` (no Fixes-Red: the revert fixed the red). End at `LANDED <sha>`, or `GAVE UP: <why>`.")
