# agentctl, part `buildview`: what an sbt build means for a change's modules, read statically from build.sbt and project/.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import re
from typing import Any
from agentctl.core import git, git_ok


# ---------------------------------------------------------------- THE BUILD AS A CHANGE'S MODULES SEE IT
# The owner, 2026-11-17 (landing stats: a build.sbt / project/ change landing meanwhile forced a FULL re-check of every
# queued change -- 6 that day at ~22 min each, most of them refactor churn that moved nothing the change's checks run):
# "make it selection-aware, conservatively". queue.py's THE RULE asks build_view_change(): the build's definition AS
# THE CHANGE'S MODULES SEE IT, at the tip its full check passed and at the rebased tip. Equal (and the hook's selection
# for the change equal, selection_change) -> the combined-tree verification round; anything else -> the full round.
#
# THE VIEW (a static read, no sbt: the decision runs under the landing lease in seconds). build.sbt, comments and
# whitespace dropped, is cut into its top-level statements:
#   - every statement that DEFINES nothing (`ThisBuild / scalacOptions ++= ...`, imports, `addCommandAlias`,
#     `ImageInputs.buildSettings`, `Global / ...`) is in every view: it reaches every module;
#   - a definition (`lazy val x =`, `val x =`, `def x(`) is in the view when a statement in the view names it, so a
#     module's project definition pulls in what it uses -- the settings helpers, the configurations, and the projects it
#     dependsOn (by name), transitively: its whole upstream, the dependsOn edges included;
#   - project/*.scala: `Obj.member` (and a bare member of an object a global statement imports, `import Dependencies._`)
#     pulls in that member, the members it names, and the object's text outside its members (imports, the signature); a
#     bare `Obj` (`.enablePlugins(Obj)`) pulls in its whole file; an AutoPlugin with `allRequirements` is in every view.
#   - every other file under project/ (plugins.sbt, build.properties, project/project/...) and every root *.sbt but
#     build.sbt (coupling.sbt, identity-impact.sbt: sbt reads them all) is in every view, whole.
# The SEEDS are the projects of the change's own module directories and of every module its selection names (the hook's
# --plan, --unit-plan and the area lines of local-tests.txt it selects: `worker/testOnly ...`, `identity/testOnly ...`) --
# what the full round would run beyond the verification, which compiles every test config under the new build anyway.
# Over-inclusion is the safe side: an identifier that merely LOOKS like a definition's name pulls it in. Anything that
# cannot be read (a file, a statement, no project for a module) answers None: the full round.
BUILD_VIEW_PATHS = re.compile(r"^([^/]+\.sbt|project/.*)$")  # every root *.sbt is the build (sbt reads them all)
SBT_DEF = re.compile(r"^(?:lazy\s+val|val|def)\s+([A-Za-z_][A-Za-z0-9_]*)")
SBT_PROJECT_DIR = re.compile(r"\bproject\s*(?:in|\.in)\s*\(?\s*file\(\s*\"([^\"]*)\"\s*\)")
SCALA_MEMBER = re.compile(r"^(?:(?:private(?:\[\w+\])?|protected|override|final|implicit|lazy|sealed|case)\s+)*"
                          r"(?:val|var|def|object|class|trait|type)\s+([A-Za-z_][A-Za-z0-9_]*)")
SCALA_TOPLEVEL = re.compile(r"^(?:(?:private|final|sealed|case|abstract)\s+)*(object|class|trait)\s+([A-Za-z_][A-Za-z0-9_]*)",
                            re.M)
SCALA_STRING = re.compile(r'"""[\s\S]*?"""|"(?:\\.|[^"\\\n])*"')
SBT_WILDCARD_IMPORT = re.compile(r"^import\s+([\w.]+?)\.(?:_|\*)\s*$")
SBT_NAMED_IMPORT = re.compile(r"^import\s+([\w.]+?)\.\{([^}]*)\}\s*$")


def _literal_end(text: str, i: int) -> int | None:
    """Where the string literal starting at text[i] ends (a triple-quoted one, or a plain one to its closing quote or the
    line's end); None when text[i] starts no literal."""
    n = len(text)
    if text.startswith('"""', i):
        j = text.find('"""', i + 3)
        return n if j < 0 else j + 3
    if text[i] != '"':
        return None
    j = i + 1
    while j < n and text[j] != '"' and text[j] != "\n":
        j += 2 if text[j] == "\\" else 1
    return j + 1


def _comment_end(text: str, i: int) -> int | None:
    """Where the comment starting at text[i] ends (a `//` one at its line's end, a block one after its `*/`); None when
    text[i] starts no comment."""
    n = len(text)
    if text.startswith("//", i):
        j = text.find("\n", i)
        return n if j < 0 else j
    if text.startswith("/*", i):
        j = text.find("*/", i + 2)
        return n if j < 0 else j + 2
    return None


