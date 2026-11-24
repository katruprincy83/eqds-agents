# EQDS agents: deployable to any workspace

Multi-agent research platform (LangGraph moderator, external OMS database read directly through the foreign catalog, wiki, evaluation). Specialist agents are
not in this repo: each has its own codebase and deploy, and is registered with the orchestrator on the UI's Specialist registry page.
Design: `../EQDS_Target_State_Design.md` · Requirements: `../EQDS_Target_State_Requirements.md` · Validation results: `docs/validation-demo.md`.

## Deploy into a new workspace

1. `cp config/env/dev.yaml config/env/<name>.yaml` and `config/models.dev.yaml config/models.<name>.yaml`; fill every `REPLACE-ME` and add a `targets:` block in `databricks.yml` (host only).
2. `databricks auth login --host <workspace-url> --profile <name>` and `export DATABRICKS_CONFIG_PROFILE=<name>`.
3. Export the external database credentials (they are written to the secret scope only, never to the repo):
   `export EQDS_AZURE_SQL_USER=... EQDS_AZURE_SQL_PASSWORD=...`
4. `make preflight TARGET=<name>` (permissions, quotas, network reachability, endpoints), then `make all TARGET=<name>`.
5. Synthetic profile only: `make seed TARGET=<name>` (deterministic data, then 15 self-checks), then `make all TARGET=<name>` again so the apps pick up the new Genie space id.
6. `make ask TARGET=<name> Q="What are the best external managers buying now, and do we own or trade any of it?"`

`make all` = `vars` (render bundle variables from the profile) → `infra` (catalog, secret scope, Lakebase project, Vector Search endpoint, service principals) → `build` (apps are vendored into `build/apps/*`) → `deploy` (`bundle deploy`) → `bootstrap` (federation, UC functions, Lakebase schemas, skills, indexes, Genie space, app grants, registry, outputs) → `apps` (deploy source and start) → `outputs`.
Everything is idempotent. `make teardown` removes bootstrap assets; `make destroy` also runs `bundle destroy`.

## Environments and CI/CD

One workspace, two environments, two branches in every repo: `main` is production (target `demo`), `dev` is pre-production (target `dev`:
apps and jobs `-dev`, catalog `eqds_dev`, Lakebase branch `dev`, experiments in `/Shared/eqds-dev`, traces stored in `eqds_dev.traces`).
Push to `dev` deploys dev and smoke-tests it; a PR `dev` to `main` promotes to production behind an approval. Details, one-time setup and the
list of what dev borrows from production: `docs/cicd.md`. Commands: `make all TARGET=dev`, `make smoke TARGET=dev`.

## Portability rules (NFR-20..22, design §4.1)

* No hostnames, catalogs, ids or secrets in code: `scripts/manifest_check.py` (CI) fails the build if one appears outside `config/env/*.yaml` and `databricks.yml` targets.
* Every asset has a repo definition, an idempotent create path, a teardown path and an entry in `bootstrap/manifest.yaml`; the same script enforces it.
* Sibling app URLs are resolved by name at runtime; the Genie space id and other bootstrap outputs flow back into the bundle through `scripts/pull_outputs.py`.

## Layout

| Path | Contents |
|---|---|
| `config/env/<target>.yaml`, `config/models.<target>.yaml` | environment profile and model tiers (the only per-environment inputs) |
| `databricks.yml`, `resources/` | bundle: schemas, volumes, warehouse, embed endpoint, experiments (platform and moderator), jobs, 2 platform apps (ui, moderator), dashboard |
| `agents/` | moderator graph and the specialist runtime (`specialist_base`) that every agent repo vendors at build time |
| `templates/agent/`, `scripts/new_agent.py` | scaffolding for a specialist agent repo (`make agent KEY=<key>`) |
| `apps/` | UI and moderator app |
| `packages/` | `eqds_llm` (tiered LLM client), `eqds_agentkit` (config, auth, Lakebase, registry) |
| `src/bootstrap`, `bootstrap/` | bootstrap steps and their SQL/JSON |
| `src/jobs`, `src/wiki`, `src/uc_functions` | job entry points, wiki triggers, UC functions |
| `seed/` | deterministic synthetic data (seed 42, as-of 2026-11-20), planted facts, validation checks |
| `eval/` | scorers, thresholds, runner |
| `scripts/` | build, vars, infra, preflight, outputs, local stack, ask |

## Specialist agents

Each specialist is its own project (`eqds-agent-<key>`: `agent/specialist.yaml`, `agent/skills/`, its own `databricks.yml` with one
app `<prefix>-sp-<key><suffix>`). The orchestrator only knows the agents in its registry (`registry.specialists` in Lakebase).

1. Create: `make agent KEY=<key>` writes `../eqds-agent-<key>` with this workspace's values filled in (or bring an existing spec with
   `python scripts/new_agent.py <key> --from-spec <specialist.yaml> --skills <skills/<key>>`).
2. Deploy, in the agent project: `make deploy`, then `make smoke`.
3. Register: UI developer view, **Under the hood → Specialist registry**, type the key, **Register agent** (or `make register KEY=<key>`
   here). The UI reads the spec from the app's `/info`; the registration job (registrar service principal) grants the declared access
   and writes the row disabled; the UI enables it after one real call.
4. **Disable** / **Enable** on the same page take effect at once. With no agent registered, the moderator refuses new runs.

## Local development

`python scripts/local_stack.py` runs the built sibling agent projects (`../eqds-agent-*`) in one process against the live workspace; moderator and UI run with `uvicorn` (`EQDS_TARGET=demo`). `make test` runs unit and contract tests.

## LLM access: Unity AI Gateway model services

Every agent LLM call goes through a Unity AI Gateway **model service** (a Unity Catalog securable), one per tier:
`<catalog>.<schema>.<name_prefix>_llm_<reasoning|standard|fast><suffix>`. No legacy Model Serving endpoints and no direct pay-per-token endpoints are used for LLMs.

| What | Where |
|---|---|
| Services, routing, fallback, rate limits, inference tables | `resources/serving/llm.yml`, rendered from `config/models.<target>.yaml` and `model_serving.rate_limits` by `scripts/bundle_vars.py` |
| Inference tables | `<catalog>.<schema>.inference_<service>_payload` (every request and response, latency, caller) |
| Caller access | `EXECUTE` on each service, granted to every app service principal by bootstrap step `app_grants` |
| Client | `packages/eqds_llm` calls `https://<workspace-id>.ai-gateway.<cloud domain>/mlflow/v1` with the service name as `model` (override with `gateway.url` in the models file or `EQDS_AI_GATEWAY_URL`) |
| Smoke test | bootstrap step `model_services` (one call per tier) |

The one remaining serving endpoint is `<name_prefix>-embed`, because Vector Search managed indexes require a Model Serving endpoint for their embedding model. Agents never call it.
