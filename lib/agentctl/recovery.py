# agentctl, part `recovery`: the recovery registry: the conditions' probes and their recoveries
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from typing import Any
from agentctl.core import err, git, recovery_dir, remote, SHORT, spawn_detached, tilde, _tokens
from agentctl.timefmt import iso_at, ts
from agentctl.statefile import read_json_file, write_json_file, unlink_file
from agentctl.selfcode import TOOL_PATH
from agentctl.trailers import held_commit
from agentctl.storetoken import red_read_token, token_env_name, no_vault, take_token_notes, token_from_processor, token_vault_key
from agentctl.worktrees import mark_internal
from agentctl.conditions import open_condition, recovery_tick, run_probe
from agentctl.conditionregistry import ConditionRegistry
from agentctl.tunnel import default_route, tunnel_ports, tunnel_probe, tunnel_state_path
from agentctl.blamerecords import blame_land_refusal
from agentctl.batchitem import batch_move, current_batch
from agentctl.forge import gh_json
from agentctl.clock import epoch_now
from agentctl.conditions import open_conditions


def probe_route(_a):
    r = default_route()
    return (True, f"default route {r[0]} via {r[1]}") if r else (False, "no default route (offline)")


def probe_tunnel(_a):
    r = tunnel_probe()
    return r == "ok", f"the store tunnel (localhost:{tunnel_ports()[0][0]}) {r}"


def probe_token(a):
    """A token without `secrets`: the environment, this processor's own, or the processor's socket."""
    ns = a.get("ns") or "movies"
    if os.environ.get("GH_TOKEN") or (ns == "movies" and os.environ.get(token_env_name(SHORT["movies"]))) or _tokens.get(ns):
        return True, f"{ns} token present"
    tok = token_from_processor(SHORT["movies"]) if ns == "movies" else None
    return bool(tok), f"{ns} token " + ("held by the processor" if tok else "unavailable")


def recover_tunnel_reset(cond, a, store):
    st = read_json_file(tunnel_state_path())
    if st.get("network") or st.get("fails") or st.get("pending_since"):
        st.update(fails=0, network=False, pending_since=None, escalated=False)
        write_json_file(tunnel_state_path(), st)
    return ""


def recover_clear_flag(cond, a, store):
    unlink_file(a["path"])
    return ""


# A RE-LAND NEVER TOUCHES THE AUTHOR'S WORKTREE (2026-11-06 22:51Z: orphan duty ran `agentctl land` IN
# ~/projects/movies-wt/es-gate-check, which rebased a live session's check worktree onto origin/main). The re-land runs
# in a throwaway tree of agentctl's own (reland_tree_path: tagged internal, named after the author's worktree so the
# change id still names it), detached at the author's HEAD: land rebases THAT tree; the author's branch, HEAD and files
# stay exactly as they were. A later re-land replaces the tree; LANDED closes it like any land's tree.
def reland_tree_path(wt):
    return recovery_dir("reland-trees", os.path.basename(os.path.realpath(wt).rstrip("/")))


def reland_tree(wt):
    """A fresh detached tree at wt's HEAD (reland_tree_path), or raise RuntimeError saying why not."""
    head = git(wt, "rev-parse", "HEAD", check=False).stdout.strip()
    if not head:
        raise RuntimeError(f"{wt} has no HEAD")
    tree = reland_tree_path(wt)
    if os.path.exists(tree):
        git(wt, "worktree", "remove", "--force", tree, check=False)
        if os.path.exists(tree):
            import shutil
            shutil.rmtree(tree, ignore_errors=True)
    git(wt, "worktree", "prune", check=False)
    os.makedirs(os.path.dirname(tree), exist_ok=True)
    r = git(wt, "worktree", "add", "-q", "--detach", mark_internal(tree), head, check=False)
    if r.returncode:
        raise RuntimeError(f"could not make a throwaway tree at {tree}: {(r.stderr or '').strip()[:200]}")
    return tree


def reland_worktree(cond, a, store):
    """`agentctl land` again for the change in a["worktree"], from a throwaway tree at its HEAD (it fetches and rebases
    there: a conflict is a real NOT LANDED). 'landing ...' when started, else why not (the caller says it: never a
    silent drop)."""
    wt = a["worktree"]
    if not os.path.isdir(wt):
        return f"not landed again: {wt} is gone"
    if git(wt, "status", "--porcelain", "--untracked-files=no", check=False).stdout.strip():
        return f"not landed again: {wt} has uncommitted changes"
    commits = git(wt, "rev-list", f"{remote()}/main..HEAD", check=False).stdout.split()
    why = blame_land_refusal(wt, commits, store, wt) or held_commit(wt, commits)
    if why:
        return f"not landed again: {why}"
    try:
        tree = reland_tree(wt)
    except RuntimeError as e:
        return f"not landed again: {e}"
    os.makedirs(recovery_dir(), exist_ok=True)
    with open(recovery_dir("reland.log"), "a") as f:
        f.write(f"=== {ts()} re-land of {wt} from {tree}\n")
        f.flush()
        spawn_detached([sys.executable, TOOL_PATH, "land"], stdin=subprocess.DEVNULL, stdout=f,
                       stderr=f, start_new_session=True, cwd=tree,
                       env={**os.environ, "AGENTCTL_NO_SECRETS": "1"})
    return f"landing {tilde(wt)} again from a throwaway tree {tilde(tree)} (attempt {a.get('attempt')})"


