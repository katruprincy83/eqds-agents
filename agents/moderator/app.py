"""eqds-moderator app (design §5.2, §6.2): run API, SSE from Lakebase events, reaper, resume, synchronous /invocations."""
from __future__ import annotations

import asyncio
import time
import json
import logging
import os
import uuid

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from apps.common import env as get_env
from apps.common import pool
from eqds_agentkit import auth, lakebase, tracing
from eqds_agentkit.memory import Store
from eqds_agentkit.registry import Registry
from eqds_agentkit.skills import PromptStore
from eqds_llm import LLM, load_model_config

from .graph import CLASS_ABORT, Moderator

log = logging.getLogger("moderator.app")
app = FastAPI(title="eqds moderator")
M: Moderator | None = None
STORE: Store | None = None
TERMINAL = ("DONE", "ERROR", "CANCELLED")
_tasks: set[asyncio.Task] = set()


class RunReq(BaseModel):
    question: str = Field(min_length=3, max_length=2000)
    rounds: int = Field(3, ge=1, le=5)
    specialists: list[str] | None = None
    company_id: str | None = Field(None, pattern=r"^C\d{4}$")
    conversation_id: str | None = None
    caller: str = "ui"


@app.on_event("startup")
async def startup():
    global M, STORE
    from databricks.sdk import WorkspaceClient

    env = get_env()
    tracing.setup(os.environ.get("EQDS_APP_VERSION", "dev"))
    root = os.environ.get("EQDS_ROOT", ".")
    w = WorkspaceClient()
    p = pool(max_size=12)
    STORE = Store(p)
    saver_pool = lakebase.RefreshingPool(p.t, min_size=1, max_size=8, options="-c search_path=lg", saver=True)
    cfg = load_model_config(os.path.join(root, env.get("models_config", "config/models.demo.yaml")), env.prefix, env.suffix, env.catalog, env.schema)
    prompts = PromptStore(env.volume_path("prompts"), os.path.join(root, "prompts"), w)
    M = Moderator(env, LLM(cfg), STORE, Registry(p, 60, env.target), prompts, saver_pool.pool, w, os.environ.get("DATABRICKS_WAREHOUSE_ID"))
    asyncio.create_task(reaper())


async def reaper():
    """Every 60 s resume runs whose heartbeat is stale (replica died / restarted): design §5.2.2, AC-04."""
    await asyncio.sleep(5)
    while True:
        try:
            for r in await asyncio.to_thread(STORE.stale_runs, 60):
                if await asyncio.to_thread(STORE.claim_stale, r["run_id"], 60):
                    log.warning("reaper resuming %s", r["run_id"])
                    spawn(M.resume, r["run_id"])
        except Exception as e:  # noqa: BLE001
            log.warning("reaper error: %s", str(e)[:120])
        await asyncio.sleep(30)


def spawn(fn, *a) -> None:
    t = asyncio.create_task(asyncio.to_thread(fn, *a))
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


@app.get("/healthz")
async def healthz():
    return {"ok": M is not None}


@app.get("/specialists")
async def specialists():
    return [s.__dict__ for s in M.reg.all(include_disabled=True)]


@app.post("/registry/refresh")
async def registry_refresh():
    """Apply a registry change now instead of waiting for the 60 s cache."""
    M.reg.refresh()
    return [{"key": s.key, "enabled": s.enabled} for s in M.reg.all(include_disabled=True)]


def _create(req: RunReq, user: str) -> tuple[str, str]:
    run_id = "r-" + uuid.uuid4().hex[:12]
    conv = req.conversation_id or "c-" + uuid.uuid4().hex[:8]
    STORE.create_run(run_id, conv, user, req.caller, req.question, req.rounds, req.specialists, req.company_id, M.defaults)
    return run_id, conv


@app.post("/runs", status_code=202)
async def create_run(req: RunReq, request: Request):
    user = auth.user_from_headers(request.headers)
    if not await asyncio.to_thread(M.reg.all):
        raise HTTPException(409, "no specialist agents are registered: register one on the Specialist registry page")
    run_id, conv = await asyncio.to_thread(_create, req, user)
    spawn(M.run_new, run_id)
    return {"run_id": run_id, "conversation_id": conv}


