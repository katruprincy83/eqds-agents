# Validation on the demo workspace (synthetic data)

Workspace: the demo AWS workspace (`config/env/demo.yaml`). External database: Azure SQL reached through Lakehouse Federation; credentials live only in secret scope `eqds-bridge`.

## What was deployed (all from the repo)

12 apps (UI, moderator, bridge MCP, 9 specialists), 17 jobs, UC schema/volumes/functions, Lakebase schemas, 4 Vector Search indexes (news index created after `ingest_news`), Genie space, embed endpoint, experiment, dashboard.

## Results

| Check | Result |
|---|---|
| `bundle validate` / `manifest_check` / unit + contract tests | pass / pass / 9 passed |
| `validate_seed` | 15 of 15 checks pass (PF-01 8 score-3 names, PF-05 -83.0m, PF-07, PF-09 +36.6m / +6.9%, PF-11 +12.9m / -46.7%, PF-13 56 conflicts / 45 confirms, PF-20, PF-21 14%) |
| Headline question on the deployed stack (2 rounds) | DONE in 279-316 s, USD 0.41-0.85, citation validation passed (coverage 1.0) |
| Eval smoke job (10 questions, 1 round) | citation_validity 1.0, claim_citation_coverage 0.99-1.0, within_budget 1.0, error_rate 0; **correctness 0.30-0.37 vs gate 0.75 (fails)** |
| `wiki_refresh` for one company (full build, 7 pages) | success, 17 min |
| `ingest_news`, `docs_index`, `build_eval` (40 questions) | success |

## Deviations from the design and requirements

1. **Managed MCP in the demo only** (`flags.use_managed_mcp: true` in `demo.yaml`): UC functions and Vector Search go through Databricks managed MCP. Managed Vector Search takes only a query, so company-filtered searches use the SDK adapter, which filters on the server. dev, staging and prod all point at the one GIC workspace (design C-04), so they share one value: `false` until managed MCP is enabled there, then `true` and `scale_to_zero: false` for all three together.
2. **Bridge compute is serverless**: it is the only bridge compute, in every environment. Serverless reaches the internet and the whitelisted internal sources, including Azure SQL federation (`network.serverless_blocked: false`).
3. **Bridge latency**: each bridge query takes 11-28 s cross-cloud, so the p95 target of 6 s is not met in this topology.
4. **Seed checks reworded**: `positions_sum` compares day-over-day position changes with that day's net trades (positions are daily snapshots); `skew` uses rebalance-day share of NCREP trades (> 15 %, observed 21.5 %) because month-end calendar days fall on weekends.
5. **Gateway limits**: the Apps gateway drops requests after about 3 minutes and streams after about 5 minutes. Specialists stop gathering at `timeout_s - 45` and finalise on partial evidence; the moderator closes each SSE stream after 240 s and clients resume with `Last-Event-ID`.
6. **Jobs call the moderator as a service principal** (`<prefix>-job-caller`, created by `make infra`) because Apps reject notebook tokens.
7. **Schedules are off** (`schedules.enabled: false`) so a demo workspace does not spend tokens unattended.

## Open items

* Answer quality: eval correctness is below the gate. Headline answers name six of the eight score-3 names and miss several planted facts at 1-2 rounds; prompts, the `efm` skill and round count need tuning before the gate is enforced.
* Not yet exercised on the deployed stack: crash/resume (AC-04), UI developer gating by group, the full 40-question eval.
