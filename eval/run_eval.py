"""Run the evaluation: ask the moderator each `eval_questions` row, score, log to MLflow, gate on thresholds (design §11.2).

    python eval/run_eval.py --target demo --subset smoke|full [--moderator-url URL]
Exit code 1 when a threshold or the baseline-drop rule fails (the CI gate)."""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
from concurrent.futures import ThreadPoolExecutor

ROOT = pathlib.Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "src", ROOT / "packages/eqds_agentkit", ROOT / "packages/eqds_llm", ROOT / "eval"):
    sys.path.insert(0, str(p))
import httpx  # noqa: E402
from eqds_agentkit.auth import job_caller_headers  # noqa: E402
import yaml  # noqa: E402

import scorers  # noqa: E402
from eqds_agentkit.envcfg import Env  # noqa: E402
from eqds_agentkit.sql import run_statement  # noqa: E402
from eqds_llm import LLM, load_model_config  # noqa: E402


def load_questions(w, env: Env, subset: str, th: dict) -> list[dict]:
    wh = next((x.id for x in w.warehouses.list() if x.name == env.app_name("wh")), None)
    rows = run_statement(w, wh, f"SELECT qid, question, expected_facts, tags FROM {env.uc}.eval_questions ORDER BY qid")
    qs = [{"qid": r["qid"], "question": r["question"], "facts": json.loads(r["expected_facts"]) if isinstance(r["expected_facts"], str) else list(r["expected_facts"]),
           "group": json.loads(r["tags"])["group"]} for r in rows]
    if subset == "smoke":
        per, out = dict(th["smoke"]["per_group"]), []
        for q in qs:
            if per.get(q["group"], 0) > 0:
                out.append(q)
                per[q["group"]] -= 1
        return out
    return qs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default=os.environ.get("EQDS_TARGET", "demo"))
    ap.add_argument("--subset", default="smoke")
    ap.add_argument("--rounds", type=int, default=0)
    ap.add_argument("--moderator-url", default="")
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--no-gate", action="store_true")
    a = ap.parse_args()
    env = Env.from_target(a.target, ROOT)
    th = yaml.safe_load((ROOT / "eval/thresholds.yaml").read_text())
    from databricks.sdk import WorkspaceClient

    w = WorkspaceClient()
    url = (a.moderator_url or w.apps.get(env.app_name("moderator")).url).rstrip("/")
    rounds = a.rounds or th[a.subset if a.subset in th else "full"].get("rounds", 3)
    llm = LLM(load_model_config(ROOT / env.get("models_config", f"config/models.{env.target}.yaml"), env.prefix, env.suffix, env.catalog, env.schema))
    qs = load_questions(w, env, a.subset, th)
    print(f"{len(qs)} questions, {rounds} round(s) against {url}", flush=True)

    def one(q):
        hdr = {**job_caller_headers(w, env), "x-eqds-user": "eval"}
        try:
            r = httpx.post(f"{url}/invocations", json={"input": [{"role": "user", "content": q["question"]}], "custom_inputs": {"rounds": rounds, "caller": "eval", "user_id": "eval"}}, headers=hdr, timeout=900)
            r.raise_for_status()
            res = r.json()["custom_outputs"]
            m = {"correctness": scorers.fact_recall(llm, res["answer"]["text"], q["facts"]), "citation_validity": scorers.citation_validity(res),
                 "claim_citation_coverage": scorers.claim_citation_coverage(res),
                 "within_budget": scorers.within_budget(res, th["budget"]["seconds_max"], th["budget"]["cost_usd_max"])}
            m.update(cost_usd=res["usage"]["cost_usd"], duration_s=res["usage"]["duration_s"])
            print(q["qid"], {k: round(v, 2) for k, v in m.items()}, flush=True)
            return {**q, **m, "answer": res["answer"]["text"][:1500], "ok": True}
        except Exception as e:  # noqa: BLE001
            print(q["qid"], "ERROR", str(e)[:160], flush=True)
            return {**q, "ok": False, "error": str(e)[:300], "correctness": 0.0, "citation_validity": 0.0, "claim_citation_coverage": 0.0, "within_budget": 0.0}

    with ThreadPoolExecutor(a.concurrency) as ex:
        out = list(ex.map(one, qs))
    agg = {k: sum(o[k] for o in out) / len(out) for k in th["thresholds"]}
    agg["error_rate"] = sum(not o["ok"] for o in out) / len(out)
    import mlflow

    mlflow.set_tracking_uri("databricks")
    mlflow.set_experiment(experiment_name=env.experiment_path)
    prev = mlflow.search_runs(filter_string="tags.eqds_eval = 'true' AND tags.passed = 'true' AND tags.subset = '%s'" % a.subset, order_by=["start_time DESC"], max_results=1)
    fails = [f"{k} {agg[k]:.2f} < {v}" for k, v in th["thresholds"].items() if agg[k] < v]
    if not prev.empty:
        fails += [f"{k} dropped {float(prev.iloc[0].get('metrics.' + k, agg[k])) - agg[k]:.2f} vs baseline" for k in th["thresholds"]
                  if float(prev.iloc[0].get("metrics." + k, agg[k])) - agg[k] > th["baseline_drop"]]
    with mlflow.start_run(run_name=f"eval-{a.subset}-{env.target}") as run:
        mlflow.set_tags({"eqds_eval": "true", "subset": a.subset, "target": env.target, "passed": str(not fails).lower(), "app_version": os.environ.get("EQDS_APP_VERSION", "dev")})
        mlflow.log_params({"n_questions": len(out), "rounds": rounds})
        mlflow.log_metrics({k: float(v) for k, v in agg.items()})
        mlflow.log_dict({"results": out}, "eval_results.json")
    print("RESULT", json.dumps({k: round(v, 3) for k, v in agg.items()}), "FAILS:", fails, "run", run.info.run_id)
    return 0 if (not fails or a.no_gate) else 1


if __name__ == "__main__":
    raise SystemExit(main())
