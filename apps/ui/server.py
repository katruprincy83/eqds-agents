"""eqds-ui backend (design §5.1): serves the research UI and proxies the moderator. FastAPI + a static SPA."""
from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
import pathlib
import time

import httpx
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from apps.common import env as get_env
from apps.common import pool
from eqds_agentkit import auth, tracing
from eqds_agentkit.memory import Store
from eqds_agentkit.sql import lit, run_statement

log = logging.getLogger("ui")
HERE = pathlib.Path(__file__).parent
ROOT = pathlib.Path(os.environ.get("EQDS_ROOT", "."))
app = FastAPI(title="eqds ui")
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
_w = None
_cache: dict[str, tuple[float, object]] = {}


def W():
    global _w
    if _w is None:
        from databricks.sdk import WorkspaceClient

        _w = WorkspaceClient()
    return _w


def store() -> Store:
    return Store(pool())


def moderator_url() -> str:
    try:
        return get_env().app_url(W(), "moderator", "MODERATOR_URL")
    except Exception as ex:  # noqa: BLE001
        raise HTTPException(503, f"moderator app not resolvable: {ex}")


def mod_headers(request: Request) -> dict[str, str]:
    h = {**auth.bearer(W()), "x-eqds-user": auth.user_from_headers(request.headers)}
    return h


def cached(key: str, ttl: float, fn):
    t = _cache.get(key)
    if t and time.time() - t[0] < ttl:
        return t[1]
    v = fn()
    _cache[key] = (time.time(), v)
    return v


def sql(q: str) -> list[dict]:
    wh = os.environ.get("DATABRICKS_WAREHOUSE_ID")
    if not wh:
        raise HTTPException(503, "no SQL warehouse configured")
    return run_statement(W(), wh, q)


@app.on_event("startup")
async def startup():
    tracing.setup(os.environ.get("EQDS_APP_VERSION", "dev"), autolog=False)


@app.get("/")
async def index():
    return FileResponse(HERE / "static" / "index.html")


@app.get("/healthz")
async def healthz():
    return {"ok": True}


# ── config ──────────────────────────────────────────────────────────────────────────────────────
def is_developer(request: Request) -> bool:
    grp = get_env().get("ui.developer_group", "")
    if not grp:
        return True
    user = auth.user_from_headers(request.headers)
    try:
        u = next(iter(W().users.list(filter=f'userName eq "{user}"')), None)
        return bool(u and any(g.display == grp for g in (u.groups or [])))
    except Exception:  # noqa: BLE001
        return False


@app.get("/api/me")
async def me(request: Request):
    e = get_env()
    return {"user": auth.user_from_headers(request.headers), "developer": await asyncio.to_thread(is_developer, request), "demo_player": bool(e.get("ui.demo_player", False)),
            "app_name": e.get("ui.app_name", "EQDS Research"), "subtitle": e.get("ui.subtitle", "Multi-agent research platform")}


@app.get("/api/config/links")
async def links():
    e = get_env()
    jid = lambda k: os.environ.get(f"JOB_ID_{k}", "")  # noqa: E731
    return {"ws": e.host, "catalog": e.catalog, "schema": e.schema, "appSuffix": e.suffix, "namePrefix": e.prefix, "exp": os.environ.get("MLFLOW_EXPERIMENT_ID", ""),
            "genie": ("" if os.environ.get("GENIE_SPACE_ID", "") == "unset" else os.environ.get("GENIE_SPACE_ID", "")), "lakebase": e.lakebase_project, "bridgeJobId": jid("BRIDGE_WORKER"), "ingestJobId": jid("INGEST_NEWS"),
            "wikiJobId": jid("WIKI_REFRESH"), "seedJobId": jid("VALIDATE_SEED"), "evalJobId": jid("EVAL"), "foreignCatalog": e.foreign_catalog, "connection": e.connection,
            "github": e.get("github_repo_url", ""), "vsEndpoint": e.vs_endpoint, "demo": e.target}


