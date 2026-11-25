# agentctl, part `redworkflow`: a red named by its workflow (renamed in place when its run says another)
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any
from agentctl.core import attempt
from agentctl.timefmt import iso_at
from agentctl.forge import forge_actions_run
from agentctl.workflows import LOCAL_FLOW
from agentctl.gating import main_workflow_files


# A RED IS NAMED BY ITS WORKFLOW (2026-11-12, b059f05c70fa): opened while the forge read a run's run-name for its
# workflow's name, the red "Identity takes (convergence run 43013487979)" #40 named a workflow no other run is in: no
# newer run of it could verify, refute or clear it, its verify dispatch found "no workflow file named" it, and it sat in
# fix-pending 6 h at a time. Such a red takes its workflow's name from its failing run's workflow file, once.
def workflow_settled(d: Any) -> bool:
    """Whether red data d needs no naming: named already, or a local or predicted red (no run of its own workflow)."""
    return bool(d.get("workflow_named") or d.get("workflow") == LOCAL_FLOW or d.get("red_kind") in ("local", "predicts"))


def workflow_rename(d: Any, name: Any, run: Any, at: Any) -> dict[str, Any]:
    """The update marking red data d named: renamed to `name` (the run's workflow file's) with a history line when
    that differs from the workflow it carries."""
    upd = {"workflow_named": True}
    if name and name != d.get("workflow"):
        upd["workflow"] = name
        upd["history"] = (d.get("history") or []) + [{"at": at, "holder": None,
                                                     "outcome": f"RENAMED: {d.get('workflow')!r} is a run-name; its "
                                                                f"workflow is {name!r} ({run.get('workflow_id')})"}]
    return upd


def red_workflow_named(store, it, o_r):
    """The red under its workflow's name (renamed in place when its run says another); it unchanged otherwise."""
    d = it["data"]
    rid = (d.get("last_run") or {}).get("databaseId")
    if not rid or workflow_settled(d):
        return it
    files = main_workflow_files(o_r)
    if not files or d.get("workflow") in files:
        return it
    run = attempt(lambda: forge_actions_run(o_r, rid), None, catch=(Exception, SystemExit))
    if not run:
        return it  # unread now: tried again next tick
    name = {f: n for n, f in files.items()}.get(run.get("workflow_id"))
    store.update_item("red", it["id"], it["version"], workflow_rename(d, name, run, iso_at(store.clock())))
    return store.item("red", it["id"])
