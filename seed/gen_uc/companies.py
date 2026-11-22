"""ref_companies (600 rows) and ref_holdings (design §14.4). SYNTHETIC DEMO DATA."""
from __future__ import annotations

import numpy as np
import pandas as pd

from seed.common import CCY, d, load_planted, rng

EM_COUNTRIES = [("China", .30), ("India", .18), ("Taiwan", .15), ("Korea", .12), ("Brazil", .06),
                ("Saudi Arabia", .05), ("Indonesia", .04), ("South Africa", .03), ("Mexico", .03),
                ("Thailand", .02), ("Malaysia", .02)]
DM_COUNTRIES = [("Japan", .65), ("United States", .20), ("Australia", .05), ("Singapore", .05), ("Germany", .05)]
SECTORS = [("Information Technology", .22), ("Financials", .18), ("Industrials", .13), ("Consumer Discretionary", .12),
           ("Materials", .08), ("Health Care", .08), ("Communication Services", .07), ("Energy", .05),
           ("Consumer Staples", .04), ("Utilities", .03)]
SUFFIX = {"China": [".HK", ".SS", ".SZ"], "India": [".NS"], "Taiwan": [".TW"], "Korea": [".KS"], "Brazil": [".SA"],
          "Saudi Arabia": [".SR"], "Japan": [".T"], "United States": [""], "Indonesia": [".JK"], "South Africa": [".JO"],
          "Mexico": [".MX"], "Thailand": [".BK"], "Malaysia": [".KL"], "Australia": [".AX"], "Singapore": [".SI"],
          "Germany": [".DE"]}
NAME_A = ["Apex", "Bright", "Cobalt", "Dragon", "Eastern", "Frontier", "Golden", "Harbor", "Imperial", "Jade", "Kinetic",
          "Lumen", "Meridian", "Nova", "Orient", "Pacific", "Quantum", "Riverbend", "Sunrise", "Titan", "Union", "Vertex",
          "Westlake", "Xin", "Yuan", "Zenith", "Aster", "Boreal", "Crest", "Delta"]
NAME_B = ["Holdings", "Industries", "Technologies", "Group", "Materials", "Logistics", "Pharma", "Semiconductor",
          "Energy", "Financial", "Foods", "Motors", "Retail", "Software", "Power", "Chemicals", "Networks", "Capital"]
INDUSTRY = {"Information Technology": ["Semiconductors", "Software", "Hardware", "IT Services"],
            "Financials": ["Banks", "Insurance", "Capital Markets"], "Industrials": ["Machinery", "Transportation", "Electrical Equipment"],
            "Consumer Discretionary": ["Autos", "Retail", "Leisure"], "Materials": ["Chemicals", "Metals & Mining"],
            "Health Care": ["Pharmaceuticals", "Health Services", "Equipment"], "Communication Services": ["Telecom", "Interactive Media"],
            "Energy": ["Oil & Gas", "Midstream"], "Consumer Staples": ["Beverages", "Food Products"], "Utilities": ["Electric Utilities", "Water"]}
REGION = {"Japan": "Japan", "China": "China", "United States": "North America", "Israel": "Middle East"}


def _pick(r, items, n):
    names, p = zip(*items)
    p = np.array(p) / sum(p)
    return r.choice(names, size=n, p=p)


def build_companies(cfg: dict) -> pd.DataFrame:
    r = rng(cfg, 1)
    pf = load_planted()
    n = cfg["n_companies"]
    planted = pf["companies"]
    rows = []
    for c in planted:
        mcap = float(np.clip(r.lognormal(np.log(8e9), 1.2), 5e8, 1.5e12))
        rows.append(dict(company_id=c["id"], ticker=c["ticker"], name=c["name"] if cfg["use_real_company_names"] else f"Synthetic {c['id']}",
                         country=c["country"], gics_sector=c["sector"], gics_industry=c["industry"], market_cap_usd=mcap, is_planted=True))
    rest = n - len(planted)
    is_em = r.random(rest) < 0.70
    country = np.where(is_em, _pick(r, EM_COUNTRIES, rest), _pick(r, DM_COUNTRIES, rest))
    sector = _pick(r, SECTORS, rest)
    mcap = np.clip(r.lognormal(np.log(8e9), 1.2, rest), 5e8, 1.5e12)
    for i in range(rest):
        cid = f"C{121 + i:04d}"
        nm = f"{r.choice(NAME_A)} {r.choice(NAME_B)}"
        suf = r.choice(SUFFIX[country[i]])
        tick = f"{int(r.integers(1000, 9999))}{suf}" if suf else "".join(r.choice(list("ABCDEFGHJKLMNPRSTUVW"), 3))
        rows.append(dict(company_id=cid, ticker=tick, name=f"{nm} {cid[-3:]}", country=country[i], gics_sector=sector[i],
                         gics_industry=r.choice(INDUSTRY[sector[i]]), market_cap_usd=float(mcap[i]), is_planted=False))
    df = pd.DataFrame(rows)
    em_set = {x[0] for x in EM_COUNTRIES}
    df["msci_em"] = df["country"].isin(em_set)
    df.loc[df.company_id == pf["quant"]["legacy_id"]["company"], "msci_em"] = False     # PF-02 id mismatch
    df["region"] = df["country"].map(lambda c: REGION.get(c, "Asia ex-China/Japan" if c not in ("Brazil", "Mexico", "South Africa", "Saudi Arabia", "Germany") else "Other"))
    df["currency"] = df["country"].map(lambda c: CCY.get(c, "USD"))
    # deterministic unique tickers
    df["ticker"] = np.where(df.duplicated("ticker", keep="first"), df["ticker"] + "X" + df["company_id"].str[-2:], df["ticker"])
    return df[["company_id", "ticker", "name", "country", "region", "gics_sector", "gics_industry", "msci_em", "currency", "market_cap_usd", "is_planted"]]


def build_holdings(cfg: dict, companies: pd.DataFrame) -> pd.DataFrame:
    """Two funds, top-60 holdings each at the snapshot date; power-law weights (top 10 ~ 45%)."""
    r = rng(cfg, 2)
    snap = d(cfg["holdings_snapshot"])
    pf = load_planted()
    must = {"NGC85": ["C0115", "C0116", "C0117", "C0118", "C0119", "C0120", "C0101", "C0102", "C0103", "C0114"],
            "NGC2383": ["C0104", "C0105", "C0106", "C0107", "C0112", "C0113"]}
    rows = []
    for fund in cfg["funds"]:
        pool = companies[companies.msci_em | companies.is_planted].sort_values("market_cap_usd", ascending=False)
        pick = list(dict.fromkeys(must[fund] + list(pool.company_id.head(80))))[:60]
        tail = pick[len(must[fund]):]
        r.shuffle(tail)
        pick = pick[:len(must[fund])] + tail
        w = 1.0 / np.arange(1, len(pick) + 1) ** 0.7
        w = w / w.sum()
        # make the planted "must" names rank sensibly: keep order as list (power law over rank)
        for i, (cid, wi) in enumerate(zip(pick, w), 1):
            rows.append(dict(fund=fund, company_id=cid, weight=float(wi), rank=i, as_of=snap))
    return pd.DataFrame(rows)
