"""Specialist app entrypoint (design §5.3, §6.1). One code base; identity and tools come from `specialist.yaml`."""
from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import time

import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from apps.common import env as get_env
from apps.common import pool
from eqds_agentkit import auth, contract, tracing
from eqds_agentkit.memory import AgentMemory

from .agent import SpecialistAgent

log = logging.getLogger("specialist.app")
app = FastAPI(title="eqds specialist")
agent: SpecialistAgent | None = None
# Registration grants CREATE on the database after the app has started, so a memory setup that failed at startup is retried
# on later calls, at most once per interval.
MEMORY_RETRY_S = 60.0
_memory_next_try = 0.0


def open_memory() -> AgentMemory:
    """The agent's own schema agent_<key>, created and owned by this app's service principal."""
    cfg_path = os.environ.get("SPECIALIST_CONFIG", "specialist.yaml")
    root = os.environ.get("EQDS_ROOT", ".")
    key = yaml.safe_load(pathlib.Path(cfg_path).read_text())["key"]
    store = AgentMemory(pool(), key)
    readers = [r for r in os.environ.get("EQDS_MEMORY_READERS", "").split(",") if r]  # the UI reads the memory of every agent
    applied = store.migrate([pathlib.Path(root) / "migrations"], readers=readers)
    log.info("agent memory %s ready (migrations applied: %s)", store.schema, applied or "none")
    return store


async def ensure_memory() -> None:
    global _memory_next_try
    if agent is None or agent.store is not None or time.monotonic() < _memory_next_try:
        return
    _memory_next_try = time.monotonic() + MEMORY_RETRY_S
    try:
        agent.store = await asyncio.to_thread(open_memory)
    except Exception as e:  # noqa: BLE001
        log.warning("lakebase still unavailable, running without memory (next retry in %.0f s): %s", MEMORY_RETRY_S, e)


@app.on_event("startup")
async def startup():
    global agent
    from databricks.sdk import WorkspaceClient

    env = get_env()
    tracing.setup(os.environ.get("EQDS_APP_VERSION", "dev"))
    cfg_path = os.environ.get("SPECIALIST_CONFIG", "specialist.yaml")
    root = os.environ.get("EQDS_ROOT", ".")
    try:
        store = open_memory()
    except Exception as e:  # noqa: BLE001
        log.warning("lakebase unavailable, running without memory (retried on the next call): %s", e)
        store = None
    agent = SpecialistAgent(cfg_path, env, WorkspaceClient(), store, root=root)
    try:
        await agent.warm()
    except Exception as e:  # noqa: BLE001
        log.warning("warm-up failed (will retry on first call): %s", e)


@app.get("/healthz")
async def healthz():
    return {"ok": agent is not None, "specialist": agent.key if agent else None}


@app.get("/info")
async def info():
    return agent.info()


@app.post("/invocations")
async def invocations(req: Request):
    body = await req.json()
    # any orchestrator can make a call idempotent: an Idempotency-Key header wins over custom_inputs.idempotency_key
    idem = req.headers.get("idempotency-key")
    if idem:
        ci = body.get("custom_inputs") or {}
        ci["idempotency_key"] = idem
        body["custom_inputs"] = ci
    await ensure_memory()
    user = auth.user_from_headers(req.headers)
    out = await agent.invoke(body, user, parent_trace=req.headers.get("x-eqds-parent-trace"))
    return JSONResponse(contract.response(out))
