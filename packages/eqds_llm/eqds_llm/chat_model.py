"""LangChain / LangGraph adapter: graphs depend on a tier, never on an endpoint (design §8.2)."""
from __future__ import annotations

import json
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult as LCChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import ConfigDict

from .client import LLM


def to_openai(messages: list[BaseMessage]) -> list[dict]:
    out: list[dict] = []
    for m in messages:
        if isinstance(m, SystemMessage):
            out.append({"role": "system", "content": m.content if isinstance(m.content, str) else str(m.content)})
        elif isinstance(m, HumanMessage):
            out.append({"role": "user", "content": m.content if isinstance(m.content, str) else str(m.content)})
        elif isinstance(m, ToolMessage):
            out.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content if isinstance(m.content, str) else json.dumps(m.content)})
        elif isinstance(m, AIMessage):
            d: dict[str, Any] = {"role": "assistant", "content": m.content if isinstance(m.content, str) else (str(m.content) if m.content else None)}
            if m.tool_calls:
                d["tool_calls"] = [{"id": tc["id"], "type": "function", "function": {"name": tc["name"], "arguments": json.dumps(tc["args"])}} for tc in m.tool_calls]
                if not d["content"]:
                    d["content"] = None
            out.append(d)
        else:
            out.append({"role": "user", "content": str(m.content)})
    return out


class EqdsChatModel(BaseChatModel):
    """Wraps `eqds_llm.LLM` for one tier. Usage and cost accumulate on `.usage` for the run's budget."""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    llm: Any
    tier: str = "standard"
    max_tokens: int | None = None
    temperature: float | None = None
    bound_tools: list[dict] | None = None
    usage: dict = {}

    @property
    def _llm_type(self) -> str:
        return f"eqds-{self.tier}"

    def bind_tools(self, tools, **kw):  # type: ignore[override]
        return self.model_copy(update={"bound_tools": [convert_to_openai_tool(t) for t in tools]})

    def _generate(self, messages, stop=None, run_manager=None, **kw) -> LCChatResult:
        res = self.llm.chat(self.tier, to_openai(messages), tools=self.bound_tools, max_tokens=self.max_tokens, temperature=self.temperature)
        u = self.usage
        u["input_tokens"] = u.get("input_tokens", 0) + res.usage.input
        u["output_tokens"] = u.get("output_tokens", 0) + res.usage.output
        u["cost_usd"] = round(u.get("cost_usd", 0.0) + res.cost_usd, 6)
        msg = AIMessage(content=res.text, tool_calls=[{"id": t.id, "name": t.name, "args": t.arguments, "type": "tool_call"} for t in res.tool_calls],
                        response_metadata={"endpoint": res.endpoint, "finish_reason": res.finish_reason},
                        usage_metadata={"input_tokens": res.usage.input, "output_tokens": res.usage.output, "total_tokens": res.usage.input + res.usage.output})
        return LCChatResult(generations=[ChatGeneration(message=msg)])
