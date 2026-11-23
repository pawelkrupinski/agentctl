# agentctl, part `watchtick`: a watch item followed to its verdict: the tick's evaluation, a `land --follow` follower's claim (WatchClaim) and watch, the verdict's ending (the worktree closed, a red attached)
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
from typing import Any, Sequence
from agentctl.core import NoToken, env_num, err, slug
from agentctl.timefmt import iso_at
from agentctl.procs import safe_cwd
from agentctl.owned import Owned, backoff, restart_if_changed
from agentctl.clones import main_clone
from agentctl.cleanup import close_worktree
from agentctl.redfixes import fixes_of
from agentctl.ciwhy import red_for
from agentctl.landwatch import (
    WATCH, WATCH_CONV_START, WATCH_EVERY, WATCH_FOLLOW_LEASE, WATCH_STALL_MIN, WATCH_TIMEOUT, start_watch,
    watch_clone, watch_eval, watch_event, watch_look, watch_outcome, watch_verdict)
from agentctl.redopen import red_attach
from agentctl.cireport import ci_why
from agentctl.statemachines import StaleVersion, TerminalState
from agentctl.clock import Clock


def watch_landed(repo: str, sha: str, pushed_at: float | None, clock: Clock, store: Any, o_r: str | None = None,
                 close: str | None = None, fixes: Sequence[str] = ()) -> int:
    """Watch Main and the convergence for a landed sha. 0 green (or nothing to watch), 3 red, 2 could not tell. OWNED
    (one watcher per sha; on agentctl's new code it re-execs as `agentctl watch`). fixes: the runs the landed commits'
    `Fixes-Red:` trailers name (the sha's own are read here too): each is verified against the new run (FIX OK / FIX
    FAILED, to the inbox)."""
    o_r = o_r or slug(repo)
    flows = WATCH.get(o_r)
    if not flows:
        print(f"watch: no CI watched for {o_r} (only movies' Main and convergence are)")
        return 0
    pushed_at = pushed_at or clock.time()
    try:
        fixes = sorted(set(fixes) | fixes_of(repo, [sha]))
    except Exception:
        fixes = sorted(set(fixes))
    claim = WatchClaim(store, repo, sha, pushed_at, o_r, clock, close)
    how, it = claim.look()
    if how == "theirs":
        how, it = claim.wait_theirs(it)
    if how == "decided":
        rc = {"ok": 0, "red": 3}[watch_outcome(it)]
        print((it.get("data") or {}).get("verdict") or f"WATCH {watch_outcome(it).upper()}: {sha[:10]} (its watch item's verdict)",
              flush=True)
    elif how == "theirs":  # still another follower's at our bound
        print(f"WATCH UNKNOWN: {sha[:10]} is watched by {(it.get('data') or {}).get('followed_by')}, undecided after "
              f"{int(env_num('AGENTCTL_WATCH_TIMEOUT', WATCH_TIMEOUT) // 60)} min; check `agentctl ci-why` later", flush=True)
        return 2
    else:  # mine, or None (the store did not answer): watched here unclaimed
        own = Owned("watch", f"{o_r}-{sha}", ["watch", "--repo", repo, "--sha", sha, "--pushed-at", str(pushed_at)]
                    + (["--close", close] if close else []) + [x for f in fixes for x in ("--fixes", f)])
        if not own.acquire():
            if how == "mine":  # another watcher here: the claim given back, so the tick is not kept off it for its lease
                claim.release()
            h = own.holder or {}
            print(f"WATCH UNKNOWN: another agentctl (pid {h.get('pid')}, {h.get('owner')}) already watches {sha[:10]}; "
                  "its verdict is its own (`agentctl orphans` lists owners)")
            return 2
        try:
            rc = _watch(repo, sha, pushed_at, o_r, flows, own, clock, fixes, claim if how == "mine" else None)
        finally:
            own.release()
    if rc == 0 and close:
        print(close_worktree(close), flush=True)
    return rc


