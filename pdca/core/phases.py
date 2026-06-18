"""The PDCA phase functions.

Agentic design notes (v3):
- Plan reasons explicitly (analysis + strategy) and writes detailed steps.
- Do is a turn-based agent: it acts one tool call at a time (read/write/run/ls/note)
  and calls finish() when the work is genuinely done, or the harness hands off at a
  turn cap. It chooses per subtask whether to act directly or automate with a script.
- Check = objective gate + the model audit, and decides ON DEMAND whether to run an
  independent PROBE (a turn-based skeptical verifier) and re-judge with its findings.
- Act can only ADOPT or ADJUST. Giving up is not a model decision: the
  harness stops on budget, or on a harness-initiated feasibility audit.
"""
import json
import os
import subprocess
from typing import Literal

from pydantic import BaseModel, Field

from pdca import config, llm
from pdca.core import session
from pdca.execution import executor
from pdca.tools.toolkit import AGENT_PROTOCOL, TOOLKIT_DOCS


class Step(BaseModel):
    id: int
    instruction: str


class QualityCriterion(BaseModel):
    text: str        # what "good" looks like, derived from the user's desired outcome
    rationale: str   # why this matters for THIS task — forces outcome-grounding


class Plan(BaseModel):
    task_type: Literal["build_fix", "produce_analyze"]
    analysis: str
    strategy: str
    objective: str
    steps: list[Step] = Field(max_length=7)
    success_criteria: list[str]
    verify_commands: list[str]
    quality_criteria: list[QualityCriterion] = Field(min_length=1, max_length=5)


class CriterionVerdict(BaseModel):
    text: str
    status: Literal["met", "unmet", "unknown"]
    reason: str


class QualityVerdict(BaseModel):
    text: str
    status: Literal["met", "unmet", "unknown"]
    reason: str      # must cite concrete observed evidence


class Report(BaseModel):
    criteria: list[CriterionVerdict]
    quality: list[QualityVerdict]
    gaps: list[str]
    overall: Literal["pass", "fail"]
    # Set on the first CHECK pass when the evidence is too thin to judge confidently
    # or smells gamed: the runner then runs an independent PROBE and re-checks.
    request_probe: bool = False
    probe_reason: str = ""


class Decision(BaseModel):
    decision: Literal["ADOPT", "ADJUST"]
    reason: str
    adjustments: list[str] = []


class Feasibility(BaseModel):
    verdict: Literal["INFEASIBLE", "CONTINUE"]
    reason: str


# ---------------------------------------------------------------- PLAN ----

