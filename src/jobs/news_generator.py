"""job_news_generator (optional, demo only): inserts synthetic headlines into the external wire every minute so the live tail
(bridge tool `news_latest`) shows fresh items. Includes the scripted Kioxia item after `at_minutes` (planted_facts live_tail_scripted).
Stop the job after the demo. SYNTHETIC DEMO DATA."""
from __future__ import annotations

import datetime as dt
import os
import sys
import time

try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
except NameError:
    pass
import pandas as pd
from _common import get_spark, secret, setup

env, args = setup([("--minutes", {"default": "30"})])
if env.data_profile != "synthetic":
    print("data_profile != synthetic: refusing to write to the external database")
    sys.exit(0)
if env.get("shared_external"):
    print(f"shared_external is set for target {env.target}: refusing to insert into the shared external wire")
    sys.exit(0)
spark = get_spark()
from seed.common import load_planted  # noqa: E402
from seed.gen_azure.load import load_batches  # noqa: E402

jdbc = dict(url=env.azure_jdbc_url, user=secret(env.secret_scope, "azure-sql-seed-user"), password=secret(env.secret_scope, "azure-sql-seed-password"))
live = load_planted()["live_tail_scripted"]
base = int(spark.read.format("jdbc").options(**jdbc).option("query", "SELECT COALESCE(MAX(news_id), 0) AS m FROM feeds.news_wire").load().collect()[0]["m"]) + 1
TOP = ["C0101", "C0102", "C0103", "C0112", "C0113"]
HEAD = ["{n}: analysts discuss demand outlook", "{n}: management comments at industry conference", "{n}: shares move on sector rotation", "{n}: broker updates estimates"]
t0 = time.time()
i = 0
sent_live = False
while time.time() - t0 < float(args.minutes) * 60:
    now = dt.datetime.utcnow().replace(microsecond=0)
    rows = []
    if not sent_live and time.time() - t0 >= float(live["at_minutes"]) * 60:
        rows.append(dict(news_id=base + i, published_at=now, company_id=live["company"], headline=live["headline"], source=live["source"], body=live["headline"] + ". SYNTHETIC.", tags="live,scripted")); i += 1
        sent_live = True
    cid = TOP[i % len(TOP)]
    rows.append(dict(news_id=base + i, published_at=now, company_id=cid, headline=HEAD[i % len(HEAD)].format(n=cid), source="Synthetic Wire", body="Synthetic live item.", tags="live")); i += 1
    load_batches(spark, jdbc, "feeds.news_wire", pd.DataFrame(rows), batch_rows=10, workers=1)
    print("inserted", len(rows), flush=True)
    time.sleep(60)
