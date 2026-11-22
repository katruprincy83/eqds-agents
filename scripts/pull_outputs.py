#!/usr/bin/env python
"""Pull bootstrap outputs (written by the `outputs` bootstrap step to the artifacts volume) into
.databricks/bundle/<target>/bootstrap-output.json so `bundle_vars.py` can feed them into the next deploy."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "packages" / "eqds_agentkit"), str(ROOT / "packages" / "eqds_llm")]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default="demo")
    ap.add_argument("--profile", default=None)
    a = ap.parse_args()
    from bootstrap.ctx import make_ctx

    ctx = make_ctx(a.target, str(ROOT), profile=a.profile)
    src = f"{ctx.env.volume_path('artifacts')}/bootstrap-output-{ctx.env.target}.json"
    data = json.loads(ctx.w.files.download(src).contents.read())
    dst = ROOT / ".databricks" / "bundle" / ctx.env.target / "bootstrap-output.json"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(data, indent=2))
    print(f"wrote {dst}: {sorted(data)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
