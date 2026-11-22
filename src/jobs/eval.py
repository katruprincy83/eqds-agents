"""job_eval (serverless): runs eval/run_eval.py against the deployed moderator. Parameters: subset (smoke|full), rounds."""
from __future__ import annotations

import os
import sys

try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
except NameError:
    pass
from _common import setup

env, args = setup([("--subset", {"default": "smoke"}), ("--rounds", {"default": "0"})])
sys.argv = ["run_eval.py", "--target", env.target, "--subset", args.subset, "--rounds", args.rounds]
sys.path.insert(0, os.path.join(os.environ["EQDS_ROOT"], "eval"))
import run_eval  # noqa: E402

raise SystemExit(run_eval.main())
