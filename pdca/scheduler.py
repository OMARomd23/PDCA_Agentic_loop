"""Scheduling — recurring, self-directed pdca runs.

The PDCA loop is goal-terminating: it runs to ADOPT / INFEASIBLE / a budget stop
and then exits. Scheduling does NOT make the loop recurring. Instead each cron
wake-up launches a FRESH, complete pdca run with a stored prompt — one wake-up =
one full session = one new session dir.

A "job" (name, prompt, schedule, workdir) is stored in
``~/.pdca_agent/schedules.json``. Installing a job writes ONE crontab line, tagged
with the comment ``pdca:<name>`` so it is idempotent (re-adding replaces, never
duplicates) and never touches unrelated crontab lines. The line calls the absolute
venv ``pdca _run-scheduled <name>`` with the venv on PATH, and redirects its own
bootstrap stdout/stderr to ``~/.pdca_agent/cron/<name>.log`` (separate from the
per-run session logs).
"""
import json
import os
import re
import sys
from datetime import datetime

from crontab import CronTab

from pdca import config

SCHEDULES_FILE = os.path.join(config.AGENT_HOME, "schedules.json")
CRON_LOG_DIR = os.path.join(config.AGENT_HOME, "cron")
DEFAULT_WORK_ROOT = os.path.join(config.AGENT_HOME, "work")

_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_COMMENT_PREFIX = "pdca:"

_PRESETS = {
    "hourly": "0 * * * *",
    "daily": "0 0 * * *",
    "weekly": "0 0 * * 0",
    "monthly": "0 0 1 * *",
}


# ---- schedule translation ---------------------------------------------

def to_cron(every: str) -> str:
    """Translate a shorthand ('5m', '30m', '2h', 'hourly', 'daily', 'weekly')
    or a raw 5-field cron expression into a cron expression."""
    every = every.strip()
    if every in _PRESETS:
        return _PRESETS[every]
    m = re.fullmatch(r"(\d+)\s*m", every)
    if m:
        n = int(m.group(1))
        if not 1 <= n <= 59:
            raise ValueError("minute shorthand must be 1-59; use a raw cron expr for more")
        return f"*/{n} * * * *"
    m = re.fullmatch(r"(\d+)\s*h", every)
    if m:
        n = int(m.group(1))
        if not 1 <= n <= 23:
            raise ValueError("hour shorthand must be 1-23; use a raw cron expr for more")
        return f"0 */{n} * * *"
    if len(every.split()) == 5:
        return every
    raise ValueError(
        f"unrecognized schedule {every!r}; use 5m/30m/2h/hourly/daily/weekly "
        "or a 5-field cron expression")


# ---- job store --------------------------------------------------------

def _load() -> dict:
    try:
        with open(SCHEDULES_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def _save(jobs: dict) -> None:
    os.makedirs(config.AGENT_HOME, exist_ok=True)
    with open(SCHEDULES_FILE, "w", encoding="utf-8") as f:
        json.dump(jobs, f, indent=2, ensure_ascii=False)


# ---- crontab line -----------------------------------------------------

def _venv_bin() -> str:
    return os.path.dirname(os.path.abspath(sys.executable))


def _pdca_bin() -> str:
    return os.path.join(_venv_bin(), "pdca")


def _cron_command(name: str, workdir: str) -> str:
    """The shell command cron runs: cd into the workdir, put the venv on PATH so
    the run's pytest/ruff/do-scripts resolve, call the internal entrypoint, and
    redirect this bootstrap's own output to the per-job cron log."""
    log = os.path.join(CRON_LOG_DIR, f"{name}.log")
    return (f"cd {workdir} && PATH={_venv_bin()}:$PATH "
            f"{_pdca_bin()} _run-scheduled {name} >> {log} 2>&1")


def _install_cron(name: str, cron_expr: str, workdir: str) -> None:
    comment = _COMMENT_PREFIX + name
    cron = CronTab(user=True)
    cron.remove_all(comment=comment)          # idempotent: replace, never duplicate
    job = cron.new(command=_cron_command(name, workdir), comment=comment)
    job.setall(cron_expr)                     # raises on an invalid expression
    cron.write()


def _uninstall_cron(name: str) -> int:
    comment = _COMMENT_PREFIX + name
    cron = CronTab(user=True)
    removed = cron.remove_all(comment=comment)
    cron.write()
    return removed


def _cron_installed(name: str) -> bool:
    comment = _COMMENT_PREFIX + name
    cron = CronTab(user=True)
    return any(True for _ in cron.find_comment(comment))


# ---- public API -------------------------------------------------------

def add(name: str, prompt: str, every: str, workdir: str | None) -> dict:
    if not _NAME_RE.match(name):
        raise ValueError(f"invalid name {name!r}; use letters, digits, '-' and '_' only")
    cron_expr = to_cron(every)
    workdir = os.path.abspath(workdir) if workdir else os.path.join(DEFAULT_WORK_ROOT, name)
    os.makedirs(CRON_LOG_DIR, exist_ok=True)
    os.makedirs(workdir, exist_ok=True)

    jobs = _load()
    existing = jobs.get(name, {})
    job = {
        "name": name,
        "prompt": prompt,
        "every": every,
        "cron": cron_expr,
        "workdir": workdir,
        "created": existing.get("created", datetime.now().isoformat(timespec="seconds")),
        "last_run": existing.get("last_run"),
    }
    _install_cron(name, cron_expr, workdir)   # do the cron write first; only persist on success
    jobs[name] = job
    _save(jobs)
    return job


def remove(name: str) -> bool:
    jobs = _load()
    _uninstall_cron(name)
    if name in jobs:
        del jobs[name]
        _save(jobs)
        return True
    return False


def list_jobs() -> list[dict]:
    jobs = _load()
    out = []
    for name in sorted(jobs):
        j = dict(jobs[name])
        j["installed"] = _cron_installed(name)
        out.append(j)
    return out


def run_once(name: str) -> int:
    """Launch a fresh, complete pdca run for a stored job. Returns its exit code."""
    jobs = _load()
    job = jobs.get(name)
    if job is None:
        print(f"no such scheduled job: {name!r}")
        return 4
    # Record the trigger time up front so `schedule list` reflects it immediately.
    job["last_run"] = datetime.now().isoformat(timespec="seconds")
    jobs[name] = job
    _save(jobs)

    from pdca import runner  # deferred: importing it builds the LLM client
    return runner.run(
        job["prompt"], job["workdir"], loop=True, max_cycles=0,
        max_seconds=config.MAX_SECONDS, max_tokens=config.MAX_TOKENS_TOTAL,
        trigger=f"scheduled:{name}",
    )