# ── runs (proxy to the moderator) ───────────────────────────────────────────────────────────────
class AskReq(BaseModel):
    question: str = Field(min_length=3, max_length=2000)
    rounds: int = 3
    specialists: list[str] | None = None
    company_id: str | None = None
    conversation_id: str | None = None
    caller: str = "ui"


@app.post("/api/ask")
async def ask(req: AskReq, request: Request):
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{moderator_url()}/runs", json=req.model_dump(), headers=mod_headers(request))
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.text[:300])
    return r.json()


@app.get("/api/runs/{run_id}/stream")
async def stream(run_id: str, request: Request):
    last = request.headers.get("last-event-id") or request.query_params.get("last_event_id") or "0"

    async def gen():
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=None)) as c:
            async with c.stream("GET", f"{moderator_url()}/runs/{run_id}/events", headers={**mod_headers(request), "last-event-id": last}) as r:
                async for chunk in r.aiter_raw():
                    yield chunk

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/runs/{run_id}")
async def run(run_id: str, request: Request):
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{moderator_url()}/runs/{run_id}", headers=mod_headers(request))
    return JSONResponse(r.json(), status_code=r.status_code)


@app.post("/api/runs/{run_id}/resume")
async def resume(run_id: str, request: Request):
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{moderator_url()}/runs/{run_id}/resume", headers=mod_headers(request))
    return JSONResponse(r.json(), status_code=r.status_code)


@app.post("/api/runs/{run_id}/crash")
async def crash(run_id: str, request: Request):
    if not await asyncio.to_thread(is_developer, request):
        raise HTTPException(403, "developers only")
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{moderator_url()}/runs/{run_id}/crash", headers=mod_headers(request))
    return JSONResponse(r.json(), status_code=r.status_code)


@app.get("/api/history")
async def history(request: Request, company_id: str | None = None):
    rows = await asyncio.to_thread(store().history, auth.user_from_headers(request.headers), company_id, 10)
    return [{"run_id": r["run_id"], "question": r["question"], "status": r["status"], "company_id": r["company_id"], "when": r["created_at"].isoformat()} for r in rows]


class FeedbackReq(BaseModel):
    run_id: str
    trace_id: str | None = None
    value: str
    comment: str = ""


@app.post("/api/feedback")
async def feedback(f: FeedbackReq, request: Request):
    tid = f.trace_id
    if not tid:
        r = await asyncio.to_thread(store().get_run, f.run_id)
        tid = r and r.get("trace_id")
    if not tid:
        raise HTTPException(404, "no trace for this run")
    await asyncio.to_thread(tracing.log_feedback, tid, f.value == "up", f.comment, auth.user_from_headers(request.headers))
    return {"ok": True, "trace_id": tid}


@app.get("/api/specialists")
async def specialists(request: Request):
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{moderator_url()}/specialists", headers=mod_headers(request))
    return r.json()


# ── wiki ────────────────────────────────────────────────────────────────────────────────────────
@app.get("/api/wiki")
async def wiki(top: int = 50):
    uc = get_env().uc

    def load():
        return sql(f"""
          WITH h AS (SELECT company_id, max(rank) AS rk FROM {uc}.ref_holdings GROUP BY company_id),
               p AS (SELECT company_id, count(DISTINCT page) AS pages, max(built_at) AS built_at, max(version) AS version FROM {uc}.wiki_pages GROUP BY company_id)
          SELECT c.company_id, c.name, c.ticker, c.gics_sector AS sector, h.rk AS holding_rank, p.pages, p.built_at, p.version,
                 s.last_refresh, s.pending_triggers, s.note
          FROM {uc}.ref_companies c LEFT JOIN h USING (company_id) LEFT JOIN p USING (company_id) LEFT JOIN {uc}.wiki_state s USING (company_id)
          WHERE p.pages IS NOT NULL OR h.rk <= {int(top)} ORDER BY sector, coalesce(h.rk, 9999), c.name""")

    return await asyncio.to_thread(cached, f"wiki{top}", 20, load)


