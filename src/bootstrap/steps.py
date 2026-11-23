"""Bootstrap steps (design §4.2): everything DABs cannot manage, all idempotent. Order and conditions: bootstrap/manifest.yaml.

Each `<x>_apply(ctx)` creates-if-absent / updates; each `<x>_teardown(ctx)` removes what the step created.
"""
from __future__ import annotations

import glob
import io
import json
import os
import re
import time

import yaml

SCHEMAS_LB = ["ops", "mem", "registry", "bridge", "lg"]


def _read(ctx, rel: str) -> str:
    with open(os.path.join(ctx.root, rel)) as f:
        return f.read()


def _statements(sql: str) -> list[str]:
    out = []
    for part in re.split(r";\s*(?:\n|$)", re.sub(r"^\s*--.*$", "", sql, flags=re.M)):
        if part.strip():
            out.append(part.strip())
    return out


def _lb_target(ctx):
    from eqds_agentkit import lakebase

    e = ctx.env
    return lakebase.resolve(e.lakebase_project, e.get("lakebase.branch", "production"), e.lakebase_database, w=ctx.w)


# ── federation ───────────────────────────────────────────────────────────────────────────────────
def federation_apply(ctx) -> None:
    for stmt in _statements(ctx.render(_read(ctx, "bootstrap/sql/10_federation.sql"))):
        ctx.sql(stmt)
    ctx.say(f"connection {ctx.env.connection} + foreign catalog {ctx.env.foreign_catalog} ensured")


def federation_teardown(ctx) -> None:
    if ctx.env.shared_external:   # the connection and foreign catalog are shared with the environment that owns them
        ctx.say(f"shared_external: keeping connection {ctx.env.connection} and foreign catalog {ctx.env.foreign_catalog}")
        return
    ctx.sql(f"DROP CATALOG IF EXISTS {ctx.env.foreign_catalog} CASCADE")
    ctx.sql(f"DROP CONNECTION IF EXISTS {ctx.env.connection}")


# ── UC functions ─────────────────────────────────────────────────────────────────────────────────
def _function_files(ctx):
    return sorted(glob.glob(os.path.join(ctx.root, "src", "uc_functions", "*.sql")))


def uc_functions_apply(ctx) -> None:
    for f in _function_files(ctx):
        ctx.sql(open(f).read().replace("{uc}", ctx.env.uc))
        ctx.say(f"function {os.path.basename(f)[:-4]}")


def uc_functions_teardown(ctx) -> None:
    for f in _function_files(ctx):
        ctx.sql(f"DROP FUNCTION IF EXISTS {ctx.env.uc}.{os.path.basename(f)[:-4]}")


# ── Lakebase ─────────────────────────────────────────────────────────────────────────────────────
def lakebase_apply(ctx) -> None:
    from eqds_agentkit import lakebase

    t = _lb_target(ctx)
    conn = lakebase.connect(t)
    conn.execute("CREATE TABLE IF NOT EXISTS public.eqds_schema_migrations (version text PRIMARY KEY, applied_at timestamptz DEFAULT now())")
    done = {r["version"] for r in conn.execute("SELECT version FROM public.eqds_schema_migrations").fetchall()}
    for f in sorted(glob.glob(os.path.join(ctx.root, "bootstrap", "lakebase", "migrations", "*.sql"))):
        v = os.path.basename(f)
        if v in done:
            continue
        conn.execute(open(f).read())
        conn.execute("INSERT INTO public.eqds_schema_migrations (version) VALUES (%s)", (v,))
        ctx.say(f"lakebase migration applied: {v}")
    # LangGraph checkpoint tables in schema `lg` (checkpointer.setup())
    from langgraph.checkpoint.postgres import PostgresSaver

    lg = lakebase.connect(t, options="-c search_path=lg")
    PostgresSaver(lg).setup()
    lg.close()
    conn.close()
    ctx.say("langgraph checkpoint tables ensured in schema lg")


