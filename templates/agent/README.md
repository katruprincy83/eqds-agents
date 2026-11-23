# {{DISPLAY}} agent (`{{KEY}}`)

A specialist agent with its own codebase and its own deploy. The EQDS orchestrator uses it only after it is registered on the
orchestrator's **Specialist registry** page; until then the orchestrator does not know it exists.

```
agent/specialist.yaml        identity, description, tools and data access (the running app serves it at /info)
agent/skills/{{KEY}}/<ver>/  the agent's instructions (SKILL.md); the version is pinned in specialist.yaml
scripts/build.py             assembles ./app = platform runtime + this agent's files
scripts/smoke.py             checks the deployed app on its own
databricks.yml               own bundle: one Databricks App, {{APP_NAME}}
```

## 1. Deploy

Needs a checkout of the platform repo (`eqds-agents`) next to this one: the runtime (ReAct loop, tools, contract, tracing) is
vendored from it at build time.

```bash
export DATABRICKS_CONFIG_PROFILE=<your profile for the workspace>
make deploy                 # = python3 scripts/build.py, databricks bundle deploy, databricks bundle run {{KEY}}
make smoke                  # GET /healthz and /info must return 200
```

## 2. Register with the orchestrator

1. Open the orchestrator UI, switch to the developer view, go to **Under the hood → Specialist registry**.
2. Type `{{KEY}}` in the registration box and click **Register agent**.
3. Wait for "{{KEY}} is registered and enabled" (about a minute). The UI checks `/healthz` and `/info`; the registration job
   (registrar service principal) grants the agent the tables, functions and models in `access:`, lets the moderator call it and
   writes the registry row; the UI then makes one real call and enables the agent only if it answers.
4. Check: `make smoke Q="<a question this agent should answer>"` and ask the same in the orchestrator.

The key is all you type: the app is found by name (`<prefix>-sp-<key><suffix>` = `{{APP_NAME}}`) and its URL is read from the
Databricks Apps API.

## 3. Turn it off and on

Same page: **Disable** hides the agent from the moderator at once (the app keeps running), **Enable** brings it back.

## 4. Change it

| Change | What to do |
|---|---|
| Skill text, code pins (new runtime) | bump the skill version in `specialist.yaml`, `make deploy` |
| `description`, `domains`, `tag_prefix`, `timeout_s` | `make deploy`, then **Register agent** again: the registry row is a copy of the spec |
| `tools` or `access` (new tables or functions) | `make deploy`, then **Register agent** again: registration applies the new grants |

Rules: the app name must stay `{{APP_NAME}}`; `tag_prefix` must be unique across agents; only list the tables and functions the
agent needs. The `permissions:` block in `databricks.yml` is the app's full permission list (each deploy resets it), so keep the
registrar, UI and moderator entries in it.

## 5. Traces

Every call the moderator makes to this agent is **one MLflow trace in this agent's own experiment**, `<platform experiment path>-{{KEY}}`
(`{{EXPERIMENT_PATH}}` for {{TARGET}}). The bundle creates the experiment (`resources.experiments.traces`) and binds the app to it, so a new
workspace needs nothing extra beyond the platform deploy, which creates the experiment's parent folder.

- Root span `specialist.{{KEY}}`, with the LLM calls and every tool call nested under it.
- Tags (searchable): `run_id`, `conversation_id`, `specialist`, `caller`, `tools`, `cached`, and `moderator_trace_id`, the id of the moderator's
  trace in the moderator's experiment. The Traces tab groups a conversation as one *session*.
- Find a run: filter `tags.run_id = '<run id>'`. The moderator's span for this agent carries `child_trace_id`, so you can follow the link in either direction.
- The UI's run trace view reads this experiment through the `CAN_READ` grant for the UI service principal in `databricks.yml`.

## 6. Environments and CI/CD

One workspace hosts two environments. This repo has two long-lived branches and one bundle target for each:

| Branch | Target | Environment | App | Experiment | Traces |
|---|---|---|---|---|---|
| `main` | `demo` | production | `{{APP_NAME}}` | `{{EXPERIMENT_PATH}}` | MLflow storage |
| `dev` | `dev` | pre-production | the same name with `-dev` | `/Shared/eqds-dev/eqds-{{KEY}}` | Unity Catalog, `eqds_dev.traces` |

Work on a feature branch, open a PR into `dev` (checks build the app for both targets), merge: the `deploy-dev` workflow deploys
to the dev environment and runs the smoke check. When dev is good, open a PR `dev` to `main`; merging it runs `deploy-prod`, which
waits for approval on the `production` GitHub environment. Manual equivalents: `make deploy TARGET=dev`, `make deploy TARGET=demo`.

The dev agent registers on the **dev** UI (`eqds-ui-dev`), never on production's, and talks to the dev platform: the dev registry, the
dev Lakebase branch, the `eqds_dev` catalog. To refresh the platform values of a target (new service principals after a platform
redeploy) run, in the platform repo, `python scripts/new_agent.py {{KEY}} --scaffold-update`.

CI needs, in this repo's settings: repository variables `DATABRICKS_HOST` and `PLATFORM_REPO` (owner/eqds-agents); secret
`PLATFORM_REPO_TOKEN` (read access to the platform repo); secrets `DATABRICKS_CLIENT_ID` and `DATABRICKS_CLIENT_SECRET` in each of the GitHub
environments `dev` and `production` (the CI service principal; it needs permission to create tables in the dev traces schema).
