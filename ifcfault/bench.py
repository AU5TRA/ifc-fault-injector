"""
Benchmarking the generator across code models.

The question this answers is narrow and worth stating precisely, because it
is NOT "which model writes the best code". It is:

    given the same model, the same clause and the same building, how often
    does the harness this model writes survive all three gates, how many
    repair rounds does it take, and what does that cost?

That is a fair question to ask of a 14B and of Opus alike, because the
target is fixed: four functions with fixed signatures, a named return shape,
and a validator that does not care who wrote the code. A model either
produces a script that passes independent verification or it does not.

Two things make the numbers comparable:

  * The task suite is fixed (TASKS below) and every model gets all of it.
  * Cost is read from the call log, which records what the provider actually
    billed, rather than estimated from token counts.

And one thing makes them honest: a cache hit costs nothing and proves
nothing, so a benchmark run always bypasses the cache. Re-running a model
therefore costs real money again  - that is the point.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .config import REPO_ROOT

LOG_PATH = REPO_ROOT / "ifcfault" / ".llm_cache" / "call_log.jsonl"


@dataclass(frozen=True)
class Task:
    """One unit of work every model is asked to do."""

    label: str
    rule_spec: str
    source: str
    #: What this task is here to exercise, for the table's footnote.
    exercises: str


#: The suite. Deliberately small and deliberately varied: a plain saved rule,
#: a rule whose mutation DELETES elements, and a multi-fault plan. Between
#: them they cover every shape the harness has to handle.
#:
#: Sources are picked so the rule actually applies  - S-rules need a
#: structural model, and asking for one on an architectural file measures
#: the corpus, not the model.
DEFAULT_TASKS: tuple[Task, ...] = (
    Task("A1-single", "A1",
         r"D:\Real World BIMs\models\dental_clinic\arc.ifc",
         "one saved rule, direct attribute write"),
    Task("S5-delete", "S5",
         r"D:\Real World BIMs\models\schependomlaan\str_engineer.ifc",
         "a rule that deletes elements, so only a marker can point at it"),
    Task("A1+A2-multi", "A1,A2",
         r"D:\Real World BIMs\models\dental_clinic\arc.ifc",
         "multi-fault plan: the loop and the exclusion set"),
)

#: Resolved OpenRouter ids. A friendly name maps to exactly one id so the
#: table can say "Qwen2.5-Coder-32B" while the runner calls the real thing.
MODELS: dict[str, str] = {
    "Claude Opus 5": "anthropic/claude-opus-5",
    "GPT-5.3-Codex": "openai/gpt-5.3-codex",
    "Qwen2.5-Coder-32B-Instruct": "qwen/qwen-2.5-coder-32b-instruct",
    "Qwen3-Coder": "qwen/qwen3-coder",
}


@dataclass
class Run:
    """What one (model, task) pair produced."""

    model_label: str
    model_id: str
    task: Task
    ok: bool = False
    #: "none" | "harness" | "rule" | "environment" | "error"
    fault: str = "error"
    rounds: int = 0
    calls: int = 0
    failed_calls: int = 0
    cost_usd: float = 0.0
    wall_s: float = 0.0
    script_path: Optional[str] = None
    note: str = ""


def _log_len() -> int:
    if not LOG_PATH.exists():
        return 0
    with LOG_PATH.open(encoding="utf-8") as fh:
        return sum(1 for _ in fh)


def _log_since(offset: int, model_id: str) -> tuple[int, int, float]:
    """(calls, failed_calls, cost) this model logged after `offset` lines.

    Read from the log rather than estimated, so the figure is what the
    provider billed  - including the calls that failed and produced nothing.
    """
    if not LOG_PATH.exists():
        return 0, 0, 0.0
    calls = failed = 0
    cost = 0.0
    with LOG_PATH.open(encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            if i < offset:
                continue
            try:
                entry = json.loads(line)
            except Exception:
                continue
            if entry.get("model") != model_id or entry.get("cached"):
                continue
            calls += 1
            if entry.get("error"):
                failed += 1
            cost += entry.get("cost_usd") or 0.0
    return calls, failed, cost


def run_one(model_label: str, model_id: str, task: Task, *, outdir: Path,
            timeout_s: Optional[int] = None, log=print) -> Run:
    """One (model, task) pair, start to finish. Never raises.

    A model that cannot produce a usable harness is a RESULT, not an error,
    so every failure path lands in the returned Run with a fault class on
    it. A benchmark that crashes on its worst performer measures nothing.
    """
    from .emit import EmitError, emit, emit_multi
    from .llm import LLMError
    from .plan import PlanError, parse_rule_spec
    from .synth import SynthesisError

    run = Run(model_label=model_label, model_id=model_id, task=task)
    rule_ids = parse_rule_spec(task.rule_spec)

    previous = os.environ.get("OPENROUTER_MODEL")
    os.environ["OPENROUTER_MODEL"] = model_id
    offset = _log_len()
    started = time.time()
    try:
        common = dict(
            source_ifc=Path(task.source), outdir=outdir,
            # The cache is keyed on the model, so a first run of a given
            # model misses anyway  - but a RE-run would hit, and a free
            # cache hit is not a measurement. Always generate for real.
            no_cache=True,
            timeout_s=timeout_s, log=lambda *a, **k: None,
        )
        result = (emit_multi(rule_ids=rule_ids, **common) if len(rule_ids) > 1
                  else emit(rule_id=rule_ids[0], **common))
        run.ok = result.ok
        run.rounds = result.rounds
        run.script_path = str(result.script_path) if result.script_path else None
        run.fault = (result.validation.fault if result.validation else "error")
        if not result.validation:
            run.note = result.message or "no validation run completed"
    except (EmitError, PlanError, SynthesisError, LLMError) as e:
        # The truncation-refusal path lands here: the client would rather
        # report nothing than hand the safety gate half a program.
        run.fault = "error"
        run.note = f"{type(e).__name__}: {e}"[:200]
    except Exception as e:                      # noqa: BLE001 - a benchmark
        run.fault = "error"                     # must survive its own subjects
        run.note = f"unexpected {type(e).__name__}: {e}"[:200]
    finally:
        run.wall_s = time.time() - started
        run.calls, run.failed_calls, run.cost_usd = _log_since(offset, model_id)
        if previous is None:
            os.environ.pop("OPENROUTER_MODEL", None)
        else:
            os.environ["OPENROUTER_MODEL"] = previous

    verdict = "PASS" if run.ok else run.fault.upper()
    log(f"    {task.label:14} {verdict:12} {run.rounds} round(s)  "
        f"{run.calls:2} call(s)  ${run.cost_usd:.4f}  {run.wall_s:.0f}s"
        + (f"  [{run.note[:60]}]" if run.note else ""))
    return run


def run_bench(model_labels: list[str], tasks=DEFAULT_TASKS, *,
              outdir: Path = Path("bench_out"), timeout_s: Optional[int] = None,
              log=print) -> list[Run]:
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    runs: list[Run] = []
    for label in model_labels:
        model_id = MODELS.get(label, label)
        log(f"\n  {label}  ({model_id})")
        for task in tasks:
            runs.append(run_one(label, model_id, task,
                                outdir=outdir / label.replace(" ", "_").replace("/", "_"),
                                timeout_s=timeout_s, log=log))
    return runs


# ---------------------------------------------------------------------------
# the table
# ---------------------------------------------------------------------------
@dataclass
class ModelSummary:
    label: str
    model_id: str
    n: int = 0
    validated: int = 0
    rounds: list[int] = field(default_factory=list)
    costs: list[float] = field(default_factory=list)
    walls: list[float] = field(default_factory=list)
    faults: dict = field(default_factory=dict)

    @property
    def pct(self) -> float:
        return 100.0 * self.validated / self.n if self.n else 0.0

    @property
    def avg_cost(self) -> float:
        return sum(self.costs) / len(self.costs) if self.costs else 0.0

    @property
    def avg_rounds(self) -> float:
        passed = [r for r, c in zip(self.rounds, self.costs)] or [0]
        return sum(passed) / len(passed)

    @property
    def avg_wall(self) -> float:
        return sum(self.walls) / len(self.walls) if self.walls else 0.0


def summarize(runs: list[Run]) -> list[ModelSummary]:
    out: dict[str, ModelSummary] = {}
    for run in runs:
        s = out.setdefault(run.model_label,
                           ModelSummary(run.model_label, run.model_id))
        s.n += 1
        s.validated += 1 if run.ok else 0
        s.rounds.append(run.rounds)
        s.costs.append(run.cost_usd)
        s.walls.append(run.wall_s)
        key = "validated" if run.ok else run.fault
        s.faults[key] = s.faults.get(key, 0) + 1
    return list(out.values())


def render_text(runs: list[Run], tasks=DEFAULT_TASKS) -> str:
    """The poster/plain-text table."""
    summaries = summarize(runs)
    best = max((s.pct for s in summaries), default=0.0)

    lines = [
        "Model                          % Validated   $ Avg. Cost   Rounds   Avg. s",
        "-" * 76,
    ]
    for s in summaries:
        star = " *" if s.pct == best and best > 0 else "  "
        pct = "-" if s.n == 0 else f"{s.pct:.2f}"
        lines.append(f"  {s.label:28} {pct:>7}{star}    {s.avg_cost:9.4f}   "
                     f"{s.avg_rounds:6.1f}   {s.avg_wall:6.0f}")
    lines += ["-" * 76, "", "Per task:", ""]

    width = max(len(t.label) for t in tasks)
    header = "  " + " " * 28 + "  ".join(f"{t.label:>{width}}" for t in tasks)
    lines.append(header)
    for s in summaries:
        cells = []
        for task in tasks:
            match = next((r for r in runs
                          if r.model_label == s.label and r.task.label == task.label), None)
            cells.append(f"{('PASS' if match.ok else match.fault):>{width}}"
                         if match else f"{'-':>{width}}")
        lines.append(f"  {s.label:28}" + "  ".join(cells))

    lines += ["", "Tasks:"]
    for task in tasks:
        lines.append(f"  {task.label:14} --rule {task.rule_spec:10} {task.exercises}")
    lines += [
        "",
        "% Validated  fraction of tasks whose script passed all three gates:",
        "             static analysis, subprocess execution against the real",
        "             model, and independent re-derivation by ifcfault/verify/.",
        "$ Avg. Cost  what the provider billed per task, read from the call log,",
        "             including calls that errored and produced nothing.",
        "Rounds       harness regeneration rounds used (1 = right first time).",
    ]
    return "\n".join(lines)


def render_latex(runs: list[Run]) -> str:
    """The same table, for a paper."""
    summaries = summarize(runs)
    best = max((s.pct for s in summaries), default=0.0)

    lines = [
        r"\begin{tabular}{lrr}",
        r"\toprule",
        r"Model & \% Validated & \$ Avg. Cost \\",
        r"\midrule",
    ]
    for s in summaries:
        pct = "-" if s.n == 0 else f"{s.pct:.2f}"
        if s.pct == best and best > 0:
            pct = r"\textbf{" + pct + "}"
        lines.append(f"{s.label} & {pct} & {s.avg_cost:.4f} \\\\")
    lines += [r"\bottomrule", r"\end{tabular}"]
    return "\n".join(lines)


def render_json(runs: list[Run]) -> str:
    return json.dumps([
        {
            "model": r.model_label, "model_id": r.model_id, "task": r.task.label,
            "rule_spec": r.task.rule_spec, "source": r.task.source,
            "validated": r.ok, "fault": r.fault, "rounds": r.rounds,
            "calls": r.calls, "failed_calls": r.failed_calls,
            "cost_usd": round(r.cost_usd, 6), "wall_s": round(r.wall_s, 1),
            "script": r.script_path, "note": r.note,
        }
        for r in runs
    ], indent=2)


# ---------------------------------------------------------------------------
# projected cost
# ---------------------------------------------------------------------------
#: Token cost of one emission, MEASURED on this project rather than guessed.
#: Taken from two cache-bypassed qwen3-coder emissions (4 harness calls each)
#: whose billed totals were $0.01287 and $0.01713.
#:
#: Tokens are a property of the WORK, not of the model: the prompt is the
#: same template and the same model inventory whoever answers it, and the
#: completion is one function of a fixed shape. So the same token counts
#: priced at another model's rate is a fair projection. What it cannot
#: predict is how many REPAIR rounds a given model needs - that is exactly
#: what differs between models, and it is why ROUNDS is a separate column
#: rather than folded into the price.
WORKLOADS: dict[str, dict] = {
    "single-fault": {"calls": 4, "prompt": 10690, "completion": 3333,
                     "note": "one saved rule (e.g. --rule A1)"},
    "multi-fault": {"calls": 4, "prompt": 12759, "completion": 3934,
                    "note": "a plan (e.g. --rule A1,A2,S1)"},
}

#: One repair round: the original prompt, the rejected answer, and the
#: concrete failure. Only ONE function is regenerated, which is why a round
#: costs a fraction of an emission rather than another whole one.
REPAIR_CALL = {"prompt": 3640, "completion": 958}

#: Synthesizing a new rule from a clause, per attempt.
SYNTHESIS_CALL = {"prompt": 2096, "completion": 1043}


def fetch_pricing() -> dict:
    """{model_id: (prompt $/token, completion $/token)} from OpenRouter."""
    import httpx

    from .llm import load_dotenv

    load_dotenv()
    response = httpx.get(
        "https://openrouter.ai/api/v1/models", timeout=30,
        headers={"Authorization": f"Bearer {os.environ.get('OPENROUTER_API_KEY', '')}"})
    out = {}
    for model in response.json()["data"]:
        price = model.get("pricing") or {}
        out[model["id"]] = (float(price.get("prompt") or 0),
                            float(price.get("completion") or 0))
    return out


def cost_of(workload: dict, price: tuple, rounds: int = 1) -> float:
    """What one emission costs at this model's rate, with `rounds` rounds."""
    prompt_rate, completion_rate = price
    base = workload["prompt"] * prompt_rate + workload["completion"] * completion_rate
    repairs = (rounds - 1) * (REPAIR_CALL["prompt"] * prompt_rate
                              + REPAIR_CALL["completion"] * completion_rate)
    return base + repairs


