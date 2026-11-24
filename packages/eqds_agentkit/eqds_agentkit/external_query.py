"""Allowlisted query templates: render one with validated arguments into (sql, params), and run it on the platform SQL warehouse.

No free text ever reaches SQL: identifier lists are regex-validated company ids, search terms are alphanumeric tokens bound as
parameters. The foreign catalog `azure_oms` (Lakehouse Federation to Azure SQL) is read under the calling app's own grants."""
from __future__ import annotations

import datetime as dt
import decimal
import os
import pathlib
import re
import time

from eqds_agentkit.external_tools import TOOLS, tokens, validate

TEMPLATE_DIR = pathlib.Path(__file__).parent / "query_templates"


def _ids(ids: list[str]) -> str:
    return ",".join(f"'{i}'" for i in ids)


def _like(col_exprs: list[str], terms: list[str], prefix: str = "q") -> tuple[str, dict]:
    params = {f"{prefix}{i}": f"%{t}%" for i, t in enumerate(terms)}
    clauses = [f"{c} ILIKE :{prefix}{i}" for i in range(len(terms)) for c in col_exprs]
    return " OR ".join(clauses), params


def render(tool: str, args: dict, uc: str, fc: str, now: dt.datetime | None = None) -> tuple[str, dict]:
    """→ (spark sql with named parameters, params). `uc` = catalog.schema, `fc` = foreign catalog."""
    a = validate(tool, args)
    d = a.model_dump(mode="json")
    sql = "\n".join(l for l in (TEMPLATE_DIR / f"{tool}.sql").read_text().splitlines() if not l.lstrip().startswith("--"))
    now = now or dt.datetime.utcnow()
    f: dict[str, str] = {"uc": uc, "fc": fc}
    p: dict = {}
    if tool == "oms_trade_flows":
        f.update(ids=_ids(d["company_ids"]), dim={"company": "t.company_id", "strategy": "t.strategy_id", "account": "t.account_id"}[d["group_by"]])
        p = {"start": d["start"], "end": d["end"]}
    elif tool == "oms_confirm_conflict":
        if d.get("company_ids"):
            f["id_filter"] = f"AND t.company_id IN ({_ids(d['company_ids'])})"
            f["universe"] = f"SELECT company_id, CAST(NULL AS INT) AS score, CAST(NULL AS INT) AS buyers FROM {uc}.ref_companies WHERE company_id IN ({_ids(d['company_ids'])})"
        else:
            f["id_filter"] = ""
            flag = "in_universe200" if d.get("basket") == "efm_universe200" else "in_top50"
            f["universe"] = (f"SELECT company_id, score, buyers FROM {uc}.efm_buy_basket "
                             f"WHERE month_end = (SELECT MAX(month_end) FROM {uc}.efm_buy_basket) AND {flag}")
        p = {"start": d["start"], "end": d["end"], "strong_musd": float(d["strong_musd"])}
    elif tool == "oms_post_trade_move":
        p = {"company_id": d["company_id"], "start": d["start"], "end": d["end"], "horizon_days": int(d["horizon_days"])}
    elif tool == "oms_positions":
        f["ids"] = _ids(d["company_ids"])
        p = {"as_of": d["as_of"], "fund": d.get("fund") or ""}
    elif tool == "research_views":
        p = {"company_id": d["company_id"], "since": d["since"]}
    elif tool == "research_notes_search":
        like, lp = _like(["title", "body"], tokens(d["query"]), "q")
        f.update(like_clause=like, limit=str(int(d["limit"])))
        p = {"company_id": d["company_id"], **lp}
    elif tool == "news_latest":
        since = (now - dt.timedelta(hours=int(d["hours"]))).strftime("%Y-%m-%d %H:%M:%S")
        f["limit"] = str(int(d["limit"]))
        if d.get("company_id"):
            f.update(join="", filter="AND n.company_id = :company_id")
            p["company_id"] = d["company_id"]
        elif d.get("ticker"):
            f.update(join=f"JOIN {uc}.ref_companies c ON c.company_id = n.company_id", filter="AND lower(c.ticker) = lower(:ticker)")
            p["ticker"] = d["ticker"]
        else:
            f.update(join="", filter="")
        p["since"] = since
    elif tool == "web_search":
        like, lp = _like(["title", "snippet", "keywords"], tokens(d["query"]), "q")
        since = (now - dt.timedelta(days=int(d["recency_days"]))).strftime("%Y-%m-%d %H:%M:%S")
        f.update(like_clause=like, limit=str(int(d["max_results"])))
        p = {"since": since, **lp}
    elif tool == "apollo_lookup":
        raise NotImplementedError("apollo_lookup is a stub until onboarded")
    sql = sql.format_map(_Safe(f))
    return sql, p


class _Safe(dict):
    def __missing__(self, k):  # unknown placeholder = template bug
        raise KeyError(f"template placeholder {{{k}}} not provided")


