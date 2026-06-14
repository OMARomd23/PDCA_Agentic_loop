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
