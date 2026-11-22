# agentctl, part `issues`: the issues anyone files for the owner: opened, closed
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import datetime as dt
import os
import time
from typing import Any, Optional
from agentctl.core import hashlib_sha1
from agentctl.timefmt import ts


ISSUE_TEXT_MAX = 4000


def issue_open(st: Any, text: Any, by: str, cwd: Optional[str] = None, now_: Optional[float] = None) -> Any:
    """A new open issue; returns the item."""
    t = now_ if now_ is not None else time.time()
    text = str(text or "").strip()[:ISSUE_TEXT_MAX]
    iid = f"{dt.datetime.fromtimestamp(t, dt.timezone.utc):%Y%m%dT%H%M%S}-{hashlib_sha1(f'{by}|{text}|{t}')[:6]}"
    return st.put_item("issue", iid, "open", {"text": text, "by": by, "at": ts(), "cwd": cwd,
                                              "session": os.environ.get("CLAUDE_CODE_SESSION_ID")})


def issue_close(st: Any, iid: str, note: str, by: str) -> Any:
    it = st.item("issue", iid)
    if not it:
        return None
    if it["state"] != "closed":
        st.transition("issue", iid, "closed", it["version"], data={"closed_by": by, "note": note, "closed_at": ts()},
                      event={"text": f"issue {iid} closed by {by}: {note}"[:400]})
    return st.item("issue", iid)
