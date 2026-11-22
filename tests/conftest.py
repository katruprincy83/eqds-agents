import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (ROOT, os.path.join(ROOT, "src"), os.path.join(ROOT, "packages", "eqds_agentkit"), os.path.join(ROOT, "packages", "eqds_llm")):
    if p not in sys.path:
        sys.path.insert(0, p)
os.environ.setdefault("EQDS_ROOT", ROOT)
