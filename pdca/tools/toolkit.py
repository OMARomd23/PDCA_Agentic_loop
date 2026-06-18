"""Prompt-text source of truth for the turn-based DO/PROBE agents.

The tools themselves live in `pdca.execution.executor.ToolContext` (run in-process
by the agent loop). This module holds only what the model is told: the tool
reference, the neutral guidance on programmatic tool-calling, and the per-turn
action protocol the loop parses.
"""

# Pasted into the DO/PROBE system prompts — the model's ONLY API reference.
TOOLKIT_DOCS = '''\
Tools you can call (one or more per turn):

  read(path) -> str
      Return a file's full text. Relative paths resolve against the working
      directory. e.g. {"tool": "read", "args": {"path": "app/utils.py"}}

  write(path, content) -> str
      Overwrite (or create) a file with content; creates parent dirs. For .py
      files the result includes a syntax-check verdict ("[syntax OK]" or
      "[SYNTAX ERROR: ...]") — read it and re-write if it failed.
      e.g. {"tool": "write", "args": {"path": "app/utils.py", "content": "..."}}

  run(cmd, timeout=120) -> str
      Run a shell command and return "exit <code>" plus its output tail. ANY
      command is permitted (including sudo and paths outside the workdir). The
      command runs in the task working directory; the workdir virtualenv is on
      PATH, so call `python`/`pip`/`pytest` directly (never `source`, never
      `./venv/bin/python`). e.g. {"tool": "run", "args": {"cmd": "pytest -q"}}

  ls(path=".") -> str
      List directory entries. e.g. {"tool": "ls", "args": {"path": "tests"}}

  note(text) -> str
      Append a line to the cycle's evidence log (findings that survive even if
      they scroll out of your turn-by-turn context).

  finish(summary) -> str
      Call this — and ONLY this — when the work is genuinely complete. `summary`
      is a short account of what you did and the state you are leaving behind; it
      is carried into the next cycle. Calling finish ends your turn budget.

Programmatic tool-calling is an OPTION, not a requirement. When a subtask is
repetitive, bulk, or computational (transform many records, generate dozens of
similar entries, crunch data, run a suite), it is usually best to `write` a small
script — in whatever language fits (python, bash, ...) — and `run` it. When you
are authoring a handful of distinct files or making targeted edits, just `write`
each one directly. Reason about the subtask in front of you and pick whichever is
actually practical; neither approach is mandated and neither is forbidden.

Paths: relative paths resolve against the working directory; absolute paths are
honored as given. UNRESTRICTED MODE is active — there is no workdir jail; act
deliberately.
'''

# How every turn must be formatted; parsed by llm.agent_loop.
AGENT_PROTOCOL = '''\
You act in a loop, one turn at a time. Each turn, reply with ONLY a JSON object:

  {"thought": "<one or two sentences of reasoning>",
   "calls": [{"tool": "<name>", "args": {<arguments>}}, ...]}

- Issue one or more tool calls per turn (they run in order; their results come
  back to you on the next turn). Start by inspecting the working directory before
  changing anything.
- Do real work across as many turns as you need. When — and only when — the task
  is genuinely done, make a single call to "finish" with a summary.
- Reply with the JSON object and nothing else (no prose, no code fences).
'''
