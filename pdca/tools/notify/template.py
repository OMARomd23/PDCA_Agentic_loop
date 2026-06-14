"""The notification email template.

Separation of concerns: deterministic run facts (outcome, exit code, cycles,
tokens, duration, ...) are filled here by code so they cannot be hallucinated;
the model only ever supplies the ``subject`` and the ``summary_html`` narrative.
This keeps the email a readable status report rather than a raw log dump.
"""
import html

# exit_code -> (badge label, accent colour)
_STATUS = {
    0: ("COMPLETED", "#1a7f37"),
    1: ("INFEASIBLE", "#9a3412"),
    2: ("BUDGET STOP", "#9a6700"),
    3: ("ENV ERROR", "#9a3412"),
}
_DEFAULT_STATUS = ("FINISHED", "#57606a")


def status_for(exit_code: int) -> tuple[str, str]:
    return _STATUS.get(exit_code, _DEFAULT_STATUS)


_HTML = """\
<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;\
max-width:640px;margin:0 auto;color:#1f2328">
  <div style="border-left:4px solid {accent};padding:4px 14px;margin-bottom:18px">
    <span style="display:inline-block;background:{accent};color:#fff;\
font-size:12px;font-weight:600;letter-spacing:.04em;padding:2px 8px;\
border-radius:4px">{badge}</span>
    <div style="font-size:13px;color:#57606a;margin-top:6px">PDCA agent run · {trigger}</div>
  </div>

  <div style="font-size:15px;line-height:1.55;margin-bottom:20px">{summary_html}</div>

  <table style="width:100%;border-collapse:collapse;font-size:13px">
    <tr><td style="{k}">Task</td><td style="{v}">{task}</td></tr>
    <tr><td style="{k}">Outcome</td><td style="{v}">{outcome}</td></tr>
    <tr><td style="{k}">Exit code</td><td style="{v}">{exit_code}</td></tr>
    <tr><td style="{k}">Cycles</td><td style="{v}">{cycles}</td></tr>
    <tr><td style="{k}">Tokens</td><td style="{v}">{tokens}</td></tr>
    <tr><td style="{k}">Duration</td><td style="{v}">{duration}</td></tr>
    <tr><td style="{k}">Workdir</td><td style="{v}">{workdir}</td></tr>
    <tr><td style="{k}">Session</td><td style="{v}">{session_id}</td></tr>
  </table>

  <div style="font-size:12px;color:#8c959f;margin-top:18px">\
Sent by the pdca notify tool — the agent's only external-notification channel.</div>
</div>
"""

_KEY_STYLE = ("padding:6px 12px 6px 0;color:#57606a;white-space:nowrap;"
              "vertical-align:top;border-bottom:1px solid #eaeef2")
_VAL_STYLE = ("padding:6px 0;vertical-align:top;border-bottom:1px solid #eaeef2;"
              "word-break:break-word")


def render(facts: dict, summary_html: str) -> str:
    """Render the full HTML email. ``facts`` holds the deterministic run data;
    ``summary_html`` is the model-written narrative (already HTML)."""
    badge, accent = status_for(facts.get("exit_code", -1))

    def esc(key: str) -> str:
        return html.escape(str(facts.get(key, "")))

    return _HTML.format(
        accent=accent,
        badge=badge,
        trigger=esc("trigger"),
        summary_html=summary_html,
        task=esc("task"),
        outcome=esc("outcome"),
        exit_code=esc("exit_code"),
        cycles=esc("cycles"),
        tokens=esc("tokens"),
        duration=esc("duration"),
        workdir=esc("workdir"),
        session_id=esc("session_id"),
        k=_KEY_STYLE,
        v=_VAL_STYLE,
    )
