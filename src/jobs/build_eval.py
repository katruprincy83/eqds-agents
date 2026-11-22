"""job_build_eval (serverless): write `eval_questions` from seed/planted_facts.yaml (single source of truth, design §14.7, §11.2)."""
from __future__ import annotations

import os
import sys

try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
except NameError:
    pass
from _common import get_spark, setup, write_table

env, args = setup()
spark = get_spark()
from seed.pipeline import build_eval_questions  # noqa: E402

write_table(spark, build_eval_questions(), env.table("eval_questions"), comment="Evaluation questions with expected facts. SYNTHETIC DEMO DATA")
