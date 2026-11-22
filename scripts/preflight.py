#!/usr/bin/env python3
"""`make preflight TARGET=<t>`: can this workspace host the platform? Exit 1 on any FAIL."""
import argparse, os, pathlib, sys
ROOT = pathlib.Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "src", ROOT / "packages/eqds_agentkit", ROOT / "packages/eqds_llm"):
    sys.path.insert(0, str(p))
from eqds_agentkit.envcfg import Env
from databricks.sdk import WorkspaceClient
from bootstrap import preflight

ap = argparse.ArgumentParser(); ap.add_argument("--target", default=os.environ.get("TARGET", "demo")); ap.add_argument("--profile", default=os.environ.get("DATABRICKS_CONFIG_PROFILE"))
a = ap.parse_args()
env = Env.from_target(a.target, ROOT)
w = WorkspaceClient(profile=a.profile) if a.profile else WorkspaceClient()
sys.exit(preflight.report(preflight.run(env, w, str(ROOT))))
