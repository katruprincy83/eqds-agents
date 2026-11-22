"""Shared app bootstrap: environment profile, Lakebase pool (token-refreshing), logging."""
from __future__ import annotations

import logging
import os

from eqds_agentkit import lakebase
from eqds_agentkit.envcfg import Env

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")

_env: Env | None = None
_pool: lakebase.RefreshingPool | None = None


def env() -> Env:
    global _env
    if _env is None:
        _env = Env.from_environ()
    return _env


def pool(min_size: int = 1, max_size: int = 8) -> lakebase.RefreshingPool:
    global _pool
    if _pool is None:
        e = env()
        t = lakebase.resolve(e.lakebase_project, e.get("lakebase.branch", "production"), e.lakebase_database)
        _pool = lakebase.RefreshingPool(t, min_size=min_size, max_size=max_size)
    return _pool
