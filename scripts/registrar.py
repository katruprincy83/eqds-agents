#!/usr/bin/env python3
"""`make registrar TARGET=<t>`: create the registrar service principal (once), record its application id in the env profile
(`service_principals.registrar`), then apply its grants (bootstrap step `app_grants`). Run before `make vars` / `make deploy`.

The registration job runs as this identity (resources/jobs/registry.yml), so the UI can register agents without anyone
running the CLI or holding the deployer's rights.
"""
import argparse, pathlib, re, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "src", ROOT / "packages/eqds_agentkit", ROOT / "packages/eqds_llm"):
    sys.path.insert(0, str(p))
from eqds_agentkit.envcfg import Env
from databricks.sdk import WorkspaceClient

ap = argparse.ArgumentParser()
ap.add_argument("--target", default="demo")
ap.add_argument("--profile", default=None)
a = ap.parse_args()

env = Env.from_target(a.target, ROOT)
w = WorkspaceClient(profile=a.profile) if a.profile else WorkspaceClient()
name = f"{env.prefix}-registrar{env.suffix}"
configured = (env.get("service_principals") or {}).get("registrar") or ""

sp = next(iter(w.service_principals.list(filter=f'displayName eq "{name}"')), None)
if sp is None:
    sp = w.service_principals.create(display_name=name, active=True)
    print(f"created service principal {name} ({sp.application_id})")
else:
    print(f"service principal {name} exists ({sp.application_id})")

if configured != sp.application_id:
    f = ROOT / "config" / "env" / f"{a.target}.yaml"
    text = f.read_text()
    new, n = re.subn(r'(service_principals: \{[^}]*?registrar: )"[^"]*"', rf'\g<1>"{sp.application_id}"', text, count=1)
    if n != 1:
        sys.exit(f"could not find service_principals.registrar in {f.relative_to(ROOT)}")
    f.write_text(new)
    print(f"recorded registrar id in {f.relative_to(ROOT)}")

# the person deploying must be able to run jobs as this identity ("Service Principal User" on the SP). This workspace's
# permissions API does not expose service principals, so it is granted in the admin console (printed below if missing).
me = w.current_user.me().user_name
try:
    w.api_client.do("PATCH", f"/api/2.0/permissions/service-principals/{sp.id}",
                    body={"access_control_list": [{"user_name": me, "permission_level": "CAN_USE"}]})
    print(f"{me} can run jobs as {name} (Service Principal User)")
except Exception as e:  # noqa: BLE001
    print(f"NOT CONFIRMED: grant {me} the Service Principal User role on {name} ({sp.application_id}) in the admin console:\n"
          f"  Settings > Identity and access > Service principals > {name} > Permissions > add {me} as 'Service Principal User'.\n"
          f"  ({str(e)[:100]})")

rc = subprocess.call([sys.executable, str(ROOT / "src/bootstrap/run.py"), "--target", a.target, "--step", "app_grants"], cwd=ROOT)
if rc:
    sys.exit(rc)
print("registrar ready. Next: make deploy (the registration job now runs as this identity).")
