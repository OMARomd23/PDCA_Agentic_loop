"""The PDCA runner: a budget-bounded autonomous loop.

Giving up is a harness decision, never a model one:
- Hard stops: total tokens (primary), wall-time, optional --max-cycles.
- Stuck detection: if the same set of verify commands keeps failing across
  consecutive cycles, the harness escalates — first forcing a strategy rethink
  (and giving the Do phase to the pro model), then running a feasibility audit
  that may stop the run only on proven environmental infeasibility.
"""
import os
import time

from pdca import config, llm
from pdca.core import phases, session, state


def _emit(msg: str) -> None:
    """Route a line to BOTH the console (unchanged behaviour) and session.log."""
    print(msg)
    session.log(msg)

RETHINK_AFTER = 3      # consecutive same-signature failures before forced rethink
AUDIT_AFTER = 5        # consecutive same-signature failures before feasibility audit
BACKSTOP_AUDIT = 8     # any 8 consecutive overall=fail cycles triggers audit regardless

# Directories created by the harness that must never appear in task lint/test gates.
_HARNESS_DIRS = (".pdca", ".pdca_probe")

# Ruff config written into every new workdir so harness dirs are excluded from
# `ruff check .` regardless of what the model plans.
_RUFF_TOML = """\
[lint]
exclude = [".pdca", ".pdca_probe"]
"""


def _setup_workdir(workdir: str) -> str:
    """Create workdir scaffolding and a .ruff.toml that excludes harness dirs."""
    os.makedirs(workdir, exist_ok=True)
    pdca_dir = os.path.join(workdir, ".pdca")
    os.makedirs(pdca_dir, exist_ok=True)
    # Only write if no ruff config exists yet (respect user's own config).
    ruff_toml = os.path.join(workdir, ".ruff.toml")
    pyproject = os.path.join(workdir, "pyproject.toml")
    if not os.path.exists(ruff_toml) and not os.path.exists(pyproject):
        with open(ruff_toml, "w") as f:
            f.write(_RUFF_TOML)
    return pdca_dir


def _failure_signature(gate: list[dict], report: phases.Report) -> tuple:
    failing_cmds = tuple(sorted(g["cmd"] for g in gate if g["code"] != 0))
    if failing_cmds:
        return failing_cmds
    # No gate failures but still overall fail: key on unmet criteria instead.
    return tuple(sorted(c.text for c in report.criteria if c.status != "met"))


def _failure_pack(report_json: str, gate: list[dict], evidence: dict,
                  probe_ev: dict | None) -> str:
    """Everything the next Plan needs to reason about this failure."""
    gate_lines = "\n".join(
        f"$ {g['cmd']}  -> exit {g['code']}\n{g['tail']}" for g in gate)
    parts = [f"CHECK REPORT:\n{report_json}", f"GATE OUTPUT:\n{gate_lines}"]
    if evidence["stderr"]:
        parts.append(f"DO-SCRIPT STDERR (tail):\n{evidence['stderr'][-800:]}")
    if probe_ev and probe_ev.get("stdout"):
        parts.append(f"PROBE FINDINGS:\n{probe_ev['stdout'][:1500]}")
    return "\n\n".join(parts)