def scala_code(text: str) -> str:
    """Scala/sbt source without its comments, each string literal kept (so a `//` inside one is no comment)."""
    out, i, n = [], 0, len(text)
    while i < n:
        end = _literal_end(text, i)
        if end is not None:
            out.append(text[i:end])
            i = end
            continue
        end = _comment_end(text, i)
        if end is not None:
            i = end
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def starts_statement(depth: int, ind: int, col: Any, stripped: str, cur: Any) -> bool:
    """Whether a line (stripped, at indentation ind) starts a new statement: outside any bracket, at or left of the
    statement column, no continuation, and a statement already open."""
    return bool(depth == 0 and ind <= col and not re.match(r"[.)}\]+\-*/|&=<>:,]", stripped) and cur)


def scala_statements(code: Any, first: Any = None) -> Any:
    """The top-level statements of comment-free code: a statement starts on a line at the indentation of the block's
    first line, outside any bracket, that is no continuation (`.`, `)`, `}`, `]`, an operator). first: the column a
    statement starts at (None: the first non-blank line's). Returns [(text, normalized text)]."""
    stmts, cur, depth, col = [], [], 0, first
    for line in code.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        ind = len(line) - len(line.lstrip())
        if col is None:
            col = ind
        if starts_statement(depth, ind, col, stripped, cur):
            stmts.append("\n".join(cur))
            cur = []
        cur.append(line)
        depth += sum(stripped.count(o) for o in "([{") - sum(stripped.count(o) for o in ")]}")
        depth = max(depth, 0)
    if cur:
        stmts.append("\n".join(cur))
    return [(s, " ".join(s.split())) for s in stmts]


def scala_object_parts(code: Any, name: Any) -> Any:
    """(header, {member: text}) of object `name` in comment-free code: its members are its body's statements that
    declare a name; the header is the rest (imports, the signature, a body statement declaring nothing). None when the
    object's body cannot be found."""
    m = re.search(rf"\bobject\s+{re.escape(name)}\b[^{{]*\{{", code)
    if not m:
        return None
    depth, j = 1, m.end()
    while j < len(code) and depth:
        depth += {"{": 1, "}": -1}.get(code[j], 0)
        j += 1
    if depth:
        return None
    body = code[m.end():j - 1]
    members, header = {}, [code[:m.end()], code[j - 1:]]
    first = next((len(l) - len(l.lstrip()) for l in body.splitlines() if l.strip()), 0)
    for raw, norm in scala_statements(body, first):
        mm = SCALA_MEMBER.match(raw.strip())
        if mm:
            members[mm.group(1)] = members.get(mm.group(1), "") + norm
        else:
            header.append(norm)
    return " ".join(" ".join(h.split()) for h in header), members


def _note_sbt_statements(b: dict[str, Any], sbt: str) -> None:
    """build.sbt's statements into b: a definition into defs (and projects, by its directory), anything else a global
    statement (an import also into imports)."""
    for raw, norm in scala_statements(scala_code(sbt), 0):
        m = SBT_DEF.match(raw)
        if m:
            b["defs"][m.group(1)] = b["defs"].get(m.group(1), "") + norm
            p = SBT_PROJECT_DIR.search(raw)
            if p:
                b["projects"][p.group(1).strip("./") or "."] = m.group(1)
            continue
        b["globals"].append(norm)
        _note_import(b, norm)


def _note_import(b: dict[str, Any], norm: str) -> None:
    """A build-wide statement that is an import: its wildcard object, or each named member's, into b's imports."""
    w = SBT_WILDCARD_IMPORT.match(norm)
    if w:
        b["imports"].setdefault("*", []).append(w.group(1).split(".")[-1])
    n = SBT_NAMED_IMPORT.match(norm)
    if n:
        for name in n.group(2).split(","):
            b["imports"][name.strip().split("=>")[0].strip()] = n.group(1).split(".")[-1]


def _note_whole_file(tree: str, rev: str, path: str, b: dict[str, Any]) -> bool:
    """A file in every view, whole (by its blob): False when its blob cannot be read."""
    blob = git(tree, "rev-parse", f"{rev}:{path}", check=False)
    if blob.returncode != 0:
        return False
    b["always"].append(("file", path, blob.stdout.strip()))
    return True


def _note_root_sbts(tree: str, rev: str, b: dict[str, Any]) -> bool:
    """Every root *.sbt but build.sbt (coupling.sbt, identity-impact.sbt: sbt reads them all) in every view, whole; False
    when the root or a blob cannot be read."""
    roots = git(tree, "ls-tree", "--name-only", rev, check=False)
    if roots.returncode != 0:
        return False
    sbts = [p for p in roots.stdout.split() if p.endswith(".sbt") and p != "build.sbt"]
    return all(_note_whole_file(tree, rev, p, b) for p in sbts)


