"""The PDCA phase functions.

Agentic design notes (v2):
- Plan reasons explicitly (analysis + strategy) and writes detailed steps.
- Do repairs its own crashed script once within the cycle.
- Check = objective gate + an agentic PROBE (the pro model writes its own
  functional-verification script and we run it) + the model audit.
- Act can only ADOPT or ADJUST. Giving up is not a model decision: the
  harness stops on budget, or on a harness-initiated feasibility audit.
"""
import json
import subprocess
from typing import Literal

from pydantic import BaseModel, Field

from pdca import config, executor, llm
from pdca.toolkit import TOOLKIT_DOCS


class Step(BaseModel):
    id: int
    instruction: str


class Plan(BaseModel):
    analysis: str
    strategy: str
    objective: str
    steps: list[Step] = Field(max_length=7)
    success_criteria: list[str]
    verify_commands: list[str]


class CriterionVerdict(BaseModel):
    text: str
    status: Literal["met", "unmet", "unknown"]
    reason: str


class Report(BaseModel):
    criteria: list[CriterionVerdict]
    gaps: list[str]
    overall: Literal["pass", "fail"]


class Decision(BaseModel):
    decision: Literal["ADOPT", "ADJUST"]
    reason: str
    adjustments: list[str] = []


class Feasibility(BaseModel):
    verdict: Literal["INFEASIBLE", "CONTINUE"]
    reason: str


# ---------------------------------------------------------------- PLAN ----

PLAN_SYSTEM = """You are the PLAN phase of an autonomous PDCA agent operating on files in a working directory.
Reason first, then plan. Produce a JSON object:
{"analysis": str,    // REASON HERE: what is the current state? If a previous cycle
                     // failed, name the EXACT failure (error text, file, line) and
                     // explain the root cause. Be thorough — several sentences.
 "strategy": str,    // The approach for THIS cycle and, if previous attempts failed,
                     // why this strategy will succeed where they did not.
 "objective": str,
 "steps": [{"id": 1, "instruction": str}, ...],   // at most 7 steps
 "success_criteria": [str, ...],                   // each independently checkable
 "verify_commands": [str, ...]}                    // shell cmds; exit 0 = success

Rules:
- Step instructions must be DETAILED and self-contained: name exact files,
  functions, and changes; include the relevant error text the executor must fix.
  Multi-sentence instructions are good. The execution agent sees only your plan,
  not the conversation.
- success_criteria define when the WHOLE USER TASK is complete — never just this
  cycle's sub-goal. The loop ADOPTs (terminates successfully) the moment all
  criteria pass, so criteria like "test output was captured" or "the bug was
  diagnosed" are forbidden: even on a diagnostic cycle, the criteria remain the
  task-level checks (which will simply fail until the task is truly done).
- success_criteria must be observable facts, never vibes. Keep them minimal (1-4)
  and make EVERY criterion directly checkable by one of verify_commands.
- Each cycle should make real progress toward the criteria: diagnose AND fix in
  the same cycle whenever possible, rather than spending a cycle only looking.
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


def plan(task: str, state_digest: str, last_check: str, lessons: str,
         rethink_count: int = 0) -> Plan:
    user = f"TASK:\n{task}\n\nSTATE (last cycles digest):\n{state_digest or '(first cycle)'}"
    if lessons:
        user += f"\n\nLESSONS / FAILED APPROACHES (do not repeat these):\n{lessons}"
    if last_check:
        user += f"\n\nPREVIOUS CYCLE FAILURE CONTEXT:\n{last_check}"
    if rethink_count:
        user += RETHINK_BLOCK.format(n=rethink_count)
    return llm.call_validated(config.MODEL_PLAN, PLAN_SYSTEM, user, Plan)


# ------------------------------------------------------------------ DO ----

DO_SYSTEM = f"""You are the DO phase of an autonomous PDCA agent. You execute a plan by writing ONE Python script.
The script runs on the host machine with these pre-imported functions:

{TOOLKIT_DOCS}

Rules:
0. RUN VERIFY COMMANDS FIRST. Before changing anything, run each of the plan's
   verify commands via run(). If ALL of them exit 0, the work is already done —
   print the passing results and stop immediately without editing any files.
   Only proceed to make changes if at least one verify command fails.
1. Write ONE complete Python script. No prose outside the code block.
2. print() ONLY conclusions, summaries, and small excerpts — never dump whole
   files or raw command output. stdout is capped; wasted prints = lost evidence.
3. Handle your own errors: check run(...)["code"], print what failed and why.
4. ACT, don't just inspect: actually make the changes the plan asks for in this
   script. When editing a file: read() it, change only what the plan requires,
   and write() the FULL file back, preserving all unrelated content exactly.
   CRITICAL: Never remove existing list entries (format lists, handlers, test cases,
   supported values) unless the plan explicitly says to remove them. Dropping items
   from a working list causes silent regressions that are hard to diagnose.
5. write() returns a syntax-check verdict for .py files — print it, and if it
   reports SYNTAX ERROR, fix and re-write the file in this same script.
