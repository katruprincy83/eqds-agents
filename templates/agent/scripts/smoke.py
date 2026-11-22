#!/usr/bin/env python3
"""Check the deployed agent on its own, before (or after) registering it with the orchestrator.

    python scripts/smoke.py [--invoke "How did C0102 trade after 2026-10-15?"]

/healthz and /info must work right after deploy. A real call (--invoke) needs the agent's data and model access, which
registration grants, so for a brand-new agent run it after registering. Uses your CLI login (DATABRICKS_CONFIG_PROFILE).
"""
import argparse
import json
import pathlib
import subprocess
import sys
import time

import httpx
import yaml

HERE = pathlib.Path(__file__).resolve().parents[1]
ap = argparse.ArgumentParser()
ap.add_argument("--invoke", default="", help="question for one real call")
a = ap.parse_args()

cfg = yaml.safe_load((HERE / "agent/specialist.yaml").read_text())
bundle = yaml.safe_load((HERE / "databricks.yml").read_text())
app_name = bundle["variables"]["app_name"]["default"]


def cli(*args):
    return json.loads(subprocess.check_output(["databricks", *args, "-o", "json"]))


url = cli("apps", "get", app_name)["url"].rstrip("/")
h = {"Authorization": f"Bearer {cli('auth', 'token')['access_token']}"}
ok = True
for path in ("/healthz", "/info"):
    r = httpx.get(url + path, headers=h, timeout=60, follow_redirects=False)
    print(f"GET {path} -> {r.status_code}")
    ok &= r.status_code == 200
info = r.json() if r.status_code == 200 else {}
if info:
    print(f"  key={info.get('key')} tag={info.get('tag_prefix')} tools={[t.get('type') for t in info.get('tool_config', [])]} access={info.get('access')}")
    ok &= info.get("key") == cfg["key"]
if a.invoke and ok:
    sys.path.insert(0, str(HERE / "app"))  # the runtime vendored by scripts/build.py
    from eqds_agentkit import contract

    body = contract.request(a.invoke, run_id=f"smoke-{int(time.time())}", round=1, key=cfg["key"], caller="smoke")
    r = httpx.post(url + "/invocations", json=body, headers=h, timeout=300, follow_redirects=False)
    print(f"POST /invocations -> {r.status_code}")
    if r.status_code == 200:
        out = contract.parse_response(r.json())
        print("  " + (out.text or "")[:600].replace("\n", " "))
        for c in out.claims[:5]:
            print(f"  claim: {c.text[:160]} {c.citations}")
    else:
        ok = False
        print("  (a new agent answers only after registration has granted its access)")
print("smoke OK" if ok else "smoke FAILED")
sys.exit(0 if ok else 1)
