"""Deterministic template text for synthetic documents (design §14.4/§14.7). SYNTHETIC DEMO DATA.

Planted sentences from planted_facts.yaml `phrases` are inserted VERBATIM so retrieval can be verified (SD-R07).
Optional LLM paraphrase of non-planted prose is off by default so facts stay exact.
"""
from __future__ import annotations

import numpy as np

WATERMARK = "SYNTHETIC — FOR DEMO ONLY"

DRIVERS = {
    "Information Technology": ["data-centre demand", "AI-related capex", "product mix and ASP trends", "inventory normalisation", "node transitions"],
    "Industrials": ["order backlog", "export demand", "pricing discipline", "capacity utilisation", "input-cost pass-through"],
    "Health Care": ["new product launches", "regulatory approvals", "volume growth in generics", "contract manufacturing wins", "channel inventory"],
    "Materials": ["spot price realisation", "cost curve position", "volume ramp", "inventory restocking"],
    "Consumer Staples": ["premiumisation", "volume recovery", "input costs", "distribution expansion"],
    "Utilities": ["regulated asset base growth", "tariff resets", "transmission capex", "financing costs"],
    "Energy": ["realised prices", "production growth", "capital discipline", "shareholder returns"],
    "Financials": ["net interest margin", "credit costs", "fee income growth", "capital ratios"],
    "Consumer Discretionary": ["same-store sales", "mix shift", "unit growth", "margin recovery"],
    "Communication Services": ["user growth", "monetisation", "content costs", "advertising demand"],
}
RISKS = ["a slower-than-expected demand recovery", "adverse regulatory developments", "FX translation effects", "supply-chain disruption",
         "higher input costs", "competitive pricing pressure", "execution risk on new capacity", "customer concentration"]
OPEN = ["We initiate with a clear view that", "Our latest checks suggest that", "Following the recent results, we think that", "After meeting management, we believe"]


def _pct(r, lo, hi):
    return f"{r.uniform(lo, hi):.1f}%"


def _para(r, sector, name, n_sent=6, extra=None):
    d = DRIVERS.get(sector, DRIVERS["Industrials"])
    s = []
    for _ in range(n_sent):
        t = r.integers(0, 6)
        if t == 0:
            s.append(f"{name} should benefit from {r.choice(d)}, which we expect to add {_pct(r, 1, 6)} to revenue growth next year.")
        elif t == 1:
            s.append(f"Management highlighted {r.choice(d)} as the main swing factor for the second half.")
        elif t == 2:
            s.append(f"We forecast operating margin of {_pct(r, 6, 28)} for the current fiscal year, versus {_pct(r, 5, 27)} last year.")
        elif t == 3:
            s.append(f"The main risk we see is {r.choice(RISKS)}, which could trim earnings by {_pct(r, 2, 9)} in a downside case.")
        elif t == 4:
            s.append(f"Relative to peers, the shares trade at {r.uniform(6, 22):.1f}x forward earnings and a free-cash-flow yield of {_pct(r, 1, 8)}.")
        else:
            s.append(f"Our estimates sit {_pct(r, 0.5, 6)} {'above' if r.random() < .5 else 'below'} consensus for the next twelve months.")
    if extra:
        s.insert(int(r.integers(0, len(s) + 1)), extra)
    return " ".join(s)


def broker_body(r, company: dict, broker: str, rating: str, target_local: float, ccy: str, published: str, planted: list[str], words: int = 2200) -> tuple[str, str]:
    name, sector = company["name"], company["gics_sector"]
    title = f"{name}: {rating} — {r.choice(['thesis intact', 'estimates up', 'valuation looks undemanding', 'watching the next catalyst', 'channel checks encouraging'])}"
    secs = []
    secs.append("## Summary\n" + _para(r, sector, name, 5, planted[0] if planted else None) + f" We rate the shares {rating} with a target price of {ccy} {target_local:,.0f}.")
    secs.append("## Investment thesis\n" + "\n\n".join(_para(r, sector, name, 7, planted[i] if len(planted) > i else None) for i in range(1, 4)))
    secs.append("## Estimates\n" + _para(r, sector, name, 6) + "\n\n| Year | Revenue growth | EBIT margin | EPS growth |\n|---|---|---|---|\n" +
                "\n".join(f"| FY{26 + k} | {_pct(r, -2, 25)} | {_pct(r, 6, 28)} | {_pct(r, -5, 35)} |" for k in range(3)))
    secs.append("## Valuation\n" + "\n\n".join(_para(r, sector, name, 6, planted[i] if len(planted) > i else None) for i in range(4, 6)))
    secs.append("## Risks\n" + _para(r, sector, name, 6, planted[6] if len(planted) > 6 else None))
    body = f"[{WATERMARK}] {broker} | {published}\n\n# {title}\n\n" + "\n\n".join(secs)
    # pad to roughly `words` words with additional analysis paragraphs
    while len(body.split()) < words:
        body += "\n\n" + _para(r, sector, name, 8)
    return title, body