PLAN_SYSTEM = """You are the PLAN phase of an autonomous PDCA agent: a senior engineer who owns
what "done" means. Done is the user's actual desired outcome — never the cheapest
artifact that makes a check turn green. Reason first, then plan. Produce a JSON object:
{"task_type": "build_fix" | "produce_analyze",
                     // build_fix: there is an objective truth (tests/build/lint pass).
                     // produce_analyze: the value is a deliverable's quality, which no
                     // shell command can fully capture. Choose honestly; it sets how
                     // much weight the quality bar below must carry.
 "analysis": str,    // REASON HERE: what is the current state? If a previous cycle
                     // failed, name the EXACT failure (error text, file, line) and
                     // explain the root cause. Be thorough — several sentences.
 "strategy": str,    // The approach for THIS cycle and, if previous attempts failed,
                     // why this strategy will succeed where they did not.
 "objective": str,
 "steps": [{"id": 1, "instruction": str}, ...],   // at most 7 steps
 "success_criteria": [str, ...],                   // OBJECTIVE FLOOR, each shell-checkable
 "verify_commands": [str, ...],                    // shell cmds; exit 0 = success
 "quality_criteria": [{"text": str, "rationale": str}, ...]}  // 1-5; the real bar (see below)

Rules:
- Step instructions must be DETAILED and self-contained: name exact files,
  functions, and changes; include the relevant error text the executor must fix.
  Multi-sentence instructions are good. The execution agent sees only your plan,
  not the conversation.
- success_criteria are the OBJECTIVE FLOOR, not the bar. They define the minimum
  machine-verifiable facts about the WHOLE USER TASK — never just this cycle's
  sub-goal. Criteria like "test output was captured" or "the bug was diagnosed"
  are forbidden: even on a diagnostic cycle the criteria stay task-level (and will
  simply fail until the task is truly done). Keep them minimal (1-4), each an
  observable fact directly checkable by one of verify_commands.
- quality_criteria are THE BAR: what makes the deliverable genuinely good for the
  user's stated outcome — the things no shell command can capture. Derive them from
  the task by asking: "if I handed this result back, would the user consider their
  goal actually met?" Each criterion names a concrete, observable property of the
  RESULT (not of the process), with a rationale tying it to this specific task.
  They must NOT restate the success_criteria or the verify_commands. A reviewer —
  not a shell — will judge them, and the loop cannot ADOPT until every one is met.
  Set their strictness from task_type: for build_fix, focus on correctness depth
  (root cause addressed, no check gamed); for produce_analyze, focus on whether the
  deliverable fully and soundly satisfies what the user asked for.
- NEVER weaken or drop a quality criterion across cycles to make a cycle pass. You
  may only refine them to be stricter or clearer. Weakening the bar to claim
  success is the one thing you may never do.
- Each cycle should make real progress toward the criteria: diagnose AND fix in
  the same cycle whenever possible, rather than spending a cycle only looking.
- verify_commands check STRUCTURAL and FUNCTIONAL facts only: a file exists, a
  script runs cleanly, a model/artifact loads, tests pass, an output has the right
  shape or type. They must NEVER grep a human-readable deliverable (a report,
  summary, explanation, write-up) for specific words or phrases. Prose quality is
  judged by the reviewer, not by string matching — grepping for keywords only
  teaches the executor to paste those keywords in, which is reward hacking. Never
  encode a quality_criterion as a text-presence check.
- verify_commands MUST use pre-installed standard tools only: pytest,
  python -m pytest, ruff, python -c '...', bash -c '...', etc.
  NEVER reference a Python file you write in this cycle (e.g. "python validate_tool.py")
  — that file becomes a lint target and will break ruff check. Use python -c '...'
  inline invocations to functionally test the artifact instead.
- python -c '...' verify commands must be single-expression or simple assignment
  checks only — NO try/except, no multi-statement control flow (those break as
  single-line commands). For error-handling checks, write a pytest test instead.
  Simple OK: python -c "from mod import fn; assert fn(2)==4"
  BAD (syntax error): python -c "try: fn([]); except ValueError: pass"
- Verify functionality, not just unit tests: where applicable include a
  verify_command that RUNS the built artifact on real input (e.g. a
  python -c "from cleaner import clean; ..." invocation on sample data),
  alongside test/lint commands.
- The execution agent writes Python scripts using read/write/run/ls on the
  working directory. It cannot ask the user anything. If facts are missing,
  step 1 gathers them (inspect files, run commands).
- When the task involves numeric output formatting (floats stored as CSV strings):
  the step instructions MUST specify the exact format to use for BOTH
  implementation AND tests. Suggested rule: use `f"{x:g}"` which removes
  trailing zeros (`200.0` → `"200"`, `1234.56` → `"1234.56"`, `-50.0` → `"-50"`).
  Inconsistent format expectations between tests and implementation will always
  fail and are the most common cause of stuck cycles.
- When writing tests: derive expected string values from Python's actual behavior —
  run `python -c "print(f'{float(x):g}')"` mentally or actually. Never hardcode
  expected values without verifying them match the formatting the implementation uses.
- Address every gap and lesson you are given; NEVER re-issue a plan that already
  failed in the same form. If the same error has survived previous cycles, your
  analysis must explain why, and the strategy must change materially
  (e.g. rewrite the file from scratch instead of patching it).
Reply with ONLY the JSON object."""

RETHINK_BLOCK = """
!!! REPEATED FAILURE — RETHINK REQUIRED !!!
The same verification failure has now survived {n} consecutive cycles. The
previous approach is NOT working. Requirements for this plan:
- analysis: diagnose why every previous attempt failed (use the evidence below).
- strategy: a MATERIALLY DIFFERENT approach. Patching again is forbidden;
  consider rewriting files from scratch, simplifying the design, splitting the
  problem, or building a minimal version first and growing it.
- step 1 must be diagnosis: read the failing files / run the failing commands
  and print the exact current errors before changing anything.
"""