@app.get("/api/wiki/{company_id}")
async def wiki_pages(company_id: str):
    if not company_id.startswith("C") or not company_id[1:].isdigit():
        raise HTTPException(400, "bad company id")
    uc = get_env().uc
    rows = await asyncio.to_thread(sql, f"""SELECT page, version, content_md, sources, built_at FROM {uc}.wiki_pages WHERE company_id = {lit(company_id)}
        QUALIFY row_number() OVER (PARTITION BY page ORDER BY version DESC) = 1 ORDER BY array_position(array('Home','Company','Industry','Catalysts','Debate','Internal View','External View','ESG','References'), page), page""")
    return rows


class WikiReq(BaseModel):
    company_id: str
    company_name: str = ""


@app.post("/api/wiki/request")
async def wiki_request(r: WikiReq, request: Request):
    user = auth.user_from_headers(request.headers)

    def go():
        with pool().connection() as c:
            rid = c.execute("INSERT INTO ops.wiki_requests (company_id, company_name, requested_by) VALUES (%s,%s,%s) RETURNING id", (r.company_id, r.company_name, user)).fetchone()["id"]
        job = os.environ.get("JOB_ID_WIKI_REFRESH")
        run_id = None
        if job:
            try:
                run_id = W().jobs.run_now(int(job), job_parameters={"only_company": r.company_id, "mode": "full"}).run_id
            except Exception as e:  # noqa: BLE001
                log.warning("wiki run_now failed: %s", e)
        return {"request_id": rid, "job_run_id": run_id}

    return await asyncio.to_thread(go)


@app.get("/api/wiki-queue")
async def wiki_queue():
    def go():
        with pool().connection() as c:
            return c.execute("SELECT id, company_id, company_name, status, detail, created_at FROM ops.wiki_requests ORDER BY id DESC LIMIT 10").fetchall()

    rows = await asyncio.to_thread(go)
    return [{**r, "created_at": r["created_at"].isoformat()} for r in rows]


# ── developer console ───────────────────────────────────────────────────────────────────────────
async def dev(request: Request):
    if not await asyncio.to_thread(is_developer, request):
        raise HTTPException(403, "developers only")


@app.get("/api/dev/runs/{run_id}/trace")
async def dev_trace(run_id: str, request: Request):
    await dev(request)

    def go():
        import mlflow

        eid = os.environ.get("MLFLOW_EXPERIMENT_ID")
        traces = mlflow.search_traces(locations=[eid] if eid else None, filter_string=f"tags.run_id = '{run_id}'", return_type="list", max_results=40)
        out = []
        for t in traces:
            spans = sorted(t.data.spans, key=lambda s: s.start_time_ns)
            depth = {}
            for s in spans:
                depth[s.span_id] = (depth.get(s.parent_id, -1) + 1) if s.parent_id else 0
            out.append({"trace_id": t.info.trace_id, "tags": dict(t.info.tags or {}), "duration_ms": t.info.execution_duration,
                        "spans": [{"name": s.name, "type": str(s.span_type), "level": depth.get(s.span_id, 0), "ms": int((s.end_time_ns - s.start_time_ns) / 1e6)} for s in spans][:200]})
        return out

    return await asyncio.to_thread(go)


@app.get("/api/dev/runs/{run_id}/invocations")
async def dev_invocations(run_id: str, request: Request):
    await dev(request)

    def go():
        with pool().connection() as c:
            return c.execute("SELECT specialist, idempotency_key, brief, response, created_at FROM mem.specialist WHERE idempotency_key LIKE %s ORDER BY created_at",
                             (run_id + ":%",)).fetchall()

    rows = await asyncio.to_thread(go)
    out = []
    for r in rows:
        _, rnd, key = r["idempotency_key"].split(":", 2)
        out.append({"key": key, "round": int(rnd), "idempotency_key": r["idempotency_key"],
                    "req": {"input": [{"role": "user", "content": r["brief"]}], "custom_inputs": {"contract_version": "1", "run_id": run_id, "round": int(rnd), "caller": "moderator", "idempotency_key": r["idempotency_key"]}},
                    "res": {"output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": (r["response"] or {}).get("text", "")}]}], "custom_outputs": r["response"]}})
    return out


