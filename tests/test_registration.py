"""Registration: spec validation, and a disabled specialist is neither listed nor callable."""
import os, sys, pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from eqds_agentkit.registry import Registry
from bootstrap import register as R

ROOT = os.path.join(os.path.dirname(__file__), "..")


class _Pool:
    def __init__(self, rows): self.rows = rows
    def connection(self):
        rows = self.rows
        class C:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def execute(s, q, p=None):
                class Cur:
                    def fetchall(c): return rows
                return Cur()
        return C()


def _row(key, enabled):
    return dict(key=key, display_name=key, tag_prefix=key[:1].upper(), description="d", domains=[], url="http://x", contract_version="1",
                timeout_s=60, enabled=enabled, version="1", owner="o", tools="")


def test_disabled_specialist_is_hidden_and_not_callable():
    rows = [_row("news", True), _row("ours", True)]
    reg = Registry(_Pool(rows), ttl_s=3600)
    assert {s.key for s in reg.all()} == {"news", "ours"}
    rows[0]["enabled"] = False  # turned off on the registration page
    assert {s.key for s in reg.all()} == {"news", "ours"}  # still cached
    reg.refresh()  # the UI triggers this right after the toggle
    assert {s.key for s in reg.all()} == {"ours"}
    assert reg.get("news") is None  # a stale brief or probe cannot reach it
    assert reg.get("news", include_disabled=True) is not None  # the registration page still lists it


def _template_spec(key="fx", tag="FX"):
    import yaml
    t = open(os.path.join(ROOT, "templates", "agent", "specialist.yaml")).read()
    for k, v in {"KEY": key, "DISPLAY": "Fx", "TAG": tag, "OWNER": "o"}.items():
        t = t.replace("{{" + k + "}}", v)
    return yaml.safe_load(t)


def test_new_agent_template_passes_registration_checks():
    from eqds_agentkit import specs
    specs.validate_spec(_template_spec(), "fx")


def test_bad_specs_are_rejected():
    from eqds_agentkit import specs
    bad = _template_spec("bad", "TOOLONG")
    with pytest.raises(specs.RegistrationError, match="tag_prefix"):
        specs.validate_spec(bad, "bad")
    with pytest.raises(specs.RegistrationError, match="missing"):
        specs.validate_spec({"key": "bad"}, "bad")
    with pytest.raises(specs.RegistrationError, match="does not match"):
        specs.validate_spec(_template_spec(), "other")


INFO = {"key": "px", "display_name": "Px", "tag_prefix": "PX", "description": "d", "domains": ["a"], "owner": "o", "version": "1.0.0", "timeout_s": 90,
        "contract_version": "1", "model_tier": "fast", "skill": {"name": "px", "version": "1.0.0"}, "tools": "UC fns", "tools_summary": "UC fns",
        "tool_config": [{"type": "uc_functions", "allow": ["price_move"]}], "access": {"tables": ["prices_daily"], "functions": ["price_move"], "volumes": []}}


def test_spec_from_a_standalone_agent_info():
    from eqds_agentkit import specs
    sp = specs.spec_from_info(INFO, "px")
    assert sp["tools"] == [{"type": "uc_functions", "allow": ["price_move"]}] and sp["access"]["tables"] == ["prices_daily"]


def test_standalone_spec_is_untrusted_input():
    from eqds_agentkit import specs
    import copy
    evil = copy.deepcopy(INFO); evil["access"]["tables"] = ["prices_daily` TO `anyone`; --"]
    with pytest.raises(specs.RegistrationError, match="invalid object names"):
        specs.spec_from_info(evil, "px")
    odd = copy.deepcopy(INFO); odd["tool_config"] = [{"type": "shell", "allow": ["rm"]}]
    with pytest.raises(specs.RegistrationError, match="unsupported tool type"):
        specs.spec_from_info(odd, "px")
    with pytest.raises(specs.RegistrationError, match="does not match"):
        specs.spec_from_info(INFO, "other")
