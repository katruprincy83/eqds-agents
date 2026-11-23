"""Dev and production share one workspace: nothing that names a resource may collide, and nothing dev does may touch production."""
from __future__ import annotations

import importlib.util
import os
import posixpath
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(ROOT, "packages", "eqds_agentkit"), os.path.join(ROOT, "packages", "eqds_llm"), os.path.join(ROOT, "src")]

from databricks.sdk.errors import NotFound  # noqa: E402

from eqds_agentkit import trace_storage as ts  # noqa: E402
from eqds_agentkit.envcfg import Env  # noqa: E402

dev, demo = Env.from_target("dev", ROOT), Env.from_target("demo", ROOT)


def test_dev_and_production_share_no_resource_name():
    assert dev.host == demo.host                                   # one workspace
    assert dev.suffix == "-dev" and demo.suffix == ""
    assert dev.catalog == "eqds_dev" and dev.catalog != demo.catalog
    assert dev.get("lakebase.branch") == "dev" and demo.get("lakebase.branch") == "production"
    assert dev.lakebase_project == demo.lakebase_project            # a branch of the same project, not a second project
    assert dev.app_name("moderator") != demo.app_name("moderator")
    assert dev.job_name("seed-uc") != demo.job_name("seed-uc")
    assert dev.app_name("sp-news") == "eqds-sp-news-dev"


def test_dev_experiments_live_in_their_own_folder():
    paths = [dev.experiment_path, dev.moderator_experiment_path, dev.agent_experiment_path("news")]
    assert {posixpath.dirname(p) for p in paths} == {"/Shared/eqds-dev"}
    assert not any(p.startswith(posixpath.dirname(demo.experiment_path) + "/") for p in paths)
    assert len({*paths, demo.experiment_path, demo.moderator_experiment_path}) == 5


def test_traces_go_to_uc_in_dev_only():
    assert dev.use_uc_trace_storage and not demo.use_uc_trace_storage
    assert dev.traces_schema == "traces" and dev.traces_schema != dev.schema


def test_dev_borrows_the_external_database():
    assert dev.shared_external and not demo.shared_external
    env = {**os.environ, "PY": sys.executable}
    out = subprocess.run(["make", "-n", "seed", "TARGET=dev", f"PY={sys.executable}"], cwd=ROOT, capture_output=True, text=True, env=env).stdout
    assert "seed_azure" not in out and "seed_uc" in out
    out = subprocess.run(["make", "-n", "seed", "TARGET=demo", f"PY={sys.executable}"], cwd=ROOT, capture_output=True, text=True, env=env).stdout
    assert "seed_azure" in out


def test_shared_external_teardown_keeps_the_foreign_catalog():
    from bootstrap import steps

    said, sql = [], []
    ctx = SimpleNamespace(env=dev, say=said.append, sql=sql.append)
    steps.federation_teardown(ctx)
    assert not sql and said
    ctx = SimpleNamespace(env=demo, say=said.append, sql=sql.append)
    steps.federation_teardown(ctx)
    assert any("DROP CATALOG" in s for s in sql)


def test_trace_table_names_are_predictable():
    assert ts.table_prefix("/Shared/eqds-dev/eqds-moderator") == "eqds_moderator"
    assert ts.table_prefix("/Shared/eqds-dev/eqds") == "eqds"
    assert ts.table_names("eqds_dev", "traces", "eqds_news") == [f"eqds_dev.traces.eqds_news_otel_{k}" for k in ("spans", "logs", "metrics", "annotations")]


def test_trace_grants_are_explicit_modify_and_select(monkeypatch):
    sql: list[str] = []
    monkeypatch.setattr(ts, "run_sql", lambda w, wh, stmt: sql.append(stmt))
    ts.grant_writer(None, "wh", "eqds_dev", "traces", "/Shared/eqds-dev/eqds-news", "sp-1")
    assert "GRANT MODIFY, SELECT ON TABLE eqds_dev.traces.eqds_news_otel_spans TO `sp-1`" in sql
    assert sum("MODIFY" in s for s in sql) == 4 and not any("ALL PRIVILEGES" in s for s in sql)
    sql.clear()
    ts.grant_reader(None, "wh", "eqds_dev", "traces", "ui")
    assert "GRANT SELECT ON SCHEMA eqds_dev.traces TO `ui`" in sql and not any("MODIFY" in s for s in sql)


