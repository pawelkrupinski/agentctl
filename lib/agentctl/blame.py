# agentctl, part `blame`: the culprit search: the boundaries of a red's range, probed one by one until the change that turned it red is found
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.statefile import note_failure, read_record
from agentctl.selfcode import TOOL_PATH
from agentctl.clock import epoch_now
from agentctl.blameknobs import knob_blame_max, knob_blame_probe_wall, knob_blame_probes
from agentctl.core import git, reds_dir, spawn_detached
from agentctl.procs import process_start, record_alive
from agentctl.inboxentry import inbox_write
from agentctl.redrepos import red_repo_tag
from agentctl.lander import blame_change_name, blame_decision
from agentctl.blameopen import CONVERGENCE_FLOWS, PATHISH, TEST_TOKEN
from agentctl.redrecord import red_what
from agentctl.fixerland import red_dispatch_log
from agentctl.redopen import red_offer
from agentctl.agewords import fmt_age
import os
import re
import signal
import subprocess
import sys
import time
from typing import Any


def blame_boundary(b, i):
    return b["base"] if i == 0 else b["groups"][i - 1]["end"]


def blame_next(b):
    """The boundary to judge next, or None when lo and hi are adjacent (decided)."""
    lo, hi = b.get("lo"), b["hi"]
    if lo is None:
        return 0
    if hi - lo <= 1:
        return None
    k = b.get("hint")
    if k is not None and lo < k < hi:  # the hinted change's parent
        return k
    if k is not None and k == lo and k + 1 < hi:  # ... and its end
        return k + 1
    return (lo + hi) // 2


def blame_waiting(b):
    """Why a `blame` offer is not taken yet: what the search is judging."""
    b = b or {}
    p = b.get("probe") or {}
    judged = len(b.get("probes") or [])
    what = (f"probing {str(p.get('commit'))[:10]} ({p.get('kind')}) on CI" if p else "reading its range")
    return (f"deciding the culprit among {len(b.get('groups') or [])} change(s): {what}; {judged} boundary(ies) judged "
            f"(at most {fmt_age(knob_blame_max())}, then a fixer)")


def blame_free(o_r, d, b, i, runs):
    """A boundary's verdict that costs nothing: a decided run of the red's own (unseeded) workflow on exactly that commit
    -- pass on success; None when no such run exists, and ALWAYS None for a convergence red: its drift seeds rotate per
    run (a green run on other seeds is never evidence, #628 for #629) and its other legs' pinned pairs rotate nightly.
    Only a re-run with the red's own seeds forced (probe_drift) judges a convergence boundary."""
    if b.get("kind") not in ("run", "checks") or d.get("workflow") in CONVERGENCE_FLOWS:
        return None
    sha = blame_boundary(b, i)
    for r in runs or []:
        if r.get("headSha") == sha and r.get("status") == "completed" and r.get("conclusion") == "success":
            return {"verdict": "pass", "evidence": f"{d.get('workflow')} #{r.get('number')} green on {sha[:10]}"}
    return None


def blame_apply(b, i, v):
    """A boundary's verdict into the search: (b, None) to go on, or (b, why) when the search ends undetermined."""
    b = dict(b, probes=(b.get("probes") or []) + [{"boundary": i, "commit": blame_boundary(b, i), **v}], probe=None)
    if v["verdict"] == "none":
        return b, f"no verdict on {blame_boundary(b, i)[:10]} ({v.get('evidence') or '?'})"
    if i == 0:
        if v["verdict"] == "fail":
            return b, (f"pre-existing: the parent {b['base'][:10]} fails the same check "
                       f"({v.get('evidence') or ''}); no change of its range is the culprit")
        b["lo"] = 0
    elif v["verdict"] == "pass":
        b["lo"] = i
    else:
        b["hi"] = i
    return b, None


def _blame_confirmed(store: Any, it: Any, b: Any, k: Any, t: Any) -> Any:
    """The culprit is group k: recorded, announced, and the red offered again with the tier its decision gives."""
    d, g = it["data"], b["groups"][k]
    b = dict(b, state="confirmed", culprit=k, parent=blame_boundary(b, k), decided_at=t,
             evidence="; ".join(str(p.get("evidence") or "") for p in b.get("probes") or [] if p.get("evidence")))
    who, text = blame_decision(b)
    line = (f"RED BLAME: {red_what(d)}: the culprit is {blame_change_name(g)} "
            f"({', '.join(c[:10] for c in g['commits'][:5])}, {g.get('subject') or ''!s:.80}), {text}: its parent "
            f"{b['parent'][:10]} passes the same check, its end fails ({b['evidence'][:200]})")
    inbox_write("red", line, [line], [], 0)
    red_dispatch_log(d, line)
    red_offer(store, it, line, data={"blame": b}, quiet=True)
    return [line]


