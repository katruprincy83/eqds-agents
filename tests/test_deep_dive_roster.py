"""Follow-up probes in the deep dive go only to the roster the moderator opened, never to other registered agents."""
from __future__ import annotations

from types import SimpleNamespace

from agents.moderator.graph import Moderator, Probe, Probes


class _Fake:
    """Just enough of Moderator for n_deep_dive: the registry, one canned LLM answer, and the event/stage hooks."""

    def __init__(self, registered: list[str], probes: list[Probe]):
        self.reg = SimpleNamespace(all=lambda: [SimpleNamespace(key=k, description=f"{k} desk") for k in registered])
        self._probes = probes
        self.events: list[tuple[str, dict]] = []
        self.asked: dict = {}

    def stage(self, *a, **k):
        pass

    def ev(self, st, name, payload):
        self.events.append((name, payload))

    def ask(self, st, tier, name, schema, user_extra="", **kw):
        self.asked = kw
        return Probes(probes=self._probes), {}

    registry_text = Moderator.registry_text


def _state(selected: list[str]) -> dict:
    return {"round": 1, "selected": selected, "messages": [], "points": [
        {"id": "p1", "topic": "net flow", "claim": "GIC sold Kioxia", "stances": {"news": "partial"}, "gaps": "no fund filing",
         "status": "OPEN", "needs_followup": False}]}


ALL = ["news", "georev", "broker", "earnings"]


def test_probe_to_an_unselected_agent_is_dropped():
    fake = _Fake(ALL, [Probe(point_id="p1", to=["georev"], text="check the filing")])
    out = Moderator.n_deep_dive(fake, _state(["news"]))
    assert out["probes"] == []


def test_probe_is_narrowed_to_the_roster():
    fake = _Fake(ALL, [Probe(point_id="p1", to=["news", "georev", "broker"], text="verify the flow")])
    out = Moderator.n_deep_dive(fake, _state(["news", "broker"]))
    assert [p["to"] for p in out["probes"]] == [["news", "broker"]]


def test_the_model_only_sees_the_roster():
    fake = _Fake(ALL, [])
    Moderator.n_deep_dive(fake, _state(["news"]))
    assert "news desk" in fake.asked["registry"] and "georev" not in fake.asked["registry"] and "earnings" not in fake.asked["registry"]


def test_empty_roster_skips_the_model_and_probes_nobody():
    fake = _Fake(ALL, [Probe(point_id="p1", to=["news"], text="x")])
    out = Moderator.n_deep_dive(fake, _state([]))
    assert out["probes"] == [] and fake.asked == {}
