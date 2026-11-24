"""External-data tool catalogue (design §5.6): typed arguments for every allowlisted query template.

The LLM never writes SQL. The agent validates arguments with these models, then renders the pinned template
(`query_templates/<name>.sql`) and runs it on the SQL warehouse with bound parameters (see external_query.py).
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator

CID = Annotated[str, Field(pattern=r"^C\d{4}$")]
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-\.&]{1,30}")


def tokens(q: str, n: int = 4) -> list[str]:
    """Search terms: alphanumeric words only (ILIKE-safe), at most n."""
    stop = {"the", "and", "for", "with", "that", "what", "from", "this", "are", "was"}
    out: list[str] = []
    for w in _WORD.findall(q or ""):
        if w.lower() not in stop and w.lower() not in [o.lower() for o in out]:
            out.append(w)
    return out[:n] or ["_"]


class _Dated(BaseModel):
    @field_validator("*", mode="before")
    @classmethod
    def _d(cls, v, info):
        if info.field_name in ("start", "end", "as_of", "since") and isinstance(v, (dt.date, dt.datetime)):
            return v.strftime("%Y-%m-%d")
        return v


class OmsTradeFlows(_Dated):
    company_ids: list[CID] = Field(min_length=1, max_length=50)
    start: dt.date
    end: dt.date
    group_by: Literal["company", "strategy", "account"] = "company"


class OmsConfirmConflict(_Dated):
    basket: Literal["efm_top50", "efm_universe200"] | None = None
    company_ids: list[CID] | None = Field(default=None, max_length=300)
    start: dt.date
    end: dt.date
    strong_musd: float = Field(20.0, ge=0, le=10000)


class OmsPostTradeMove(_Dated):
    company_id: CID
    start: dt.date
    end: dt.date
    horizon_days: int = Field(21, ge=1, le=120)


class OmsPositions(_Dated):
    company_ids: list[CID] = Field(min_length=1, max_length=50)
    as_of: dt.date
    fund: str = Field("", pattern=r"^[A-Za-z0-9_\-]{0,12}$")


class ResearchViews(_Dated):
    company_id: CID
    since: dt.date = dt.date(2025, 1, 1)


class ResearchNotesSearch(BaseModel):
    company_id: CID
    query: str = Field(min_length=2, max_length=200)
    limit: int = Field(5, ge=1, le=5)


class NewsLatest(BaseModel):
    company_id: CID | None = None
    ticker: str | None = Field(None, pattern=r"^[A-Za-z0-9\.\-]{1,12}$")
    hours: int = Field(48, ge=1, le=24 * 120)
    limit: int = Field(10, ge=1, le=25)


class WebSearch(BaseModel):
    query: str = Field(min_length=2, max_length=200)
    max_results: int = Field(5, ge=1, le=10)
    recency_days: int = Field(7, ge=1, le=365)


class ApolloLookup(BaseModel):
    entity: str = Field(min_length=2, max_length=120)
    fields: list[str] = Field(default_factory=list, max_length=10)


TOOLS: dict[str, dict] = {
    "oms_trade_flows": {"model": OmsTradeFlows, "version": 1, "object": "azure_oms.oms.trades", "zone": "external",
                        "doc": "GIC's own trades: net/gross USD, accounts buying/selling and trade count for companies over a window, grouped by company, strategy or account."},
    "oms_confirm_conflict": {"model": OmsConfirmConflict, "version": 1, "object": "azure_oms.oms.trades", "zone": "external",
                             "doc": "Classify each name of the external-manager top-50 basket (or a company list) as confirm / conflict / none from GIC net flows; includes book totals."},
    "oms_post_trade_move": {"model": OmsPostTradeMove, "version": 1, "object": "azure_oms.oms.trades + prices_daily", "zone": "external",
                            "doc": "Trade-weighted subsequent price return after GIC trades in a window."},
    "oms_positions": {"model": OmsPositions, "version": 1, "object": "azure_oms.oms.positions", "zone": "external",
                      "doc": "GIC positions: quantity, market value, weight and active weight per company (optionally for one fund)."},
    "research_views": {"model": ResearchViews, "version": 1, "object": "azure_oms.research.analyst_views", "zone": "external",
                       "doc": "GIC analyst rating and active-weight history for a company with rationale."},
    "research_notes_search": {"model": ResearchNotesSearch, "version": 1, "object": "azure_oms.research.notes", "zone": "external",
                              "doc": "Search GIC internal research notes for a company; returns note id, title, date and the matching snippet."},
    "news_latest": {"model": NewsLatest, "version": 1, "object": "azure_oms.feeds.news_wire", "zone": "external",
                    "doc": "Latest news headlines for a company or ticker from the live wire."},
    "web_search": {"model": WebSearch, "version": 1, "object": "azure_oms.feeds.web_index", "zone": "external",
                   "doc": "Live web search (vendor REST from the agent; `web_search.mode: stub` reads the demo web_index table instead)."},
    "apollo_lookup": {"model": ApolloLookup, "version": 1, "object": "apollo", "zone": "external",
                      "doc": "Apollo entity lookup (stub until onboarded)."},
}


def validate(tool: str, args: dict) -> BaseModel:
    if tool not in TOOLS:
        raise ValueError(f"unknown external tool {tool!r}")
    return TOOLS[tool]["model"].model_validate(args)