def blame_finish(store, it, b, why, t):
    """The search decided: confirmed (lo and hi adjacent), or undetermined with why. The red is offered again with the
    tier the decision gives (red_offer). Returns the act lines."""
    d = it["data"]
    if why is None:
        why = blame_unproven(d, b)
    if why is None:
        k = b["hi"] - 1
        g = b["groups"][k]
        if g.get("bot"):
            why = f"the culprit {g['commits'][0][:10]} is a bot's change ({g.get('author')}): never reverted"
        else:
            return _blame_confirmed(store, it, b, k, t)
    b = dict(b, state="undetermined", why=why, decided_at=t)
    line = f"RED BLAME: {red_what(d)}: no culprit determined ({why}): the normal fixer flow"
    red_dispatch_log(d, line)
    red_offer(store, it, line, data={"blame": b}, quiet=True)
    return [line]


def _passing_probes(b: Any) -> Any:
    """The search's probes that passed at its boundary lo."""
    return [p for p in b.get("probes") or [] if p.get("boundary") == b.get("lo") and p.get("verdict") == "pass"]


def _seeded_ci_proof(probes: Any) -> Any:
    """Whether a CI drift probe among probes replayed the red's own seeds."""
    return any(p.get("ci") and p.get("kind") == "drift" and p.get("seeds") is True for p in probes)


def _parent_unproven(b: Any) -> Any:
    """Why the search's passing parent is no proof for a drift red -- no CI probe re-ran it with the red's own seeds --
    or None."""
    lo = b.get("lo")
    proof = _passing_probes(b)
    if not _seeded_ci_proof(proof):
        return (f"the parent {str(blame_boundary(b, lo or 0))[:10]} was never re-run with the red's own seeds "
                f"({'; '.join(str(p.get('evidence') or '') for p in proof)[:160] or 'no pass'}): no evidence for a culprit")
    return None


def blame_unproven(d, b):
    """Why the search's passing parent (boundary lo) is no proof for this red, or None. A convergence red's parent
    passes ONLY on a drift probe that replayed the red's own seeds, each row's verdict line naming the seed it pinned
    (probe_drift: `seeds: True`); anything else -- a green run on rotating seeds, an unknown kind -- leaves the culprit
    undetermined: the normal fixer, never a revert."""
    if d.get("workflow") not in CONVERGENCE_FLOWS:
        return None
    if b.get("kind") != "drift":
        return "no CI check repeats this red with its own seeds (not a takes-drift row): the culprit is undetermined"
    return _parent_unproven(b)


def blame_step(store, it, repo, o_r, t):
    """One step of a probing red's culprit search (the red tick, before its offer step). Returns act lines, or None when
    the red is not probing."""
    b = it["data"].get("blame") or {}
    if b.get("state") != "probing":
        return None
    if t - float(b.get("since") or t) > knob_blame_max():
        blame_probe_stop(b.get("probe"))
        return blame_finish(store, it, b, f"no decision in {fmt_age(t - float(b['since']))}", t)
    p = b.get("probe")
    if p:
        v = blame_probe_result(p, t)
        if v is None:
            return []
        b, why = blame_apply(b, p["boundary"], v)
        if why:
            return blame_finish(store, it, b, why, t)
    return _blame_search(store, it, repo, o_r, t, b)


def _blame_search(store: Any, it: Any, repo: Any, o_r: Any, t: Any, b: Any) -> Any:
    """The search from boundary to boundary: each one a stored run decides (blame_free), else a CI probe is started (the
    step's lines) or the search ends (blame_finish)."""
    d = it["data"]
    while True:
        i = blame_next(b)
        if i is None:
            return blame_finish(store, it, b, None, t)
        v = blame_free(o_r, d, b, i, b.get("runs"))
        if v:
            b, why = blame_apply(b, i, v)
            if why:
                return blame_finish(store, it, b, why, t)
            continue
        why = _blame_unprobeable(d, b, i)
        if why:
            return blame_finish(store, it, b, why, t)
        return _blame_probe_line(store, it, repo, o_r, b, i)


