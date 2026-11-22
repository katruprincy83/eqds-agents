"""LLM client: tiers, retry with backoff, endpoint fallback, normalization, cost, tracing (design §8.2)."""
from __future__ import annotations

import logging
import os
import random
import time
from typing import Any

from .config import ModelConfig
from .cost import cost_usd
from .normalize import ChatResult, normalize, parse_structured


def gateway_url(workspace_host: str, workspace_id: str, override: str = "") -> str:
    """Base URL of the Unity AI Gateway. Explicit override (profile `gateway.url` or EQDS_AI_GATEWAY_URL) wins; otherwise
    `https://<workspace-id>.ai-gateway.<cloud domain>/mlflow/v1`, the domain being the cloud domain of the workspace host (verify on Azure/GCP and set `gateway.url` if it differs)."""
    url = os.environ.get("EQDS_AI_GATEWAY_URL") or override
    if url:
        return url.rstrip("/")
    host = workspace_host.replace("https://", "").replace("http://", "").rstrip("/")
    if not host or not workspace_id:
        raise RuntimeError("cannot derive the AI Gateway URL: need the workspace host and id (or set gateway.url / EQDS_AI_GATEWAY_URL)")
    for dom in ("cloud.databricks.com", "gcp.databricks.com", "azuredatabricks.net"):
        if host.endswith("." + dom):
            return f"https://{workspace_id}.ai-gateway.{dom}/mlflow/v1"
    return f"https://{workspace_id}.ai-gateway.{host.split('.', 1)[1]}/mlflow/v1"

log = logging.getLogger("eqds_llm")


class RateLimited(Exception):
    def __init__(self, msg: str = "", retry_after: float | None = None):
        super().__init__(msg)
        self.retry_after = retry_after


class Retryable(Exception):
    pass


class ModelUnavailable(Exception):
    pass


def _status(e: Exception) -> int | None:
    for attr in ("status_code", "http_status", "code"):
        v = getattr(e, attr, None)
        if isinstance(v, int):
            return v
    resp = getattr(e, "response", None)
    if resp is not None and isinstance(getattr(resp, "status_code", None), int):
        return resp.status_code
    return None


def _span(name: str, span_type: str = "CHAT_MODEL"):
    try:
        import mlflow

        return mlflow.start_span(name=name, span_type=span_type)
    except Exception:  # noqa: BLE001
        from contextlib import nullcontext

        return nullcontext(None)


