"""Specialist invocation contract v1 (design §6.1). Pydantic models are the single definition used by the moderator,
specialists, eval and tests. Validated against the golden samples in `tests/fixtures`."""
from __future__ import annotations

import re
import time
from typing import Any, Literal

from pydantic import BaseModel, Field

CONTRACT_VERSION = "1"
Stance = Literal["support", "oppose", "partial", "gap", "not_addressed", "error"]
TAG_RE = re.compile(r"\[([A-Z]{1,3}\d+)\]")


class Claim(BaseModel):
    id: str = ""
    text: str
    stance: Stance = "partial"
    confidence: float = Field(0.6, ge=0, le=1)
    citations: list[str] = Field(default_factory=list)


class Source(BaseModel):
    id: str
    type: str = "uc_function"           # uc_function | vector_search | genie | external_db | bridge | doc
    ref: str = ""
    system: str | None = None
    template: str | None = None
    as_of: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    title: str | None = None
    snippet: str | None = None


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0


class ToolCall(BaseModel):
    tool: str
    server: str = ""
    template: str | None = None
    zone: str = "serverless"
    latency_ms: int = 0


class SpecialistAnswer(BaseModel):
    """Structured output the specialist LLM must produce (§5.3 step 4). Citations are the tag ids of tool results."""
    summary: str = Field(description="2-5 sentence answer to the brief, with [TAG] citations for every number or factual claim")
    claims: list[Claim] = Field(description="atomic claims; stance support/oppose/partial/gap/not_addressed relative to the brief")


class InvocationOutputs(BaseModel):
    specialist: str
    text: str = ""
    claims: list[Claim] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)
    tool_calls: list[ToolCall] = Field(default_factory=list)
    activity: list[dict[str, Any]] = Field(default_factory=list)
    trace_id: str | None = None
    usage: Usage = Field(default_factory=Usage)
    cached: bool = False


def request(brief: str, *, run_id: str, round: int, key: str, conversation_id: str = "", user_id: str = "", caller: str = "moderator",
            company_id: str | None = None, model_tier: str | None = None, prior_findings: list[dict] | None = None) -> dict:
    return {
        "input": [{"role": "user", "content": brief}],
        "custom_inputs": {
            "contract_version": CONTRACT_VERSION, "conversation_id": conversation_id, "run_id": run_id, "round": round,
            "caller": caller, "user_id": user_id, "company_id": company_id, "model_tier": model_tier,
            "idempotency_key": f"{run_id}:{round}:{key}", "prior_findings": prior_findings or [],
        },
        "stream": False,
    }


def response(out: InvocationOutputs) -> dict:
    return {
        "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": out.text}]}],
        "custom_outputs": out.model_dump(mode="json"),
    }


def parse_response(body: dict) -> InvocationOutputs:
    co = dict(body.get("custom_outputs") or {})
    if not co.get("text"):
        try:
            co["text"] = body["output"][0]["content"][0]["text"]
        except Exception:  # noqa: BLE001
            co["text"] = ""
    return InvocationOutputs.model_validate(co)


def error_outputs(key: str, message: str, stance: Stance = "error") -> InvocationOutputs:
    return InvocationOutputs(specialist=key, text=message, claims=[Claim(id="c1", text=message, stance=stance, confidence=1.0)])


def tags_in(text: str) -> list[str]:
    return TAG_RE.findall(text or "")


class Timer:
    def __init__(self) -> None:
        self.t0 = time.time()

    def ms(self) -> int:
        return int((time.time() - self.t0) * 1000)
