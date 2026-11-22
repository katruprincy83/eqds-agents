"""Live web search for the classic bridge worker (design §5.6, option 2: the classic worker is the only external egress).

One HTTPS GET to a FIXED vendor endpoint taken from the environment profile (`web_search.endpoint`). The agent can supply
only the validated query, result count and recency; it cannot change the host or the path. The API key is passed in a
request header and is never logged or returned. Vendor: You.com Search API (`X-API-Key`).
"""
from __future__ import annotations

import datetime as dt
import hashlib
from urllib.parse import urlsplit

import httpx

SNIPPET_MAX = 500


class WebSearchError(RuntimeError):
    """Vendor refused or failed the call. The message never contains the key."""


def freshness(recency_days: int) -> str:
    """Map the agent's recency window onto the vendor's freshness buckets (day, week, month, year)."""
    if recency_days <= 1:
        return "day"
    if recency_days <= 7:
        return "week"
    if recency_days <= 31:
        return "month"
    return "year"


def _published(value) -> str | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).isoformat()
    except ValueError:
        return None


def normalize(payload: dict, max_results: int) -> list[dict]:
    """Vendor JSON → rows with the same columns as the `web_search@1` template: doc_id, title, url, snippet, published_at."""
    rows: list[dict] = []
    seen: set[str] = set()
    for item in ((payload.get("results") or {}).get("web") or []):
        url = (item.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        snippet = item.get("description") or " ".join(str(s) for s in (item.get("snippets") or []))
        rows.append({
            "doc_id": hashlib.sha1(url.encode()).hexdigest()[:16],
            "title": (item.get("title") or "").strip(),
            "url": url,
            "snippet": snippet[:SNIPPET_MAX],
            "published_at": _published(item.get("page_age")),
        })
        if len(rows) >= max_results:
            break
    return rows


def _raise_for(status: int) -> None:
    if status in (401, 403):
        raise WebSearchError(f"vendor rejected the API key (HTTP {status}); check the secret youdotcom-api-key")
    if status == 402:
        raise WebSearchError("vendor prepaid credits are depleted (HTTP 402); top up the vendor account")
    if status == 429:
        raise WebSearchError("vendor rate limit reached (HTTP 429)")
    if status >= 400:
        raise WebSearchError(f"vendor returned HTTP {status}")


def search(query: str, max_results: int, recency_days: int, endpoint: str, api_key: str, timeout_s: float = 15.0,
           transport: httpx.BaseTransport | None = None) -> list[dict]:
    if not endpoint.startswith("https://"):
        raise WebSearchError("web_search.endpoint must be https")
    params = {"query": query, "count": max_results, "freshness": freshness(recency_days)}
    with httpx.Client(timeout=timeout_s, transport=transport) as c:
        try:
            r = c.get(endpoint, params=params, headers={"X-API-Key": api_key, "Accept": "application/json"})
        except httpx.HTTPError as e:
            raise WebSearchError(f"vendor unreachable from this compute: {type(e).__name__}") from None
    _raise_for(r.status_code)
    return normalize(r.json(), max_results)


def host_of(endpoint: str) -> str:
    return urlsplit(endpoint).hostname or ""
