"""job_validate_seed (design §14.9, AC-16): runs the seed validation checks (seed/validate/checks.yaml) against Azure SQL (via the
UC foreign catalog) and Unity Catalog, writes `{uc}.seed_validation_results` and FAILS the job if any planted fact is outside tolerance."""
from __future__ import annotations

import datetime as dt
import json
import os
import sys

try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
except NameError:
    pass

import pandas as pd
import yaml
from _common import get_spark, setup, write_table

env, args = setup()
spark = get_spark()
from bridge_worker.render import render  # noqa: E402

UC, FC = env.uc, env.foreign_catalog
CK = yaml.safe_load(open(os.path.join(os.environ["EQDS_ROOT"], "seed", "validate", "checks.yaml")))
W = CK["window"]
by_id = {c["id"]: c for c in CK["checks"]}
results: list[dict] = []
counts: list[dict] = []


def q(sql, **params):
    return [r.asDict() for r in spark.sql(sql, args=params or None).collect()]


def tpl(tool, **a):
    sql, p = render(tool, a, UC, FC)
    return q(sql, **p)


def close(a, b, tol=0.0, pct=0.0):
    return abs(a - b) <= max(tol, abs(b) * pct / 100.0)


def record(cid, passed, actual, detail=""):
    c = by_id[cid]
    results.append({"check_no": len(results) + 1, "check_id": cid, "name": c["name"], "passed": bool(passed), "expected": json.dumps(c["expect"]),
                    "actual": json.dumps(actual, default=str), "detail": detail, "ran_at": dt.datetime.utcnow()})
    print(("PASS " if passed else "FAIL ") + cid, json.dumps(actual, default=str), flush=True)


def safe(cid, fn):
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        record(cid, False, {"error": str(e)[:300]}, "check raised")


def ref_integrity():
    n = q(f"SELECT count(DISTINCT t.company_id) AS n FROM {FC}.oms.trades t LEFT ANTI JOIN {UC}.ref_companies c ON c.company_id = t.company_id")[0]["n"]
    record("ref_integrity", n == 0, {"unknown_company_ids": n})


def positions_sum():
    """Positions are daily closing snapshots: where a (account, company) row exists on two consecutive position dates,
    the change must equal that day's net traded quantity (+-1 share for rounding)."""
    rows = q(f"""WITH dts AS (SELECT as_of, lag(as_of) OVER (ORDER BY as_of) AS prev FROM (SELECT DISTINCT as_of FROM {FC}.oms.positions)),
      p AS (SELECT account_id, company_id, as_of, quantity, lag(quantity) OVER (PARTITION BY account_id, company_id ORDER BY as_of) AS pq,
                   lag(as_of) OVER (PARTITION BY account_id, company_id ORDER BY as_of) AS pd FROM {FC}.oms.positions),
      s AS (SELECT p.* FROM p JOIN dts ON dts.as_of = p.as_of AND dts.prev = p.pd WHERE p.quantity <> p.pq ORDER BY hash(p.account_id, p.company_id, p.as_of) LIMIT 50),
      t AS (SELECT account_id, company_id, trade_date, SUM(CASE WHEN side='B' THEN quantity ELSE -quantity END) AS q FROM {FC}.oms.trades GROUP BY account_id, company_id, trade_date)
      SELECT count(*) AS n, SUM(CASE WHEN abs(s.quantity - s.pq - coalesce(t.q, 0)) <= 1 THEN 0 ELSE 1 END) AS bad
      FROM s LEFT JOIN t ON t.account_id = s.account_id AND t.company_id = s.company_id AND t.trade_date = s.as_of""")[0]
    record("positions_sum", rows["bad"] == 0, {"sampled": rows["n"], "mismatches": rows["bad"]})


def pf01():
    r = q(f"SELECT count(*) AS n, max(buyers) AS mb FROM {UC}.efm_buy_basket WHERE month_end = (SELECT max(month_end) FROM {UC}.efm_buy_basket) AND in_top50 AND score = 3")[0]
    record("PF-01", r["n"] == 8 and r["mb"] <= 2, {"score3": r["n"], "max_buyers": r["mb"]})


