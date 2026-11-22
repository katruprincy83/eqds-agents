"""Single, pure-pandas entry point that builds every synthetic dataset (design §14.7). SYNTHETIC DEMO DATA.

The jobs (`src/jobs/seed_*.py`) call these functions and only add the Spark/JDBC write layer, so the same
code is unit-tested locally and runs unchanged in any workspace. Output is deterministic (seed + DEMO_AS_OF).
"""
from __future__ import annotations

import pandas as pd

from seed.common import d, load_config, load_planted
from seed.gen_azure.gen import (build_accounts, build_news, build_positions, build_research, build_trades,
                                build_web_index)
from seed.gen_docs.docs import build_broker, build_earnings_docs, build_experts
from seed.gen_uc.companies import build_companies, build_holdings
from seed.gen_uc.market import base_prices, build_efm, build_prices, build_quant, efm_tables
from seed.gen_uc.misc import build_earnings_calendar, build_geo_revenue, build_wiki_seed


def build_uc(cfg: dict | None = None) -> dict[str, pd.DataFrame]:
    """UC reference/market tables (no documents)."""
    cfg = cfg or load_config()
    companies = build_companies(cfg)
    p0 = base_prices(cfg, companies)
    mgr, sh, cids, bh, wnd = build_efm(cfg, companies, p0)
    efm_managers, efm_holdings, basket = efm_tables(cfg, mgr, sh, cids, bh, wnd, p0)
    top50 = list(basket.company_id[basket.in_top50])
    prices = build_prices(cfg, companies, p0, top50)
    # the basket is re-derived with real prices so values are consistent with prices_daily
    efm_managers, efm_holdings, basket = efm_tables(cfg, mgr, sh, cids, bh, wnd, p0, prices=prices)
    holdings = build_holdings(cfg, companies)
    quant = build_quant(cfg, companies)
    calendar = build_earnings_calendar(cfg, companies)
    geo = build_geo_revenue(cfg, companies)
    wiki_pages, wiki_state = build_wiki_seed(cfg, companies)
    return {
        "ref_companies": companies, "ref_holdings": holdings, "efm_managers": efm_managers,
        "efm_holdings": efm_holdings, "efm_buy_basket": basket, "em_quant_scores": quant,
        "prices_daily": prices, "earnings_calendar": calendar, "geo_revenue": geo,
        "wiki_pages": wiki_pages, "wiki_state": wiki_state,
    }


def build_docs(uc: dict[str, pd.DataFrame], cfg: dict | None = None) -> dict[str, pd.DataFrame]:
    """Documents + chunk tables (broker, expert, earnings)."""
    cfg = cfg or load_config()
    broker, broker_chunks = build_broker(cfg, uc["ref_companies"], uc["prices_daily"], uc["ref_holdings"])
    experts, expert_chunks = build_experts(cfg, uc["ref_companies"])
    earn, earn_chunks = build_earnings_docs(cfg, uc["ref_companies"], uc["earnings_calendar"], uc["ref_holdings"])
    return {"broker_reports": broker, "broker_chunks": broker_chunks, "expert_calls": experts,
            "expert_chunks": expert_chunks, "earnings_calls": earn, "earnings_chunks": earn_chunks}


def build_azure(uc: dict[str, pd.DataFrame], cfg: dict | None = None) -> dict[str, pd.DataFrame]:
    """Azure SQL content (oms / research / feeds)."""
    cfg = cfg or load_config()
    companies, prices = uc["ref_companies"], uc["prices_daily"]
    strategies, accounts = build_accounts(cfg)
    trades, info = build_trades(cfg, companies, prices, uc["ref_holdings"], uc["efm_buy_basket"], accounts)
    positions = build_positions(cfg, trades, prices, uc["ref_holdings"], accounts, companies)
    views, notes = build_research(cfg, companies)
    news = build_news(cfg, companies, uc["earnings_calendar"])
    web = build_web_index(cfg, companies)
    return {"strategies": strategies, "accounts": accounts, "trades": trades, "positions": positions,
            "analyst_views": views, "notes": notes, "news_wire": news, "web_index": web, "_info": info}


def build_eval_questions() -> pd.DataFrame:
    """`eval_questions` rows from planted_facts.yaml (single source of truth, SD-02): 15 headline, 15 deep dive, 10 gaps."""
    import json

    pf = load_planted()
    facts = {f["id"]: f for f in pf["facts"]}
    rows, n = [], 0
    for group in ("headline", "deep_dive", "gaps"):
        for q in pf["eval_questions"][group]:
            n += 1
            rows.append({
                "qid": f"Q{n:03d}", "question": q["q"],
                "expected_facts": [facts[p]["eval_fact"] for p in q["pfs"]],
                "expected_answer": " ".join(facts[p]["eval_fact"] + "." for p in q["pfs"]),
                "tags": json.dumps({"group": group, "pfs": q["pfs"], "cite": q.get("cite", []),
                                    "expected_specialists": sorted({facts[p]["expected_specialist"] for p in q["pfs"]})}),
            })
    return pd.DataFrame(rows)


__all__ = ["build_uc", "build_docs", "build_azure", "build_eval_questions", "d"]
