# agentctl, part `workflowdispatch`: a workflow run dispatched on main (red_verify_dispatch), and the inputs a dispatched run carries, read back
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import re
from typing import Any
from agentctl.govpool import json
from agentctl.statefile import read_record
from agentctl.clock import epoch_now
from agentctl.core import remote
from agentctl.ghapp import urllib
from agentctl.workflows import workflow_file_of
from agentctl.forge import api, gh_json
from agentctl.redrepos import RED_WORKFLOWS_FILE, verify_dispatchable
from agentctl.workflowcron import workflow_crons
from agentctl.runowners import record_dispatch
from agentctl.paths import dispatches_dir


def verify_inputs(o_r: Any, run_id: Any, workflow_text: str) -> Any:
    """The inputs a verify dispatch repeats: the failing run's own ({} with no run)."""
    if not run_id:
        return {}
    return run_dispatch_inputs(o_r, run_id) or run_title_inputs(o_r, run_id, workflow_text)


def red_verify_dispatch(o_r, repo, workflow, sha, run_id=None):
    """The red's workflow dispatched on main to verify a landed fix (no wait: the next tick reads its run), with the
    failing run's own inputs (run_id's) so the verifying run repeats it. Archive recording #823 (2026-10-29): dispatched
    with no inputs, its required `runs` input made Forgejo refuse it (HTTP 400), so no run could ever verify the red."""
    path, txt = workflow_file_of(repo, f"{remote()}/main", workflow) if repo and workflow else (None, "")
    if not path:
        return f"FIX UNVERIFIED: no workflow file named {workflow!r} on main"
    if "workflow_dispatch" not in txt:
        return f"FIX UNVERIFIED until {workflow}'s next scheduled run (no workflow_dispatch)"
    if workflow_crons(txt) and not verify_dispatchable(repo, workflow):  # A VERIFY DISPATCH IS PER WORKFLOW
        return (f"FIX UNVERIFIED until {workflow}'s next scheduled run (scheduled, and {RED_WORKFLOWS_FILE} says "
                f"verify_dispatch no: never dispatched to verify)")
    inputs = verify_inputs(o_r, run_id, txt)
    try:
        api(o_r, "POST", f"actions/workflows/{os.path.basename(path)}/dispatches", body={"ref": "main", "inputs": inputs})
        record_dispatch(branch="main", workflow=workflow, sha=sha, agent="red verify", pid=os.getpid(), inputs=inputs,
                        purpose=f"verify the fix {sha[:10]}", key=f"verify-{os.path.basename(path)}-{int(epoch_now())}")
    except (Exception, SystemExit) as e:
        return f"FIX UNVERIFIED: could not dispatch {workflow} ({http_error_text(e)[:240]})"
    return f"dispatched {workflow} on main to verify {sha[:10]}" + (f" with {json.dumps(inputs)}" if inputs else "")


def run_dispatch_inputs(o_r, run_id):
    """The workflow_dispatch inputs run_id was started with ({} for any other event, or when its run cannot be read).
    GitHub serves no run's inputs: they are the dispatch record's (agentctl's own, written by `dispatch run`)."""
    rec = read_record(dispatches_dir("inputs", f"{run_id}.json"))
    return {str(k): str(v) for k, v in rec.items()} if isinstance(rec, dict) else {}


RUN_TITLE_INPUT = re.compile(r"(?<!\S)([A-Za-z_][\w-]*)=(\S*)")


def run_title_inputs(o_r, run_id, workflow_text):
    """The dispatch inputs a run that agentctl did not start carries in its title: the workflow's `run-name` writes them
    as `name=value` (`Verify scrape recording recording=<id> countries=<cc,...>`), and only names the workflow declares
    as an input are taken. {} when its title names none or the run cannot be read. 2026-11-11, Verify scrape recording
    #23: dispatched by Record scrape fixtures, its verify went out without the required `recording` (HTTP 422)."""
    try:
        title = gh_json(o_r, "run", "view", str(run_id), "--json", "displayTitle").get("displayTitle") or ""
    except (Exception, SystemExit):
        return {}
    return {k: v for k, v in RUN_TITLE_INPUT.findall(title)
            if re.search(rf"^\s+{re.escape(k)}:\s*$", workflow_text or "", re.M)}


def http_error_text(e):
    """An exception's text, with the response body when it is an HTTP error (the forge names a refusal's cause there)."""
    text = str(e)
    if isinstance(e, urllib.error.HTTPError):
        try:
            body = e.read().decode("utf-8", "replace").strip()
        except Exception:
            body = ""
        if body:
            text += f": {body}"
    return text
