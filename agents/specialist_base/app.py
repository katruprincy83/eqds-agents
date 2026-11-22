"""Specialist app entrypoint (design §5.3, §6.1). One code base; identity and tools come from `specialist.yaml`."""
from __future__ import annotations

import logging
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from apps.common import env as get_env
from apps.common import pool
from eqds_agentkit import auth, contract, tracing
from eqds_agentkit.memory import Store

from .agent import SpecialistAgent

log = logging.getLogger("specialist.app")
app = FastAPI(title="eqds specialist")
agent: SpecialistAgent | None = None


@app.on_event("startup")
async def startup():
    global agent
    from databricks.sdk import WorkspaceClient

    env = get_env()
    tracing.setup(os.environ.get("EQDS_APP_VERSION", "dev"))
    store = None
    try:
        store = Store(pool())
    except Exception as e:  # noqa: BLE001
        log.warning("lakebase unavailable, running without memory: %s", e)
    agent = SpecialistAgent(os.environ.get("SPECIALIST_CONFIG", "specialist.yaml"), env, WorkspaceClient(), store, root=os.environ.get("EQDS_ROOT", "."))
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
    user = auth.user_from_headers(req.headers)
    out = await agent.invoke(body, user)
    return JSONResponse(contract.response(out))
