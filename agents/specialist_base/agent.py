"""Specialist agent (design §5.3): a LangGraph ReAct agent over the specialist's allowed tools, followed by a structured
finalisation step that emits the `SpecialistAnswer` schema; citation tags are assigned from tool results (FR-DT-06)."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import time
from typing import Any

import yaml
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.prebuilt import create_react_agent

from eqds_agentkit import contract, tracing
from eqds_agentkit.contract import Claim, InvocationOutputs, SpecialistAnswer, Usage
from eqds_agentkit.memory import Store
from eqds_agentkit.skills import PromptStore, VolumeSkillStore
from eqds_llm import LLM, load_model_config
from eqds_llm.chat_model import EqdsChatModel

from .tools import ToolContext, build_bridge_tools, build_static_tools, tagged

log = logging.getLogger("specialist")


class SpecialistAgent:
    def __init__(self, cfg_path: str | os.PathLike, env, w, store: Store | None, root: str | os.PathLike | None = None):
        self.cfg = yaml.safe_load(pathlib.Path(cfg_path).read_text())
        self.key, self.env, self.w, self.store = self.cfg["key"], env, w, store
        root = pathlib.Path(root or pathlib.Path(cfg_path).parent)
        mc = root / env.get("models_config", "config/models.demo.yaml")
        self.llm = LLM(load_model_config(mc, env.prefix, env.suffix, env.catalog, env.schema))
        self.skills = VolumeSkillStore(env.volume_path("skills"), root / "skills", w)
        self.prompts = PromptStore(env.volume_path("prompts"), root / "prompts", w)
        self.warehouse_id = os.environ.get("DATABRICKS_WAREHOUSE_ID")
        self.bridge_url = os.environ.get("BRIDGE_MCP_URL")  # local override; deployed apps resolve by name (see _bridge_url)
        self._static: list | None = None
        self._lock = asyncio.Lock()

    def _bridge_url(self) -> str | None:
        if self.bridge_url:
            return self.bridge_url
        try:
            return self.env.app_url(self.w, "bridge-mcp") + "/mcp"
        except Exception:  # noqa: BLE001
            return None

    # ── lifecycle ──────────────────────────────────────────────────────────────────────────
    async def warm(self) -> None:
        async with self._lock:
            if self._static is None:
                self._static = await build_static_tools(self.cfg, self.env, self.w, self.warehouse_id)
        sk = self.cfg.get("skill") or {}
        self.skills.load(sk.get("name", self.key), sk.get("version", "1.0.0"))

    def info(self) -> dict:
        c = self.cfg
        return {k: c.get(k) for k in ("key", "display_name", "tag_prefix", "description", "domains", "model_tier", "skill", "version", "owner", "timeout_s")} | {
            "contract_version": contract.CONTRACT_VERSION, "tools": c.get("tools_summary", ""),
            # what registration needs to grant the agent its access when it is deployed outside the platform repo
            "tools_summary": c.get("tools_summary", ""), "tool_config": c.get("tools") or [], "access": c.get("access") or {}}

    # ── invocation ─────────────────────────────────────────────────────────────────────────
    async def invoke(self, body: dict, user: str = "anonymous") -> InvocationOutputs:
        timer = contract.Timer()
        ci = body.get("custom_inputs") or {}
        brief = next((m["content"] for m in reversed(body.get("input", [])) if m.get("role") == "user"), "")
        run_id, rnd = ci.get("run_id", ""), ci.get("round", 1)
        idem = ci.get("idempotency_key") or f"{run_id}:{rnd}:{self.key}"
        if self.store is not None and ci.get("caller") != "eval":
            hit = await asyncio.to_thread(self.store.cached_response, idem)
            if hit:
                out = InvocationOutputs.model_validate(hit)
                out.cached = True
                out.activity = [{"component": f"eqds-sp-{self.key}", "zone": "state", "label": "idempotency cache hit", "detail": idem, "link": {"kind": "lakebase", "ref": "mem.specialist"}}]
                return out
        await self.warm()
        try:
            out = await asyncio.wait_for(self._run(brief, ci, user, timer), timeout=float(self.cfg.get("timeout_s", 90)) + 10)
        except asyncio.TimeoutError:
            out = contract.error_outputs(self.key, f"{self.cfg['display_name']} timed out; no evidence returned.", "error")
        except Exception as e:  # noqa: BLE001
            log.exception("specialist %s failed", self.key)
            out = contract.error_outputs(self.key, f"{self.cfg['display_name']} failed: {type(e).__name__}: {str(e)[:200]}", "error")
        out.usage.latency_ms = timer.ms()
        if self.store is not None and out.claims and out.claims[0].stance != "error":
            await asyncio.to_thread(self.store.put_interaction, self.key, ci.get("conversation_id", ""), ci.get("caller", ""), idem, brief, out.model_dump(mode="json"))
        return out

    async def _run(self, brief: str, ci: dict, user: str, timer: contract.Timer) -> InvocationOutputs:
        cfg, key = self.cfg, self.key
        tier = ci.get("model_tier") or cfg.get("model_tier", "standard")
        ctx = ToolContext(cfg["tag_prefix"])
        skill = self.skills.load((cfg.get("skill") or {}).get("name", key), (cfg.get("skill") or {}).get("version", "1.0.0"))
        system_t = self.prompts.load("specialist", "system", cfg.get("prompt_version", "1.0.0"))
        final_t = self.prompts.load("specialist", "finalize", cfg.get("prompt_version", "1.0.0"))
        cname = cfg["display_name"]
        asof = self.env.get("demo_as_of", "2026-11-20")
        system = system_t.format(display_name=cname, key=key, tag_prefix=cfg["tag_prefix"], skill=skill.text, as_of=asof,
                                 max_tool_calls=cfg.get("max_tool_calls", 8), catalog=self.env.catalog, schema=self.env.schema)
        mem = []
        if self.store is not None and ci.get("conversation_id"):
            mem = await asyncio.to_thread(self.store.recent_interactions, key, ci["conversation_id"], 5)
        user_msg = brief
        if ci.get("company_id"):
            user_msg += f"\n\n(Company in focus: {ci['company_id']})"
        if ci.get("prior_findings"):
            user_msg += "\n\nPrior findings from other specialists (do not repeat; verify or challenge if your data allows):\n" + "\n".join(
                f"- [{f.get('from')}] {f.get('claim')}" for f in ci["prior_findings"][:8])
        if mem:
            system += "\n\nEarlier in this conversation you answered:\n" + "\n".join(f"- Q: {m['brief'][:200]} → {(m['response'] or {}).get('text', '')[:300]}" for m in mem)

        tools = [tagged(t, ctx, m["kind"], m["ref"], m["server"], f"{self.env.uc}", self.env.foreign_catalog) for t, m in (self._static or [])]
        for spec in cfg.get("tools", []):
            if spec["type"] == "bridge_mcp" and self._bridge_url():
                hdrs = {"x-eqds-run-id": ci.get("run_id", ""), "x-eqds-user": user, "x-eqds-caller": f"sp-{key}"}
                tools += [tagged(t, ctx, m["kind"], m["ref"], m["server"], self.env.uc, self.env.foreign_catalog) for t, m in await build_bridge_tools(spec, self.w, self._bridge_url(), hdrs)]
        if not tools:
            raise RuntimeError("no tools available")

        model = EqdsChatModel(llm=self.llm, tier=tier, max_tokens=2048, usage={})
        agent = create_react_agent(model, tools, prompt=SystemMessage(content=system))
        limit = int(cfg.get("max_tool_calls", 8)) * 2 + 3
        msgs: list = []
        # Soft deadline: stop gathering at (timeout_s - finalise reserve) and finalise on the evidence collected so far.
        # (The Databricks Apps gateway drops requests that run past ~3 minutes, so a hard overrun would lose everything.)
        soft = max(30.0, float(cfg.get("timeout_s", 90)) - 45.0)
        t_start = time.monotonic()

        async def gather():
            nonlocal msgs
            async for st in agent.astream({"messages": [HumanMessage(content=user_msg)]}, config={"recursion_limit": limit}, stream_mode="values"):
                msgs = st["messages"]

        try:
            await asyncio.wait_for(gather(), timeout=soft)
        except asyncio.TimeoutError:
            log.warning("react loop hit the soft deadline (%.0fs) after %d messages; finalising on partial evidence", time.monotonic() - t_start, len(msgs))
        except Exception as e:  # noqa: BLE001  recursion limit etc: finalise with the evidence gathered so far
            log.warning("react loop ended early: %s", str(e)[:120])
        evidence = []
        draft = ""
        for m in msgs:
            if isinstance(m, ToolMessage):
                evidence.append(f"[tool result: {m.name}]\n{str(m.content)[:16000]}")
            elif isinstance(m, AIMessage) and m.content and not m.tool_calls:
                draft = m.content if isinstance(m.content, str) else str(m.content)
        tags = [s.id for s in ctx.sources]
        fin_user = (f"BRIEF:\n{user_msg}\n\nEVIDENCE (every item carries a citation tag; valid tags: {', '.join(tags) or 'none'}):\n" + ("\n\n".join(evidence) or "(no tool results)") +
                    f"\n\nYOUR DRAFT ANSWER:\n{draft or '(none)'}")
        fin = self.llm.chat(tier, [{"role": "system", "content": final_t.format(display_name=cname, tag_prefix=cfg["tag_prefix"], tags=", ".join(tags) or "none")},
                                    {"role": "user", "content": fin_user}], response_schema=SpecialistAnswer, max_tokens=2500)
        ans: SpecialistAnswer = fin.parsed
        valid = set(tags)
        claims: list[Claim] = []
        for i, c in enumerate(ans.claims, 1):
            cites = [t for t in c.citations if t in valid]
            c2 = c.model_copy(update={"id": f"c{i}", "citations": cites})
            if not cites and c2.stance in ("support", "oppose", "partial"):
                c2 = c2.model_copy(update={"confidence": min(c2.confidence, 0.3)})
            claims.append(c2)
        text = ans.summary
        usage = Usage(input_tokens=model.usage.get("input_tokens", 0) + fin.usage.input, output_tokens=model.usage.get("output_tokens", 0) + fin.usage.output,
                      cost_usd=round(model.usage.get("cost_usd", 0.0) + fin.cost_usd, 5))
        trace_id = tracing.tag_trace(run_id=ci.get("run_id"), conversation_id=ci.get("conversation_id"), user_id=user, caller=ci.get("caller"),
                                     specialist=key, round=ci.get("round"))
        ctx.activity.insert(0, {"component": f"eqds-sp-{key}", "zone": "serverless", "label": "POST /invocations", "detail": cfg.get("tools_summary", ""),
                                "link": {"kind": "app", "ref": f"eqds-sp-{key}"}})
        return InvocationOutputs(specialist=key, text=text, claims=claims, sources=[s for s in ctx.sources if s.id in set(tags)],
                                 tool_calls=ctx.tool_calls, activity=ctx.activity, trace_id=trace_id, usage=usage)