def run(task: str, workdir: str, loop: bool, max_cycles: int,
        max_seconds: int, max_tokens: int, trigger: str = "manual",
        notify: bool = False, notify_to: str = "") -> int:
    """Exit codes: 0 ADOPT / single-cycle done, 1 proven infeasible,
    2 budget stop (tokens/time/cycles), 3 environment error.

    `trigger` ("manual" or "scheduled:<name>") is recorded in the session's
    meta.json so manual and cron-launched runs can be told apart."""
    workdir = os.path.realpath(workdir)
    session.start(task, workdir, trigger=trigger)
    if config.UNRESTRICTED:
        _emit("⚠ UNRESTRICTED MODE — full system access")

    cycle, outcome, exit_code = 0, "single cycle done", 0
    try:
        pdca_dir = _setup_workdir(workdir)

        # Sanity-check the environment once before starting: if basic tools (pytest,
        # ruff) are missing, exit early with a clear message instead of burning cycles.
        _env_check = phases.run_gate(["pytest --version", "ruff --version"], workdir)
        missing = [g["cmd"].split()[0] for g in _env_check if g["code"] == 127]
        if missing:
            _emit(f"ENVIRONMENT ERROR: commands not found: {missing}")
            _emit("Make sure the venv is in PATH: PATH=$PWD/.venv/bin:$PATH pdca ...")
            outcome, exit_code = f"ENVIRONMENT ERROR: missing {missing}", 3
            return exit_code

        start = time.monotonic()
        last_check_pack = ""
        lessons: list[str] = []
        last_sig, stuck = None, 0
        fail_streak = 0          # backstop: total consecutive overall=fail cycles
        probe_ev: dict | None = None
        # Last cycle's results, surfaced to notify so the email can report findings.
        evidence: dict | None = None
        report = None

        while True:
            cycle += 1
            if llm.tokens_used > max_tokens:
                outcome, exit_code, cycle = f"STOPPED: token budget ({max_tokens})", 2, cycle - 1
                break
            if time.monotonic() - start > max_seconds:
                outcome, exit_code, cycle = f"STOPPED: max wall-time ({max_seconds}s)", 2, cycle - 1
                break
            if max_cycles and cycle > max_cycles:
                outcome, exit_code, cycle = f"STOPPED: max cycles ({max_cycles})", 2, cycle - 1
                break

            session.set_cycle(cycle)
            _emit(f"--- cycle {cycle} | elapsed {int(time.monotonic() - start)}s | "
                  f"tokens {llm.tokens_used} ---")

            digest = state.load_digest(workdir)
            lessons_text = "\n".join(f"- {x}" for x in lessons[-15:])
            rethink = stuck >= RETHINK_AFTER

            plan = phases.plan(task, digest, last_check_pack, lessons_text,
                               rethink_count=stuck if rethink else 0)
            _emit(f"[PLAN]  {len(plan.steps)} steps, {len(plan.success_criteria)} criteria, "
                  f"{len(plan.verify_commands)} verify cmds"
                  + (" [RETHINK]" if rethink else "") + f" — {plan.objective}")

            do_model = config.MODEL_PLAN if rethink else config.MODEL_DO
            ev_log = os.path.join(pdca_dir, f"evidence_cycle_{cycle}.log")
            evidence = phases.do(plan, digest, workdir, ev_log, model=do_model)
            with open(os.path.join(pdca_dir, f"cycle_{cycle}_script.txt"), "w") as f:
                f.write(evidence["script"])
            if evidence["stderr"]:
                with open(os.path.join(pdca_dir, f"cycle_{cycle}_stderr.txt"), "w") as f:
                    f.write(evidence["stderr"])
            _emit(f"[DO]    script ran {evidence['elapsed']}s, "
                  f"{'ok' if evidence['ok'] else 'FAILED'}"
                  + (f" ({evidence['attempts']} attempts)" if evidence["attempts"] > 1 else "")
                  + (" [pro]" if do_model == config.MODEL_PLAN else ""))

            # Probe is only useful when gate commands pass (catching subtle issues near done).
            # Skip it when gates obviously fail — saves a pro-model call and avoids noise.
            quick_gate = phases.run_gate(plan.verify_commands, workdir)
            gate_passed = all(g["code"] == 0 for g in quick_gate)
            if gate_passed:
                probe_log = os.path.join(pdca_dir, f"probe_cycle_{cycle}.log")
                probe_ev = phases.probe(plan, workdir, probe_log)
                with open(os.path.join(pdca_dir, f"probe_{cycle}_script.txt"), "w") as f:
                    f.write(probe_ev["script"])
                _emit(f"[PROBE] ran {probe_ev['elapsed']}s, "
                      f"{'ok' if probe_ev['ok'] else 'FAILED'}")
            else:
                probe_ev = {"ok": True, "stdout": "(skipped: gate failed)", "stderr": "", "notes": ""}
                _emit("[PROBE] skipped (gate failed)")

            report, gate = phases.check(task, plan, evidence, probe_ev, workdir,
                                        pre_gate=quick_gate)
            met = sum(1 for c in report.criteria if c.status == "met")
            _emit(f"[CHECK] {met}/{len(report.criteria)} criteria met, overall={report.overall}")
            report_json = report.model_dump_json(indent=1)

            decision = phases.act(task, report, cycle, digest, lessons_text)
            _emit(f"[ACT]   {decision.decision}: {decision.reason}")

            state.append_cycle(workdir, cycle, plan, evidence, report, gate, decision)

            if not loop:
                print("\nCheck report:\n" + report_json)
                session.log("check report:\n" + report_json)
                break
            if decision.decision == "ADOPT":
                outcome, exit_code = "ADOPTED ✓", 0
                break

            # ---- failed cycle bookkeeping: context, lessons, stuck ladder ----
            last_check_pack = _failure_pack(report_json, gate, evidence, probe_ev)
            for adj in decision.adjustments:
                if adj not in lessons:
                    lessons.append(adj)
            lessons.append(f"cycle {cycle} ({'rethink' if rethink else 'normal'}): "
                           f"failed — {decision.reason}")

            fail_streak += 1
            sig = _failure_signature(gate, report)
            stuck = stuck + 1 if sig == last_sig else 1
            last_sig = sig

            # Primary ladder: same failure signature for AUDIT_AFTER cycles.
            # Backstop: any BACKSTOP_AUDIT consecutive overall=fail cycles (catches
            # tasks that keep changing verify commands to dodge the signature check).
            should_audit = (stuck >= AUDIT_AFTER) or (fail_streak >= BACKSTOP_AUDIT)
            if should_audit:
                feas = phases.feasibility_audit(task, state.load_digest(workdir, 4),
                                                last_check_pack)
                _emit(f"[AUDIT] feasibility: {feas.verdict} — {feas.reason}"
                      + (f" [backstop at {fail_streak} fails]" if fail_streak >= BACKSTOP_AUDIT
                         else f" [stuck={stuck}]"))
                if feas.verdict == "INFEASIBLE":
                    outcome, exit_code = f"STOPPED: proven infeasible — {feas.reason}", 1
                    break
                stuck = RETHINK_AFTER  # stay in rethink mode, audit again later
                fail_streak = 0  # reset backstop; audit already reviewed the history

        _emit(f"\n{cycle} cycle(s) | {outcome} | {llm.tokens_used} tokens total")

        # End-of-run notification — the agent's only external channel. Opt-in:
        # only fires when the caller enabled it and gave a recipient. Deferred
        # import keeps Resend/compose cost off paths that never send; the tool
        # swallows its own errors so this can never change the exit code.
        if notify and notify_to:
            try:
                from pdca.tools import notify as notify_tool
                notify_tool.notify_run_complete(
                    recipient=notify_to,
                    task=task, workdir=workdir, trigger=trigger,
                    outcome=outcome, exit_code=exit_code, cycles=cycle,
                    tokens=llm.tokens_used,
                    session_id=session.current.id if session.current else "",
                    duration_s=time.monotonic() - start,
                    state_digest=state.load_digest(workdir, 3),
                    result_output=(evidence or {}).get("stdout", ""),
                    report_json=report.model_dump_json(indent=1) if report else "",
                )
            except Exception as e:
                _emit(f"[NOTIFY] skipped: {e}")

        return exit_code
    finally:
        session.close(outcome, exit_code, cycle, llm.tokens_used)
