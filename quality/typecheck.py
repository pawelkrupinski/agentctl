#!/usr/bin/env python3
"""agentctl's strict type-check measure: `mypy --strict` errors per part, as `mypy-errors<TAB><part><TAB>N` rows.

  python3 quality/typecheck.py [--root DIR] [--errors]    rows (--errors: every error, mapped back to its part and line)

ONE NAMESPACE (lib/agentctl/__init__.py): the parts read each other's names without imports, so mypy checking a part
alone would call every such name undefined. mypy checks what the entry runs instead: the parts concatenated in PARTS
order, one module, exactly the namespace agentctl executes; each error is mapped back to its part's own line.
"""
import ast
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ERROR = re.compile(r"^[^:]+:(\d+): error: (.*)$")


def part_order(root):
    init = os.path.join(root, "lib", "agentctl", "__init__.py")
    lists = {}  # NAME -> its tuple of part names; PARTS may be a sum of others (MODULES + NAMESPACE)
    for node in ast.parse(open(init).read()).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            value = node.value
            terms = [value.left, value.right] if isinstance(value, ast.BinOp) and isinstance(value.op, ast.Add) \
                else [value]
            try:
                lists[node.targets[0].id] = [n for t in terms for n in (
                    lists[t.id] if isinstance(t, ast.Name) else ast.literal_eval(t))]
            except (ValueError, KeyError, TypeError):
                continue
    if "PARTS" not in lists:
        raise SystemExit("types: no PARTS in lib/agentctl/__init__.py")
    return list(lists["PARTS"])


def concatenate(root, out):
    """Writes the parts into `out` in order; returns [(first line, part)]."""
    starts, line = [], 1
    with open(out, "w") as f:
        for part in part_order(root):
            text = open(os.path.join(root, "lib", "agentctl", f"{part}.py")).read()
            # a real module's imports of its siblings are the namespace's own names here: blanked, lines kept
            text = re.sub(r"^from agentctl\.\w+ import (\([^)]*\)|.*)$", lambda m: "\n" * m.group(0).count("\n"), text,
                          flags=re.M)
            if not text.endswith("\n"):
                text += "\n"
            starts.append((line, part))
            f.write(text)
            line += text.count("\n")
    return starts


def locate(starts, line):
    part, first = starts[0][1], 1
    for start, name in starts:
        if start > line:
            break
        part, first = name, start
    return part, line - first + 1


def mypy_errors(root, run=subprocess.run):
    """[(part, line, message)] of `mypy --strict` over the concatenated namespace."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "agentctl_namespace.py")
        starts = concatenate(root, path)
        cache = os.path.join(os.environ.get("AGENTCTL_QUALITY_HOME") or os.path.expanduser("~/.cache/agentctl-quality"),
                             "mypy-cache")
        r = run([sys.executable, "-m", "mypy", "--strict", "--no-error-summary", "--hide-error-context",
                 "--no-pretty", "--show-error-codes", "--cache-dir", cache, path],
                capture_output=True, text=True, cwd=tmp, timeout=900)
        if r.returncode not in (0, 1):
            raise SystemExit(f"types: mypy failed (exit {r.returncode}):\n{(r.stderr or r.stdout)[-1500:]}")
        return parse(r.stdout, starts)


def parse(output, starts):
    out = []
    for line in output.splitlines():
        m = ERROR.match(line)
        if m:
            part, at = locate(starts, int(m.group(1)))
            out.append((part, at, m.group(2)))
    return out


def rows(errors):
    counts = {}
    for part, _, _ in errors:
        counts[part] = counts.get(part, 0) + 1
    return [("mypy-errors", part, n) for part, n in sorted(counts.items())]


def main(argv):
    root = argv[argv.index("--root") + 1] if "--root" in argv else os.path.dirname(HERE)
    errors = mypy_errors(root)
    if "--errors" in argv:
        for part, line, message in errors:
            print(f"lib/agentctl/{part}.py:{line}: {message}")
        return
    for kind, key, value in rows(errors):
        print(f"{kind}\t{key}\t{value}")


if __name__ == "__main__":
    main(sys.argv[1:])
