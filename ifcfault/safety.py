"""
Static safety gate for model-authored code.

This runs BEFORE anything generated is executed, anywhere, and it never
executes the code it inspects  - it is pure `ast` analysis. It is the first
of three gates; the other two are the subprocess boundary in `validate.py`
and the independent verification in `verify/`.

What it is actually defending against: a 32B code model asked to write a
file-writing script will, given the chance, reach for `subprocess`,
`shutil.rmtree`, `os.remove`, `requests`, or an `exec` of a string it built
 - not maliciously, just because those appear in training data near "write a
script". A generated harness that deletes a directory or phones home is a
bug that costs real source models, so the allowlist is deliberately tight
and additions should be argued for.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass

# Modules a generated harness legitimately needs.
ALLOWED_IMPORTS = {
    "argparse", "collections", "dataclasses", "datetime", "hashlib", "itertools",
    "json", "math", "os.path", "pathlib", "statistics", "sys", "textwrap", "time",
    "typing", "uuid",
    "ifcopenshell", "ifcopenshell.guid", "ifcopenshell.util", "ifcopenshell.util.placement",
    "__future__",
}

# Bare `os` is allowed only for os.path/os.makedirs-style use; the dangerous
# members are blocked by name below rather than banning the module outright,
# since `os.makedirs` is genuinely useful for an --outdir.
ALLOWED_IMPORTS.add("os")

FORBIDDEN_IMPORTS = {
    "subprocess", "shutil", "socket", "http", "urllib", "requests", "httpx",
    "multiprocessing", "threading", "ctypes", "pickle", "marshal", "importlib",
    "pty", "signal", "tempfile", "webbrowser", "smtplib", "ftplib",
}

# Callables that must never appear, by name.
FORBIDDEN_CALLS = {
    "eval", "exec", "compile", "__import__", "input", "breakpoint",
    "system", "popen", "spawn", "spawnl", "spawnv", "fork", "execv", "execve",
    "remove", "unlink", "rmdir", "rmtree", "removedirs", "chmod", "chown",
    "kill", "killpg", "truncate",
}

# Attribute chains that must never appear, even if the name alone looks fine.
FORBIDDEN_ATTRS = {
    ("os", "system"), ("os", "popen"), ("os", "remove"), ("os", "unlink"),
    ("os", "rmdir"), ("os", "removedirs"), ("os", "execv"), ("os", "fork"),
    ("sys", "exit_"),  # placeholder: sys.exit itself is fine and expected
}

FORBIDDEN_DUNDERS = {"__subclasses__", "__globals__", "__builtins__", "__code__", "__bases__"}


@dataclass
class SafetyReport:
    ok: bool
    violations: list[str]

    def summary(self) -> str:
        return "clean" if self.ok else "; ".join(self.violations)


def _module_allowed(name: str) -> bool:
    if name in ALLOWED_IMPORTS:
        return True
    top = name.split(".")[0]
    if top in FORBIDDEN_IMPORTS:
        return False
    return top in {m.split(".")[0] for m in ALLOWED_IMPORTS}


def check_source(source: str, *, required_defs: tuple[str, ...] = ()) -> SafetyReport:
    """Inspect generated source. Returns every violation found, not just the
    first, so one repair round can fix all of them."""
    violations: list[str] = []
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return SafetyReport(False, [f"SyntaxError line {e.lineno}: {e.msg}"])

    defined: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in FORBIDDEN_IMPORTS:
                    violations.append(f"forbidden import: {alias.name}")
                elif not _module_allowed(alias.name):
                    violations.append(f"import not on the allowlist: {alias.name}")

        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level and node.level > 0:
                violations.append(
                    f"relative import 'from {'.' * node.level}{module} import ...'  - the "
                    f"generated script is standalone and has no package to import from"
                )
            elif module.split(".")[0] in FORBIDDEN_IMPORTS:
                violations.append(f"forbidden import: from {module} import ...")
            elif not _module_allowed(module):
                violations.append(f"import not on the allowlist: from {module} import ...")

        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in FORBIDDEN_CALLS:
                violations.append(f"forbidden call: {func.id}(...)")
            elif isinstance(func, ast.Attribute):
                if func.attr in FORBIDDEN_CALLS and not _is_safe_attr_call(func):
                    violations.append(f"forbidden call: ....{func.attr}(...)")
                if isinstance(func.value, ast.Name) and (func.value.id, func.attr) in FORBIDDEN_ATTRS:
                    violations.append(f"forbidden call: {func.value.id}.{func.attr}(...)")

        elif isinstance(node, ast.Attribute):
            if node.attr in FORBIDDEN_DUNDERS:
                violations.append(f"forbidden attribute access: .{node.attr}")

        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)

    for required in required_defs:
        if required not in defined:
            violations.append(f"required definition missing: {required}")

    # Deduplicate but keep first-seen order, so the feedback message is short.
    seen, ordered = set(), []
    for v in violations:
        if v not in seen:
            seen.add(v)
            ordered.append(v)
    return SafetyReport(not ordered, ordered)


def _is_safe_attr_call(func: ast.Attribute) -> bool:
    """`str.removeprefix`, `list.remove` on a local list, `Path.chmod`  - some
    forbidden NAMES are harmless as methods on ordinary objects. Only allow
    the few that are unambiguous."""
    safe_methods = {"remove"}  # list.remove / set.remove
    if func.attr not in safe_methods:
        return False
    # os.remove / shutil.remove style calls are caught by FORBIDDEN_ATTRS.
    return not (isinstance(func.value, ast.Name) and func.value.id in {"os", "shutil", "pathlib"})
