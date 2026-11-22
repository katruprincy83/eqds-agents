"""job_env_preflight: run the environment preflight inside the workspace (design §4.1 E10)."""
from __future__ import annotations

import os
import sys

try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
except NameError:
    pass
from _common import setup

env, args = setup()
from databricks.sdk import WorkspaceClient  # noqa: E402

from bootstrap import preflight  # noqa: E402

rc = preflight.report(preflight.run(env, WorkspaceClient(), os.environ["EQDS_ROOT"]))
if rc:
    raise SystemExit("preflight failed")