@app.get("/api/dev/runs/{run_id}/memory")
async def dev_memory(run_id: str, request: Request):
    await dev(request)
    e = get_env()

    def go():
        s = store()
        run = s.get_run(run_id)
        with pool().connection() as c:
            turns = c.execute("SELECT turn, question, answer_summary, run_id FROM mem.conversation WHERE conversation_id = %s ORDER BY turn", (run["conversation_id"],)).fetchall() if run else []
            spec = c.execute("SELECT specialist, caller, idempotency_key, left(brief, 160) AS brief, created_at FROM mem.specialist WHERE conversation_id = %s ORDER BY created_at DESC LIMIT 20", (run["conversation_id"],)).fetchall() if run else []
            reg = c.execute("SELECT key, version FROM registry.specialists ORDER BY key").fetchall()
        return {"conversation_id": run and run["conversation_id"], "turns": turns, "specialist": [{**r, "created_at": r["created_at"].isoformat()} for r in spec],
                "skills": [{"agent": r["key"], "version": r["version"], "path": f"{e.volume_path('skills')}/{r['key']}/{r['version']}/SKILL.md"} for r in reg]}

    return await asyncio.to_thread(go)


@app.post("/api/dev/net/serverless-probe")
async def serverless_probe(request: Request):
    await dev(request)
    e = get_env()

    def go():
        t0 = time.time()
        try:
            wh = next((w for w in W().warehouses.list() if w.enable_serverless_compute), None)
            wid = wh.id if wh else os.environ.get("DATABRICKS_WAREHOUSE_ID")
            rows = run_statement(W(), wid, f"SELECT company_id, side, value_usd FROM {e.foreign_catalog}.oms.trades WHERE trade_date >= '2026-10-22' LIMIT 10")
            return {"ok": True, "blocked": False, "warehouse": wh.name if wh else wid, "serverless": bool(wh), "rows": rows, "ms": int((time.time() - t0) * 1000)}
        except Exception as ex:  # noqa: BLE001
            return {"ok": False, "blocked": True, "error": str(ex)[:600], "ms": int((time.time() - t0) * 1000)}

    return await asyncio.to_thread(go)


@app.post("/api/dev/net/bridge-probe")
async def bridge_probe(request: Request):
    await dev(request)

    async def go():
        from langchain_mcp_adapters.client import MultiServerMCPClient

        url = get_env().app_url(W(), "bridge-mcp", "BRIDGE_MCP_URL")
        url = url if url.endswith("/mcp") else url + "/mcp"
        t0 = time.time()
        c = MultiServerMCPClient({"bridge": {"transport": "streamable_http", "url": url, "headers": {**auth.bearer(W()), "x-eqds-user": auth.user_from_headers(request.headers), "x-eqds-caller": "ui-probe"}}})
        tools = {t.name: t for t in await c.get_tools()}
        res = await tools["oms_confirm_conflict"].ainvoke({"basket": "efm_top50", "start": "2026-10-22", "end": "2026-11-20", "strong_musd": 20})
        txt = res[0]["text"] if isinstance(res, list) else str(res)
        d = json.loads(txt)
        return {"ok": d.get("status") == "ok", "ms": int((time.time() - t0) * 1000), "status": d.get("status"), "source": d.get("source"), "summary": d.get("summary"),
                "rows": (d.get("rows") or [])[:10], "error": d.get("error")}

    try:
        return await go()
    except Exception as ex:  # noqa: BLE001
        return {"ok": False, "error": str(ex)[:500]}


