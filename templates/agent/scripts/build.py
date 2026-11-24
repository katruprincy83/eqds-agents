#!/usr/bin/env python3
"""Assemble the deployable app source in ./app from the EQDS specialist runtime plus THIS agent's own files.

    python scripts/build.py [--platform runtime] [--target demo]

The runtime (LangGraph ReAct loop, tools, contract, tracing, memory) is the snapshot in ./runtime, written by the platform's
`new_agent.py --scaffold-update` (platform_lock.yaml names its commit). Everything agent-specific lives in ./agent: specialist.yaml,
skills/ and migrations/. The dependency pins come from the snapshot's build script, so the two never drift.
"""
import argparse, importlib.util, pathlib, shutil, sys

import yaml

HERE = pathlib.Path(__file__).resolve().parents[1]
ap = argparse.ArgumentParser()
ap.add_argument("--platform", default=str(HERE / "runtime"), help="the runtime snapshot (default ./runtime)")
ap.add_argument("--target", default="demo")
a = ap.parse_args()
P = pathlib.Path(a.platform).resolve()
if not (P / "agents/specialist_base/app.py").exists():
    sys.exit(f"{P} is not a runtime snapshot (run the platform's new_agent.py --scaffold-update)")

spec = importlib.util.spec_from_file_location("platform_build_apps", P / "scripts/build_apps.py")
pb = importlib.util.module_from_spec(spec)
sys.path.insert(0, str(P / "packages/eqds_agentkit"))
spec.loader.exec_module(pb)

cfg = yaml.safe_load((HERE / "agent/specialist.yaml").read_text())
key = cfg["key"]
out = HERE / "app"
if out.exists():
    shutil.rmtree(out)
out.mkdir()
ign = pb.IGNORE
shutil.copytree(P / "packages/eqds_agentkit/eqds_agentkit", out / "eqds_agentkit", ignore=ign)
shutil.copytree(P / "packages/eqds_llm/eqds_llm", out / "eqds_llm", ignore=ign)
(out / "apps").mkdir()
shutil.copy2(P / "apps/__init__.py", out / "apps/__init__.py")
shutil.copy2(P / "apps/common.py", out / "apps/common.py")
(out / "agents").mkdir()
shutil.copy2(P / "agents/__init__.py", out / "agents/__init__.py")
shutil.copytree(P / "agents/specialist_base", out / "agents/specialist_base", ignore=ign)
shutil.copytree(P / "prompts", out / "prompts", ignore=ign)  # shared prompt templates of the runtime
# this agent's own files
(out / f"agents/specialists/{key}").mkdir(parents=True)
shutil.copy2(HERE / "agent/specialist.yaml", out / f"agents/specialists/{key}/specialist.yaml")
shutil.copytree(HERE / "agent/skills", out / "skills")
if (HERE / "agent/migrations").exists():  # this agent's own Lakebase migrations, applied to agent_<key> once each on startup
    shutil.copytree(HERE / "agent/migrations", out / "migrations")
# environment profile and model tiers of the platform this agent plugs into
(out / "config/env").mkdir(parents=True)
shutil.copy2(P / f"config/env/{a.target}.yaml", out / f"config/env/{a.target}.yaml")
models = yaml.safe_load((P / f"config/env/{a.target}.yaml").read_text()).get("models_config", f"config/models.{a.target}.yaml")
(out / models).parent.mkdir(parents=True, exist_ok=True)
shutil.copy2(P / models, out / models)
(out / "requirements.txt").write_text("\n".join(f"{p}=={pb.PINS[p]}" for p in pb.REQ["specialist"]) + "\n")
(out / "app.yaml").write_text(yaml.safe_dump({"command": ["python", "-m", "uvicorn", "agents.specialist_base.app:app", "--host", "0.0.0.0", "--port", "8000"]}, sort_keys=False))
print(f"built {out} for agent '{key}' (runtime from {P})")
