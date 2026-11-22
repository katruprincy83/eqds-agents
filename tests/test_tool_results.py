"""Result shapes from the tool layer (SDK adapter and managed MCP) must reach the citation code as row dicts."""
import asyncio
import json

from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from agents.specialist_base import tools as T


def test_unwrap_mcp_text_block_is_parsed():
    raw = [{"type": "text", "text": json.dumps([{"company_id": "C0272", "text": "x"}])}]
    assert T._unwrap(raw) == [{"company_id": "C0272", "text": "x"}]


def test_unwrap_managed_table_becomes_row_dicts():
    table = {"is_truncated": False, "columns": ["company_id", "name"], "rows": [["C0102", "Weichai Power A"]]}
    assert T._unwrap(json.dumps(table)) == [{"company_id": "C0102", "name": "Weichai Power A"}]
    assert T._unwrap((table, None)) == [{"company_id": "C0102", "name": "Weichai Power A"}]


def test_unwrap_sdk_rows_with_text_field_are_not_mistaken_for_content_blocks():
    # Vector Search rows carry a "text" field but no "type"; they must not be joined and dropped
    rows = [{"chunk_id": "EX-1", "company_id": "C0272", "text": "Management highlighted demand."}]
    assert T._unwrap(rows) == rows


def test_unwrap_bridge_envelope_untouched():
    env = {"status": "ok", "rows": [{"doc_id": "a"}], "source": {"object": "x"}}
    assert T._unwrap(json.dumps(env)) == env


class _Args(BaseModel):
    query: str
    num_results: int = 6
    company_id: str | None = None


class _QueryOnly(BaseModel):
    query: str


def _fake_tool(name, payload, calls):
    # the managed server accepts only `query`
    async def run(query: str):
        calls.append(("managed", query))
        return json.dumps(payload)
    return StructuredTool.from_function(coroutine=run, name=name, description="managed", args_schema=_QueryOnly)


def _fake_adapter(name, payload, calls):
    async def run(query: str, num_results: int = 6, company_id: str | None = None):
        calls.append(("adapter", query, company_id))
        return payload
    return StructuredTool.from_function(coroutine=run, name=name, description="adapter", args_schema=_Args)


def test_managed_vector_search_unfiltered_uses_managed_and_caps_results():
    calls = []
    rows = [{"company_id": f"C{i:04d}", "text": "t"} for i in range(5)]
    inner = _fake_tool("expert_chunks_idx", rows, calls)
    adapter = _fake_adapter("expert_chunks_idx", [], calls)
    t = T.managed_vector_search_tool(inner, adapter, "expert_chunks_idx", "d")
    out = json.loads(asyncio.run(t.ainvoke({"query": "margin", "num_results": 2})))
    assert calls == [("managed", "margin")]
    assert len(out) == 2


def test_managed_vector_search_company_filter_goes_to_adapter_server_side():
    calls = []
    inner = _fake_tool("expert_chunks_idx", [{"company_id": "C0001"}], calls)
    adapter = _fake_adapter("expert_chunks_idx", [{"company_id": "C0272", "text": "t"}], calls)
    t = T.managed_vector_search_tool(inner, adapter, "expert_chunks_idx", "d")
    out = json.loads(asyncio.run(t.ainvoke({"query": "margin", "num_results": 3, "company_id": "C0272"})))
    assert calls == [("adapter", "margin", "C0272")]
    assert out == [{"company_id": "C0272", "text": "t"}]
