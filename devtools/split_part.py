#!/usr/bin/env python3
"""Split one agentctl part into several, as a PURE MOVE: the new parts' bodies concatenated in order are exactly the
old file, and PARTS lists them in its place, so the one namespace executes the same statements in the same order.

usage: split_part.py <lib/agentctl dir> <part> <plan.tsv>
plan.tsv rows: <first line marker>\t<new part>\t<one-line description>  (marker: an exact line that starts the piece;
the first row's marker is ignored: the piece starts at line 1 and keeps the old part's name or another)."""
import ast
import os
import sys

lib, part, plan = sys.argv[1], sys.argv[2], sys.argv[3]
src = open(os.path.join(lib, f"{part}.py")).read()
lines = src.splitlines(keepends=True)
rows = [l.rstrip("\n").split("\t") for l in open(plan) if l.strip()]
tree = ast.parse(src)
tops = {n.lineno for n in tree.body} | {min([n.lineno] + [d.lineno for d in getattr(n, "decorator_list", [])]) for n in tree.body}
cuts = [0]
for marker, _name, _desc in rows[1:]:
    idx = [i for i, l in enumerate(lines) if l.rstrip("\n") == marker]
    assert len(idx) == 1, (marker, len(idx))
    i = idx[0]
    # the cut must sit at a top-level boundary: the next code line after the comment block starts a top-level stmt
    j = i
    while lines[j].startswith("#") or not lines[j].strip():
        j += 1
    assert j + 1 in tops, (marker, j + 1)
    assert not any(n.lineno < j + 1 and n.end_lineno >= i + 1 for n in tree.body), ("cut inside a statement", marker)
    cuts.append(i)
assert cuts == sorted(cuts), cuts
cuts.append(len(lines))
pieces = []
for (marker, name, desc), a, b in zip(rows, cuts, cuts[1:]):
    body = "".join(lines[a:b])
    header = "" if name == part else (f"# agentctl, part `{name}`: {desc}\n"
                                      f"# Runs inside agentctl's one namespace, after the parts before it in "
                                      f"lib/agentctl/__init__.py's PARTS (it uses their names).\n\n")
    pieces.append((name, header, body.rstrip("\n") + "\n" if name != rows[-1][1] else body))
# pure-move proof: the bodies, headers aside, are the old file
joined = "".join(b for _, _, b in pieces)
assert joined.replace("\n", "") == src.replace("\n", ""), "not a pure move (text)"
assert ast.dump(ast.parse(joined)) == ast.dump(tree), "not a pure move (ast)"
for name, header, body in pieces:
    with open(os.path.join(lib, f"{name}.py"), "w") as f:
        f.write(header + body)
init = os.path.join(lib, "__init__.py")
s = open(init).read()
names = [n for _, n, _ in rows]
new = ", ".join(f'"{n}"' for n in names)
assert s.count(f'"{part}"') == 1
s = s.replace(f'"{part}"', new)
open(init, "w").write(s)
for name, header, body in pieces:
    print(f"{name}\t{len((header + body).splitlines())}")