6. NEVER write helper/validation scripts (validate_*.py, check_*.py, test_manual.py,
   etc.) to the working directory — they become lint targets and will break ruff.
   To verify functionality inline, use: run('python -c "from mod import fn; ..."').
   In test files, do NOT add `import pytest` unless the test code explicitly uses
   `pytest.raises`, `pytest.mark`, `pytest.fixture`, or other `pytest.*` names.
   Plain `assert`-based tests need no pytest import — ruff will flag it as F401.
7. Keep the script simple and linear.
   Finish by running the plan's verify command(s) via run() and printing exit codes.
   If a verify command fails: read the error output, identify the SPECIFIC failure
   (e.g. which test failed and why, which line), fix the root cause in THIS same
   script, then run the verify command once more and print the final result.
   Do this at most ONCE per verify command — if the second run still fails, print
   the final failing output and stop. Never guess at a fix without reading the error.
Reply with a single ```python code block."""

REPAIR_USER = """Your previous script crashed before finishing. Here is the script and the error.

SCRIPT:
```python
{script}
```

STDERR:
{stderr}

Return the corrected COMPLETE script (one ```python block, same rules)."""


def do(plan_obj: Plan, state_digest: str, workdir: str, evidence_log: str,
       model: str | None = None) -> dict:
    model = model or config.MODEL_DO
    steps = "\n".join(f"{s.id}. {s.instruction}" for s in plan_obj.steps)
    user = (
        f"OBJECTIVE: {plan_obj.objective}\n\nSTRATEGY: {plan_obj.strategy}\n\n"
        f"PLAN STEPS:\n{steps}\n\n"
        f"SUCCESS CRITERIA (what your evidence must speak to):\n"
        + "\n".join(f"- {c}" for c in plan_obj.success_criteria)
        + (f"\n\nSTATE DIGEST:\n{state_digest}" if state_digest else "")
        + "\n\nWrite the script now."
    )
    script_text, _ = llm.call(model, DO_SYSTEM, user)
    result = executor.execute(script_text, workdir, evidence_log)
    result["attempts"] = 1
    # In-cycle repair: a crashed or empty do-script costs one cheap retry, not a cycle.
    if not result["ok"]:
        err = result["stderr"] or result["stdout"] or "(no output — script may be empty or exited immediately)"
        repair = REPAIR_USER.format(script=executor.strip_fences(script_text),
                                    stderr=err[-1500:])
        script_text, _ = llm.call(model, DO_SYSTEM, repair)
        result = executor.execute(script_text, workdir, evidence_log)
        result["attempts"] = 2
    result["script"] = executor.strip_fences(script_text)
    return result


# --------------------------------------------------------------- CHECK ----

def run_gate(verify_commands: list[str], workdir: str) -> list[dict]:
    """Objective gate: run each verify command, no model involved."""
    results = []
    for cmd in verify_commands:
        try:
            p = subprocess.run(cmd, shell=True, cwd=workdir,
                               capture_output=True, text=True, timeout=120)
            results.append({"cmd": cmd, "code": p.returncode,
                            "tail": (p.stdout + p.stderr)[-2000:]})
        except subprocess.TimeoutExpired:
            results.append({"cmd": cmd, "code": -1, "tail": "timeout after 120s"})
        except OSError as e:
            results.append({"cmd": cmd, "code": -1, "tail": f"failed to run: {e}"})
    return results


PROBE_SYSTEM = f"""You are the hands-on verifier of a PDCA agent's CHECK phase: a skeptical reviewer
who trusts nothing the execution agent claimed. Write ONE Python script that
INDEPENDENTLY verifies the success criteria, using these pre-imported functions:

{TOOLKIT_DOCS}

Your script must:
1. Create fresh sample inputs of your own under ".pdca_probe/" and ACTUALLY RUN
   the built artifact on them via run("python -c 'from module import fn; ...'").
   Check the actual outputs against what the task requires.
2. Review the code briefly: read() key files; print short excerpts ONLY of
   concrete problems — stubs that return None/pass, hardcoded magic values,
   swallowed bare `except:`, tests with no assert statements.
3. Print one line per success criterion:
   "CRITERION n: OK|VIOLATED|UNCLEAR — <concrete observed evidence>"
   - OK: you ran the artifact and the output matched requirements.
   - VIOLATED: you ran the artifact and the output was WRONG (print the actual vs expected).
   - UNCLEAR: the artifact crashed or you couldn't get real output.
   Style preferences, missing error-handling for hypothetical edge cases, or
   code review opinions do NOT count as VIOLATED — only functional failures do.
   In Python, int/float interchangeability (3.0 == 3) is NOT a bug; only flag
   type issues when the task explicitly requires a specific type.
