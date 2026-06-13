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

# Every invocation logs a full session under here (see session.py).
AGENT_HOME = os.path.expanduser("~/.pdca_agent")
