#!/usr/bin/env python3
"""Render `config/env/<target>.yaml` (+ bootstrap outputs) into DAB variable overrides (design §4.1 E2, E8).

Writes `.databricks/bundle/<target>/variable-overrides.json`, which `databricks bundle` reads automatically.
The environment profile stays the single source of truth; databricks.yml only declares the variables.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "eqds_agentkit"))
sys.path.insert(0, str(ROOT / "packages" / "eqds_llm"))

from eqds_agentkit.envcfg import Env  # noqa: E402

JOB_DEPS = ["pandas", "numpy", "pyyaml", "pydantic>=2.6", "psycopg[binary,pool]>=3.1", "httpx", "reportlab", "databricks-sdk>=0.50"]


def compute_vars(env: Env) -> dict:
    mode = env.get("bridge_compute", "classic")
    if mode == "serverless":
        return {
            "bridge_compute": "serverless", "azure_cluster_key": "", "azure_env_key": "azure_env",
            "azure_job_clusters": [], "azure_libraries": [],
            "azure_environments": [{"environment_key": "azure_env",
                                    "spec": {"environment_version": str(env.get("serverless_env_version", "3")), "dependencies": JOB_DEPS}}],
        }
    cluster = {
        "spark_version": env["spark_version"], "node_type_id": env["node_types"]["bridge"],
        "num_workers": 0, "data_security_mode": "SINGLE_USER",
        "spark_conf": {"spark.databricks.cluster.profile": "singleNode", "spark.master": "local[*, 4]"},
        "custom_tags": {"ResourceClass": "SingleNode", "project": env.prefix},
    }
    pol = (env.get("policies") or {}).get("bridge")
    if pol:
        cluster["policy_id"] = pol
        cluster["apply_policy_default_values"] = True
    return {
        "bridge_compute": "classic", "azure_cluster_key": "azure_cluster", "azure_env_key": "",
        "azure_job_clusters": [{"job_cluster_key": "azure_cluster", "new_cluster": cluster}],
        "azure_environments": [],
        "azure_libraries": [{"pypi": {"package": p}} for p in JOB_DEPS],
    }


def llm_vars(env: Env) -> dict:
    """Unity AI Gateway model services, one per tier (design §8.0): routing from config/models.<target>.yaml, rate limits
    from the profile, inference table `<service>_payload` in the environment schema."""
    import yaml

    from eqds_llm.config import TIERS, service_name

    models = yaml.safe_load((ROOT / env.get("models_config", f"config/models.{env.target}.yaml")).read_text())
    rl = (env.get("model_serving") or {}).get("rate_limits") or {}
    limits = [{"key": "RATE_LIMIT_KEY_SERVICE", "renewal_period": "RATE_LIMIT_RENEWAL_PERIOD_MINUTE", "requests": int(rl.get("endpoint_per_min", 600))},
              {"key": "RATE_LIMIT_KEY_USER_DEFAULT", "renewal_period": "RATE_LIMIT_RENEWAL_PERIOD_MINUTE", "requests": int(rl.get("user_per_min", 60))}]

    def dest(name: str, model: str, pct: int | None = None) -> dict:
        d = {"name": name, "destination_type": "DESTINATION_TYPE_PAY_PER_TOKEN_FOUNDATION_MODEL", "pay_per_token_config": {"model": f"models/{model}"}}
        if pct is not None:
            d["traffic_percentage"] = pct
        return d

    out: dict = {}
    for tier in TIERS:
        t = models["tiers"][tier]
        sid = service_name(env.prefix, env.suffix, tier)
        routing = {"destinations": [dest("primary", t["model"], 100)]}
        if t.get("gateway_fallback"):
            routing["fallback"] = {"destinations": [dest(f"fallback_{i + 1}", m) for i, m in enumerate(t["gateway_fallback"])]}
        out[f"llm_{tier}_id"] = sid
        out[f"llm_{tier}_config"] = {"routing": routing, "rate_limits": limits,
                                     "inference_table": {"parent": f"schemas/{env.catalog}.{env.schema}", "table_name_prefix": f"inference_{sid}"}}
    return out


def render(env: Env) -> dict:
    gen_path = ROOT / ".databricks" / "bundle" / env.target / "bootstrap-output.json"
    gen = json.loads(gen_path.read_text()) if gen_path.exists() else {}
    ms = env.get("model_serving", {}) or {}
    sched = env.get("schedules", {}) or {}
    v = {
        "target_name": env.target, "name_prefix": env.prefix, "catalog": env.catalog, "schema": env.schema, "suffix": env.suffix,
        "workspace_host": env.host, "lakebase_project": env.lakebase_project, "lakebase_branch": env.get("lakebase.branch", "production"),
        "lakebase_database": env.lakebase_database, "vector_search_endpoint": env.vs_endpoint,
        "genie_space_id": env.get("genie_space_id") or gen.get("genie_space_id") or "unset",  # app env values must be non-empty
        "experiment_path": env.experiment_path, "foreign_catalog": env.foreign_catalog, "connection": env.connection,
        "secret_scope": env.secret_scope, "data_profile": env.data_profile, "external_db_mode": env.external_db_mode,
        "live_tools_mode": env.get("live_tools_mode", "bridge"),
        "wiki_refresh_cron": sched.get("wiki_refresh_cron", "0 30 7 * * ?"), "wiki_refresh_tz": sched.get("wiki_refresh_tz", "Asia/Singapore"),
        "ingest_news_cron": sched.get("ingest_news_cron", "0 */15 * * * ?"),
        "serverless_env_version": str(env.get("serverless_env_version", "3")),
        "embed_served_entities": [ms.get("embed_served_entity") or {
            "name": "embed", "entity_name": ms.get("embed_entity", "system.ai.gte_large_en_v1_5"),
            "entity_version": str(ms.get("embed_version", "1")), "min_provisioned_throughput": int(ms.get("pt_min", 0)),
            "max_provisioned_throughput": int(ms.get("pt_max", 19000)), "scale_to_zero_enabled": bool(ms.get("scale_to_zero", True))}],
        "github_repo_url": env.get("github_repo_url", ""),
        "schedule_status": "UNPAUSED" if sched.get("enabled", False) else "PAUSED",
        "bridge_worker_status": "UNPAUSED" if (env.get("bridge_worker") or {}).get("enabled", True) else "PAUSED",
        "app_version": os.environ.get("EQDS_APP_VERSION", "dev"),
        "ui_permissions": [{"level": "CAN_USE", "group_name": g} for g in (env.get("ui.access_groups") or [])],
        "sp_ci": (env.get("service_principals") or {}).get("ci", ""),
    }
    reg = (env.get("service_principals") or {}).get("registrar", "")
    # registration runs as the registrar service principal (make registrar); otherwise as the deployer (databricks.yml default).
    # `registrar_job_identity: deployer` keeps the job on the deployer while the registrar principal exists for app permissions: needed
    # until a workspace admin grants the deployer 'Service Principal User' on it (no API for that grant in every workspace).
    if reg and env.get("registrar_job_identity", "registrar") != "deployer":
        v["registrar_run_as"] = {"service_principal_name": reg}
    v.update(compute_vars(env))
    v.update(llm_vars(env))
    return v


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default=os.environ.get("TARGET", "demo"))
    a = ap.parse_args()
    env = Env.from_target(a.target)
    out = ROOT / ".databricks" / "bundle" / env.target / "variable-overrides.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(render(env), indent=2, sort_keys=True))
    print(f"wrote {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
