"""Render an allowlisted template with validated arguments into (sql, params). No free text ever reaches SQL:
identifier lists are regex-validated company ids, search terms are alphanumeric tokens bound as parameters."""
from __future__ import annotations

import datetime as dt
import os
import pathlib

from eqds_agentkit.bridge_tools import TOOLS, tokens, validate

TEMPLATE_DIR = pathlib.Path(__file__).parent / "templates"


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