QUALITY_STALL_BLOCK = """
!!! QUALITY NOT IMPROVING — CHANGE FOCUS !!!
The objective checks may be passing, but the quality bar has not improved for
several cycles. The loop is thrashing on the DELIVERABLE'S SUBSTANCE, not on
plumbing. For this plan:
- Stop adding or tweaking verify_commands and stop chasing specific wording; a
  reviewer judges quality by substance, never by the presence of any phrase.
- Consolidate: work from the SINGLE best existing artifact. Do not spawn new
  parallel scripts/files — improve what is already there.
- Make the deliverable genuinely good on its merits: the depth, correctness, and
  completeness of the actual content the user asked for. Name in the steps exactly
  what substance is missing (from the quality gaps) and how this cycle adds it.
"""


def plan(task: str, state_digest: str, last_check: str, lessons: str,
         rethink_count: int = 0, quality_stalled: bool = False) -> Plan:
    session.set_phase("PLAN")
    user = f"TASK:\n{task}\n\nSTATE (last cycles digest):\n{state_digest or '(first cycle)'}"
    if lessons:
        user += f"\n\nLESSONS / FAILED APPROACHES (do not repeat these):\n{lessons}"
    if last_check:
        user += f"\n\nPREVIOUS CYCLE FAILURE CONTEXT:\n{last_check}"
    if rethink_count:
        user += RETHINK_BLOCK.format(n=rethink_count)
    if quality_stalled:
        user += QUALITY_STALL_BLOCK
    return llm.call_validated(config.MODEL_PLAN, PLAN_SYSTEM, user, Plan)


# ------------------------------------------------------------------ DO ----

DO_SYSTEM = f"""You are the DO phase of an autonomous PDCA agent: a meticulous engineer who takes
quality seriously and does the real work. You execute a plan by ACTING — one turn
at a time, using tools — until the work is genuinely done.

{TOOLKIT_DOCS}

{AGENT_PROTOCOL}

You are given the ORIGINAL USER TASK and the full DEFINITION OF DONE (success
criteria = the objective floor, quality criteria = the bar). Work toward ALL of
it, not just this cycle's steps: the loop only finishes when every success AND
quality criterion is met, so build something that will satisfy the quality bar too.

This is an ITERATIVE loop across cycles. After the first cycle the working
directory already contains prior cycles' work. ALWAYS inspect it first (ls, read
the existing files, and the PRIOR SUMMARY shown to you) and BUILD ON it — fix the
specific thing that failed, keep everything that already worked. Do NOT rewrite
working files from scratch: that throws away progress and re-introduces bugs the
loop already solved. Rewrite only when the previous approach is fundamentally
wrong and you say why.

How to work:
- Decide, per subtask, whether to act directly or to automate. Writing a few
  distinct files or making a targeted edit → just `write`/`read` them. A
  repetitive/bulk/computational subtask → `write` a script (any language that
  fits) and `run` it. Reason about what is actually practical; neither is required.
- ACT, don't just inspect. When editing a file, `read` it, change only what the
  plan requires, and `write` the FULL file back, preserving all unrelated content
  exactly. CRITICAL: never remove existing list entries (format lists, handlers,
  test cases, supported values) unless the plan explicitly says to — dropping items
  causes silent regressions.
- After a `write` to a .py file, check the syntax verdict it returns; if it reports
  SYNTAX ERROR, fix and re-write before moving on.
- Verify your own work as you go: `run` the plan's verify commands and the actual
  artifact, read the failures, and fix the root cause. You are NOT required to end
  on green — but only call `finish` when you genuinely believe the work is complete.
- NEVER game a check. Do not hardcode answers, embed canned or templated output,
  special-case the inputs a check happens to use, or build a trivial/degenerate
  setup whose success is an artifact of the check rather than real evidence the
  outcome was achieved. Produce genuinely correct work and let the checks pass as a
  consequence. Your output is independently reviewed for exactly this; gamed work
  will be rejected.

Call `finish(summary)` when — and only when — the work is genuinely done; the
summary is carried into the next cycle. If you cannot finish within your turn
budget, the harness hands off your progress automatically."""


