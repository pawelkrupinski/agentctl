# agentctl, part `notifyurl`: where an owner notification's click goes (the owner, 2026-11-03: "can agentctl notifications
# open a tab in chrome when clicked and highlight the relevant section?").
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.
# A red -> the failing JOB's page anchored at the failing step (GitHub highlights it); else the run; an ask with no run
# -> a local HTML page of the question, its section highlighted by :target; anything else -> None (a plain notification).

from typing import Any
import os
import re
from agentctl.context import Context
from agentctl.core import attempt, state_root
from agentctl.repos import red_repo_of



import html as _html


def run_page_url(o_r, run_id):
    return f"https://github.com/{o_r}/actions/runs/{run_id}"


def job_step_url(o_r, run_id, job_id, step=None, line=1):
    u = f"{run_page_url(o_r, run_id)}/job/{job_id}"
    return u + (f"#step:{step}:{line}" if step else "")


def failing_job_step(jobs, names=None):
    """(job_id, step number) of the first failed job (one of `names` first, when given) and its first failed step."""
    bad = [j for j in jobs or [] if j.get("conclusion") in ("failure", "timed_out", "cancelled")]
    pick = next((j for j in bad if names and j.get("name") in names), None) or next(iter(bad), None)
    if not pick:
        return None, None
    return pick.get("databaseId"), first_failed_step(pick)


def first_failed_step(job: Any) -> Any:
    """The number of the job's first failed step, or None."""
    return next((s.get("number") for s in job.get("steps") or [] if s.get("conclusion") in ("failure", "timed_out")), None)


# A decided run's jobs never change: read once per process (2026-11-03: a `gh run view` per notification, 10-20 s each
# over a lossy uplink, made the notifier ~4 events/min -- slower than five unclaimed reds escalating, 3 h behind, W6):
# the reader keeps them (CiRuns.run_jobs).


def _red_run_jobs(o_r: Any, run_id: Any, read_jobs: Any, *, ctx: Context) -> Any:
    """The jobs of a red's run: read_jobs' when given, else the reader's (CiRuns.run_jobs; None: unread)."""
    try:
        return read_jobs() if read_jobs else ctx.runs.run_jobs(o_r, run_id)
    except (Exception, SystemExit):
        return None


def red_url(d, read_jobs=None, *, ctx):
    """The click target of red data d: failing step's line, else the failing job, else the run, else None."""
    run_id = (d.get("last_run") or {}).get("databaseId")
    if not run_id:
        return None
    o_r = red_repo_of(d)
    jobs = _red_run_jobs(o_r, run_id, read_jobs, ctx=ctx)
    job_id, step = failing_job_step(jobs, set(str(j) for j in d.get("jobs") or [d.get("job")] if j))
    return job_step_url(o_r, run_id, job_id, step) if job_id else run_page_url(o_r, run_id)


def ask_page(key, title, text, how=None):
    """A local page rendering the question, the answer command and links, the question section highlighted (:target)."""
    d = state_root("ask-pages")
    os.makedirs(d, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(key))
    p = os.path.join(d, f"{safe}.html")
    esc = _html.escape
    with open(p, "w") as f:
        f.write("<!doctype html><meta charset=utf-8><title>" + esc(title) + "</title><style>body{font:16px system-ui;"
                "max-width:46em;margin:2em auto}section:target{background:#fff3b0;border-left:6px solid #e6a700;padding:1em}"
                "pre{white-space:pre-wrap;background:#eee;padding:.6em}</style><h1>" + esc(title) + "</h1>"
                "<section id=question><h2>Waiting on you</h2><p>" + esc(text) + "</p>"
                + (f"<pre>{esc(how)}</pre>" if how else "") + "</section>")
    return f"file://{p}#question"


def item_red_url(key: Any, store: Any, *, ctx: Context) -> Any:
    """The click target of the red item `key` of the store, or None (no key, no store, no such red, no target)."""
    it = attempt(lambda: store.item("red", key), None, catch=(Exception,)) if key and store is not None else None
    return red_url(it["data"], ctx=ctx) if it else None


def notification_url(ev, store, *, ctx):
    """The URL a notification of event ev opens, or None."""
    refs = ev.get("refs") or {}
    key = refs.get("key")
    u = item_red_url(key, store, ctx=ctx)
    if u:
        return u
    if refs.get("run_url"):
        return refs["run_url"]
    if str(ev.get("kind", "")).startswith(("red.ask", "red.remind")) and key:
        return ask_page(key, f"agentctl: {ev.get('kind')}", str(refs.get("verdict") or ev.get("text") or ""))
    return None
