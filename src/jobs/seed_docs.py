"""job_seed_docs (serverless): broker PDFs, expert / earnings transcripts -> raw_docs volume + document + chunk tables
(design §14.4/§14.7). Planted sentences are inserted verbatim. SYNTHETIC DEMO DATA."""
from __future__ import annotations

import os
import sys

try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
except NameError:
    pass

import os

from _common import get_spark, setup, write_table  # noqa: E402

env, args = setup()
spark = get_spark()

from seed.gen_docs.pdf import render_pdf  # noqa: E402
from seed.pipeline import build_docs, build_uc  # noqa: E402

uc = build_uc()
dc = build_docs(uc)
vol = env.volume_path("raw_docs")
for sub in ("broker", "expert", "earnings"):
    os.makedirs(f"{vol}/{sub}", exist_ok=True)

br = dc["broker_reports"].copy()
paths = []
for r in br.itertuples():
    if r.has_pdf:
        p = f"{vol}/broker/{r.doc_id}.pdf"
        render_pdf(p, r.title, r.body)
        paths.append(p)
    else:
        paths.append(None)
br["pdf_path"] = paths
print("pdfs written:", sum(p is not None for p in paths))

for r in dc["expert_calls"].itertuples():
    with open(f"{vol}/expert/{r.call_id}.md", "w") as f:
        f.write(f"# {r.topic}\n\n_SYNTHETIC — FOR DEMO ONLY_\n\n{r.body}\n")
for r in dc["earnings_calls"].itertuples():
    with open(f"{vol}/earnings/{r.call_id}.md", "w") as f:
        f.write(f"# {r.title}\n\n_SYNTHETIC — FOR DEMO ONLY_\n\n{r.body}\n")

write_table(spark, br, env.table("broker_reports"))
write_table(spark, dc["expert_calls"], env.table("expert_calls"))
write_table(spark, dc["earnings_calls"], env.table("earnings_calls"))
# chunk tables feed the Delta Sync indexes -> change data feed on (§7.2)
for t in ("broker_chunks", "expert_chunks", "earnings_chunks"):
    write_table(spark, dc[t], env.table(t), cdf=True)
print("seed_docs done")
