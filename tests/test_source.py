"""The source: cudl/ is split into modules, and bin/cudl, the one file that runs, is built from them."""

import ast
import builtins
import json
import re
import shutil
import subprocess
import symtable
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import bundle  # noqa: E402

# what each module may import from: a module builds on the ones below it, never the other way
ALLOWED = {
    "core": set(),
    "lab": {"core"},
    "journal": {"core", "lab"},
    "stack": {"core", "lab"},
    "sessions": {"core", "lab", "journal", "stack"},
    "features": {"core", "lab", "journal", "stack", "sessions"},
    "finish": {"core", "lab", "journal", "stack", "sessions", "features"},
    "setup": {"core", "lab", "journal"},
    "migrate": {"core", "lab", "journal", "sessions"},
    "cli": {"core", "lab", "journal", "stack", "sessions", "features", "finish", "setup", "migrate"},
}


def modules():
    return {p.stem: p.read_text() for p in sorted((ROOT / "cudl").glob("*.py")) if p.stem != "__init__"}


class TestSource(unittest.TestCase):
    def test_bin_cudl_is_the_bundle_of_the_modules(self):
        p = subprocess.run([sys.executable, str(ROOT / "tools/bundle.py"), "--check"], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_modules_import_only_along_the_graph(self):
        self.assertEqual(set(modules()), set(ALLOWED))
        self.assertEqual(sorted(modules()), sorted(bundle.MODULES))  # a module bundle.py leaves out isn't in bin/cudl
        for i, name in enumerate(bundle.MODULES):  # the bundle's order puts what a module uses before it
            self.assertLessEqual(ALLOWED[name], set(bundle.MODULES[:i]), name)
        for name, text in modules().items():
            used = {n.module.split(".")[1] for n in ast.walk(ast.parse(text))
                    if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("cudl.")}
            self.assertLessEqual(used, ALLOWED[name], f"cudl/{name}.py imports from {used - ALLOWED[name]}")

    def test_every_name_a_module_uses_is_defined_or_imported_there(self):
        # in bin/cudl every name is in one namespace, so a missing import would only show here
        def reads(table):
            out = {s.get_name() for s in table.get_symbols()
                   if s.is_referenced() and (s.is_global() or table.get_type() == "module")}
            for child in table.get_children():
                out |= reads(child)
            return out

        for name, text in modules().items():
            table = symtable.symtable(text, name, "exec")
            bound = {s.get_name() for s in table.get_symbols() if s.is_assigned() or s.is_imported() or s.is_namespace()}
            undefined = {u for u in reads(table) - bound if not hasattr(builtins, u)} - {"__file__", "__doc__"}
            self.assertEqual(undefined, set(), f"cudl/{name}.py")

    def test_the_package_imports(self):
        # every `from cudl.x import y` names something x has; no bytecode left in the repository
        every = ", ".join(f"cudl.{m}" for m in bundle.MODULES)
        p = subprocess.run([sys.executable, "-B", "-c", f"import sys; sys.path.insert(0, {str(ROOT)!r}); import {every}"],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_the_template_names_only_hook_events_cudl_has(self):
        # `cudl hook` with an event it doesn't have does nothing (settings an older cudl wrote), so a typo in
        # the template's hook commands would do nothing too
        cli = ast.parse((ROOT / "cudl/cli.py").read_text())
        table = next(n.value for n in cli.body
                     if isinstance(n, ast.Assign) and any(getattr(t, "id", None) == "HOOKS" for t in n.targets))
        known = {k.value for k in table.keys}
        named = set()
        for f in ("template/.claude/settings.json", "template/.codex/hooks.json"):
            for groups in json.loads((ROOT / f).read_text())["hooks"].values():
                named |= {e for g in groups for h in g["hooks"] for e in re.findall(r"\bhook ([a-z-]+)", h["command"])}
        self.assertEqual(named, known)  # and every hook cudl has is wired into a template

    def bundle_with(self, change):
        """bundle.py's output for a copy of cudl/ that change(copy) edited."""
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copytree(ROOT / "cudl", Path(tmp) / "cudl")
            change(Path(tmp) / "cudl")
            return bundle.bundle(Path(tmp))

    def test_the_bundler_refuses_what_one_file_cant_hold(self):
        """Each of these works in the package but would break bin/cudl, quietly or at every run (the code
        review). The modules' docstrings are one line each."""
        def add(line, module="cli", where="after the docstring"):
            def change(pkg):
                lines = (pkg / f"{module}.py").read_text().splitlines(keepends=True)
                i = next(i for i, x in enumerate(lines) if x.startswith('"""')) + 1 if where else len(lines)
                lines.insert(i, line + "\n")
                (pkg / f"{module}.py").write_text("".join(lines))
            return change

        cases = {
            "an alias": add("from cudl.journal import build_index as rebuild_index", "setup"),
            "a relative import": add("from .core import git"),
            "a module object": add("import cudl.finish", "setup"),
            "a module object, from cudl": add("from cudl import finish", "setup"),
            "an unknown module": add("from cudl.nosuch import x"),
            "a __future__ import": add("from __future__ import annotations"),
            "an import that a definition elsewhere would replace": add("from subprocess import run", "migrate"),
            "a __main__ block": add('if __name__ == "__main__":\n    print("hi")', where=None),
            "a name defined twice": add("def git():\n    pass", where=None),
            "a module missing from MODULES": lambda pkg: (pkg / "extra.py").write_text('"""More."""\n'),
        }
        for what, change in cases.items():
            with self.subTest(what), self.assertRaises(bundle.BundleError):
                self.bundle_with(change)
        self.assertEqual(self.bundle_with(lambda pkg: None), (ROOT / "bin/cudl").read_text())

    def test_the_bundle_keeps_a_module_s_whole_docstring(self):
        def two_paragraphs(pkg):
            text = (pkg / "stack.py").read_text()
            first = text.splitlines()[2]
            (pkg / "stack.py").write_text(text.replace(first, first[:-3] + "\n\nThe second paragraph.\n\"\"\"", 1))
        self.assertIn("# The second paragraph.", self.bundle_with(two_paragraphs))


if __name__ == "__main__":
    unittest.main()
