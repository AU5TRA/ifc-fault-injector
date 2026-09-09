"""
The orchestrator: one IFC file plus one rule in, one validated standalone
script out.

    resolve the rule  ->  generate the harness, one function at a time
         ->  assemble the script  ->  static gate
         ->  run it in a subprocess against the real model
         ->  verify the result independently  ->  hand it over

The retry loop is deliberately narrow, in two senses.

It re-asks the model only when the failure is something the model could
actually fix: a crashed harness, a missing report section, or - for a
synthesized rule - a clause that did not end up violated. When a SAVED rule
simply does not fit the model you pointed it at, no amount of regeneration
helps, so that stops and says so with the verifier's measurements attached.

And it regenerates only the ONE harness function that owns the failure. The
other three have already passed validation; re-rolling them to fix an
unrelated bug is how a working script becomes a broken one.
"""
from __future__ import annotations

import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import ifcopenshell

from . import codegen, synth
from . import harness as harness_mod
from .codegen import RuleCode
from .config import NAME_TAG_TEMPLATE, VIOLATION_PSET, colour_for
from .inventory import model_inventory
from .library import get as get_rule
from .llm import QwenClient
from .safety import check_source
from .validate import ValidationResult, validate

MAX_ROUNDS = 4

#: Rules whose mutation removes elements rather than editing one. The harness
#: needs to know, because a deleted element cannot be coloured - only marked.
DELETION_RULES = ("S4", "S5")


class EmitError(RuntimeError):
    pass


@dataclass
class EmitResult:
    ok: bool
    script_path: Optional[Path] = None
    validation_report_path: Optional[Path] = None
    validation: Optional[ValidationResult] = None
    rule_id: str = ""
    rule_origin: str = ""
    rounds: int = 0
    saved_rule_path: Optional[Path] = None
    message: str = ""
    notes: list[str] = field(default_factory=list)


def default_stem(source_ifc: Path, rule_id: str) -> str:
    building = source_ifc.parent.name or "model"
    return f"{building}_{source_ifc.stem}_{rule_id}".replace(" ", "_")


