#!/usr/bin/env python3
"""Create a standalone specialist agent project (own repo, own bundle, own app) that plugs into this platform.

    python scripts/new_agent.py <key> [--out ../eqds-agent-<key>] [--target demo] [--display "Name"] [--tag XY]
    python scripts/new_agent.py <key> --from-spec path/specialist.yaml --skills path/to/skills/<key>   # bring an existing agent

Without --from-spec it writes a starter spec and skill to edit. Platform values (workspace host, SQL warehouse, MLflow experiment,
and the registrar / UI / moderator service principals) are looked up in the target workspace with your CLI login, so the
generated bundle deploys as is. The project is not registered: its owner does that on the orchestrator's registry page.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import shutil
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "eqds_agentkit"))
from eqds_agentkit.envcfg import Env  # noqa: E402

TPL = ROOT / "templates" / "agent"


def platform_values(env: Env) -> dict[str, str]:
    from databricks.sdk import WorkspaceClient

    w = WorkspaceClient()
    reg = (env.get("service_principals") or {}).get("registrar") or ""
    if not reg:
        sys.exit("service_principals.registrar is not set in the environment profile: run `make registrar` first")
    wh = next((x.id for x in w.warehouses.list() if x.name == env.app_name("wh")), None)
    if not wh:
        sys.exit(f"SQL warehouse {env.app_name('wh')} not found (deploy the platform first)")
    return {"HOST": w.config.host.rstrip("/"), "WAREHOUSE_ID": wh,
            "EXPERIMENT_ID": w.experiments.get_by_name(env.experiment_path).experiment.experiment_id,
            "REGISTRAR_SP": reg, "UI_SP": w.apps.get(env.app_name("ui")).service_principal_client_id,
            "MODERATOR_SP": w.apps.get(env.app_name("moderator")).service_principal_client_id}


def fill(text: str, values: dict[str, str]) -> str:
    for k, v in values.items():
        text = text.replace("{{" + k + "}}", str(v))
    left = re.findall(r"\{\{[A-Z_]+\}\}", text)
    if left:
        raise SystemExit(f"template placeholders not filled: {sorted(set(left))}")
    return text


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("key")
    ap.add_argument("--out", default="")
    ap.add_argument("--target", default="demo")
    ap.add_argument("--display", default="")
    ap.add_argument("--tag", default="")
    ap.add_argument("--owner", default="Equities research")
    ap.add_argument("--from-spec", default="")
    ap.add_argument("--skills", default="", help="folder holding <version>/SKILL.md (with --from-spec)")
    ap.add_argument("--force", action="store_true", help="overwrite generated files in an existing project")
    a = ap.parse_args()

    key = a.key
    if not re.fullmatch(r"[a-z][a-z0-9]{1,21}", key):  # app names allow lowercase letters, digits and hyphens only
        sys.exit("key: lowercase letters and digits, 2-22 characters")
    env = Env.from_target(a.target, str(ROOT))
    app_name = env.app_name(f"sp-{key}")
    if len(app_name) > 30:
        sys.exit(f"app name {app_name} is longer than 30 characters")
    out = pathlib.Path(a.out or ROOT.parent / f"eqds-agent-{key}").resolve()
    if out.exists() and any(out.iterdir()) and not a.force:
        sys.exit(f"{out} exists and is not empty (use --force to regenerate the scaffolding)")

    existing = out / "agent" / "specialist.yaml"
    if a.from_spec or existing.exists():  # bring an existing agent, or regenerate the scaffolding of a project around its own spec
        spec = yaml.safe_load(pathlib.Path(a.from_spec or existing).read_text())
        if spec.get("key") != key:
            sys.exit(f"--from-spec has key '{spec.get('key')}', expected '{key}'")
    else:
        spec = {"key": key, "display_name": a.display or key.capitalize(), "tag_prefix": (a.tag or key[:2]).upper(), "version": "0.1.0"}
    values = {"KEY": key, "DISPLAY": spec["display_name"], "TAG": spec["tag_prefix"], "OWNER": a.owner, "APP_NAME": app_name,
              "TARGET": env.target, "VERSION": str(spec.get("version", "0.1.0")), **platform_values(env)}

    (out / "scripts").mkdir(parents=True, exist_ok=True)
    (out / "databricks.yml").write_text(fill((TPL / "databricks.yml").read_text(), values))
    (out / "Makefile").write_text(fill((TPL / "Makefile").read_text(), values))
    (out / "README.md").write_text(fill((TPL / "README.md").read_text(), values))
    shutil.copy2(TPL / "gitignore", out / ".gitignore")
    for f in ("build.py", "smoke.py"):
        shutil.copy2(TPL / "scripts" / f, out / "scripts" / f)

    agent = out / "agent"
    skills = agent / "skills" / key
    if a.from_spec and pathlib.Path(a.from_spec).resolve() != (agent / "specialist.yaml").resolve():
        agent.mkdir(exist_ok=True)
        shutil.copy2(a.from_spec, agent / "specialist.yaml")
        if a.skills and pathlib.Path(a.skills).resolve() != skills.resolve():
            if skills.exists():
                shutil.rmtree(skills)
            shutil.copytree(a.skills, skills)
    elif not (agent / "specialist.yaml").exists():
        (skills / "0.1.0").mkdir(parents=True, exist_ok=True)
        (agent / "specialist.yaml").write_text(fill((TPL / "specialist.yaml").read_text(), values))
        (skills / "0.1.0" / "SKILL.md").write_text(fill((TPL / "SKILL.md").read_text(), values))
    print(f"wrote {out} (app {app_name}, platform {env.target})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
