# SPEC — PDCA-LOOP: A Closed-Loop Agentic CLI with Programmatic Tool Calling (DeepSeek)


---

## 1. What this system is

A command-line agentic system. You type:

```
pdca "refactor utils.py into modules and make sure tests still pass" --loop
```

…and the system runs a **closed PDCA loop** (Plan → Do → Check → Act) autonomously until the task is verifiably done or a hard stop fires. Two execution concepts define it:

1. **PDCA as the control loop** — the Deming cycle from quality engineering, mapped onto agents:
   - **Plan** (deepseek-v4-pro): analyze the task + current state, produce a structured plan with explicit, machine-checkable success criteria.
   - **Do** (deepseek-v4-flash): execute the plan steps — via programmatic tool calling (below).
   - **Check** (deepseek-v4-pro): compare actual results against the plan's success criteria, using objective verification commands first and model judgment second.
   - **Act** (deepseek-v4-flash): decide the loop's next move — **ADOPT** (done, record lessons, exit), **ADJUST** (feed gaps back into the next Plan), or **ABANDON** (task infeasible, exit with report).
   The strong/expensive model thinks (Plan, Check); the cheap/fast model executes (Do, Act). That cost asymmetry is deliberate.

2. **Programmatic tool calling as the execution style** — instead of the classic one-JSON-tool-call-per-round-trip dance, the Do agent **writes one Python script** that calls your tool functions directly (loops, conditionals, batching, early exit). Your harness executes that script on the VM and returns **only its printed output** to the model. Intermediate data (a 2,000-line file it read, 50 grep results) never enters the model's context — only what the script chooses to `print()`. This is the context/token-saving mechanism from Anthropic's docs, re-implemented locally.

### Critical clarification (put this at the top of your CLAUDE.md too)

Anthropic's "programmatic tool calling" is a **server-side Claude API feature** (Claude's code runs in Anthropic's container). **DeepSeek has no such feature.** We are implementing the *pattern* ourselves: model writes script → our harness executes it locally → only stdout returns. Do not let Claude Code try to call any Anthropic endpoint for this; the executor is ours.

---

## 2. Why these two concepts fit together

Classic tool calling inside a PDCA loop would be slow and token-hungry: every file read, every shell command is a full model round-trip, and every result lands in context. With programmatic execution, one Do phase = one script = one round-trip, and the script filters its own output. The PDCA structure then gives that powerful-but-blunt execution step the thing it lacks: a planner that constrains it, a checker that audits it, and an actor that closes the loop. Plan tells Do *what* to do; Do returns *evidence*; Check verifies evidence against criteria; Act routes the loop.

---

## 3. Architecture

```
            ┌──────────────────────── CLI (pdca) ────────────────────────┐
            │  parse task, flags (--loop, --max-cycles, --workdir)        │
            └──────────────────────────┬─────────────────────────────────┘
                                       ▼
        ┌───────────────────────  PDCA RUNNER  ───────────────────────────┐
        │  cycle = 1..max_cycles                                           │
        │                                                                  │
        │  ① PLAN   v4-pro    task + state + last check report             │
        │            → JSON plan {steps[], success_criteria[],             │
        │                         verify_commands[]}                       │
        │                                                                  │
        │  ② DO     v4-flash  plan step(s) + toolkit docs                  │
        │            → ONE python script using toolkit functions           │
        │            → EXECUTOR runs it (sandbox, timeout)                 │
        │            → stdout (capped) = the evidence                      │
        │                                                                  │
        │  ③ CHECK  gate first: run plan.verify_commands (exit codes)      │
        │           then v4-pro: criteria × evidence × gate output         │
        │            → JSON report {criterion: met/unmet/unknown, gaps[]}  │
        │                                                                  │
        │  ④ ACT    v4-flash  check report + history summary               │
        │            → ADOPT (exit ✓) | ADJUST (→ next PLAN) | ABANDON     │
        │                                                                  │
        │  STATE.md updated every cycle (plan, evidence digest, verdicts)  │
        └──────────────────────────────────────────────────────────────────┘
        Hard stops (any phase): max_cycles, max wall-time, max total tokens
```

---

## 4. Component contracts (Claude Code implements these exactly)

### 4.1 `config.py`
- Reads `DEEPSEEK_API_KEY` from env. Hard error if missing.
- Constants: `BASE_URL = "https://api.deepseek.com"`, `MODEL_PLAN = "deepseek-v4-pro"`, `MODEL_DO = "deepseek-v4-flash"`, `MODEL_CHECK = "deepseek-v4-pro"`, `MODEL_ACT = "deepseek-v4-flash"`.
- Defaults: `MAX_CYCLES = 6`, `MAX_SECONDS = 1800`, `MAX_TOKENS_TOTAL = 400_000`, `SCRIPT_TIMEOUT = 120`, `STDOUT_CAP = 6000` chars.
- Uses the `openai` Python package (DeepSeek is OpenAI-compatible). No other LLM dependency.

