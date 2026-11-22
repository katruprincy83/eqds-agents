"""Bridge worker (design §5.6): claims `bridge.requests` rows from Lakebase and runs allowlisted templates against the
UC foreign catalog with spark.sql. Runs as a continuous Lakeflow job (classic single node in the target state, serverless
where classic nodes are unavailable, see `bridge_compute` in the environment profile). I/O-only: no LLM calls.
"""
from __future__ import annotations

import datetime as dt
import decimal
import json
import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from psycopg.types.json import Jsonb

log = logging.getLogger("bridge_worker")
MAX_ROWS = 500
TOOL_TIMEOUT_S = 20


def _jsonable(v):
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (dt.datetime, dt.date)):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    return v


def summarize_confirm_conflict(rows: list[dict]) -> dict:
    out: dict = {}
    for cls in ("strong_conflict", "conflict", "none", "confirm", "strong_confirm"):
        rs = [r for r in rows if r["class"] == cls]
        out[cls] = {"names": len(rs), "net_musd": round(sum(r["net_musd"] for r in rs), 2)}
    conf = [r for r in rows if r["net_musd"] < 0]
    ok = [r for r in rows if r["net_musd"] > 0]
    out["book"] = {"conflicts": len(conf), "conflict_musd": round(sum(abs(r["net_musd"]) for r in conf), 1),
                   "confirms": len(ok), "confirm_musd": round(sum(r["net_musd"] for r in ok), 1)}
    return out


WEB_COLUMNS = ["doc_id", "title", "url", "snippet", "published_at"]


class Executor:
    def __init__(self, spark, env, secret_fn=None):
        self.spark, self.env, self._secret_fn = spark, env, secret_fn
        self._web_key: str | None = None

    def run(self, tool: str, args: dict) -> dict:
        from bridge_worker.render import render, source_for

        if tool == "web_search" and self.env.get("web_search.mode", "stub") == "vendor":
            return self._web_search_vendor(args)
        sql, params = render(tool, args, self.env.uc, self.env.foreign_catalog)
        t0 = time.time()
        self.spark.sparkContext.setLocalProperty("spark.job.description", f"bridge:{tool}") if hasattr(self.spark, "sparkContext") else None
        df = self.spark.sql(sql, args=params)
        rows = [_jsonable(r.asDict(recursive=True)) for r in df.limit(MAX_ROWS + 1).collect()]
        truncated = len(rows) > MAX_ROWS
        rows = rows[:MAX_ROWS]
        res = {"rows": rows, "columns": df.columns, "row_count": len(rows), "truncated": truncated,
               "source": source_for(tool, self.env.uc, self.env.foreign_catalog), "latency_ms": int((time.time() - t0) * 1000)}
        if tool == "oms_confirm_conflict":
            res["summary"] = summarize_confirm_conflict(rows)
        return res


    def _web_search_vendor(self, args: dict) -> dict:
        """Live web search from classic compute. The host is fixed by the profile; only validated arguments are sent."""
        from bridge_worker import web_search as ws
        from eqds_agentkit.bridge_tools import validate

        a = validate("web_search", args).model_dump(mode="json")
        if self._web_key is None:
            if self._secret_fn is None:
                raise RuntimeError("web_search.mode=vendor needs a secret reader")
            self._web_key = self._secret_fn(self.env.secret_scope, self.env.get("web_search.secret_key", "youdotcom-api-key"))
        endpoint = self.env.get("web_search.endpoint")
        if not endpoint:
            raise RuntimeError("web_search.endpoint is not set in the environment profile")
        t0 = time.time()
        rows = ws.search(a["query"], a["max_results"], a["recency_days"], endpoint, self._web_key,
                         float(self.env.get("web_search.timeout_s", 15)))
        return {"rows": rows, "columns": WEB_COLUMNS, "row_count": len(rows), "truncated": False,
                "source": {"system": "web_search_vendor", "vendor": self.env.get("web_search.vendor", ""), "object": ws.host_of(endpoint),
                           "template": "web_search@2", "as_of": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")},
                "latency_ms": int((time.time() - t0) * 1000)}


def claim(conn):
    return conn.execute(
        """UPDATE bridge.requests SET status = 'RUNNING', started_at = now()
           WHERE id = (SELECT id FROM bridge.requests WHERE status = 'PENDING' AND expires_at > now()
                       ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1)
           RETURNING id, tool, args""").fetchone()


def finish(conn, rid, result=None, error=None):
    conn.execute("UPDATE bridge.requests SET status = %s, result = %s, error = %s, finished_at = now() WHERE id = %s AND status = 'RUNNING'",
                 ("ERROR" if error else "DONE", Jsonb(result) if result is not None else None, error, rid))


def handle(pool, ex: Executor, req: dict, timeout_s: float = TOOL_TIMEOUT_S) -> None:
    rid = req["id"]
    box: dict = {}

    def go():
        try:
            box["r"] = ex.run(req["tool"], req["args"])
        except Exception as e:  # noqa: BLE001
            box["e"] = f"{type(e).__name__}: {str(e)[:600]}"

    th = threading.Thread(target=go, daemon=True)
    th.start()
    th.join(timeout_s)
    with pool.connection() as c:
        if th.is_alive():
            finish(c, rid, error=f"timeout after {int(timeout_s)}s")
        elif "e" in box:
            finish(c, rid, error=box["e"])
        else:
            finish(c, rid, result=box["r"])


def run(spark, env, max_runtime_s: float | None = None, pool_size: int = 4, secret_fn=None) -> None:
    from eqds_agentkit import lakebase

    t = lakebase.resolve(env.lakebase_project, env.get("lakebase.branch", "production"), env.lakebase_database)
    pool = lakebase.RefreshingPool(t, min_size=1, max_size=pool_size + 2)
    ex = Executor(spark, env, secret_fn)
    tp = ThreadPoolExecutor(pool_size)
    started, last_expire = time.time(), 0.0
    log.info("bridge worker started (%s, compute=%s)", env.target, env.get("bridge_compute"))
    listener = None
    listener_at = 0.0
    while max_runtime_s is None or time.time() - started < max_runtime_s:
        try:
            if listener is None or time.time() - listener_at > 20 * 60:  # token rotation: reconnect the LISTEN connection
                if listener is not None:
                    listener.close()
                listener = lakebase.connect(t)
                listener.execute("LISTEN bridge")
                listener_at = time.time()
            did = False
            while True:
                with pool.connection() as c:
                    req = claim(c)
                if not req:
                    break
                did = True
                tp.submit(handle, pool, ex, req, float(env.get('bridge.tool_timeout_s', TOOL_TIMEOUT_S)))
            if time.time() - last_expire > 5:
                with pool.connection() as c:
                    c.execute("UPDATE bridge.requests SET status = 'EXPIRED', finished_at = now() WHERE status IN ('PENDING','RUNNING') AND expires_at < now()")
                    c.execute("DELETE FROM bridge.requests WHERE created_at < now() - interval '7 days'")
                last_expire = time.time()
            if not did:
                for _ in listener.notifies(timeout=2.0, stop_after=1):
                    break
        except Exception as e:  # noqa: BLE001
            log.exception("worker loop error: %s", e)
            listener = None
            time.sleep(3)
    pool.close()