@app.get("/api/dev/models")
async def dev_models(request: Request):
    await dev(request)
    e = get_env()
    path = ROOT / e.get("models_config", "config/models.demo.yaml")
    text = path.read_text() if path.exists() else ""
    from eqds_llm import load_model_config

    cfg = load_model_config(path, e.prefix, e.suffix, e.catalog, e.schema) if text else None

    def service(full):
        """Unity AI Gateway model service: state, destinations, inference table."""
        try:
            d = W().api_client.do("GET", f"/api/2.1/unity-catalog/model-services/{full}")
            c = d.get("config", {})
            dests = [x.get("pay_per_token_config", {}).get("model") or x.get("provisioned_throughput_config", {}).get("model_serving_endpoint") or x.get("name")
                     for x in c.get("routing", {}).get("destinations", [])]
            fb = [x.get("pay_per_token_config", {}).get("model", x.get("name")) for x in c.get("routing", {}).get("fallback", {}).get("destinations", [])]
            return {"state": "ACTIVE", "destinations": dests, "gateway_fallback": fb, "inference_table": (c.get("inference_table") or {}).get("table", ""),
                    "rate_limits": c.get("rate_limits", [])}
        except Exception as ex:  # noqa: BLE001
            return {"state": "MISSING", "error": str(ex)[:120]}

    def embed_state(name):
        try:
            s = W().serving_endpoints.get(name)
            return str(s.state.ready).split(".")[-1] if s.state else "?"
        except Exception:  # noqa: BLE001
            return "MISSING"

    tiers = []
    for k, t in (cfg.tiers.items() if cfg else []):
        info = await asyncio.to_thread(service, t.primary)
        tiers.append({"tier": k, "primary": t.primary, "fallback": t.fallback, **info})
    embed = cfg.embedding_endpoint if cfg else f"{e.prefix}-embed{e.suffix}"
    return {"tiers": tiers, "embed": {"endpoint": embed, "state": await asyncio.to_thread(embed_state, embed)}, "yaml": text}


@app.post("/api/dev/models/fault-test")
async def fault_test(request: Request):
    await dev(request)

    def go():
        import logging as lg

        from eqds_llm import LLM, load_model_config

        e = get_env()
        cfg = load_model_config(ROOT / e.get("models_config", "config/models.demo.yaml"), e.prefix, e.suffix, e.catalog, e.schema)
        lines: list[str] = []

        class H(lg.Handler):
            def emit(self, rec):
                lines.append(rec.getMessage())

        h = H()
        lg.getLogger("eqds_llm").addHandler(h)
        os.environ["EQDS_LLM_FAULT"] = "429:primary"
        os.environ["EQDS_LLM_FAULT_COUNT"] = str(cfg.retry.max_attempts)
        t0 = time.time()
        try:
            llm = LLM(cfg)
            r = llm.chat("standard", [{"role": "user", "content": "Reply with the single word: pong"}], max_tokens=20)
            return {"ok": True, "answered_by": r.endpoint, "primary": cfg.tier("standard").primary, "fell_back": r.endpoint != cfg.tier("standard").primary, "text": r.text[:40],
                    "ms": int((time.time() - t0) * 1000), "log": lines, "injected": f"429 x{cfg.retry.max_attempts} on {cfg.tier('standard').primary}"}
        except Exception as ex:  # noqa: BLE001
            return {"ok": False, "error": str(ex)[:300], "log": lines}
        finally:
            os.environ.pop("EQDS_LLM_FAULT", None)
            lg.getLogger("eqds_llm").removeHandler(h)

    return await asyncio.to_thread(go)


@app.get("/api/dev/seed/validation")
async def seed_validation(request: Request):
    await dev(request)
    uc = get_env().uc

    def go():
        try:
            return {"checks": sql(f"SELECT check_id, name, passed, expected, actual, detail, ran_at FROM {uc}.seed_validation_results ORDER BY check_no"), "ok": True}
        except Exception as e:  # noqa: BLE001
            return {"checks": [], "ok": False, "error": str(e)[:200]}

    return await asyncio.to_thread(go)


