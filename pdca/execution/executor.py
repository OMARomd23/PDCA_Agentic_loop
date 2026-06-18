"""Execution substrate for the turn-based DO/PROBE agents.

The agents act one tool call at a time. `ToolContext` is the harness-side handler
for those calls (read/write/run/ls/note/finish): it runs in-process, bound to a
single task workdir + evidence log, and every shell `run` inherits the workdir's
virtualenv on PATH via `workdir_env` — the same fix the objective gate uses.
"""
import os
import subprocess
import sys

from pdca import config


def workdir_env(workdir: str, base: dict | None = None) -> dict:
    """Env for running commands in the task workdir, with any virtualenv the model
    created inside the workdir prepended to PATH.

    DO/PROBE shell-outs and gate commands otherwise resolve `python`/`pip`/`pytest`
    to the harness interpreter — NOT the task venv — so the model would have to
    remember `./venv/bin/python` everywhere and break when it forgets or uses
    `source` (which fails under /bin/sh). Detecting the venv by its `pyvenv.cfg`
    marker (works regardless of the dir's name) and putting its bin/ first makes the
    environment uniform, so the model stops fighting it."""
    env = dict(base if base is not None else os.environ)
    bins = []
    try:
        for name in sorted(os.listdir(workdir)):
            d = os.path.join(workdir, name)
            if os.path.isfile(os.path.join(d, "pyvenv.cfg")) and os.path.isdir(os.path.join(d, "bin")):
                bins.append(os.path.join(d, "bin"))
    except OSError:
        pass
    if bins:
        env["PATH"] = os.pathsep.join(bins + [env.get("PATH", "")])
        env["VIRTUAL_ENV"] = os.path.dirname(bins[0])
    return env


def check_workdir_python_files(workdir: str) -> str:
    """Scan top-level .py files (excluding harness dirs) for syntax errors.
    Returns a warning string if any are broken, else empty string. Run as an
    end-of-loop sweep so a file written with bad syntax surfaces as evidence."""
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
    return ("broken files in workdir:\n" + "\n".join(errors)) if errors else ""


class ToolContext:
    """Per-cycle handler for one execution agent's tool calls.

    Bound to a single workdir + evidence log. `dispatch(name, args)` runs the
    named tool and returns a string result for the agent's next turn. `finish`
    sets the DONE flag the agent loop watches; `summary` is what the cycle carries
    forward. `last_error` holds the most recent failing-command tail (the failure
    signal Check / the next Plan build on)."""

    TOOLS = ("read", "write", "run", "ls", "note", "finish")

    def __init__(self, workdir: str, evidence_log: str):
        self.workdir = os.path.realpath(workdir)
        self.evidence_log = evidence_log
        self.finished = False
        self.summary = ""
        self.last_error = ""

    # ---- path policy (mirrors the old toolkit: UNRESTRICTED => no jail) ----
    def _resolve(self, path: str) -> str:
        full = os.path.realpath(os.path.join(self.workdir, path))
        if not config.UNRESTRICTED and full != self.workdir and not full.startswith(self.workdir + os.sep):
            raise ValueError(f"path escapes workdir: {path}")
        return full

    # ---- individual tools ------------------------------------------------
    def read(self, path: str) -> str:
        with open(self._resolve(path), "r", encoding="utf-8", errors="replace") as f:
            return f.read()

    def write(self, path: str, content: str) -> str:
        full = self._resolve(path)
        os.makedirs(os.path.dirname(full) or full, exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
        msg = f"wrote {len(content)} chars to {path}"
        if full.endswith(".py"):
            chk = subprocess.run([sys.executable, "-m", "py_compile", full],
                                 capture_output=True, text=True)
            if chk.returncode != 0:
                err = chk.stderr.strip()[-400:]
                msg += f" [SYNTAX ERROR: {err}]"
                self.note(f"SYNTAX ERROR in {path}: {err}")
            else:
                msg += " [syntax OK]"
        return msg

    def run(self, cmd: str, timeout: int = config.SCRIPT_TIMEOUT) -> str:
        env = workdir_env(self.workdir)
        try:
            p = subprocess.run(cmd, shell=True, cwd=self.workdir, env=env,
                               capture_output=True, text=True, timeout=timeout)
            tail = (p.stdout + p.stderr)
            if p.returncode != 0:
                self.last_error = f"$ {cmd}\nexit {p.returncode}\n{tail[-800:]}"
            return f"exit {p.returncode}\n{tail[-config.TOOL_OUTPUT_CAP:]}"
        except subprocess.TimeoutExpired:
            self.last_error = f"$ {cmd}\ntimeout after {timeout}s"
            return f"timeout after {timeout}s"

    def ls(self, path: str = ".") -> str:
        return "\n".join(sorted(os.listdir(self._resolve(path)))) or "(empty)"

    def note(self, text: str) -> str:
        if self.evidence_log:
            with open(self.evidence_log, "a", encoding="utf-8") as f:
                f.write(str(text).rstrip("\n") + "\n")
        return "noted"

    def finish(self, summary: str = "") -> str:
        self.finished = True
        self.summary = summary
        return "finished"

    # ---- dispatch --------------------------------------------------------
    def dispatch(self, name: str, args: dict) -> str:
        if name not in self.TOOLS:
            return f"ERROR: unknown tool '{name}'. Available: {', '.join(self.TOOLS)}"
        try:
            return str(getattr(self, name)(**(args or {})))
        except TypeError as e:
            return f"ERROR calling {name}: bad arguments ({e})"
        except Exception as e:  # tool errors are evidence, never fatal to the loop
            return f"ERROR in {name}: {type(e).__name__}: {e}"