def estimate(model_labels: list[str], tasks=DEFAULT_TASKS) -> str:
    """What a benchmark run would cost, before spending anything."""
    try:
        pricing = fetch_pricing()
    except Exception as e:
        return f"could not fetch live pricing: {e}"

    lines = [f"Projected cost, {len(tasks)} task(s) per model, 1 round each:", ""]
    total = 0.0
    for label in model_labels:
        model_id = MODELS.get(label, label)
        price = pricing.get(model_id)
        if not price:
            lines.append(f"  {label:30} NOT ON OPENROUTER")
            continue
        per = cost_of(WORKLOADS["single-fault"], price)
        lines.append(f"  {label:30} ${per:7.4f}/task   ${per * len(tasks):7.4f} total")
        total += per * len(tasks)
    lines += ["", f"  {'TOTAL':30} ${total:7.4f}",
              "",
              "  Repairs add roughly one call each; a 4-round worst case about",
              "  doubles it. Wall-clock is dominated by parsing the IFC in the",
              "  validation subprocess, not by the model."]
    return "\n".join(lines)


#: What this project has actually MEASURED, as opposed to projected. Keyed by
#: OpenRouter id. Anything absent shows as "-" in the table, exactly as the
#: SWE-agent table dashes the configurations it did not run.
MEASURED: dict[str, dict] = {
    "qwen/qwen3-coder": {
        "emissions": 4, "validated": 3, "rounds": [1, 2, 1, 1],
        "calls": 55, "failed_calls": 0,
        "note": "3/4 validated; the 4th failed `environment` (S1 needs beams, "
                "the architectural model has none) - not a harness failure",
    },
    "qwen/qwen-2.5-coder-32b-instruct": {
        "emissions": 3, "validated": 0, "rounds": [],
        "calls": 40, "failed_calls": 40,
        "note": "0/40 calls returned a complete body: the provider answers "
                "HTTP 200 with finish_reason=error and a body cut mid-token",
    },
}