@app.post("/api/dev/seed/validate")
async def seed_validate(request: Request):
    await dev(request)
    job = os.environ.get("JOB_ID_VALIDATE_SEED")
    if not job:
        raise HTTPException(503, "validate job not configured")
    r = await asyncio.to_thread(lambda: W().jobs.run_now(int(job)))
    return {"run_id": r.run_id}


@app.get("/api/dev/registry")
async def dev_registry(request: Request):
    await dev(request)

    def go():
        with pool().connection() as c:
            return c.execute("SELECT key, display_name, tag_prefix, tools, app, version, owner, enabled, url, updated_at FROM registry.specialists ORDER BY key").fetchall()

    rows = await asyncio.to_thread(go)
    return [{**r, "updated_at": r["updated_at"].isoformat()} for r in rows]


class RegReq(BaseModel):
    action: str
    key: str


@app.post("/api/dev/registry")
async def dev_registry_post(r: RegReq, request: Request):
    await dev(request)
    e = get_env()

    def go():
        if r.action in ("enable", "disable"):
            with pool().connection() as c:
                c.execute("UPDATE registry.specialists SET enabled = %s, updated_at = now() WHERE key = %s", (r.action == "enable", r.key))
            return {"ok": True}
        if r.action == "register":
            f = ROOT / "agents" / "specialists" / r.key / "specialist.yaml"
            if not f.exists():
                raise HTTPException(404, f"no specialist.yaml for {r.key}")
            sp = yaml.safe_load(f.read_text())
            app_name = e.app_name(f"sp-{r.key}")
            url = W().apps.get(app_name).url
            from eqds_agentkit.registry import upsert_specialist

            with pool().connection() as c:
                upsert_specialist(c, sp, url, e.target, app_name)
            return {"ok": True, "url": url}
        raise HTTPException(400, "unknown action")

    return await asyncio.to_thread(go)


@app.get("/api/dev/eval/latest")
async def eval_latest(request: Request):
    await dev(request)

    def go():
        import mlflow

        eid = os.environ.get("MLFLOW_EXPERIMENT_ID")
        df = mlflow.search_runs(experiment_ids=[eid], filter_string="tags.eqds_eval = 'true'", order_by=["start_time DESC"], max_results=1)
        if df.empty:
            return {"run": None}
        r = df.iloc[0]
        metrics = {k.split("metrics.")[1]: float(v) for k, v in r.items() if k.startswith("metrics.") and v == v}
        th = yaml.safe_load((ROOT / "eval" / "thresholds.yaml").read_text()) if (ROOT / "eval" / "thresholds.yaml").exists() else {}
        return {"run": r["run_id"], "name": r.get("tags.mlflow.runName"), "started": str(r["start_time"]), "metrics": metrics, "thresholds": th.get("thresholds", {}), "n": r.get("params.n_questions")}

    try:
        return await asyncio.to_thread(go)
    except Exception as e:  # noqa: BLE001
        return {"run": None, "error": str(e)[:200]}


@app.post("/api/dev/eval/run")
async def eval_run(request: Request):
    await dev(request)
    job = os.environ.get("JOB_ID_EVAL")
    if not job:
        raise HTTPException(503, "eval job not configured")
    r = await asyncio.to_thread(lambda: W().jobs.run_now(int(job), job_parameters={"subset": "smoke", "rounds": "1"}))
    return {"run_id": r.run_id}


@app.get("/api/dev/ci")
async def dev_ci(request: Request):
    await dev(request)
    url = get_env().get("github_repo_url", "")
    if not url:
        return {"configured": False}
    repo = url.rstrip("/").split("github.com/")[-1]
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"https://api.github.com/repos/{repo}/actions/runs?per_page=5", headers={"Accept": "application/vnd.github+json"})
    if r.status_code != 200:
        return {"configured": True, "error": r.text[:200]}
    return {"configured": True, "runs": [{"name": x["name"], "status": x["status"], "conclusion": x["conclusion"], "branch": x["head_branch"], "url": x["html_url"], "title": x["display_title"]} for x in r.json().get("workflow_runs", [])]}
