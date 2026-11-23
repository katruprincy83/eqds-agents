#!/usr/bin/env python3
"""`make smoke TARGET=<t>`: the gate between `dev` and production (docs/cicd.md). Read-only apart from running the validation job.

Checks, in order (exit code 1 on the first group that fails):
  1. every platform app of the target is ACTIVE with an ACTIVE compute
  2. the apps answer /healthz (UI, moderator) with the caller's OAuth token
  3. the moderator lists its specialists (/specialists); an empty registry is reported, not failed (agents are registered by hand)
  4. `--jobs`: the `validate_seed` job succeeds (seed self-checks; needs the seed to have run in this environment)
  5. `--ask "question"`: one moderator run reaches DONE (needs at least one registered specialist)
"""
from __future__ import annotations

import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "packages", "eqds_agentkit"))

import httpx  # noqa: E402
from databricks.sdk import WorkspaceClient  # noqa: E402

from eqds_agentkit.envcfg import Env  # noqa: E402

fails: list[str] = []


def check(ok: bool, msg: str) -> bool:
    print(f"  [{'ok' if ok else 'FAIL'}] {msg}", flush=True)
    if not ok:
        fails.append(msg)
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default=os.environ.get("TARGET", "demo"))
    ap.add_argument("--profile", default=None)
    ap.add_argument("--jobs", action="store_true", help="also run the validate_seed job")
    ap.add_argument("--ask", default="", help="also ask the moderator this question and wait for DONE")
    a = ap.parse_args()
    env = Env.from_target(a.target)
    w = WorkspaceClient(profile=a.profile) if a.profile else WorkspaceClient(host=env.host)
    print(f"smoke target={env.target} host={env.host}", flush=True)

    urls: dict[str, str] = {}
    for comp in ("ui", "moderator", "bridge-mcp"):
        name = env.app_name(comp)
        try:
            app = w.apps.get(name)
        except Exception as e:  # noqa: BLE001
            check(False, f"app {name}: {str(e)[:100]}")
            continue
        app_state = str(app.app_status.state) if app.app_status else "?"
        compute = str(app.compute_status.state) if app.compute_status else "?"
        check("RUNNING" in app_state or "ACTIVE" in compute, f"app {name}: app={app_state} compute={compute}")
        urls[comp] = (app.url or "").rstrip("/")
    if fails:
        return 1

    tok = w.config.authenticate().get("Authorization", "")
    h = {"Authorization": tok}
    for comp in ("ui", "moderator"):
        try:
            r = httpx.get(f"{urls[comp]}/healthz", headers=h, timeout=30)
            check(r.status_code == 200, f"{env.app_name(comp)} /healthz -> {r.status_code}")
        except Exception as e:  # noqa: BLE001
            check(False, f"{env.app_name(comp)} /healthz: {str(e)[:100]}")
    try:
        r = httpx.get(f"{urls['moderator']}/specialists", headers=h, timeout=30)
        keys = [s.get("key") for s in (r.json() if r.status_code == 200 else [])] if r.headers.get("content-type", "").startswith("application/json") else []
        check(r.status_code == 200, f"moderator /specialists -> {r.status_code}: {keys or 'registry empty (register agents on the UI registry page)'}")
    except Exception as e:  # noqa: BLE001
        check(False, f"moderator /specialists: {str(e)[:100]}")

    if a.jobs:
        job = env.job_name("validate-seed")
        found = next(iter(w.jobs.list(name=job)), None)
        if not check(found is not None, f"job {job} exists"):
            return 1
        run = w.jobs.run_now(job_id=found.job_id).result()
        check(str(run.state.result_state).endswith("SUCCESS"), f"job {job}: {run.state.result_state}")

    if a.ask:
        r = httpx.post(f"{urls['moderator']}/runs", json={"question": a.ask, "rounds": 1}, headers=h, timeout=60)
        if check(r.status_code < 300, f"POST /runs -> {r.status_code}"):
            rid = r.json()["run_id"]
            t0, status = time.time(), ""
            while time.time() - t0 < 600:
                status = httpx.get(f"{urls['moderator']}/runs/{rid}", headers=h, timeout=30).json().get("status", "")
                if status in ("DONE", "FAILED", "CANCELLED"):
                    break
                time.sleep(5)
            check(status == "DONE", f"moderator run {rid}: {status} after {time.time() - t0:.0f}s")

    print("smoke FAILED" if fails else "smoke ok", flush=True)
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
