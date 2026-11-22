"""job_env_bootstrap / job_env_teardown entry: drives bootstrap/manifest.yaml inside the workspace (design §4.1 E9)."""
from __future__ import annotations

import os
import sys

try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
except NameError:
    pass
from _common import get_spark, setup

env, args = setup([("--teardown", {"action": "store_true"}), ("--force_customer", {"action": "store_true"}), ("--step", {"action": "append", "default": []})])
from bootstrap.run import main  # noqa: E402

argv = ["--target", env.target, "--root", os.environ["EQDS_ROOT"]] + (["--teardown"] if args.teardown else []) + (["--force-customer"] if args.force_customer else [])
for s in args.step:
    argv += ["--step", s]
raise SystemExit(main(argv, spark=get_spark()))
