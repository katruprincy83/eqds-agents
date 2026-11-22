"""job_seed_uc (serverless): synthetic UC tables (design §14.4). Deterministic: seed + DEMO_AS_OF. SYNTHETIC DEMO DATA."""
from __future__ import annotations

import os
import sys

try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
except NameError:
    pass

import json

from _common import get_spark, setup, write_table  # noqa: E402  (job dir is on sys.path when run as a script)

env, args = setup()
spark = get_spark()

from pyspark.sql import types as T  # noqa: E402

from seed.common import load_planted  # noqa: E402
from seed.pipeline import build_eval_questions, build_uc  # noqa: E402

uc = build_uc()
spark.sql(f"CREATE SCHEMA IF NOT EXISTS {env.uc}")

quant = uc["em_quant_scores"].copy()
quant["drivers"] = quant["drivers"].map(lambda m: {k: float(v) for k, v in (m or {}).items()})
quant_schema = T.StructType([T.StructField("company_id", T.StringType()), T.StructField("month_end", T.DateType()),
                             T.StructField("score", T.DoubleType()), T.StructField("drivers", T.MapType(T.StringType(), T.DoubleType())),
                             T.StructField("quintile", T.IntegerType())])
rows = [(r.company_id, r.month_end, None if r.score != r.score else float(r.score), r.drivers, int(r.quintile)) for r in quant.itertuples()]
spark.createDataFrame(rows, quant_schema).write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(env.table("em_quant_scores"))
spark.sql(f"COMMENT ON TABLE {env.table('em_quant_scores')} IS 'SYNTHETIC DEMO DATA'")

for name in ["ref_companies", "ref_holdings", "efm_managers", "efm_holdings", "efm_buy_basket", "prices_daily",
             "earnings_calendar", "geo_revenue", "wiki_pages", "wiki_state"]:
    write_table(spark, uc[name], env.table(name))

ev = build_eval_questions()
write_table(spark, ev, env.table("eval_questions"))

pf = load_planted()
facts = []
for f in pf["facts"]:
    facts.append({"id": f["id"], "expected_specialist": f["expected_specialist"], "source_type": f["source_type"], "eval_fact": f["eval_fact"]})
write_table(spark, __import__("pandas").DataFrame(facts), env.table("demo_planted_facts"))

# Genie / consumers prefer typed, commented columns for the few tables people read directly
spark.sql(f"COMMENT ON COLUMN {env.table('efm_buy_basket')}.score IS 'Number of consecutive month-ends (0-3) the basket name was bought by better-half managers'")
spark.sql(f"COMMENT ON COLUMN {env.table('efm_buy_basket')}.in_top50 IS 'True if the name is in the external managers top-50 buy basket'")
print("seed_uc done", json.dumps({k: len(v) for k, v in uc.items()}))
