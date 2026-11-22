"""Evaluation scorers (design §11.2). Pure functions over a moderator RunResult, plus an LLM-judged fact-recall scorer."""
from __future__ import annotations

import re

TAG = re.compile(r"\[([A-Z]{1,3}\d+)\]")
FACTUAL = re.compile(r"\d|[A-Z][a-z]+ [A-Z][a-z]+|\bUSD\b|\$")


def sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z\[])", text) if s.strip()]


def citation_validity(res: dict) -> float:
    text = res["answer"]["text"]
    tags = TAG.findall(text)
    return 1.0 if tags and all(t in res.get("sources", {}) for t in tags) else (1.0 if not tags and not FACTUAL.search(text) else 0.0)


def claim_citation_coverage(res: dict) -> float:
    factual = [s for s in sentences(res["answer"]["text"]) if FACTUAL.search(s)]
    return sum(bool(TAG.search(s)) for s in factual) / max(1, len(factual))


def within_budget(res: dict, seconds_max: float = 480, cost_max: float = 2.0) -> float:
    u = res.get("usage", {})
    return 1.0 if u.get("duration_s", 0) <= seconds_max and u.get("cost_usd", 0) <= cost_max else 0.0


def fact_recall(llm, answer: str, expected_facts: list[str]) -> float:
    """Fraction of expected facts the answer states (fast-tier judge; facts are judged one by one, strictly)."""
    from pydantic import BaseModel

    class Verdicts(BaseModel):
        found: list[bool]

    if not expected_facts:
        return 1.0
    facts = "\n".join(f"{i + 1}. {f}" for i, f in enumerate(expected_facts))
    r = llm.chat("fast", [{"role": "system", "content": "You grade a research answer. For each numbered expected fact answer true only if the answer states it (same meaning, numbers within rounding) or clearly states the evidence is absent when the fact is a gap. Return JSON {\"found\": [bool, ...]} in order. Reply with ONLY that JSON object, no explanation."},
                          {"role": "user", "content": f"EXPECTED FACTS:\n{facts}\n\nANSWER:\n{answer}"}], response_schema=Verdicts, max_tokens=1500)
    f = (r.parsed.found + [False] * len(expected_facts))[: len(expected_facts)]
    return sum(f) / len(f)
