# agentctl, part `configfile`: the config files' knobs read into the environment (env overrides config)
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
from typing import Any


# THE CONFIG FILE (docs/simplification-2026-10.md item 7): every knob has a code default; a TOML file may set it, and an
# environment variable overrides both. Read once, here, before any other code reads a knob: each `key = value` (or
# `[section] key = value`, flattened as section_key) not already in the environment is put there as AGENTCTL_<KEY>,
# so every existing `$AGENTCTL_*` read (and every child process) sees it. Files, lowest precedence first:
#   /etc/agentctl/agentctl.toml           the server copy (infra provides it on ci-runner)
#   ~/.config/agentctl/agentctl.toml      the user's
# $AGENTCTL_CONFIG names the one file to read instead ("-": none; the tests). `agentctl config` shows what applies.
CONFIG_FILES = ("/etc/agentctl/agentctl.toml", os.path.join(os.path.expanduser("~"), ".config", "agentctl", "agentctl.toml"))


CONFIG_SOURCE = {}   # AGENTCTL_<KEY> -> the file that set it


CONFIG_ERRORS = []


def config_files():
    c = os.environ.get("AGENTCTL_CONFIG")
    if c:
        return [] if c == "-" else [c]
    return list(CONFIG_FILES)


def config_flatten(d, prefix=""):
    for k, v in d.items():
        key = f"{prefix}{k}".upper().replace("-", "_").replace(".", "_")
        if isinstance(v, dict):
            yield from config_flatten(v, key + "_")
        else:
            yield ("AGENTCTL_" + key[len("AGENTCTL_"):] if key.startswith("AGENTCTL_") else "AGENTCTL_" + key), v


def config_load():
    """Put the config files' knobs into the environment where it does not set them (env overrides config)."""
    try:
        import tomllib
    except ImportError:  # Python < 3.11: no config files, the environment and the defaults only
        return
    vals = {}
    for path in config_files():  # later files win over earlier ones
        data = config_read(path)
        for k, v in config_flatten(data):
            vals[k] = (v, path)
    config_apply(vals)


def config_read(path: Any) -> Any:
    """The config file's tables ({} for a missing one, or one that does not read: noted in CONFIG_ERRORS)."""
    import tomllib
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        CONFIG_ERRORS.append(f"{path}: {e}")
        return {}


def config_text(v: Any) -> Any:
    """A config value as its environment text: a bool 1 / 0, a list comma-joined."""
    if isinstance(v, bool):
        return "1" if v else "0"
    return ",".join(map(str, v)) if isinstance(v, list) else str(v)


def config_apply(vals: Any) -> Any:
    """The knobs {name: (value, file)} put into the environment, the user's own environment winning."""
    # What an earlier agentctl process put here from a config file (a re-exec, a child) is config, not the user's
    # environment: re-read, so an edited or removed knob applies after the next restart.
    inherited = [k for k in os.environ.get("AGENTCTL_CONFIG_SET", "").split(",") if k]
    for k in inherited:
        if k not in vals:
            os.environ.pop(k, None)
    for k, (v, path) in vals.items():
        if k in os.environ and k not in inherited:
            continue  # the environment says otherwise: it wins
        os.environ[k] = config_text(v)
        CONFIG_SOURCE[k] = path
    if CONFIG_SOURCE or inherited:
        os.environ["AGENTCTL_CONFIG_SET"] = ",".join(sorted(CONFIG_SOURCE))
