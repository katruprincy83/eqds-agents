#!/usr/bin/env python3
"""Create a standalone specialist agent project (own repo, own bundle, own app) that plugs into this platform.

    python scripts/new_agent.py <key> [--out ../eqds-agent-<key>] [--target demo] [--display "Name"] [--tag XY]
    python scripts/new_agent.py <key> --from-spec path/specialist.yaml --skills path/to/skills/<key>   # bring an existing agent

Without --from-spec it writes a starter spec and skill to edit. Platform values (workspace host, SQL warehouse, and the
registrar / UI / moderator service principals) are looked up in the target workspace with your CLI login, so the
generated bundle deploys as is. The project is not registered: its owner does that on the orchestrator's registry page.

The bundle gets one target per environment: the one given by --target (default demo = production, the variable defaults) and every
environment of --also (default dev). An environment whose platform is not deployed yet is skipped with a warning; run again with
--scaffold-update once it is. --scaffold-update refreshes the generated scaffolding of an existing project (databricks.yml, scripts, CI workflows, the
deploy/traces targets of the Makefile, the runtime snapshot runtime/ with platform_lock.yaml, README sections 1 to 6) and touches
nothing the owner wrote (agent/, the README intro).
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


def platform_values(env: Env, key: str) -> dict[str, str]:
    from databricks.sdk import WorkspaceClient

    w = WorkspaceClient()
    reg = (env.get("service_principals") or {}).get("registrar") or ""
    if not reg:
        sys.exit("service_principals.registrar is not set in the environment profile: run `make registrar` first")
    wh = next((x.id for x in w.warehouses.list() if x.name == env.app_name("wh")), None)
    if not wh:
        sys.exit(f"SQL warehouse {env.app_name('wh')} not found (deploy the platform first)")
    return {"HOST": w.config.host.rstrip("/"), "WAREHOUSE_ID": wh,
            "EXPERIMENT_PATH": env.agent_experiment_path(key),  # created by the agent's own bundle, nothing to look up
            "REGISTRAR_SP": reg, "UI_SP": w.apps.get(env.app_name("ui")).service_principal_client_id,
            "MODERATOR_SP": w.apps.get(env.app_name("moderator")).service_principal_client_id}


def render_targets(key: str, primary: Env, primary_vals: dict[str, str], others: list[tuple[Env, dict[str, str]]]) -> str:
    """The `targets:` block. The primary environment is the default target and uses the variable defaults; the others override every
    platform value (app name, experiment, warehouse, service principals) because the bundle owns the app's whole permission list."""
    out = ["targets:", f"  {primary.target}:", "    default: true", "    workspace:", f"      host: {primary_vals['HOST']}"]
    for env, v in others:
        out += [f"  {env.target}:", "    workspace:", f"      host: {v['HOST']}", "    variables:",
                f"      platform_target: {env.target}", f"      app_name: {env.app_name(f'sp-{key}')}", f"      warehouse_id: {v['WAREHOUSE_ID']}",
                f"      experiment_path: \"{v['EXPERIMENT_PATH']}\"", f"      registrar_sp: {v['REGISTRAR_SP']}", f"      ui_sp: {v['UI_SP']}",
                f"      moderator_sp: {v['MODERATOR_SP']}"]
    return "\n".join(out) + "\n"


def patch_makefile(text: str, key: str) -> str:
    """Bring the deploy and traces targets of an existing Makefile up to date without touching anything else the owner added."""
    text = text.replace("PLATFORM ?= ../eqds-agents", "PLATFORM ?= runtime", 1)  # the runtime snapshot replaces the platform checkout
    if "bind_traces.py" in text:
        return text
    old = f"deploy: build\n\tdatabricks bundle deploy -t $(TARGET)\n\tdatabricks bundle run -t $(TARGET) {key}\n"
    new = (f"deploy: build\n\tdatabricks bundle deploy -t $(TARGET)\n\t$(PY) scripts/bind_traces.py --platform $(PLATFORM) --target $(TARGET)\n"
           f"\tdatabricks bundle run -t $(TARGET) {key}\n\ntraces:\n\t$(PY) scripts/bind_traces.py --platform $(PLATFORM) --target $(TARGET)\n")
    if old not in text:
        sys.exit("Makefile: the deploy target is not the generated one; add scripts/bind_traces.py to it by hand (see templates/agent/Makefile)")
    text = text.replace(old, new, 1).replace(".PHONY: build deploy smoke", ".PHONY: build deploy smoke traces", 1)
    text = text.replace("\t$(PY) scripts/smoke.py $(if $(Q)", "\t$(PY) scripts/smoke.py --target $(TARGET) $(if $(Q)", 1)
    return text


def fill(text: str, values: dict[str, str]) -> str:
    for k, v in values.items():
        text = text.replace("{{" + k + "}}", str(v))
    left = re.findall(r"\{\{[A-Z_]+\}\}", text)
    if left:
        raise SystemExit(f"template placeholders not filled: {sorted(set(left))}")
    return text


# the platform's specialist runtime, vendored into each agent repo (runtime/) so it builds without a platform checkout
RUNTIME_PATHS = ["packages/eqds_agentkit/eqds_agentkit", "packages/eqds_llm/eqds_llm", "apps/__init__.py", "apps/common.py",
                 "agents/__init__.py", "agents/specialist_base", "prompts", "scripts/build_apps.py", "requirements", "config/env", "config/models.*.yaml"]