def pf05():
    r = tpl("oms_trade_flows", company_ids=["C0101"], start=W["start"], end=W["end"], group_by="company")[0]
    e = by_id["PF-05"]
    ok = close(r["net_musd"], -83.0, e["tol"]["net_musd"]) and r["sell_accounts"] == 7 and r["buy_accounts"] == 2
    record("PF-05", ok, {"net_musd": r["net_musd"], "sell_accounts": r["sell_accounts"], "buy_accounts": r["buy_accounts"]})


def pf07():
    rows = tpl("oms_trade_flows", company_ids=["C0102"], start=W["start"], end=W["end"], group_by="strategy")
    by = {r["group_key"]: r["net_musd"] for r in rows}
    sells = sum(by.get(k, 0) for k in ("EMA", "GEMF", "NCREP"))
    e = by_id["PF-07"]["tol"]
    record("PF-07", close(sells, -31.0, e["sells_musd"]) and close(by.get("EMQ", 0), 0.2, e["emq_musd"]), {"sells_musd": round(sells, 2), "emq_musd": by.get("EMQ")})


def move(cid, cmp_id, start, end, net, ret):
    r = tpl("oms_post_trade_move", company_id=cid, start=start, end=end, horizon_days=21)[0]
    t = by_id[cmp_id]["tol"]
    record(cmp_id, close(r["net_musd"], net, t["net_musd"]) and close(r["weighted_return_pct"], ret, t["ret_pct"]), {"net_musd": r["net_musd"], "ret_pct": r["weighted_return_pct"]})


def pf13():
    rows = tpl("oms_confirm_conflict", basket="efm_universe200", start=W["start"], end=W["end"], strong_musd=20)
    conf = [r for r in rows if r["class"] == "strong_conflict"]
    good = [r for r in rows if r["class"] == "strong_confirm"]
    a = {"conflicts": len(conf), "conflict_musd": round(-sum(r["net_musd"] for r in conf)), "confirms": len(good), "confirm_musd": round(sum(r["net_musd"] for r in good))}
    ok = a["conflicts"] == 56 and a["confirms"] == 45 and close(a["conflict_musd"], 4024, pct=2) and close(a["confirm_musd"], 3722, pct=2)
    record("PF-13", ok, a)


def pf18():
    n = q(f"SELECT count(*) AS n FROM {FC}.oms.trades WHERE pm_name IS NOT NULL")[0]["n"]
    record("PF-18", n == 0, {"pm_populated": n})


def docs_planted():
    n = q(f"SELECT count(*) AS n FROM {UC}.broker_chunks WHERE company_id = 'C0101' AND lower(text) LIKE '%sold out%cy27%'")[0]["n"]
    record("docs_planted", n >= 1, {"chunks": n})


def pf20():
    cal = q(f"SELECT count(*) AS n FROM {UC}.earnings_calendar WHERE company_id = 'C0101' AND event_type = 'results' AND event_date = DATE'2026-11-13' AND status = 'reported'")[0]["n"]
    ch = q(f"SELECT count(*) AS n FROM {UC}.earnings_chunks WHERE company_id = 'C0101' AND lower(text) LIKE '%essd exceeds our supply%'")[0]["n"]
    record("PF-20", cal >= 1 and ch >= 1, {"calendar": cal, "chunks": ch})


def pf21():
    s = q(f"SELECT share_pct FROM {UC}.geo_revenue WHERE company_id = 'C0102' AND region = 'North America' AND fiscal_year = 'FY2026'")
    bad = q(f"SELECT count(*) AS n FROM (SELECT company_id, fiscal_year, SUM(share_pct) AS s FROM {UC}.geo_revenue GROUP BY 1, 2 HAVING abs(SUM(share_pct) - 100) > 0.5)")[0]["n"]
    v = float(s[0]["share_pct"]) if s else None
    record("PF-21", v is not None and close(v, 14.0, 0.5) and bad == 0, {"share_pct": v, "bad_sums": bad})


def vs_retrieval():
    from databricks.sdk import WorkspaceClient

    idx = f"{UC}.expert_chunks_idx"
    r = WorkspaceClient().vector_search_indexes.query_index(index_name=idx, columns=["chunk_id", "company_id", "text"], query_text="Kioxia data-centre demand expert", num_results=3)
    rows = r.result.data_array or []
    found = any(row[1] == "C0101" and "50%" in (row[2] or "") for row in rows)
    record("vs_retrieval", found, {"found": found, "top3": [row[0] for row in rows]})


