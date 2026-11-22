#!/usr/bin/env python3
"""Register / list / enable / disable / unregister specialists without touching the moderator.

  python scripts/register_specialist.py register news --target demo
  python scripts/register_specialist.py list
  python scripts/register_specialist.py disable news      # hidden from the moderator and the UI
  python scripts/register_specialist.py enable news
  python scripts/register_specialist.py unregister news   # removes the registry row only
"""
import argparse, os, pathlib, sys
ROOT = pathlib.Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "src", ROOT / "packages/eqds_agentkit", ROOT / "packages/eqds_llm"):
    sys.path.insert(0, str(p))
from bootstrap.ctx import make_ctx
from bootstrap import register as R

ap = argparse.ArgumentParser()
ap.add_argument("action", choices=["register", "list", "enable", "disable", "unregister"])
ap.add_argument("key", nargs="?")
ap.add_argument("--target", default=os.environ.get("TARGET", "demo"))
ap.add_argument("--profile", default=os.environ.get("DATABRICKS_CONFIG_PROFILE"))
a = ap.parse_args()
if a.action != "list" and not a.key:
    ap.error(f"{a.action} needs a specialist key")
ctx = make_ctx(a.target, str(ROOT), profile=a.profile)
try:
    if a.action == "register":
        r = R.register(ctx, a.key)
        print(f"registered {r['key']} at {r['url']}. The moderator picks it up within 60 s (immediately via the registration page).")
    elif a.action == "list":
        for r in R.listing(ctx):
            print(f"{'ON ' if r['enabled'] else 'OFF'} {r['key']:<10} {r['tag_prefix']:<3} {r['display_name']:<28} {r['app']}")
    elif a.action in ("enable", "disable"):
        R.set_enabled(ctx, a.key, a.action == "enable"); print(f"{a.key}: {a.action}d")
    else:
        R.unregister(ctx, a.key); print(f"{a.key}: registry row removed")
except R.RegistrationError as e:
    print(f"FAILED: {e}"); sys.exit(1)