def expert_body(r, company: dict, role: str, call_date: str, planted: list[str], words: int = 2200) -> tuple[str, str]:
    name, sector = company["name"], company["gics_sector"]
    topic = f"{role} on {name}"
    qa = []
    qs = ["How would you characterise demand over the past two quarters?", "Where are the main bottlenecks today?",
          "How do pricing and mix look versus a year ago?", "What do customers say about supplier concentration?",
          "What would change your view over the next twelve months?", "How does the company compare with its closest rivals?"]
    pl = list(planted)
    for q in qs:
        ans = _para(r, sector, name, 6, pl.pop(0) if pl else None)
        qa.append(f"**Q:** {q}\n\n**A:** {ans}")
    body = f"[{WATERMARK}] Expert call transcript | {call_date} | Expert: {role}. Compliance cleared.\n\n" + "\n\n".join(qa)
    while len(body.split()) < words:
        body += f"\n\n**Q:** Anything else worth flagging?\n\n**A:** {_para(r, sector, name, 8)}"
    return topic, body


def earnings_body(r, company: dict, period: str, call_date: str, doc_type: str, planted: list[str], broker_names: list[str]) -> tuple[str, str]:
    name, sector = company["name"], company["gics_sector"]
    if doc_type == "results_note":
        body = f"[{WATERMARK}] Results note | {name} | {period} | {call_date}\n\n" + _para(r, sector, name, 9, planted[0] if planted else None)
        return f"{name} {period} results note", body
    ceo, cfo, ir = "Hiroshi Tanabe" if company["country"] == "Japan" else "Li Wen", "Akiko Mori" if company["country"] == "Japan" else "Zhang Min", "Investor Relations"
    prepared = f"**{ir}:** Welcome to the {name} {period} earnings call. [{WATERMARK}]\n\n**{ceo} (CEO):** " + _para(r, sector, name, 14, planted[0] if planted else None) + \
        f"\n\n**{cfo} (CFO):** " + _para(r, sector, name, 14)
    qa = []
    for i in range(10):
        an = f"{r.choice(['Anna', 'Raj', 'Wei', 'Sora', 'Mia', 'Tomas'])} {r.choice(['Lim', 'Gupta', 'Chen', 'Sato', 'Kovacs'])} ({r.choice(broker_names)})"
        qa.append(f"**{an}:** {r.choice(['Can you elaborate on', 'How should we think about', 'What is the outlook for'])} {r.choice(DRIVERS.get(sector, DRIVERS['Industrials']))}?\n\n**{ceo}:** " + _para(r, sector, name, 7, planted[1] if (i == 3 and len(planted) > 1) else None))
    return f"{name} {period} earnings call", prepared + "\n\n## Q&A\n\n" + "\n\n".join(qa)


def research_note(r, company: dict, analyst: str, title: str, published: str, fixed: str | None = None, words: int = 500) -> str:
    name, sector = company["name"], company["gics_sector"]
    if fixed:
        return f"[{WATERMARK}] {analyst} | {published}\n\n{fixed}\n\n" + _para(r, sector, name, 8)
    body = f"[{WATERMARK}] {analyst} | {published}\n\n**Thesis.** " + _para(r, sector, name, 6) + "\n\n**What changed.** " + _para(r, sector, name, 5) + "\n\n**Risks.** " + _para(r, sector, name, 4)
    while len(body.split()) < words:
        body += " " + _para(r, sector, name, 4)
    return body


def chunk_text(text: str, size_words: int = 600, overlap_sents: int = 3) -> list[str]:
    """Sentence-aware chunks of ~800 tokens (words ~ 0.75 tokens) with sentence overlap.
    Whole sentences are never split, so planted sentences survive verbatim inside one chunk."""
    import re
    sents = [x for x in re.split(r"(?<=[.!?])\s+|\n+", text) if x.strip()]
    out, cur, n = [], [], 0
    for snt in sents:
        w = len(snt.split())
        if cur and n + w > size_words:
            out.append(" ".join(cur))
            cur = cur[-overlap_sents:]
            n = sum(len(x.split()) for x in cur)
        cur.append(snt)
        n += w
    if cur:
        out.append(" ".join(cur))
    return out
