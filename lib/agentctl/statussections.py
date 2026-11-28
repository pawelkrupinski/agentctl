# agentctl, part `statussections`: the status model's sections: each one's name, producer and weight
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any, Callable
from agentctl.caches import StatusMemos
from agentctl.statusagents import status_agents
from agentctl.ownerleases import owners_status
from agentctl.reverts import REVERT_OPEN, revert_brief
from agentctl.issues import status_issues
from agentctl.reduntracked import red_untracked_list
from agentctl.statusheader import status_header
from agentctl.statusreds import status_reds
from agentctl.statusfamilies import status_families
from agentctl.statusqueue import status_conditions, status_events, status_landing


def status_section_sources(t: float, repo: str, need: Any, memos: StatusMemos, *, store_probe: Callable[[Any, float], Any],
                           dispatcher: Callable[[Any, float], Any]) -> tuple[Any, ...]:
    """The status model's sections: (name, producer, weight) each, every producer reading the store through `need`; the
    store's probe and the dispatcher's reading are the status module's own (a lower module: passed in)."""
    return (("header", lambda: status_header(t), 1),
               ("store", lambda: store_probe(need(), t), 1),
               ("dispatcher", lambda: dispatcher(need(), t), 1),
               ("reds", lambda: status_reds(need(), t, repo), 1),
               ("families", lambda: status_families(need(), t), 1),
               ("untracked", lambda: [{"repo": o_r, **r} for o_r, r in red_untracked_list(need())], 1),
               ("owners", lambda: owners_status(need(), t), 1),
               ("agents", lambda: status_agents(need(), t, memos), 2),
               ("landing", lambda: status_landing(need(), t), 1),
               ("conditions", lambda: status_conditions(need(), t), 1),
               ("issues", lambda: status_issues(need(), t), 1),
               ("reverts", lambda: [revert_brief(i) for i in need().items("revert", REVERT_OPEN)], 1),
               ("events", lambda: status_events(need()), 1))
