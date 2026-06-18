# Handoff: Outcome-Tied Quality Gate for the PDCA Loop

**Status:** IMPLEMENTED 2026-06-16 (all 5 phases). Kept as design rationale.
**Audience:** anyone touching the quality-gate logic later.
**Owner intent:** make the loop produce work judged against *what the user actually
wants*, not the cheapest artifact that passes a shell check.

---

## 0. Why this exists (read first)

The loop currently has exactly **one** definition of "done": the plan's
`success_criteria`, enforced by `verify_commands` (shell). Because quality cannot
be expressed as a shell command, every meaningful bar gets dropped during
planning and "done" collapses to file-exists / string-contains / exit-0 checks.
The loop then produces the minimum artifact that turns those checks green and
ADOPTs. This is Goodhart's law compiled into the architecture: the proxy becomes
the target.

PDCA is, originally, a *quality* management cycle. The loop should therefore
**derive the quality bar dynamically from the user's desired outcome** and enforce
it as a real gate — not as more shell commands.

**Do NOT bake task-specific examples into any prompt.** The guidance below is
written as intent and meaning. When you write the actual prompt text, keep every
clause domain-agnostic: it must read identically well for a data task, a refactor,
a document, a build fix, or anything else. Express the *principle*, never a case.

---

## 1. The design in one sentence

Introduce a second gate — a **quality gate** judged by the model against
outcome-derived `quality_criteria` — and make ADOPT require **both** gates:

> `overall = pass` **iff** the objective (shell) gate is all-green **AND** every
> quality criterion is met.

The objective gate stays authoritative for **failure** (a non-zero command can
never be "done"). The quality gate is authoritative for **blocking adoption** (a
green shell gate is necessary but not sufficient).

### Task-type awareness

The plan declares a `task_type`:

