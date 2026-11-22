"""Broker reports, expert calls, earnings calls + chunk tables (design §14.4). SYNTHETIC DEMO DATA."""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from seed.common import CCY, FX, d, load_planted, rng
from seed.gen_docs.text import broker_body, chunk_text, earnings_body, expert_body

CCY_NAME = {"JPY": "JPY", "CNY": "CNY", "INR": "INR", "HKD": "HKD", "TWD": "TWD", "KRW": "KRW", "BRL": "BRL", "SAR": "SAR", "USD": "USD", "ILS": "ILS"}


def _phr(pf, where, company):
    return [p["text"] for p in pf["phrases"] if p["where"] == where and p["company"] == company]


def build_broker(cfg: dict, companies: pd.DataFrame, prices: pd.DataFrame, holdings: pd.DataFrame):
    r = rng(cfg, 30)
    pf = load_planted()
    as_of = d(cfg["demo_as_of"])
    brokers = cfg["brokers"]
    comp = companies.set_index("company_id")
    last_px = prices[prices.date == prices.date.max()].set_index("company_id").close
    rows = []
    cov = holdings[holdings.fund == "NGC85"].company_id.head(50).tolist() + [c["id"] for c in pf["companies"]]
    cov = list(dict.fromkeys(cov))
    pop = companies.company_id.values
    w = 1.0 / (1 + np.arange(len(pop))) ** 0.6
    # planted Kioxia: 11 reports, all Buy, targets 24,000..43,500 JPY
    k14a, k14b, _k14c = _phr(pf, "broker", "C0101")
    c_cut = "One broker cuts its target and expects NAND prices to fall from 2H CY27."
    c_ai = "Another broker flags AI-capex durability as the key risk to the thesis."
    kt = np.linspace(24000, 43500, 11)
    for i in range(11):
        ph = [k14a] if i in (0, 1, 3, 5, 6, 8) else []
        if i in (3, 6, 9, 10):
            ph.append(k14b)
        if i == 4:
            ph.append(c_cut)
        if i == 8:
            ph.append(c_ai)
        rows.append(dict(company_id="C0101", broker=brokers[i % 10], rating="Buy", target_price=float(kt[i]), currency="JPY",
                         published_at=dt.datetime.combine(as_of - dt.timedelta(days=int(3 + i * 4)), dt.time(7, 30)), planted=ph))
    # planted Weichai: 14 reports (4 cut targets)
    wa, wb = _phr(pf, "broker", "C0102")
    for i in range(14):
        ph = []
        if i in (0, 2, 5, 7, 9):
            ph.append(wa)
        if i in (1, 4, 6, 10):
            ph.append(wb)
        rt = "Buy" if i % 3 else "Hold"
        rows.append(dict(company_id="C0102", broker=brokers[(i + 2) % 10], rating=rt, target_price=float(r.uniform(38, 62)), currency="CNY",
                         published_at=dt.datetime.combine(as_of - dt.timedelta(days=int(2 + i * 3)), dt.time(7, 45)), planted=ph))
    n_rest = cfg["n_broker_reports"] - len(rows)
    ids = r.choice(pop, n_rest, p=w / w.sum())
    for cid in ids:
        rt = r.choice(["Buy", "Hold", "Sell"], p=[.55, .35, .10])
        px = float(last_px.get(cid, 25.0))
        t = px * (float(r.lognormal(0.15, 0.12)) if rt == "Buy" else (1.0 if rt == "Hold" else 0.85))
        base_dt = as_of - dt.timedelta(days=int(r.integers(0, 365)))
        rows.append(dict(company_id=cid, broker=brokers[int(r.integers(0, 10))], rating=rt, target_price=t,
                         currency=comp.loc[cid, "currency"], published_at=dt.datetime.combine(base_dt, dt.time(int(r.integers(6, 10)), int(r.integers(0, 59)))), planted=[]))
    df = pd.DataFrame(rows).sort_values("published_at").reset_index(drop=True)
    df.insert(0, "doc_id", [f"BR-{i + 1:06d}" for i in range(len(df))])
    titles, bodies = [], []
    for _, x in df.iterrows():
        c = comp.loc[x.company_id].to_dict()
        c["name"] = c["name"]
        t, b = broker_body(r, c, x.broker, x.rating, x.target_price, x.currency, str(x.published_at.date()), list(x.planted), words=int(r.integers(1600, 2800)))
        titles.append(t)
        bodies.append(b)
    df["title"], df["body"] = titles, bodies
    df = df.drop(columns=["planted"])
    pdf_set = list(df[df.company_id.isin(cov)].sort_values("published_at", ascending=False).doc_id.head(cfg["n_broker_pdfs"]))
    df["has_pdf"] = df.doc_id.isin(pdf_set)
    df["pdf_path"] = None
    chunks = []
    for _, x in df.iterrows():
        for j, ch in enumerate(chunk_text(x.body)):
            chunks.append(dict(chunk_id=f"{x.doc_id}-{j:02d}", doc_id=x.doc_id, company_id=x.company_id, broker=x.broker, rating=x.rating,
                               published_at=x.published_at, text=ch))
    return df, pd.DataFrame(chunks)


