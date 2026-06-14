"""Runs model-written Do scripts in a separate process.

Pattern core: the script may read/produce arbitrary amounts of data, but only
its (capped) printed output ever returns to the model.
"""
import os
import re
import subprocess
import sys
import tempfile
import time

from pdca import config

_TOOLKIT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
_PRELUDE = "from toolkit import read, write, run, ls, note\n"


def strip_fences(text: str) -> str:
    m = re.search(r"```(?:python|py)?\s*\n(.*?)```", text, re.DOTALL)
    return (m.group(1) if m else text).strip()


def _check_workdir_python_files(workdir: str) -> str:
    """Scan top-level .py files (excluding harness dirs) for syntax errors.
    Returns a warning string if any are broken, else empty string."""
    errors = []
    harness = {".pdca", ".pdca_probe"}
    for name in os.listdir(workdir):
        if name.startswith(".") or name in harness:
            continue
        if not name.endswith(".py"):
            continue
        full = os.path.join(workdir, name)
        if not os.path.isfile(full):
            continue
        result = subprocess.run(
            [sys.executable, "-m", "py_compile", full],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            errors.append(f"{name}: {result.stderr.strip()[-300:]}")
    return ("\n\nHARNESS SYNTAX CHECK: broken files in workdir:\n" + "\n".join(errors)) if errors else ""


def execute(script: str, workdir: str, evidence_log: str) -> dict:
    """Returns {ok, stdout, stderr, elapsed, notes}. Non-zero exit is not
    fatal — it becomes evidence for Check."""
    source = _PRELUDE + strip_fences(script)
    env = dict(os.environ,
               PDCA_WORKDIR=os.path.realpath(workdir),
               PDCA_EVIDENCE_LOG=evidence_log,
               PDCA_UNRESTRICTED="1" if config.UNRESTRICTED else "0",
               PYTHONPATH=_TOOLKIT_DIR)
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "do_script.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write(source)
        start = time.monotonic()
        try:
            p = subprocess.run(
                [sys.executable, path], cwd=workdir, env=env,
                capture_output=True, text=True, timeout=config.SCRIPT_TIMEOUT,
            )
            ok, stdout, stderr = p.returncode == 0, p.stdout, p.stderr
        except subprocess.TimeoutExpired as e:
            ok = False
            stdout = (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            stderr = f"SCRIPT KILLED: exceeded {config.SCRIPT_TIMEOUT}s timeout"
        elapsed = time.monotonic() - start

    # Post-execution harness check: if a .py file was written with a syntax
    # error, surface it in stderr so the repair path triggers.
    syntax_warn = _check_workdir_python_files(workdir)
    if syntax_warn:
        stderr = (stderr + syntax_warn)[:2000]
        ok = False  # force repair / next-cycle fix

    notes = ""
    if evidence_log and os.path.exists(evidence_log):
        with open(evidence_log, encoding="utf-8") as f:
            notes = f.read()
    return {
        "ok": ok,
        "stdout": stdout[:config.STDOUT_CAP],
        "stderr": stderr[:2000],
        "elapsed": round(elapsed, 1),
        "notes": notes[:2000],
    }
