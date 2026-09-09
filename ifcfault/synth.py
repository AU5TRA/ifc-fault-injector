"""
Synthesizing a rule for a clause nobody has implemented.

This is the riskiest thing the tool does, and it is gated differently from
harness generation. A harness writes files; a rule EDITS a building model,
and a subtly wrong edit produces a test case that looks fine and quietly
tests the wrong thing. So:

  * the static gate is stricter  - no file I/O at all, no `open`, and the
    three contract functions must all be present
  * the code is never executed here. It reaches a subprocess only as part of
    a fully assembled script, in `emit.py`
  * a synthesized rule that passes is written to `library/generated/` so the
    same clause never has to be synthesized twice  - but it is NOT thereby
    trusted. It is reachable next time because a human left the file there.

The prompt is in `prompts/new_rule.md`, and the requirements it imposes
(target something currently compliant, derive the injected value from the
element, be deterministic) exist because a 32B model reliably gets those
three wrong on the first attempt.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .config import REPO_ROOT
from .llm import QwenClient, render_prompt
from .safety import check_source

GENERATED_DIR = REPO_ROOT / "ifcfault" / "library" / "generated"

MAX_ATTEMPTS = 4

REQUIRED_RULE_DEFS = ("applicable", "candidates", "apply_violation")

# Anything a rule must never do, on top of the shared safety gate. A rule
# only reads and edits the in-memory model; it has no business writing files.
RULE_FORBIDDEN_IMPORTS = {"argparse", "os", "sys", "json", "pathlib", "time", "hashlib"}


class SynthesisError(RuntimeError):
    """Raised when the model cannot produce acceptable rule code. Escalates
    to the human rather than shipping something unchecked."""


@dataclass
class SynthesizedRule:
    rule_id: str
    source: str
    attempts: int
    model: str
    saved_to: Optional[str] = None


def extract_code_block(text: str) -> str:
    match = re.search(r"```python\s*\n(.*?)```", text, re.DOTALL)
    if match is None:
        match = re.search(r"```\s*\n(.*?)```", text, re.DOTALL)
    if match is None:
        raise ValueError("no fenced code block in the response")
    return match.group(1).strip()


def check_rule_source(source: str) -> list[str]:
    """The shared static gate, plus the rule-specific bans."""
    report = check_source(source, required_defs=REQUIRED_RULE_DEFS)
    violations = list(report.violations)

    for name in RULE_FORBIDDEN_IMPORTS:
        if re.search(rf"^\s*import\s+{name}\b", source, re.MULTILINE) or \
           re.search(rf"^\s*from\s+{name}\b", source, re.MULTILINE):
            violations.append(
                f"a rule must not import {name}  - it only reads and edits the in-memory "
                f"model; the harness owns all file and argument handling"
            )

    for metadata in ("RULE_ID", "CLAUSE", "DOMAIN"):
        if not re.search(rf"^{metadata}\s*=", source, re.MULTILINE):
            violations.append(f"missing required module constant {metadata}")

    if "random" in source:
        violations.append("`random` breaks reproducibility  - the target must be chosen "
                          "by a deterministic score")
    if "uuid4" in source:
        violations.append("`uuid4` breaks reproducibility  - new entities must get "
                          "hash-derived GlobalIds")

    seen, ordered = set(), []
    for v in violations:
        if v not in seen:
            seen.add(v)
            ordered.append(v)
    return ordered


def synthesize(client: QwenClient, *, rule_id: str, domain: str, clause: str,
               inventory: dict, seed: int = 1) -> SynthesizedRule:
    """Ask the model for rule code and keep asking until it passes the static
    gate, or give up and escalate."""
    payload = {
        "rule_id": rule_id,
        "domain": domain,
        "clause": clause,
        "model_inventory": inventory,
    }
    messages = [{"role": "user", "content": render_prompt("new_rule.md", payload)}]

    last_error = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        raw = client.chat(messages, purpose=f"synthesize-rule:{rule_id}:attempt{attempt}",
                          seed=seed, max_tokens=3000)
        try:
            code = extract_code_block(raw)
        except ValueError as e:
            last_error = str(e)
            messages += [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": f"Invalid: {last_error}. Respond with ONE fenced "
                                            f"python code block and nothing else."},
            ]
            continue

        violations = check_rule_source(code)
        if not violations:
            return SynthesizedRule(rule_id=rule_id, source=code, attempts=attempt,
                                   model=client.model)

        last_error = "; ".join(violations)
        messages += [
            {"role": "assistant", "content": raw},
            {"role": "user", "content": f"Rejected by the static safety gate: {last_error}\n"
                                        f"Fix every point and resend the complete module as "
                                        f"one fenced python code block."},
        ]

    raise SynthesisError(
        f"could not get acceptable code for rule {rule_id} in {MAX_ATTEMPTS} attempts. "
        f"Last rejection: {last_error}"
    )


def repair(client: QwenClient, rule_id: str, previous_source: str, failure: str,
           seed: int = 1) -> str:
    """One more round after the assembled script failed against the real model.

    The feedback is the verifier's own words  - a concrete measured failure
    ("L/d 19.9 does not exceed 24"), not a vague "it didn't work".
    """
    messages = [
        {"role": "user", "content": "Here is the rule module you wrote:\n\n"
                                    f"```python\n{previous_source}\n```"},
        {"role": "user", "content":
            "Run against the real model, it produced a file that FAILED independent "
            f"verification:\n\n{failure}\n\n"
            "The most common causes are: the injected value did not actually clear the "
            "clause threshold for this element (derive it from the element's own "
            "dimensions instead of using a constant), the target was already "
            "non-compliant before the injection, or the mutation changed more of the "
            "file than its Mutation.extra declared.\n\n"
            "Fix it and resend the complete module as one fenced python code block."},
    ]
    raw = client.chat(messages, purpose=f"repair-rule:{rule_id}", seed=seed, max_tokens=3000)
    code = extract_code_block(raw)
    violations = check_rule_source(code)
    if violations:
        raise SynthesisError(f"the repaired rule failed the static gate: {'; '.join(violations)}")
    return code


def library_imports_for(source: str) -> str:
    """The import block a synthesized rule needs to work as a library module.

    Inside a generated script the rule needs no imports at all: the closure
    inlines every helper it references right above it. Saved back into the
    library it is an ordinary module again, so the same references have to
    become real imports - otherwise the file looks fine, sits in the
    registry directory, and raises NameError the moment anything loads it.
    """
    import ast

    from .codegen import HELPER_MODULES, _absolute_imports, _names_in, build_index

    tree = ast.parse(source)
    lines = source.splitlines()
    index, _ = build_index()

    referenced: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        referenced |= _names_in(node)

    by_module: dict[str, list[str]] = {}
    for name in sorted(referenced):
        symbol = index.get(name)
        if symbol is not None:
            by_module.setdefault(symbol.module, []).append(name)

    out = ["from __future__ import annotations", ""]
    out.extend(_absolute_imports(tree, lines))
    if out[-1] != "":
        out.append("")
    for module_name in HELPER_MODULES:
        names = by_module.get(module_name)
        if names:
            # Two dots: a saved rule lives in library/generated/, so the
            # helper modules are one package up.
            out.append(f"from ..{module_name} import {', '.join(sorted(set(names)))}")
    return "\n".join(out)


def save_generated(rule: SynthesizedRule, clause: str) -> Path:
    """Keep a validated synthesized rule so the clause never needs
    synthesizing again. Reachable next run only because the file is left
    here  - being generated is not what makes it trusted."""
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = GENERATED_DIR / f"{rule.rule_id.lower()}_{stamp}.py"

    header = '\n'.join([
        '"""',
        f"{rule.rule_id}  - SYNTHESIZED RULE, NOT HUMAN-REVIEWED.",
        "",
        f"Written by {rule.model} for this clause:",
        f"  {clause}",
        "",
        f"Accepted after {rule.attempts} attempt(s): it passed the static safety gate, ran",
        "against a real model in a subprocess, and the file it produced passed independent",
        "verification.",
        "",
        "It is in this directory so the same clause does not have to be synthesized again.",
        "Read it before relying on it  - none of the above is a substitute for a human",
        "having checked that it implements the clause it claims to.",
        '"""',
    ])
    body = header + "\n" + library_imports_for(rule.source) + "\n\n\n" + rule.source + "\n"

    # If it cannot even be imported it does not belong in the registry
    # directory, where the next run will try to load it.
    import ast

    try:
        ast.parse(body)
    except SyntaxError as e:
        raise SynthesisError(
            f"refusing to save rule {rule.rule_id}: the assembled module does not parse "
            f"({e.msg} at line {e.lineno})"
        ) from e

    path.write_text(body, encoding="utf-8")
    return path
