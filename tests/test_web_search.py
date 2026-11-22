import json

import httpx
import pytest

from bridge_worker import web_search as ws
from bridge_worker.worker import Executor
from eqds_agentkit.envcfg import Env

PAYLOAD = {"results": {"web": [
    {"url": "https://a.example/x", "title": " A ", "description": "first", "page_age": "2026-11-16T06:04:24Z"},
    {"url": "https://a.example/x", "title": "dup", "description": "dup"},
    {"url": "https://b.example/y", "title": "B", "snippets": ["s1", "s2"], "page_age": "not a date"},
    {"url": "", "title": "no url"},
]}, "metadata": {}}


def transport(status=200, body=PAYLOAD, seen=None):
    def handler(request: httpx.Request):
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, json=body)
    return httpx.MockTransport(handler)


def test_normalize_dedupes_and_maps_columns():
    rows = ws.normalize(PAYLOAD, 10)
    assert [r["url"] for r in rows] == ["https://a.example/x", "https://b.example/y"]
    assert rows[0]["title"] == "A" and rows[0]["published_at"].startswith("2026-11-16T06:04:24")
    assert rows[1]["snippet"] == "s1 s2" and rows[1]["published_at"] is None
    assert set(rows[0]) == {"doc_id", "title", "url", "snippet", "published_at"}


def test_max_results_caps_rows():
    assert len(ws.normalize(PAYLOAD, 1)) == 1


def test_freshness_buckets():
    assert [ws.freshness(d) for d in (1, 7, 30, 90)] == ["day", "week", "month", "year"]


def test_request_goes_to_fixed_endpoint_with_key_in_header_only():
    seen = []
    rows = ws.search("SK Hynix", 3, 7, "https://ydc-index.io/v1/search", "KEY123", transport=transport(seen=seen))
    req = seen[0]
    assert req.url.host == "ydc-index.io" and req.url.path == "/v1/search"
    assert req.headers["x-api-key"] == "KEY123"
    assert "KEY123" not in str(req.url)
    assert req.url.params["query"] == "SK Hynix" and req.url.params["count"] == "3" and req.url.params["freshness"] == "week"
    assert len(rows) == 2


@pytest.mark.parametrize("status,needle", [(401, "rejected the API key"), (402, "credits are depleted"), (429, "rate limit"), (500, "HTTP 500")])
def test_vendor_errors_are_clear_and_keyless(status, needle):
    with pytest.raises(ws.WebSearchError, match=needle) as e:
        ws.search("q", 3, 7, "https://ydc-index.io/v1/search", "KEY123", transport=transport(status=status, body={"error": "x"}))
    assert "KEY123" not in str(e.value)


def test_plain_http_endpoint_refused():
    with pytest.raises(ws.WebSearchError, match="https"):
        ws.search("q", 3, 7, "http://ydc-index.io/v1/search", "k")


def test_executor_vendor_mode_reads_secret_and_calls_fixed_endpoint(monkeypatch):
    env = Env.from_target("demo")
    env.raw.setdefault("web_search", {})["mode"] = "vendor"
    calls = {}

    def fake_search(query, max_results, recency_days, endpoint, api_key, timeout_s=15.0, transport=None):
        calls.update(query=query, endpoint=endpoint, key=api_key, n=max_results, days=recency_days)
        return ws.normalize(PAYLOAD, max_results)

    monkeypatch.setattr(ws, "search", fake_search)
    ex = Executor(spark=None, env=env, secret_fn=lambda scope, key: (calls.setdefault("scope_key", (scope, key)), "SECRET")[1])
    out = ex.run("web_search", {"query": "GIC SK Hynix", "max_results": 2, "recency_days": 7})
    assert calls["scope_key"] == (env.secret_scope, "youdotcom-api-key")
    assert calls["endpoint"] == "https://ydc-index.io/v1/search" and calls["key"] == "SECRET"
    assert out["row_count"] == 2 and out["columns"] == ["doc_id", "title", "url", "snippet", "published_at"]
    assert out["source"]["system"] == "web_search_vendor" and out["source"]["object"] == "ydc-index.io"


def test_executor_rejects_bad_arguments_before_any_call(monkeypatch):
    env = Env.from_target("demo")
    env.raw.setdefault("web_search", {})["mode"] = "vendor"
    monkeypatch.setattr(ws, "search", lambda *a, **k: pytest.fail("vendor must not be called"))
    ex = Executor(spark=None, env=env, secret_fn=lambda *a: "k")
    with pytest.raises(Exception):
        ex.run("web_search", {"query": "x" * 300, "max_results": 3, "recency_days": 7})