def _read_notes(evidence_log: str) -> str:
    """The cycle's note() lines, capped — surfaced to Check as evidence."""
    if evidence_log and os.path.exists(evidence_log):
        with open(evidence_log, encoding="utf-8") as f:
            return f.read()[:2000]
    return ""


def do(plan_obj: Plan, state_digest: str, workdir: str, evidence_log: str,
       model: str | None = None, task: str = "", prior_summary: str = "",
       prev_stderr: str = "") -> dict:
    session.set_phase("DO")
    model = model or config.MODEL_DO
    steps = "\n".join(f"{s.id}. {s.instruction}" for s in plan_obj.steps)
    verify = "\n".join(f"- {c}" for c in plan_obj.verify_commands) or "(none)"
    user = (
        (f"ORIGINAL USER TASK (the real goal):\n{task}\n\n" if task else "")
        + f"OBJECTIVE: {plan_obj.objective}\n\nSTRATEGY: {plan_obj.strategy}\n\n"
        f"PLAN STEPS:\n{steps}\n\n"
        f"SUCCESS CRITERIA — objective floor:\n"
        + "\n".join(f"- {c}" for c in plan_obj.success_criteria)
        + "\n\nQUALITY CRITERIA — the bar the deliverable must clear to finish:\n"
        + "\n".join(f"- {q.text}" for q in plan_obj.quality_criteria)
        + f"\n\nVERIFY COMMANDS (run these to check the floor):\n{verify}"
        + (f"\n\nSTATE DIGEST:\n{state_digest}" if state_digest else "")
        + (f"\n\nPRIOR CYCLE SUMMARY (build on this; fix what failed, keep what "
           f"worked — do not start over):\n{prior_summary}" if prior_summary else "")
        + (f"\n\nPRIOR CYCLE'S LAST ERROR (the specific thing to fix):\n{prev_stderr[-1200:]}"
           if prev_stderr else "")
        + "\n\nInspect the working directory and begin."
    )
    ctx = executor.ToolContext(workdir, evidence_log)
    result = llm.agent_loop(model, DO_SYSTEM, user, ctx, config.MAX_DO_TURNS)
    result["notes"] = _read_notes(evidence_log)
    syntax_warn = executor.check_workdir_python_files(workdir)
    if syntax_warn:
        result["stderr"] = (result["stderr"] + "\nHARNESS SYNTAX CHECK: " + syntax_warn)[:2000]
        result["ok"] = False
    session.record_script("DO", result["transcript"])
    return result


# --------------------------------------------------------------- CHECK ----

def run_gate(verify_commands: list[str], workdir: str) -> list[dict]:
    """Objective gate: run each verify command, no model involved. Runs with the
    workdir's venv on PATH (if any) so `python`/`pytest` match what DO and the
    probe use — verify commands need no `./venv/bin/` prefix or `source`."""
    results = []
    env = executor.workdir_env(workdir)
    for cmd in verify_commands:
        try:
            p = subprocess.run(cmd, shell=True, cwd=workdir, env=env,
                               capture_output=True, text=True, timeout=120)
            results.append({"cmd": cmd, "code": p.returncode,
                            "tail": (p.stdout + p.stderr)[-2000:]})
        except subprocess.TimeoutExpired:
            results.append({"cmd": cmd, "code": -1, "tail": "timeout after 120s"})
        except OSError as e:
            results.append({"cmd": cmd, "code": -1, "tail": f"failed to run: {e}"})
    return results


