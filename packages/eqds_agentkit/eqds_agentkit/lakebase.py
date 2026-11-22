"""Lakebase (Postgres) access with OAuth token refresh (design §5.7, §5.2.2).

Works identically for a user, a job service principal and an app service principal: the connection identity is
`WorkspaceClient().current_user.me().user_name`, the password is a short-lived Lakebase credential minted for the
project's read-write endpoint. A pool re-mints the token for every new connection, so tokens never expire mid-run.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg import sql  # noqa: F401  (re-exported for callers)
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

_TOKEN_TTL_S = 45 * 60  # credentials last 1 h; refresh early


@dataclass
class LakebaseTarget:
    project: str
    branch: str
    database: str
    host: str
    endpoint: str
    user: str


def _w():
    from databricks.sdk import WorkspaceClient

    return WorkspaceClient()


def resolve(project: str | None = None, branch: str | None = None, database: str | None = None, w=None) -> LakebaseTarget:
    """Resolve host / endpoint / identity from the workspace. Env vars override (set by the Apps `postgres` resource)."""
    w = w or _w()
    project = project or os.environ.get("LAKEBASE_PROJECT") or ""
    branch = branch or os.environ.get("LAKEBASE_BRANCH") or "production"
    database = database or os.environ.get("LAKEBASE_DATABASE") or os.environ.get("PGDATABASE") or "databricks_postgres"
    branch_name = f"projects/{project}/branches/{branch}"
    host = os.environ.get("LAKEBASE_HOST") or os.environ.get("PGHOST")
    endpoint = None
    for ep in w.postgres.list_endpoints(parent=branch_name):
        if ep.status and ep.status.endpoint_type and "READ_WRITE" in str(ep.status.endpoint_type):
            endpoint = ep.name
            host = host or ep.status.hosts.host
            break
    if endpoint is None:
        raise RuntimeError(f"no read-write endpoint on {branch_name}")
    user = os.environ.get("LAKEBASE_USER") or os.environ.get("PGUSER") or w.current_user.me().user_name
    return LakebaseTarget(project, branch, database, host, endpoint, user)


class TokenSource:
    def __init__(self, target: LakebaseTarget, w=None):
        self.t, self.w, self._tok, self._at, self._lock = target, w or _w(), None, 0.0, threading.Lock()

    def get(self) -> str:
        with self._lock:
            if not self._tok or time.time() - self._at > _TOKEN_TTL_S:
                self._tok = self.w.postgres.generate_database_credential(endpoint=self.t.endpoint).token
                self._at = time.time()
            return self._tok


def _conninfo(t: LakebaseTarget, token: str, timeout: int = 15) -> str:
    return f"host={t.host} port=5432 dbname={t.database} user={t.user} password={token} sslmode=require connect_timeout={timeout}"


def connect(target: LakebaseTarget | None = None, autocommit: bool = True, **kw) -> psycopg.Connection:
    t = target or resolve()
    return psycopg.connect(_conninfo(t, TokenSource(t).get()), autocommit=autocommit, row_factory=dict_row, **kw)


class RefreshingPool:
    """psycopg pool whose connections always carry a fresh token (connection subclass injects the password)."""

    def __init__(self, target: LakebaseTarget | None = None, min_size: int = 1, max_size: int = 10, options: str | None = None, saver: bool = False):
        self.t = target or resolve()
        tokens = TokenSource(self.t)

        class _TokenConnection(psycopg.Connection):
            @classmethod
            def connect(cls, conninfo: str = "", **kwargs):  # type: ignore[override]
                kwargs["password"] = tokens.get()
                return super().connect(conninfo, **kwargs)

        self.pool = ConnectionPool(
            conninfo=f"host={self.t.host} port=5432 dbname={self.t.database} user={self.t.user} sslmode=require connect_timeout=15"
                     + (f" options='{options}'" if options else ""),
            connection_class=_TokenConnection, min_size=min_size, max_size=max_size, max_lifetime=30 * 60,
            kwargs={"autocommit": True, "row_factory": dict_row, **({"prepare_threshold": 0} if saver else {})},
            check=ConnectionPool.check_connection, open=False)
        self.pool.open(wait=False)

    def connection(self, timeout: float = 15.0):
        return self.pool.connection(timeout=timeout)

    def close(self) -> None:
        self.pool.close()