- `build_fix` — there is usually an objective truth (tests/build/lint). The shell
  gate carries most of the weight; quality criteria are lighter (e.g. the fix
  addresses the root cause; the solution wasn't gamed to pass a check).
- `produce_analyze` — there is no shell command that captures "good." The quality
  gate carries most of the weight; the shell gate only confirms the artifact
  exists and runs.

`task_type` tunes how strict the quality gate is and what kind of criteria PLAN
writes. It does **not** disable the quality gate for either type.

---

## 2. Principles to encode (the 5 + 7 we agreed on)

Every change below traces to these. Preserve the intent, not the wording.

The five directions:
1. Add a **persona / quality standard** to each phase (counterweight to minimalism).
2. **Anti-hardcoding / anti-gaming**: produce genuinely correct work; never
   construct output, lookups, or a trivial setup whose success is an artifact of
   the check rather than evidence the outcome was achieved.
3. **Tie "done" to the user's outcome**, not to a proxy.
4. **Quality is a gate**, dynamically deduced from what the user wants — judged,
   not grep'd — and it can block ADOPT.
5. **Task-type awareness**: gate-is-truth for `build_fix`, judge-is-truth for
   `produce_analyze`.

The seven lessons from the leaked-prompt study (corroborating sources):
1. Install a quality bar as a persona ("senior engineer", "take quality seriously").
2. Name the failure mode and give a positive standard (don't deliver the minimum
   that technically satisfies the criteria).
3. Persist end-to-end; don't stop at the floor. Definition of done = the user's
   outcome, not a proxy.
4. Forbid gaming checks (hardcoding / special-casing / trivial setups); write
   correct work and let checks pass as a consequence.
5. Verification is a judgment about sufficiency, not a fixed minimal command set.
6. Two reviews — plan review *and* a post-implementation quality review that can
   actually block — not one functional-only check.
7. Certify on inspected output, not on existence.

---

## 3. Code map (what to touch)

All paths relative to repo root. Line numbers are from the current tree; re-read
before editing.

- `pdca/core/phases.py` — **most of the work.** Models, all five phase prompts,
  and the `check()` decision logic.
  - models: `Plan`, `Report`, plus new `QualityCriterion`, `QualityVerdict`
  - prompts: `PLAN_SYSTEM`, `DO_SYSTEM`, `PROBE_SYSTEM`, `CHECK_SYSTEM`, `ACT_SYSTEM`
  - logic: `check()` (the force-pass block ~`phases.py:333-345` is the Goodhart bug)
  - `plan()` user-message builder (surface prior quality verdicts; pass task_type through)
- `pdca/core/state.py` — `append_cycle()` block: persist quality verdicts so the
  next PLAN sees them.
- `pdca/core/runner.py` — emit lines only (`[CHECK]` should report quality counts);
  no control-flow change needed (see §6).
- `pdca/llm.py` — no change (schema flows through `call_validated`).
- `pdca/config.py` — no change expected.

---

## 4. Data model changes (`phases.py`)

```python
class QualityCriterion(BaseModel):
    text: str        # what "good" looks like, derived from the user's desired outcome
    rationale: str   # why this matters for THIS task — forces outcome-grounding

class QualityVerdict(BaseModel):
    text: str
    status: Literal["met", "unmet", "unknown"]
    reason: str      # must cite concrete observed evidence

class Plan(BaseModel):
    task_type: Literal["build_fix", "produce_analyze"]
    analysis: str
    strategy: str
    objective: str
    steps: list[Step] = Field(max_length=7)
    success_criteria: list[str]
    verify_commands: list[str]
    quality_criteria: list[QualityCriterion] = Field(min_length=1, max_length=5)

class Report(BaseModel):
    criteria: list[CriterionVerdict]
    quality: list[QualityVerdict]
    gaps: list[str]
    overall: Literal["pass", "fail"]
```

Keep `success_criteria`/`verify_commands` exactly as they are — they remain the
objective floor. `quality_criteria` is the new, additive layer.

---

## 5. Prompt changes (intent, per phase — keep domain-agnostic)

Add one **persona** line + the targeted clause(s) to each existing system prompt.
Write them as general principle. No examples drawn from any specific domain or
from our test runs.

**PLAN** — persona: a senior engineer and the owner of "what done means for the
user." Encode:
- `success_criteria` + `verify_commands` are only the objective floor and are NOT
  the bar.
- Separately derive `quality_criteria` from the user's actual desired outcome:
  what a competent practitioner — or the user — would require for the deliverable
  to be genuinely good. The test to apply: "if I handed this result back, would
  the user consider their goal met?"
- Each quality criterion states a concrete, observable property of the *result*,
  with a rationale tying it to this task.
- Declare `task_type` and let it set how demanding the quality criteria are.
- quality_criteria may be refined to be **stricter or clearer** across cycles but
  **never weakened** to force a pass.

**DO** — persona: a meticulous engineer who takes quality seriously. Encode the
**anti-gaming** principle generically:
- Never hardcode answers, embed canned/templated output, special-case inputs, or
  construct a trivial/degenerate setup whose success is an artifact of the check
  rather than real evidence the outcome was achieved.
- Produce genuinely correct work and let the checks pass as a consequence.
- A check that cannot actually distinguish a good result from a bad one does not
  demonstrate the outcome — do the real work, don't satisfy the letter of the check.

**PROBE** — keep the existing skeptical-reviewer framing, but widen what counts as
a violation. Today it flags only functional failures and excludes "opinions";
explicitly carve out as **violations** (not opinions): gamed/canned output not
derived from real work, and trivial/degenerate setups that cannot demonstrate the
outcome. The probe should certify on **inspected output**, not on existence.

**CHECK** — persona: a demanding quality auditor and the user's advocate. New
responsibility: emit `quality` verdicts against `quality_criteria`, each citing
concrete observed evidence from the probe/output. State explicitly that a
deliverable can pass every shell command and still fail the quality gate, and that
"done" judges the **user's outcome**, not the cycle's sub-goal.

**ACT** — persona: the delivery lead. ADOPT only when the report shows the user's
outcome met (both gates), never on a green floor alone. (The code enforces this
too — see §6 — but the prompt should agree with the code.)

---

## 6. The `check()` decision logic (the actual fix)

Replace the current force-pass block (~`phases.py:333-345`). Intent:

```
gate_fail  = any verify command exited non-zero
gate_green = all verify commands exited zero
quality_met = every quality verdict is "met"

if gate_fail:
    overall = "fail"                         # floor not met — unconditional
    (record the failing command in gaps)
elif gate_green and quality_met:
    overall = "pass"                         # floor + outcome both met
elif gate_green and not quality_met:
    overall = "fail"                         # green shell != done (the Goodhart fix)
    (extend gaps with each unmet quality criterion + its reason)
```

Keep the existing behavior of upgrading a `CriterionVerdict` to "met" when its
backing gate command exited 0 — that part was sound. What changes is that
`overall` no longer rides on the shell gate alone.

**Runner control flow needs no change.** Because ADOPT now already implies quality
met, "don't stop at the floor / spend the budget" falls out for free: the loop
keeps iterating (consuming its token/time budget) until the quality gate passes or
a stop condition fires. Quality gaps already propagate into `lessons` and
`last_check_pack`, so they drive the next PLAN. Only update the `[CHECK]` emit to
also print quality counts.

---

## 7. State / digest wiring

`state.append_cycle()` (`pdca/core/state.py`): add a `**Quality:**` line to the
cycle block summarizing each quality verdict (status + text). This is also an
anti-dodge measure: it makes any weakening of quality_criteria across cycles
visible to the next PLAN and CHECK.

In `plan()`'s user-message builder, ensure prior quality verdicts reach the next
plan (they already arrive via the digest once §7 is done, and via
`last_check_pack` since `Report` now contains `quality`).

---

## 8. Anti-dodge safeguards

Risk: the loop weakens `quality_criteria` next cycle to pass trivially.
Mitigations, in order of preference:
1. The "never weaken, only refine stricter" rule in PLAN (§5).
2. Prior quality_criteria/verdicts surfaced in the digest (§7), so drift is visible.
3. The existing feasibility-audit ladder (`runner.py` RETHINK/AUDIT/BACKSTOP)
   already bounds infinite loops.

If weakening still appears in practice, escalate: pin cycle-1 `quality_criteria`
into the session `meta.json` and feed them forward verbatim instead of letting
each cycle re-derive them. Treat this as a follow-up, not part of the first pass.

---

## 9. Implementation phases

Do them in this order; each is independently checkable.

**Phase 1 — Models + `check()` logic.**
Add `QualityCriterion`, `QualityVerdict`; extend `Plan` and `Report`; rewrite the
`check()` force-pass block per §6.
*Acceptance:* unit-exercise `check()` with synthetic gate/report inputs — green
gate + unmet quality ⇒ `overall == "fail"`; green gate + met quality ⇒ `"pass"`;
any non-zero gate ⇒ `"fail"`. No live model calls needed.

**Phase 2 — PLAN prompt.**
Persona + quality-deduction + task_type + never-weaken (§5). Highest-leverage
single change.
*Acceptance:* run PLAN on two unrelated tasks (one buildy, one producey); confirm
it emits a sensible `task_type` and 1–5 outcome-grounded quality_criteria that are
NOT restatements of the shell checks, and that read as general quality bars.

**Phase 3 — DO anti-gaming + PROBE/CHECK quality judging.**
DO persona + anti-gaming clause; PROBE violation-scope widening; CHECK persona +
`quality` verdict emission (§5).
*Acceptance:* CHECK returns `quality` verdicts with evidence-citing reasons; PROBE
flags gamed/degenerate output as a violation rather than ignoring it.

**Phase 4 — State + runner wiring.**
`append_cycle()` quality line (§7); `[CHECK]` emit shows quality counts.
*Acceptance:* STATE.md cycle blocks show quality verdicts; console shows
`quality X/Y`.

**Phase 5 — End-to-end validation.**
Re-run a real, non-trivial task end to end. Expected behavior: a minimal/gamed
first attempt that turns the shell gate green now **fails the quality gate**,
`overall=fail`, and the loop spends its budget improving the deliverable before
ADOPT — instead of adopting the floor. Confirm the loop still terminates (ADOPTs
or hits a stop condition), and that a genuinely complete task still ADOPTs without
spurious quality blocks.

---

## 10. Acceptance for the whole change

- A task with no possible shell-expressible quality check can still be blocked
  from ADOPT on quality grounds.
- ADOPT never happens on a green shell gate alone.
- A correct, complete deliverable still ADOPTs (no false-negative quality blocks
  that trap the loop until budget exhaustion on already-good work).
- All prompt additions are domain-agnostic — grep the diff for any task-specific
  noun or example and remove it.

---

## 11. Out of scope (do not do now)

- New phases or new model roles (quality judging folds into the existing CHECK
  call — it already sees task, criteria, gate, probe, and evidence; one pro call).
- `meta.json` pinning of quality_criteria (only if §8 mitigations prove
  insufficient).
- Any change to `verify_commands` semantics or the objective gate — it stays.
