# What Each Phase Is Told — Prompt Reference

A faithful walkthrough of what the agent receives at every phase of the PDCA
loop. Source of truth: `pdca/core/phases.py` (prompts + schemas),
`pdca/tools/toolkit.py` (tool docs + `AGENT_PROTOCOL`), and
`pdca/execution/executor.py` (`ToolContext`, which actually runs the DO/PROBE
tool calls). Keep this in sync when the prompts change.

---

## Two questions worth answering up front

**Is DO one script?** No — not anymore. DO is a **turn-based agent**. Each turn it
issues one or more tool calls (`read/write/run/ls/note`); the harness runs them,
feeds the results back, and the model decides the next action. It loops until it
calls `finish()` or hits a turn cap (`MAX_DO_TURNS`). Writing a script and running
it is just one option (`write` + `run`) it may pick for a repetitive/bulk subtask —
distinct deliverable files are normally separate `write` calls, one per file. The
old "one Python orchestration script per cycle" model is gone, along with its
helper-script and in-cycle-repair rules (errors are now visible each turn).

**Is it Python specifically?** No. The tools are language-agnostic actions; via
`run()` the agent can shell out to **anything** ("Any command is permitted,
including sudo"), and any script it chooses to write can be in whatever language
fits the subtask (python, bash, …). The prompt explains programmatic tool-calling
as an *option*, biasing neither toward nor away from it.

---

## The tools DO and PROBE may call

Injected into the DO and PROBE system prompts (`TOOLKIT_DOCS`). They are dispatched
in-process by `executor.ToolContext`, one turn at a time (no subprocess wrapping the
whole agent):

- `read(path) -> str` — full text of a file (relative to workdir).
- `write(path, content) -> str` — overwrite/create a file (creates parent dirs);
  for `.py` files the result includes a syntax-check verdict.
- `run(cmd, timeout=120) -> str` — run any shell command (sudo / outside-workdir
  paths permitted); cwd is the task workdir, the workdir venv is on PATH; hard
  timeout; returns `exit <code>` + output tail.
- `ls(path=".") -> str` — list directory entries.
- `note(text) -> str` — append a line to the cycle's evidence log.
- `finish(summary) -> str` — the explicit DONE signal; `summary` is carried into the
  next cycle. The agent calls this — and only this — when the work is genuinely done.

Each turn is a JSON object `{"thought", "calls":[{"tool","args"}]}` (the
`AGENT_PROTOCOL`). Tool results and the transcript are capped (`TOOL_OUTPUT_CAP`,
`STDOUT_CAP`). UNRESTRICTED MODE is active: no workdir jail.

---

## PLAN  (model: pro / `MODEL_PLAN`)

**Persona:** "a senior engineer who owns what 'done' means. Done is the user's
actual desired outcome — never the cheapest artifact that makes a check turn
green."

**Must output JSON:** `task_type` (`build_fix` | `produce_analyze`), `analysis`
(reason about current state; if a prior cycle failed, name the exact error /
file / line and root cause), `strategy`, `objective`, `steps` (≤7, each
detailed/self-contained), `success_criteria` (objective floor), `verify_commands`
(shell; exit 0 = pass), `quality_criteria` (1–5; each `text` + `rationale` = the
real bar).

**Key rules it is given:**
- Steps must be detailed and self-contained — the executor sees only the plan,
  not the conversation.
- `success_criteria` are the FLOOR, not the bar: task-level (never a sub-goal like
  "bug diagnosed"), minimal (1–4), each directly shell-checkable.
- `quality_criteria` are THE BAR: derived from "if I handed this back, would the
  user consider their goal met?"; a concrete observable property of the RESULT;
  must NOT restate success_criteria; judged by a reviewer, not a shell; **never
  weakened** across cycles (only refined stricter).
- `verify_commands` check STRUCTURAL/FUNCTIONAL facts only and must **never grep a
  human-readable deliverable for words/phrases** (that is reward hacking); use
  only pre-installed tools; never reference a `.py` file written this cycle;
  `python -c` commands must be single-expression.
- Numeric-formatting guidance (`f"{x:g}"`, derive expected test strings from
  actual behavior).
- Never re-issue a plan that already failed in the same form.

**Conditionally appended to the user message:**
- `RETHINK_BLOCK` — after 3 stuck cycles (same failure signature): demand a
  materially different approach; step 1 must be diagnosis.
- `QUALITY_STALL_BLOCK` — after 2 cycles with no gain in quality-met count: stop
  tweaking checks/wording, consolidate to the single best artifact, add real
  substance.

**User message contains:** the task, the last-cycles state digest, lessons /
failed approaches, previous-cycle failure context, and either block above when
triggered.

---

## DO  (model: flash / `MODEL_DO`; pro on rethink or quality-stall cycles)

**Persona:** "a meticulous engineer who takes quality seriously and does the real
work." A turn-based agent (no single script).

**User message gives it:** the ORIGINAL USER TASK, the full definition of done
(success criteria = floor, quality criteria = bar), the objective/strategy/steps,
the verify commands, the state digest, and **the best prior cycle's `finish`
summary + its last error** to build on (the workdir itself persists, so it
re-inspects the real files each cycle).

**Told to:**
- Work toward ALL of the definition of done (success AND quality), not just this
  cycle's steps.
- Iterate across cycles: inspect the workdir first, **build on prior work, fix what
  failed, keep what worked — do not rewrite from scratch** unless the approach is
  fundamentally wrong (and say why).
- Decide per subtask whether to act directly (`write`/`read` a few distinct files,
  targeted edits) or to **automate** (a repetitive/bulk/computational subtask → write
  a script in whatever language fits and `run` it). Neither is required or forbidden.
- ACT, don't just inspect; full-file writes that **never drop existing list entries**;
  check the `write` syntax verdict for `.py` files. The venv is on PATH — call
  `python`/`pip`/`pytest` directly; never `source`, never `./venv/bin/python`.
- Verify your own work with `run` as you go; **not required to end on green**, but
  call `finish` only when the work is genuinely complete.
- **Never game a check** — no hardcoded/canned/templated output, no special-casing
  inputs, no trivial/degenerate setups; output is independently reviewed for this.

**Loop control (code, not prompt):** the agent loops until it calls `finish` or
hits `MAX_DO_TURNS`, then the harness hands off whatever exists. Tool errors are
returned inline (visible the next turn), so the old one-shot crash-repair step is
gone. An end-of-loop syntax sweep of workdir `.py` files surfaces broken files as
evidence.

---

## PROBE  (model: pro / `MODEL_CHECK`; **on-demand — runs only when CHECK requests it**)

**Persona:** "a skeptical reviewer who trusts nothing the execution agent
claimed." A turn-based verification agent (no single script). Conditional: it runs
only when CHECK's first pass sets `request_probe` (see below), and it is handed
CHECK's `probe_reason` — what to verify. DO never decides whether it is audited.

**Told to (across as many turns as it needs):**
- Create its OWN fresh fixtures under `.pdca_probe/` and actually RUN the built
  artifact; check real outputs against requirements.
- Review code for stubs / hardcoded values / swallowed excepts / tests with no
  asserts, and specifically for **GAMED work** (templated output not from real
  computation, hardcoded answers, degenerate setups) — violations, not opinions.
- Never modify project files; only create fixtures under `.pdca_probe/`.
- End by calling `finish(summary)` where the summary carries one
  `CRITERION n: OK|VIOLATED|UNCLEAR — <evidence>` line per success criterion plus
  any gamed-work findings; certify on inspected output, never on file existence.

**Known gap (unchanged):** PROBE's user message is given the **success_criteria**
and CHECK's `probe_reason`, not the full quality_criteria. Candidate fix: also show
the probe the quality criteria so it gathers evidence against the bar.

---

## CHECK  (model: pro / `MODEL_CHECK`)

**Persona:** "a demanding quality auditor and the user's advocate." Judges the
OBJECTIVE FLOOR and the QUALITY BAR against the user's actual outcome.

**User message gives it:** the original task, success_criteria, quality_criteria
(with rationale), the objective-gate results (commands + exit codes), the
execution evidence (capped transcript/last-error/notes), and — **only when a probe
was requested** — the probe report.

**Must output JSON:** a verdict per `criteria`, a verdict per `quality`, `gaps`,
`overall`, plus **`request_probe` (bool) + `probe_reason` (str)**.

**Two-pass, on-demand PROBE (the user-chosen design):** CHECK runs every cycle on
DO's evidence first, with no probe. If it cannot confidently verify the outcome
from the evidence + gate alone, or it suspects gaming/Goodhart, it sets
`request_probe=true` and writes in `probe_reason` exactly what to verify. The
runner then runs PROBE and **re-invokes CHECK** with the probe report to finalize;
on that second pass `request_probe` is forced false in code (never loops). When the
gate + evidence already settle the outcome, no probe runs.

**Told to:**
- A non-zero verify command can NEVER be "met"; the gate outranks the narrative.
- Run its OWN reward-hacking audit (fabricated data, templated reports, stubs,
  string-match tricks, Goodhart) in ADDITION to any probe.
- Judge quality by **SUBSTANCE, never keyword presence** — keyword-stuffed but
  shallow = unmet; missing phrase but substance present = still met.
- A deliverable can pass every command and still fail the quality bar.

**Backstop enforced in code (not the prompt), in `check()`:**
- any gate command non-zero → `overall = fail`;
- gate green AND every quality criterion met → `overall = pass` (and unmet
  criteria are upgraded, since the gate verified them);
- gate green but quality unmet → `overall = fail` (the Goodhart fix — green shell
  is necessary, never sufficient).

---

## ACT  (model: flash / `MODEL_ACT`)

**Persona:** "the delivery lead." Outputs `decision` (`ADOPT`|`ADJUST`), `reason`,
`adjustments`.

**Told to:** ADOPT only if `overall=pass` and the original task is genuinely
complete — never on a green floor while quality criteria remain unmet; otherwise
ADJUST, turning the gaps (objective AND quality) into concrete next-plan
adjustments. Giving up is not available to it.

**Enforced in code both ways:** can't ADOPT when `overall != pass`; auto-ADOPTs
when `overall == pass`.

---

## FEASIBILITY  (model: pro / `MODEL_PLAN`; harness-triggered only)

Runs only after repeated failure (stuck-ladder / backstop). Decides `INFEASIBLE`
vs `CONTINUE`. Told that "it keeps failing" / "it's hard" / model mistakes are NOT
infeasibility — only hard environmental blockers count (permission denied outside
the workdir, missing credentials/resources that cannot be created, directly
contradictory requirements). Giving up is a harness decision, never a phase
model's.

---

## Model routing (current, `config.py`)

- PLAN: pro · DO: flash (pro on rethink/quality-stall) · PROBE: pro · CHECK: pro ·
  ACT: flash · FEASIBILITY: pro.
- DO and PROBE now make **multiple** model calls per cycle (one per turn, up to
  `MAX_DO_TURNS` / `MAX_PROBE_TURNS`), so their per-cycle cost/latency scales with
  the number of turns rather than a single decode. PLAN/CHECK/ACT/FEASIBILITY stay
  single-shot JSON calls; CHECK can fire twice in a cycle (pass 1, then pass 2 after
  a probe). PROBE only runs in cycles where CHECK requests it.
- Both DeepSeek v4 models reason by default; reasoning cannot be disabled via the
  API. Latency is dominated by output-token decode (~50–70 tok/s).
