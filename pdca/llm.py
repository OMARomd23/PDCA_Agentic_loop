"""Thin DeepSeek wrapper: one call function, a global token counter,
and JSON-mode with validate-or-retry-once semantics.

Every HTTP completion is also handed verbatim to the active session log
(prompt, raw completion, model, tokens, latency) — this is the single
chokepoint, so nothing the model said is discarded."""
import json
import time

from openai import OpenAI
from pydantic import BaseModel, ValidationError

from pdca import config
from pdca.core import session

# timeout: a single stalled request must never hang the loop silently.
_client = OpenAI(api_key=config.DEEPSEEK_API_KEY, base_url=config.BASE_URL,
                 timeout=300.0, max_retries=2)

tokens_used = 0


def _completion(model: str, system: str, user: str, json_mode: bool) -> tuple[str, int]:
    global tokens_used
    kwargs = {}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    t0 = time.monotonic()
    resp = _client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.0,
        **kwargs,
    )
    latency = time.monotonic() - t0
    used = resp.usage.total_tokens if resp.usage else 0
    tokens_used += used
    content = resp.choices[0].message.content or ""
    session.record_call(model, system, user, content, used, latency)
    return content, used


def _completion_msgs(model: str, messages: list, json_mode: bool) -> tuple[str, int]:
    """Like _completion but for a multi-turn messages list (the agent loop).
    Records the call through the same session chokepoint; to keep raw logs
    legible it stores the system prompt + the most recent user message as the
    'prompt' (the full transcript is reconstructable from the call sequence)."""
    global tokens_used
    kwargs = {}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    t0 = time.monotonic()
    resp = _client.chat.completions.create(
        model=model, messages=messages, temperature=0.0, **kwargs)
    latency = time.monotonic() - t0
    used = resp.usage.total_tokens if resp.usage else 0
    tokens_used += used
    content = resp.choices[0].message.content or ""
    sys_txt = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
    last_user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
    session.record_call(model, sys_txt, last_user, content, used, latency)
    return content, used


def _short_args(args: dict) -> str:
    parts = []
    for k, v in (args or {}).items():
        s = str(v).replace("\n", " ")
        parts.append(f"{k}={s[:40]}{'…' if len(s) > 40 else ''}")
    return ", ".join(parts)


def agent_loop(model: str, system: str, user: str, ctx, max_turns: int) -> dict:
    """Drive a turn-based tool-use conversation against `ctx` (a ToolContext).

    Each turn the model replies with a JSON object {"thought", "calls":[{tool,args}]}
    (json-mode, the protocol the harness already relies on). We dispatch every call
    in order, append the results, and loop until the model calls `finish` or the
    turn budget is exhausted. Returns an evidence dict shaped like the old single-
    script result so Check / state / runner consume it unchanged."""
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": user}]
    transcript: list[str] = []
    t0 = time.monotonic()
    turns = 0
    while turns < max_turns and not ctx.finished:
        turns += 1
        text, _ = _completion_msgs(model, messages, json_mode=True)
        messages.append({"role": "assistant", "content": text})
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as e:
            messages.append({"role": "user", "content":
                             f"Your reply was not valid JSON ({e}). Reply with ONLY "
                             "the action JSON object."})
            transcript.append(f"[turn {turns}] (invalid JSON, re-prompted)")
            continue
        thought = (obj.get("thought") or "").strip()
        if thought:
            transcript.append(f"[turn {turns}] {thought}")
        calls = obj.get("calls") or []
        if not calls:
            messages.append({"role": "user", "content":
                             "No tool calls. Issue at least one call, or call "
                             "'finish' when the work is genuinely complete."})
            continue
        results = []
        for c in calls:
            name = (c.get("tool") or "").strip()
            args = c.get("args") or {}
            out = ctx.dispatch(name, args)
            head = (out[:160].splitlines() or [""])[0]
            transcript.append(f"  → {name}({_short_args(args)}) → {head}")
            results.append(f"[{name}] {out}")
            if ctx.finished:
                break
        results_text = "\n".join(results)[:config.TOOL_OUTPUT_CAP]
        messages.append({"role": "user", "content": f"RESULTS:\n{results_text}"})

    elapsed = round(time.monotonic() - t0, 1)
    hit_cap = not ctx.finished
    transcript_text = "\n".join(transcript)
    stdout = transcript_text
    if ctx.summary:
        stdout = f"FINISH SUMMARY:\n{ctx.summary}\n\n{transcript_text}"
    if hit_cap:
        stdout = f"[hit {max_turns}-turn cap without finishing]\n{stdout}"
    return {
        "ok": not hit_cap,           # finished of its own accord vs forced handoff
        "stdout": stdout[:config.STDOUT_CAP],
        "stderr": ctx.last_error[:2000],
        "summary": ctx.summary,
        "turns": turns,
        "elapsed": elapsed,
        "transcript": transcript_text,
    }


def call(model: str, system: str, user: str, json_mode: bool = False):
    """Returns (text_or_parsed_json, usage_tokens). In json_mode, a parse
    failure is retried once with the error appended; second failure raises."""
    text, used = _completion(model, system, user, json_mode)
    if not json_mode:
        return text, used
    try:
        return json.loads(text), used
    except json.JSONDecodeError as e:
        retry_user = (
            f"{user}\n\nYour previous reply was not valid JSON "
            f"({e}). Reply again with ONLY a valid JSON object."
        )
        text, used2 = _completion(model, system, retry_user, json_mode=True)
        return json.loads(text), used + used2  # raises loudly on 2nd failure


def call_validated(model: str, system: str, user: str, schema: type[BaseModel]) -> BaseModel:
    """JSON call validated against a Pydantic schema; one retry with the
    validation error appended, then raise."""
    text, _ = _completion(model, system, user, json_mode=True)
    try:
        return schema.model_validate_json(text)
    except ValidationError as e:
        retry_user = (
            f"{user}\n\nYour previous JSON failed validation:\n{e}\n"
            f"Reply again with ONLY a JSON object matching the required schema."
        )
        text, _ = _completion(model, system, retry_user, json_mode=True)
        return schema.model_validate_json(text)  # raises loudly on 2nd failure
