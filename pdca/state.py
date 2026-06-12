"""STATE.md — the loop's memory. Appended every cycle; only the last ~2
cycles are loaded back into prompts, keeping the loop's own context small."""
import os
import re
from datetime import datetime

from pdca.phases import Decision, Plan, Report

STATE_FILE = "STATE.md"


def _path(workdir: str) -> str:
    return os.path.join(workdir, STATE_FILE)


def append_cycle(workdir: str, cycle: int, plan: Plan, evidence: dict,
                 report: Report, gate: list[dict], decision: Decision) -> None:
    stdout_digest = "\n".join(evidence["stdout"].splitlines()[:10])
    if evidence["stderr"]:
        stdout_digest += "\n[stderr tail] " + evidence["stderr"][-300:].replace("\n", " | ")
    verdicts = "; ".join(f"{c.status}: {c.text}" for c in report.criteria)
    gate_line = "; ".join(f"`{g['cmd']}` -> {g['code']}" for g in gate) or "(no verify commands)"
    block = f"""
## Cycle {cycle} — {datetime.now().isoformat(timespec='seconds')}
**Objective:** {plan.objective}
**Strategy:** {plan.strategy}
**Criteria:** {'; '.join(plan.success_criteria)}
**Evidence digest:**
```
{stdout_digest}
```
**Gate:** {gate_line}
**Check:** overall={report.overall} | {verdicts}
**Act:** {decision.decision} — {decision.reason}
**Adjustments/lessons:** {'; '.join(decision.adjustments) or '(none)'}
"""
    with open(_path(workdir), "a", encoding="utf-8") as f:
        if cycle == 1 and f.tell() == 0:
            f.write("# PDCA STATE\n")
        f.write(block)


def load_digest(workdir: str, last_n: int = 2) -> str:
    """Return the last N cycle blocks (the only history fed back to prompts)."""
    try:
        with open(_path(workdir), encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        return ""
    blocks = re.split(r"(?=^## Cycle )", text, flags=re.MULTILINE)
    cycles = [b for b in blocks if b.startswith("## Cycle ")]
    return "\n".join(cycles[-last_n:]).strip()