def emit(*, source_ifc: Path, rule_id: Optional[str] = None,
         new_rule: Optional[dict] = None, outdir: Path = Path("generated"),
         seed: int = 1, no_cache: bool = False, keep_validation_outputs: bool = False,
         timeout_s: Optional[int] = None, log=print) -> EmitResult:
    """Emit one validated script.

    Exactly one of `rule_id` (a rule already in the library) or `new_rule`
    ({"rule_id", "domain", "clause"}) must be given.
    """
    if bool(rule_id) == bool(new_rule):
        raise EmitError("give exactly one of rule_id (a saved rule) or new_rule (a new clause)")
    if not source_ifc.exists():
        raise EmitError(f"source IFC not found: {source_ifc}")

    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    result = EmitResult(ok=False)

    log(f"[1/6] reading {source_ifc.name} ({source_ifc.stat().st_size / (1 << 20):.1f} MB)")
    model = ifcopenshell.open(str(source_ifc))
    inventory = model_inventory(model, source_ifc)
    del model  # a 340MB model held open across several LLM calls is wasteful
    log(f"      schema {inventory['schema']}, units {inventory['length_unit']}, "
        f"{sum(inventory['entity_counts'].values())} elements of interest")

    client = QwenClient(no_cache=no_cache)

    # -- the rule ----------------------------------------------------------
    synthesized: Optional[synth.SynthesizedRule] = None
    if rule_id:
        module = get_rule(rule_id)
        if module is None:
            raise EmitError(f"unknown rule '{rule_id}'. Run `ifcfault rules` to list them, "
                            f"or add --clause and --domain to have a new one written.")
        rule = codegen.extract_rule(module)
        log(f"[2/6] rule {rule.rule_id}: lifted from the library ({rule.origin})")
    else:
        spec = dict(new_rule or {})
        log(f"[2/6] rule {spec['rule_id']}: no saved implementation - asking "
            f"{client.model} to write one")
        synthesized = synth.synthesize(
            client, rule_id=spec["rule_id"], domain=spec["domain"],
            clause=spec["clause"], inventory=inventory, seed=seed,
        )
        log(f"      accepted after {synthesized.attempts} attempt(s)")
        rule = codegen.parse_synthesized_rule(
            synthesized.source, origin=f"synthesized by {synthesized.model}")
        # The model is asked for these but may drift; the request wins.
        rule.rule_id = spec["rule_id"]
        rule.clause = spec.get("clause") or rule.clause
        rule.domain = spec.get("domain") or rule.domain

    result.rule_id = rule.rule_id
    result.rule_origin = rule.origin
    stem = default_stem(source_ifc, rule.rule_id)
    name_tag = NAME_TAG_TEMPLATE.format(rule_id=rule.rule_id)
    colour = colour_for(rule.rule_id)

    shared = {
        "rule_id": rule.rule_id,
        "rule_clause": rule.clause,
        "rule_domain": rule.domain,
        "rule_element": rule.element,
        "rule_origin": rule.origin,
        "output_stem": stem,
        "colour": {"name": colour.name, "hex": colour.hex, "rgb": list(colour.rgb)},
        "name_tag_prefix": name_tag,
        "violation_pset": VIOLATION_PSET,
        "target_is_a_storey": rule.rule_id == "S5",
        "rule_deletes_elements": rule.rule_id in DELETION_RULES,
        "model_inventory": inventory,
    }

    # -- the harness, one function at a time -------------------------------
    log(f"[3/6] asking {client.model} for the harness, one function at a time")
    _, parts = harness_mod.generate_harness(client, shared, seed=seed, log=log)

    script_path = outdir / f"{stem}_inject.py"
    validation: Optional[ValidationResult] = None
    work_root = Path(tempfile.mkdtemp(prefix="ifcfault_validate_"))

    try:
        for round_no in range(1, MAX_ROUNDS + 1):
            result.rounds = round_no
            log(f"[4/6] assembling the script (round {round_no})")
            script = codegen.assemble(
                rule=rule, harness_source=harness_mod.assemble_parts(parts),
                source_ifc=source_ifc, output_stem=stem, model_name=client.model,
                harness_origin=f"round {round_no}",
            )
            candidate = work_root / f"candidate_{round_no}.py"
            candidate.write_text(script, encoding="utf-8")

            # Belt and braces: the assembled whole gets the static gate too,
            # not just each fragment the model returned.
            whole = check_source(script, required_defs=(
                "main", "applicable", "candidates", "apply_violation"))
            if not whole.ok:
                log(f"      assembled script rejected statically: {whole.summary()}")
                if round_no == MAX_ROUNDS:
                    break
                parts["main"] = harness_mod.regenerate_part(
                    client, "main", shared, parts["main"], whole.summary(), seed)
                continue

            log(f"[5/6] running it against the real model in a subprocess "
                f"(parsing {source_ifc.name} takes a while)")
            started = time.time()
            validation = validate(
                candidate, source_ifc, work_root / f"run_{round_no}",
                rule_id=rule.rule_id, output_stem=stem, name_tag=name_tag,
                pset_name=VIOLATION_PSET,
                rule_is_synthesized=synthesized is not None,
                **({"timeout_s": timeout_s} if timeout_s else {}),
            )
            log(f"      {'PASSED' if validation.ok else 'FAILED'} in "
                f"{time.time() - started:.0f}s ({len(validation.checks)} checks)")
            for check in validation.checks:
                if not check.passed:
                    log(f"        FAIL {check.name}: {check.message or check.evidence}")

            if validation.ok:
                break
            if not validation.retryable:
                log(f"      fault class '{validation.fault}' is not something regeneration "
                    f"can fix - stopping here")
                break
            if round_no == MAX_ROUNDS:
                break

            if validation.fault == "harness":
                part = harness_mod.choose_part_to_repair(validation)
                log(f"      repairing {part}() with the concrete failure")
                parts[part] = harness_mod.regenerate_part(
                    client, part, shared, parts[part], validation.feedback, seed)
            elif synthesized is not None:
                log("      repairing the synthesized rule with the concrete failure")
                synthesized.source = synth.repair(
                    client, rule.rule_id, synthesized.source, validation.feedback, seed)
                repaired = codegen.parse_synthesized_rule(synthesized.source, rule.origin)
                repaired.rule_id, repaired.clause, repaired.domain = (
                    rule.rule_id, rule.clause, rule.domain)
                rule = repaired

        # -- hand over -----------------------------------------------------
        result.validation = validation
        script_path.write_text(
            codegen.assemble(
                rule=rule, harness_source=harness_mod.assemble_parts(parts),
                source_ifc=source_ifc, output_stem=stem, model_name=client.model,
                harness_origin=("validated" if (validation and validation.ok)
                                else "DID NOT PASS VALIDATION"),
            ),
            encoding="utf-8",
        )
        result.script_path = script_path

        if validation is None:
            result.message = "no validation run completed"
            return result

        report_path = outdir / f"{stem}_emission_validation.txt"
        report_path.write_text(
            _validation_document(validation, script_path, source_ifc, rule,
                                 client.model, result.rounds, parts),
            encoding="utf-8")
        result.validation_report_path = report_path

        if keep_validation_outputs and validation.outputs:
            kept = outdir / f"{stem}_validation_run"
            kept.mkdir(parents=True, exist_ok=True)
            for path in validation.outputs.values():
                produced = Path(path)
                if produced.exists() and produced.suffix in (".txt", ".json"):
                    shutil.copy2(produced, kept / produced.name)
            result.notes.append(f"validation run artefacts kept in {kept}")

        if validation.ok and synthesized is not None:
            saved = synth.save_generated(synthesized, rule.clause)
            result.saved_rule_path = saved
            shown = saved
            try:
                shown = saved.relative_to(Path.cwd())
            except ValueError:
                pass
            result.notes.append(
                f"synthesized rule saved to {shown} - review it before relying on it")

        result.ok = validation.ok
        result.message = ("validated" if validation.ok
                          else f"validation failed ({validation.fault})")
        log(f"[6/6] {'wrote' if validation.ok else 'wrote (UNVALIDATED)'} {script_path}")
        return result

    finally:
        shutil.rmtree(work_root, ignore_errors=True)