# ---------------------------------------------------------------------------
# provider price ranges
# ---------------------------------------------------------------------------
def fetch_endpoints(model_id: str) -> list[tuple]:
    """Every (prompt, completion) $/token this model is served at.

    OpenRouter routes one model id to several providers, each with its own
    price, so "the" price of an open-weight model is a range rather than a
    number. This matters more than it sounds: qwen3-coder is served between
    $0.22 and $0.975 per million prompt tokens, a 4.4x spread, and the
    billed cost of calls in this project's own log varies from 0.64x to
    4.12x the headline rate for exactly that reason.
    """
    import httpx

    from .llm import load_dotenv

    load_dotenv()
    author, _, slug = model_id.partition("/")
    response = httpx.get(
        f"https://openrouter.ai/api/v1/models/{author}/{slug}/endpoints", timeout=30,
        headers={"Authorization": f"Bearer {os.environ.get('OPENROUTER_API_KEY', '')}"})
    endpoints = response.json()["data"]["endpoints"]
    return sorted({(float(e["pricing"]["prompt"]), float(e["pricing"]["completion"]))
                   for e in endpoints})


def cost_range(workload: dict, prices: list[tuple], rounds: int = 1) -> tuple:
    """(cheapest, dearest) cost for this workload across a model's providers."""
    costs = [cost_of(workload, p, rounds) for p in prices]
    return (min(costs), max(costs)) if costs else (0.0, 0.0)


