"""Moderator: LangGraph StateGraph (design §5.2).

reformulate → select_specialists → (Send fan-out) call_specialist → map_debate ⇄ deep_dive → verdicts → synthesize_answer
→ suggest_next → finalize. A Postgres checkpointer on Lakebase (`thread_id = run_id`) makes every run resumable; each node
writes its SSE events to `ops.run_events` BEFORE returning, so any replica can stream and a reconnect resumes at Last-Event-ID.
"""
from __future__ import annotations

import json
import logging
import operator
import re
import time
from typing import Annotated, Any, TypedDict

import httpx
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from pydantic import BaseModel, Field

from eqds_agentkit import auth, contract, tracing
from eqds_agentkit.memory import Store
from eqds_agentkit.registry import Registry
from eqds_agentkit.skills import PromptStore
from eqds_agentkit.sql import lit, run_statement
from eqds_llm import LLM

log = logging.getLogger("moderator")
TAG = re.compile(r"\[([A-Z]{1,3}\d+)\]")
CLASS_ABORT: set[str] = set()   # run ids whose worker thread must "crash" (developer console: AC-04 demonstration)


class Crash(Exception):
    """Simulated process death: no status update, no heartbeat; the reaper resumes the run from its checkpoint."""


def _merge(a: dict, b: dict) -> dict:
    return {**(a or {}), **(b or {})}


def _add_usage(a: dict, b: dict) -> dict:
    a, b = a or {}, b or {}
    return {k: round(a.get(k, 0) + b.get(k, 0), 6) for k in set(a) | set(b)}


class DebateState(TypedDict, total=False):
    run_id: str
    conversation_id: str
    user_id: str
    caller: str
    question: str
    reformulated: str
    company_id: str | None
    manual_specialists: list[str] | None
    max_rounds: int
    round: int
    selected: list[str]
    briefs: dict[str, str]
    messages: Annotated[list[dict], operator.add]
    points: list[dict]
    sources: Annotated[dict, _merge]
    verdicts: list[dict]
    answer: dict | None
    ask_next: list[str]
    usage: Annotated[dict, _add_usage]
    budget: dict
    started_at: float
    memory_turns: int
    probes: list[dict]
    result: dict | None


# ── structured outputs ──────────────────────────────────────────────────────────────────────────
class Reform(BaseModel):
    reformulated: str
    company_names: list[str] = Field(default_factory=list)


class SelItem(BaseModel):
    key: str
    brief: str


class Selection(BaseModel):
    selected: list[SelItem]


class PointM(BaseModel):
    id: str
    topic: str
    claim: str
    stances: dict[str, str] = Field(default_factory=dict)
    gaps: list[str] = Field(default_factory=list)
    needs_followup: bool = False


class MapOut(BaseModel):
    points: list[PointM]


class Probe(BaseModel):
    point_id: str = ""
    to: list[str]
    text: str


class Probes(BaseModel):
    probes: list[Probe]


class VerdictM(BaseModel):
    point_id: str
    verdict: str
    status: str
    text: str


class Verdicts(BaseModel):
    verdicts: list[VerdictM]


class AnswerM(BaseModel):
    stance: str
    paragraphs: list[str]


class NextM(BaseModel):
    questions: list[str]


def first_line(text: str, n: int = 160) -> str:
    t = (text or "").strip().split("\n")[0]
    return t if len(t) <= n else t[: n - 1].rsplit(" ", 1)[0] + "…"


def provisional_verdict(stances: dict[str, str]) -> tuple[str, str]:
    sup = [k for k, v in stances.items() if v == "support"]
    opp = [k for k, v in stances.items() if v == "oppose"]
    par = [k for k, v in stances.items() if v == "partial"]
    if sup and opp:
        return "OPEN", "OPEN"
    if len(sup) >= 2 and not opp:
        return "CONSENSUS", "RESOLVED"
    if len(opp) >= 2 and not sup:
        return "CONSENSUS", "RESOLVED"
    if len(sup) + len(opp) + len(par) >= 1:
        return "ONE_SOURCE", "RESOLVED" if not par else "OPEN"
    return "OPEN", "OPEN"


def sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z\[])", text.strip()) if s.strip()]


def check_citations(paragraphs: list[str], valid: set[str]) -> dict:
    """NFR-12: every sentence with a number or factual claim must carry at least one tag that exists in `sources`."""
    uncited, bad_tags, total_factual = [], [], 0
    for para in paragraphs:
        for s in sentences(para):
            tags = TAG.findall(s)
            bad_tags += [t for t in tags if t not in valid]
            if re.search(r"\d", s):
                total_factual += 1
                if not any(t in valid for t in tags):
                    uncited.append(s)
    cov = 1.0 if not total_factual else (total_factual - len(uncited)) / total_factual
    return {"ok": not uncited and not bad_tags, "uncited": uncited, "bad_tags": sorted(set(bad_tags)), "coverage": round(cov, 3)}


class Moderator:
    def __init__(self, env, llm: LLM, store: Store, registry: Registry, prompts: PromptStore, pool_for_saver, w=None, warehouse_id: str | None = None,
                 http: httpx.Client | None = None):
        self.env, self.llm, self.store, self.reg, self.prompts, self.w, self.wh = env, llm, store, registry, prompts, w, warehouse_id
        self.http = http or httpx.Client(timeout=httpx.Timeout(10.0, read=240.0))
        self.pv = env.get("moderator.prompt_version", "1.0.0")
        self.defaults = {"cost_usd_max": float(env.get("budget.cost_usd_max", 2.0)), "seconds_max": float(env.get("budget.seconds_max", 480))}
        self.max_specialists = int(env.get("moderator.max_specialists", 6))
        self.saver = PostgresSaver(pool_for_saver)
        self.graph = self._build().compile(checkpointer=self.saver)

    # ── graph ──────────────────────────────────────────────────────────────────────────────
    def _build(self) -> StateGraph:
        g = StateGraph(DebateState)
        g.add_node("reformulate", self._guard(self.n_reformulate))
        g.add_node("select_specialists", self._guard(self.n_select))
        g.add_node("call_specialist", self._guard(self.n_call_specialist))
        g.add_node("map_debate", self._guard(self.n_map))
        g.add_node("deep_dive", self._guard(self.n_deep_dive))
        g.add_node("verdicts", self._guard(self.n_verdicts))
        g.add_node("synthesize_answer", self._guard(self.n_synthesize))
        g.add_node("suggest_next", self._guard(self.n_suggest))
        g.add_node("finalize", self._guard(self.n_finalize))
        g.add_edge(START, "reformulate")
        g.add_edge("reformulate", "select_specialists")
        g.add_conditional_edges("select_specialists", self.fan_out_opening, ["call_specialist", "map_debate"])
        g.add_edge("call_specialist", "map_debate")
        g.add_conditional_edges("map_debate", self.after_map, ["deep_dive", "verdicts"])
        g.add_conditional_edges("deep_dive", self.fan_out_probes, ["call_specialist", "map_debate"])
        g.add_edge("verdicts", "synthesize_answer")
        g.add_edge("synthesize_answer", "suggest_next")
        g.add_edge("suggest_next", "finalize")
        g.add_edge("finalize", END)
        return g

    def _guard(self, fn):
        def wrapper(state):
            if state.get("run_id") in CLASS_ABORT:
                raise Crash(state["run_id"])
            return fn(state)
        wrapper.__name__ = fn.__name__
        return wrapper

    # ── helpers ────────────────────────────────────────────────────────────────────────────
    def ev(self, st: DebateState, type_: str, payload: dict) -> None:
        self.store.add_event(st["run_id"], type_, payload)

    def stage(self, st, stage: str, status: str) -> None:
        self.ev(st, "stage", {"stage": stage, "status": status})
        if status == "active":
            self.store.update_run(st["run_id"], stage=stage)

    def activity(self, st, component: str, zone: str, label: str, detail: str = "", link: dict | None = None) -> None:
        self.ev(st, "activity", {"component": component, "zone": zone, "label": label, "detail": detail, "link": link or {"kind": "app", "ref": component}})

    def prompt(self, name: str, **kw) -> str:
        return self.prompts.load("moderator", name, self.pv).format(**kw)

    def ask(self, st, tier: str, name: str, schema, user_extra: str = "", **kw):
        """One LLM call on a tier with a structured response. → (parsed, usage delta)."""
        res = self.llm.chat(tier, [{"role": "system", "content": self.prompt(name, **kw)}, {"role": "user", "content": user_extra or "Return the JSON now."}],
                            response_schema=schema, max_tokens=3500 if tier == "reasoning" else 1500)
        self.activity(st, "eqds-moderator", "model", f"llm.{tier} · {name}", f"{res.endpoint} · {res.usage.input} in / {res.usage.output} out · ${res.cost_usd:.3f}",
                      {"kind": "endpoint", "ref": res.endpoint or ""})
        return res.parsed, {"input_tokens": res.usage.input, "output_tokens": res.usage.output, "cost_usd": res.cost_usd}

    def transcript(self, st, rounds: set[int] | None = None, max_chars: int = 14000) -> str:
        out = []
        for m in st.get("messages", []):
            if m["from"] == "moderator":
                continue
            if rounds and m["round"] not in rounds:
                continue
            cl = "\n  ".join(f"({c['stance']}) {c['text'][:420]} {' '.join('[' + t + ']' for t in c.get('citations', []))}" for c in m.get("claims", []))
            out.append(f"[round {m['round']}] {m['from']}:\n  {cl}" if cl else f"[round {m['round']}] {m['from']}: {m['text'][:600]}")
        t = "\n".join(out)
        return t if len(t) <= max_chars else t[:max_chars] + "\n…(truncated)"

    def registry_text(self, keys: list[str] | None = None) -> str:
        return "\n".join(f"- {s.key}: {s.description}" for s in self.reg.all() if not keys or s.key in keys)

    def elapsed(self, st) -> float:
        return time.time() - st.get("started_at", time.time())

    def budget_pct(self, st) -> float:
        b = st.get("budget") or self.defaults
        return max(st.get("usage", {}).get("cost_usd", 0) / max(b["cost_usd_max"], 1e-9), self.elapsed(st) / max(b["seconds_max"], 1e-9))

    def emit_usage(self, st) -> None:
        u, b = st.get("usage", {}), st.get("budget") or self.defaults
        self.ev(st, "usage", {"cost_usd": round(u.get("cost_usd", 0), 4), "duration_s": int(self.elapsed(st)), "tokens_in": int(u.get("input_tokens", 0)),
                              "tokens_out": int(u.get("output_tokens", 0)), "budget": {**b, "pct_used": round(self.budget_pct(st) * 100, 1)}})

    # ── nodes ──────────────────────────────────────────────────────────────────────────────
    def n_reformulate(self, st: DebateState) -> dict:
        self.ev(st, "status", {"status": "RUNNING"})
        self.stage(st, "who_opens", "active")
        turns = self.store.recent_turns(st["conversation_id"], 5) if st.get("conversation_id") else []
        mem = "\n".join(f"- Q: {t['question']}\n  A: {t['answer_summary']}" for t in turns) or "(none)"
        if turns:
            self.activity(st, "eqds-moderator", "state", "memory loaded", f"{len(turns)} earlier turns", {"kind": "lakebase", "ref": "mem.conversation"})
        r, u = self.ask(st, "fast", "reformulate", Reform, user_extra=st["question"], memory=mem)
        company_id = st.get("company_id")
        if not company_id and r.company_names and self.wh:
            try:
                rows = run_statement(self.w, self.wh, f"SELECT company_id FROM {self.env.uc}.ref_companies WHERE lower(name) = lower({lit(r.company_names[0])}) "
                                                      f"OR lower(name) LIKE lower({lit('%' + r.company_names[0] + '%')}) ORDER BY length(name) LIMIT 1")
                company_id = rows[0]["company_id"] if rows else None
            except Exception as e:  # noqa: BLE001
                log.warning("company resolution failed: %s", str(e)[:100])
        return {"reformulated": r.reformulated, "company_id": company_id, "usage": u, "round": 1, "memory_turns": len(turns)}

    def n_select(self, st: DebateState) -> dict:
        manual = st.get("manual_specialists")
        active = {s.key for s in self.reg.all()}
        if manual:
            keys = [k for k in manual if k in active]
            r, u = self.ask(st, "fast", "select", Selection, user_extra="Write briefs for exactly these specialists.", max_specialists=len(keys), registry=self.registry_text(keys),
                            question=st["reformulated"], company=st.get("company_id") or "none")
            briefs = {i.key: i.brief for i in r.selected if i.key in keys}
            for k in keys:
                briefs.setdefault(k, st["reformulated"])
        else:
            r, u = self.ask(st, "fast", "select", Selection, max_specialists=self.max_specialists, registry=self.registry_text(), question=st["reformulated"],
                            company=st.get("company_id") or "none")
            briefs = {i.key: i.brief for i in r.selected if i.key in active}
            keys = list(briefs)[: self.max_specialists]
            briefs = {k: briefs[k] for k in keys}
        if not keys:
            keys = [s.key for s in self.reg.all()][:3]
            briefs = {k: st["reformulated"] for k in keys}
        self.ev(st, "who_opens", {"reformulated": st["reformulated"], "selected": keys, "briefs": briefs, "selection": "manual" if manual else "auto",
                                  "memory_turns_used": st.get("memory_turns", 0)})
        self.stage(st, "who_opens", "done")
        self.stage(st, "opening_round", "active")
        self.activity(st, "eqds-moderator", "state", "checkpoint written", f"thread {st['run_id']}", {"kind": "lakebase", "ref": "lg.checkpoints"})
        return {"selected": keys, "briefs": briefs, "usage": u}

    def fan_out_opening(self, st: DebateState):
        sends = [Send("call_specialist", {**{k: st[k] for k in ("run_id", "conversation_id", "user_id", "caller", "company_id", "reformulated") if k in st},
                                          "key": k, "brief": st["briefs"][k], "round": 1, "prior": []}) for k in st["selected"]]
        return sends or "map_debate"

    def n_call_specialist(self, st: dict) -> dict:
        key, brief, rnd = st["key"], st["brief"], st["round"]
        spec = self.reg.get(key)
        # moderator → specialist message
        mod_msg = {"round": rnd, "from": "moderator", "to": [key], "text": brief, "claims": [], "sources": []}
        self.ev(st, "message", {"round": rnd, "from": "moderator", "to": [key], "first_line": first_line(brief), "text": brief, "claims": [], "tool_calls": []})
        if spec is None or not spec.url:
            out = contract.error_outputs(key, f"Specialist {key} is not registered or has no endpoint.", "not_addressed")
        else:
            body = contract.request(brief, run_id=st["run_id"], round=rnd, key=key, conversation_id=st.get("conversation_id", ""), user_id=st.get("user_id", ""),
                                    caller="moderator", company_id=st.get("company_id"), prior_findings=st.get("prior", []))
            self.activity(st, f"eqds-sp-{key}", "serverless", "POST /invocations", f"→ {spec.url}", {"kind": "app", "ref": f"eqds-sp-{key}"})
            with tracing.span(f"specialist.{key}", "AGENT", inputs={"brief": brief[:4000], "round": rnd}, run_id=st["run_id"], round=rnd, specialist=key,
                              endpoint=spec.url) as sp:
                # The specialist records its own trace in ITS experiment. We pass our trace id so that trace can point back, and we
                # keep its trace id on this span (`child_trace_id`) so the two can be followed in either direction.
                hdrs = {**auth.bearer(self.w), "x-eqds-user": st.get("user_id", ""), "x-eqds-run-id": st["run_id"]}
                if (tid := tracing.current_trace_id()):
                    hdrs["x-eqds-parent-trace"] = tid
                try:
                    r = self.http.post(f"{spec.url.rstrip('/')}/invocations", json=body, timeout=httpx.Timeout(10.0, read=float(spec.timeout_s) + 15), headers=hdrs)
                    r.raise_for_status()
                    out = contract.parse_response(r.json())
                except Exception as e:  # noqa: BLE001  FR-OR-07: a failing specialist degrades to not_addressed
                    log.warning("specialist %s failed: %s", key, str(e)[:160])
                    out = contract.error_outputs(key, f"{spec.display_name} did not respond ({type(e).__name__}).", "not_addressed")
                    if sp is not None:
                        sp.set_attribute("error", f"{type(e).__name__}: {str(e)[:200]}")
                if sp is not None:
                    try:
                        if out.trace_id:
                            sp.set_attribute("child_trace_id", out.trace_id)
                        sp.set_outputs({"text": out.text[:4000], "claims": len(out.claims), "sources": len(out.sources), "cached": out.cached,
                                        "tool_calls": [{"tool": t.tool, "server": t.server, "latency_ms": t.latency_ms} for t in out.tool_calls],
                                        "usage": out.usage.model_dump(mode="json")})
                    except Exception:  # noqa: BLE001
                        pass
        for a in out.activity:
            self.ev(st, "activity", a)
        tcs = [t.model_dump() for t in out.tool_calls]
        msg = {"round": rnd, "from": key, "to": ["moderator"], "text": out.text, "claims": [c.model_dump() for c in out.claims],
               "sources": [s.id for s in out.sources], "first_line": first_line(out.text)}
        self.ev(st, "message", {"round": rnd, "from": key, "to": ["moderator"], "first_line": first_line(out.text), "text": out.text,
                                "claims": msg["claims"], "tool_calls": tcs, "cached": out.cached})
        src = {s.id: s.model_dump(exclude_none=True) for s in out.sources}
        return {"messages": [mod_msg, msg], "sources": src,
                "usage": {"input_tokens": out.usage.input_tokens, "output_tokens": out.usage.output_tokens, "cost_usd": out.usage.cost_usd}}

    def n_map(self, st: DebateState) -> dict:
        if st.get("round", 1) <= 1:
            self.stage(st, "opening_round", "done")
        self.stage(st, "map", "active")
        keys = sorted({m["from"] for m in st["messages"] if m["from"] != "moderator"})
        r, u = self.ask(st, "reasoning", "map", MapOut, question=st["reformulated"], keys=", ".join(keys), transcript=self.transcript(st))
        points = []
        for i, p in enumerate(r.points, 1):
            stances = {k: (v if v in ("support", "oppose", "partial", "not_addressed") else "not_addressed") for k, v in p.stances.items() if k in keys}
            verdict, status = provisional_verdict(stances)
            points.append({"id": p.id or f"p{i}", "topic": p.topic, "claim": p.claim, "stances": stances, "gaps": p.gaps, "needs_followup": p.needs_followup,
                           "verdict": verdict, "status": status})
        self.ev(st, "map", {"points": [{k: p[k] for k in ("id", "verdict", "status", "claim", "stances", "topic")} for p in points]})
        self.stage(st, "map", "done")
        self.emit_usage(st)
        return {"points": points, "usage": u}

    def after_map(self, st: DebateState) -> str:
        open_pts = [p for p in st.get("points", []) if p["status"] == "OPEN" or p.get("needs_followup")]
        over = self.budget_pct(st) >= 0.8
        if over and open_pts and st.get("round", 1) < st.get("max_rounds", 1):
            self.activity(st, "eqds-moderator", "state", "budget check", f"{self.budget_pct(st) * 100:.0f}% used: skipping to verdicts", {"kind": "lakebase", "ref": "ops.runs"})
        if st.get("round", 1) < st.get("max_rounds", 1) and open_pts and not over:
            return "deep_dive"
        return "verdicts"

    def n_deep_dive(self, st: DebateState) -> dict:
        rnd = st["round"] + 1
        # the debate roster is the set the moderator opened (who_opens): follow-up probes go only to those specialists,
        # never to other registered agents (a manual selection stays a manual selection for the whole run)
        roster = list(st.get("selected") or [])
        self.stage(st, "deep_dive", "active")
        if not roster:
            self.ev(st, "deep_dive", {"round": rnd, "probes": []})
            return {"round": rnd, "probes": [], "usage": {}}
        open_pts = [p for p in st["points"] if p["status"] == "OPEN" or p.get("needs_followup")]
        txt = "\n".join(f"- {p['id']} ({p['topic']}): {p['claim']} | stances: {p['stances']} | gaps: {p['gaps']}" for p in open_pts)
        r, u = self.ask(st, "reasoning", "deepdive", Probes, max_probes=3, registry=self.registry_text(roster), open_points=txt)
        probes = [{"point_id": p.point_id, "to": [k for k in p.to if k in roster][:3], "text": p.text} for p in r.probes if any(k in roster for k in p.to)][:3]
        prior = [{"from": m["from"], "claim": (m["claims"][0]["text"] if m["claims"] else m["text"][:200]), "tags": (m["claims"][0].get("citations", []) if m["claims"] else [])}
                 for m in st["messages"] if m["from"] != "moderator" and m["round"] == st["round"]][:8]
        self.ev(st, "deep_dive", {"round": rnd, "probes": probes})
        return {"round": rnd, "probes": [{**p, "prior": prior} for p in probes], "usage": u}

    def fan_out_probes(self, st: DebateState):
        sends = []
        for p in st.get("probes", []):
            for k in p["to"]:
                sends.append(Send("call_specialist", {**{kk: st[kk] for kk in ("run_id", "conversation_id", "user_id", "caller", "company_id", "reformulated") if kk in st},
                                                      "key": k, "brief": p["text"], "round": st["round"], "prior": p.get("prior", [])}))
        return sends or "map_debate"

    def n_verdicts(self, st: DebateState) -> dict:
        self.stage(st, "deep_dive", "done") if st.get("round", 1) > 1 else None
        self.stage(st, "verdicts", "active")
        valid = ", ".join(sorted(st.get("sources", {}))) or "none"
        pts = "\n".join(f"- {p['id']} ({p['topic']}): {p['claim']} | stances: {p['stances']} | gaps: {p['gaps']} | provisional: {p['verdict']}/{p['status']}" for p in st.get("points", []))
        r, u = self.ask(st, "reasoning", "verdicts", Verdicts, question=st["reformulated"], tags=valid, points=pts, transcript=self.transcript(st))
        by_id = {v.point_id: v for v in r.verdicts}
        verdicts = []
        for p in st.get("points", []):
            v = by_id.get(p["id"])
            if not v:
                continue
            verdict = v.verdict.upper() if v.verdict.upper() in ("CONSENSUS", "ONE_SOURCE", "OPEN") else p["verdict"]
            status = v.status.upper() if v.status.upper() in ("RESOLVED", "OPEN") else p["status"]
            verdicts.append({"point_id": p["id"], "verdict": verdict, "status": status, "text": v.text})
            self.ev(st, "verdict", verdicts[-1])
        self.stage(st, "verdicts", "done")
        points = [{**p, **{k: next((v[k] for v in verdicts if v["point_id"] == p["id"]), p[k]) for k in ("verdict", "status")}} for p in st.get("points", [])]
        return {"verdicts": verdicts, "points": points, "usage": u}

    def evidence_text(self, st, limit: int = 12000) -> str:
        lines = []
        for m in st["messages"]:
            if m["from"] == "moderator":
                continue
            for c in m.get("claims", []):
                if c["stance"] in ("support", "oppose", "partial") and c.get("citations"):
                    lines.append(f"- ({m['from']}) {c['text']} {' '.join('[' + t + ']' for t in c['citations'])}")
        t = "\n".join(lines)
        return t[:limit]

    def n_synthesize(self, st: DebateState) -> dict:
        self.stage(st, "answer", "active")
        valid = set(st.get("sources", {}))
        vtxt = "\n".join(f"- {v['point_id']} [{v['verdict']}/{v['status']}] {v['text']}" for v in st.get("verdicts", []))
        r, u = self.ask(st, "reasoning", "synthesize", AnswerM, tags=", ".join(sorted(valid)) or "none", question=st["reformulated"], verdicts=vtxt, evidence=self.evidence_text(st))
        chk = check_citations(r.paragraphs, valid)
        repairs = 0
        if not chk["ok"]:
            problems = [f"uncited: {s}" for s in chk["uncited"][:8]] + [f"unknown tag: {t}" for t in chk["bad_tags"]]
            self.activity(st, "eqds-moderator", "model", "citation validator", f"repair pass · {len(problems)} problems", {"kind": "endpoint", "ref": "reasoning"})
            r2, u2 = self.ask(st, "reasoning", "repair", AnswerM, tags=", ".join(sorted(valid)), problems="\n".join(problems),
                              answer=json.dumps({"stance": r.stance, "paragraphs": r.paragraphs}), evidence=self.evidence_text(st))
            u = _add_usage(u, u2)
            repairs = 1
            chk2 = check_citations(r2.paragraphs, valid)
            if chk2["coverage"] >= chk["coverage"] and len(chk2["bad_tags"]) <= len(chk["bad_tags"]):
                r, chk = r2, chk2
        paras = [TAG.sub(lambda m: m.group(0) if m.group(1) in valid else "", p) for p in r.paragraphs]
        ans = {"stance": r.stance, "paragraphs": paras, "text": "\n\n".join(paras),
               "validation": {"citation_coverage": chk["coverage"], "passed": chk["ok"], "repairs": repairs, "uncited": chk["uncited"][:5]}}
        self.ev(st, "answer", {"stance": ans["stance"], "paragraphs": paras})
        self.stage(st, "answer", "done")
        return {"answer": ans, "usage": u}

    def n_suggest(self, st: DebateState) -> dict:
        r, u = self.ask(st, "fast", "suggest", NextM, question=st["reformulated"], answer=st["answer"]["text"][:3000])
        qs = [q for q in r.questions if q][:3]
        self.ev(st, "ask_next", {"questions": qs})
        return {"ask_next": qs, "usage": u}

    def n_finalize(self, st: DebateState) -> dict:
        self.emit_usage(st)
        u = st.get("usage", {})
        stance_by_pt = {p["id"]: p.get("stances", {}) for p in st.get("points", [])}
        verdict_by = {v["point_id"]: v for v in st.get("verdicts", [])}
        points = [{"id": p["id"], "topic": p["topic"], "verdict": verdict_by.get(p["id"], p)["verdict"], "status": verdict_by.get(p["id"], p)["status"],
                   "text": verdict_by.get(p["id"], {}).get("text", p["claim"]), "claim": p["claim"], "stances": stance_by_pt[p["id"]], "gaps": p.get("gaps", [])} for p in st.get("points", [])]
        msgs = [{"round": m["round"], "from": m["from"], "to": m["to"], "text": m["text"], "claims": m.get("claims", [])} for m in st["messages"]]
        trace_id = tracing.tag_trace(run_id=st["run_id"], conversation_id=st.get("conversation_id"), user_id=st.get("user_id"), caller=st.get("caller"),
                                     rounds=st.get("round"), specialists=",".join(st.get("selected", [])))
        tracing.preview(st["question"], (st.get("answer") or {}).get("text"))
        result = {"run_id": st["run_id"], "question": st["question"], "reformulated": st["reformulated"], "company_id": st.get("company_id"), "rounds_run": st.get("round", 1),
                  "answer": st["answer"], "points": points, "messages": msgs, "sources": st.get("sources", {}), "ask_next": st.get("ask_next", []),
                  "usage": {"cost_usd": round(u.get("cost_usd", 0), 4), "duration_s": int(self.elapsed(st)), "tokens_in": int(u.get("input_tokens", 0)), "tokens_out": int(u.get("output_tokens", 0))},
                  "specialists": st.get("selected", []), "trace_id": trace_id}
        self.store.update_run(st["run_id"], status="DONE", result=result, cost_usd=u.get("cost_usd", 0), trace_id=trace_id, finished_at=_now())
        try:
            self.store.add_turn(st["conversation_id"], st["question"], st["answer"]["text"][:600], st["run_id"])
        except Exception as e:  # noqa: BLE001
            log.warning("turn not stored: %s", e)
        self.ev(st, "status", {"status": "DONE"})
        self.ev(st, "done", {"run_id": st["run_id"], "trace_id": trace_id})
        return {"result": result}

    # ── run control ────────────────────────────────────────────────────────────────────────
    def config(self, run_id: str) -> dict:
        return {"configurable": {"thread_id": run_id}, "recursion_limit": 80}

    def initial_state(self, run: dict) -> DebateState:
        return {"run_id": run["run_id"], "conversation_id": run["conversation_id"], "user_id": run.get("user_id") or "", "caller": run.get("caller") or "ui",
                "question": run["question"], "company_id": run.get("company_id"), "manual_specialists": run.get("specialists"),
                "max_rounds": int(run.get("rounds") or 3), "round": 1, "messages": [], "sources": {}, "usage": {}, "points": [],
                "budget": run.get("budget") or self.defaults, "started_at": time.time()}

    def run_new(self, run_id: str) -> None:
        run = self.store.get_run(run_id)
        self.store.update_run(run_id, status="RUNNING")
        self._drive(run_id, self.initial_state(run))

    def resume(self, run_id: str) -> None:
        self.store.update_run(run_id, status="RUNNING")
        self.ev({"run_id": run_id}, "status", {"status": "RESUMED", "detail": "continuing from the last checkpoint"})
        self._drive(run_id, None)

    def _drive(self, run_id: str, init: DebateState | None) -> None:
        import threading

        stop = threading.Event()

        def beat():
            while not stop.wait(10):
                try:
                    self.store.heartbeat(run_id)
                except Exception:  # noqa: BLE001
                    pass

        t = threading.Thread(target=beat, daemon=True)
        t.start()
        try:
            with tracing.span("moderator.run", "AGENT", inputs={"question": (init or {}).get("question"), "resumed": init is None}, run_id=run_id):
                self.graph.invoke(init, self.config(run_id))
        except Crash:
            log.warning("run %s: simulated crash; waiting for the reaper", run_id)
            CLASS_ABORT.discard(run_id)
            stop.set()
            self.store.update_run(run_id, heartbeat_at=_ago(120))
            return
        except Exception as e:  # noqa: BLE001
            log.exception("run %s failed", run_id)
            self.store.update_run(run_id, status="ERROR", error=f"{type(e).__name__}: {str(e)[:500]}", finished_at=_now())
            self.ev({"run_id": run_id}, "error", {"code": type(e).__name__, "message": str(e)[:300], "retryable": True})
            self.ev({"run_id": run_id}, "status", {"status": "ERROR"})
        finally:
            stop.set()


def _now():
    import datetime as dt

    return dt.datetime.now(dt.timezone.utc)


def _ago(s: int):
    import datetime as dt

    return dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=s)
