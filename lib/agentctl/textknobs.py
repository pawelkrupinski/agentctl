# agentctl, part `textknobs`: the text, path and fixture knobs: one typed accessor each, its default and doc beside it
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
from typing import Any


DEFAULT_REMOTE = "origin"  # GitHub's remote name in every clone (laptop and ci-runner)


GHAPP_LANDER_REPOS_DEFAULT = "pawelkrupinski/movies"


def env_text(name: str, default: str = "") -> str:
    """A knob's text: the environment's (or the config file's) when set and not empty, else the default."""
    return os.environ.get(name) or default


def knob_no_secrets() -> bool:
    return bool(env_text("AGENTCTL_NO_SECRETS"))  # set: this process never runs `secrets` (a child of recovery)


def knob_detached() -> bool:
    return bool(env_text("AGENTCTL_DETACHED"))  # set by spawn_detached: a background process never runs `secrets`


def knob_interactive() -> bool:
    return env_text("AGENTCTL_INTERACTIVE") == "1"  # 1: the vault may be asked though no terminal is attached


def knob_sock_dir() -> str:
    return env_text("AGENTCTL_SOCK_DIR")  # the token hand-off sockets' directory (default /tmp/agentctl-<uid>)


def knob_remote() -> str:
    return env_text("AGENTCTL_REMOTE").strip() or DEFAULT_REMOTE  # the push and fetch remote of every clone


def knob_state_root() -> str:
    return env_text("AGENTCTL_STATE_ROOT")  # the state root (default ~/.cache/agentctl)


def knob_scratch() -> str:
    return env_text("AGENTCTL_SCRATCH")  # the scratch directory (default: the Claude session's, else the state root)


def knob_host() -> str:
    return env_text("AGENTCTL_HOST")  # ci-runner on the reconciler's host (infra sets it)


def knob_role() -> str:
    return env_text("AGENTCTL_ROLE")  # server forces the server role


def knob_reds_dir() -> str:
    return env_text("AGENTCTL_REDS_DIR")  # the reds' directory (default reds under the state root)


def knob_dispatch_dir() -> str:
    return env_text("AGENTCTL_DISPATCH_DIR")  # the dispatches' directory (default beside the reds')


def knob_recovery_dir() -> str:
    return env_text("AGENTCTL_RECOVERY_DIR")  # the recovery notes' directory


def knob_jvm_ps() -> str:
    return env_text("AGENTCTL_JVM_PS")  # a fixture file of ps rows standing for the process table (tests)


def knob_jvm_cwd() -> str:
    return env_text("AGENTCTL_JVM_CWD")  # a fixture file of `pid cwd` rows (tests)


def knob_jvm_kept() -> str:
    return env_text("AGENTCTL_JVM_KEPT")  # a fixture file of kept checkouts (tests)


def knob_sbt_reap() -> str:
    return os.environ.get("AGENTCTL_SBT_REAP", "")  # 0: never reap this machine's sbt servers (tests)


def knob_github_api() -> str:
    return env_text("AGENTCTL_GITHUB_API", "https://api.github.com")  # the GitHub API base


def knob_identity() -> str:
    return env_text("AGENTCTL_IDENTITY")  # github-app: credentials come from the GitHub Apps (ci-runner)


def knob_github_app(role: str) -> tuple[str, str]:
    """(app id, private key file) of a GitHub App role ($AGENTCTL_GITHUB_<ROLE>_APP_ID / _KEY_FILE), "" when unset."""
    return (env_text(f"AGENTCTL_GITHUB_{role.upper()}_APP_ID").strip(),
            env_text(f"AGENTCTL_GITHUB_{role.upper()}_KEY_FILE").strip())


def knob_github_lander_repos() -> str:
    return env_text("AGENTCTL_GITHUB_LANDER_REPOS", GHAPP_LANDER_REPOS_DEFAULT)  # the repos the lander App writes to, comma-separated


def knob_caffeinate() -> str:
    return os.environ.get("AGENTCTL_CAFFEINATE", "/usr/bin/caffeinate")  # the keep-awake tool; empty: none


def knob_awake_pid() -> str:
    return env_text("AGENTCTL_AWAKE_PID")  # the process that already holds the keep-awake (a child's inherited copy)


def knob_sleep_log() -> str:
    return env_text("AGENTCTL_SLEEP_LOG")  # a file standing for `pmset -g log` (tests)


def knob_gov_busy_probe() -> str:
    return env_text("AGENTCTL_GOV_BUSY_PROBE")  # a command printing the busy cores (tests)


def knob_gov_load_probe() -> str:
    return env_text("AGENTCTL_GOV_LOAD_PROBE")  # a command printing the load, read as the busy cores (tests)


def knob_gov_ps_probe() -> str:
    return env_text("AGENTCTL_GOV_PS_PROBE")  # a file of ps `pcpu args` rows (tests)


def knob_laptop_pressure() -> str | None:
    return os.environ.get("AGENTCTL_LAPTOP_PRESSURE")  # set (tests): the machine's pressure, `-` or empty for none


def knob_inbox_dir() -> str:
    return env_text("AGENTCTL_INBOX_DIR")  # the inbox's directory (default inbox under the state root)


def knob_owners_dir() -> str:
    return env_text("AGENTCTL_OWNERS_DIR")  # the owners' directory (default owners under the state root)


def knob_red_fixer() -> str:
    return env_text("AGENTCTL_RED_FIXER")  # a red fixer's own family (set in a fixer's environment)


def knob_store_token() -> str:
    return env_text("AGENTCTL_STORE_TOKEN")  # the store token an agent's environment carries


def knob_claude() -> str:
    return env_text("AGENTCTL_CLAUDE")  # the `claude` CLI to launch (tests' fake); default: PATH, ~/.local/bin


def knob_claude_settings() -> str:
    return env_text("AGENTCTL_CLAUDE_SETTINGS")  # the interactive sessions' settings.json (default ~/.claude/settings.json)
