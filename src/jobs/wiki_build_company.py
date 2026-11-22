"""wiki_refresh task 2 (for-each): build the stale pages of one company by asking the moderator once per page (caller='wiki'),
write a new `wiki_pages` version and update `wiki_state` (design §10)."""
from __future__ import annotations

import datetime as dt
import json
import os
import sys

try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
except NameError:
    pass
import httpx
from _common import get_spark, setup

env, args = setup([("--item", {"default": "{}"}), ("--rounds", {"default": "2"})])
spark = get_spark()
from databricks.sdk import WorkspaceClient  # noqa: E402

from wiki.prompts import PAGE_QUESTIONS  # noqa: E402

item = json.loads(args.item)
cid, mode, pages = item["company_id"], item.get("mode", "refresh"), item.get("stale_pages") or list(PAGE_QUESTIONS)
uc = env.uc
w = WorkspaceClient()
name = spark.sql(f"SELECT name FROM {uc}.ref_companies WHERE company_id = '{cid}'").collect()[0]["name"]
url = w.apps.get(env.app_name("moderator")).url.rstrip("/")
from eqds_agentkit.auth import job_caller_headers  # noqa: E402

hdr = {**job_caller_headers(w, env), "x-eqds-user": "wiki"}
print(f"building {name} ({cid}) mode={mode} pages={pages}", flush=True)
now = dt.datetime.utcnow()
prev = {r["page"]: r["version"] for r in spark.sql(f"SELECT page, max(version) AS version FROM {uc}.wiki_pages WHERE company_id = '{cid}' GROUP BY page").collect()}
rows = []
for page in pages:
    q = PAGE_QUESTIONS[page].format(name=name)
    body = {"input": [{"role": "user", "content": q}], "custom_inputs": {"rounds": int(args.rounds), "company_id": cid, "caller": "wiki", "user_id": "wiki"}}
    r = httpx.post(f"{url}/invocations", json=body, headers=hdr, timeout=900)
    r.raise_for_status()
    res = r.json()
    ans = (res.get("custom_outputs") or res).get("answer") or {}
    text = ans.get("text") or "\n\n".join(ans.get("paragraphs", []))
    srcs = (res.get("custom_outputs") or res).get("sources", {})
    md = f"# {name}: {page}\n\n{text}\n\n_Built {now:%Y-%m-%d} by the research agents. SYNTHETIC DEMO DATA._\n"
    rows.append((cid, page, int(prev.get(page, 0)) + 1, md, json.dumps(list(srcs)[:60]), now))
    print(f"  {page}: {len(text)} chars", flush=True)
df = spark.createDataFrame(rows, "company_id string, page string, version int, content_md string, sources string, built_at timestamp")
df.write.mode("append").saveAsTable(f"{uc}.wiki_pages")
full = f", last_full_build = current_timestamp()" if mode == "full" else ""
exists = spark.sql(f"SELECT count(*) AS n FROM {uc}.wiki_state WHERE company_id = '{cid}'").collect()[0]["n"]
if exists:
    spark.sql(f"UPDATE {uc}.wiki_state SET last_refresh = current_timestamp(), pending_triggers = '[]', note = 'built {now:%Y-%m-%d}'{full} WHERE company_id = '{cid}'")
else:
    spark.sql(f"INSERT INTO {uc}.wiki_state VALUES ('{cid}', current_timestamp(), current_timestamp(), '[]', 'built {now:%Y-%m-%d}')")
print("done", cid)
