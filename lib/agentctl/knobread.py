# agentctl, part `knobread`: the knob readers: a number or a text from the environment (the config file's values are put there), else the default
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os


def env_number(name: str, default: float) -> float:
    try:
        return float(os.environ[name]) if os.environ.get(name) else default
    except ValueError:
        return default


def env_text(name: str, default: str = "") -> str:
    """A knob's text: the environment's (or the config file's) when set and not empty, else the default."""
    return os.environ.get(name) or default

