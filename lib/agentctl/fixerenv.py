# agentctl, part `fixerenv`: a dispatched fixer's environment: no parent session, no secret but the credential agentctl reads (SECRET_ENV), and the brief files its prompt reads (BRIEF; the override is knob_brief_src).
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
from typing import Any
import re
from contextlib import suppress
from agentctl.core import HOME, SHORT
from agentctl.repos import repo_signs
from agentctl.storetoken import no_vault, token, token_env_name


# -- dispatch: a headless Claude Code fixer in a fresh worktree
SECRET_ENV = re.compile(r"TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|PRIVATE|CREDENTIAL|VAULT", re.I)


def fixer_env(o_r: str, num: Any, wrapper_pid: int | None = None) -> dict[str, str]:
    """A clean environment for a dispatched fixer: no parent Claude session, no secret but the credential agentctl
    itself reads, never PREPUSH_SKIP."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CLAUDE_CODE_", "CLAUDECODE")) and k not in ("CLAUDE_PID", "PREPUSH_SKIP", "AI_AGENT")
           and not SECRET_ENV.search(k)}
    with suppress(Exception, SystemExit):
        tok = token(o_r)
        env[token_env_name(o_r)] = tok
    # EVERY FIXER TALKS TO THE STORE (`red claim | beat | release`, `land`) with the STORE TOKEN, whatever its repo: the
    # filter above drops it with every other secret, and a non-movies repo's credential is gh's own (none here)
    if "AGENTCTL_STORE_TOKEN" not in env:
        with suppress(Exception, SystemExit):
            with no_vault():
                env["AGENTCTL_STORE_TOKEN"] = token(SHORT["movies"])
    env.update(AGENTCTL_AGENT_NAME=f"red-fixer-{num}", AGENTCTL_RED_FIXER=str(num))
    # launchd gives the processor neither USER nor LOGNAME, and without them `claude` finds no login in the keychain
    # ("Not logged in", 2026-10-27); nor ~/.local/bin on PATH
    import pwd
    me = pwd.getpwuid(os.getuid()).pw_name
    env.setdefault("USER", me)
    env.setdefault("LOGNAME", me)
    path = env.get("PATH", "/usr/bin:/bin")
    env["PATH"] = ":".join(dict.fromkeys([os.path.join(HOME, ".local", "bin"), "/opt/homebrew/bin"] + path.split(":")))
    # SIGNING THROUGH THE SECRETS AGENT (issue 20261116T224456-3b788f): launchd hands its jobs its own SSH_AUTH_SOCK, an
    # empty agent, while ~/.zshrc points commit.gpgsign (gpg.format ssh) at ~/.ssh/secrets-agent.sock; a fixer's or a
    # revert's commit then failed "No private key found". Where that socket is, every spawned session signs through it.
    # The socket is a symlink to the secrets daemon's own (issue 20261119T155416-99cfd9, core red 1071: the daemon's end
    # was not there when the fixer started, so os.path.exists said no and the session kept launchd's empty agent for
    # good): the configured route counts, dangling or not -- once the daemon is up the same session signs.
    agent = os.path.join(HOME, ".ssh", "secrets-agent.sock")
    if os.path.lexists(agent):
        env["SSH_AUTH_SOCK"] = agent
    # NO SIGNATURE WHERE MAIN KEEPS NONE (2026-11-22: #348/#349's revert and Order independence #105's fix, tested, sat
    # BLOCKED ON OWNER on the vault for 91 and 43 min, each owner turn a new `claude` process asking its own Touch ID).
    # movies' land rebases every change with NO_SIGN, so the signature those waits were for never reached main: a
    # fixer of a repo whose main is unsigned (red-repos.json `signed: false`) commits unsigned, git's own env config.
    if not repo_signs(o_r):
        n = int(env.get("GIT_CONFIG_COUNT") or 0)
        env.update({"GIT_CONFIG_COUNT": str(n + 2), f"GIT_CONFIG_KEY_{n}": "commit.gpgsign",
                    f"GIT_CONFIG_VALUE_{n}": "false", f"GIT_CONFIG_KEY_{n + 1}": "tag.gpgsign",
                    f"GIT_CONFIG_VALUE_{n + 1}": "false"})
    if wrapper_pid:
        env["AGENTCTL_CLAIM_PID"] = str(wrapper_pid)
    return env


BRIEF = os.path.join(HOME, "bin", "briefs", "movies-agent-preamble.md")