4. NEVER modify or delete project files; only create fixtures under ".pdca_probe/".
5. print() only conclusions and small excerpts; stdout is capped.
Reply with a single ```python code block."""


def probe(plan_obj: Plan, workdir: str, evidence_log: str) -> dict:
    user = (
        f"OBJECTIVE: {plan_obj.objective}\n\nSUCCESS CRITERIA:\n"
        + "\n".join(f"{i+1}. {c}" for i, c in enumerate(plan_obj.success_criteria))
        + "\n\nWrite the verification script now."
    )
    script_text, _ = llm.call(config.MODEL_CHECK, PROBE_SYSTEM, user)
    result = executor.execute(script_text, workdir, evidence_log)
    result["script"] = executor.strip_fences(script_text)
    return result


CHECK_SYSTEM = """You are the CHECK phase of a PDCA agent: an auditor comparing evidence against success criteria.
You receive: the plan's criteria, the execution agent's evidence (capped
stdout/stderr/notes), the OBJECTIVE GATE results (verify commands + exit codes),
and an independent PROBE report (a functional verification script's findings).

Hard rules:
- A criterion whose verify command exited non-zero can NEVER be "met", no matter
  how confident the prose evidence sounds. The gate outranks the narrative.
- A criterion directly verified by a gate command that exited 0 IS "met" unless
  the gate output, probe, or evidence contradicts it. Reserve "unknown" for
  criteria that nothing speaks to.
- The probe outranks the execution agent's claims: if the probe found stubs,
  hardcoded answers, or functional failures, say so in gaps even when tests pass.
- gaps must be CONCRETE and actionable: quote the exact error text, file and
  line where known — the next plan is built directly from your gaps.

You also receive the ORIGINAL USER TASK. "overall" judges the TASK, not the
cycle: "pass" requires that every criterion is met, every gate command exited 0,
AND the evidence shows the original task as a whole is complete. If the plan's
criteria only cover a sub-goal (e.g. diagnosis), list what the task still needs
in gaps and return "fail".

Reply with ONLY a JSON object:
{"criteria": [{"text": str, "status": "met"|"unmet"|"unknown", "reason": str}, ...],
 "gaps": [str, ...],
 "overall": "pass"|"fail"}"""


def check(task: str, plan_obj: Plan, evidence: dict, probe_ev: dict,
          workdir: str, pre_gate: list[dict] | None = None) -> tuple[Report, list[dict]]:
    gate = pre_gate if pre_gate is not None else run_gate(plan_obj.verify_commands, workdir)
    user = (
        f"ORIGINAL USER TASK:\n{task}\n\n"
        "SUCCESS CRITERIA:\n" + "\n".join(f"- {c}" for c in plan_obj.success_criteria)
        + "\n\nOBJECTIVE GATE RESULTS:\n" + json.dumps(gate, indent=1)
        + f"\n\nEXECUTION EVIDENCE:\nscript exit ok: {evidence['ok']}\n"
        + f"stdout:\n{evidence['stdout']}\n"
        + (f"stderr:\n{evidence['stderr']}\n" if evidence["stderr"] else "")
        + (f"notes:\n{evidence['notes']}\n" if evidence.get("notes") else "")
        + f"\nPROBE REPORT (independent functional verification):\nprobe ran ok: {probe_ev['ok']}\n"
        + f"{probe_ev['stdout'][:3000]}\n"
        + (f"probe stderr:\n{probe_ev['stderr'][:600]}\n" if probe_ev["stderr"] else "")
    )
    report = llm.call_validated(config.MODEL_CHECK, CHECK_SYSTEM, user, Report)
    # Belt and braces: the gate is authoritative in both directions, not just prompt.
    if any(g["code"] != 0 for g in gate) and report.overall == "pass":
        report.overall = "fail"
        report.gaps.append("a verify command exited non-zero; overall forced to fail")
    elif all(g["code"] == 0 for g in gate) and report.overall == "fail":
        # All gate commands passed — success criteria are verified by objective evidence.
        # The CHECK model cannot override gate results: if every verify command exited 0,
        # the task is complete. Probe findings become gaps for future runs, not blockers.
        report.overall = "pass"
        for c in report.criteria:
            if c.status != "met":
                c.status = "met"
                c.reason += " [runner override: verify command exited 0]"
    return report, gate


# ----------------------------------------------------------------- ACT ----

ACT_SYSTEM = """You are the ACT phase of a PDCA agent. Decide the loop's next move from the check report.
Reply with ONLY a JSON object:
{"decision": "ADOPT"|"ADJUST", "reason": str, "adjustments": [str, ...]}

- ADOPT only if the check report's overall is "pass" AND the report shows the
  ORIGINAL USER TASK fully complete — never for a completed sub-goal.
- Otherwise ADJUST. Giving up is not available to you: the harness manages
  budgets and infeasibility. Your job is to convert the report's gaps into
  the most useful concrete adjustments for the next Plan — name files, errors,
  and the specific change of approach if the current one keeps failing."""


def act(task: str, report: Report, cycle: int, history_digest: str,
        lessons: str) -> Decision:
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
    user = (
        f"TASK:\n{task}\n\nHISTORY (recent cycles):\n{history_digest}\n\n"
        f"LATEST FAILURE CONTEXT:\n{last_check}"
    )
    return llm.call_validated(config.MODEL_PLAN, FEASIBILITY_SYSTEM, user, Feasibility)