@app.get("/runs/{run_id}")
async def get_run(run_id: str):
    r = await asyncio.to_thread(STORE.get_run, run_id)
    if not r:
        raise HTTPException(404, "unknown run")
    return {"run_id": run_id, "status": r["status"], "stage": r["stage"], "result": r["result"], "error": r["error"], "question": r["question"],
            "conversation_id": r["conversation_id"], "cost_usd": float(r["cost_usd"]) if r["cost_usd"] is not None else None, "trace_id": r["trace_id"]}


@app.get("/runs/{run_id}/events")
async def events(run_id: str, request: Request):
    last = int(request.headers.get("last-event-id") or request.query_params.get("last_event_id") or 0)

    async def gen():
        nonlocal last
        idle = 0
        t_open = time.monotonic()
        while True:
            if await request.is_disconnected():
                return
            if time.monotonic() - t_open > 240:  # end cleanly before the Apps gateway's ~300 s cap; EventSource reconnects with Last-Event-ID
                return
            evs = await asyncio.to_thread(STORE.events_after, run_id, last)
            for e in evs:
                last = e["seq"]
                yield {"id": str(e["seq"]), "event": e["type"], "data": json.dumps(e["payload"], default=str)}
            if evs:
                idle = 0
            else:
                idle += 1
                run = await asyncio.to_thread(STORE.get_run, run_id)
                if run and run["status"] in TERMINAL and idle >= 2:
                    return
                if not run and idle > 6:
                    return
            await asyncio.sleep(0.5)

    return EventSourceResponse(gen(), ping=15)


@app.post("/runs/{run_id}/resume", status_code=202)
async def resume(run_id: str):
    r = await asyncio.to_thread(STORE.get_run, run_id)
    if not r:
        raise HTTPException(404, "unknown run")
    if r["status"] in TERMINAL:
        raise HTTPException(409, f"run already {r['status']}")
    spawn(M.resume, run_id)
    return {"run_id": run_id, "status": "RESUMING"}


@app.post("/runs/{run_id}/crash", status_code=202)
async def crash(run_id: str, request: Request):
    """Developer console (AC-04): simulate the replica dying mid-run. The reaper (or POST /resume) continues it."""
    CLASS_ABORT.add(run_id)
    return {"run_id": run_id, "crash_requested": True}


@app.get("/history")
async def history(request: Request, company_id: str | None = None, user: str | None = None):
    u = user or auth.user_from_headers(request.headers)
    rows = await asyncio.to_thread(STORE.history, u, company_id, 10)
    return [{**r, "created_at": r["created_at"].isoformat() if r["created_at"] else None, "cost_usd": float(r["cost_usd"]) if r["cost_usd"] is not None else None} for r in rows]


@app.post("/invocations")
async def invocations(body: dict, request: Request):
    """Synchronous ResponsesAgent wrapper (eval, wiki, Ada): runs to completion and returns the RunResult."""
    q = next((m["content"] for m in reversed(body.get("input", [])) if m.get("role") == "user"), "")
    ci = body.get("custom_inputs") or {}
    req = RunReq(question=q, rounds=int(ci.get("rounds", 3)), specialists=ci.get("specialists"), company_id=ci.get("company_id"),
                 conversation_id=ci.get("conversation_id"), caller=ci.get("caller", "eval"))
    user = ci.get("user_id") or auth.user_from_headers(request.headers)
    run_id, _ = await asyncio.to_thread(_create, req, user)
    await asyncio.to_thread(M.run_new, run_id)
    r = await asyncio.to_thread(STORE.get_run, run_id)
    if r["status"] != "DONE":
        return JSONResponse({"error": r["error"], "status": r["status"]}, status_code=502)
    res = r["result"]
    return {"output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": res["answer"]["text"]}]}], "custom_outputs": res}
