"""Execution context for bootstrap steps: works inside a job (Spark) and from a laptop / CI (SQL warehouse API)."""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Ctx:
    env: Any
    w: Any
    root: str
    spark: Any = None
    outputs: dict = field(default_factory=dict)
    _wh: str | None = None

    # ── SQL ────────────────────────────────────────────────────────────────────────────────────
    def warehouse_id(self) -> str:
        if self._wh:
            return self._wh
        name = self.env.app_name("wh")  # `${prefix}-wh${suffix}`
        for wh in self.w.warehouses.list():
            if wh.name == name:
                self._wh = wh.id
                return wh.id
        raise RuntimeError(f"SQL warehouse {name} not found (run `bundle deploy` first)")

    def sql(self, stmt: str) -> list[dict]:
        if self.spark is not None:
            return [r.asDict() for r in self.spark.sql(stmt).collect()]
        from databricks.sdk.service.sql import StatementState

        r = self.w.statement_execution.execute_statement(statement=stmt, warehouse_id=self.warehouse_id(), wait_timeout="50s")
        while r.status.state in (StatementState.PENDING, StatementState.RUNNING):
            time.sleep(2)
            r = self.w.statement_execution.get_statement(r.statement_id)
        if r.status.state != StatementState.SUCCEEDED:
            raise RuntimeError(f"SQL failed: {r.status.error.message if r.status.error else r.status.state}\n{stmt[:300]}")
        cols = [c.name for c in (r.manifest.schema.columns if r.manifest and r.manifest.schema else [])]
        return [dict(zip(cols, row)) for row in (r.result.data_array if r.result and r.result.data_array else [])]

    # ── helpers ────────────────────────────────────────────────────────────────────────────────
    def say(self, msg: str) -> None:
        print(f"  {msg}", flush=True)

    def app_url(self, component: str) -> str | None:
        try:
            return self.w.apps.get(self.env.app_name(component)).url
        except Exception:  # noqa: BLE001
            return None

    def render(self, text: str, extra: dict | None = None) -> str:
        """Substitute ${name} placeholders from the environment profile (string.Template; unknown names are left intact)."""
        from string import Template

        e = self.env
        a = e.raw.get("azure_sql", {}) or {}
        sps = e.raw.get("service_principals", {}) or {}
        m = {"catalog": e.catalog, "schema": e.schema, "uc": e.uc, "connection": e.connection, "foreign_catalog": e.foreign_catalog,
             "azure_host": a.get("host", ""), "azure_port": a.get("port", 1433), "azure_db": a.get("database", ""),
             "secret_scope": e.secret_scope, "name_prefix": e.prefix, "suffix": e.suffix, "vector_search_endpoint": e.vs_endpoint,
             "sp_ci": sps.get("ci", ""), "sp_bridge": sps.get("bridge", ""), "sp_ingest": sps.get("ingest", ""),
             "sp_wiki": sps.get("wiki", ""), "sp_seed": sps.get("seed", ""), "target": e.target}
        m.update(extra or {})
        return Template(text).safe_substitute(m)


def make_ctx(target: str, root: str | None = None, spark: Any = None, profile: str | None = None) -> Ctx:
    from databricks.sdk import WorkspaceClient

    from eqds_agentkit.envcfg import Env, repo_root

    root = str(repo_root(root))
    env = Env.from_target(target, root)
    prof = profile or os.environ.get("DATABRICKS_CONFIG_PROFILE")
    w = WorkspaceClient(profile=prof) if prof and spark is None else WorkspaceClient()
    return Ctx(env=env, w=w, root=root, spark=spark)
