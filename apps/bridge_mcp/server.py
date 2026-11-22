"""eqds-bridge-mcp (design §5.6): MCP server (streamable HTTP) that exposes the allowlisted bridge tools to serverless
specialists. It never touches the sources: it queues a request in Lakebase `bridge.requests` and waits for the bridge
worker (which sits in the network zone that can reach Azure SQL / the web) to write the result."""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import logging
import time

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from psycopg.types.json import Jsonb
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from apps.common import env, pool
from eqds_agentkit.bridge_tools import TOOLS, validate

log = logging.getLogger("bridge_mcp")
WAIT_S = 30.0
ctx_headers: contextvars.ContextVar[dict] = contextvars.ContextVar("hdrs", default={})
server = Server("eqds-bridge")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    out = []
    for name, t in TOOLS.items():
        if name == "apollo_lookup" and not env().get("flags.apollo", False):
            continue
        schema = t["model"].model_json_schema()
        out.append(types.Tool(name=name, description=f"{t['doc']} (template {name}@{t['version']}; source {t['object']})", inputSchema=schema))
    return out


def _enqueue(tool: str, args: dict, hdrs: dict) -> str:
    with pool().connection() as c:
        return str(c.execute("""INSERT INTO bridge.requests (tool, args, run_id, caller, user_id)
                                VALUES (%s,%s,%s,%s,%s) RETURNING id""",
                             (tool, Jsonb(args), hdrs.get("x-eqds-run-id"), hdrs.get("x-eqds-caller"), hdrs.get("x-eqds-user"))).fetchone()["id"])


def _poll(rid: str) -> dict | None:
    with pool().connection() as c:
        return c.execute("SELECT status, result, error FROM bridge.requests WHERE id = %s", (rid,)).fetchone()


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    t0 = time.time()
    try:
        model = validate(name, arguments or {})
    except Exception as e:  # noqa: BLE001
        return [types.TextContent(type="text", text=json.dumps({"status": "invalid_arguments", "error": str(e)[:800]}))]
    args = model.model_dump(mode="json")
    rid = await asyncio.to_thread(_enqueue, name, args, ctx_headers.get())
    wait_s = float(env().get('bridge.wait_s', WAIT_S))
    deadline = t0 + wait_s
    delay = 0.25
    while time.time() < deadline:
        row = await asyncio.to_thread(_poll, rid)
        if row and row["status"] == "DONE":
            res = dict(row["result"] or {})
            res.update(status="ok", request_id=rid, tool=name, bridge_latency_ms=int((time.time() - t0) * 1000))
            return [types.TextContent(type="text", text=json.dumps(res, default=str))]
        if row and row["status"] in ("ERROR", "EXPIRED"):
            return [types.TextContent(type="text", text=json.dumps({"status": "error", "tool": name, "request_id": rid, "error": row["error"] or row["status"]}))]
        await asyncio.sleep(delay)
        delay = min(delay * 1.3, 1.0)
    def _expire():
        with pool().connection() as c:
            c.execute("UPDATE bridge.requests SET status='EXPIRED', finished_at=now() WHERE id=%s AND status IN ('PENDING','RUNNING')", (rid,))
    await asyncio.to_thread(_expire)
    return [types.TextContent(type="text", text=json.dumps({"status": "unavailable", "tool": name, "request_id": rid,
                                                          "error": f"bridge worker did not answer within {int(wait_s)}s"}))]


manager = StreamableHTTPSessionManager(app=server, stateless=True, json_response=True)


class HeaderMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            ctx_headers.set({k.decode().lower(): v.decode() for k, v in scope["headers"]})
        await self.app(scope, receive, send)


class McpASGI:
    async def __call__(self, scope, receive, send):
        await manager.handle_request(scope, receive, send)


async def health(_):
    st = {"ok": True}
    try:
        with pool().connection() as c:
            r = c.execute("""SELECT count(*) FILTER (WHERE status='PENDING') pending,
                                    count(*) FILTER (WHERE status='DONE' AND finished_at > now() - interval '5 minutes') done_5m,
                                    max(finished_at) last_done FROM bridge.requests""").fetchone()
        st.update({k: (str(v) if v is not None else None) for k, v in r.items()})
    except Exception as e:  # noqa: BLE001
        st = {"ok": False, "error": str(e)[:200]}
    return JSONResponse(st, status_code=200 if st["ok"] else 503)


@contextlib.asynccontextmanager
async def lifespan(app):
    async with manager.run():
        yield


app = Starlette(routes=[Route("/healthz", health), Route("/", health), Route("/mcp", McpASGI(), methods=["GET", "POST", "DELETE"])], lifespan=lifespan)
app.add_middleware(HeaderMiddleware)
