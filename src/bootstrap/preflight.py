"""Environment preflight (design §4.1 E10, NFR-22): verifies that a workspace can host the platform BEFORE deploying.
Used by `make preflight` (laptop/CI) and job_env_preflight. Every check returns (name, status, detail); status in ok|warn|fail."""
from __future__ import annotations

import os
import time
from dataclasses import dataclass

import yaml


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""


def run(env, w, root: str, spark=None) -> list[Check]:
    out: list[Check] = []

    def add(name, fn):
        try:
            status, detail = fn()
        except Exception as e:  # noqa: BLE001
            status, detail = "fail", str(e)[:200]
        out.append(Check(name, status, detail))

    def auth():
        me = w.current_user.me()
        return "ok", f"{me.user_name} on {env.host}"

    def catalog():
        c = w.catalogs.get(env.catalog)
        return "ok", f"catalog {c.name} exists"

    def secrets():
        keys = {s.key for s in w.secrets.list_secrets(env.secret_scope)}
        need = {"azure-sql-user", "azure-sql-password"} if env.external_db_mode in ("bridge", "direct") else set()
        miss = need - keys
        return ("fail", f"missing in scope {env.secret_scope}: {sorted(miss)}; run `make infra` with EQDS_AZURE_SQL_USER/PASSWORD") if miss else ("ok", f"scope {env.secret_scope}: {sorted(keys)}")

    def lakebase():
        p = w.postgres.get_project(name=f"projects/{env.lakebase_project}")
        return "ok", f"project {p.name}"

    def vs():
        eps = [e.name for e in w.vector_search_endpoints.list_endpoints()]
        return ("ok", f"endpoint {env.vs_endpoint} exists") if env.vs_endpoint in eps else ("warn", f"endpoint {env.vs_endpoint} will be created by bootstrap (slow: ~10 min)")

    def apps():
        list(w.apps.list())
        return "ok", "Databricks Apps API reachable"

    def models():
        """Every tier is a Unity AI Gateway model service created by `bundle deploy`; check what it is built from now."""
        from eqds_llm import load_model_config

        cfg = load_model_config(os.path.join(root, env.get("models_config", f"config/models.{env.target}.yaml")), env.prefix, env.suffix, env.catalog, env.schema)
        bad = []
        for tier, t in cfg.tiers.items():
            for model in (t.model, *t.gateway_fallback):
                try:
                    w.registered_models.get(model)
                except Exception:  # noqa: BLE001
                    bad.append(f"{tier}:{model}")
        if bad:
            return "fail", f"foundation models not found in Unity Catalog: {sorted(set(bad))}"
        missing = []
        for tier, t in cfg.tiers.items():
            try:
                w.api_client.do("GET", f"/api/2.1/unity-catalog/model-services/{t.primary}")
            except Exception:  # noqa: BLE001
                missing.append(t.primary)
        if missing:
            return "warn", f"model services will be created by `bundle deploy`: {missing}"
        return "ok", f"model services present for {len(cfg.tiers)} tiers"

    def serverless():
        sw = [x for x in w.warehouses.list() if x.enable_serverless_compute]
        return ("ok", f"serverless SQL warehouse available ({sw[0].name})") if sw else ("warn", "no serverless warehouse yet (bundle deploy creates one)")

    def classic():
        if env.get("bridge_compute", "classic") != "classic":
            return "ok", f"bridge_compute={env.get('bridge_compute')} (classic not needed)"
        nt = {n.node_type_id for n in w.clusters.list_node_types().node_types}
        want = env["node_types"]["bridge"]
        return ("ok", f"node type {want} available") if want in nt else ("fail", f"node type {want} not available in this workspace")

    def mcp():
        if not env.get("flags.use_managed_mcp", False):
            return "ok", "flags.use_managed_mcp=false (SDK adapters used)"
        # managed MCP is JSON-RPC over POST; a GET is rejected even when the server is enabled
        cat, sch = env.catalog, env.schema
        for path in (f"/api/2.0/mcp/functions/{cat}/{sch}", f"/api/2.0/mcp/vector-search/{cat}/{sch}"):
            r = w.api_client.do("POST", path, body={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
            if "error" in r:
                return "fail", f"managed MCP {path}: {r['error'].get('message', '')[:160]}"
        return "ok", "managed MCP reachable (UC functions, Vector Search)"

    def azure():
        if env.external_db_mode == "none":
            return "ok", "no external DB"
        try:
            w.connections.get(env.connection)
            return "ok", f"UC connection {env.connection} exists"
        except Exception:  # noqa: BLE001
            return "warn", f"UC connection {env.connection} will be created by bootstrap (needs secrets)"

    for n, f in [("auth", auth), ("catalog", catalog), ("secret scope", secrets), ("lakebase project", lakebase), ("vector search endpoint", vs), ("apps", apps),
                 ("model services (AI Gateway)", models), ("serverless warehouse", serverless), ("classic compute", classic), ("managed MCP", mcp), ("external DB connection", azure)]:
        t0 = time.time()
        add(n, f)
        out[-1].detail += f" ({time.time() - t0:.1f}s)"
    return out


def report(checks: list[Check]) -> int:
    icon = {"ok": "PASS", "warn": "WARN", "fail": "FAIL"}
    for c in checks:
        print(f"[{icon[c.status]}] {c.name}: {c.detail}")
    return 1 if any(c.status == "fail" for c in checks) else 0
