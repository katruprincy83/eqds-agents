"""job_ingest_news (bridge compute; every 15 min): land news history from `azure_oms.feeds.news_wire` into UC `news_items`
with an incremental MERGE and sync the news index (design §7.2, pattern A). The only external data that is copied into UC."""
from __future__ import annotations

import os
import sys

try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
except NameError:
    pass
from _common import get_spark, setup

env, args = setup()
spark = get_spark()
uc, fc = env.uc, env.foreign_catalog
spark.sql(f"""CREATE TABLE IF NOT EXISTS {uc}.news_items (news_id BIGINT, published_at TIMESTAMP, company_id STRING, headline STRING, source STRING, body STRING, tags STRING)
              COMMENT 'News history landed from the external wire (pattern A). SYNTHETIC DEMO DATA' TBLPROPERTIES (delta.enableChangeDataFeed = true)""")
wm = spark.sql(f"SELECT coalesce(max(published_at), TIMESTAMP'1970-01-01') AS w FROM {uc}.news_items").collect()[0]["w"]
spark.sql(f"""MERGE INTO {uc}.news_items t USING (
                SELECT news_id, published_at, company_id, headline, source, coalesce(body, headline) AS body, tags
                FROM {fc}.feeds.news_wire WHERE published_at > TIMESTAMP'{wm}' - INTERVAL 1 HOUR) s
              ON t.news_id = s.news_id WHEN NOT MATCHED THEN INSERT *""")
n = spark.sql(f"SELECT count(*) AS n FROM {uc}.news_items").collect()[0]["n"]
print("news_items rows:", n, flush=True)
try:
    from databricks.sdk import WorkspaceClient

    WorkspaceClient().vector_search_indexes.sync_index(f"{uc}.news_items_idx")
    print("news index sync triggered")
except Exception as e:  # noqa: BLE001
    print("news index not synced:", str(e)[:100])