def _fmt_range(lo: float, hi: float) -> str:
    if abs(hi - lo) < 1e-9:
        return f"{lo:.4f}"
    return f"{lo:.4f}-{hi:.4f}"


def render_cost_review(model_labels: list[str]) -> str:
    """The projected-cost table, in the shape of the SWE-agent results table.

    Two workload columns instead of two benchmarks. Cost is projected from
    token counts this project MEASURED, priced at every provider the model
    is actually served by. % Validated is filled in only where this project
    has really run the model and dashed otherwise - a number nobody measured
    is worse than a gap.
    """
    rows = []
    for label in model_labels:
        model_id = MODELS.get(label, label)
        try:
            prices = fetch_endpoints(model_id)
        except Exception:
            prices = []
        measured = MEASURED.get(model_id)
        rows.append((label, model_id, prices, measured))

    lines = [
        "=" * 88,
        " COST REVIEW  --  one validated injection script, by code model",
        "=" * 88,
        "",
        "                                Single-fault emission        Multi-fault emission",
        "Model                       Prov.  % Valid.   $ Est. Cost    % Valid.   $ Est. Cost",
        "-" * 88,
    ]
    for label, model_id, prices, measured in rows:
        if not prices:
            lines.append(f"  {label:26}    -       -           -            -           -")
            continue
        n = len(prices)
        if measured and measured["emissions"]:
            pct = f"{100.0 * measured['validated'] / measured['emissions']:.2f}"
        else:
            pct = "-"
        s_lo, s_hi = cost_range(WORKLOADS["single-fault"], prices)
        m_lo, m_hi = cost_range(WORKLOADS["multi-fault"], prices)
        lines.append(f"  {label:26} {n:4}   {pct:>7}   {_fmt_range(s_lo, s_hi):>13}  "
                     f"{pct:>7}   {_fmt_range(m_lo, m_hi):>13}")
    lines += ["-" * 88, ""]

    # measured reality, where it exists
    lines += [" MEASURED  (what this project actually billed)", "-" * 88]
    any_measured = False
    for label, model_id, prices, measured in rows:
        if not measured:
            continue
        any_measured = True
        lines.append(
            f"  {label:26} {measured['calls']:3} call(s), "
            f"{measured['failed_calls']} failed | "
            f"{measured['validated']}/{measured['emissions']} emission(s) validated")
        lines.append(f"  {'':26} {measured['note']}")
    if not any_measured:
        lines.append("  (none)")
    lines += ["", "=" * 88, " METHOD", "-" * 88]
    lines += [
        "",
        " Token counts are MEASURED on this project, not guessed. Two cache-bypassed",
        " qwen3-coder emissions, four harness calls each:",
        "",
        f"   single-fault   {WORKLOADS['single-fault']['prompt']:6} prompt + "
        f"{WORKLOADS['single-fault']['completion']:5} completion tok   (billed $0.01287)",
        f"   multi-fault    {WORKLOADS['multi-fault']['prompt']:6} prompt + "
        f"{WORKLOADS['multi-fault']['completion']:5} completion tok   (billed $0.01713)",
        f"   repair round   {REPAIR_CALL['prompt']:6} prompt + "
        f"{REPAIR_CALL['completion']:5} completion tok   (ONE function only)",
        "",
        " Tokens are a property of the WORK, not of the model: the prompt is the same",
        " template and the same model inventory whoever answers it, and the completion",
        " is one function of a fixed shape. Pricing those same counts at another",
        " model's published rate is therefore a fair projection.",
        "",
        " What it does NOT predict is how many repair rounds a model needs -- and that",
        " is exactly what differs between models. A model that needs three rounds costs",
        " about 1.5x its single-round figure. % Validated and rounds can only come from",
        " running the suite; `python -m ifcfault bench` does that.",
        "",
        " 'Prov.' is how many endpoints OpenRouter serves the model from. An open-weight",
        " model is routed across providers at different prices, so its cost is a RANGE,",
        " and which end you land on is not under your control. Observed in this",
        " project's log: billed cost per call ran from 0.64x to 4.12x the headline rate.",
        "",
        " Cost excludes the validation run, which is local: parsing the IFC and",
        " re-deriving the clause costs wall-clock, not tokens.",
        "",
        "=" * 88,
    ]
    return "\n".join(lines)


