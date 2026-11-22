# EQDS: one command path from a clean repo to a running environment (design §4.1 E10-E12, NFR-20..22).
#   make all TARGET=demo        # vars -> infra -> build -> deploy -> bootstrap -> start apps -> outputs
# Secrets: export EQDS_AZURE_SQL_USER / EQDS_AZURE_SQL_PASSWORD before `make infra` (they only ever land in the secret scope).
TARGET ?= demo
PY ?= python
# platform apps only: specialist agents have their own repos (python scripts/new_agent.py <key>) and are registered on the UI
APPS := bridge_mcp moderator ui
JOBS_SEED := seed_uc seed_docs seed_azure validate_seed

.PHONY: all vars infra build deploy bootstrap apps outputs preflight seed docs test manifest destroy teardown ask register registrar agent

all: vars infra build deploy bootstrap apps outputs

vars:
	$(PY) scripts/gen_app_resources.py
	$(PY) scripts/bundle_vars.py --target $(TARGET)

infra:
	$(PY) scripts/infra.py --target $(TARGET)

preflight:
	$(PY) scripts/preflight.py --target $(TARGET)

build:
	$(PY) scripts/build_apps.py --target $(TARGET)

deploy: vars build
	databricks bundle validate -t $(TARGET)
	databricks bundle deploy -t $(TARGET)

bootstrap:
	$(PY) -m src.bootstrap.run --target $(TARGET)

# bundle deploy uploads source; `bundle run <app>` deploys it and starts the app (apps that are already running restart)
apps:
	@for a in $(APPS); do databricks bundle run -t $(TARGET) $$a > /tmp/eqds_run_$$a.log 2>&1 & sleep 3; done; wait
	@grep -L "started successfully" /tmp/eqds_run_*.log || true

# Synthetic data (data_profile=synthetic): deterministic, then self-checks
seed:
	@for j in $(JOBS_SEED); do databricks bundle run -t $(TARGET) $$j || exit 1; done
	databricks bundle run -t $(TARGET) docs_index
	databricks bundle run -t $(TARGET) ingest_news
	$(PY) -m src.bootstrap.run --target $(TARGET) --step vector_search --step genie --step grants --step outputs

# pulls bootstrap outputs (genie space id, urls) so the next deploy picks them up
outputs:
	$(PY) scripts/pull_outputs.py --target $(TARGET)

manifest:
	$(PY) scripts/manifest_check.py

test:
	$(PY) -m pytest -q tests

# self-service: make register KEY=news | make register ACTION=disable KEY=news | make register ACTION=list
# new agent project: make agent KEY=fx  (writes ../eqds-agent-fx)
ACTION ?= register
register:
	$(PY) scripts/register_specialist.py $(ACTION) $(KEY) --target $(TARGET)

agent:
	$(PY) scripts/new_agent.py $(KEY) --target $(TARGET)

# one-time: the identity that runs UI-triggered registration (creates the SP, records its id, applies its grants)
registrar:
	$(PY) scripts/registrar.py --target $(TARGET)

ask:
	@TOK=$$(databricks auth token -o json | $(PY) -c "import json,sys;print(json.load(sys.stdin)['access_token'])"); \
	URL=$$(databricks apps get $$($(PY) -c "import yaml;e=yaml.safe_load(open('config/env/$(TARGET).yaml'));print(e['name_prefix']+'-moderator'+e.get('suffix',''))") -o json | $(PY) -c "import json,sys;print(json.load(sys.stdin)['url'])"); \
	$(PY) scripts/ask.py --url $$URL --token $$TOK "$(Q)"

teardown:
	$(PY) -m src.bootstrap.run --target $(TARGET) --teardown

destroy: teardown
	databricks bundle destroy -t $(TARGET) --auto-approve