PROBE_SYSTEM = f"""You are the hands-on verifier of a PDCA agent's CHECK phase: a skeptical reviewer
who trusts nothing the execution agent claimed. You INDEPENDENTLY verify the
deliverable by acting one turn at a time, using tools.

{TOOLKIT_DOCS}

{AGENT_PROTOCOL}

Your job, across as many turns as you need:
1. Create fresh sample inputs of your OWN under ".pdca_probe/" and ACTUALLY RUN the
   built artifact on them (e.g. run("python -c 'from module import fn; ...'") or run
   the program). Check the real outputs against what the task requires — never
   certify on a file merely existing.
2. Read key files and look for concrete problems: stubs that return None/pass,
   hardcoded magic values, swallowed bare `except:`, tests with no assert statements.
   Watch specifically for GAMED work: canned or templated output not derived from
   real computation, answers hardcoded to satisfy a check, or a trivial/degenerate
   setup whose success is an artifact of the check rather than evidence the outcome
   was achieved. These ARE violations, not opinions — capture the exact excerpt.
3. NEVER modify or delete project files; only create fixtures under ".pdca_probe/".
   Pure style preferences and missing handling for hypothetical edge cases are NOT
   violations — only functional failures and gamed work are. In Python, int/float
   interchangeability (3.0 == 3) is not a bug unless a specific type is required.

When done, call `finish(summary)` where the summary contains one line per success
criterion:
   "CRITERION n: OK|VIOLATED|UNCLEAR — <concrete observed evidence>"
   - OK: you ran the artifact and the output matched requirements.
   - VIOLATED: the output was WRONG (give actual vs expected) OR produced by gaming.
   - UNCLEAR: the artifact crashed or you couldn't get real output.
Plus a short note on any gamed work you found, with the proving excerpt."""


def probe(plan_obj: Plan, workdir: str, evidence_log: str, probe_reason: str = "") -> dict:
    session.set_phase("PROBE")
    user = (
        f"OBJECTIVE: {plan_obj.objective}\n\nSUCCESS CRITERIA:\n"
        + "\n".join(f"{i+1}. {c}" for i, c in enumerate(plan_obj.success_criteria))
        + (f"\n\nWHY CHECK ASKED FOR A PROBE:\n{probe_reason}" if probe_reason else "")
        + "\n\nBegin verifying."
    )
    ctx = executor.ToolContext(workdir, evidence_log)
    result = llm.agent_loop(config.MODEL_CHECK, PROBE_SYSTEM, user, ctx, config.MAX_PROBE_TURNS)
    result["notes"] = _read_notes(evidence_log)
    session.record_script("PROBE", result["transcript"])
    return result


CHECK_SYSTEM = """You are the CHECK phase of a PDCA agent: a demanding quality auditor and the
user's advocate. You judge two things against the evidence — the OBJECTIVE FLOOR
(success_criteria) and the QUALITY BAR (quality_criteria) — and you answer to the
user's actual outcome, not the cycle's sub-goal.
You receive: the plan's success_criteria and quality_criteria, the execution
agent's evidence (capped transcript/last-error/notes), the OBJECTIVE GATE results
(verify commands + exit codes), and — only when one was requested — an independent
PROBE report (a skeptical verifier's hands-on findings).

PROBE is ON-DEMAND. On your first look there is usually NO probe report. PROBE is
expensive, so request it only when you actually need hands-on verification you
cannot get from the evidence and gate alone — e.g. the evidence is thin or merely
asserts success, the deliverable's correctness can't be judged from the transcript,
or you suspect gaming/Goodhart. To request it, set "request_probe": true and put in
"probe_reason" exactly what an independent verifier should run and check. When the
gate plus evidence already settle the outcome, do NOT request a probe. If a probe
report IS present, judge using it and set "request_probe": false.

Hard rules:
- A criterion whose verify command exited non-zero can NEVER be "met", no matter
  how confident the prose evidence sounds. The gate outranks the narrative.
- A criterion directly verified by a gate command that exited 0 IS "met" unless
  the gate output, probe, or evidence contradicts it. Reserve "unknown" for
  criteria that nothing speaks to.
- A probe (when present) outranks the execution agent's claims: if it found stubs,
  hardcoded answers, gamed/degenerate work, or functional failures, say so in gaps
  even when every verify command passed.
- gaps must be CONCRETE and actionable: quote the exact error text, file and
  line where known — the next plan is built directly from your gaps.

Reward-hacking audit (do this yourself, in ADDITION to the probe):
- Independently look for gamed work, not just functional failures: answers or
  reports hardcoded / templated rather than computed from real results; fake or
  fabricated data, stub or no-op implementations; criteria satisfied by exact
  string-matching tricks rather than substance; a setup so trivial or degenerate
  that passing it proves nothing about the real outcome (Goodhart's law).
- If you find any of these, the relevant quality criteria are "unmet" even when
  every verify command exited 0, and you must name the specific hack in gaps.

Quality bar (the part a shell gate cannot capture):
- For each quality criterion, judge it from the PROBE findings and the actual
  output — NOT from the file merely existing. Cite concrete observed evidence in
  every reason. Mark "unmet" when the deliverable does not genuinely satisfy it,
  "unknown" only when nothing speaks to it.
- Judge by SUBSTANCE, never by whether the output contains particular words or
  phrases. A deliverable stuffed with the "right" keywords but shallow, generic,
  or unsupported is UNMET; do not be satisfied by surface tokens. Conversely, do
  not mark a criterion unmet merely because an expected phrase is missing when the
  substance is plainly present. You judge the work, not string patterns.
- A deliverable can pass every verify command and still fail the quality bar. When
  it does, that is a real failure: name the shortfall in gaps so the next plan
  fixes it. Passing the floor is necessary, never sufficient.

You also receive the ORIGINAL USER TASK. "overall" judges the TASK, not the cycle.
Return "pass" only when every success criterion is met, every gate command exited
0, AND every quality criterion is met — i.e. the user's outcome is genuinely
achieved. Otherwise return "fail" and list what is still missing in gaps.

Reply with ONLY a JSON object:
{"criteria": [{"text": str, "status": "met"|"unmet"|"unknown", "reason": str}, ...],
 "quality": [{"text": str, "status": "met"|"unmet"|"unknown", "reason": str}, ...],
 "gaps": [str, ...],
 "overall": "pass"|"fail",
 "request_probe": bool,
 "probe_reason": str}"""


