"""job_env_bootstrap / job_env_teardown / `make bootstrap`: drives bootstrap/manifest.yaml (design §4.1 E9, §4.2).

    python src/bootstrap/run.py --target demo                    # all steps, in manifest order
    python src/bootstrap/run.py --target demo --step uc_functions
    python src/bootstrap/run.py --target demo --teardown         # reverse order; refuses data_profile=customer without --force-customer
"""
from __future__ import annotations

import argparse
import os
import sys
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))) if "__file__" in globals() else os.getcwd()


def main(argv=None, spark=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default=os.environ.get("EQDS_TARGET", "demo"))
    ap.add_argument("--root", default=os.environ.get("EQDS_ROOT"))
    ap.add_argument("--step", action="append", default=[])
    ap.add_argument("--skip", action="append", default=[])
    ap.add_argument("--teardown", action="store_true")
    ap.add_argument("--force-customer", action="store_true")
    ap.add_argument("--profile", default=None)
    a, _ = ap.parse_known_args(argv)
    root = a.root or ROOT
    os.environ["EQDS_ROOT"] = root
    for p in (root, os.path.join(root, "src"), os.path.join(root, "packages", "eqds_agentkit"), os.path.join(root, "packages", "eqds_llm")):
        if p not in sys.path:
            sys.path.insert(0, p)
    import yaml

    from bootstrap import steps as S
    from bootstrap.ctx import make_ctx

    ctx = make_ctx(a.target, root, spark=spark, profile=a.profile)
    manifest = yaml.safe_load(open(os.path.join(root, "bootstrap", "manifest.yaml")))
    plan = manifest["bootstrap_steps"]
    if a.teardown:
        if ctx.env.data_profile == "customer" and not a.force_customer:
            print("refusing to tear down a target with data_profile=customer (use --force-customer)")
            return 2
        plan = list(reversed(plan))
    failures = 0
    print(f"bootstrap target={ctx.env.target} host={ctx.env.host} {'TEARDOWN' if a.teardown else 'APPLY'}", flush=True)
    for st in plan:
        if (a.step and st["id"] not in a.step) or st["id"] in a.skip:
            continue
        when = st.get("when") or {}
        if any(ctx.env.get(k) not in v for k, v in when.items()):
            print(f"- {st['id']}: skipped (profile)", flush=True)
            continue
        fn = getattr(S, st["teardown" if a.teardown else "apply"])
        print(f"- {st['id']}", flush=True)
        try:
            fn(ctx)
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"  FAILED: {e}", flush=True)
            traceback.print_exc()
    print("bootstrap done" if not failures else f"bootstrap finished with {failures} failed step(s)", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