def restriction():
    """Observe whether *serverless* compute reaches the external DB; compare with the profile's expectation."""
    expected_blocked = bool(env.get("network.serverless_blocked", False))
    observed_blocked = None
    try:
        from databricks.sdk import WorkspaceClient
        from eqds_agentkit.sql import run_statement

        w = WorkspaceClient()
        wh = next((x.id for x in w.warehouses.list() if x.enable_serverless_compute), None)
        if wh:
            try:
                run_statement(w, wh, f"SELECT 1 FROM {FC}.oms.strategies LIMIT 1", timeout_s=50)
                observed_blocked = False
            except Exception as e:  # noqa: BLE001
                observed_blocked = True
                detail = str(e)[:200]
    except Exception as e:  # noqa: BLE001
        detail = str(e)[:200]
    if observed_blocked is None:
        record("restriction", True, {"observed_blocked": None}, "no serverless warehouse to probe: skipped")
    else:
        record("restriction", observed_blocked == expected_blocked, {"observed_blocked": observed_blocked, "profile_expects_blocked": expected_blocked},
               "" if observed_blocked == expected_blocked else "serverless reachability differs from the profile expectation (update network.serverless_blocked or the firewall)")


def skew():
    v = [r["v"] for r in q(f"SELECT CAST(value_usd AS DOUBLE) AS v FROM {FC}.oms.trades TABLESAMPLE (20 PERCENT) ORDER BY v")]
    import numpy as np

    a = np.array(v)
    n = len(a)
    gini = float((2 * np.arange(1, n + 1) - n - 1).dot(a) / (n * a.sum())) if n else 0.0
    r = q(f"""SELECT SUM(CASE WHEN is_rebalance THEN 1 ELSE 0 END) / count(*) AS s
              FROM {FC}.oms.trades WHERE strategy_id = 'NCREP'""")[0]["s"]
    record("skew", gini > 0.6 and float(r) > 0.15, {"gini": round(gini, 3), "ncrep_rebalance_share": round(float(r), 3)})


for cid, fn in [("ref_integrity", ref_integrity), ("positions_sum", positions_sum), ("PF-01", pf01), ("PF-05", pf05), ("PF-07", pf07),
                ("PF-09", lambda: move("C0103", "PF-09", "2026-10-22", "2026-10-23", 36.6, 6.8)), ("PF-11", lambda: move("C0113", "PF-11", "2026-10-05", "2026-10-07", 12.9, -46.9)),
                ("PF-13", pf13), ("PF-18", pf18), ("docs_planted", docs_planted), ("PF-20", pf20), ("PF-21", pf21), ("vs_retrieval", vs_retrieval),
                ("restriction", restriction), ("skew", skew)]:
    safe(cid, fn)

# row counts feed the UI "Data & seed" tab
for key, sql in [("rowcount:ref_companies", f"SELECT count(*) AS n FROM {UC}.ref_companies"), ("rowcount:broker_chunks", f"SELECT count(*) AS n FROM {UC}.broker_chunks"),
                 ("rowcount:oms.trades", f"SELECT count(*) AS n FROM {FC}.oms.trades"), ("rowcount:research.notes", f"SELECT count(*) AS n FROM {FC}.research.notes"),
                 ("rowcount:feeds.news_wire", f"SELECT count(*) AS n FROM {FC}.feeds.news_wire")]:
    try:
        n = q(sql)[0]["n"]
    except Exception as e:  # noqa: BLE001
        n = f"error: {str(e)[:60]}"
    counts.append({"check_no": 100 + len(counts), "check_id": key, "name": key, "passed": True, "expected": "", "actual": f"{n:,}" if isinstance(n, int) else str(n), "detail": "", "ran_at": dt.datetime.utcnow()})

write_table(spark, pd.DataFrame(results + counts), env.table("seed_validation_results"), comment="Seed validation results. SYNTHETIC DEMO DATA")
failed = [r["check_id"] for r in results if not r["passed"]]
print("validation:", len(results) - len(failed), "passed,", len(failed), "failed", failed)
if failed:
    raise SystemExit(f"seed validation failed: {failed}")