### 4.2 `llm.py` — one thin wrapper
- `call(model, system, user, json_mode=False) -> (text, usage_tokens)`.
- If `json_mode`, request JSON output and parse; on parse failure, retry ONCE with the parse error appended. Second failure raises.
- Accumulates a global token counter (for the hard stop and the final cost line).

### 4.3 `toolkit.py` — the functions Do-scripts may call
The injected "standard library" for model-written scripts. Keep to five:
- `read(path) -> str`
- `write(path, content) -> str`
- `run(cmd, timeout=60) -> dict{code, out, err}` — shell on the VM, cwd = workdir
- `ls(path=".") -> list[str]`
- `note(text)` — appends a line to the cycle's evidence log (so scripts can record findings without polluting prints)

Rules: all paths resolved under `--workdir` (default `./work`); reject escapes. `run` always has a timeout. These functions are *real Python functions*, documented in a docstring block that gets pasted into the Do prompt verbatim — that docstring is the model's only API reference, so write it carefully (signatures + one-line examples).

### 4.4 `executor.py` — runs model-written scripts (the heart of the pattern)
- Input: a Python source string from the Do agent.
- Strip markdown fences if present.
- Execute it in a separate process: write to a temp file, run `python temp.py` with `subprocess` (cwd = workdir, timeout = `SCRIPT_TIMEOUT`), with the toolkit importable (simplest: the temp file is prefixed with `from toolkit import read, write, run, ls, note` and `toolkit.py` is on the path).
- Capture stdout + stderr. Return `{ok, stdout[:STDOUT_CAP], stderr[:2000], elapsed}`.
- A non-zero exit or exception is NOT fatal to the loop — it becomes evidence for Check.
- Separate-process execution (not `exec()` in-harness) is required: it gives you the timeout kill, isolates crashes, and keeps harness state clean.

### 4.5 `phases.py` — the four phase functions
Each phase = one prompt + one `llm.call` + parsing. Their I/O:

**plan(task, state, last_check) -> Plan** — JSON with:
```
{ "objective": str,
  "steps": [ {"id": 1, "instruction": str} ],           # ≤ 5 steps
  "success_criteria": [ str ],                           # each independently checkable
  "verify_commands": [ str ] }                           # shell cmds whose exit codes gate Check
```
Prompt rules: criteria must be observable/testable, not vibes ("tests in tests/ pass via pytest -q", not "code is clean"). If the task itself is ambiguous, the plan's first step is to gather the missing facts via a Do script (inspect files), not to ask the user.

**do(plan, state) -> Evidence** — prompt = plan steps + toolkit docstring + the three script rules:
1. Write ONE complete Python script. No prose outside the code block.
2. `print()` ONLY conclusions, summaries, and small excerpts — never dump whole files or raw command output (the harness caps stdout; wasted prints = lost evidence).
3. Handle your own errors: check `run(...)["code"]`, print what failed and why.
Then call the executor. Evidence = `{script, stdout, stderr, ok}`.

**check(plan, evidence) -> Report** — TWO layers, in order:
1. **Objective gate (no model):** run every `plan.verify_commands` via the executor's `run`; collect exit codes + tails of output.
2. **Model audit (v4-pro):** given criteria + evidence + gate results, return JSON `{criteria: [{text, status: met|unmet|unknown, reason}], gaps: [str], overall: pass|fail}`. Rule in the prompt: a criterion whose verify command failed can never be `met`, whatever the prose evidence says. (The gate outranks the narrative — this is your maker/checker lesson applied to PDCA.)

**act(report, cycle, history_digest) -> Decision** — JSON `{decision: ADOPT|ADJUST|ABANDON, reason, adjustments: [str]}`.
- ADOPT only if `report.overall == pass`. (Enforce in code, not just in the prompt.)
- ADJUST: `adjustments` become input to the next Plan.
- ABANDON allowed only with a concrete infeasibility reason (missing credentials, contradictory requirements), not "it's hard".

### 4.6 `state.py` — `STATE.md` in the workdir
Per cycle, append: timestamp, plan digest (objective + criteria), evidence digest (first ~10 lines of stdout), check verdicts, act decision, lessons. Loaded (last ~2 cycles only) into Plan and Act prompts. The model forgets; the file doesn't — and capping what's loaded keeps the loop's own context small, which is the whole theme of this build.