def recover_clear_hold(cond: dict[str, Any] | None, a: dict[str, Any], st: Any) -> str:
    it = current_batch(st, a["d"])
    if it and it["state"] == "held":
        batch_move(st, it, "assembling", text=f"batch {it['id']}: its hold lifted")
    return "the batch hold is lifted"


def token_unavailable_note(ns: str, why: str, store: Any) -> None:
    """No source had the token: ONE condition per spell (UNKNOWN in the inbox), cleared by the token probe -- the
    processor holds one again -- with its entry acked. The remote store's own call needs this very token: its read
    fails again, which only leaves a note for a later take (storetoken.take_token_notes), so this never recurses."""
    if ns != "movies":
        return
    try:
        with no_vault():  # the note's own store call never prompts for the token it notes missing
            open_condition("UNKNOWN", f"UNKNOWN: token unavailable ({why}): run `secrets unlock {token_vault_key('pawelkrupinski/movies')[0]}` in any session (the store token's vault); the "
                                      "next land or `land status` re-seeds the processor",
                           run_probe(probe_token, {"ns": ns}), [], key=f"token-{ns}", command="token", exit_code=2,
                           store=store)
    except (Exception, SystemExit) as e:
        err(f"token: could not note the missing token ({str(e)[:120]})")


def note_missing_tokens(store: Any) -> None:
    """The missing tokens' notes this process left since the last take (every tick's recovery step, and the end of
    every command: cli), each a condition in store."""
    for ns, why in take_token_notes():
        token_unavailable_note(ns, why, store)


_last_recovery = [0.0]


def maybe_recovery_tick(store: Any, registry: ConditionRegistry, now: float) -> None:
    """A tick's recovery step at now (the tick's clock): the missing tokens' notes, then the condition machine's
    recheck at most every 5 s."""
    note_missing_tokens(store)
    if now - _last_recovery[0] < 5:
        return
    _last_recovery[0] = now
    recovery_tick(store, registry)


def red_no_read_access(repos, store):
    """ONE condition while ci-runner cannot read the other watched repos: cleared by the red_read probe (the reader App
    configured on the host, and their runs read)."""
    return open_condition("UNKNOWN", f"NO READ ACCESS: {', '.join(repos)}: the reconciler cannot read their runs -- "
                                     "the lander App is push-only on movies (needs the agentctl-reader App); their "
                                     "reds are not watched until then",
                          run_probe(probe_red_read, {"repos": list(repos)}), [], key="red-read-access", command="red",
                          exit_code=2, store=store)


def probe_red_read(a):
    """Every repo in a["repos"] has a reader token and its runs read."""
    for o_r in a.get("repos") or []:
        try:
            red_read_token(o_r)
            # listing: a read probe only (any answer proves the token reads)
            gh_json(o_r, "run", "list", "--limit", "1", "--json", "databaseId")
        except (Exception, SystemExit) as e:
            return False, f"{o_r}: {str(e).replace('agentctl: ', '')[:160]}"
    return True, "every watched repo's runs read"


def _probe_report(c: Any) -> str:
    """The condition's probe (its `since` left out) and what clears it."""
    args = json.dumps({k: v for k, v in c["probe"]["args"].items() if k != "since"})
    return f"  probe: {c['probe']['kind']} {args} -- clears when {c.get('until')}\n"


def _checked_report(c: Any, t: Any) -> str:
    """When it was last checked, what the check said, when it is next due."""
    lr = c.get("last_result") or {}
    return (f"  last checked {iso_at(float(c.get('last_checked') or 0))}: {'cannot tell' if lr.get('ok') is None else 'fails'}"
            f" ({lr.get('detail')}); next check in {max(0, int(float(c.get('next_check') or 0) - t))} s"
            f" (fails {c.get('fails') or 0})\n")


def _condition_report(c: Any, t: Any) -> str:
    """One open condition as `recover` prints it."""
    return (f"{c['kind']} {c['key']} (open since {c.get('opened')}{', ESCALATED' if c.get('level') else ''}): "
            f"{c.get('text', '')[:140]}\n" + _probe_report(c) + _checked_report(c, t)
            + f"  on recover: {', '.join(n for n, _x in c.get('on_recover') or []) or 'ack + gate dirty'}")


def cmd_recover(a, store, registry: ConditionRegistry):
    if a.rcmd == "tick":
        lines = recovery_tick(store, registry, force=True)
        print("\n".join(lines) if lines else "recover: nothing cleared")
        return 0
    cs = open_conditions(store)
    if not cs:
        print("recover: no open conditions")
        return 0
    t = epoch_now()
    for c in cs:
        print(_condition_report(c, t))
    return 0
