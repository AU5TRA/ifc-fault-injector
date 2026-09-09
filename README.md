# ifcfault

Give it one real IFC building model and one building-code rule. It gives you
back **a standalone Python script** that you run yourself to produce the
faulty model.

```
python -m ifcfault emit --source "D:\Real World BIMs\models\dental_clinic\arc.ifc" --rule A1
    -> generated/dental_clinic_arc_A1_inject.py

python generated/dental_clinic_arc_A1_inject.py --outdir out
    -> out/dental_clinic_arc_A1.ifc            faulty, UNMARKED
    -> out/dental_clinic_arc_A1_colored.ifc    faulty, marked up for a viewer
    -> out/dental_clinic_arc_A1_report.txt     what changed, where, how to find it
    -> out/dental_clinic_arc_A1_record.json    the same facts, machine-readable
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

## Two output IFCs, on purpose

- **`<stem>.ifc`** carries the fault and **no visual marking at all.** This is
  the one you feed to the checker under test. Colouring it would hand the
  checker the answer.
- **`<stem>_colored.ifc`** carries the same fault, marked up three ways, so a
  human can find it. Open this one in Revit or an IFC viewer.

## Finding the fault in a viewer

Marking is deliberately redundant, because no single handle survives every
viewer:

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
is violated. Plus that the coloured file is genuinely findable, and that the
prose report does not contradict the JSON record.

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

62 tests. The ones needing a real model skip when the corpus is absent, so the
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
