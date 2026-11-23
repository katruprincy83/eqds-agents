"""Tool construction for specialists (design §5.3, §5.5).

Tool sources, all returning LangChain tools whose results are wrapped so every result carries citation tags:
* bridge MCP (custom app)                      → `DatabricksMultiServerMCPClient`-style streamable HTTP, always MCP
* UC functions / Vector Search                 → managed MCP when `flags.use_managed_mcp` is true, otherwise the SDK adapters below
  (same functions, same indexes, same arguments; selectable per environment because managed MCP is a preview feature).
  Managed Vector Search takes only `query`; company-filtered searches use the adapter so the filter runs on the server.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
import time
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field, create_model

from eqds_agentkit.contract import Source, ToolCall

log = logging.getLogger("specialist.tools")
ROW_TAG_TOOLS = {"news_latest", "web_search", "research_notes_search"}
MAX_ROWS_TO_LLM = 60


class ToolContext:
    """Per-invocation collector: assigns citation tags (`{prefix}{n}`), records sources, tool calls and activity events."""

    def __init__(self, tag_prefix: str):
        self.prefix, self.n = tag_prefix, 0
        self.sources: list[Source] = []
        self.tool_calls: list[ToolCall] = []
        self.activity: list[dict] = []

    def tag(self, **src) -> str:
        self.n += 1
        s = Source(id=f"{self.prefix}{self.n}", **src)
        self.sources.append(s)
        return s.id


# ── SDK adapters (UC functions, Vector Search) ───────────────────────────────────────────────────
_NUM = ("INT", "LONG", "BIGINT", "SHORT", "BYTE", "FLOAT", "DOUBLE", "DECIMAL")


def _lit(v: Any, type_text: str) -> str:
    t = type_text.upper()
    if v is None:
        return "NULL"
    if t.startswith("ARRAY"):
        return "ARRAY(" + ",".join(_lit(x, "STRING") for x in v) + ")"
    if t.startswith(_NUM):
        return str(float(v)) if t.startswith(("DOUBLE", "FLOAT", "DECIMAL")) else str(int(v))
    if t == "DATE":
        return "DATE'" + str(v)[:10] + "'"
    if t == "BOOLEAN":
        return "TRUE" if v else "FALSE"
    return "'" + str(v).replace("\\", "\\\\").replace("'", "''") + "'"


def _py_type(type_text: str):
    t = type_text.upper()
    if t.startswith("ARRAY"):
        return list[str]
    if t.startswith(("DOUBLE", "FLOAT", "DECIMAL")):
        return float
    if t.startswith(_NUM):
        return int
    if t == "DATE":
        return dt.date
    if t == "BOOLEAN":
        return bool
    return str


from eqds_agentkit.sql import run_statement  # noqa: E402


def uc_function_tools(w, catalog: str, schema: str, allow: list[str], warehouse_id: str) -> list[StructuredTool]:
    tools = []
    for fn in allow:
        full = f"{catalog}.{schema}.{fn}"
        info = w.functions.get(full)
        params = [(p.name, p.type_text or "STRING", p.comment or "") for p in (info.input_params.parameters if info.input_params else [])]
        fields = {n: (_py_type(t), Field(description=c or n)) for n, t, c in params}
        Args = create_model(f"{fn}_args", **fields)  # type: ignore[call-overload]
        ptypes = {n: t for n, t, _ in params}

        def make(fn=fn, full=full, ptypes=ptypes):
            def call(**kw):
                args = ", ".join(_lit(kw.get(n), t) for n, t in ptypes.items())
                return run_statement(w, warehouse_id, f"SELECT * FROM {full}({args})")
            return call

        impl = make()
        tools.append(StructuredTool.from_function(
            coroutine=lambda impl=impl, **kw: asyncio.to_thread(impl, **kw), name=fn,
            description=(info.comment or fn) + f" (Unity Catalog function {full})", args_schema=Args))
        tools[-1].metadata = {"kind": "uc_function", "ref": full, "server": "uc-functions"}
    return tools


class VSArgs(BaseModel):
    query: str = Field(description="natural-language search query")
    num_results: int = Field(6, ge=1, le=10)
    company_id: str | None = Field(None, description="restrict to one company id (e.g. C0101)", pattern=r"^C\d{4}$")


def vector_search_tools(w, catalog: str, schema: str, indexes: list[dict]) -> list[StructuredTool]:
    tools = []
    for ix in indexes:
        full = f"{catalog}.{schema}.{ix['name']}"
        cols = ix["columns"]

        def make(full=full, cols=cols):
            def call(query: str, num_results: int = 6, company_id: str | None = None):
                flt = json.dumps({"company_id": company_id}) if company_id else None
                r = w.vector_search_indexes.query_index(index_name=full, columns=cols, query_text=query, num_results=num_results,
                                                        filters_json=flt, query_type="HYBRID")
                names = [c.name for c in r.manifest.columns]
                return [dict(zip(names, row)) for row in (r.result.data_array or [])]
            return call

        impl = make()
        tools.append(StructuredTool.from_function(
            coroutine=lambda impl=impl, **kw: asyncio.to_thread(impl, **kw), name=ix["name"],
            description=ix.get("description", f"Semantic search over {ix['name']}"), args_schema=VSArgs))
        tools[-1].metadata = {"kind": "vector_search", "ref": full, "server": "vector-search"}
    return tools


# ── result wrapping: citation tags ───────────────────────────────────────────────────────────────
def compact(rows: list, drop: tuple = ("source_ref", "month_end")) -> list:
    """Smaller tool results for the LLM: no nulls, floats rounded, bookkeeping columns dropped (kept in the Source)."""
    out = []
    for r in rows:
        if not isinstance(r, dict):
            out.append(r)
            continue
        out.append({k: (round(v, 2) if isinstance(v, float) else v) for k, v in r.items() if v is not None and k not in drop})
    return out


def _short(s: Any, n: int) -> Any:
    return s[:n] + "…" if isinstance(s, str) and len(s) > n else s


def _parse_text(txt: str) -> Any:
    try:
        return json.loads(txt)
    except Exception:  # noqa: BLE001
        return txt


def _table_to_rows(x: Any) -> Any:
    """Managed UC functions return a table ({columns, rows}); the SDK path returns row dicts. Normalise to row dicts."""
    if isinstance(x, dict) and isinstance(x.get("columns"), list) and isinstance(x.get("rows"), list) and x["rows"] and isinstance(x["rows"][0], list):
        return [dict(zip(x["columns"], row)) for row in x["rows"]]
    return x


def _unwrap(raw: Any) -> Any:
    """MCP tools return a list of content blocks (or a str); SDK tools return python objects. Tables become row dicts."""
    if isinstance(raw, tuple):  # (content, artifact)
        raw = raw[0]
    if isinstance(raw, list) and raw and isinstance(raw[0], dict) and raw[0].get("type") == "text" and "text" in raw[0]:
        raw = _parse_text("".join(b.get("text", "") for b in raw))
    elif isinstance(raw, str):
        raw = _parse_text(raw)
    return _table_to_rows(raw)


def tagged(base: StructuredTool, ctx: ToolContext, kind: str, ref: str, server: str, uc_schema: str = "", fc: str = "azure_oms") -> StructuredTool:
    """Wrap a tool so its output is returned to the LLM with citation tags and a Source is recorded."""
    name = base.name

    async def run(**kw):
        t0 = time.time()
        try:
            raw = await base.ainvoke(kw)
        except Exception as e:  # noqa: BLE001
            ctx.tool_calls.append(ToolCall(tool=name, server=server, zone="external" if kind == "bridge" else "serverless", latency_ms=int((time.time() - t0) * 1000)))
            return json.dumps({"status": "error", "error": str(e)[:400]})
        data = _unwrap(raw)
        lat = int((time.time() - t0) * 1000)
        now = dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        if kind == "bridge":
            if not isinstance(data, dict) or data.get("status") != "ok":
                ctx.tool_calls.append(ToolCall(tool=name, server=server, zone="external", latency_ms=lat))
                return json.dumps(data if isinstance(data, dict) else {"status": "error", "error": str(data)[:300]}, default=str)
            src = data.get("source") or {}
            rows = (data.get("rows") or [])
            tmpl = src.get("template")
            ctx.tool_calls.append(ToolCall(tool=name, server=server, template=tmpl, zone="external", latency_ms=lat))
            ctx.activity.append({"component": "eqds-bridge-mcp", "zone": "external", "label": f"{name} → {src.get('object', '')}",
                                 "detail": f"{tmpl} · {data.get('bridge_latency_ms', lat) / 1000:.1f} s · {len(rows)} rows", "link": {"kind": "foreign", "ref": src.get("object", "")}})
            if name in ROW_TAG_TOOLS:
                out = []
                for r in rows[:10]:
                    ident = r.get("note_id") or r.get("news_id") or r.get("doc_id")
                    tag = ctx.tag(type="external_db", ref=f"{src.get('object')}#{ident}", system=src.get("system") or "azure_sql", template=tmpl, as_of=src.get("as_of", now),
                                  detail={"args": kw}, title=r.get("title") or r.get("headline"), snippet=_short(r.get("snippet") or r.get("headline"), 300))
                    out.append({"tag": tag, **{k: _short(v, 700) for k, v in r.items()}})
                return json.dumps({"status": "ok", "rows": out, "row_count": len(rows)}, default=str)
            tag = ctx.tag(type="external_db", ref=src.get("object", ref), system=src.get("system") or "azure_sql", template=tmpl, as_of=src.get("as_of", now), detail={"args": kw})
            body = {k: v for k, v in data.items() if k in ("rows", "summary", "truncated", "row_count")}
            body["rows"] = compact((body.get("rows") or [])[:MAX_ROWS_TO_LLM], drop=())
            return json.dumps({"tag": tag, "status": "ok", **body}, default=str)
        if kind == "vector_search":
            rows = data if isinstance(data, list) else (data.get("rows") if isinstance(data, dict) else []) or []
            ctx.tool_calls.append(ToolCall(tool=name, server=server, zone="serverless", latency_ms=lat))
            ctx.activity.append({"component": "vector-search", "zone": "serverless", "label": f"{name}", "detail": f"{len(rows)} chunks · {lat / 1000:.1f} s", "link": {"kind": "table", "ref": ref.rsplit('_idx', 1)[0]}})
            out = []
            for r in rows:
                title = r.get("broker") or r.get("expert_role") or r.get("doc_type") or ""
                when = r.get("published_at") or r.get("call_date")
                tag = ctx.tag(type="vector_search", ref=f"{ref}#{r.get('chunk_id')}", as_of=str(when) if when else None,
                              detail={"query": kw.get("query"), "company_id": r.get("company_id"), "doc": r.get("doc_id") or r.get("call_id")},
                              title=f"{title} {when or ''}".strip(), snippet=_short(r.get("text"), 400))
                out.append({"tag": tag, **{k: (_short(v, 900) if k == "text" else v) for k, v in r.items() if k != "chunk_id"}})
            return json.dumps({"status": "ok", "rows": out}, default=str)
        # uc_function
        rows = data if isinstance(data, list) else []
        ctx.tool_calls.append(ToolCall(tool=name, server=server, zone="serverless", latency_ms=lat))
        ctx.activity.append({"component": "unity-catalog", "zone": "serverless", "label": f"{name}()", "detail": f"{len(rows)} rows · {lat / 1000:.1f} s", "link": {"kind": "table", "ref": "ref_companies"}})
        ref_s = (rows[0].get("source_ref") if rows and isinstance(rows[0], dict) else None) or ref
        as_of = None
        if rows and isinstance(rows[0], dict):
            as_of = str(rows[0].get("month_end") or rows[0].get("to_date") or rows[0].get("event_date") or "") or None
        tag = ctx.tag(type="uc_function", ref=ref_s, as_of=as_of or now[:10], detail={"function": ref, "args": kw})
        return json.dumps({"tag": tag, "as_of": as_of, "rows": compact(rows[:MAX_ROWS_TO_LLM]), "row_count": len(rows)}, default=str, separators=(",", ":"))

    w = StructuredTool.from_function(coroutine=run, name=name, description=base.description, args_schema=base.args_schema)
    return w


async def build_bridge_tools(spec: dict, w, bridge_url: str, headers: dict[str, str]) -> list[tuple[StructuredTool, dict]]:
    """Per request: the MCP client carries the run/user headers so the bridge queue rows are attributable."""
    from langchain_mcp_adapters.client import MultiServerMCPClient

    from eqds_agentkit.auth import bearer

    allow = spec.get("allow", [])
    client = MultiServerMCPClient({"bridge": {"transport": "streamable_http", "url": bridge_url, "headers": {**bearer(w), **headers},
                                              "timeout": 120, "sse_read_timeout": 120}})
    return [(t, {"kind": "bridge", "ref": t.name, "server": "bridge-mcp"}) for t in await client.get_tools() if not allow or t.name in allow]


def managed_vector_search_tool(inner: StructuredTool, adapter: StructuredTool, short: str, description: str) -> StructuredTool:
    """Managed Vector Search takes only `query` and returns its top matches, so a company filter applied after the search
    would drop most results. Company-filtered calls therefore go to the SDK adapter, which filters on the server.
    Unfiltered calls use managed MCP. Both return the same row shape."""
    async def call(query: str, num_results: int = 6, company_id: str | None = None):
        if company_id:
            raw = await adapter.ainvoke({"query": query, "num_results": num_results, "company_id": company_id})
        else:
            raw = await inner.ainvoke({"query": query})
        rows = _unwrap(raw)
        rows = rows if isinstance(rows, list) else []
        return json.dumps(rows[:num_results], default=str)

    return StructuredTool.from_function(coroutine=call, name=short, description=description, args_schema=VSArgs)


async def build_static_tools(cfg: dict, env, w, warehouse_id: str | None) -> list[tuple[StructuredTool, dict]]:
    """UC functions and Vector Search tools (cached for the life of the app)."""
    out: list[tuple[StructuredTool, dict]] = []
    cat, sch = env.catalog, env.schema
    managed = bool(env.get("flags.use_managed_mcp", False))
    for spec in cfg.get("tools", []):
        typ, allow = spec["type"], spec.get("allow", [])
        if typ == "uc_functions":
            if managed:
                from databricks_langchain import DatabricksMCPServer, DatabricksMultiServerMCPClient

                c = DatabricksMultiServerMCPClient([DatabricksMCPServer(name="uc", url=f"{env.host}/api/2.0/mcp/functions/{cat}/{sch}", workspace_client=w, handle_tool_error=True)])
                for t in await c.get_tools():
                    short = t.name.split("__")[-1]
                    if not allow or short in allow:
                        t.name = short
                        out.append((t, {"kind": "uc_function", "ref": f"{cat}.{sch}.{short}", "server": "uc-functions-mcp"}))
            else:
                for t in await asyncio.to_thread(uc_function_tools, w, cat, sch, allow, warehouse_id):
                    out.append((t, t.metadata))
        elif typ == "vector_search":
            if managed:
                from databricks_langchain import DatabricksMCPServer, DatabricksMultiServerMCPClient

                c = DatabricksMultiServerMCPClient([DatabricksMCPServer(name="vs", url=f"{env.host}/api/2.0/mcp/vector-search/{cat}/{sch}", workspace_client=w, handle_tool_error=True)])
                desc = {i["name"]: i.get("description", "") for i in spec["indexes"]}
                adapters = {a.name: a for a in vector_search_tools(w, cat, sch, spec["indexes"])}
                for t in await c.get_tools():
                    short = t.name.split("__")[-1]
                    if short in desc:
                        vt = managed_vector_search_tool(t, adapters[short], short, desc[short] or f"Semantic search over {short}")
                        out.append((vt, {"kind": "vector_search", "ref": f"{cat}.{sch}.{short}", "server": "vector-search-mcp"}))
            else:
                for t in vector_search_tools(w, cat, sch, spec["indexes"]):
                    out.append((t, t.metadata))
    return out