class LLM:
    """`LLM(cfg).chat("standard", messages)`. Graphs name tiers, never endpoints.

    Fault injection (design §13, AC-08): `EQDS_LLM_FAULT=429:primary` (or `500:primary`, `429:all`) makes the primary
    endpoint answer that status for the first N calls (`EQDS_LLM_FAULT_COUNT`, default 4) so fallback can be demonstrated.
    """

    def __init__(self, cfg: ModelConfig, client: Any = None, token: str | None = None, host: str | None = None):
        self.cfg = cfg
        self._client = client
        self._token, self._host = token, host
        self._faults_left: dict[str, int] = {}

    # ── client ─────────────────────────────────────────────────────────────────────────────────
    def client(self):
        """OpenAI-compatible client for the Unity AI Gateway (`<workspace-id>.ai-gateway.<domain>/mlflow/v1`).

        The model name of a request is a model service, `catalog.schema.service`. Credentials are refreshed per request
        (app / job OAuth tokens expire hourly)."""
        if self._client is None:
            import httpx
            from openai import OpenAI

            if self._token:
                host = (self._host or os.environ.get("DATABRICKS_HOST", "")).rstrip("/")
                if host and not host.startswith("http"):
                    host = "https://" + host
                base = gateway_url(host, os.environ.get("DATABRICKS_WORKSPACE_ID", ""), self.cfg.gateway_url)
                token = self._token

                class _Static(httpx.Auth):
                    def auth_flow(self, request):
                        request.headers["Authorization"] = f"Bearer {token}"
                        yield request

                auth: httpx.Auth = _Static()
            else:
                from databricks.sdk import WorkspaceClient

                w = WorkspaceClient()
                base = gateway_url(w.config.host, str(w.get_workspace_id()), self.cfg.gateway_url)

                class _Sdk(httpx.Auth):
                    def auth_flow(self, request):
                        request.headers.update(w.config.authenticate())
                        yield request

                auth = _Sdk()
            self._client = OpenAI(api_key="unused", base_url=base, http_client=httpx.Client(auth=auth, timeout=120), max_retries=0)
        return self._client

    # ── fault injection ────────────────────────────────────────────────────────────────────────
    def _maybe_fault(self, endpoint: str, is_primary: bool) -> None:
        spec = os.environ.get("EQDS_LLM_FAULT")
        if not spec:
            return
        code, _, scope = spec.partition(":")
        if scope == "primary" and not is_primary:
            return
        left = self._faults_left.setdefault(spec, int(os.environ.get("EQDS_LLM_FAULT_COUNT", "4")))
        if left > 0:
            self._faults_left[spec] = left - 1
            if code == "429":
                raise RateLimited(f"injected 429 on {endpoint}", retry_after=0.01)
            raise Retryable(f"injected {code} on {endpoint}")

    def _backoff(self, attempt: int, retry_after: float | None = None) -> float:
        r = self.cfg.retry
        d = retry_after if retry_after else min(r.max_delay_s, r.base_delay_s * (2 ** attempt))
        return d * (0.75 + random.random() * 0.5) if not retry_after else d

    # ── chat ───────────────────────────────────────────────────────────────────────────────────
    def chat(self, tier: str, messages: list[dict], tools: list[dict] | None = None, response_schema: Any | None = None,
             max_tokens: int | None = None, temperature: float | None = None, **kw) -> ChatResult:
        t = self.cfg.tier(tier)
        endpoints = [e for e in (t.primary, t.fallback) if e]
        last: Exception | None = None
        for ei, endpoint in enumerate(endpoints):
            for attempt in range(self.cfg.retry.max_attempts):
                with _span(f"llm.{tier}") as span:
                    try:
                        self._maybe_fault(endpoint, ei == 0)
                        args: dict[str, Any] = dict(model=endpoint, messages=messages, max_tokens=max_tokens or t.max_output_tokens, **kw)
                        if tools:
                            args["tools"] = tools
                        if temperature is not None:
                            args["temperature"] = temperature
                        raw = self.client().chat.completions.create(**args)
                        res = normalize(raw, endpoint)
                        res.cost_usd = cost_usd(self.cfg.pricing, self.cfg.price_keys.get(endpoint, endpoint), res.usage.input, res.usage.output)
                        if response_schema is not None and not res.tool_calls:
                            try:
                                res.parsed = parse_structured(res.text, response_schema)
                            except Exception as pe:  # noqa: BLE001  one repair retry (design §8.3)
                                fix = messages + [{"role": "assistant", "content": res.text},
                                                  {"role": "user", "content": f"Your reply was not valid JSON for the required schema ({pe}). Reply with ONLY the JSON object."}]
                                raw2 = self.client().chat.completions.create(model=endpoint, messages=fix, max_tokens=max_tokens or t.max_output_tokens)
                                r2 = normalize(raw2, endpoint)
                                res.parsed = parse_structured(r2.text, response_schema)
                                res.usage.input += r2.usage.input
                                res.usage.output += r2.usage.output
                                res.cost_usd += cost_usd(self.cfg.pricing, self.cfg.price_keys.get(endpoint, endpoint), r2.usage.input, r2.usage.output)
                                res.text = r2.text
                        if span is not None:
                            span.set_attributes({"endpoint": endpoint, "tier": tier, "tokens_in": res.usage.input,
                                                 "tokens_out": res.usage.output, "cost_usd": res.cost_usd, "fallback": ei > 0})
                        return res
                    except RateLimited as e:
                        last = e
                        time.sleep(self._backoff(attempt, e.retry_after))
                    except Retryable as e:
                        last = e
                        time.sleep(self._backoff(attempt))
                    except Exception as e:  # noqa: BLE001
                        code = _status(e)
                        if code in self.cfg.retry.retry_on:
                            last = e
                            ra = None
                            hdr = getattr(getattr(e, "response", None), "headers", None)
                            if hdr and hdr.get("retry-after"):
                                try:
                                    ra = float(hdr["retry-after"])
                                except ValueError:
                                    ra = None
                            time.sleep(self._backoff(attempt, ra if code == 429 else None))
                        else:
                            raise
            log.warning("tier %s failing over from %s", tier, endpoint)
        raise ModelUnavailable(f"tier {tier} unavailable after retries and fallback: {last}")