def lakebase_teardown(ctx) -> None:
    from eqds_agentkit import lakebase

    conn = lakebase.connect(_lb_target(ctx))
    for s in SCHEMAS_LB:
        conn.execute(f"DROP SCHEMA IF EXISTS {s} CASCADE")
    conn.execute("DROP TABLE IF EXISTS public.eqds_schema_migrations")
    conn.close()


# ── skills and prompts (UC volumes) ──────────────────────────────────────────────────────────────
def skills_apply(ctx) -> None:
    n = 0
    for sub in ("skills", "prompts"):
        base = os.path.join(ctx.root, sub)
        if not os.path.isdir(base):
            continue
        dest = ctx.env.volume_path(sub)
        for dp, _, files in os.walk(base):
            for fn in files:
                if fn.startswith("."):
                    continue
                src = os.path.join(dp, fn)
                rel = os.path.relpath(src, base)
                ctx.w.files.upload(f"{dest}/{rel}", io.BytesIO(open(src, "rb").read()), overwrite=True)
                n += 1
    ctx.say(f"uploaded {n} skill/prompt files")


def skills_teardown(ctx) -> None:
    for sub in ("skills", "prompts"):
        _rm_tree(ctx, ctx.env.volume_path(sub))


def _rm_tree(ctx, path: str) -> None:
    try:
        for e in ctx.w.files.list_directory_contents(path):
            if e.is_directory:
                _rm_tree(ctx, e.path)
                ctx.w.files.delete_directory(e.path)
            else:
                ctx.w.files.delete(e.path)
    except Exception:  # noqa: BLE001
        pass


# ── Vector Search ────────────────────────────────────────────────────────────────────────────────
def vector_search_apply(ctx, wait_s: int = 1500) -> None:
    from databricks.sdk.errors import NotFound
    from databricks.sdk.service import vectorsearch as vs

    cfg = yaml.safe_load(ctx.render(_read(ctx, "bootstrap/vector_search.yaml")))
    ep, emb = cfg["endpoint"], cfg["embedding_endpoint"]
    deadline = time.time() + wait_s
    while True:  # endpoint online
        try:
            e = ctx.w.vector_search_endpoints.get_endpoint(ep)
        except NotFound:
            ctx.w.vector_search_endpoints.create_endpoint(name=ep, endpoint_type=vs.EndpointType.STANDARD)
            continue
        if str(e.endpoint_status.state).endswith("ONLINE"):
            break
        if time.time() > deadline:
            raise TimeoutError(f"vector search endpoint {ep} not ONLINE: {e.endpoint_status.state}")
        time.sleep(15)
    while True:  # embedding endpoint ready
        s = ctx.w.serving_endpoints.get(emb)
        if s.state and str(s.state.ready).endswith("READY"):
            break
        if time.time() > deadline:
            raise TimeoutError(f"embedding endpoint {emb} not READY")
        time.sleep(15)
    for ix in cfg["indexes"]:
        full = f"{ctx.env.uc}.{ix['name']}"
        src = f"{ctx.env.uc}.{ix['source_table']}"
        try:
            ctx.w.tables.get(src)
        except NotFound:
            if ix.get("optional"):
                ctx.say(f"skip {full} (source {ix['source_table']} not landed yet)")
                continue
            raise
        try:
            ctx.w.vector_search_indexes.get_index(full)
            ctx.say(f"index {full} exists")
        except NotFound:
            ctx.w.vector_search_indexes.create_index(
                name=full, endpoint_name=ep, primary_key=ix["primary_key"], index_type=vs.VectorIndexType.DELTA_SYNC,
                delta_sync_index_spec=vs.DeltaSyncVectorIndexSpecRequest(
                    source_table=src, pipeline_type=vs.PipelineType.TRIGGERED, columns_to_sync=ix["sync"],
                    embedding_source_columns=[vs.EmbeddingSourceColumn(name=ix["text_column"], embedding_model_endpoint_name=emb)]))
            ctx.say(f"index {full} created")
    # trigger a sync for every index (idempotent; waits for ONLINE so retrieval checks can run)
    for ix in cfg["indexes"]:
        full = f"{ctx.env.uc}.{ix['name']}"
        try:
            ctx.w.vector_search_indexes.get_index(full)
        except NotFound:
            continue
        while True:
            st = ctx.w.vector_search_indexes.get_index(full).status
            if st.ready:
                try:
                    ctx.w.vector_search_indexes.sync_index(full)
                except Exception as e:  # noqa: BLE001
                    ctx.say(f"sync {full}: {str(e)[:80]}")
                break
            if time.time() > deadline:
                ctx.say(f"index {full} still provisioning; re-run bootstrap later")
                break
            time.sleep(20)
    ctx.outputs["vector_search_endpoint"] = ep


