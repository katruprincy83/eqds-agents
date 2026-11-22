"""job_register_specialist: registers one specialist agent (UI-triggered, see resources/jobs/registry.yml).

Runs as the registrar service principal. The UI checks the agent's contract first (its identity can call the agents). The same code path as `scripts/register_specialist.py register <key>`:
spec check, contract check (one real call), access grants, moderator CAN_USE, registry row (enabled).
Exits 1 with `FAILED: <reason>` when any step fails, so the UI shows the reason.
"""
from __future__ import annotations

import base64
import json
import os
import sys

from _common import setup

env, args = setup([("--key", {"required": True}), ("--requested-by", {"default": ""}), ("--contract-checked", {"default": "false"}), ("--spec-b64", {"default": ""})])

from bootstrap import register as R  # noqa: E402
from bootstrap.ctx import make_ctx  # noqa: E402

print(f"registration of '{args.key}' requested by '{args.requested_by or 'unknown'}' on {env.target}", flush=True)
ctx = make_ctx(env.target, os.environ["EQDS_ROOT"])
try:
    spec = json.loads(base64.b64decode(args.spec_b64)) if args.spec_b64 else None  # standalone agent: spec read from its /info by the UI
    r = R.register(ctx, args.key, contract_checked=args.contract_checked == "true", spec=spec)
except R.RegistrationError as e:
    print(f"FAILED: {e}", flush=True)
    sys.exit(1)
print(f"registered {r['key']} at {r['url']}", flush=True)
