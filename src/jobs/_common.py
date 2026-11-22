"""Shared bootstrap for job entry points. Jobs receive `--target` and `--root` (the synced bundle files path).

Nothing environment-specific here: the profile is read from `config/env/<target>.yaml` (design §4.1 E1).
"""
from __future__ import annotations

import argparse
import os
import sys


def setup(extra_args: list[tuple[str, dict]] | None = None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default=os.environ.get("EQDS_TARGET", "demo"))
    ap.add_argument("--root", default=os.environ.get("EQDS_ROOT"))
    for flag, kw in extra_args or []:
        ap.add_argument(flag, **kw)
    a, _ = ap.parse_known_args()
    root = a.root or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    os.environ["EQDS_ROOT"] = root
    for p in (root, os.path.join(root, "src"), os.path.join(root, "packages", "eqds_agentkit"), os.path.join(root, "packages", "eqds_llm")):
        if p not in sys.path:
            sys.path.insert(0, p)
    from eqds_agentkit.envcfg import Env

    return Env.from_target(a.target, root), a


def get_spark():
    from pyspark.sql import SparkSession

    return SparkSession.builder.getOrCreate()


def secret(scope: str, key: str) -> str:
    try:
        from pyspark.dbutils import DBUtils

        return DBUtils(get_spark()).secrets.get(scope, key)
    except Exception:
        import base64

        from databricks.sdk import WorkspaceClient

        v = WorkspaceClient().secrets.get_secret(scope=scope, key=key).value
        return base64.b64decode(v).decode()


def write_table(spark, pdf, fqn: str, comment: str = "SYNTHETIC DEMO DATA", cdf: bool = False, schema=None, mode: str = "overwrite"):
    """Write a pandas DataFrame as a managed Delta table with a comment (SD tables are labelled, §14.1)."""
    df = spark.createDataFrame(pdf, schema=schema) if schema is not None else spark.createDataFrame(pdf)
    w = df.write.format("delta").mode(mode).option("overwriteSchema", "true")
    w.saveAsTable(fqn)
    spark.sql(f"COMMENT ON TABLE {fqn} IS '{comment}'")
    if cdf:
        spark.sql(f"ALTER TABLE {fqn} SET TBLPROPERTIES (delta.enableChangeDataFeed = true)")
    print(f"wrote {fqn}: {len(pdf)} rows", flush=True)