def vector_search_teardown(ctx) -> None:
    cfg = yaml.safe_load(ctx.render(_read(ctx, "bootstrap/vector_search.yaml")))
    for ix in cfg["indexes"]:
        try:
            ctx.w.vector_search_indexes.delete_index(f"{ctx.env.uc}.{ix['name']}")
        except Exception:  # noqa: BLE001
            pass


# ── Genie space ──────────────────────────────────────────────────────────────────────────────────
def _genie_payload(ctx) -> tuple[dict, str]:
    d = json.loads(ctx.render(_read(ctx, "bootstrap/genie/eqds_research_data.json")))
    n = [0]

    def gid() -> str:
        n[0] += 1
        return f"{n[0]:032x}"

    tables = sorted(({"identifier": f"{ctx.env.uc}.{t['name']}", "description": [t["description"]]} for t in d["tables"]), key=lambda x: x["identifier"])
    space = {
        "version": 2,
        "config": {"sample_questions": sorted(({"id": gid(), "question": [q]} for q in d["sample_questions"]), key=lambda x: x["id"])},
        "data_sources": {"tables": tables},
        "instructions": {
            "text_instructions": [{"id": gid(), "content": d["instructions"]}],
            "example_question_sqls": sorted(({"id": gid(), "question": [x["question"]], "sql": [x["sql"]]} for x in d.get("example_sqls", [])), key=lambda x: x["id"]),
        },
    }
    return space, d["title"]


def genie_apply(ctx) -> None:
    space, title = _genie_payload(ctx)
    existing = ctx.env.get("genie_space_id")
    if not existing:
        for sp in ctx.w.genie.list_spaces().spaces or []:
            if sp.title == title:
                existing = sp.space_id
    if existing:
        ctx.w.genie.update_space(space_id=existing, title=title, serialized_space=json.dumps(space))
        ctx.say(f"genie space {existing} updated")
        ctx.outputs["genie_space_id"] = existing
        return
    sp = ctx.w.genie.create_space(warehouse_id=ctx.warehouse_id(), serialized_space=json.dumps(space), title=title,
                                  description=json.loads(ctx.render(_read(ctx, "bootstrap/genie/eqds_research_data.json")))["description"])
    ctx.outputs["genie_space_id"] = sp.space_id
    ctx.say(f"genie space created: {sp.space_id}")


def genie_teardown(ctx) -> None:
    _, title = _genie_payload(ctx)
    for sp in ctx.w.genie.list_spaces().spaces or []:
        if sp.title == title:
            ctx.w.genie.trash_space(sp.space_id)