def source_for(tool: str, uc: str, fc: str) -> dict:
    t = TOOLS[tool]
    return {"system": "azure_sql", "object": t["object"].replace("azure_oms", fc), "template": f"{tool}@{t['version']}",
            "as_of": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")}


# ── execution on the SQL warehouse ───────────────────────────────────────────────────────────────
MAX_ROWS = 500
WEB_COLUMNS = ["doc_id", "title", "url", "snippet", "published_at"]
_TS = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _param(name: str, value):
    """A typed statement parameter. Dates and timestamps are sent typed, so the comparisons match the column types."""
    from databricks.sdk.service.sql import StatementParameterListItem

    if isinstance(value, bool):
        return StatementParameterListItem(name=name, value=str(value).lower(), type="BOOLEAN")
    if isinstance(value, int):
        return StatementParameterListItem(name=name, value=str(value), type="INT")
    if isinstance(value, float):
        return StatementParameterListItem(name=name, value=repr(value), type="DOUBLE")
    text = str(value)
    typ = "TIMESTAMP" if _TS.fullmatch(text) else "DATE" if _DATE.fullmatch(text) else "STRING"
    return StatementParameterListItem(name=name, value=text, type=typ)


def _jsonable(v):
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (dt.datetime, dt.date)):
        return v.isoformat()
    return v


def execute(w, warehouse_id: str, sql: str, params: dict, timeout_s: float = 50.0) -> tuple[list[str], list[dict], bool]:
    """Run one statement on the warehouse with bound parameters → (columns, rows, truncated)."""
    from databricks.sdk.service.sql import StatementState

    r = w.statement_execution.execute_statement(statement=sql, warehouse_id=warehouse_id, parameters=[_param(k, v) for k, v in params.items()],
                                                wait_timeout="30s", row_limit=MAX_ROWS + 1)
    t0 = time.time()
    while r.status.state in (StatementState.PENDING, StatementState.RUNNING) and time.time() - t0 < timeout_s:
        time.sleep(0.5)
        r = w.statement_execution.get_statement(r.statement_id)
    if r.status.state != StatementState.SUCCEEDED:
        msg = (r.status.error.message if r.status.error else "") or str(r.status.state)
        raise RuntimeError(f"warehouse query failed: {msg[:400]}")
    cols = [c.name for c in r.manifest.schema.columns]
    types = [(c.type_name.value if c.type_name else "STRING") for c in r.manifest.schema.columns]
    data = r.result.data_array or []
    rows = []
    for raw in data:
        row = {}
        for name, typ, v in zip(cols, types, raw):
            if v is not None and typ in ("DOUBLE", "FLOAT", "DECIMAL"):
                v = float(v)
            elif v is not None and typ in ("INT", "LONG", "BIGINT", "SHORT", "BYTE"):
                v = int(v)
            row[name] = v
        rows.append(row)
    return cols, rows, len(rows) > MAX_ROWS


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


def run(w, warehouse_id: str, env, tool: str, args: dict) -> dict:
    """Validate, render and run one allowlisted tool → the result the agent's tool wrapper expects (status, rows, source)."""
    args = validate(tool, args).model_dump(mode="json")
    if tool == "web_search" and env.get("web_search.mode", "stub") == "vendor":
        return web_search_vendor(env, args)
    sql, params = render(tool, args, env.uc, env.foreign_catalog)
    t0 = time.time()
    cols, rows, truncated = execute(w, warehouse_id, sql, params)
    rows = [{k: _jsonable(v) for k, v in r.items()} for r in rows[:MAX_ROWS]]
    res = {"status": "ok", "rows": rows, "columns": cols, "row_count": len(rows), "truncated": truncated,
           "source": source_for(tool, env.uc, env.foreign_catalog), "latency_ms": int((time.time() - t0) * 1000)}
    if tool == "oms_confirm_conflict":
        res["summary"] = summarize_confirm_conflict(rows)
    return res


def web_search_vendor(env, args: dict) -> dict:
    """Live web search straight from the agent: one fixed https endpoint from the profile, the key from the app's secret resource."""
    from eqds_agentkit import web_search as ws

    key = os.environ.get("YOUDOTCOM_API_KEY", "")
    endpoint = env.get("web_search.endpoint")
    if not key:
        raise RuntimeError("web_search.mode=vendor needs the YOUDOTCOM_API_KEY app secret")
    if not endpoint:
        raise RuntimeError("web_search.endpoint is not set in the environment profile")
    t0 = time.time()
    rows = ws.search(args["query"], args["max_results"], args["recency_days"], endpoint, key, float(env.get("web_search.timeout_s", 15)))
    return {"status": "ok", "rows": rows, "columns": WEB_COLUMNS, "row_count": len(rows), "truncated": False,
            "source": {"system": "web_search_vendor", "vendor": env.get("web_search.vendor", ""), "object": ws.host_of(endpoint),
                       "template": "web_search@2", "as_of": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")},
            "latency_ms": int((time.time() - t0) * 1000)}