def _blame_unprobeable(d: Any, b: Any, i: Any) -> Any:
    """Why boundary i cannot be probed on CI (the probes are spent, or no check repeats this red), or None."""
    started = len([x for x in b.get("probes") or [] if x.get("ci")])
    if started >= knob_blame_probes():
        return f"inconclusive within {started} CI probes"
    if b.get("kind") in ("drift", "checks"):
        return None
    if b.get("kind") is None:
        return "no CI check repeats this red on another commit (a convergence leg's pinned pair rotates nightly)"
    return f"no decided {d.get('workflow')} run on {blame_boundary(b, i)[:10]}"


def _blame_probe_line(store: Any, it: Any, repo: Any, o_r: Any, b: Any, i: Any) -> Any:
    """Starts the CI probe of boundary i, records it on the red, and returns the step's line."""
    d = it["data"]
    b = dict(b, probe=blame_probe_start(repo, o_r, d, b, i))  # counted among the CI probes when its verdict lands (ci: True)
    store.update_item("red", it["id"], it["version"], {"blame": b})
    line = (f"RED BLAME: {red_what(d)}: probing {blame_boundary(b, i)[:10]} (boundary {i} of {len(b['groups'])}, "
            f"{b['kind']}) on CI")
    red_dispatch_log(d, line)
    return [line]


# -- the probes: a detached `agentctl red probe` per boundary, its verdict in a JSON file
def blame_probe_start(repo, o_r, d, b, i):
    num = (b.get("run") or {}).get("number")
    out = reds_dir(red_repo_tag(o_r, num), f"blame-{i}-{os.urandom(2).hex()}.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    argv = [sys.executable, TOOL_PATH, "red", "probe", "--kind", b["kind"], "--repo", repo, "--commit",
            blame_boundary(b, i), "--run", str(num), "--out", out, "--tokens", " ".join(blame_tokens(d))]
    with open(out[:-5] + ".log", "a") as logf:
        p = spawn_detached(argv, cwd=repo, stdin=subprocess.DEVNULL, stdout=logf, stderr=logf, start_new_session=True)
    return {"boundary": i, "commit": blame_boundary(b, i), "out": out, "pid": p.pid, "process_start": process_start(p.pid),
            "at": epoch_now(), "kind": b["kind"]}


def blame_tokens(d):
    line = " ".join([str(d.get("line") or "")])
    return sorted(set(TEST_TOKEN.findall(line)) | {os.path.basename(p) for p in PATHISH.findall(line)})


def blame_probe_result(p, t):
    """The probe's verdict ({verdict: pass|fail|none, evidence, ci: True}), or None while it runs."""
    rec = read_record(p.get("out"))
    if rec and rec.get("verdict"):
        return {**rec, "ci": True}
    if p.get("pid") and record_alive(p):
        if t - float(p.get("at") or t) > knob_blame_probe_wall() + 300:
            blame_probe_stop(p)
            return {"verdict": "none", "evidence": "the probe ran past its wall time", "ci": True}
        return None
    return {"verdict": "none", "evidence": "the probe ended with no verdict (see its .log)", "ci": True}


def blame_probe_stop(p):
    if p and p.get("pid") and int(p["pid"]) != os.getpid() and record_alive(p):
        try:
            os.killpg(int(p["pid"]), signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError as e:
            note_failure("blame probe not signalled", e)


def blame_refuted(repo: Any, b: Any, sha: Any) -> Any:
    """The landed fix `sha` that a run refuted is this red's revert (its `Reverts-Change:` trailer): the culprit was
    wrong. The blame, marked refuted with the reland due -- the laptop's dispatcher relands it (blame_reland)."""
    if (b or {}).get("state") != "confirmed" or not (b.get("revert") or {}).get("ok") or not sha:
        return None
    body = git(repo, "log", "-1", "--format=%B", sha, check=False).stdout if repo else ""
    if "Reverts-Change:" not in body:
        return None
    return dict(b, state="refuted", why=f"its revert {sha[:10]} did not clear the red: the culprit was wrong",
                revert=dict(b["revert"], landed=sha), reland_due=sha)
