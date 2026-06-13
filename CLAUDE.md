# CLAUDE.md

**Critical clarification:** Anthropic's "programmatic tool calling" is a
server-side Claude API feature (Claude's code runs in Anthropic's container).
DeepSeek has no such feature. This repo implements the *pattern* locally:
the Do model writes a Python script → `pdca/executor.py` runs it in a
subprocess on this machine → only its capped stdout returns to the model.
Never call any Anthropic endpoint for this; the executor is ours.

Implement strictly per `Project_Spec.md`. Small files matching the component
names in spec §4 (`config.py`, `llm.py`, `toolkit.py`, `executor.py`,
`phases.py`, `state.py`, `runner.py`, `cli.py`), plus `session.py` (per-run
logging) and `scheduler.py` (cron jobs) added for the v3 features below.

Dependencies (per owner's instruction, superseding the spec's plain-python
note): `openai` (DeepSeek is OpenAI-compatible), `pydantic` (phase schemas +
validate-or-retry), `typer` (CLI), `python-dotenv`, `python-crontab` (scheduling).
No agent frameworks — the PDCA loop is a plain `while` loop in `runner.py`; hard
rules (ADOPT requires a passing gate, stdout-only context) are enforced in code.

**v2 harness design (supersedes spec §4.5/§5 where they conflict):**
- Giving up is NEVER a model decision. Act only ADOPTs or ADJUSTs; the spec's
  ABANDON is removed. Runs end on the token budget (`--max-tokens`, primary),
  wall-time, an optional `--max-cycles` (default unlimited), or a
  harness-initiated feasibility audit that stops only on proven environmental
  infeasibility (permissions, missing resources) after repeated identical
  failures.
- Stuck ladder in `runner.py`: same failing verify-commands 3 cycles in a row →
  Plan forced into RETHINK mode (materially different strategy required) and Do
  escalated to the pro model; 5 in a row → feasibility audit.
- Plan reasons explicitly (`analysis` + `strategy` JSON fields) and gets a rich
  failure pack on every failed cycle: full check report, gate output tails,
  do-script stderr, probe findings, plus a deduplicated lessons ledger.
- Check is three layers: objective gate (verify commands, no model) → PROBE
  (the pro model writes its own functional-verification script — fresh inputs
  under `.pdca_probe/`, actually runs what was built, reviews code for stubs) →
  model audit. Gate and probe outrank the Do agent's claims.
- Harness assists: `toolkit.write()` auto-syntax-checks `.py` files and reports
  in its return value; a crashed Do script gets one in-cycle repair attempt.

**v3 features (session logging, unrestricted execution, scheduling):**
- `session.py` — every invocation opens ONE session dir under
  `~/.pdca_agent/sessions/run_session_<date>_<time>_<8hex>/` and is the single
  source of truth: `session.log` (timeline), `raw/<cycle>_<phase>_<seq>.json`
  (verbatim prompt + raw completion + model/tokens/latency for every call),
  `scripts/` (each Do/Probe script as executed), `meta.json` (task, workdir,
  trigger, start/end, outcome, exit code, tokens, cycles). Console output is
  UNCHANGED — the runner's `_emit` writes to both; `meta.json` is finalised in a
  `finally`. The capture point is the single `llm._completion` chokepoint, so the
  module keeps a null-safe `current` Session and phases tag their phase, the
  runner tags the cycle. NB: the facade fn is `session.start` (not `open`) — a
  module-level `open` would shadow the builtin used for file writes.
- Unrestricted execution: `config.UNRESTRICTED` (default True). The executor
  passes `PDCA_UNRESTRICTED` into the Do/Probe subprocess; `toolkit._resolve`
  drops the workdir jail when set (relative paths still resolve to the workdir,
  absolute paths honored), and `run()` has no allowlist (sudo allowed). KEEP the
  non-restriction rails: per-command timeout, stdout cap, harness stops. Banner
  `⚠ UNRESTRICTED MODE — full system access` prints at startup. Flip the flag to
  re-enable the jail without code surgery.
- Scheduling is NOT a recurring loop: the PDCA loop is goal-terminating, so each
  cron wake-up launches a FRESH full `pdca` run with a stored prompt (one wake-up
  = one session). `scheduler.py` stores jobs in `~/.pdca_agent/schedules.json` and
  manages the user crontab via `python-crontab`, tagging each line `# pdca:<name>`
  for idempotency (replace, never duplicate; never touch unrelated lines). The
  line uses the absolute venv `pdca`, prefixes the venv on `PATH`, cds to the
  workdir, redirects to `~/.pdca_agent/cron/<name>.log`, and calls the hidden
  `pdca _run-scheduled <name>`. `runner.run` takes a `trigger` ("manual" vs
  "scheduled:<name>") recorded in `meta.json`. The CLI is now a Typer GROUP
  (`run` + `schedule …` + hidden `_run-scheduled`); `cli.entry` injects an
  implicit `run` when argv[0] isn't a known subcommand/help flag, preserving the
  historical `pdca "task"` form. Entry point in `pyproject.toml` is
  `pdca.cli:entry` — re-run `pip install -e .` after touching it.

Run everything under the repo venv: `.venv/bin/pdca "task" --loop`
(prefix `PATH=$PWD/.venv/bin:$PATH` so Do scripts and gate commands can find
`pytest`). `DEEPSEEK_API_KEY` lives in `.env`.