class WatchClaim:
    def __init__(self, store, repo, sha, pushed_at, o_r, clock, close=None):
        self.store, self.repo, self.sha, self.pushed_at, self.o_r, self.close = store, repo, sha, pushed_at, o_r, close
        self.clock = clock
        self.me = f"{os.uname().nodename}:{os.getpid()}"

    def lease(self, t):
        return {"followed_by": self.me, "followed_until": t + env_num("AGENTCTL_WATCH_FOLLOW_LEASE", WATCH_FOLLOW_LEASE)}

    def look(self):
        """('mine' | 'theirs' | 'decided', item), or (None, None) when the store did not answer."""
        t = self.clock.time()
        try:
            st = self.store
            it = start_watch(st, self.repo, self.sha, self.pushed_at, worktree=self.repo, close=self.close, o_r=self.o_r,
                             session=os.environ.get("CLAUDE_CODE_SESSION_ID"), follower=self.lease(t))
            for _ in range(3):
                dd = it.get("data") or {}
                if watch_outcome(it) in ("ok", "red"):
                    return "decided", it
                if dd.get("followed_by") == self.me:
                    return "mine", it
                if dd.get("followed_by") and float(dd.get("followed_until") or 0) > t:
                    return "theirs", it
                try:  # unclaimed (the batch's, the tick's) or lapsed: ours now
                    st.update_item("watch", self.sha, it["version"], self.lease(t))
                    return "mine", st.item("watch", self.sha)
                except StaleVersion:
                    it = st.item("watch", self.sha)
            return "theirs", it
        except (Exception, SystemExit) as e:
            err(f"watch {self.sha[:10]}: the store did not answer ({str(e)[:120]}): watched here, unclaimed")
            return None, None

    def wait_theirs(self, it):
        """Another live follower watches it: its verdict is read from the item, polled until decided, our own bound,
        or its claim lapsing (then ours)."""
        err(f"watch: {self.sha[:10]} is already watched by {it['data'].get('followed_by')}: its verdict is read from "
            "the store, not watched twice")
        t0, timeout = self.clock.time(), env_num("AGENTCTL_WATCH_TIMEOUT", WATCH_TIMEOUT)
        delays = backoff(env_num("AGENTCTL_WATCH_POLL", 60), 300)
        how = "theirs"
        while how == "theirs" and self.clock.time() - t0 < timeout:
            self.clock.sleep(next(delays))
            how, got = self.look()
            it = got or it
            if how is None:  # a store blip while reading: read again
                how = "theirs"
        return how, it

    def renew(self):
        try:
            st = self.store
            it = st.item("watch", self.sha)
            # only its OWN claim: one that lapsed (a laptop asleep) and was taken over is the new follower's now --
            # renewing it would take it back from under that one, and the tick would leave both watching
            if self.mine(it):
                st.update_item("watch", self.sha, it["version"], self.lease(self.clock.time()))
        except (Exception, SystemExit) as e:  # renewed at the next look; a lapse lets the tick evaluate it meanwhile
            err(f"watch {self.sha[:10]}: could not renew its claim ({str(e)[:120]})")

    def mine(self, it):
        return bool(it) and watch_outcome(it) not in ("ok", "red") and (it.get("data") or {}).get("followed_by") == self.me

    def release(self):
        """Its own claim given up (never another follower's): the tick carries a `watching` item on to its own deadline;
        an `unknown` one waits for the next follower."""
        try:
            st = self.store
            it = st.item("watch", self.sha)
            if self.mine(it):
                st.update_item("watch", self.sha, it["version"], {"followed_by": None, "followed_until": 0})
        except (Exception, SystemExit) as e:  # it lapses by itself
            err(f"watch {self.sha[:10]}: could not give its claim up ({str(e)[:120]})")

    def finish(self, state, line, red_runs=()):
        """ok / red: the verdict on the item (finish_watch, told: this follower printed it). Anything else (unknown,
        stalled, no token here) gives the claim up (release)."""
        if state not in ("ok", "red"):
            return self.release()
        try:
            st = self.store
            it = st.item("watch", self.sha)
            if it and watch_outcome(it) not in ("ok", "red"):
                finish_watch(st, it, state, line, list(red_runs), told=True)
        except (Exception, SystemExit) as e:
            err(f"watch {self.sha[:10]}: could not write its verdict to the store ({str(e)[:120]})")