# ── Unity AI Gateway model services (created by `bundle deploy`, resources/serving/llm.yml) ───────
def model_services_apply(ctx) -> None:
    """Verify each tier's model service exists and answers through the gateway; record destinations and inference tables."""
    from eqds_llm import LLM, load_model_config

    cfg = load_model_config(os.path.join(ctx.root, ctx.env.get("models_config", f"config/models.{ctx.env.target}.yaml")),
                            ctx.env.prefix, ctx.env.suffix, ctx.env.catalog, ctx.env.schema)
    llm = LLM(cfg)
    out = {}
    for tier, t in cfg.tiers.items():
        try:
            d = ctx.w.api_client.do("GET", f"/api/2.1/unity-catalog/model-services/{t.primary}")
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"model service {t.primary} not found: run `make deploy` first ({str(e)[:100]})") from e
        table = (d.get("config", {}).get("inference_table") or {}).get("table", "")
        r = llm.chat(tier, [{"role": "user", "content": "Reply with the single word: pong"}], max_tokens=16)
        out[tier] = {"service": t.primary, "inference_table": table, "answered_by": r.endpoint}
        ctx.say(f"model service {t.primary} ok ({tier}); inference table {table or 'none'}")
    ctx.outputs["model_services"] = out


def model_services_teardown(ctx) -> None:
    """`bundle destroy` removes the services; their inference tables are UC tables and are dropped here."""
    for full in _model_service_names(ctx):
        sch, _, svc = full.rpartition(".")
        try:
            ctx.sql(f"DROP TABLE IF EXISTS {sch}.inference_{svc}_payload")
        except Exception as e:  # noqa: BLE001
            ctx.say(f"inference table {sch}.inference_{svc}_payload not dropped: {str(e)[:80]}")


# ── app grants (UC + Lakebase) ───────────────────────────────────────────────────────────────────
def access_of(sp: dict) -> dict:
    """Access spec for one specialist: its declared grants plus its Lakebase schema and Vector Search indexes."""
    indexes = [i["name"] for t in sp.get("tools", []) if t.get("type") == "vector_search" for i in t.get("indexes", [])]
    return {**(sp.get("access") or {}), "lakebase": ["mem"], "vector_indexes": indexes}


def _components(ctx) -> dict[str, dict]:
    """platform component -> access spec. Specialist agents live in their own repos; registration grants their access."""
    return dict(yaml.safe_load(_read(ctx, "bootstrap/app_access.yaml")))


def _lakebase_role(ctx, conn, comp: str, sp: str) -> None:
    """The app's service principal needs a Postgres role (OAuth) before it can be granted anything."""
    try:
        conn.execute("CREATE EXTENSION IF NOT EXISTS databricks_auth")
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (sp,)).fetchone()
        if not exists:
            conn.execute("SELECT databricks_create_role(%s, 'service_principal')", (sp,))
            ctx.say(f"lakebase role created for {comp}")
    except Exception as e:  # noqa: BLE001
        ctx.say(f"lakebase role for {comp} not created: {str(e)[:100]}")


def apply_component_grants(ctx, conn, comp: str, sp: str, spec: dict) -> None:
    """Unity Catalog, model service, embedding and Lakebase grants for one app's service principal."""
    uc = ctx.env.uc
    cat = ctx.env.catalog
    stmts = [f"GRANT USE CATALOG ON CATALOG {cat} TO `{sp}`", f"GRANT USE SCHEMA ON SCHEMA {uc} TO `{sp}`"]
    stmts += [f"GRANT SELECT ON TABLE {uc}.{t} TO `{sp}`" for t in spec.get("tables", [])]
    stmts += [f"GRANT EXECUTE ON FUNCTION {uc}.{f} TO `{sp}`" for f in spec.get("functions", [])]
    stmts += [f"GRANT READ VOLUME ON VOLUME {uc}.{v} TO `{sp}`" for v in spec.get("volumes", [])]
    stmts += [f"GRANT SELECT ON TABLE {uc}.{ix} TO `{sp}`" for ix in spec.get("vector_indexes", [])]
    for s in stmts:
        try:
            ctx.sql(s)
        except Exception as e:  # noqa: BLE001
            ctx.say(f"grant failed ({s[:70]}...): {str(e)[:100]}")
    if comp != "bridge-mcp":  # the bridge makes no LLM calls
        _grant_model_services(ctx, sp)
    if spec.get("vector_indexes"):  # managed Vector Search embeds the query on the embedding endpoint as the caller
        _grant_embedding_endpoint(ctx, sp)
    if conn is not None:
        _lakebase_role(ctx, conn, comp, sp)
        for s in spec.get("lakebase", []):
            for q in (f'GRANT USAGE ON SCHEMA {s} TO "{sp}"', f'GRANT ALL ON ALL TABLES IN SCHEMA {s} TO "{sp}"',
                      f'GRANT ALL ON ALL SEQUENCES IN SCHEMA {s} TO "{sp}"', f'GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA {s} TO "{sp}"'):
                try:
                    conn.execute(q)
                except Exception as e:  # noqa: BLE001
                    ctx.say(f"lakebase grant skipped for {comp}.{s}: {str(e)[:90]}")
                    break
    ctx.say(f"grants applied for {comp} ({sp})")


