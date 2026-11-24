"""Write layer for the external Azure SQL database (design §14.7). SYNTHETIC DEMO DATA.

Two paths, chosen automatically, same data:
  * `jdbc`    Spark JDBC bulk write (where the compute accepts JDBC DML).
  * `batches` T-SQL `INSERT ... VALUES` batches sent through the JDBC *read* path (`prepareQuery`), for serverless
              compute where Spark refuses DML on JDBC sinks (UNSUPPORTED_DATA_SOURCE_WRITE). Each batch is guarded
              by an idempotency token because Spark may execute the prefix more than once (schema inference + read).
"""
from __future__ import annotations

import datetime as dt
import math
import re
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

LOG_TABLE = "oms._seed_batches"


def lit(v) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)) or v is pd.NaT or (v is pd.NA):
        return "NULL"
    if isinstance(v, (bool, np.bool_)):
        return "1" if v else "0"
    if isinstance(v, (int, np.integer)):
        return str(int(v))
    if isinstance(v, (float, np.floating)):
        return repr(float(v))
    if isinstance(v, pd.Timestamp):
        v = v.to_pydatetime()
    if isinstance(v, dt.datetime):
        return "'" + v.strftime("%Y-%m-%d %H:%M:%S") + "'"
    if isinstance(v, dt.date):
        return "'" + v.isoformat() + "'"
    return "N'" + str(v).replace("'", "''") + "'"


def _batch_sql(table: str, cols: list[str], rows: list[tuple], token: str) -> str:
    values = ",\n".join("(" + ",".join(lit(x) for x in r) + ")" for r in rows)
    return (f"SET XACT_ABORT ON;\nIF NOT EXISTS (SELECT 1 FROM {LOG_TABLE} WHERE batch_id = '{token}')\nBEGIN\n BEGIN TRAN;\n"
            f" INSERT INTO {table} ({', '.join(cols)}) VALUES\n{values};\n INSERT INTO {LOG_TABLE} (batch_id) VALUES ('{token}');\n COMMIT;\nEND;\n")


def exec_batch(spark, jdbc: dict, sql: str) -> None:
    spark.read.format("jdbc").options(**jdbc).option("prepareQuery", sql).option("query", "SELECT 1 AS ok").load().collect()


def run_sql_file(spark, jdbc: dict, path: str) -> None:
    """Run T-SQL batches (split on GO) through the JDBC prepareQuery prefix (no Python SQL driver needed)."""
    for batch in re.split(r"^\s*GO\s*$", open(path).read(), flags=re.M | re.I):
        if batch.strip():
            exec_batch(spark, jdbc, batch.strip() + "\n")


def load_batches(spark, jdbc: dict, table: str, pdf: pd.DataFrame, batch_rows: int = 1000, workers: int = 8) -> None:
    cols = list(pdf.columns)
    per = max(10, min(batch_rows, int(400_000 / max(1, int(pdf.astype(str).apply(lambda s: s.str.len()).sum(axis=1).mean() + 20)))))
    recs = list(pdf.astype(object).where(pdf.notna(), None).itertuples(index=False, name=None))
    chunks = [(i // per, recs[i:i + per]) for i in range(0, len(recs), per)]
    tag = table.replace(".", "_")

    def one(c):
        n, rows = c
        exec_batch(spark, jdbc, _batch_sql(table, cols, rows, f"{tag}:{n}"))
        return len(rows)

    with ThreadPoolExecutor(workers) as ex:
        done = sum(ex.map(one, chunks))
    print(f"  batch-inserted {done} rows into {table} in {len(chunks)} batches", flush=True)


def load_table(spark, jdbc: dict, table: str, pdf: pd.DataFrame, mode_state: dict) -> None:
    """Write `pdf` to `table`. mode_state['mode'] starts as 'jdbc' and flips to 'batches' on the first serverless refusal."""
    if mode_state.get("mode", "jdbc") == "jdbc":
        try:
            spark.createDataFrame(pdf).repartition(8).write.format("jdbc").options(**jdbc, dbtable=table).mode("append").save()
            print(f"  jdbc-wrote {len(pdf)} rows into {table}", flush=True)
            return
        except Exception as e:  # noqa: BLE001
            if "UNSUPPORTED_DATA_SOURCE_WRITE" not in str(e):
                raise
            print("  Spark refuses JDBC DML on this compute (serverless): switching to T-SQL batch inserts", flush=True)
            mode_state["mode"] = "batches"
    load_batches(spark, jdbc, table, pdf)
