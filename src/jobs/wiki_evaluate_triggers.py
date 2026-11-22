"""wiki_refresh task 1: evaluate the trigger rules over UC data and publish the company list as a task value (design §10)."""
from __future__ import annotations

import datetime as dt
import json
import os
import sys

try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
except NameError:
    pass
from _common import get_spark, setup

env, args = setup([("--only_company", {"default": ""}), ("--mode", {"default": ""}), ("--today", {"default": ""})])
spark = get_spark()
from wiki.triggers import ALL_PAGES, Evidence, evaluate  # noqa: E402

uc = env.uc
today = dt.date.fromisoformat(args.today) if args.today else dt.date.fromisoformat(os.environ.get("DEMO_AS_OF") or env.get("demo_as_of", "") or "2026-11-20")


def rows(sql):
    return [r.asDict() for r in spark.sql(sql).collect()]


state = {r["company_id"]: r for r in rows(f"SELECT * FROM {uc}.wiki_state")}
out = []
targets = [args.only_company] if args.only_company else list(state)
for cid in targets:
    if args.only_company and args.only_company not in state:  # requested wiki without any build yet: full build
        out.append({"company_id": cid, "mode": "full", "stale_pages": ALL_PAGES, "triggers": ["requested"]})
        continue
    s = state[cid]
    lb, lf = s["last_refresh"].date(), s["last_full_build"].date()
    ev = Evidence(
        cid, lb, lf,
        reported_events=[r["event_date"] for r in rows(f"SELECT event_date FROM {uc}.earnings_calendar WHERE company_id='{cid}' AND event_type='results' AND status='reported'")],
        scheduled_events=[r["event_date"] for r in rows(f"SELECT event_date FROM {uc}.earnings_calendar WHERE company_id='{cid}' AND event_type='results' AND status='scheduled'")],
        broker_dates=[r["d"] for r in rows(f"SELECT CAST(published_at AS DATE) AS d FROM {uc}.broker_reports WHERE company_id='{cid}' AND CAST(published_at AS DATE) > DATE'{lb}' AND CAST(published_at AS DATE) <= DATE'{today}'")],
        rating_target_changes=rows(f"""WITH r AS (SELECT rating, target_price, broker, published_at, lag(rating) OVER (PARTITION BY broker ORDER BY published_at) AS pr,
                                                 lag(target_price) OVER (PARTITION BY broker ORDER BY published_at) AS pt
                                          FROM {uc}.broker_reports WHERE company_id='{cid}')
                                       SELECT count(*) AS n FROM r WHERE CAST(published_at AS DATE) > DATE'{lb}' AND (rating <> pr OR abs(target_price/pt - 1) > 0.02)""")[0]["n"],
        other_new_items=rows(f"SELECT (SELECT count(*) FROM {uc}.expert_calls WHERE company_id='{cid}' AND call_date > DATE'{lb}') + (SELECT count(*) FROM {uc}.earnings_calls WHERE company_id='{cid}' AND call_date > DATE'{lb}') AS n")[0]["n"])
    d = evaluate(ev, today)
    if args.mode == "full":
        out.append({"company_id": cid, "mode": "full", "stale_pages": ALL_PAGES, "triggers": ["forced"]})
    elif d.mode != "none":
        out.append({"company_id": cid, "mode": d.mode, "stale_pages": d.stale_pages, "triggers": d.triggers})
    pend = json.dumps(d.triggers)
    spark.sql(f"UPDATE {uc}.wiki_state SET pending_triggers = '{pend}' WHERE company_id = '{cid}'")
print(f"{len(out)} companies to build", json.dumps(out)[:2000])
try:
    from pyspark.dbutils import DBUtils

    DBUtils(spark).jobs.taskValues.set(key="companies", value=out[:200])
except Exception as e:  # noqa: BLE001
    print("task value not set (not running in a job):", e)
