"""Tracing layout: one experiment per agent and one for the moderator, a root span per specialist invocation, trace ids that
point both ways. Runs against a throwaway local MLflow store, so it needs no workspace."""
import asyncio
import os
import pathlib
import sys

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


# ── experiment layout ─────────────────────────────────────────────────────────────────────────────
def test_experiment_paths_are_siblings_of_the_platform_experiment():
    from eqds_agentkit.envcfg import Env

    env = Env.from_target("demo", str(ROOT))
    base = env.experiment_path
    assert env.moderator_experiment_path == base + "-moderator"
    assert env.agent_experiment_path("news") == base + "-news"
    # same parent folder, so the folder the platform deploy creates is the only prerequisite (an experiment cannot hold another)
    assert os.path.dirname(env.agent_experiment_path("news")) == os.path.dirname(base) == os.path.dirname(env.moderator_experiment_path)


def _app(fname, key):
    return yaml.safe_load((ROOT / "resources/apps" / fname).read_text())["resources"]["apps"][key]


def _res(app, name):
    return next(r for r in app["resources"] if r["name"] == name)


def test_each_platform_app_writes_to_the_right_experiment():
    mod, ui, bridge = _app("moderator.yml", "moderator"), _app("ui.yml", "ui"), _app("bridge_mcp.yml", "bridge_mcp")
    assert _res(mod, "experiment")["experiment"]["experiment_id"] == "${resources.experiments.exp_moderator.id}"
    assert _res(mod, "experiment")["experiment"]["permission"] == "CAN_MANAGE"
    assert _res(ui, "experiment")["experiment"]["experiment_id"] == "${resources.experiments.exp_main.id}"
    assert _res(bridge, "experiment")["experiment"]["experiment_id"] == "${resources.experiments.exp_main.id}"
    # the UI also reads the moderator's traces (run trace view), read only
    assert _res(ui, "experiment-moderator")["experiment"] == {"experiment_id": "${resources.experiments.exp_moderator.id}", "permission": "CAN_READ"}
    assert {"name": "MODERATOR_EXPERIMENT_ID", "value_from": "experiment-moderator"} in ui["config"]["env"]
    exps = yaml.safe_load((ROOT / "resources/experiments/experiment.yml").read_text())["resources"]["experiments"]
    assert exps["exp_moderator"]["name"] == "${var.experiment_path}-moderator"


def test_agent_template_creates_its_own_experiment_and_binds_the_app_to_it():
    import new_agent

    vals = {k: "x" for k in ("KEY", "DISPLAY", "TARGET", "APP_NAME", "WAREHOUSE_ID", "REGISTRAR_SP", "UI_SP", "MODERATOR_SP", "VERSION", "HOST")}
    vals["EXPERIMENT_PATH"] = "/Shared/eqds/demo-news"
    doc = yaml.safe_load(new_agent.fill((new_agent.TPL / "databricks.yml").read_text(), vals))  # fill() fails on any unfilled placeholder
    exp = doc["resources"]["experiments"]["traces"]
    assert exp["name"] == "${var.experiment_path}" and doc["variables"]["experiment_path"]["default"] == "/Shared/eqds/demo-news"
    assert "experiment_id" not in doc["variables"]  # no workspace-specific id baked into the bundle any more
    assert {"level": "CAN_READ", "service_principal_name": "${var.ui_sp}"} in exp["permissions"]
    app = doc["resources"]["apps"]["x"]
    assert _res(app, "experiment")["experiment"]["experiment_id"] == "${resources.experiments.traces.id}"
    assert {"name": "MLFLOW_EXPERIMENT_ID", "value_from": "experiment"} in app["config"]["env"]


# ── one trace per invocation, linked both ways ────────────────────────────────────────────────────
@pytest.fixture
def mlflow_store(tmp_path):
    import mlflow

    mlflow.set_tracking_uri(f"sqlite:///{tmp_path}/mlflow.db")
    exp = mlflow.set_experiment("agent-news-test")
    yield mlflow, exp.experiment_id
    mlflow.set_tracking_uri("file:///tmp/_unused_mlruns")


def _agent(monkeypatch, out):
    from agents.specialist_base.agent import SpecialistAgent

    a = object.__new__(SpecialistAgent)
    a.key = "news"

    async def inner(self, body, user, ci, brief):
        # a child span, as the LLM client and the tool loop create them: it must nest under the root span
        import mlflow

        with mlflow.start_span(name="llm.fast", span_type="LLM"):
            pass
        return out

    monkeypatch.setattr(SpecialistAgent, "_invoke", inner)
    return a


def test_specialist_invocation_is_one_tagged_trace_with_nested_spans(monkeypatch, mlflow_store):
    from eqds_agentkit.contract import Claim, InvocationOutputs, ToolCall

    mlflow, eid = mlflow_store
    out = InvocationOutputs(specialist="news", text="Nvidia headlines.", claims=[Claim(id="c1", text="x", stance="support", confidence=0.7, citations=[])],
                            tool_calls=[ToolCall(tool="web_search", server="bridge-mcp", zone="external", latency_ms=5)])
    body = {"input": [{"role": "user", "content": "latest news for NVIDIA"}],
            "custom_inputs": {"run_id": "r-1", "round": 1, "conversation_id": "conv-9", "caller": "moderator"}}
    res = asyncio.run(_agent(monkeypatch, out).invoke(body, "alice@x.com", parent_trace="tr-moderator-1"))

    mlflow.flush_trace_async_logging()  # traces are exported in the background
    traces = mlflow.search_traces(locations=[eid], return_type="list")
    assert len(traces) == 1  # nothing landed as a loose single-span trace
    t = traces[0]
    names = {s.name: s for s in t.data.spans}
    assert set(names) == {"specialist.news", "llm.fast"}
    assert names["llm.fast"].parent_id == names["specialist.news"].span_id  # nested, not a separate trace
    tags = t.info.tags
    assert (tags["run_id"], tags["specialist"], tags["caller"], tags["tools"]) == ("r-1", "news", "moderator", "web_search")
    assert tags["moderator_trace_id"] == "tr-moderator-1"  # points back at the moderator's trace
    assert res.trace_id == t.info.trace_id  # and the moderator is told this trace's id
    md = t.info.trace_metadata
    assert md["mlflow.trace.session"] == "conv-9" and md["mlflow.trace.user"] == "alice@x.com"  # conversation grouping in the Traces UI


def test_cached_answer_still_leaves_a_trace_marked_cached(monkeypatch, mlflow_store):
    from eqds_agentkit.contract import InvocationOutputs

    mlflow, eid = mlflow_store
    out = InvocationOutputs(specialist="news", text="from cache", cached=True, trace_id="old-trace")
    body = {"input": [{"role": "user", "content": "q"}], "custom_inputs": {"run_id": "r-2"}}
    res = asyncio.run(_agent(monkeypatch, out).invoke(body, "anonymous"))
    mlflow.flush_trace_async_logging()
    t = mlflow.search_traces(locations=[eid], return_type="list")[0]
    assert t.info.tags["cached"] == "True" and "tools" not in t.info.tags
    assert res.trace_id == t.info.trace_id != "old-trace"  # the stale id from the cached copy is replaced


def test_tracing_helpers_are_no_ops_outside_a_trace():
    from eqds_agentkit import tracing

    assert tracing.current_trace_id() is None
    assert tracing.tag_trace(run_id="x") is None
    tracing.preview("a", "b")  # must not raise