def app_grants_apply(ctx) -> None:
    from eqds_agentkit import lakebase

    conn = None
    try:
        conn = lakebase.connect(_lb_target(ctx))
    except Exception as e:  # noqa: BLE001
        ctx.say(f"lakebase unavailable, skipping role grants: {str(e)[:80]}")
    comps_all = _components(ctx)
    for comp, spec in comps_all.items():
        try:
            app = ctx.w.apps.get(ctx.env.app_name(comp))
        except Exception:  # noqa: BLE001
            ctx.say(f"app {ctx.env.app_name(comp)} not deployed yet: skipped")
            continue
        apply_component_grants(ctx, conn, comp, app.service_principal_client_id, spec)
    if conn is not None:
        conn.close()
    _grant_job_caller(ctx)
    registrar_grants(ctx)


def _model_service_names(ctx) -> list[str]:
    from eqds_llm.config import TIERS, service_name

    return [f"{ctx.env.uc}.{service_name(ctx.env.prefix, ctx.env.suffix, t)}" for t in TIERS]


def _grant_model_services(ctx, principal: str) -> None:
    """EXECUTE on the Unity AI Gateway model services (created by `bundle deploy`) lets the principal call the LLM tiers."""
    for full in _model_service_names(ctx):
        try:
            ctx.w.api_client.do("PATCH", f"/api/2.1/unity-catalog/permissions/model_service/{full}",
                                body={"changes": [{"principal": principal, "add": ["EXECUTE"]}]})
        except Exception as e:  # noqa: BLE001
            ctx.say(f"EXECUTE on model service {full} not granted to {principal}: {str(e)[:100]}")


def _grant_embedding_endpoint(ctx, principal: str) -> None:
    """CAN_QUERY on the project's embedding endpoint (the only serving endpoint) for principals that run Vector Search."""
    from databricks.sdk.service.serving import ServingEndpointAccessControlRequest, ServingEndpointPermissionLevel

    name = f"{ctx.env.prefix}-embed{ctx.env.suffix}"
    try:
        ep = ctx.w.serving_endpoints.get(name)
        ctx.w.serving_endpoints.update_permissions(
            serving_endpoint_id=ep.id,
            access_control_list=[ServingEndpointAccessControlRequest(service_principal_name=principal,
                                                                     permission_level=ServingEndpointPermissionLevel.CAN_QUERY)])
    except Exception as e:  # noqa: BLE001
        ctx.say(f"CAN_QUERY on {name} not granted to {principal}: {str(e)[:100]}")


def _grant_job_caller(ctx) -> None:
    """The job-caller service principal (created by `make infra`) may call the moderator app (eval + wiki jobs)."""
    from databricks.sdk.service.apps import AppAccessControlRequest, AppPermissionLevel

    name = f"{ctx.env.prefix}-job-caller{ctx.env.suffix}"
    try:
        found = list(ctx.w.service_principals.list(filter=f'displayName eq "{name}"'))
        if not found:
            ctx.say(f"{name} not found (run make infra); eval/wiki jobs will need EQDS_EVAL_TOKEN")
            return
        ctx.w.apps.update_permissions(app_name=ctx.env.app_name("moderator"),
                                      access_control_list=[AppAccessControlRequest(service_principal_name=found[0].application_id, permission_level=AppPermissionLevel.CAN_USE)])
        ctx.say(f"CAN_USE on {ctx.env.app_name('moderator')} granted to {name}")
    except Exception as e:  # noqa: BLE001
        ctx.say(f"job-caller grant skipped: {str(e)[:100]}")


