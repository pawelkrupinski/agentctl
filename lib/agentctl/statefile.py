# agentctl, part `statefile`: THE STATE FILES: one JSON reader (read_record), one text reader (read_text_or) and one atomic writer (write_json).
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's only.

import contextlib
import json
import os
import sys
import threading
from typing import Any


def load_record(path: str) -> Any:
    """The JSON at path; OSError (missing, unreadable) or ValueError (torn) for a caller that says why it could not."""
    with open(path) as f:
        return json.load(f)


def read_record(path: str, default: Any = None) -> Any:
    """THE JSON STATE READER: the JSON at path, or default when it is missing or unreadable (a torn or foreign file)."""
    try:
        return load_record(path)
    except (OSError, ValueError):
        return default


def json_object(text: str | None) -> dict[str, Any]:
    """The JSON object in a stored text column: NULL, '' and '{}' all read as {} (a lease's meta, an item's data)."""
    obj: dict[str, Any] = json.loads(text or "{}")
    return obj


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
    never sees a torn file, two writers never share a temporary, and a failed write leaves no temporary behind. mode: a new file's permissions (0o600: a secret)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode), "w") as f:
            json.dump(obj, f, indent=indent, sort_keys=sort_keys)
        os.replace(tmp, path)
    except BaseException:  # a failed write (an unserialisable value, a full disk) leaves path as it was and no temporary
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def note_failure(what: str, e: BaseException) -> None:
    """Tell a best-effort step's failure on stderr, `agentctl: <what> (<the error, 160 characters at most>)`: a step that
    may fail without failing its caller never fails silently (tests/test_silent_excepts.py)."""
    print(f"agentctl: {what} ({str(e)[:160]})", file=sys.stderr, flush=True)


def unlink_file(path: str) -> bool:
    """Remove path: True when it went, False when it was already gone (FileNotFoundError, the expected case). Any other
    OSError (a permission, a busy directory) is told on stderr and not raised: a cleanup never fails its caller."""
    try:
        os.unlink(path)
        return True
    except FileNotFoundError:
        return False
    except OSError as e:
        note_failure(f"could not remove {path}", e)
        return False


def write_json_file(path: str, d: Any) -> None:
    """write_json, best effort: a state file that cannot be written is left as it was, and the failure is told."""
    try:
        write_json(path, d, indent=None)
    except OSError as e:
        note_failure(f"could not write {path}", e)


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
