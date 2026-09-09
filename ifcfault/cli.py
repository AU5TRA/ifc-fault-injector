"""
Command line entry point.

    python -m ifcfault rules
    python -m ifcfault emit    --source <model.ifc> --rule A1
    python -m ifcfault emit    --source <model.ifc> --rule A6 \
                               --domain architectural --clause "IBC 1011.2: ..."
    python -m ifcfault survey  --source <model.ifc>

argparse rather than a CLI framework: one real command, and the fewer
dependencies the emitted scripts' provenance rests on, the better.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__


def _cmd_rules(args) -> int:
    from .library import describe_all

    rules = describe_all()
    width = max(len(r["rule_id"]) for r in rules)
    print(f"{len(rules)} rule(s) available:\n")
    for rule in rules:
        tag = "" if rule["source"] == "built-in" else "  [generated]"
        print(f"  {rule['rule_id']:<{width}}  {rule['domain']:<14} {rule['element']:<20}{tag}")
        print(f"  {'':<{width}}  {rule['clause']}")
        print()
    print("Pass one of these to `emit --rule`. For a clause that is not here, pass")
    print("--rule with a new id plus --domain and --clause, and it will be synthesized.")
    return 0


def _cmd_survey(args) -> int:
    """Which saved rules apply to this model, and why not, for the rest."""
    import json

    import ifcopenshell

    from .inventory import model_inventory
    from .library import registry

    source = Path(args.source)
    if not source.exists():
        print(f"error: no such file: {source}", file=sys.stderr)
        return 2

    model = ifcopenshell.open(str(source))
    report = {
        "source": str(source),
        "inventory": model_inventory(model, source),
        "rules": {},
    }
    for rule_id, module in sorted(registry().items()):
        try:
            applicability = module.applicable(model)
            entry = {"applicable": applicability.ok, "reason": applicability.reason}
            if applicability.ok:
                targets = module.candidates(model)
                entry["candidate_count"] = len(targets)
                if targets:
                    best = sorted(targets, key=lambda t: (-t.score, t.global_id))[0]
                    entry["best_target"] = {
                        "global_id": best.global_id,
                        "justification": best.justification,
                    }
        except Exception as e:
            entry = {"applicable": False, "reason": f"query raised {type(e).__name__}: {e}"}
        report["rules"][rule_id] = entry

    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return 0

    inventory = report["inventory"]
    print(f"{source.name}   - {inventory['schema']}, units {inventory['length_unit']}, "
          f"{inventory['storey_count']} storey(s)")
    print()
    for rule_id, entry in report["rules"].items():
        if entry["applicable"]:
            print(f"  {rule_id}  APPLIES     {entry.get('candidate_count', 0)} candidate(s)")
            best = entry.get("best_target")
            if best:
                print(f"          would target {best['global_id']}")
                print(f"          {best['justification']}")
        else:
            print(f"  {rule_id}  no          {entry['reason']}")
        print()
    return 0


def _cmd_emit(args) -> int:
    from .emit import EmitError, emit
    from .llm import LLMError
    from .synth import SynthesisError

    source = Path(args.source)
    new_rule = None
    rule_id = args.rule

    if args.clause:
        if not args.domain:
            print("error: --clause needs --domain (architectural or structural)",
                  file=sys.stderr)
            return 2
        new_rule = {"rule_id": args.rule, "domain": args.domain, "clause": args.clause}
        rule_id = None
    elif args.domain:
        print("error: --domain only means something together with --clause", file=sys.stderr)
        return 2

    try:
        result = emit(
            source_ifc=source, rule_id=rule_id, new_rule=new_rule,
            outdir=Path(args.outdir), seed=args.seed, no_cache=args.no_cache,
            keep_validation_outputs=args.keep_validation_outputs,
            timeout_s=args.timeout,
        )
    except (EmitError, SynthesisError, LLMError) as e:
        print(f"\nerror: {e}", file=sys.stderr)
        return 1

    print()
    if result.ok:
        print("Done. Run the script yourself to produce the faulty model:")
        print()
        print(f"  python {result.script_path} --outdir out")
        print()
        print("It writes four files:")
        print(f"  <stem>.ifc          faulty, UNMARKED  - feed this to a compliance checker")
        print(f"  <stem>_colored.ifc  faulty, marked up  - open this in Revit or a viewer")
        print(f"  <stem>_report.txt   what was changed, where, and how to find it")
        print(f"  <stem>_record.json  the same facts, machine-readable")
    else:
        print(f"The script was written but did NOT pass validation ({result.message}).")
        print("It is on disk so you can read it, but do not trust its output yet.")
    print()
    print(f"  script     : {result.script_path}")
    print(f"  validation : {result.validation_report_path}")
    for note in result.notes:
        print(f"  note       : {note}")
    return 0 if result.ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ifcfault",
        description="Generate a runnable script that injects one building-code violation "
                    "into a real IFC model.",
    )
    parser.add_argument("--version", action="version", version=f"ifcfault {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    rules = subparsers.add_parser("rules", help="list the rules available")
    rules.set_defaults(func=_cmd_rules)

    survey = subparsers.add_parser(
        "survey", help="which rules apply to a model, and why not, for the rest")
    survey.add_argument("--source", required=True, help="path to an .ifc file")
    survey.add_argument("--json", action="store_true", help="machine-readable output")
    survey.set_defaults(func=_cmd_survey)

    emit_parser = subparsers.add_parser(
        "emit", help="generate a validated injection script for one model and one rule")
    emit_parser.add_argument("--source", required=True, help="path to the real .ifc model")
    emit_parser.add_argument("--rule", required=True,
                             help="a saved rule id (see `rules`), or a NEW id to synthesize "
                                  "when combined with --clause")
    emit_parser.add_argument("--clause",
                             help="plain-language code clause; use this for a rule that does "
                                  "not exist yet, and the model will write it")
    emit_parser.add_argument("--domain", choices=("architectural", "structural"),
                             help="required with --clause")
    emit_parser.add_argument("--outdir", default="generated",
                             help="where to write the script (default: generated/)")
    emit_parser.add_argument("--seed", type=int, default=1,
                             help="LLM seed; changing it asks for a different harness")
    emit_parser.add_argument("--no-cache", action="store_true",
                             help="bypass the LLM response cache (costs real API calls)")
    emit_parser.add_argument("--keep-validation-outputs", action="store_true",
                             help="keep the report/record the validation run produced")
    emit_parser.add_argument("--timeout", type=int, default=None,
                             help="seconds to allow the validation run (default 1800)")
    emit_parser.set_defaults(func=_cmd_emit)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
