#!/usr/bin/env python3
"""Store this agent's traces in Unity Catalog, for environments whose platform profile asks for it (`flags.use_uc_trace_storage`, dev).

    python scripts/bind_traces.py --platform ../eqds-agents --target dev

Binds the agent's experiment (created by `databricks bundle deploy`) to <catalog>.<traces_schema> and grants the app's service principal
MODIFY + SELECT on the four trace tables. Idempotent; run after the bundle deploy and before the app starts (`make deploy` does).
Production (`demo`) keeps traces in MLflow's own storage: the script then does nothing. The binding of an experiment is permanent.
Needs: the runtime snapshot (./runtime: profile + the shared trace helpers, same one scripts/build.py uses), mlflow>=3.14,
databricks-sdk, and an identity that may create tables in the traces schema (the CI service principal).
"""
import argparse
import pathlib
import sys

import yaml

HERE = pathlib.Path(__file__).resolve().parents[1]
ap = argparse.ArgumentParser()
ap.add_argument("--platform", default=str(HERE / "runtime"))
ap.add_argument("--target", default="demo")
a = ap.parse_args()
P = pathlib.Path(a.platform).resolve()
sys.path.insert(0, str(P / "packages/eqds_agentkit"))

profile = yaml.safe_load((P / f"config/env/{a.target}.yaml").read_text())
if not (profile.get("flags") or {}).get("use_uc_trace_storage"):
    print(f"target {a.target}: traces stay in MLflow storage (flags.use_uc_trace_storage is off), nothing to bind")
    sys.exit(0)

from databricks.sdk import WorkspaceClient  # noqa: E402

from eqds_agentkit import trace_storage as ts  # noqa: E402

bundle = yaml.safe_load((HERE / "databricks.yml").read_text())
over = ((bundle.get("targets") or {}).get(a.target) or {}).get("variables") or {}


def var(name: str) -> str:
    return over.get(name) or bundle["variables"][name]["default"]


path, app_name, wh = var("experiment_path"), var("app_name"), var("warehouse_id")
catalog, schema = profile["catalog"], profile.get("traces_schema", "traces")
w = WorkspaceClient()
res = ts.bind(path, catalog, schema, wh)
print(f"traces of {path}: {res} ({catalog}.{schema}.{ts.table_prefix(path)}_otel_*)")
if res == "missing" or res.startswith("conflict"):
    sys.exit(f"cannot continue: {res} (deploy the bundle first; a conflicting binding is permanent)")
sp = w.apps.get(app_name).service_principal_client_id
ts.grant_writer(w, wh, catalog, schema, path, sp)
print(f"MODIFY + SELECT on the trace tables granted to {app_name} ({sp})")
