# agentctl, part `redtickothers`: the red tick of the repos besides the primary: each ticked in its own try, the unreadable noted, the skipped said
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any
from agentctl.conditionregistry import ConditionRegistry
from agentctl.redknobs import knob_red_others_every, knob_red_skip_say
from agentctl.core import print_error
from agentctl.tokenerrors import NoReadAccess
from agentctl.repos import RED_PRIMARY, red_repo_clone, red_repo_short, red_repos
from agentctl.context import Context
from agentctl.storetoken import red_read_token
from agentctl.recovery import red_no_read_access
from agentctl.redgate import red_gate_snap
from agentctl.redtick import _red_lock, red_tick


def tick_other_repo(store: Any, o_r: str, now_: Any, dispatch: bool, registry: ConditionRegistry, *, ctx: Context) -> list[str]:
    """One other watched repo's red tick: its lines, prefixed with the repo; none (its skip said) when it fails."""
    try:
        snap = red_gate_snap(o_r, store, ctx=ctx)
        clone = red_repo_clone(o_r)
        with _red_lock:
            acts = [f"{red_repo_short(o_r)}: {x}" for x in red_tick(store, o_r, clone, snap, now_=now_, dispatch=dispatch,
                                                                     registry=registry, ctx=ctx)]
        ctx.caches.others_said.pop(o_r, None)
        return acts
    except (Exception, SystemExit) as e:
        red_others_skipped(ctx.caches.others_said, o_r, str(e).replace('agentctl: ', '')[:200], ctx.clock.time())
        return []


def note_no_read_access(unread: list[str], store: Any) -> None:
    try:
        red_no_read_access(unread, store)
    except (Exception, SystemExit) as e:
        print_error(f"red tick: could not note the missing read access ({str(e)[:120]})")


def red_tick_others(store, registry, now_=None, dispatch=False, force=True, *, ctx):
    """The red tick of every watched repo but the primary (red-repos.json): its gate read, its reds observed and
    stepped, each repo inside its own try (one unreadable repo never stops the others or the primary). Its clone is
    this machine's when there is one (Fixes-Red trailers, authors); without one (ci-runner) its reds open and clear
    by its gate runs alone."""
    acts = []
    if not force and ctx.clock.time() - ctx.caches.ran_at.get("red others", 0.0) < knob_red_others_every():
        return acts
    ctx.caches.ran_at["red others"] = ctx.clock.time()
    unread = []
    for c in red_repos():
        o_r = c["repo"]
        if o_r == RED_PRIMARY:
            continue
        try:
            red_read_token(o_r)
        except NoReadAccess:
            unread.append(o_r)  # one condition for all of them, below: no line per tick
            continue
        acts += tick_other_repo(store, o_r, now_, dispatch, registry, ctx=ctx)
    if unread:
        note_no_read_access(unread, store)
    for line in acts:
        print_error(f"red: {line}")
    return acts


def red_others_skipped(said: dict[str, tuple[str, float]], o_r: str, why: str, now_: float) -> str | None:
    """Another repo's tick failed: said on a new reason, else at most every RED_SKIP_SAY s (it was every tick)."""
    last = said.get(o_r)
    if last and last[0] == why and now_ - last[1] < knob_red_skip_say():
        return None
    said[o_r] = (why, now_)
    line = f"red tick: {o_r}: skipped ({why})" + (" (still)" if last and last[0] == why else "")
    print_error(line)
    return line