def _grant_app_use(ctx, callee: str, callers: list[str]) -> None:
    """CAN_USE on one app for the service principals of several apps, one call at a time (concurrent updates conflict)."""
    from databricks.sdk.service.apps import AppAccessControlRequest, AppPermissionLevel

    callee_name = ctx.env.app_name(callee)
    for comp in callers:
        try:
            sp = ctx.w.apps.get(ctx.env.app_name(comp)).service_principal_client_id
            for attempt in range(4):
                try:
                    ctx.w.apps.update_permissions(app_name=callee_name, access_control_list=[AppAccessControlRequest(service_principal_name=sp, permission_level=AppPermissionLevel.CAN_USE)])
                    break
                except Exception as e:  # noqa: BLE001
                    if "CONFLICT" in str(e) and attempt < 3:
                        time.sleep(2 + attempt * 2)
                        continue
                    raise
            ctx.say(f"CAN_USE on {callee_name} granted to {comp}")
        except Exception as e:  # noqa: BLE001
            ctx.say(f"app grant {comp} -> {callee_name} skipped: {str(e)[:100]}")


def registrar_grants(ctx) -> None:
    """Grants for the registrar service principal (`make registrar`), which runs the registration job (resources/jobs/registry.yml).

    The job can only do what registration needs: read the catalog and grant on its objects (MANAGE on the catalog), use the
    warehouse, manage the specialist apps and the bridge it wires (CAN_MANAGE), manage the embedding endpoint and the model
    services (so it can grant them), and write `registry.specialists`. The deployer sets this up; the job never escalates itself.
    """
    from databricks.sdk.service.apps import AppAccessControlRequest, AppPermissionLevel
    from databricks.sdk.service.serving import ServingEndpointAccessControlRequest, ServingEndpointPermissionLevel
    from databricks.sdk.service.sql import WarehouseAccessControlRequest, WarehousePermissionLevel

    from eqds_agentkit import lakebase

    sp = (ctx.env.get("service_principals") or {}).get("registrar") or ""
    if not sp:
        ctx.say("registrar not configured (service_principals.registrar): run `make registrar` first")
        return
    cat = ctx.env.catalog
    # USE SCHEMA is needed to grant on objects inside a schema (MANAGE alone is not enough)
    for s in (f"GRANT USE CATALOG ON CATALOG {cat} TO `{sp}`", f"GRANT USE SCHEMA ON CATALOG {cat} TO `{sp}`", f"GRANT MANAGE ON CATALOG {cat} TO `{sp}`"):
        try:
            ctx.sql(s)
        except Exception as e:  # noqa: BLE001
            ctx.say(f"registrar grant failed ({s[:60]}...): {str(e)[:100]}")
    try:
        ctx.w.warehouses.set_permissions(warehouse_id=ctx.warehouse_id(),
                                         access_control_list=[WarehouseAccessControlRequest(service_principal_name=sp, permission_level=WarehousePermissionLevel.CAN_USE)])
    except Exception as e:  # noqa: BLE001
        ctx.say(f"registrar warehouse CAN_USE not granted: {str(e)[:100]}")
    for comp in _components(ctx):
        if not (comp.startswith("sp-") or comp == "bridge-mcp"):  # only the apps registration wires
            continue
        name = ctx.env.app_name(comp)
        for attempt in range(4):
            try:
                ctx.w.apps.update_permissions(app_name=name, access_control_list=[AppAccessControlRequest(service_principal_name=sp, permission_level=AppPermissionLevel.CAN_MANAGE)])
                ctx.say(f"CAN_MANAGE on {name} granted to the registrar")
                break
            except Exception as e:  # noqa: BLE001
                if "CONFLICT" in str(e) and attempt < 3:
                    time.sleep(2 + attempt * 2)
                    continue
                ctx.say(f"registrar CAN_MANAGE on {name} not granted: {str(e)[:100]}")
                break
    try:
        ep = ctx.w.serving_endpoints.get(f"{ctx.env.prefix}-embed{ctx.env.suffix}")
        ctx.w.serving_endpoints.update_permissions(serving_endpoint_id=ep.id,
                                                   access_control_list=[ServingEndpointAccessControlRequest(service_principal_name=sp, permission_level=ServingEndpointPermissionLevel.CAN_MANAGE)])
    except Exception as e:  # noqa: BLE001
        ctx.say(f"registrar CAN_MANAGE on the embedding endpoint not granted: {str(e)[:100]}")
    for full in _model_service_names(ctx):
        try:
            ctx.w.api_client.do("PATCH", f"/api/2.1/unity-catalog/permissions/model_service/{full}",
                                body={"changes": [{"principal": sp, "add": ["MANAGE"]}]})
        except Exception as e:  # noqa: BLE001
            ctx.say(f"registrar MANAGE on model service {full} not granted: {str(e)[:100]}")
    conn = lakebase.connect(_lb_target(ctx))
    try:
        _lakebase_role(ctx, conn, "registrar", sp)
        conn.execute(f'ALTER ROLE "{sp}" CREATEROLE')  # it creates the Postgres role of each newly registered agent
        conn.execute(f'GRANT USAGE ON SCHEMA registry TO "{sp}"')
        conn.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON registry.specialists TO "{sp}"')
        ctx.say("registrar: registry.specialists read/write granted")
        # the registrar grants each new agent's role access to `mem`; it does not own those objects, so it needs the grant option
        for q in (f'GRANT USAGE ON SCHEMA mem TO "{sp}" WITH GRANT OPTION',
                  f'GRANT ALL ON ALL TABLES IN SCHEMA mem TO "{sp}" WITH GRANT OPTION',
                  f'GRANT ALL ON ALL SEQUENCES IN SCHEMA mem TO "{sp}" WITH GRANT OPTION',
                  f'GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA mem TO "{sp}" WITH GRANT OPTION'):
            conn.execute(q)
        ctx.say("registrar: mem grant option applied")
    except Exception as e:  # noqa: BLE001
        ctx.say(f"registrar registry grant failed: {str(e)[:100]}")
    finally:
        conn.close()