def _watch(repo, sha, pushed_at, o_r, flows, own, clock, fixes=(), claim=None):
    timeout = env_num("AGENTCTL_WATCH_TIMEOUT", WATCH_TIMEOUT)
    conv_start = env_num("AGENTCTL_WATCH_CONV_START", WATCH_CONV_START)
    t0 = clock.time()
    err(f"watching Main and the convergence for {sha[:10]} (up to {int(timeout // 60)} min; `--no-watch` skips this)"
        + (f"; its Fixes-Red {', '.join(fixes)} is verified by the red's own item (`agentctl red status`)" if fixes else ""))
    done, said = {}, set()  # kind -> (run, ok | None for none started)
    # NO PROGRESS: nothing about the followed runs (which run, its status, its last update) changed for this long ends
    # the watch with a named verdict (a queue no runner serves, a runner lost mid-run), not a silent 3 h wait
    stall = env_num("AGENTCTL_WATCH_STALL_MIN", WATCH_STALL_MIN) * 60
    state, changed = None, clock.time()
    delays = backoff(env_num("AGENTCTL_WATCH_POLL", 60), 300)
    # UNREACHABLE: a failed read backs off (to 5 min) and is retried until the watch's own bound; only then UNKNOWN,
    # saying for how long GitHub could not be read (2026-10-27: ten failures in a row used to end it in minutes)
    unreachable, failures, last_error, err_delays = None, 0, "", None

    def verdict(rc, line, red_runs=()):
        print(line, flush=True)
        if claim:
            claim.finish({0: "ok", 3: "red"}.get(rc, "unknown"), line, red_runs)
        return rc

    def on_run(kind, run):
        name = flows[kind][0]
        if run is None:
            print(f"{kind}: none started within {int(conv_start // 60)} min of the push", flush=True)
            return
        if (kind, run["databaseId"]) not in said:
            said.add((kind, run["databaseId"]))
            err(f"{name}: following run #{run.get('number')} ({run.get('status')}, head {str(run.get('headSha'))[:10]})")
        if kind in done:
            print(f"{name} #{run.get('number')}: {run.get('conclusion')}", flush=True)

    while True:
        seen = {}
        try:
            seen = watch_look(repo, sha, o_r, flows, done, pushed_at, t0, clock.time(), on_run)
            if unreachable is not None:
                err(f"watch: GitHub answers again after {red_for(iso_at(unreachable))} ({failures} failed reads)")
            unreachable, failures, err_delays = None, 0, None
        except NoToken as e:  # no vault for this process: retrying cannot help
            return verdict(2, f"WATCH UNKNOWN: no token ({str(e).replace('agentctl: ', '')}); "
                              f"check `agentctl ci-why` for {sha[:10]}")
        except Exception as e:  # a network blip, GitHub down: read again, backing off, until the bound
            failures += 1
            last_error = str(e)[:160]
            if unreachable is None:
                unreachable, err_delays = clock.time(), backoff(env_num("AGENTCTL_WATCH_ERROR_POLL", 30), 300)
            err(f"watch: could not read the runs ({last_error}) [{failures} failed, unreachable for "
                f"{red_for(iso_at(unreachable))}]")
        v = watch_verdict(o_r, sha, flows, done)
        if v and v[0] == "red":
            verdict(3, v[1], v[2])
            try:
                print(ci_why(o_r, 4, repo=repo)[1])
            except (Exception, SystemExit) as e:
                print(f"(agentctl ci-why failed: {e})")
            return 3
        if v:
            return verdict(0, v[1])
        if clock.time() - t0 >= timeout:
            left = ", ".join(flows[k][0] for k in flows if k not in done)
            if unreachable is not None:
                return verdict(2, f"WATCH UNKNOWN: GitHub's runs could not be read for {red_for(iso_at(unreachable))} "
                                  f"({failures} failed reads, the last: {last_error}); {left} undecided after "
                                  f"{int(timeout // 60)} min; check `agentctl ci-why` later")
            return verdict(2, f"WATCH UNKNOWN: {left} undecided after {int(timeout // 60)} min; check `agentctl ci-why` "
                              "later")
        if unreachable is None:  # a failed read says nothing about the runs: no stall judged on it
            now_state = (tuple(sorted(done)), tuple(sorted(seen.items())))
            if now_state != state:
                state, changed = now_state, clock.time()
            elif clock.time() - changed >= stall:
                left = ", ".join(f"{flows[k][0]} {('run ' + str(v[0]) + ' ' + str(v[1])) if v else 'no run'}"
                                 for k, v in [(k, seen.get(k)) for k in flows if k not in done])
                return verdict(2, f"WATCH STALLED: no change in {left} for {int(stall // 60)} min; check `agentctl "
                                  "ci-why` later")
        if claim:
            claim.renew()
        restart_if_changed([own])
        clock.sleep(next(err_delays) if unreachable is not None else next(delays))