def _validation_document(validation: ValidationResult, script_path: Path, source_ifc: Path,
                         rule: RuleCode, model_name: str, rounds: int,
                         parts: dict) -> str:
    bar = "=" * 78
    part_summary = ", ".join(f"{name}() {len(src.splitlines())}L"
                             for name, src in parts.items())
    lines = [
        bar,
        "EMISSION-TIME VALIDATION REPORT",
        bar,
        "",
        "What was checked BEFORE this script was handed over. This is not the report",
        "the script itself writes when you run it - that one describes the violation;",
        "this one records whether the script can be trusted to produce it.",
        "",
        f"  script          : {script_path.name}",
        f"  source model    : {source_ifc}",
        f"  rule            : {rule.rule_id} - {rule.clause}",
        f"  rule provenance : {rule.origin}",
        f"  harness author  : {model_name}",
        f"  harness parts   : {part_summary}",
        f"  rounds needed   : {rounds}",
        f"  validated at    : {time.strftime('%Y-%m-%dT%H:%M:%S')}",
        "",
        bar,
        "",
        validation.text_report(),
        "",
        bar,
        "HOW TO READ A FAILURE",
        bar,
        "",
        "  harness      the generated harness is at fault - it crashed, wrote nothing,",
        "               or left a required section out of its report. Regenerating the",
        "               one function that owns the failure can fix this, and the tool",
        "               retries automatically.",
        "",
        "  rule         the harness worked, but the rule and this model did not agree:",
        "               the clause did not end up violated, or more of the file changed",
        "               than the mutation record declared. For a SAVED rule that means",
        "               the rule does not suit this particular model - pick a different",
        "               model or a different rule. It is reported rather than papered",
        "               over precisely because a passing-but-wrong test case is worse",
        "               than no test case at all.",
        "",
        "  environment  the rule reports itself inapplicable to this model (no element",
        "               of the right kind, no usable geometry). Nothing is wrong with",
        "               the code; run `ifcfault survey --source <model>` to see which",
        "               rules do apply.",
        "",
    ]
    if validation.stdout.strip():
        lines += [bar, "SCRIPT STDOUT", bar, "", validation.stdout.strip(), ""]
    if validation.stderr.strip():
        lines += [bar, "SCRIPT STDERR", bar, "", validation.stderr.strip()[-4000:], ""]
    return "\n".join(lines)
