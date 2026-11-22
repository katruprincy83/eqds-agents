"""job_bridge_worker: continuous bridge worker (design §5.6). Entry point of the Lakeflow job."""
import logging

from _common import get_spark, secret, setup

env, args = setup()
logging.basicConfig(level=logging.INFO)
from bridge_worker.worker import run  # noqa: E402

run(get_spark(), env, max_runtime_s=float(env.get("bridge_worker_max_runtime_s", 0)) or None, secret_fn=secret)
