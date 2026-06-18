"""Tests for the turn-based execution agent (no API calls).

A FakeClient stands in for the OpenAI client and replays a scripted sequence of
assistant turns, so we exercise llm.agent_loop + executor.ToolContext end to end:
tool dispatch, the finish/DONE exit, the turn cap, and CHECK's on-demand probe path.
"""
import json
import types

import pytest

from pdca import llm
from pdca.execution.executor import ToolContext


class _Msg:
    def __init__(self, content): self.content = content


class _Choice:
    def __init__(self, content): self.message = _Msg(content)


class _Resp:
    def __init__(self, content):
        self.choices = [_Choice(content)]
        self.usage = types.SimpleNamespace(total_tokens=10)


class FakeClient:
    """Replays `turns` (list of dicts) as successive assistant JSON replies."""
    def __init__(self, turns):
        self._turns = [json.dumps(t) for t in turns]
        self.calls = 0
        self.chat = types.SimpleNamespace(completions=self)

    def create(self, model, messages, temperature=0.0, **kwargs):
        i = min(self.calls, len(self._turns) - 1)
        self.calls += 1
        return _Resp(self._turns[i])


@pytest.fixture
def fake_client(monkeypatch):
    def _install(turns):
        client = FakeClient(turns)
        monkeypatch.setattr(llm, "_client", client)
        return client
    return _install


def test_dispatches_tools_and_finishes(fake_client, tmp_path):
    fake_client([
        {"thought": "write a file", "calls": [
            {"tool": "write", "args": {"path": "out.txt", "content": "hello"}}]},
        {"thought": "done", "calls": [
            {"tool": "finish", "args": {"summary": "wrote out.txt"}}]},
    ])
    ctx = ToolContext(str(tmp_path), str(tmp_path / "ev.log"))
    res = llm.agent_loop("m", "sys", "go", ctx, max_turns=10)
    assert (tmp_path / "out.txt").read_text() == "hello"
    assert ctx.finished and res["ok"] and res["turns"] == 2
    assert res["summary"] == "wrote out.txt"


def test_writes_multiple_files_one_per_call(fake_client, tmp_path):
    # The point of the redesign: distinct files are separate write calls, never
    # one mega-script — the agent is free to do this directly.
    fake_client([
        {"thought": "three files", "calls": [
            {"tool": "write", "args": {"path": "a.txt", "content": "A"}},
            {"tool": "write", "args": {"path": "b.txt", "content": "B"}},
            {"tool": "write", "args": {"path": "c.txt", "content": "C"}}]},
        {"thought": "finish", "calls": [{"tool": "finish", "args": {"summary": "3 files"}}]},
    ])
    ctx = ToolContext(str(tmp_path), str(tmp_path / "ev.log"))
    llm.agent_loop("m", "sys", "go", ctx, max_turns=10)
    assert [(tmp_path / n).read_text() for n in ("a.txt", "b.txt", "c.txt")] == ["A", "B", "C"]


def test_script_then_run_path(fake_client, tmp_path):
    # And the agent CAN automate: write a script and run it. Neither forced nor forbidden.
    fake_client([
        {"thought": "automate", "calls": [
            {"tool": "write", "args": {"path": "gen.py",
             "content": "open('result.txt','w').write('42')\n"}}]},
        {"thought": "run it", "calls": [{"tool": "run", "args": {"cmd": "python3 gen.py"}}]},
        {"thought": "finish", "calls": [{"tool": "finish", "args": {"summary": "ran gen.py"}}]},
    ])
    ctx = ToolContext(str(tmp_path), str(tmp_path / "ev.log"))
    res = llm.agent_loop("m", "sys", "go", ctx, max_turns=10)
    assert (tmp_path / "result.txt").read_text() == "42"
    assert res["ok"]


def test_hits_turn_cap_without_finishing(fake_client, tmp_path):
    fake_client([{"thought": "loop forever", "calls": [
        {"tool": "ls", "args": {}}]}])  # never finishes
    ctx = ToolContext(str(tmp_path), str(tmp_path / "ev.log"))
    res = llm.agent_loop("m", "sys", "go", ctx, max_turns=3)
    assert not ctx.finished and not res["ok"] and res["turns"] == 3
    assert "cap" in res["stdout"].lower()


def test_invalid_json_is_reprompted_not_fatal(fake_client, tmp_path):
    client = fake_client([{"thought": "bad", "calls": [
        {"tool": "finish", "args": {"summary": "ok"}}]}])
    # Force the first reply to be non-JSON, then fall back to the scripted finish.
    client._turns = ["not json at all"] + client._turns
    ctx = ToolContext(str(tmp_path), str(tmp_path / "ev.log"))
    res = llm.agent_loop("m", "sys", "go", ctx, max_turns=5)
    assert ctx.finished and res["ok"]


def test_empty_file_read_does_not_crash_transcript(fake_client, tmp_path):
    (tmp_path / "empty.txt").write_text("")
    fake_client([
        {"thought": "read empty", "calls": [{"tool": "read", "args": {"path": "empty.txt"}}]},
        {"thought": "done", "calls": [{"tool": "finish", "args": {"summary": "ok"}}]},
    ])
    ctx = ToolContext(str(tmp_path), str(tmp_path / "ev.log"))
    res = llm.agent_loop("m", "sys", "go", ctx, max_turns=5)
    assert res["ok"]


def test_run_records_last_error(fake_client, tmp_path):
    ctx = ToolContext(str(tmp_path), str(tmp_path / "ev.log"))
    out = ctx.dispatch("run", {"cmd": "exit 7"})
    assert "exit 7" in out and "exit 7" in ctx.last_error


def test_py_syntax_verdict_on_write(tmp_path):
    ctx = ToolContext(str(tmp_path), str(tmp_path / "ev.log"))
    assert "syntax OK" in ctx.dispatch("write", {"path": "good.py", "content": "x = 1\n"})
    assert "SYNTAX ERROR" in ctx.dispatch("write", {"path": "bad.py", "content": "def (\n"})


def test_unknown_tool_is_reported_not_raised(tmp_path):
    ctx = ToolContext(str(tmp_path), str(tmp_path / "ev.log"))
    assert "unknown tool" in ctx.dispatch("frobnicate", {}).lower()


def test_check_clears_request_probe_when_probe_present(monkeypatch, tmp_path):
    # CHECK must never loop: once a probe report is supplied, request_probe is forced
    # false even if the model asks again.
    from pdca.core import phases

    def fake_validated(model, system, user, schema):
        return phases.Report(criteria=[], quality=[], gaps=[], overall="fail",
                             request_probe=True, probe_reason="again")
    monkeypatch.setattr(phases.llm, "call_validated", fake_validated)
    plan = phases.Plan(task_type="build_fix", analysis="a", strategy="s", objective="o",
                       steps=[], success_criteria=["c"], verify_commands=[],
                       quality_criteria=[phases.QualityCriterion(text="q", rationale="r")])
    evidence = {"ok": True, "stdout": "", "stderr": "", "notes": ""}
    probe_ev = {"ok": True, "stdout": "CRITERION 1: OK", "stderr": "", "notes": ""}
    report, _ = phases.check("task", plan, evidence, str(tmp_path),
                             probe_ev=probe_ev, pre_gate=[])
    assert report.request_probe is False
