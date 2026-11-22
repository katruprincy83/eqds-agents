#!/usr/bin/env python3
"""Show the latest run of bundle jobs: scripts/runstat.py [job_key ...] [--target demo]"""
import argparse, os, sys
from databricks.sdk import WorkspaceClient
ap = argparse.ArgumentParser(); ap.add_argument("jobs", nargs="*"); ap.add_argument("--target", default="demo"); ap.add_argument("--out", action="store_true")
a = ap.parse_args()
w = WorkspaceClient(profile=os.environ.get("DATABRICKS_CONFIG_PROFILE"))
for j in w.jobs.list():
    n = j.settings.name
    key = n.replace("-" + a.target, "").split("-", 1)[-1].replace("-", "_")
    if a.jobs and key not in a.jobs: continue
    runs = list(w.jobs.list_runs(job_id=j.job_id, limit=1))
    if not runs: continue
    r = runs[0]; st = r.state
    print(f"{key:18} {st.life_cycle_state.value if st.life_cycle_state else ''} {st.result_state.value if st.result_state else ''} run={r.run_id} {(st.state_message or '')[:160]}")
    if a.out and r.tasks:
        for t in r.tasks:
            try:
                o = w.jobs.get_run_output(t.run_id)
                if o.error: print("  ERROR:", o.error[:1500]); print((o.error_trace or "")[-2500:])
                if o.logs: print(o.logs[-3000:])
                if o.notebook_output: print(o.notebook_output.result)
            except Exception as e: print("  (no output)", str(e)[:100])
