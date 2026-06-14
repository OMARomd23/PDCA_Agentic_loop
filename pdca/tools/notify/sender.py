"""Resend delivery wrapper — the actual outbound email call.

The API key is read from config (env / .env), never hardcoded. This mirrors the
working reference at /home/omar/Documents/resend_test.py.
"""
import resend

from pdca import config


def send(subject: str, html_body: str, to: str) -> dict:
    """Send one email to `to`. Raises if no API key is set."""
    if not config.RESEND_API_KEY:
        raise RuntimeError("RESEND_API_KEY not set (env or .env)")
    resend.api_key = config.RESEND_API_KEY
    return resend.Emails.send({
        "from": config.NOTIFY_FROM,
        "to": to,
        "subject": subject,
        "html": html_body,
    })