def check(task: str, plan_obj: Plan, evidence: dict, workdir: str,
          probe_ev: dict | None = None,
          pre_gate: list[dict] | None = None) -> tuple[Report, list[dict]]:
    session.set_phase("CHECK")
    gate = pre_gate if pre_gate is not None else run_gate(plan_obj.verify_commands, workdir)
    user = (
        f"ORIGINAL USER TASK:\n{task}\n\n"
        "SUCCESS CRITERIA (objective floor):\n"
        + "\n".join(f"- {c}" for c in plan_obj.success_criteria)
        + "\n\nQUALITY CRITERIA (the bar — judge each from the output/probe, not file existence):\n"
        + "\n".join(f"- {q.text}  (why it matters: {q.rationale})"
                    for q in plan_obj.quality_criteria)
        + "\n\nOBJECTIVE GATE RESULTS:\n" + json.dumps(gate, indent=1)
        + f"\n\nEXECUTION EVIDENCE:\nagent finished cleanly: {evidence['ok']}\n"
        + f"transcript / summary:\n{evidence['stdout']}\n"
        + (f"last error:\n{evidence['stderr']}\n" if evidence["stderr"] else "")
        + (f"notes:\n{evidence['notes']}\n" if evidence.get("notes") else "")
        + (f"\nPROBE REPORT (independent verification — requested this cycle):\n"
           f"probe finished cleanly: {probe_ev['ok']}\n{probe_ev['stdout'][:3000]}\n"
           + (f"probe last error:\n{probe_ev['stderr'][:600]}\n" if probe_ev["stderr"] else "")
           if probe_ev else "\n(No probe report this pass — request one if you need hands-on verification.)\n")
    )
    report = llm.call_validated(config.MODEL_CHECK, CHECK_SYSTEM, user, Report)
    if probe_ev is not None:
        report.request_probe = False  # a probe already ran; never loop on it
    # Two gates decide adoption, enforced in code so the prompt cannot drift from it:
    #   - objective (shell) gate: authoritative for FAILURE. A non-zero verify command
    #     can never be "done", no matter how good the prose or quality judgment.
    #   - quality gate: authoritative for BLOCKING adoption. A green shell gate is the
    #     floor, not the bar — the deliverable must also meet the user's outcome.
    # overall = pass  iff  every verify command exited 0  AND  every quality criterion met.
    gate_fail = any(g["code"] != 0 for g in gate)
    gate_green = not gate_fail
    quality_met = all(q.status == "met" for q in report.quality)
    if gate_fail:
        report.overall = "fail"
        report.gaps.append("a verify command exited non-zero; overall forced to fail")
    elif gate_green and quality_met:
        # Objective floor met and outcome verified — the gate confirms each criterion.
        report.overall = "pass"
        for c in report.criteria:
            if c.status != "met":
                c.status = "met"
                c.reason += " [runner override: verify command exited 0]"
    else:  # gate_green but quality unmet: the Goodhart fix — green shell != done.
        report.overall = "fail"
        report.gaps.extend(
            f"quality unmet: {q.text} — {q.reason}"
            for q in report.quality if q.status != "met"
        )
    return report, gate


