"""job_state_export / job_state_import: portable operational state (design §4.2): Lakebase runs, memory and runtime registry rows <-> Parquet in the
`state_exports` volume. Config, code, data seeds are NOT state (they come from Git). Usage: --mode export|import [--path <dir>]."""
from __future__ import annotations

import datetime as dt
import json
import os
import sys

try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
except NameError:
    pass
import pandas as pd
from _common import setup

env, args = setup([("--mode", {"default": "export"}), ("--path", {"default": ""})])
from eqds_agentkit import lakebase  # noqa: E402
from psycopg.types.json import Jsonb  # noqa: E402

TABLES = ["ops.runs", "ops.run_events", "ops.wiki_requests", "mem.conversation", "mem.specialist", "registry.specialists"]
conn = lakebase.connect(lakebase.resolve(env.lakebase_project, env.get("lakebase.branch", "production"), env.lakebase_database))
base = args.path or f"{env.volume_path('state_exports')}/{dt.datetime.utcnow():%Y%m%dT%H%M%S}"

if args.mode == "export":
    os.makedirs(base, exist_ok=True)
    manifest = {"target": env.target, "created": dt.datetime.utcnow().isoformat(), "tables": {}}
    for t in TABLES:
        rows = conn.execute(f"SELECT * FROM {t}").fetchall()
        df = pd.DataFrame(rows)
        for c in df.columns:
            if len(df) and isinstance(df[c].iloc[0], (dict, list)):
                df[c] = df[c].map(lambda v: json.dumps(v) if v is not None else None)
        df.to_parquet(f"{base}/{t}.parquet", index=False)
        manifest["tables"][t] = len(df)
        print("exported", t, len(df))
    json.dump(manifest, open(f"{base}/manifest.json", "w"), indent=2)
    print("state exported to", base)
else:
    for t in TABLES:
        f = f"{base}/{t}.parquet"
        if not os.path.exists(f):
            continue
        df = pd.read_parquet(f)
        if df.empty:
            continue
        info = {r["column_name"]: r["data_type"] for r in conn.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_schema = %s AND table_name = %s", t.split(".")).fetchall()}
        cols = list(df.columns)
        sql = f"INSERT INTO {t} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) ON CONFLICT DO NOTHING"
        n = 0
        for rec in df.itertuples(index=False):
            vals = []
            for c, v in zip(cols, rec):
                if v is None or (isinstance(v, float) and v != v):
                    vals.append(None)
                elif info.get(c) == "jsonb":
                    vals.append(Jsonb(json.loads(v) if isinstance(v, str) else v))
                elif hasattr(v, "tolist"):
                    vals.append(v.tolist())
                else:
                    vals.append(v)
            n += conn.execute(sql, vals).rowcount
        print("imported", t, n)
        if t == "ops.run_events":
            conn.execute("SELECT setval(pg_get_serial_sequence('ops.run_events','seq'), GREATEST(1, (SELECT coalesce(max(seq),1) FROM ops.run_events)))")