def app_grants_teardown(ctx) -> None:
    pass  # revoked implicitly when the apps (and their service principals) are destroyed


# ── traces in Unity Catalog (flags.use_uc_trace_storage) ──────────────────────────────────────────
def _trace_experiments(ctx) -> list[str]:
    """The platform's experiments plus every agent experiment already deployed (`<experiment_path>-<key>`, created by the agents' bundles)."""
    import posixpath

    from databricks.sdk.service.workspace import ObjectType

    e = ctx.env
    paths = {e.experiment_path, e.moderator_experiment_path}
    try:
        for o in ctx.w.workspace.list(posixpath.dirname(e.experiment_path)):
            if o.object_type == ObjectType.MLFLOW_EXPERIMENT and o.path.startswith(e.experiment_path + "-"):
                paths.add(o.path)
    except Exception as ex:  # noqa: BLE001
        ctx.say(f"could not list agent experiments ({str(ex)[:80]}); binding the platform ones")
    return sorted(paths)


def trace_storage_apply(ctx) -> None:
    """Bind every experiment of this environment to the UC trace location, then grant the platform apps: writers on the platform
    experiments' tables, the UI SELECT on the whole traces schema. Agent apps are granted by their own `make traces`. Idempotent."""
    from eqds_agentkit import trace_storage as ts

    e = ctx.env
    wh = ctx.warehouse_id()
    for path in _trace_experiments(ctx):
        res = ts.bind(path, e.catalog, e.traces_schema, wh)
        ctx.say(f"traces of {path}: {res} ({e.catalog}.{e.traces_schema}.{ts.table_prefix(path)}_otel_*)")
        if res.startswith("conflict"):
            ctx.say("  already bound elsewhere: a binding is permanent, create a new experiment to move it")
    platform = [e.experiment_path, e.moderator_experiment_path]
    for comp in ("moderator", "bridge-mcp", "ui"):
        try:
            sp = ctx.w.apps.get(e.app_name(comp)).service_principal_client_id
        except Exception as ex:  # noqa: BLE001
            ctx.say(f"{e.app_name(comp)}: not deployed, trace grants skipped ({str(ex)[:60]})")
            continue
        for path in platform:
            ts.grant_writer(ctx.w, wh, e.catalog, e.traces_schema, path, sp)
        if comp == "ui":
            ts.grant_reader(ctx.w, wh, e.catalog, e.traces_schema, sp)
        ctx.say(f"trace tables granted to {e.app_name(comp)}")


