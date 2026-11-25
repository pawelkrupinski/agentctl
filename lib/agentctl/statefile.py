# agentctl, part `statefile`: THE STATE FILES: one JSON reader (read_record), one text reader (read_text_or) and one atomic writer (write_json).
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's only.

import json
import os
import threading
from typing import Any


def read_record(path: str, default: Any = None) -> Any:
    """THE JSON STATE READER: the JSON at path, or default when it is missing or unreadable (a torn or foreign file)."""
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def read_text_or(path: str, default: str) -> str:
    """The text at path, or default when it cannot be read (OSError only: missing, unreadable, a directory)."""
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return default


def read_json_file(path: str) -> dict[str, Any]:
    """read_record for a file holding an object: {} when it is missing, unreadable or not an object."""
    d = read_record(path)
    return d if isinstance(d, dict) else {}


def write_json(path: str, obj: Any, indent: int | None = 1, mode: int = 0o666, sort_keys: bool = False) -> None:
    """THE JSON STATE WRITER, atomic: a temporary of this process and thread beside path, then os.replace -- a reader
    never sees a torn file and two writers never share a temporary. mode: a new file's permissions (0o600: a secret)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode), "w") as f:
        json.dump(obj, f, indent=indent, sort_keys=sort_keys)
    os.replace(tmp, path)


def write_json_file(path: str, d: Any) -> None:
    """write_json, best effort: a state file that cannot be written is left as it was."""
    try:
        write_json(path, d, indent=None)
    except OSError:
        pass


def mtime(path: Any) -> float:
    try:
        return os.stat(path).st_mtime
    except (OSError, TypeError):
        return 0.0


def tail_bytes(path: str, max_bytes: int) -> tuple[bytes, int]:
    """(the last max_bytes of the file, its size). Raises OSError/TypeError."""
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        return f.read(max_bytes), size