def render_cost_latex(model_labels: list[str]) -> str:
    lines = [
        r"\begin{tabular}{lrrrrr}",
        r"\toprule",
        r"& & \multicolumn{2}{c}{Single-fault} & \multicolumn{2}{c}{Multi-fault} \\",
        r"\cmidrule(lr){3-4}\cmidrule(lr){5-6}",
        r"Model & Prov. & \% Valid. & \$ Est. Cost & \% Valid. & \$ Est. Cost \\",
        r"\midrule",
    ]
    for label in model_labels:
        model_id = MODELS.get(label, label)
        try:
            prices = fetch_endpoints(model_id)
        except Exception:
            prices = []
        measured = MEASURED.get(model_id)
        if not prices:
            lines.append(f"{label} & - & - & - & - & - \\\\")
            continue
        pct = (f"{100.0 * measured['validated'] / measured['emissions']:.2f}"
               if measured and measured["emissions"] else "-")
        s = _fmt_range(*cost_range(WORKLOADS["single-fault"], prices)).replace("-", "--")
        m = _fmt_range(*cost_range(WORKLOADS["multi-fault"], prices)).replace("-", "--")
        lines.append(f"{label} & {len(prices)} & {pct} & {s} & {pct} & {m} \\\\")
    lines += [r"\bottomrule", r"\end{tabular}"]
    return "\n".join(lines)
