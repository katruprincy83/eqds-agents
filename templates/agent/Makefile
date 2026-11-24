# make deploy        build ./app from runtime/ (platform runtime snapshot) + agent/, deploy the bundle, bind the traces, start the app
# make smoke         check /healthz and /info of the deployed app (add Q="question" for one real call)
# make traces        bind this agent's experiment to the environment's UC trace location and grant the app (no-op when the platform
#                    profile has flags.use_uc_trace_storage off, i.e. production); `make deploy` runs it before the app starts
# Environments: TARGET=demo is production (branch main), TARGET=dev is dev (branch dev). CI sets TARGET from the branch.
PLATFORM ?= runtime
TARGET ?= {{TARGET}}
PY ?= python3
Q ?=

.PHONY: build deploy smoke traces

build:
	$(PY) scripts/build.py --platform $(PLATFORM) --target $(TARGET)

deploy: build
	databricks bundle deploy -t $(TARGET)
	$(PY) scripts/bind_traces.py --platform $(PLATFORM) --target $(TARGET)
	databricks bundle run -t $(TARGET) {{KEY}}

traces:
	$(PY) scripts/bind_traces.py --platform $(PLATFORM) --target $(TARGET)

smoke:
	$(PY) scripts/smoke.py --target $(TARGET) $(if $(Q),--invoke "$(Q)")
