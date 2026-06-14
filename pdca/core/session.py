"""Per-run session logging — the single source of truth for what happened.

Every `pdca` invocation opens exactly one Session under
``~/.pdca_agent/sessions/run_session_<date>_<time>_<8hex>/`` and routes all
logging through it, alongside (never replacing) the existing console output:

  session.log   human-readable timeline: phases entered, models used, decisions,
                cycle numbers, timestamps.
  raw/          one JSON file per model call — the exact prompt sent and the exact
                raw completion received, plus model id / tokens / latency. Named
                ``<cycle>_<phase>_<seq>.json``. Nothing the model said is discarded.
  scripts/      every Do/Probe script the model wrote, as it was executed.
  meta.json     task, workdir, trigger, start/end time, final outcome, exit code,
                total tokens, cycles used.

The module keeps a single ``current`` Session for the active run. The thin
module-level functions (log/set_cycle/set_phase/record_call/record_script) are
null-safe so call sites stay clean and a missing session simply no-ops.
"""
import json
import os
import secrets
import time
from datetime import datetime

from pdca import config

SESSIONS_ROOT = os.path.join(config.AGENT_HOME, "sessions")

current: "Session | None" = None


class Session:
    def __init__(self, task: str, workdir: str, trigger: str = "manual"):
        ts = datetime.now()
        self.id = f"run_session_{ts:%Y-%m-%d}_{ts:%H-%M-%S}_{secrets.token_hex(4)}"
        self.dir = os.path.join(SESSIONS_ROOT, self.id)
        self.raw_dir = os.path.join(self.dir, "raw")
        self.scripts_dir = os.path.join(self.dir, "scripts")
        os.makedirs(self.raw_dir, exist_ok=True)
        os.makedirs(self.scripts_dir, exist_ok=True)
        self.log_path = os.path.join(self.dir, "session.log")
        self.meta_path = os.path.join(self.dir, "meta.json")

        self.task = task
        self.workdir = workdir
        self.trigger = trigger
        self.start = time.monotonic()
        self.start_iso = ts.isoformat(timespec="seconds")
        self.cycle = 0
        self.phase = "INIT"
        self._seq: dict[tuple[int, str], int] = {}
        self.model_calls = 0

        self._write_meta(outcome="(running)", exit_code=None, cycles=0,
                         tokens=0, end_iso=None)
        self.log(f"session {self.id} opened | trigger={trigger}")
        self.log(f"task: {task}")
        self.log(f"workdir: {workdir}")

    # ---- timeline ------------------------------------------------------
    def log(self, msg: str) -> None:
        line = f"{datetime.now().isoformat(timespec='seconds')}  {msg}\n"
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(line)

    def set_cycle(self, cycle: int) -> None:
        self.cycle = cycle

    def set_phase(self, phase: str) -> None:
        self.phase = phase

    # ---- raw model calls ----------------------------------------------
    def record_call(self, model: str, system: str, user: str, completion: str,
                    tokens: int, latency: float) -> str:
        """Persist one model call verbatim. Returns the raw/ filename."""
        self.model_calls += 1
        key = (self.cycle, self.phase)
        seq = self._seq.get(key, 0) + 1
        self._seq[key] = seq
        name = f"{self.cycle}_{self.phase}_{seq}.json"
        payload = {
            "cycle": self.cycle,
            "phase": self.phase,
            "seq": seq,
            "model": model,
            "tokens": tokens,
            "latency_s": round(latency, 3),
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "prompt": {"system": system, "user": user},
            "completion": completion,
        }
        with open(os.path.join(self.raw_dir, name), "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        self.log(f"cycle {self.cycle} {self.phase}: model={model} "
                 f"tokens={tokens} latency={latency:.2f}s -> raw/{name}")
        return name

    # ---- scripts -------------------------------------------------------
    def record_script(self, phase: str, script: str, attempt: int | None = None) -> str:
        suffix = f"_attempt{attempt}" if attempt is not None else ""
        name = f"cycle{self.cycle}_{phase}{suffix}.py"
        with open(os.path.join(self.scripts_dir, name), "w", encoding="utf-8") as f:
            f.write(script)
        self.log(f"cycle {self.cycle} {phase}: script -> scripts/{name} "
                 f"({len(script)} chars)")
        return name

    # ---- meta ----------------------------------------------------------
    def _write_meta(self, outcome: str, exit_code: int | None, cycles: int,
                    tokens: int, end_iso: str | None) -> None:
        meta = {
            "session_id": self.id,
            "task": self.task,
            "workdir": self.workdir,
            "trigger": self.trigger,
            "start_time": self.start_iso,
            "end_time": end_iso,
            "outcome": outcome,
            "exit_code": exit_code,
            "total_tokens": tokens,
            "cycles": cycles,
            "model_calls": self.model_calls,
        }
        with open(self.meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)

    def close(self, outcome: str, exit_code: int, cycles: int, tokens: int) -> None:
        end_iso = datetime.now().isoformat(timespec="seconds")
        self._write_meta(outcome=outcome, exit_code=exit_code, cycles=cycles,
                         tokens=tokens, end_iso=end_iso)
        elapsed = round(time.monotonic() - self.start, 1)
        self.log(f"session closed | outcome={outcome} | exit={exit_code} | "
                 f"cycles={cycles} | tokens={tokens} | "
                 f"model_calls={self.model_calls} | elapsed={elapsed}s")


# ---- module-level, null-safe facade -----------------------------------

def start(task: str, workdir: str, trigger: str = "manual") -> Session:
    global current
    current = Session(task, workdir, trigger)
    return current


def close(outcome: str, exit_code: int, cycles: int, tokens: int) -> None:
    global current
    if current is not None:
        current.close(outcome, exit_code, cycles, tokens)
    current = None


def log(msg: str) -> None:
    if current is not None:
        current.log(msg)


def set_cycle(cycle: int) -> None:
    if current is not None:
        current.set_cycle(cycle)


def set_phase(phase: str) -> None:
    if current is not None:
        current.set_phase(phase)


def record_call(model: str, system: str, user: str, completion: str,
                tokens: int, latency: float) -> None:
    if current is not None:
        current.record_call(model, system, user, completion, tokens, latency)


def record_script(phase: str, script: str, attempt: int | None = None) -> None:
    if current is not None:
        current.record_script(phase, script, attempt)
