"""Ask the moderator (local or deployed) and stream the run. usage: python scripts/ask.py [--url http://localhost:8802] [--rounds 3] "question" """
import argparse, json, sys, time
import httpx
ap = argparse.ArgumentParser(); ap.add_argument("question"); ap.add_argument("--url", default="http://localhost:8802"); ap.add_argument("--rounds", type=int, default=3)
ap.add_argument("--token", default=None); ap.add_argument("--specialists", default=""); ap.add_argument("--verbose", action="store_true"); a = ap.parse_args()
h = {"Authorization": f"Bearer {a.token}"} if a.token else {}
r = httpx.post(f"{a.url}/runs", json={"question": a.question, "rounds": a.rounds, "specialists": a.specialists.split(",") if a.specialists else None}, headers=h, timeout=30); r.raise_for_status()
run = r.json(); print("run", run); t0 = time.time()
last_id = "0"; finished = False
while not finished:
    try:
        with httpx.stream("GET", f"{a.url}/runs/{run['run_id']}/events", headers={**h, "last-event-id": last_id}, timeout=900) as s:
            ev = None
            for line in s.iter_lines():
                if line.startswith("id:"): last_id = line[3:].strip()
                elif line.startswith("event:"): ev = line[6:].strip()
                elif line.startswith("data:"):
                    d = json.loads(line[5:])
                    t = f"{time.time()-t0:5.0f}s"
                    if ev == "done": finished = True
                    if ev == "stage": print(t, "STAGE", d["stage"], d["status"])
                    elif ev == "message": print(t, f"MSG r{d['round']} {d['from']}→{','.join(d['to'])}: {d['first_line'][:140]}")
                    elif ev == "who_opens": print(t, "WHO", d["selected"], "|", d["reformulated"][:150])
                    elif ev == "map": print(t, "MAP", [(p['id'], p['verdict'], p['status']) for p in d["points"]])
                    elif ev == "verdict": print(t, "VERDICT", d["point_id"], d["verdict"], d["status"], d["text"][:120])
                    elif ev == "answer": print(t, "ANSWER", d["stance"]); [print("   ", p) for p in d["paragraphs"]]
                    elif ev == "ask_next": print(t, "NEXT", d["questions"])
                    elif ev == "usage": print(t, "USAGE", d)
                    elif ev == "activity" and a.verbose: print(t, "  act", d["component"], d["label"], d["detail"][:80])
                    elif ev in ("status", "done", "error", "deep_dive"): print(t, ev.upper(), json.dumps(d)[:200])
        finished = True if finished else (httpx.get(f"{a.url}/runs/{run['run_id']}", headers=h).json().get("status") in ("DONE", "FAILED", "CANCELLED"))
    except (httpx.RemoteProtocolError, httpx.ReadError, httpx.ReadTimeout):
        print("  (stream dropped; reconnecting)")
res = httpx.get(f"{a.url}/runs/{run['run_id']}", headers=h).json()
print("status", res["status"], res.get("error"))
if res.get("result"): print("citation validation:", res["result"]["answer"].get("validation"), "sources:", len(res["result"]["sources"]))
