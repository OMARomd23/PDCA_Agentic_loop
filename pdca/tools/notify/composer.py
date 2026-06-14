"""The model writes the email subject and body.

Per the design, the agent — not hardcoded strings — authors the human-facing
prose. We hand it the deterministic run facts plus the tail of STATE.md and ask
for a tight subject and a 2-4 sentence HTML summary. Everything else (the facts
table, status badge) is rendered deterministically in template.py.

If the model call fails for any reason we fall back to a plain deterministic
subject/summary: notify must never depend on the model succeeding.
"""
import html

from pydantic import BaseModel, Field

from pdca import config, llm

_SYSTEM = """\
You write a concise completion-status email about an autonomous coding-agent run.
You are given the run's factual outcome and the tail of its STATE.md log.

Return JSON with exactly two fields:
  subject       a short, specific subject line (<= 80 chars, no surrounding quotes)
  summary_html  2-4 sentences of valid inline HTML summarising WHAT the agent
                actually accomplished and the final outcome.

Rules:
- Summarise; do NOT paste logs, code, commands, or the raw STATE text.
- Use only simple inline tags (<p>, <strong>, <em>); no <html>/<body>/<script>/style.
- Be factual and grounded in the provided data; do not invent results.
- The status badge and a facts table are added automatically — do not repeat
  exit codes, token counts, or cycle numbers in your prose."""


class EmailContent(BaseModel):
    subject: str = Field(..., max_length=200)
    summary_html: str


def _fallback(facts: dict) -> tuple[str, str]:
    badge = {0: "completed", 1: "infeasible", 2: "stopped (budget)"}.get(
        facts.get("exit_code", -1), "finished")
    task = html.escape(str(facts.get("task", "")))[:200]
    return (
        f"PDCA run {badge}: {str(facts.get('task',''))[:60]}",
        f"<p>The agent run <strong>{badge}</strong> for task: {task}.</p>",
    )


def compose(facts: dict, state_digest: str) -> tuple[str, str]:
    """Return (subject, summary_html). Never raises — falls back on error."""
    user = (
        "RUN FACTS:\n"
        f"- task: {facts.get('task')}\n"
        f"- trigger: {facts.get('trigger')}\n"
        f"- outcome: {facts.get('outcome')}\n"
        f"- exit_code: {facts.get('exit_code')}\n"
        f"- cycles: {facts.get('cycles')}\n"
        f"- tokens: {facts.get('tokens')}\n"
        f"- duration: {facts.get('duration')}\n\n"
        "RECENT STATE.md (tail, for context only):\n"
        f"{state_digest or '(no STATE.md — first/only cycle)'}"
    )
    try:
        out = llm.call_validated(config.MODEL_ACT, _SYSTEM, user, EmailContent)
        subject = out.subject.strip() or _fallback(facts)[0]
        summary = out.summary_html.strip() or _fallback(facts)[1]
        return subject, summary
    except Exception:
        return _fallback(facts)