def _note_scala_file(tree: str, rev: str, path: str, b: dict[str, Any]) -> bool:
    """A project/*.scala file: its text, its objects, and whether it is an AutoPlugin in every view; False when unreadable."""
    r = git(tree, "show", f"{rev}:{path}", check=False)
    if r.returncode != 0:
        return False
    code = scala_code(r.stdout)
    b["files"][path] = " ".join(code.split())
    if re.search(r"\bextends\s+AutoPlugin\b", code) and re.search(r"\btrigger\b[^=\n]*=\s*allRequirements\b", code):
        b["always"].append(("file", path))
    for kind, name in SCALA_TOPLEVEL.findall(code):
        b["objects"][name] = (path, scala_object_parts(code, name) if kind == "object" else None)
    return True


def _note_project_file(tree: str, rev: str, path: str, b: dict[str, Any]) -> bool:
    """One file under project/ (a target directory's none): a .scala file read, any other whole; False when unreadable."""
    if "/target/" in f"/{path}":
        return True
    if re.fullmatch(r"project/[^/]+\.scala", path):
        return _note_scala_file(tree, rev, path, b)
    return _note_whole_file(tree, rev, path, b)


def read_build(tree: str, rev: str) -> dict[str, Any] | None:
    """The build at rev, parsed: {defs, globals, projects (dir -> def name), files ({path: code}), objects
    ({name: (file, parts or None)}), always ([unit]), imports ({member: object})}; None when unreadable."""
    sbt = git(tree, "show", f"{rev}:build.sbt", check=False)
    if sbt.returncode != 0:
        return None
    listing = git(tree, "ls-tree", "-r", "--name-only", rev, "--", "project", check=False)
    if listing.returncode != 0:
        return None
    b: dict[str, Any] = {"defs": {}, "globals": [], "projects": {}, "files": {}, "objects": {}, "always": [],
                         "imports": {}}
    _note_sbt_statements(b, sbt.stdout)
    if not _note_root_sbts(tree, rev, b):
        return None
    return b if all(_note_project_file(tree, rev, p, b) for p in listing.stdout.split()) else None


class BuildView:
    """THE VIEW above over a parsed build b: the units reachable from the seeds, each with its text."""

    def __init__(self, b: dict[str, Any]) -> None:
        self.b = b
        self.wild = set(b["imports"].get("*", []))

    def text_of(self, u: tuple[Any, ...]) -> Any:
        b = self.b
        if u[0] == "global":
            return u[1]
        if u[0] == "def":
            return b["defs"][u[1]]
        if u[0] == "file":
            return b["files"].get(u[1]) if len(u) == 2 else u[2]
        if u[0] == "header":
            return b["objects"][u[1]][1][0]
        return b["objects"][u[1]][1][1][u[2]]  # member

    def obj_member(self, o: str, m: str) -> list[tuple[Any, ...]]:
        path, parts = self.b["objects"][o]
        if parts is None or m not in parts[1]:
            return [("file", path)]
        return [("header", o), ("member", o, m)]

    def members_of(self, obj: str) -> dict[str, Any]:
        """The members of project/ object obj (none for an unread one)."""
        return (self.b["objects"][obj][1] or ("", {}))[1]

    def string_files(self, text: str) -> list[tuple[Any, ...]]:
        """The project/ files a string literal of text names (`"Coupling.scala"`: a module's source)."""
        return [("file", f) for lit in SCALA_STRING.findall(text) for f in self.b["files"]
                if os.path.basename(f) in lit]

    def named_units(self, u: tuple[Any, ...], name: str, member: str | None) -> list[tuple[Any, ...]]:
        """The units identifier `name` (`name.member`) pulls in from unit u."""
        here = u[1] if u[0] in ("header", "member") else None
        if here and name == here and not member:
            return []  # the object's own signature
        defined = [("def", name)] if name in self.b["defs"] and u[0] in ("global", "def") else []  # project/ code
        own = [("member", here, name)] if here and self.members_of(here).get(name) is not None else []  # sees no def
        return defined + self.object_units(name, member) + own + self.imported_units(name)

    def object_units(self, name: str, member: str | None) -> list[tuple[Any, ...]]:
        """The units a project/ object named `name` (`name.member`) pulls in: the member, else its whole file."""
        if name not in self.b["objects"]:
            return []
        return self.obj_member(name, member) if member else [("file", self.b["objects"][name][0])]

    def imported_units(self, name: str) -> list[tuple[Any, ...]]:
        """The units a bare `name` pulls in through build.sbt's imports: a named import's object, every wildcard one's."""
        owner = self.b["imports"].get(name)
        out = self.obj_member(owner, name) if owner in self.b["objects"] else []
        for o in self.wild:
            if o in self.b["objects"] and self.members_of(o).get(name) is not None:
                out += self.obj_member(o, name)
        return out

    def references(self, u: tuple[Any, ...], text: str) -> list[tuple[Any, ...]]:
        """The units unit u (its text) names, in the order they are read."""
        out = self.string_files(text) if u[0] in ("global", "def") else []
        code = SCALA_STRING.sub('""', text)  # a name inside a string (an alias's command, a path) is no reference
        for m in re.finditer(r"([A-Za-z_][A-Za-z0-9_]*)(\s*\.\s*([A-Za-z_][A-Za-z0-9_]*))?", code):
            out += self.named_units(u, m.group(1), m.group(3))
        return out

    def reachable(self, todo: list[tuple[Any, ...]]) -> dict[tuple[Any, ...], str] | None:
        """{unit: text} of everything todo reaches; None when a unit cannot be read."""
        view: dict[tuple[Any, ...], str] = {}
        while todo:
            u = todo.pop()
            if u in view:
                continue
            text = self.text_of(u)
            if text is None:
                return None
            view[u] = text
            if (u[0] == "file" and len(u) == 3) or (u[0] == "global" and text.startswith("import ")):
                continue  # a whole file; or an import: what it imports is read where it is used (b["imports"])
            todo += self.references(u, text)
        return view


