"""job_docs_index (serverless): (re)sync the Vector Search Delta-Sync indexes after chunk tables change (design §7.2, M2).
Parsing/chunking happens in job_seed_docs (synthetic profile) or the customer ingestion pipeline; this job only syncs and waits."""
from __future__ import annotations

import os
import sys
import time

try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
except NameError:
    pass
import yaml
from _common import setup

env, args = setup()
from databricks.sdk import WorkspaceClient  # noqa: E402
from databricks.sdk.errors import NotFound  # noqa: E402

w = WorkspaceClient()
cfg = yaml.safe_load(open(os.path.join(os.environ["EQDS_ROOT"], "bootstrap", "vector_search.yaml")).read().replace("${name_prefix}", env.prefix).replace("${suffix}", env.suffix).replace("${vector_search_endpoint}", env.vs_endpoint))
for ix in cfg["indexes"]:
    full = f"{env.uc}.{ix['name']}"
    try:
        w.vector_search_indexes.sync_index(full)
        print("sync started", full, flush=True)
    except NotFound:
        print("skip (index missing; run bootstrap)", full)
    except Exception as e:  # noqa: BLE001
        print("sync skipped", full, str(e)[:120])
deadline = time.time() + 1500
for ix in cfg["indexes"]:
    full = f"{env.uc}.{ix['name']}"
    while time.time() < deadline:
        try:
            st = w.vector_search_indexes.get_index(full).status
        except NotFound:
            break
        if st.ready and not (st.message or "").lower().startswith("sync"):
            print("ready", full)
            break
        time.sleep(15)