def _infra():
    spec = importlib.util.spec_from_file_location("eqds_infra", os.path.join(ROOT, "scripts", "infra.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class FakePostgres:
    def __init__(self, branches=()):
        self.branches, self.created, self.endpoints = set(branches), [], {}

    def get_branch(self, name):
        if name not in self.branches:
            raise NotFound("no branch")

    def create_branch(self, parent, branch, branch_id):
        self.branches.add(f"{parent}/branches/{branch_id}")
        self.created.append((branch_id, branch.spec.source_branch))
        return SimpleNamespace(wait=lambda: None)

    def list_endpoints(self, parent):
        return self.endpoints.get(parent, [])

    def create_endpoint(self, parent, endpoint, endpoint_id):
        self.endpoints.setdefault(parent, []).append(SimpleNamespace(status=SimpleNamespace(endpoint_type="ENDPOINT_TYPE_READ_WRITE")))
        return SimpleNamespace(wait=lambda: None)


def test_dev_lakebase_branch_is_created_from_production_and_reset_once(monkeypatch):
    infra = _infra()
    resets = []
    monkeypatch.setattr(infra, "reset_lakebase_data", lambda w, env: resets.append(env.get("lakebase.branch")))
    pg = FakePostgres(branches={"projects/eqds-lakebase/branches/production"})
    w = SimpleNamespace(postgres=pg)
    infra.ensure_lakebase_branch(w, dev)
    assert pg.created == [("dev", "projects/eqds-lakebase/branches/production")] and resets == ["dev"]
    assert pg.endpoints["projects/eqds-lakebase/branches/dev"]
    infra.ensure_lakebase_branch(w, dev)           # second run: nothing created, data kept
    assert len(pg.created) == 1 and resets == ["dev"]


def test_production_lakebase_branch_is_never_touched(monkeypatch):
    infra = _infra()
    pg = FakePostgres()
    monkeypatch.setattr(infra, "reset_lakebase_data", lambda w, env: pytest.fail("production data must never be reset"))
    infra.ensure_lakebase_branch(SimpleNamespace(postgres=pg), demo)
    assert not pg.created


def test_traces_schema_only_created_when_the_flag_is_on():
    infra = _infra()
    made = []
    w = SimpleNamespace(schemas=SimpleNamespace(get=lambda n: (_ for _ in ()).throw(NotFound("x")), create=lambda **k: made.append(k)))
    infra.ensure_schemas(w, demo)
    assert not made
    infra.ensure_schemas(w, dev)
    assert made and made[0]["catalog_name"] == "eqds_dev" and made[0]["name"] == "traces"


# ── agent repos: one bundle, a target per environment ────────────────────────────────────────────
def _vals(env, key):
    return {"HOST": "https://ws", "WAREHOUSE_ID": f"wh-{env.target}", "EXPERIMENT_PATH": env.agent_experiment_path(key),
            "REGISTRAR_SP": f"reg-{env.target}", "UI_SP": f"ui-{env.target}", "MODERATOR_SP": f"mod-{env.target}"}


def test_agent_bundle_gets_a_dev_target_that_overrides_every_platform_value():
    import yaml

    import new_agent

    block = new_agent.render_targets("news", demo, _vals(demo, "news"), [(dev, _vals(dev, "news"))])
    doc = yaml.safe_load(block)
    assert doc["targets"]["demo"]["default"] is True and "variables" not in doc["targets"]["demo"]   # production uses the defaults
    v = doc["targets"]["dev"]["variables"]
    assert v == {"platform_target": "dev", "app_name": "eqds-sp-news-dev", "warehouse_id": "wh-dev", "experiment_path": "/Shared/eqds-dev/eqds-news",
                 "registrar_sp": "reg-dev", "ui_sp": "ui-dev", "moderator_sp": "mod-dev"}
    template = yaml.safe_load(new_agent.fill((new_agent.TPL / "databricks.yml").read_text(), {
        "KEY": "news", "DISPLAY": "News", "TARGET": "demo", "APP_NAME": "eqds-sp-news", "VERSION": "1", "TARGETS": block, **_vals(demo, "news")}))
    assert set(v) <= set(template["variables"])      # every override names a declared variable


def test_makefile_of_an_existing_agent_is_patched_in_place():
    import new_agent

    old = ".PHONY: build deploy smoke eval\n\nbuild:\n\tx\n\ndeploy: build\n\tdatabricks bundle deploy -t $(TARGET)\n\tdatabricks bundle run -t $(TARGET) news\n\n" \
          "smoke:\n\t$(PY) scripts/smoke.py $(if $(Q),--invoke \"$(Q)\")\n\neval: build\n\tmine\n"
    new = new_agent.patch_makefile(old, "news")
    assert "bind_traces.py" in new and "\ntraces:" in new and "smoke.py --target $(TARGET)" in new and "eval: build\n\tmine" in new
    assert new_agent.patch_makefile(new, "news") == new                      # idempotent
