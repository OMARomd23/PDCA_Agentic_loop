"""The agent decides what to report and how to present it.

This is the run's reporting voice. Rather than a fixed "status" blurb, the model
is given the actual results the run produced (the final execution output and the
check report) on top of the run facts and STATE.md tail, and is asked to REASON —
for THIS task — about what the user most wants to know and the clearest way to
present it (prose, a table, a key/value list, the right units/precision), then
write the email body in that form. Deterministic facts (outcome, exit code,
tokens, ...) stay code-filled in template.py and are never delegated to the model.

If the model call fails for any reason we fall back to a plain deterministic
subject/body: notify must never depend on the model succeeding.
"""
import html

from pydantic import BaseModel, Field

from pdca import config, llm

# The execution output can be large; keep enough to carry real findings without
# blowing the prompt. The Do script's stdout is already capped upstream (~6 KB).
_MAX_RESULT_CHARS = 4000

_SYSTEM = """\
You are the reporting voice of an autonomous agent. A run just finished and you
write the email that tells the user what happened. The whole point of this email
is to convey the actual RESULT — not merely that the run passed or failed.

You are given the task, the run's factual outcome, the tail of its STATE.md log,
the final execution output, and the check report.

First reason (privately — do NOT show this reasoning) about THIS specific run:
  1. What was the user actually trying to learn or achieve with this task?
  2. Which concrete findings/results in the data answer that? (e.g. real numbers,
     the contents of a report the agent produced, what was built/changed.)
  3. What is the clearest way to present those results for this task's nature —
     flowing prose, an HTML table, a key/value or bulleted list — and the right
     units and precision (e.g. GB vs bytes, rounded sensibly)?

Then return JSON with exactly two fields:
  subject    a short, specific subject line (<= 80 chars, no surrounding quotes)
  body_html  the email body as valid inline HTML, LEADING with the actual
             results/findings in the format you judged best, then (if useful) a
             one-line note on the outcome.

Rules:
- Show real results, don't just describe process. Never write "a report was
  saved" when you can show the report's key contents instead.
- You MAY use <p>, <strong>, <em>, <code>, <ul>/<ol>/<li>, and <table>/<tr>/
  <th>/<td> with minimal inline styles. No <html>/<body>/<head>/<script>/<style>
  wrappers and no external images, links, or resources.
- Be factual and grounded ONLY in the provided data; never invent numbers. If the
  run produced no usable findings (e.g. it failed early), say briefly what
  happened and why.
- Summarise long output into the most useful figures — do not paste raw logs,
  full file dumps, or code verbatim.
- A status badge and a facts table (outcome, exit code, tokens, cycles, duration)
  are added automatically — don't repeat those numbers in your prose."""


class EmailContent(BaseModel):
    subject: str = Field(..., max_length=200)
    body_html: str


def _fallback(facts: dict) -> tuple[str, str]:
    badge = {0: "completed", 1: "infeasible", 2: "stopped (budget)"}.get(
        facts.get("exit_code", -1), "finished")
    task = html.escape(str(facts.get("task", "")))[:200]
    return (
        f"PDCA run {badge}: {str(facts.get('task',''))[:60]}",
        f"<p>The agent run <strong>{badge}</strong> for task: {task}.</p>",
    )


def compose(facts: dict, state_digest: str, result_output: str = "",
            report_json: str = "") -> tuple[str, str]:
    """Return (subject, body_html). Never raises — falls back on error.

    ``result_output`` is the final Do-script stdout and ``report_json`` the check
    report; together they carry the run's actual findings so the email can report
    them rather than just the pass/fail status."""
    user = (
        "RUN FACTS:\n"
        f"- task: {facts.get('task')}\n"
        f"- trigger: {facts.get('trigger')}\n"
        f"- outcome: {facts.get('outcome')}\n"
        f"- exit_code: {facts.get('exit_code')}\n"
        f"- cycles: {facts.get('cycles')}\n"
        f"- tokens: {facts.get('tokens')}\n"
        f"- duration: {facts.get('duration')}\n\n"
        "FINAL EXECUTION OUTPUT (the agent's own printed results, truncated):\n"
        f"{(result_output or '(none captured)')[:_MAX_RESULT_CHARS]}\n\n"
        "CHECK REPORT (criteria + evidence):\n"
        f"{report_json or '(none)'}\n\n"
        "RECENT STATE.md (tail, for context):\n"
        f"{state_digest or '(no STATE.md — first/only cycle)'}"
    )
    try:
        # Pro model: this is a reasoning + presentation decision, not a rote blurb.
        out = llm.call_validated(config.MODEL_PLAN, _SYSTEM, user, EmailContent)
        subject = out.subject.strip() or _fallback(facts)[0]
        body = out.body_html.strip() or _fallback(facts)[1]
        return subject, body
    except Exception:
        return _fallback(facts)
