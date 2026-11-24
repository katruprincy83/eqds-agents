"""A specialist that started without memory (no CREATE on the database yet) sets it up on a later call, at most once per interval."""
import asyncio
from types import SimpleNamespace

import pytest

from agents.specialist_base import app as sp_app


@pytest.fixture
def clock(monkeypatch):
    now = {"t": 1000.0}
    monkeypatch.setattr(sp_app.time, "monotonic", lambda: now["t"])
    monkeypatch.setattr(sp_app, "_memory_next_try", 0.0)
    return now


def _opener(results):
    calls = []

    def open_memory():
        calls.append(1)
        r = results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    return open_memory, calls


def test_memory_is_attached_once_setup_succeeds(monkeypatch, clock):
    store = object()
    opener, calls = _opener([PermissionError("permission denied for database eqds_prod"), store])
    monkeypatch.setattr(sp_app, "open_memory", opener)
    monkeypatch.setattr(sp_app, "agent", SimpleNamespace(store=None))

    asyncio.run(sp_app.ensure_memory())
    assert sp_app.agent.store is None and len(calls) == 1

    asyncio.run(sp_app.ensure_memory())  # within the interval: no new attempt
    assert len(calls) == 1

    clock["t"] += sp_app.MEMORY_RETRY_S
    asyncio.run(sp_app.ensure_memory())
    assert sp_app.agent.store is store and len(calls) == 2


def test_no_retry_when_memory_is_already_up(monkeypatch, clock):
    opener, calls = _opener([])
    monkeypatch.setattr(sp_app, "open_memory", opener)
    monkeypatch.setattr(sp_app, "agent", SimpleNamespace(store=object()))

    asyncio.run(sp_app.ensure_memory())
    assert calls == []
