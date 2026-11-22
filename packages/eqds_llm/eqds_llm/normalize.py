"""Normalization of chat-completion responses (design §8.3): fixes the string-vs-list content breakage across models."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class Usage:
    input: int = 0
    output: int = 0


@dataclass
class ChatResult:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    reasoning: str = ""
    usage: Usage = field(default_factory=Usage)
    finish_reason: str | None = None
    model: str | None = None
    endpoint: str | None = None
    cost_usd: float = 0.0
    parsed: Any = None


def _get(o: Any, k: str, default=None):
    if isinstance(o, dict):
        return o.get(k, default)
    return getattr(o, k, default)


def _content_to_text(content: Any) -> tuple[str, str]:
    """str | list[parts] | None -> (text, reasoning)."""
    if content is None:
        return "", ""
    if isinstance(content, str):
        return content, ""
    text, reasoning = [], []
    for part in content:
        if isinstance(part, str):
            text.append(part)
            continue
        t = _get(part, "type")
        if t in ("text", "output_text", None):
            text.append(_get(part, "text", "") or "")
        elif t in ("reasoning", "thinking"):
            r = _get(part, "text") or _get(part, "thinking") or ""
            if not r:
                for s in _get(part, "summary", []) or []:
                    r += _get(s, "text", "") or ""
            reasoning.append(r)
    return "".join(text), "".join(reasoning)


def _args(a: Any) -> dict:
    if a is None or a == "":
        return {}
    if isinstance(a, dict):
        return a
    try:
        v = json.loads(a)
        return v if isinstance(v, dict) else {"value": v}
    except (TypeError, json.JSONDecodeError):
        return {"_raw": str(a)}


def normalize(raw: Any, endpoint: str | None = None) -> ChatResult:
    choices = _get(raw, "choices") or []
    msg = _get(choices[0], "message") if choices else None
    text, reasoning = _content_to_text(_get(msg, "content") if msg is not None else _get(raw, "content"))
    r2 = _get(msg, "reasoning_content") if msg is not None else None
    if r2 and not reasoning:
        reasoning = r2 if isinstance(r2, str) else str(r2)
    tcs = []
    for tc in (_get(msg, "tool_calls") or []) if msg is not None else []:
        fn = _get(tc, "function") or tc
        tcs.append(ToolCall(id=_get(tc, "id") or f"call_{len(tcs)}", name=_get(fn, "name"), arguments=_args(_get(fn, "arguments"))))
    u = _get(raw, "usage") or {}
    usage = Usage(int(_get(u, "prompt_tokens", _get(u, "input_tokens", 0)) or 0), int(_get(u, "completion_tokens", _get(u, "output_tokens", 0)) or 0))
    return ChatResult(text=text, tool_calls=tcs, reasoning=reasoning, usage=usage,
                      finish_reason=_get(choices[0], "finish_reason") if choices else None, model=_get(raw, "model"), endpoint=endpoint)


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def extract_json(text: str) -> Any:
    """Parse JSON from plain text, fenced blocks, or the first balanced object/array in the text."""
    t = text.strip()
    for cand in [t] + _FENCE.findall(t):
        try:
            return json.loads(cand.strip())
        except json.JSONDecodeError:
            pass
    for open_c, close_c in (("{", "}"), ("[", "]")):
        i = t.find(open_c)
        while i != -1:
            depth, in_s, esc = 0, False, False
            for j in range(i, len(t)):
                c = t[j]
                if in_s:
                    esc = (c == "\\") and not esc
                    if c == '"' and not esc:
                        in_s = False
                    continue
                if c == '"':
                    in_s = True
                elif c == open_c:
                    depth += 1
                elif c == close_c:
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(t[i:j + 1])
                        except json.JSONDecodeError:
                            break
            i = t.find(open_c, i + 1)
    raise ValueError("no JSON found in model output")


def parse_structured(text: str, schema: Any | None = None) -> Any:
    """Structured output returned as text -> parsed (and validated with a pydantic model class when given)."""
    data = extract_json(text)
    if schema is None:
        return data
    if hasattr(schema, "model_validate"):
        return schema.model_validate(data)
    return data
