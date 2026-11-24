"""Serverless is the only bridge compute: the classic option is gone from profiles, bundle variables, jobs and the manifest."""
from __future__ import annotations

import importlib.util
import os
import re

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGETS = ("dev", "demo", "staging", "prod")
CLASSIC_KEYS = ("azure_cluster", "azure_job_clusters", "azure_libraries", "job_cluster_key", "job_clusters", "node_type_id")


def _env(target):
    from eqds_agentkit.envcfg import Env

    return Env.from_target(target, ROOT)


def _bundle_vars():
    spec = importlib.util.spec_from_file_location("bundle_vars", os.path.join(ROOT, "scripts", "bundle_vars.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_every_profile_is_serverless():
    for t in TARGETS:
        env = _env(t)
        assert env.get("bridge_compute") == "serverless", t
        assert (env.get("network.serverless_blocked") is not None), t
        assert "bridge" not in (env.get("node_types") or {}), f"{t}: node_types.bridge is classic-only"


def test_compute_vars_render_only_the_serverless_environment():
    bv = _bundle_vars()
    for t in TARGETS:
        out = bv.compute_vars(_env(t))
        assert set(out) == {"serverless_env_key", "serverless_environments"}, t
        (env_spec,) = out["serverless_environments"]
        assert env_spec["environment_key"] == out["serverless_env_key"] == "external", t
        assert "environment_version" in env_spec["spec"] and env_spec["spec"]["dependencies"], t


def test_bundle_vars_have_no_classic_cluster_fields():
    bv = _bundle_vars()
    for t in TARGETS:
        rendered = bv.render(_env(t))
        assert not [k for k in rendered if k.startswith("azure_") or k == "bridge_compute"], (t, rendered.keys())
        text = repr(rendered)
        for key in CLASSIC_KEYS:
            assert key not in text, (t, key)


def test_databricks_yml_and_jobs_have_no_classic_wiring():
    with open(os.path.join(ROOT, "databricks.yml")) as f:
        bundle = f.read()
    assert "bridge_compute" not in bundle and "azure_job_clusters" not in bundle
    job_files = [os.path.join(ROOT, "resources", "jobs", n) for n in ("platform.yml", "seed.yml")]
    for path in job_files:
        with open(path) as f:
            text = f.read()
        for pattern in (r"job_clusters:", r"job_cluster_key:", r"libraries: \$\{var\.azure", r"\$\{var\.azure_"):
            assert not re.search(pattern, text), (os.path.basename(path), pattern)


def test_manifest_bridge_jobs_run_on_serverless():
    with open(os.path.join(ROOT, "bootstrap", "manifest.yaml")) as f:
        jobs = yaml.safe_load(f)["jobs"]
    for name in ("seed_azure", "validate_seed", "news_generator", "ingest_news", "bridge_worker"):
        assert jobs[name]["compute"] == "serverless", name
    assert not [n for n, j in jobs.items() if j["compute"] == "bridge_compute"]
