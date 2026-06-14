"""Configuration: API key, model routing, hard-stop defaults."""
import os
import sys

from dotenv import load_dotenv

load_dotenv()

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
if not DEEPSEEK_API_KEY:
    sys.exit("FATAL: DEEPSEEK_API_KEY not set (env or .env)")

BASE_URL = "https://api.deepseek.com"

MODEL_PLAN = "deepseek-v4-pro"
MODEL_DO = "deepseek-v4-flash"
MODEL_CHECK = "deepseek-v4-pro"
MODEL_ACT = "deepseek-v4-flash"

MAX_CYCLES = 6
MAX_SECONDS = 1800
MAX_TOKENS_TOTAL = 400_000
SCRIPT_TIMEOUT = 120
STDOUT_CAP = 6000  # chars of Do-script stdout that may enter model context

# Unrestricted execution. When True there is NO filesystem jail and NO command
# allowlist: toolkit read/write/run/ls operate anywhere the OS permits, and run()
# may invoke any command including sudo. The operational rails that are NOT about
# restriction stay on regardless: the per-command timeout, stdout capping (context
# discipline — the whole point of programmatic tool calling), and the harness
# stopping conditions (token/time/cycle/stuck-ladder). Flip to False to re-enable
# the workdir jail without code surgery.
UNRESTRICTED = True

# Every invocation logs a full session under here (see session.py).
AGENT_HOME = os.path.expanduser("~/.pdca_agent")

# ---- Notifications (tools/notify) -------------------------------------
# Resend is the email delivery backend. The API key is read from the env /.env
# (never hardcoded); if it is absent, the notify tool simply no-ops — a missing
# key must never block or fail a run.
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
# Sender identity: Resend's shared, pre-verified test domain (not a personal
# address). Notifications are OFF by default and opt-in per run via
# `--notify --notify-to <email>`; the recipient is supplied there, never here.
NOTIFY_FROM = os.environ.get("PDCA_NOTIFY_FROM", "agent_pdca@resend.dev")
