#!/usr/bin/env python
"""CI gate (design §4.2): every asset has a repo definition, an idempotent create path and a manifest entry.
Fails when a job/app in resources/ is missing from bootstrap/manifest.yaml (or vice versa), a bootstrap step has no
implementation, an asset's defined_in path is missing, or an environment-specific value leaks into code (E1)."""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "packages" / "eqds_agentkit"), str(ROOT / "packages" / "eqds_llm")]
errs: list[str] = []
man = yaml.safe_load((ROOT / "bootstrap" / "manifest.yaml").read_text())


def keys(kind: str) -> set[str]:
    out: set[str] = set()
    for f in (ROOT / "resources" / kind).glob("*.yml"):
        out |= set((yaml.safe_load(f.read_text()).get("resources", {}).get(kind) or {}).keys())
    return out


jobs, apps = keys("jobs"), keys("apps")
if jobs != set(man["jobs"]):
    errs.append(f"jobs differ: resources-only={sorted(jobs - set(man['jobs']))} manifest-only={sorted(set(man['jobs']) - jobs)}")
if apps != set(man["apps"]):
    errs.append(f"apps differ: resources-only={sorted(apps - set(man['apps']))} manifest-only={sorted(set(man['apps']) - apps)}")
from bootstrap import steps  # noqa: E402

for st in man["bootstrap_steps"]:
    for k in ("apply", "teardown"):
        if not hasattr(steps, st[k]):
            errs.append(f"bootstrap step {st['id']}: {st[k]} is not implemented in src/bootstrap/steps.py")
for a in man["assets"]:
    if not (ROOT / a["defined_in"]).exists():
        errs.append(f"asset {a['id']}: defined_in {a['defined_in']} does not exist")

# E1: no hostnames, workspace ids or credentials in code/config outside config/env/*.yaml and databricks.yml targets
pat = re.compile(r"dbc-[0-9a-f]{8}-[0-9a-f]{4}|\.cloud\.databricks\.com|\.azuredatabricks\.net|database\.windows\.net|DataBr1ck5")
skip = {"build", ".venv", ".git", "mlruns", "node_modules", ".databricks", "demo", "__pycache__"}
for f in ROOT.rglob("*"):
    if not f.is_file() or set(f.relative_to(ROOT).parts) & skip or f.suffix in {".png", ".pdf", ".pptx", ".db", ".pyc"}:
        continue
    rel = str(f.relative_to(ROOT))
    if rel.startswith(("config/env/", "docs/", "tests/")) or rel in {"databricks.yml", "scripts/manifest_check.py", "Makefile", "README.md"}:
        continue
    if f.suffix in {".py", ".yml", ".yaml", ".sql", ".json", ".js", ".html", ".md"} and pat.search(f.read_text(errors="ignore")):
        errs.append(f"E1: environment-specific value in {rel}")
if errs:
    print("\n".join("FAIL " + e for e in errs))
    sys.exit(1)
print(f"manifest ok: {len(jobs)} jobs, {len(apps)} apps, {len(man['bootstrap_steps'])} bootstrap steps, {len(man['assets'])} assets")
