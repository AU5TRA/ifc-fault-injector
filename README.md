# ifcfault

Give it one real IFC building model and one or more building-code rules. It
gives you back **a standalone Python script** that you run yourself to
produce the faulty model.

```
python -m ifcfault emit --source "D:\Real World BIMs\models\dental_clinic\arc.ifc" --rule A1
    -> generated/dental_clinic_arc_A1_inject.py

python generated/dental_clinic_arc_A1_inject.py --outdir out
    -> out/dental_clinic_arc_A1.ifc            faulty, UNMARKED
    -> out/dental_clinic_arc_A1_report.txt     what changed, where, how to find it
    -> out/dental_clinic_arc_A1_record.json    the same facts, machine-readable

python generated/dental_clinic_arc_A1_inject.py --outdir out --colored
    -> out/dental_clinic_arc_A1_colored.ifc    the same fault, marked up for a viewer
    -> (report and record, rewritten to describe this run)
```

Several faults in one file, if you want them — see
[Injecting several faults at once](#injecting-several-faults-at-once):

```
python -m ifcfault emit --source model.ifc --rule A1,A3,S5    # three different rules
python -m ifcfault emit --source model.ifc --rule A1:3        # three different doors
```

The point of the tool is testing automated compliance checkers. You cannot
tell whether a checker works without files whose defects you know exactly,
and hand-authoring those against real 300MB building models is not viable.

## Why a script rather than just the file

The script *is* the deliverable. It is one file with no dependency beyond
`ifcopenshell`, it reads top to bottom, and it says in its own header what
wrote each part of it. You can put it in an appendix, hand it to a reviewer,
re-run it on a different model, or edit the threshold and re-run. A bare
output IFC would tell you none of that.

## One IFC per run, and you choose which

A run writes exactly one IFC. Marking is **off by default**, because the
unmarked file is the one the tool exists to produce.

| Invocation | Writes | For |
|---|---|---|
| nothing, or `--no-color` | `<stem>.ifc` | The checker under test. **No visual marking at all** — colouring it would hand the checker the answer. |
| `--colored` | `<stem>_colored.ifc` | A human. The same fault, marked up three ways. Open it in Revit or an IFC viewer. |

The two flags share one argparse destination, so they are true opposites and
the last one given wins.

Both runs pick the same target and make the same edit — selection is
deterministic — so the two files carry the identical fault and differ only in
whether it is signposted. The `.txt` report and `.json` record are rewritten
each run and describe the file **that** run produced; the record carries a
top-level `"colored"` key, so a file found on disk later can be told apart
from its twin.

Never hand the coloured file to a checker you are evaluating. That is the
whole reason the default is the plain one.

## Finding the fault in a viewer

All of this applies to the `--colored` run. Marking is deliberately
redundant, because no single handle survives every viewer:

| Handle | What it is | Where it works |
|---|---|---|
| Colour | `IfcStyledItem` -> `IfcSurfaceStyle`, one fixed colour per rule | BIMvision, Solibri, FZKViewer, usBIM, BlenderBIM |
| Name tag | element renamed `[!A1 VIOLATION!] <original name>` | everywhere, including Revit search and schedules |
| Marker box | a coloured `IfcBuildingElementProxy` cube at the fault | everywhere; Revit imports it as a Generic Model |
| Property set | `Pset_ViolationMarker` with rule, clause and colour | Revit surfaces it as element parameters |

**Revit specifically:** its IFC importer frequently discards presentation
styles, so the element may not come in coloured. Search the name tag or look
for the marker box. The report says this too.

A rule that *deletes* elements (S4, S5) has nothing left to colour, so the
marker box is the only handle - which is why it is always added.

Colours are fixed per rule: A1 red, A2 orange, A3 yellow, A4 green, A5 blue,
S1 purple, S2 cyan, S3 magenta, S4 brown, S5 maroon. Every report ends with
the full legend.

## The rules

```
python -m ifcfault rules                          # what is available
python -m ifcfault survey --source model.ifc      # which of them fit THIS model, and why not
```

| Rule | Clause | Mechanism |
|---|---|---|
| A1 | IBC 1010.1.1, egress door width >= 815mm | write `IfcDoor.OverallWidth` |
| A2 | IBC 1011.5.2, stair riser <= 178mm | overwrite `RiserHeight` |
| A3 | IBC Ch.7 / Table 716.1(2), fire rating | downgrade or blank `FireRating` |
| A4 | ANSI A117.1 404.2.4, manoeuvring clearance | move the opening along its host wall |
| A5 | IBC 1020.4, dead-end corridor | delete one `IfcRelSpaceBoundary` |
| S1 | EC2 7.4.2 / ACI 318 9.3.1.1, span/depth | private cross-section resize |
| S2 | EC8 5.4.1.2.1, column min dimension | private cross-section resize |
| S3 | ACI 318 Table 7.3.1.1, slab thickness | private extrusion depth change |
| S4 | ASCE 7 Table 12.3-2, floating column | delete the supporting column(s) |
| S5 | ASCE 7 / EC8 4.2.3.3, soft storey | delete every wall on one storey |

Two properties every rule holds to, because they are what make a test case
worth having:

- **The target must currently COMPLY.** Making an already-broken element worse
  is not an injected defect; a checker would have flagged the untouched file
  too.
- **The injected value is derived from the element, not a constant.** A fixed
  400mm beam depth violates a span/depth limit over an 11m span and satisfies
  it over an 8m one. The rules solve for a value that clears the threshold by
  a clear margin whatever the element's dimensions.

## Injecting several faults at once

One script can carry more than one violation. Pass several rule ids to
`--rule` and they all go into a single emitted script, injected in the order
you wrote them.

```
python -m ifcfault emit --source model.ifc --rule A1,S1
    -> generated/<building>_<model>_A1_S1_inject.py
```

Three ways to write a plan, and they mix freely:

| Form | Means |
|---|---|
| `--rule A1` | one fault (exactly as before) |
| `--rule A1,S1,A3` | three faults, one of each, in that order |
| `--rule A1:3` | three faults of the **same** rule, on three **different** doors |
| `--rule A1 --rule S1` | the flag repeats, same as `--rule A1,S1` |
| `--rule A1:2,S5` | two A1 faults then one S5 — mixed |

Ids are case-insensitive, so `--rule a1,s5` works.

### Example commands

```
# One architectural + one structural fault
python -m ifcfault emit --source "D:\Real World BIMs\models\dental_clinic\arc.ifc" \
  --rule A1,A3

# Five faults across both domains, in this order
python -m ifcfault emit --source model.ifc --rule A1,A2,A5,S1,S3

# Three narrow doors, three different doors
python -m ifcfault emit --source model.ifc --rule A1:3

# Two narrow doors, one bad fire rating, one soft storey
python -m ifcfault emit --source model.ifc --rule A1:2,A3,S5

# Every built-in rule at once, and check the marking too
python -m ifcfault emit --source model.ifc \
  --rule A1,A2,A3,A4,A5,S1,S2,S3,S4,S5 --validate-colored
```

Then run the script exactly as you would a single-fault one:

```
python generated/dental_clinic_arc_A1_A3_inject.py --outdir out
    -> out/dental_clinic_arc_A1_A3.ifc            BOTH faults, unmarked
    -> out/dental_clinic_arc_A1_A3_report.txt     one section per fault
    -> out/dental_clinic_arc_A1_A3_record.json    a `mutations` array

python generated/dental_clinic_arc_A1_A3_inject.py --outdir out --colored
    -> out/dental_clinic_arc_A1_A3_colored.ifc    A1 in red, A3 in yellow
```

### Output names

The plan names the file. A rule used once appears as its id; a rule used
several times gets a count, so a name never repeats itself:

| Plan | Output stem |
|---|---|
| `A1` | `..._A1` |
| `A1,S1` | `..._A1_S1` |
| `A1:3` | `..._A1x3` |
| `A1:2,S1` | `..._A1x2_S1` |

A single-rule emission produces byte-for-byte the same paths it always has,
so nothing that already points at `..._A1.ifc` breaks.

### Repeats land on different elements

`--rule A1:3` narrows three different doors, not the same door three times.
Every rule's `candidates()` takes an `exclude` set, and `main()` threads a
growing one through the plan: fault 2 cannot choose what fault 1 already
took, or anything fault 1 deleted.

This matters because a rule's own selection band usually still contains the
element it just broke. A1 picks doors between 700mm and 1100mm and writes
750mm — without the exclusion set, all three faults would re-select the same
highest-scoring door and the script would claim three defects while
injecting one.

### Order is part of the recipe

Faults are applied one after another, each to the model the previous one
left behind. `--rule S5,A1` and `--rule A1,S5` are different plans and may
pick different targets — S5 empties a storey, so an A1 that runs after it
chooses from the doors that are left.

### All or nothing

If any fault in the plan cannot be injected — the rule is inapplicable to
this model, or there is no candidate left outside the exclusion set — the
script **writes nothing and exits 2**. A file named for four faults that
quietly contains three is a wrong test case, not a partial one, and a
checker evaluated against it would be scored against the wrong answer key.

Run `survey` first to see what a model can actually support:

```
python -m ifcfault survey --source model.ifc
```

### Telling the faults apart

Each fault keeps its own rule's colour, so `--colored` output stays readable
with several faults in one file: A1 red, A3 yellow, S5 maroon. The report
prints one block per fault, headed like

```
--- FAULT 2 of 4: A1#2 (RED #E6194B) ---
```

and the record's `mutations` array is in injection order, each entry
carrying its own `slot`, `label`, `rule_id`, `target` and `mutation`.

### One rule per file, several rules per script

Rules are inlined **once each**, however many times the plan uses them, with
their top-level names suffixed by rule id — `candidates_A1`, `candidates_S1`.
That suffix is the only change made to the library source; the bodies are the
reviewed originals, comments intact.

It is needed because every rule defines the same three contract functions,
and several share constants too — `THRESHOLD_MM` is defined by four rules
and `_has_swept_profile` by two. Without namespacing the last definition in
the file would silently win.

### A new clause cannot go straight into a plan

`--clause`/`--domain` synthesize **one** new rule and need the model's full
attention on that clause, so they cannot be combined with a multi-rule plan.
Synthesize it on its own first — which saves it to
`ifcfault/library/generated/` — and then use its id in any plan:

```
python -m ifcfault emit --source model.ifc --rule A6 --domain architectural \
  --clause "IBC 1010.1.1.1 - clear opening height ... not less than 2032 mm"

python -m ifcfault emit --source model.ifc --rule A1,A6,S1
```

### What validation checks for a plan

Everything it checks for one fault, per fault, plus two things that only
exist for a plan:

- **`plan_delivered`** — the record declares exactly the faults that were
  asked for, in order. This is what makes all-or-nothing enforceable rather
  than aspirational.
- **the diff is the union** — `check_no_unintended_diff` is given the union
  of what every fault declares, and stays exactly as strict as it is for one:
  a GlobalId no fault declared is still an unexplained change.

Clause re-derivation runs **per fault**, independently, with the failing
fault named in the check (`a1_clause_violated [A1#2]`). One fault passing
says nothing about the next.

The cap is 25 faults per plan. That is not a technical limit — it is there so
`--rule A1:500` fails loudly instead of running for an hour.

## A clause that has no rule yet

Pass a new id with `--clause` and `--domain`, and the model writes the rule:

```
python -m ifcfault emit --source model.ifc --rule A6 --domain architectural \
  --clause "IBC 1010.1.1.1 - clear opening height of an egress door shall be not less than 2032 mm"
```

If it passes validation the rule is saved to `ifcfault/library/generated/`, so
the same clause never has to be synthesized twice. It is reachable next run
because a human left the file there - being generated is not what makes it
trusted. Read it before relying on it.

## What the model writes, and what it does not

| Part | Author |
|---|---|
| Harness: `main()`, argparse, the colouring calls, the whole report layout | **generated by the model**, one function at a time |
| A known rule's `applicable`/`candidates`/`apply_violation` | **lifted verbatim from the library** by AST source extraction, comments intact |
| A new clause's rule | **generated by the model**, then saved |
| A multi-fault plan's loop: `main()` walking FAULTS, threading the exclusion set | **generated by the model**, from its own multi-fault prompt |
| Namespacing a rule's symbols (`candidates_A1`) so several share a file | deterministic AST rewrite, not the model |
| IFC primitives: styled items, marker geometry, deterministic GUIDs, deletion cascade | the reviewed helper library |

The model writes policy and flow. The library provides IFC mechanics. Only
the helpers a script actually references are inlined - an A1 script carries
five short functions, an A5 script carries the space-adjacency graph builder.

The harness is generated as **four separate functions**
(`_mark_violation`, `_report_top`, `_report_bottom`, `main`), each requested
and validated on its own. A 32B code model asked for the whole thing in one
go produces something that looks right and quietly drops a step; asked for
one function with one job and a named return shape, it is reliable. When a
round fails, only the function the traceback blames is regenerated - the
other three have already passed.

## Nothing ships unvalidated

1. **Static gate** (`safety.py`): import allowlist, no `eval`/`exec`/
   `subprocess`/`shutil`/network/`os.remove`, required definitions present.
   Runs before anything is executed anywhere.
2. **Subprocess execution** against your real model, with a timeout. Never
   imported into the generator's own process.
3. **Independent verification** (`verify/`), which imports nothing from the
   rule library and re-derives every quantity itself - the unit scale, the
   cross-section, the storey ordering, the space graph - deliberately using
   the opposite unit convention. If both agree, they agreed twice by two
   routes. There is a test that fails if that independence is ever broken.

It checks that the file parses, has no dangling references, has no
relationship left with an empty member list (an EXPRESS `SET [1:?]`
violation that `ifcopenshell.remove()` leaves behind), differs from the source
in *exactly* the ways the mutation record declares, and that the clause really
is violated. Plus that the prose report does not contradict the JSON record.

**A pass checks one run in one mode**, because the script writes one IFC per
run — it verifies the file that run actually produced and assumes nothing
about a file it did not see. The plain (`--no-color`) pass is the one that
carries the guarantees above, and is always made.

`--validate-colored` adds a *second* execution, with `--colored`, that checks
the marking is genuinely findable — the colour, the name tag, the marker box.
It is off by default because it costs another full parse of the source model,
which on a 340MB file is minutes, and because the unmarked file is the
deliverable. Turn it on when you intend to hand the coloured file to a human.
The verdict is the AND of the two passes: marking that cannot be found is a
failure even when the file you would give a checker is perfect.

The strict "nothing else changed" diff runs only in the plain pass. Marking
deliberately adds styled items, a marker proxy and a property set, so running
that check against a marked file would mean loosening it — and a loosened
diff proves nothing. What the plain pass proves about the edit holds for the
marked file too, since both runs make the identical edit.

Either mode also fails if the script writes the *other* mode's file: a
`main()` that ignores the flag and writes both would otherwise pass every
check while quietly handing a compliance checker a coloured model.

Failures are classified, because the right response differs:

- **harness** - the generated code is broken. Regenerate the one function at
  fault. Automatic.
- **rule** - the harness worked but the rule and this model do not fit: the
  clause did not end up violated, or more of the file changed than was
  declared. For a *saved* rule, regeneration cannot help, so it stops and
  hands you the verifier's measurements. A passing-but-wrong test case is
  worse than no test case.
- **environment** - the rule reports itself inapplicable to this model. Run
  `survey` to see what does fit.

The `<stem>_emission_validation.txt` written next to each script records all
of this.

## Determinism

Running an emitted script twice produces byte-identical output. Targets are
chosen by a ranked score with ties broken on GlobalId, so nothing depends on
iteration order; every entity the marking creates gets a hash-derived
GlobalId rather than a random one; and LLM calls are made at temperature 0
and cached on disk by `hash(model, messages, params)`.

## Setup

```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env      # then put your OpenRouter key in it
```

Needs CPython 3.10-3.12 (ifcopenshell ships no PyPy wheels). The source
models are not in this repo - point `--source` at wherever yours live.

```
.venv\Scripts\python -m pytest tests/ -q
```

137 tests. The ones needing a real model skip when the corpus is absent, so the
suite runs on a fresh clone; set `IFCFAULT_SOURCE_ROOT` to point at yours.

## A note on the model

The default is `qwen/qwen-2.5-coder-32b-instruct`, set in `.env` via
`OPENROUTER_MODEL`.

**At the time of writing, the only OpenRouter provider serving that model
(Cloudflare) is unreliable for generations beyond a few hundred tokens.** It
answers with HTTP 200, `finish_reason: "error"`, and a body truncated
mid-token, injecting `{"error": {"code": 500}}` into the stream. It fails
deterministically: the same request truncates at the same character with a
different seed and a different temperature, so retrying does not help, and
asking it to continue does not splice cleanly.

The client detects this and refuses to return a truncated response rather
than handing the safety gate half a program. If you hit it, set
`OPENROUTER_MODEL=qwen/qwen3-coder` (same family, healthy provider) - that is
what the scripts currently in `generated/` were built with. Switch back when
the endpoint recovers; nothing else changes.
