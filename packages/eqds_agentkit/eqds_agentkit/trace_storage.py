"""Traces in Unity Catalog (profile flag `flags.use_uc_trace_storage`, schema `traces_schema` of the environment's catalog).

An MLflow experiment is bound ONCE to a UC trace location (four Delta tables `<prefix>_otel_{spans,logs,metrics,annotations}`);
after that every trace written for the experiment lands there instead of in MLflow's own storage. The binding is permanent and does
not migrate older traces, so it is only switched on for environments where that is intended (dev). Production keeps the flag off.

Used by the platform bootstrap step `trace_storage` (all experiments of the platform and of every agent already deployed) and by each
agent repo's `make traces` (its own experiment, right after its deploy). Both call the same functions, so the rules live here:

  * the schema must exist first (`make infra` creates it); MLflow does not create it
  * the table prefix is derived from the experiment's leaf name, so table names are predictable (needed for grants)
  * the app that writes traces needs MODIFY and SELECT on the four tables, granted explicitly (ALL PRIVILEGES is not enough);
    readers get SELECT. Grants are plain SQL, so they do not depend on the tables existing when the app is deployed.
"""
from __future__ import annotations

import os
import posixpath
import re
import time
from typing import Any

TABLE_KINDS = ("spans", "logs", "metrics", "annotations")


def table_prefix(experiment_path: str) -> str:
    """`/Shared/eqds-dev/eqds-moderator` -> `eqds_moderator` (UC identifiers: letters, digits, underscore)."""
    leaf = posixpath.basename(experiment_path.rstrip("/"))
    return re.sub(r"[^a-z0-9]+", "_", leaf.lower()).strip("_")


def table_names(catalog: str, schema: str, prefix: str) -> list[str]:
    return [f"{catalog}.{schema}.{prefix}_otel_{k}" for k in TABLE_KINDS]


def bind(experiment_path: str, catalog: str, schema: str, warehouse_id: str) -> str:
    """Bind the experiment (it must exist) to `catalog.schema` with a predictable table prefix. Returns `bound`, `ok` (already bound to
    exactly this location), `missing` (no such experiment) or `conflict:<where it is bound>` (bound elsewhere; never rebound)."""
    import mlflow
    from mlflow.entities.trace_location import UnityCatalog

    mlflow.set_tracking_uri("databricks")
    os.environ["MLFLOW_TRACING_SQL_WAREHOUSE_ID"] = warehouse_id
    exp = mlflow.get_experiment_by_name(experiment_path)
    if exp is None:
        return "missing"
    prefix = table_prefix(experiment_path)
    cur: Any = getattr(exp, "trace_location", None)
    if cur is not None:
        want = (catalog, schema, prefix)
        have = (getattr(cur, "catalog_name", None), getattr(cur, "schema_name", None), getattr(cur, "table_prefix", None))
        if have[:2] == want[:2] and (have[2] in (None, prefix) or str(have[2]).endswith(prefix)):
            return "ok"
        return f"conflict:{'.'.join(str(x) for x in have if x)}"
    mlflow.set_experiment(experiment_name=experiment_path, trace_location=UnityCatalog(catalog_name=catalog, schema_name=schema, table_prefix=prefix))
    return "bound"


def run_sql(w, warehouse_id: str, stmt: str) -> None:
    from databricks.sdk.service.sql import StatementState

    r = w.statement_execution.execute_statement(statement=stmt, warehouse_id=warehouse_id, wait_timeout="50s")
    while r.status.state in (StatementState.PENDING, StatementState.RUNNING):
        time.sleep(2)
        r = w.statement_execution.get_statement(r.statement_id)
    if r.status.state != StatementState.SUCCEEDED:
        raise RuntimeError(f"SQL failed: {r.status.error.message if r.status.error else r.status.state}: {stmt[:200]}")


def grant_writer(w, warehouse_id: str, catalog: str, schema: str, experiment_path: str, principal: str) -> list[str]:
    """The identity that WRITES the traces of `experiment_path` (an app's service principal): MODIFY + SELECT on its four tables."""
    stmts = [f"GRANT USE CATALOG ON CATALOG {catalog} TO `{principal}`", f"GRANT USE SCHEMA ON SCHEMA {catalog}.{schema} TO `{principal}`"]
    stmts += [f"GRANT MODIFY, SELECT ON TABLE {t} TO `{principal}`" for t in table_names(catalog, schema, table_prefix(experiment_path))]
    for s in stmts:
        run_sql(w, warehouse_id, s)
    return stmts


def grant_reader(w, warehouse_id: str, catalog: str, schema: str, principal: str) -> list[str]:
    """A reader of every trace in the schema, present and future (the UI's run trace view): SELECT on the schema."""
    stmts = [f"GRANT USE CATALOG ON CATALOG {catalog} TO `{principal}`", f"GRANT USE SCHEMA ON SCHEMA {catalog}.{schema} TO `{principal}`",
             f"GRANT SELECT ON SCHEMA {catalog}.{schema} TO `{principal}`"]
    for s in stmts:
        run_sql(w, warehouse_id, s)
    return stmts


def search_location(catalog: str, schema: str, experiment_path: str) -> str:
    """The `locations` value for `mlflow.search_traces` over the traces of one experiment: `<catalog>.<schema>.<table prefix>`.
    Search each location on its own (a search over several prefixes returns only the first one's traces)."""
    return f"{catalog}.{schema}.{table_prefix(experiment_path)}"
