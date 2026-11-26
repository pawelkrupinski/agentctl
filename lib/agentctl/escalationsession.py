# agentctl, part `escalationsession`: the escalation dispatcher's world: a fresh worktree, a `claude --bg` session on the red fixers' own launch path, its state in `claude agents`, its stop, claude's workspace trust
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import re
from typing import Any, Optional
from agentctl.escalationtable import EscalationRoute
from agentctl.escalations import escalation_pass
from agentctl.fixersession import SESSION_DENIED_TOOLS, bg_ended, bg_session, bg_start, bg_stop, claude_trust_gap
from agentctl.fixerblockers import cached_dispatch_blocker, claude_cli, interactive_mode
from agentctl.fixerenv import fixer_env
from agentctl.fixer import FIXER_IDENTITY_ENV
from agentctl.repos import escalation_repo_path
from agentctl.core import SHORT
from agentctl.worktrees import fresh_worktree
from agentctl.procs import socket_host

ESCALATION_LABEL = "escalation · "  # `claude agents` lists each session as `escalation · <kind>`


class SessionLauncher:
    """The real launcher (escalations.py EscalationLauncher): what the red fixers launch with, for an escalation."""

    def host(self) -> str:
        return socket_host()

    def blocker(self) -> Optional[str]:
        """Dispatch OFF, no usable claude CLI, a permission mode a headless session cannot run in (the red's)."""
        return cached_dispatch_blocker()

    def repo_path(self, repo: str) -> Optional[str]:
        return escalation_repo_path(repo)

    def trust_gap(self, path: str) -> Optional[str]:
        return claude_trust_gap(path)

    def start(self, route: EscalationRoute, kind: str, condition: dict[str, Any], prompt: str,
              repo: str) -> tuple[Optional[dict[str, Any]], str]:
        """A fresh worktree <repo>-wt-escalation-<kind> off main and a background session in it: ({session, worktree,
        label}, how) or (None, why not)."""
        cli = claude_cli()
        if not cli:
            return None, "no `claude` CLI"
        slug = re.sub(r"[^a-z0-9]+", "-", kind.lower()).strip("-")
        wt, why_not = fresh_worktree(repo, "escalation", slug)
        if wt is None:
            return None, str(why_not)
        label = f"{ESCALATION_LABEL}{kind}"
        argv = [cli, "--bg", prompt, "--permission-mode", interactive_mode(), "--permission-prompts", "none",
                "-n", label, "--model", route.model, "--disallowedTools", *SESSION_DENIED_TOOLS]
        env = {k: v for k, v in fixer_env(SHORT["movies"], f"escalation-{slug}").items() if k not in FIXER_IDENTITY_ENV}
        bid, why = bg_start(argv, wt, env)
        if not bid:
            return None, f"`claude --bg` {why}"
        return {"session": bid, "worktree": wt, "label": label}, f"session '{label}' in {wt}"

    def session_over(self, session: str) -> Optional[bool]:
        """True when the session's turn is over (ended, or no longer listed), False while it works, None when
        `claude agents` cannot be read."""
        cli = claude_cli()
        row = bg_session(cli, session) if cli else {}
        if row == {}:
            return None
        return row is None or bg_ended(row)

    def stop(self, session: str) -> None:
        cli = claude_cli()
        if cli:
            bg_stop(cli, session)


def escalation_step(store: Any, t: float) -> list[str]:
    """The laptop sync's duty (syncsteps.py): one escalation dispatcher pass with the real launcher."""
    return escalation_pass(store, t, SessionLauncher())