# ----------------------------------------------------------------- ACT ----

ACT_SYSTEM = """You are the ACT phase of a PDCA agent: the delivery lead. Decide the loop's next
move from the check report, judged against the user's actual outcome.
Reply with ONLY a JSON object:
{"decision": "ADOPT"|"ADJUST", "reason": str, "adjustments": [str, ...]}

- ADOPT only if the check report's overall is "pass" AND the report shows the
  ORIGINAL USER TASK genuinely complete to the user's outcome — never for a
  completed sub-goal, and never on a green objective floor alone while quality
  criteria remain unmet.
- Otherwise ADJUST. Giving up is not available to you: the harness manages
  budgets and infeasibility. Your job is to convert the report's gaps (objective
  AND quality) into the most useful concrete adjustments for the next Plan — name
  files, errors, and the specific change of approach if the current one keeps
  failing. A quality shortfall is a real gap: turn it into a concrete improvement,
  do not settle for the minimum that passed the floor."""


def act(task: str, report: Report, cycle: int, history_digest: str,
        lessons: str) -> Decision:
    session.set_phase("ACT")
    user = (
        f"ORIGINAL USER TASK:\n{task}\n\nCYCLE: {cycle}\n\n"
        f"CHECK REPORT:\n{report.model_dump_json(indent=1)}\n\n"
        f"HISTORY DIGEST:\n{history_digest or '(none)'}"
        + (f"\n\nLESSONS SO FAR:\n{lessons}" if lessons else "")
    )
    decision = llm.call_validated(config.MODEL_ACT, ACT_SYSTEM, user, Decision)
    # Hard rule, enforced in code both ways: the gate+audit decide adoption.
    if decision.decision == "ADOPT" and report.overall != "pass":
        decision = Decision(
            decision="ADJUST",
            reason="ADOPT rejected by runner: check overall is not pass",
            adjustments=report.gaps or ["address unmet criteria from check report"],
        )
    if decision.decision == "ADJUST" and report.overall == "pass":
        decision = Decision(
            decision="ADOPT",
            reason="all criteria met and gate passed; runner adopts",
            adjustments=[],
        )
    return decision


# ------------------------------------------------- FEASIBILITY (harness) ----

FEASIBILITY_SYSTEM = """You are a feasibility auditor for an autonomous agent that is repeatedly failing
at a task. Decide whether the task is PROVABLY infeasible in this environment.

INFEASIBLE requires hard environmental evidence in the history: permission
denied outside the working directory, required credentials/resources that do
not exist and cannot be created, or directly contradictory requirements.
"It keeps failing", "it's hard", or model mistakes are NOT infeasibility —
answer CONTINUE for those.

Reply with ONLY a JSON object: {"verdict": "INFEASIBLE"|"CONTINUE", "reason": str}"""


def feasibility_audit(task: str, history_digest: str, last_check: str) -> Feasibility:
    session.set_phase("AUDIT")
    user = (
        f"TASK:\n{task}\n\nHISTORY (recent cycles):\n{history_digest}\n\n"
        f"LATEST FAILURE CONTEXT:\n{last_check}"
    )
    return llm.call_validated(config.MODEL_PLAN, FEASIBILITY_SYSTEM, user, Feasibility)