def cmd_watch(a, clock, store):
    a.inbox_cwd = a.agent_cwd or a.close or a.repo or safe_cwd()  # its inbox entry routes to the agent's worktree
    repo = main_clone(a.repo or safe_cwd())
    os.chdir(repo)  # not the worktree: --close removes it, and so may its agent
    return watch_landed(repo, a.sha, a.pushed_at, clock, store, close=a.close, fixes=a.fixes or ())


def tick_watches(clock, st):
    """One evaluation of each due watch item. Returns what it decided."""
    t = clock.time()
    did = []
    for it in st.items("watch", states=("watching",)):
        dd = it["data"]
        if float(dd.get("followed_until") or 0) > t:  # a live `land --follow` watches it and writes its verdict here
            continue
        try:
            did += tick_watch(st, it, t)
        except (StaleVersion, TerminalState):  # its follower claimed or decided it meanwhile: it is that one's
            pass
    return did


def tick_watch(st, it, t):
    """One evaluation of a due watch item: what it decided (StaleVersion / TerminalState when a follower took it)."""
    dd = it["data"]
    if t > it["deadline"]:
        st.transition("watch", it["id"], "unknown", it["version"],
                      event=watch_event(it, f"WATCH UNKNOWN: Main or the convergence still undecided for {it['id'][:10]} "
                                            f"after {int(WATCH_TIMEOUT // 3600)} h: a stuck run (`agentctl ci-why`)",
                                        "escalate", 2))
        return [f"watch {it['id'][:10]}: unknown"]
    if float(dd.get("next_at") or 0) > t:
        return []
    flows = WATCH.get(dd["o_r"]) or {}
    try:
        verdict = watch_eval(watch_clone(dd), it["id"], dd["pushed_at"], dd["o_r"], flows, t)
    except (Exception, SystemExit) as e:  # unreadable now: tried again at the next look, the deadline bounds it
        err(f"watch {it['id'][:10]}: could not read the runs ({str(e)[:120]})")
        verdict = None
    if verdict is None:
        st.update_item("watch", it["id"], it["version"], {"next_at": t + env_num("AGENTCTL_WATCH_POLL", WATCH_EVERY)})
        return []
    finish_watch(st, it, *verdict)
    return [f"watch {it['id'][:10]}: {verdict[0]}"]


def finish_watch(st, it, state, line, red_runs, told=False):
    """The watch item's verdict (ok / red / unknown), kept on it for a later follower (`verdict`); its event goes to
    its author's inbox unless told (a follower printed it to the author already); on ok its worktree closed, on red
    the red machine takes each red run, its author offered first. An `unknown` item a follower re-watched keeps the
    re-watch's verdict as its `outcome`, the claim given up with it."""
    dd = it["data"]
    if it["state"] == "unknown":
        st.update_item("watch", it["id"], it["version"], {"verdict": line, "outcome": state, "followed_by": None,
                                                          "followed_until": 0})
    else:
        ev = watch_event(it, line, code={"ok": 0, "red": 3}.get(state, 2))
        if told:
            ev["audience"] = {}
        st.transition("watch", it["id"], state, it["version"], data={"verdict": line}, event=ev)
    if state == "ok" and not told and dd.get("close") and os.path.isdir(dd["close"]):
        err(close_worktree(dd["close"]))
    for wf, n in red_runs:
        try:
            red_attach(st, wf, n, author={"session": dd.get("session"), "worktree": dd.get("worktree")})
        except (Exception, SystemExit) as e:
            err(f"watch {it['id'][:10]}: could not attach {wf} #{n} ({str(e)[:120]})")
