# make deploy        build ./app from the platform runtime + agent/, deploy the bundle, start the app
# make smoke         check /healthz and /info of the deployed app (add Q="question" for one real call)
PLATFORM ?= ../eqds-agents
TARGET ?= {{TARGET}}
PY ?= python3
Q ?=

.PHONY: build deploy smoke

build:
	$(PY) scripts/build.py --platform $(PLATFORM) --target $(TARGET)

deploy: build
	databricks bundle deploy -t $(TARGET)
	databricks bundle run -t $(TARGET) {{KEY}}

smoke:
	$(PY) scripts/smoke.py $(if $(Q),--invoke "$(Q)")
