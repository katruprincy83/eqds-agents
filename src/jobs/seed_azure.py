"""job_seed_azure: schema + synthetic rows in the external Azure SQL database (design §14.7).

Runs on compute that can reach the database (classic in the reference design; serverless when the
firewall allows it: `bridge_compute`). DDL and bulk writes both via Spark JDBC (no Python SQL driver).
Only the EQDS schemas (oms, research, feeds) are touched. The `customer` data profile never writes. SYNTHETIC DEMO DATA.
"""
from __future__ import annotations

import os
import sys

try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
except NameError:
    pass

import re
import sys

from _common import get_spark, secret, setup  # noqa: E402

env, args = setup()
if env.data_profile != "synthetic":
    print("data_profile != synthetic: refusing to write to the external database")
    sys.exit(0)
if env.get("shared_external"):
    # the external database is shared with another environment (dev uses the production one read-only): DDL here drops tables
    print(f"shared_external is set for target {env.target}: refusing to write to the shared external database")
    sys.exit(0)
spark = get_spark()

import os  # noqa: E402

from seed.gen_azure.load import LOG_TABLE, load_table, run_sql_file  # noqa: E402
from seed.pipeline import build_azure, build_uc  # noqa: E402

az = env["azure_sql"]
user = secret(env.secret_scope, "azure-sql-seed-user")
pwd = secret(env.secret_scope, "azure-sql-seed-password")
ROOT = os.environ["EQDS_ROOT"]
jdbc = dict(url=env.azure_jdbc_url, user=user, password=pwd, batchsize="10000")


run_sql_file(spark, jdbc, f"{ROOT}/seed/gen_azure/ddl_drop.sql")
run_sql_file(spark, jdbc, f"{ROOT}/seed/gen_azure/ddl.sql")
run_sql_file(spark, jdbc, f"{ROOT}/seed/gen_azure/ddl_log.sql")
print("DDL applied", flush=True)

uc = build_uc()
data = build_azure(uc)
state = {"mode": "jdbc"}
order = [("strategies", "oms.strategies"), ("accounts", "oms.accounts"), ("trades", "oms.trades"), ("positions", "oms.positions"),
         ("analyst_views", "research.analyst_views"), ("notes", "research.notes"), ("news_wire", "feeds.news_wire"), ("web_index", "feeds.web_index")]
for key, table in order:
    pdf = data[key].copy()
    if key == "trades":
        pdf["pm_name"] = pdf["pm_name"].astype("object").where(pdf["pm_name"].notna(), None)   # deliberately NULL (PF-18)
    if key == "analyst_views":
        pdf = pdf.drop(columns=[c for c in ("view_id",) if c in pdf.columns])
    if key == "positions":
        pdf["active_weight_bps"] = pdf["active_weight_bps"].astype("object").where(pdf["active_weight_bps"].notna(), None)
    load_table(spark, jdbc, table, pdf, state)
    print(f"loaded {table}: {len(pdf)} rows", flush=True)
exec_drop = f"IF OBJECT_ID('{LOG_TABLE}') IS NOT NULL DROP TABLE {LOG_TABLE};"
spark.read.format("jdbc").options(**jdbc).option("prepareQuery", exec_drop + "\n").option("query", "SELECT 1 AS ok").load().collect()
print("seed_azure done")