def snapshot_runtime(out: pathlib.Path) -> str:
    """Copy the runtime paths of this platform checkout into <out>/runtime (same layout) and write <out>/platform_lock.yaml with the
    commit they came from. The agent builds and deploys from this copy alone."""
    import subprocess

    dest = out / "runtime"
    if dest.exists():
        shutil.rmtree(dest)
    ign = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", "tests")
    for rel in RUNTIME_PATHS:
        for src in sorted(ROOT.glob(rel)):
            target = dest / src.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(src, target, ignore=ign)
            else:
                shutil.copy2(src, target)
    commit = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip() or "unknown"
    (out / "platform_lock.yaml").write_text(yaml.safe_dump({"platform_commit": commit, "runtime": RUNTIME_PATHS}, sort_keys=False))
    return commit


def _copy_scaffold(out: pathlib.Path) -> None:
    for f in ("build.py", "smoke.py", "bind_traces.py"):
        shutil.copy2(TPL / "scripts" / f, out / "scripts" / f)
    (out / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
    for f in (TPL / ".github" / "workflows").glob("*.yml"):
        shutil.copy2(f, out / ".github" / "workflows" / f.name)
    commit = snapshot_runtime(out)
    print(f"runtime snapshot written to {out / 'runtime'} (platform commit {commit[:12]})")


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
    ap.add_argument("--also", default="dev", help="further environments to give a bundle target (comma separated, '' for none)")
    ap.add_argument("--scaffold-update", action="store_true", help="refresh the generated scaffolding of an existing project (see the module docstring)")
    ap.add_argument("--bundle-only", action="store_true", help="rewrite only databricks.yml of an existing project (after a template change); keeps every other file")
    a = ap.parse_args()

    key = a.key
    if not re.fullmatch(r"[a-z][a-z0-9]{1,21}", key):  # app names allow lowercase letters, digits and hyphens only
        sys.exit("key: lowercase letters and digits, 2-22 characters")
    env = Env.from_target(a.target, str(ROOT))
    app_name = env.app_name(f"sp-{key}")
    if len(app_name) > 30:
        sys.exit(f"app name {app_name} is longer than 30 characters")
    out = pathlib.Path(a.out or ROOT.parent / f"eqds-agent-{key}").resolve()
    if out.exists() and any(out.iterdir()) and not (a.force or a.bundle_only or a.scaffold_update):
        sys.exit(f"{out} exists and is not empty (use --force to regenerate the scaffolding)")

    existing = out / "agent" / "specialist.yaml"
    if a.from_spec or existing.exists():  # bring an existing agent, or regenerate the scaffolding of a project around its own spec
        spec = yaml.safe_load(pathlib.Path(a.from_spec or existing).read_text())
        if spec.get("key") != key:
            sys.exit(f"--from-spec has key '{spec.get('key')}', expected '{key}'")
    else:
        spec = {"key": key, "display_name": a.display or key.capitalize(), "tag_prefix": (a.tag or key[:2]).upper(), "version": "0.1.0"}
    primary_vals = platform_values(env, key)
    others: list[tuple[Env, dict[str, str]]] = []
    for t in [x.strip() for x in a.also.split(",") if x.strip() and x.strip() != env.target]:
        e2 = Env.from_target(t, str(ROOT))
        if len(e2.app_name(f"sp-{key}")) > 30:
            sys.exit(f"app name {e2.app_name(f'sp-{key}')} is longer than 30 characters")
        try:
            others.append((e2, platform_values(e2, key)))
        except SystemExit as ex:
            print(f"warning: no target {t} written: {ex}", file=sys.stderr)
    values = {"KEY": key, "DISPLAY": spec["display_name"], "TAG": spec["tag_prefix"], "OWNER": a.owner, "APP_NAME": app_name,
              "TARGET": env.target, "VERSION": str(spec.get("version", "0.1.0")), **primary_vals,
              "TARGETS": render_targets(key, env, primary_vals, others)}

    (out / "scripts").mkdir(parents=True, exist_ok=True)
    (out / "databricks.yml").write_text(fill((TPL / "databricks.yml").read_text(), values))
    targets = [env.target] + [e.target for e, _ in others]
    if a.bundle_only:
        print(f"rewrote {out / 'databricks.yml'} (app {app_name}, targets {targets})")
        return 0
    if a.scaffold_update:
        mk = out / "Makefile"
        mk.write_text(patch_makefile(mk.read_text(), key))
        rd = out / "README.md"
        fresh = fill((TPL / "README.md").read_text(), values)
        if rd.exists() and "\n## 1. Deploy" in rd.read_text():  # sections 1 to 6 are generated: replace them, keep the owner's intro
            old = rd.read_text()
            rd.write_text(old[:old.index("\n## 1. Deploy")] + fresh[fresh.index("\n## 1. Deploy"):])
        elif rd.exists() and "## 6. Environments and CI/CD" not in rd.read_text():
            rd.write_text(rd.read_text().rstrip("\n") + "\n" + fresh[fresh.index("\n## 6. Environments and CI/CD"):])
        _copy_scaffold(out)
        print(f"scaffolding updated in {out} (targets {targets})")
        return 0
    (out / "Makefile").write_text(fill((TPL / "Makefile").read_text(), values))
    (out / "README.md").write_text(fill((TPL / "README.md").read_text(), values))
    shutil.copy2(TPL / "gitignore", out / ".gitignore")
    _copy_scaffold(out)

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
