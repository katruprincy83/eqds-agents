"""Developer helper: render a bridge template and run it on the SQL warehouse (params inlined as literals).
usage: python scripts/try_template.py --target demo oms_trade_flows '{"company_ids":["C0101"],"start":"2026-10-22","end":"2026-11-20"}'"""
import argparse, json, os, sys
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (root, os.path.join(root, "src"), os.path.join(root, "packages", "eqds_agentkit")):
    sys.path.insert(0, p)
from bootstrap.ctx import make_ctx
from bridge_worker.render import render
ap = argparse.ArgumentParser(); ap.add_argument("--target", default="demo"); ap.add_argument("tool"); ap.add_argument("args")
a = ap.parse_args()
ctx = make_ctx(a.target, root)
sql, params = render(a.tool, json.loads(a.args), ctx.env.uc, ctx.env.foreign_catalog)
for k in sorted(params, key=len, reverse=True):
    v = params[k]
    lit = str(v) if isinstance(v, (int, float)) else "'" + str(v).replace("'", "''") + "'"
    sql = sql.replace(":" + k, lit)
rows = ctx.sql(sql)
print(json.dumps(rows[:int(os.environ.get("N", 12))], indent=1)[:6000]); print(len(rows), "rows")
