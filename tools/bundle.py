#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build bin/cudl, the one file that runs, from the modules in cudl/.

Every lab keeps a copy of bin/cudl as .cudl/cudl, so what runs stays one file; the source is the
modules. The file is the shebang, then cudl/__init__.py's license notice and docstring, the standard
library imports once each, each module's code in layer order under a banner with its docstring, and
the __main__ block. The `from cudl.… import` lines are dropped: in one file every name is already
there. So a module imports another only as `from cudl.<module> import <name>` (no `as`, no relative
import, no module object); no name is defined in two modules, nor imported in one and defined in
another; and a module has no __future__ import and no `if __name__` block. The bundler refuses
anything else, and compiles what it writes.

    python3 tools/bundle.py           write bin/cudl
    python3 tools/bundle.py --check   exit 1 if bin/cudl isn't the bundle of cudl/
"""

import ast
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# the layer order: a module imports only from modules before it (tests/test_source.py holds the graph)
MODULES = ["core", "lab", "journal", "stack", "sessions", "features", "finish", "setup", "migrate", "cli"]
WIDTH = 100


class BundleError(Exception):
    pass


def internal(node):
    """An import of cudl's own modules, in any form: `from cudl.x import …`, `import cudl…`, a relative one."""
    if isinstance(node, ast.ImportFrom):
        return node.level > 0 or (node.module or "").split(".")[0] == "cudl"
    return isinstance(node, ast.Import) and any(a.name.split(".")[0] == "cudl" for a in node.names)


def bundlable(node):
    """The one form of internal import the bundle can drop: every name it binds is already in the file."""
    return isinstance(node, ast.ImportFrom) and node.level == 0 and node.module.count(".") == 1 and \
        node.module.split(".")[1] in MODULES and all(a.asname is None and a.name != "*" for a in node.names)


def bindings(tree):
    """The names a module's top level defines (not imports)."""
    out = set()
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(n.name)
        elif isinstance(n, (ast.Assign, ast.AnnAssign)):
            for t in (n.targets if isinstance(n, ast.Assign) else [n.target]):
                out |= {x.id for x in ast.walk(t) if isinstance(x, ast.Name)}
    return out


def read_module(root, name):
    """(docstring, standard-library imports as {bound name: statement}, defined names, code)."""
    text = (root / "cudl" / f"{name}.py").read_text()
    tree = ast.parse(text)
    for n in tree.body:
        if isinstance(n, ast.If) and "__name__" in {x.id for x in ast.walk(n.test) if isinstance(x, ast.Name)}:
            raise BundleError(f"cudl/{name}.py:{n.lineno}: no `if __name__ …` in a module: in the bundle, every "
                              "module runs as __main__")
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module == "__future__":
            raise BundleError(f"cudl/{name}.py:{n.lineno}: a __future__ import has to open its file, which it can't "
                              "inside the bundle")
    body = tree.body
    doc = ast.get_docstring(tree)
    head = 1 if doc is not None else 0
    imports = {}
    end = body[head - 1].end_lineno if head else 0
    i = head
    while i < len(body) and isinstance(body[i], (ast.Import, ast.ImportFrom)):
        n = body[i]
        end = n.end_lineno
        if internal(n):
            if not bundlable(n):
                raise BundleError(f"cudl/{name}.py:{n.lineno}: cudl's modules import each other only as `from "
                                  "cudl.<module> import <name>`, without `as` (the bundle drops these lines)")
        else:
            for a in n.names:
                bound = a.asname or a.name.split(".")[0]
                stmt = (f"import {a.name}" + (f" as {a.asname}" if a.asname else "")) if isinstance(n, ast.Import) \
                    else (n.module, a.name + (f" as {a.asname}" if a.asname else ""))
                imports[bound] = stmt
        i += 1
    for n in ast.walk(ast.Module(body=body[i:], type_ignores=[])):
        if internal(n):
            raise BundleError(f"cudl/{name}.py:{n.lineno}: cudl's own imports go at the top of a module, before its "
                              "code (the bundle drops them)")
    code = "\n".join(text.splitlines()[end:]).strip("\n")
    return doc or name, imports, bindings(tree), code


def bundle(root=ROOT):
    found = sorted(p.stem for p in (root / "cudl").glob("*.py") if p.stem != "__init__")
    if sorted(MODULES) != found:
        raise BundleError(f"MODULES lists {', '.join(MODULES)}; cudl/ has {', '.join(found)}: each module once")
    init = (root / "cudl" / "__init__.py").read_text()
    tree = ast.parse(init)
    if not (tree.body and isinstance(tree.body[0], ast.Expr) and isinstance(tree.body[0].value, ast.Constant)):
        raise BundleError("cudl/__init__.py: the docstring (cudl --help prints its first paragraph) is missing")
    notice = [line for line in init.splitlines()[:tree.body[0].lineno - 1] if line.startswith("#")]
    docstring = ast.get_source_segment(init, tree.body[0])
    imports, where, parts = {}, {}, []
    for name in MODULES:
        doc, mod_imports, defined, code = read_module(root, name)
        for bound, stmt in mod_imports.items():
            if imports.setdefault(bound, stmt) != stmt:
                raise BundleError(f"cudl/{name}.py: `{bound}` is imported as two different things")
        for n in defined:
            if n in where:
                raise BundleError(f"{n} is defined in cudl/{where[n]}.py and cudl/{name}.py: one file can hold one")
            where[n] = name
        title = f"# --- cudl/{name}.py "
        about = "\n#\n".join(textwrap.fill(" ".join(par.split()), width=WIDTH, initial_indent="# ",
                                            subsequent_indent="# ") for par in doc.split("\n\n"))
        parts.append(title + "-" * (WIDTH - len(title)) + "\n" + about + "\n\n\n" + code)
    clash = set(where) & set(imports)
    if clash:  # in one file, the definition would replace the import for every module
        raise BundleError(", ".join(f"{n} is defined in cudl/{where[n]}.py and imported elsewhere" for n in sorted(clash)))
    plain = sorted(s for s in imports.values() if isinstance(s, str))
    froms = {}
    for s in imports.values():
        if not isinstance(s, str):
            froms.setdefault(s[0], []).append(s[1])
    lines = plain + [f"from {m} import {', '.join(sorted(ns))}" for m, ns in sorted(froms.items())]
    text = ("#!/usr/bin/env python3\n" + "\n".join(notice) + "\n\n" + docstring + "\n\n" + "\n".join(lines)
            + "\n\n\n" + "\n\n\n".join(parts) + '\n\n\nif __name__ == "__main__":\n    main()\n')
    try:
        compile(text, "bin/cudl", "exec")
    except SyntaxError as e:
        raise BundleError(f"the bundle doesn't compile: {e}") from None
    return text


def main():
    out = ROOT / "bin" / "cudl"
    try:
        text = bundle()
    except BundleError as e:
        sys.exit(f"bundle: {e}")
    if sys.argv[1:] == ["--check"]:
        if not out.is_file() or out.read_text() != text:
            sys.exit("bin/cudl isn't the bundle of cudl/: run python3 tools/bundle.py")
        return
    if sys.argv[1:]:
        sys.exit(__doc__.split("\n\n")[-1])
    out.write_text(text)
    out.chmod(0o755)


if __name__ == "__main__":
    main()