def build_view(tree: str, rev: str, module_dirs: Any) -> dict[str, str] | None:
    """{unit: text} of the build at rev as the modules in module_dirs see it (THE VIEW above); None when unreadable or
    a module directory has no project in build.sbt."""
    b = read_build(tree, rev)
    if b is None:
        return None
    seeds = []
    for d in sorted(module_dirs):
        if d not in b["projects"]:
            return None
        seeds.append(("def", b["projects"][d]))
    view = BuildView(b).reachable([("global", g) for g in b["globals"]] + seeds + list(b["always"]))
    return None if view is None else {":".join(str(x) for x in u): t for u, t in view.items()}


def _selection_words(selection: Any) -> Any:
    """Every word of a hook selection's modes but the compile and lint plans."""
    for key, words in (selection or {}).items():
        if key not in ("--compile-plan", "--lint-plan"):
            yield from " ".join(words or []).split()


def selection_modules(selection: Any, module_dirs: Any) -> Any:
    """The module directories a hook selection (hook_selection: {mode: words}, local-tests lines) names: a word or line
    token `<module>/...` of a known module. The compile and lint plans are left out: the verification runs them, under
    the new build."""
    out = set()
    for w in _selection_words(selection):
        head = w.split("/", 1)[0]
        if "/" in w and head in module_dirs:
            out.add(head)
    return out


def change_module_dirs(paths: Any, module_dirs: Any) -> Any:
    """The module directories the change's own paths are under."""
    return {p.split("/", 1)[0] for p in paths if "/" in p and p.split("/", 1)[0] in module_dirs}


def build_view_change(tree: Any, full_tip: Any, tip: Any, paths: Any, selection: Any) -> Any:
    """None when the build's view for the change (its modules and the modules its selection at tip names) is the same
    at full_tip and at tip; else why not, in words (THE VIEW above)."""
    if any(BUILD_VIEW_PATHS.match(p) for p in paths):
        return "the change itself changes the build"
    if not full_tip or not git_ok(tree, "cat-file", "-e", f"{full_tip}^{{commit}}"):
        return "the tip of its full check is unknown"
    now = read_build(tree, tip)
    if now is None:
        return "the build cannot be read"
    dirs = set(now["projects"]) - {"."}
    modules = change_module_dirs(paths, dirs) | selection_modules(selection, dirs)
    old, new = build_view(tree, full_tip, modules), build_view(tree, tip, modules)
    if old is None or new is None:
        return "its build view cannot be read"
    return _moved_view_words(old, new, modules)


def _moved_view_words(old: Any, new: Any, modules: Any) -> Any:
    """None when the two views are alike; else the units that moved, in words."""
    moved = sorted(k for k in set(old) | set(new) if old.get(k) != new.get(k))
    if not moved:
        return None
    what = ", ".join(build_view_unit_words(k) for k in moved[:3]) + (f" (+{len(moved) - 3} more)" if len(moved) > 3 else "")
    return f"what {', '.join(sorted(modules)) or 'every module'} build{'s' if len(modules) == 1 else ''} with changes ({what})"


def build_view_unit_words(k: Any) -> Any:
    """A view unit's key in words: `def:worker` -> worker, `global:<text>` -> a build-wide setting, `member:O:m` -> O.m."""
    kind, _, rest = k.partition(":")
    return {"global": "a build-wide setting", "def": rest, "file": rest, "header": rest,
            "member": rest.replace(":", ".")}.get(kind, k)
