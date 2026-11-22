"""earnings_calendar, geo_revenue, wiki seeds (design §14.4). SYNTHETIC DEMO DATA."""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from seed.common import d, load_planted, rng

SEASONS = [(1, 20, 2, 25), (4, 20, 5, 25), (7, 20, 8, 25), (10, 20, 11, 20)]    # (m1, d1, m2, d2) reporting seasons


def _season_dates(r, year_q):
    out = []
    for (y, q) in year_q:
        m1, d1, m2, d2 = SEASONS[q]
        a = dt.date(y, m1, d1)
        b = dt.date(y, m2, d2)
        out.append(a + dt.timedelta(days=int(r.integers(0, (b - a).days + 1))))
    return out


def _label(fy: str, dd: dt.date) -> str:
    season = {1: 0, 2: 0, 4: 1, 5: 1, 7: 2, 8: 2, 10: 3, 11: 3}.get(dd.month, 0)
    if fy == "FY12":      # Jan-Feb reports Q4 of prior year
        q, yr = [(4, dd.year - 1), (1, dd.year), (2, dd.year), (3, dd.year)][season]
        return f"Q{q} FY12/{str(yr)[-2:]}"
    q, yr = [(3, dd.year), (4, dd.year), (1, dd.year + 1), (2, dd.year + 1)][season]
    return f"Q{q} FY3/{str(yr)[-2:]}" if q <= 2 or True else ""


def build_earnings_calendar(cfg: dict, companies: pd.DataFrame) -> pd.DataFrame:
    r = rng(cfg, 7)
    pf = load_planted()
    as_of = d(cfg["demo_as_of"])
    planted_ids = [c["id"] for c in pf["companies"]]
    cover = list(dict.fromkeys(planted_ids + list(companies.sort_values("market_cap_usd", ascending=False).company_id.head(330))))[:330]
    periods = [(2025, 3), (2026, 0), (2026, 1), (2026, 2), (2026, 3), (2027, 0)]       # last 4 reported + next 2
    rows = []
    for cid in cover:
        country = companies.loc[companies.company_id == cid, "country"].iloc[0]
        fy = "FY3" if country in ("Japan", "India") else "FY12"
        dates = _season_dates(r, periods)
        for k, (dd, (y, q)) in enumerate(zip(dates, periods)):
            label = _label(fy, dd)
            rows.append(dict(company_id=cid, fiscal_period=label, event_type="results", event_date=dd,
                             status="reported" if dd <= as_of else "scheduled"))
        if r.random() < 0.8:
            rows.append(dict(company_id=cid, fiscal_period="AGM", event_type="agm", event_date=dt.date(2026, int(r.integers(5, 7)), int(r.integers(1, 28))), status="reported"))
        if r.random() < 0.4:
            rows.append(dict(company_id=cid, fiscal_period="Investor day", event_type="investor_day", event_date=dt.date(2026, int(r.integers(9, 12)), int(r.integers(1, 28))), status="reported" if True else "scheduled"))
    df = pd.DataFrame(rows)
    # planted overrides
    for e in pf["earnings"]:
        sel = (df.company_id == e["company"]) & (df.event_type == "results") & (df.event_date.map(lambda x: x.year == 2026 and x.month in (10, 11)))
        i = df[sel].index[0]       # the Oct-Nov 2026 reporting-season row
        df.loc[i, ["fiscal_period", "event_date", "status"]] = [e["period"], d(e["date"]), e["status"]]
    df["status"] = np.where(df.event_date <= as_of, "reported", "scheduled")
    df["event_date"] = pd.to_datetime(df.event_date).dt.date
    return df.reset_index(drop=True)


REGIONS = ["China", "Japan", "Asia ex-China/Japan", "North America", "Europe", "Other"]
HOME = {"China": "China", "Japan": "Japan", "India": "Asia ex-China/Japan", "Taiwan": "Asia ex-China/Japan", "Korea": "Asia ex-China/Japan",
        "United States": "North America"}


def build_geo_revenue(cfg: dict, companies: pd.DataFrame) -> pd.DataFrame:
    r = rng(cfg, 8)
    pf = load_planted()
    rows = []
    fys = ["FY2023", "FY2024", "FY2025", "FY2026"]
    ov = {(o["company"], fy): v for o in pf["geo_revenue"] for fy, v in o["share_pct"].items()}
    cov = set(companies.sort_values("market_cap_usd", ascending=False).company_id.head(250)) | {c["id"] for c in pf["companies"]}
    for cid, country, mcap in zip(companies.company_id, companies.country, companies.market_cap_usd):
        if cid not in cov:
            continue
        home = HOME.get(country, "Other")
        rev_total0 = mcap * r.uniform(0.3, 1.2)
        home_share = float(np.clip(r.normal(0.65, 0.15), 0.25, 0.95))
        for k, fy in enumerate(fys):
            alpha = np.ones(6) * 0.8
            alpha[REGIONS.index(home)] += 6.0
            w = r.dirichlet(alpha)
            w[REGIONS.index(home)] = max(w[REGIONS.index(home)], home_share - 0.1 + 0.03 * (3 - k))
            w = w / w.sum()
            if (cid, fy) in ov:
                na = ov[(cid, fy)] / 100.0
                idx = REGIONS.index("North America")
                others = np.delete(w, idx)
                others = others / others.sum() * (1 - na)
                w = np.insert(others, idx, na)
            w = np.round(w * 100, 1)
            w[np.argmax(w)] += round(100 - w.sum(), 1)
            tot = rev_total0 * (1.05 ** k)
            for reg, sh in zip(REGIONS, w):
                rows.append(dict(company_id=cid, fiscal_year=fy, region=reg, revenue_usd=float(tot * sh / 100), share_pct=float(sh), source="synthetic fundamentals vendor"))
    return pd.DataFrame(rows)


def build_wiki_seed(cfg: dict, companies: pd.DataFrame):
    pf = load_planted()
    r = rng(cfg, 9)
    pages = ["Home", "Company", "Industry", "Catalysts", "Debate", "Internal View", "External View", "ESG", "References"]
    state, wp = [], []
    names = companies.set_index("company_id")["name"].to_dict()
    pre = {w["company"]: w for w in pf["wiki_prebuilt"]}
    # 40 wikis: the 4 prebuilt + 36 lightweight placeholders (excluding C0116-C0120, which have none)
    ids = list(pre) + [c for c in companies.sort_values("market_cap_usd", ascending=False).company_id
                       if c not in pre and c not in {"C0116", "C0117", "C0118", "C0119", "C0120"}][:36]
    for cid in ids:
        info = pre.get(cid)
        built = d(info["updated"]) if info else d("2026-10-01") + dt.timedelta(days=int(r.integers(0, 30)))
        state.append(dict(company_id=cid, last_full_build=dt.datetime.combine(built, dt.time(7, 30)), last_refresh=dt.datetime.combine(built, dt.time(7, 30)),
                          pending_triggers="[]", note=(info or {}).get("note", "")))
        for p in pages:
            wp.append(dict(company_id=cid, page=p, version=1, content_md=f"# {names[cid]}: {p}\n\n_SYNTHETIC DEMO DATA. Seed placeholder; rebuilt by job_wiki_refresh._\n",
                           sources="[]", built_at=dt.datetime.combine(built, dt.time(7, 30))))
    return pd.DataFrame(wp), pd.DataFrame(state)