### 4.7 `cli.py` / entry point
```
pdca "task text"                 # one full PDCA cycle, then stop and print the check report
pdca "task text" --loop          # the /pdca-loop mode: cycle until ADOPT/ABANDON/hard stop
   [--max-cycles 6] [--max-seconds 1800] [--workdir ./work]
```
Console output per cycle: one line per phase (`[PLAN] 3 steps, 2 criteria`, `[DO] script ran 4.2s, ok`, `[CHECK] 1/2 criteria met`, `[ACT] ADJUST: tests still failing`). Final line always: cycles used, outcome, total tokens.

---

## 5. Hard rules (encode in code, not only in prompts)

1. **ADOPT requires a passing gate.** The Act model cannot declare success if any verify command failed — check `report.overall` in the runner before honoring ADOPT.
2. **Every loop has three hard stops** — cycles, wall-time, total tokens. Whichever trips first ends the run with a clear `STOPPED:` reason.
3. **Only stdout enters context.** Never feed the Do script's source or raw tool data back to Check/Plan beyond the capped stdout + gate outputs. This discipline IS the programmatic-tool-calling benefit; break it and you've rebuilt the expensive thing.
4. **JSON phases validate or retry once, then fail loudly.** No silently proceeding with a half-parsed plan.
5. **Model-written code runs with real VM permissions — treat that as the design fact it is.** You accepted this (it's a VM), so: dedicated workdir, no secrets in the VM's env beyond the API key, snapshot the VM before first run, and never point `--workdir` at anything you can't lose.

---

## 6. Build order — what to tell Claude Code at each phase

Work in a repo containing this SPEC.md and a CLAUDE.md that says: *"Implement strictly per SPEC.md. Plain Python + the openai package only. No frameworks (no LangChain etc). Ask before adding any dependency. Small files matching the component names in §4."*

- **Phase 1 — plumbing:** "Implement config.py and llm.py per SPEC §4.1–4.2. Then a smoke test: one call to v4-flash printing the reply and token usage." *(Verify: it runs; you see usage numbers.)*
- **Phase 2 — toolkit + executor:** "Implement toolkit.py and executor.py per §4.3–4.4. Test by executing this hand-written script string: one that reads a file, runs `echo hi`, prints a summary. Also prove the timeout kills a `sleep 999` script and a path escape is rejected." *(This phase is the programmatic-calling pattern, isolated. Understand it fully before continuing.)*
- **Phase 3 — Do phase end-to-end:** "Implement the do() phase per §4.5. Hardcode a one-step plan ('count .py files and report the largest'). Show me the generated script and its stdout." *(Verify: the model writes a sensible script; only its prints come back.)*
- **Phase 4 — Plan + Check:** "Implement plan() and check() per §4.5, including the objective gate running verify_commands before the model audit." *(Test on a repo with a failing test: the gate must report fail even if stdout sounds confident.)*
- **Phase 5 — Act + runner + CLI:** "Implement act(), the PDCA runner with all three hard stops, state.py, and the CLI per §4.6–4.7 and §5."
- **Phase 6 — the two acceptance tests (below).** Fix until both pass.

## 7. Acceptance tests (definition of done)

1. **Convergence:** seed the workdir with a small Python project containing one buggy function + a failing pytest. Run `pdca "make all tests pass" --loop`. Expected: Plan emits criteria + `pytest -q` as a verify command → Do edits → Check gates → if green, Act ADOPTs. Should converge in 1–3 cycles.
2. **Safe non-convergence:** run `pdca "make tests pass in /nonexistent" --loop --max-cycles 3`. Expected: no crash, no infinite loop — either a reasoned ABANDON or `STOPPED: max cycles`, with STATE.md telling the story.

Bonus check while watching test 1: confirm token counts per cycle stay modest even when Do reads large files — if they don't, something is leaking raw data into context (violating rule 3).

---

## 8. What you get at the end

A CLI where `pdca "task" --loop` runs a genuinely closed loop: an expensive model that plans with checkable criteria, a cheap model that executes whole batches of tool work in single scripts (the programmatic pattern — one round-trip where classic tool calling needs ten, and only conclusions entering context), an objective gate plus expensive auditor that can't be sweet-talked, and an actor that either ships, adjusts, or abandons — all under hard stops, with a state file as its memory. Conceptually you'll have implemented, by hand and on a non-Anthropic API: context engineering via output filtering, model-tier routing by cognitive role, the maker/checker split (Do's flash vs Check's pro + gate), and a classical control loop (PDCA) as agent architecture. That combination is precisely the "design the system that prompts" skill — and it's a strong portfolio piece with a clear story: *"I re-implemented Anthropic's programmatic tool calling pattern on DeepSeek and wrapped it in a PDCA controller."*
