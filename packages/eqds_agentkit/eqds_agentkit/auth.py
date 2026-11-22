"""Authentication helpers (design §6.3): app service-principal M2M tokens, OBO passthrough."""
from __future__ import annotations

import threading
import time

_lock = threading.Lock()
_cache: dict[str, tuple[str, float]] = {}


def sp_token(w=None) -> str:
    """OAuth bearer token of the running identity (app SP in Databricks Apps, the CLI user locally). Cached ~50 min."""
    with _lock:
        tok = _cache.get("t")
        if tok and tok[1] > time.time():
            return tok[0]
        if w is None:
            from databricks.sdk import WorkspaceClient

            w = WorkspaceClient()
        h = w.config.authenticate()
        t = h["Authorization"].split(" ", 1)[1]
        _cache["t"] = (t, time.time() + 50 * 60)
        return t


def bearer(w=None) -> dict[str, str]:
    return {"Authorization": f"Bearer {sp_token(w)}"}


def user_from_headers(headers) -> str:
    """Identity forwarded by Databricks Apps (`x-forwarded-email`) or by the moderator (`x-eqds-user`)."""
    for h in ("x-eqds-user", "x-forwarded-email", "x-forwarded-preferred-username"):
        v = headers.get(h)
        if v:
            return v
    return "anonymous"


def obo_token(headers) -> str | None:
    return headers.get("x-forwarded-access-token")


def job_caller_headers(w, env) -> dict[str, str]:
    """Authorization header for batch jobs that call the moderator app. Databricks Apps accept OAuth tokens only (not notebook
    tokens), so in order: EQDS_EVAL_TOKEN (CI) -> the job-caller service principal in the secret scope (M2M) -> the ambient identity
    (a laptop with `databricks auth login`)."""
    import os

    tok = os.environ.get("EQDS_EVAL_TOKEN")
    if tok:
        return {"Authorization": f"Bearer {tok}"}
    try:
        from databricks.sdk import WorkspaceClient
        from databricks.sdk.runtime import dbutils

        cid = dbutils.secrets.get(env.secret_scope, "job-caller-client-id")
        sec = dbutils.secrets.get(env.secret_scope, "job-caller-client-secret")
        return WorkspaceClient(host=w.config.host, client_id=cid, client_secret=sec, auth_type="oauth-m2m").config.authenticate()
    except Exception as e:  # noqa: BLE001
        print(f"job_caller_headers: falling back to the ambient identity ({type(e).__name__}: {str(e)[:160]})", flush=True)
        return w.config.authenticate()
