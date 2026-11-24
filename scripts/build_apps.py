#!/usr/bin/env python3
"""Assemble deployable Databricks App source directories under build/apps/<key>/ (design §12.2, E4).

Each app gets only what it needs: the vendored shared packages (eqds_agentkit, eqds_llm), apps/common.py, its own module,
the agent code, prompts, the environment profile for the target and pinned requirements. `databricks bundle deploy`
uploads these directories (resources/apps/*.yml `source_code_path`). Re-runnable and deterministic.

    python scripts/build_apps.py --target demo [--only ui,moderator]
"""
from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "eqds_agentkit"))
from eqds_agentkit.envcfg import Env  # noqa: E402

# exact pins taken from the validated development environment (reproducible deploys)
PINS = {
    "fastapi": "0.162.7", "uvicorn": "0.54.0", "httpx": "0.28.1", "pydantic": "2.14.3", "pyyaml": "6.0.3", "sse-starlette": "3.7.0",
    "psycopg[binary,pool]": "3.3.9", "databricks-sdk": "0.169.0", "openai": "2.54.0", "mlflow": "3.18.0",
    "langgraph": "1.2.22", "langgraph-checkpoint-postgres": "3.2.1", "langchain-core": "1.8.0", "langchain-mcp-adapters": "0.3.2",
    "databricks-langchain": "0.23.0", "mcp": "1.30.0",
}
BASE = ["fastapi", "uvicorn", "httpx", "pydantic", "pyyaml", "psycopg[binary,pool]", "databricks-sdk", "mlflow"]
REQ = {
    "ui": BASE + ["openai", "langchain-core", "langchain-mcp-adapters"],
    "moderator": BASE + ["sse-starlette", "openai", "langchain-core", "langgraph", "langgraph-checkpoint-postgres"],
    "specialist": BASE + ["openai", "langchain-core", "langchain-mcp-adapters", "langgraph", "databricks-langchain"],  # used by templates/agent/scripts/build.py
}
CMD = {  # uvicorn on the platform port (Apps default 8000)
    "ui": "apps.ui.server:app", "moderator": "agents.moderator.app:app", "specialist": "agents.specialist_base.app:app",
}
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", "tests")


def requirements_text(kind: str) -> str:
    """The app's requirements.txt: the fully pinned lock (requirements/<kind>.lock, made by scripts/lock_requirements.py) when it
    exists, so the Apps builder does one plain install instead of solving transitive versions (which can backtrack for minutes and
    time the deploy out). Falls back to the top-level pins only."""
    lock = ROOT / "requirements" / f"{kind}.lock"
    if lock.exists():
        return lock.read_text()
    return "\n".join(f"{p}=={PINS[p]}" for p in REQ[kind]) + "\n"


def copy(src: str, dst: pathlib.Path) -> None:
    s = ROOT / src
    if s.is_dir():
        shutil.copytree(s, dst / src if "/" in src else dst / s.name, ignore=IGNORE, dirs_exist_ok=True)
    else:
        (dst / src).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(s, dst / src)


def build(app: str, kind: str, out: pathlib.Path, env: Env, models_cfg: str) -> None:
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    # shared packages, vendored as top-level modules
    shutil.copytree(ROOT / "packages/eqds_agentkit/eqds_agentkit", out / "eqds_agentkit", ignore=IGNORE)
    shutil.copytree(ROOT / "packages/eqds_llm/eqds_llm", out / "eqds_llm", ignore=IGNORE)
    (out / "apps").mkdir()
    shutil.copy2(ROOT / "apps/__init__.py", out / "apps/__init__.py")
    shutil.copy2(ROOT / "apps/common.py", out / "apps/common.py")
    if kind == "ui":
        shutil.copytree(ROOT / "apps/ui", out / "apps/ui", ignore=IGNORE)
    else:
        (out / "agents").mkdir()
        shutil.copy2(ROOT / "agents/__init__.py", out / "agents/__init__.py")
        shutil.copytree(ROOT / "agents/moderator", out / "agents/moderator", ignore=IGNORE)
    if kind in ("moderator", "ui"):
        copy("prompts", out)
    (out / "config/env").mkdir(parents=True)
    shutil.copy2(ROOT / "config/env" / f"{env.target}.yaml", out / "config/env" / f"{env.target}.yaml")
    shutil.copy2(ROOT / models_cfg, out / models_cfg) if (ROOT / models_cfg).exists() else None
    if kind == "ui":
        (out / "eval").mkdir()
        if (ROOT / "eval/thresholds.yaml").exists():
            shutil.copy2(ROOT / "eval/thresholds.yaml", out / "eval/thresholds.yaml")
    (out / "requirements.txt").write_text(requirements_text(kind))
    (out / "app.yaml").write_text(yaml.safe_dump({"command": ["python", "-m", "uvicorn", CMD[kind], "--host", "0.0.0.0", "--port", "8000"]}, sort_keys=False))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default=os.environ.get("TARGET", "demo"))
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    env = Env.from_target(a.target)
    models_cfg = env.get("models_config", f"config/models.{env.target}.yaml")
    apps = {"ui": "ui", "moderator": "moderator"}  # specialist agents build in their own repos (templates/agent)
    only = set(filter(None, a.only.split(",")))
    for name, kind in apps.items():
        if only and name not in only:
            continue
        build(name, kind, ROOT / "build/apps" / name, env, models_cfg)
        print(f"built build/apps/{name} ({kind})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
