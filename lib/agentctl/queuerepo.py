# agentctl, part `queuerepo`: the repository a `land` or `waiting` subcommand works on
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import argparse
from agentctl.core import git, git_ok
from agentctl.procs import safe_cwd
from agentctl.repos import movies_repo_path
from agentctl.clones import main_clone


def waiting_repo(a: argparse.Namespace) -> str:
    """The clone a `waiting` / `land` queue command acts on: --repo, else the checkout it runs in."""
    here = a.repo or safe_cwd()
    if a.wcmd == "await":
        return main_clone(here)
    if git_ok(here, "rev-parse", "--show-toplevel"):
        return git(here, "rev-parse", "--show-toplevel").strip()
    return main_clone(movies_repo_path())  # outside any git checkout (e.g. ~/bitcashier): the movies clone's queue, as `await` resolves it
