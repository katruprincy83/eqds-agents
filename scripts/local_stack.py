"""Developer helper: run specialist agents in ONE local process (`/sp/<key>/invocations`) and register them in the
Lakebase registry with localhost URLs, so the moderator / UI can be exercised locally against the live workspace.
Agents come from their own project folders (default: the sibling ../eqds-agent-* checkouts), built with `make build` there.
usage: python scripts/local_stack.py [--port 8830] [--agents ../eqds-agent-news,../eqds-agent-ours]
(register the agents again on the UI registry page afterwards to restore their app URLs)"""
import argparse, glob, os, sys, yaml
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (root, os.path.join(root, "src"), os.path.join(root, "packages", "eqds_agentkit"), os.path.join(root, "packages", "eqds_llm")):
    sys.path.insert(0, p)
os.environ.setdefault("EQDS_ROOT", root)
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from databricks.sdk import WorkspaceClient
from eqds_agentkit import auth, contract
from eqds_agentkit.envcfg import Env
from eqds_agentkit.memory import Store
from agents.specialist_base.agent import SpecialistAgent
from apps.common import pool

ap = argparse.ArgumentParser(); ap.add_argument("--port", type=int, default=8830); ap.add_argument("--agents", default="")
ap.add_argument("--target", default="demo"); a = ap.parse_args()
env = Env.from_target(a.target, root)
env.raw["flags"]["use_managed_mcp"] = bool(env.get("flags.use_managed_mcp", False))
w = WorkspaceClient(); store = Store(pool())
projects = a.agents.split(",") if a.agents else sorted(glob.glob(os.path.join(os.path.dirname(root), "eqds-agent-*")))
app = FastAPI(); agents = {}
for proj in projects:
    built = glob.glob(os.path.join(proj, "app", "agents", "specialists", "*", "specialist.yaml"))  # ./app from the project's `make build`
    if not built:
        print(f"skipped {proj}: not built (run `make build` there)"); continue
    f = built[0]
    cfg = yaml.safe_load(open(f))
    ag = SpecialistAgent(f, env, w, store, root=os.path.join(proj, "app")); agents[cfg["key"]] = ag
    def make(ag):
        async def inv(req: Request):
            return JSONResponse(contract.response(await ag.invoke(await req.json(), auth.user_from_headers(req.headers))))
        return inv
    app.post(f"/sp/{cfg['key']}/invocations")(make(ag))
    from eqds_agentkit.registry import upsert_specialist
    with pool().connection() as c:
        upsert_specialist(c, cfg, f"http://localhost:{a.port}/sp/{cfg['key']}", env.target, f"local-{cfg['key']}")
print("registered", list(agents)); uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="warning")
