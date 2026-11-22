# agentctl, part `notifyurl`: where an owner notification's click goes (the owner, 2026-11-03: "can agentctl notifications
# open a tab in chrome when clicked and highlight the relevant section?").
# Runs inside agentctl's one namespace (it uses core's gh_json, HOME, state_root).
# A red -> the failing JOB's page anchored at the failing step (GitHub highlights it); else the run; an ask with no run
# -> a local HTML page of the question, its section highlighted by :target; anything else -> None (a plain notification).

import os
import re
from agentctl.core import process_cache, state_root
from agentctl.forge import gh_json
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
    pick = next((j for j in bad if names and j.get("name") in names), None) or (bad[0] if bad else None)
    if not pick:
        return None, None
    step = next((s.get("number") for s in pick.get("steps") or [] if s.get("conclusion") in ("failure", "timed_out")), None)
    return pick.get("databaseId"), step


# A decided run's jobs never change: read once per process (2026-11-03: a `gh run view` per notification, 10-20 s each
# over a lossy uplink, made the notifier ~4 events/min -- slower than five unclaimed reds escalating, 3 h behind, W6).
_run_jobs = process_cache({})


def red_url(d, read_jobs=None):
    """The click target of red data d: failing step's line, else the failing job, else the run, else None."""
    run_id = (d.get("last_run") or {}).get("databaseId")
    if not run_id:
        return None
    o_r = red_repo_of(d)
    jobs = None if read_jobs else _run_jobs.get((o_r, str(run_id)))
    if jobs is None:
        try:
            jobs = (read_jobs or (lambda: gh_json(o_r, "run", "view", str(run_id), "--json", "jobs").get("jobs")))()
            if jobs and not read_jobs:
                _run_jobs[(o_r, str(run_id))] = jobs
        except (Exception, SystemExit):
            jobs = None
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


def notification_url(ev, store=None):
    """The URL a notification of event ev opens, or None."""
    refs = ev.get("refs") or {}
    key = refs.get("key")
    it = None
    if key and store is not None:
        try:
            it = store.item("red", key)
        except Exception:
            it = None
    if it:
        u = red_url(it["data"])
        if u:
            return u
    if refs.get("run_url"):
        return refs["run_url"]
    if str(ev.get("kind", "")).startswith(("red.ask", "red.remind")) and key:
        return ask_page(key, f"agentctl: {ev.get('kind')}", str(refs.get("verdict") or ev.get("text") or ""))
    return None