def trace_storage_teardown(ctx) -> None:
    ctx.say("trace tables kept: an experiment's UC binding is permanent; drop the schema manually to discard the traces")


# ── specialist registry ──────────────────────────────────────────────────────────────────────────
def registry_apply(ctx) -> None:
    """Rows are written by registration only (UI registry page or `make register`); bootstrap reports what is there."""
    from eqds_agentkit import lakebase

    conn = lakebase.connect(_lb_target(ctx))
    rows = conn.execute("SELECT key, enabled FROM registry.specialists ORDER BY key").fetchall()
    conn.close()
    ctx.say("registry: " + (", ".join(f"{r['key']}{'' if r['enabled'] else ' (disabled)'}" for r in rows) or "empty (register agents on the UI registry page)"))


def registry_teardown(ctx) -> None:
    from eqds_agentkit import lakebase

    conn = lakebase.connect(_lb_target(ctx))
    conn.execute("DELETE FROM registry.specialists")
    conn.close()


# ── profile grants ───────────────────────────────────────────────────────────────────────────────
def grants_apply(ctx) -> None:
    n = 0
    for stmt in _statements(ctx.render(_read(ctx, "bootstrap/sql/20_grants.sql"))):
        if "``" in stmt:  # principal not configured in the profile: run as deployer (demo)
            continue
        try:
            ctx.sql(stmt)
            n += 1
        except Exception as e:  # noqa: BLE001
            ctx.say(f"grant skipped: {str(e)[:110]}")
    ctx.say(f"{n} profile grants applied")


def grants_teardown(ctx) -> None:
    pass


# ── outputs (written to the artifacts volume; `make outputs` pulls them locally) ─────────────────
def outputs_apply(ctx) -> None:
    out = dict(ctx.outputs)
    out["catalog"], out["schema"] = ctx.env.catalog, ctx.env.schema
    try:
        out["warehouse_id"] = ctx.warehouse_id()
    except Exception:  # noqa: BLE001
        pass
    try:
        out["experiment_id"] = ctx.w.experiments.get_by_name(ctx.env.experiment_path).experiment.experiment_id
    except Exception:  # noqa: BLE001
        pass
    out["apps"] = {c: ctx.app_url(c) for c in ["ui", "moderator", "bridge-mcp"]}
    path = f"{ctx.env.volume_path('artifacts')}/bootstrap-output-{ctx.env.target}.json"
    prev = {}
    try:
        prev = json.loads(ctx.w.files.download(path).contents.read())
    except Exception:  # noqa: BLE001
        pass
    prev.update({k: v for k, v in out.items() if v})
    ctx.w.files.upload(path, io.BytesIO(json.dumps(prev, indent=2).encode()), overwrite=True)
    ctx.say(f"outputs written to {path}: {sorted(prev)}")


def outputs_teardown(ctx) -> None:
    try:
        ctx.w.files.delete(f"{ctx.env.volume_path('artifacts')}/bootstrap-output-{ctx.env.target}.json")
    except Exception:  # noqa: BLE001
        pass
