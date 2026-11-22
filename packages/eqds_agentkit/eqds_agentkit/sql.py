"""SQL Statement API helper (serverless SQL warehouse) used by apps that need UC reads without Spark."""
from __future__ import annotations

import time


def _cast(v, ctype: str):
    if v is None:
        return None
    c = (ctype or "").upper()
    try:
        if c in ("INT", "LONG", "BYTE", "SHORT"):
            return int(v)
        if c in ("DOUBLE", "FLOAT", "DECIMAL"):
            return float(v)
        if c == "BOOLEAN":
            return v.lower() == "true"
    except ValueError:
        return v
    return v


def run_statement(w, warehouse_id: str, sql: str, timeout_s: int = 50) -> list[dict]:
    from databricks.sdk.service.sql import StatementState

    r = w.statement_execution.execute_statement(statement=sql, warehouse_id=warehouse_id, wait_timeout="30s")
    t0 = time.time()
    while r.status.state in (StatementState.PENDING, StatementState.RUNNING) and time.time() - t0 < timeout_s:
        time.sleep(1.0)
        r = w.statement_execution.get_statement(r.statement_id)
    if r.status.state != StatementState.SUCCEEDED:
        raise RuntimeError(f"statement {r.status.state}: {r.status.error.message if r.status.error else ''}")
    cols = [(c.name, c.type_name.value if c.type_name else "") for c in r.manifest.schema.columns]
    return [{n: _cast(v, t) for (n, t), v in zip(cols, row)} for row in (r.result.data_array or [])] if r.result else []


def lit(s: str) -> str:
    return "'" + str(s).replace("\\", "\\\\").replace("'", "''") + "'"