def build_experts(cfg: dict, companies: pd.DataFrame):
    r = rng(cfg, 31)
    pf = load_planted()
    as_of = d(cfg["demo_as_of"])
    comp = companies.set_index("company_id")
    roles = r.choice(["Former employee", "Customer", "Supplier", "Channel partner"], cfg["n_expert_calls"], p=[.35, .25, .20, .20])
    pool = companies.company_id.values[:200]
    rows = []
    k15 = _phr(pf, "expert", "C0101")
    for i in range(cfg["n_expert_calls"]):
        cid = "C0101" if i == 0 else str(r.choice(pool))
        role = "Former Kioxia insider" if i == 0 else f"{roles[i]} of {comp.loc[cid, 'name']}"
        cd = d("2026-11-04") if i == 0 else as_of - dt.timedelta(days=int(r.integers(1, 300)))
        topic, body = expert_body(r, comp.loc[cid].to_dict(), role, str(cd), k15 if i == 0 else [], words=int(r.integers(1500, 3000)))
        rows.append(dict(call_id=f"EX-{i + 1:04d}", company_id=cid, expert_role=role if i else "Former employee (Kioxia insider)", call_date=cd,
                         compliance_cleared=True, topic=topic, body=body))
    df = pd.DataFrame(rows)
    ch = []
    for _, x in df.iterrows():
        for j, t in enumerate(chunk_text(x.body)):
            ch.append(dict(chunk_id=f"{x.call_id}-{j:02d}", call_id=x.call_id, company_id=x.company_id, expert_role=x.expert_role, call_date=x.call_date, text=t))
    return df, pd.DataFrame(ch)


def build_earnings_docs(cfg: dict, companies: pd.DataFrame, calendar: pd.DataFrame, holdings: pd.DataFrame):
    r = rng(cfg, 32)
    pf = load_planted()
    as_of = d(cfg["demo_as_of"])
    comp = companies.set_index("company_id")
    cov = list(dict.fromkeys([c["id"] for c in pf["companies"]] + holdings[holdings.fund == "NGC85"].company_id.tolist()))[:50]
    res = calendar[(calendar.event_type == "results") & (calendar.status == "reported") & calendar.company_id.isin(cov)]
    res = res.sort_values("event_date", ascending=False).groupby("company_id").head(4)
    ph = {"C0101": _phr(pf, "earnings", "C0101"), "C0102": _phr(pf, "earnings", "C0102")}
    rows = []
    n = 0
    for _, e in res.iterrows():
        for dt_ in ("call_transcript", "results_note"):
            n += 1
            latest = e.event_date >= (as_of - dt.timedelta(days=40))
            planted = ph.get(e.company_id, []) if latest else []
            title, body = earnings_body(r, comp.loc[e.company_id].to_dict(), e.fiscal_period, str(e.event_date), dt_, planted, cfg["brokers"])
            rows.append(dict(call_id=f"EC-{n:04d}", company_id=e.company_id, fiscal_period=e.fiscal_period, call_date=e.event_date, doc_type=dt_, title=title, body=body))
    df = pd.DataFrame(rows)
    ch = []
    for _, x in df.iterrows():
        for j, t in enumerate(chunk_text(x.body)):
            ch.append(dict(chunk_id=f"{x.call_id}-{j:02d}", call_id=x.call_id, company_id=x.company_id, fiscal_period=x.fiscal_period, call_date=x.call_date, doc_type=x.doc_type, text=t))
    return df, pd.DataFrame(ch)
