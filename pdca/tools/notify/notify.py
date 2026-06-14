"""The notify tool: the agent's ONLY external-notification mechanism.

Called once at the end of a run to email a report on what happened. Notifications
are opt-in: the runner only invokes this when the user enabled them and supplied a
recipient (`--notify --notify-to <email>`). Orchestration only — it hands the run's
facts AND its actual results to the model, which reasons about what to report and
how to present it (composer), renders the template, and delivers via Resend
(sender). Any failure is logged and swallowed: a notification problem must never
change a run's exit code.
"""
from pdca.core import session
from pdca.tools.notify import composer, sender, template


def _fmt_duration(seconds: float | int | None) -> str:
    if not seconds:
        return "—"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    return f"{s // 60}m {s % 60}s"


def notify_run_complete(*, recipient: str, task: str, workdir: str, trigger: str,
                        outcome: str, exit_code: int, cycles: int, tokens: int,
                        session_id: str = "", duration_s: float | None = None,
                        state_digest: str = "", result_output: str = "",
                        report_json: str = "") -> bool:
    """Compose and send the end-of-run report email to `recipient`. Returns True
    on send, False if skipped or on any (logged, swallowed) error.

    `result_output` (final Do-script stdout) and `report_json` (the check report)
    carry the run's actual findings, so the model can report WHAT happened — with
    a presentation it picks for the task — not just whether it passed."""
    try:
        if not recipient:
            session.log("notify: skipped (no recipient)")
            return False

        facts = {
            "task": task,
            "workdir": workdir,
            "trigger": trigger,
            "outcome": outcome,
            "exit_code": exit_code,
            "cycles": cycles,
            "tokens": tokens,
            "duration": _fmt_duration(duration_s),
            "session_id": session_id,
        }
        subject, summary_html = composer.compose(facts, state_digest,
                                                 result_output, report_json)
        body = template.render(facts, summary_html)
        result = sender.send(subject, body, recipient)
        session.log(f"notify: sent to {recipient} "
                    f"(id={result.get('id') if isinstance(result, dict) else result})")
        return True
    except Exception as e:
        session.log(f"notify: failed ({type(e).__name__}: {e})")
        return False
