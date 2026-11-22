"""External-manager holdings, buy basket, EM quant scores and prices (design §14.4). SYNTHETIC DEMO DATA.

Planted facts (PF-01/02/04/09/10/11/12/16/19) are constructed, not hoped for:
 * the buy basket is DERIVED from efm_holdings (score = month-ends where a better-half manager net-increased),
   but the holdings in the 3-month window are constrained so exactly the 8 planted names reach score 3;
 * prices get bridge overrides so `price_move` returns the planted percentages.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from seed.common import FX, business_days, d, load_planted, month_ends, rng

SECTOR_VOL = {"Information Technology": .022, "Communication Services": .024, "Health Care": .018, "Industrials": .016,
              "Consumer Discretionary": .015, "Consumer Staples": .015, "Financials": .013, "Utilities": .010,
              "Energy": .019, "Materials": .016}


# ───────────────────────────────── EFM ─────────────────────────────────
def base_prices(cfg: dict, companies: pd.DataFrame) -> pd.Series:
    r = rng(cfg, 3)
    p0 = np.exp(r.normal(np.log(25.0), 0.9, len(companies)))
    return pd.Series(p0, index=companies.company_id.values)


def build_efm(cfg: dict, companies: pd.DataFrame, p0: pd.Series):
    """Returns (managers_df, shares[T, M, N] array, company_ids, flags). `flags` records constructed targets."""
    r = rng(cfg, 4)
    pf = load_planted()
    T, M, N = len(cfg["month_ends"]), cfg["n_efm_managers"], len(companies)
    cids = companies.company_id.values
    idx = {c: i for i, c in enumerate(cids)}
    em = companies.msci_em.values | companies.is_planted.values
    pop = np.where(em, 1.0, 0.4) / (1 + np.argsort(np.argsort(r.permutation(N)))) ** 0.9
    for c in pf["efm"]["score3"]:
        pop[idx[c]] = np.sort(pop)[-60 + int(c[-1])]          # planted names reasonably popular
    pop = pop / pop.sum()

    mgr = pd.DataFrame({
        "manager_id": [f"M{i + 1:03d}" for i in range(M)],
        "name": [f"{a} {b}" for a, b in zip(r.choice(["Harbourline", "Northgate", "Redwood", "Silverpine", "Tidewater", "Ironbridge", "Lakeshore",
                                                       "Stonebrook", "Eastfield", "Granite", "Bluehaven", "Oakhurst", "Sundial", "Cedarpoint", "Westmark"], M),
                                           r.choice(["Capital", "Partners", "Asset Management", "Investors", "Advisors"], M))],
        "style": r.choice(["Growth", "Value", "GARP", "Quant"], M, p=[.40, .25, .20, .15]),
        "quality_score": r.normal(0.15, 0.35, M),
    })
    mgr["name"] = mgr["name"] + " " + mgr["manager_id"].str[-3:]
    order = mgr.quality_score.rank(ascending=False, method="first")
    mgr["better_half"] = order <= M // 2
    # planted designated buyers must be better-half managers: force those indices into the better half
    forced = {int(v) for c, mv in pf["efm"]["buyers"].items() for v in mv.values()}
    for i in forced:
        mgr.loc[i - 1, "better_half"] = True
    bh = mgr.better_half.values

    aum = r.lognormal(np.log(6e9), 0.7, M)
    shares = np.zeros((T, M, N))
    for m in range(M):
        k = int(np.clip(r.poisson(55), 35, 80))
        held = r.choice(N, size=k, replace=False, p=pop)
        w = 1.0 / np.arange(1, k + 1) ** 1.1
        w = w / w.sum()
        shares[0, m, held] = (w * aum[m]) / p0.values[held]
    for t in range(1, T):
        prev = shares[t - 1]
        cur = prev.copy()
        for m in range(M):
            held = np.flatnonzero(prev[m] > 0)
            exits = held[r.random(len(held)) < 0.15]
            keep = np.setdiff1d(held, exits)
            cur[m, keep] = prev[m, keep] * np.exp(r.normal(0, 0.12, len(keep)))
            cur[m, exits] = 0
            if len(exits):
                avail = np.flatnonzero(cur[m] == 0)
                pp = pop[avail] / pop[avail].sum()
                new = r.choice(avail, size=len(exits), replace=False, p=pp)
                cur[m, new] = (aum[m] / 55.0 / 3.0) / p0.values[new] * r.lognormal(0, 0.4, len(new))
        shares[t] = cur

    # ── constrain the 3-month window for better-half managers ──
    wnd = [month_ends(cfg).index(d(x)) for x in cfg["efm_window"]]
    bh_idx = np.flatnonzero(bh)
    planted_idx = {idx[c]: c for c in pf["efm"]["score3"]}
    target = r.choice([0, 1, 2], size=N, p=[.45, .30, .25])
    flag = np.zeros((3, N), bool)
    designated = {}
    for n in range(N):
        if n in planted_idx:
            flag[:, n] = True
            for j, me in enumerate(cfg["efm_window"]):
                designated[(j, n)] = pf["efm"]["buyers"][planted_idx[n]][me] - 1
        else:
            for j in r.choice(3, size=target[n], replace=False):
                flag[j, n] = True
    for j, t in enumerate(wnd):
        prev, cur = shares[t - 1], shares[t]
        for n in range(N):
            # non-buyers: better-half managers cannot increase
            inc = bh_idx[cur[bh_idx, n] > prev[bh_idx, n]]
            cur[inc, n] = prev[inc, n]
            if flag[j, n]:
                b = designated.get((j, n))
                if b is None:
                    holders = bh_idx[prev[bh_idx, n] > 0]
                    b = int(r.choice(holders)) if len(holders) else int(r.choice(bh_idx))
                base = prev[b, n] if prev[b, n] > 0 else (aum.mean() / 55.0 / 3.0) / p0.values[n]
                cur[b, n] = base * 1.25
        if t + 1 < T:      # keep later months consistent: carry forward the override as the new base
            pass
    # months after the window (none: the window ends at M0 = last month) are not modified
    return mgr, shares, cids, bh, wnd


def efm_tables(cfg: dict, mgr, shares, cids, bh, wnd, p0: pd.Series, prices: pd.DataFrame | None = None):
    me = month_ends(cfg)
    T, M, N = shares.shape
    # basket (derived from holdings, never hard-coded)
    score = np.zeros(N, int)
    buyers_set = [set() for _ in range(N)]
    netbuy = np.zeros(N)
    for t in wnd:
        diff = shares[t] - shares[t - 1]
        for n in range(N):
            b = np.flatnonzero(bh & (diff[:, n] > 0))
            if len(b):
                score[n] += 1
                buyers_set[n].update(b.tolist())
        netbuy += np.where(bh[:, None], np.clip(diff, 0, None), 0).sum(axis=0) * p0.values
    basket = pd.DataFrame({"company_id": cids, "score": score, "buyers": [len(s) for s in buyers_set],
                           "net_buy_usd": netbuy, "month_end": me[-1]})
    pf = load_planted()
    s3 = set(pf["efm"]["score3"])
    basket["rank_key"] = basket.score * 1e18 + basket.net_buy_usd
    basket = basket.sort_values("rank_key", ascending=False).reset_index(drop=True)
    basket["in_top50"] = False
    basket.loc[:49, "in_top50"] = True
    # held-universe for calibration (design §14.5 PF-13)
    held_val = (shares[-1] * p0.values).sum(axis=0)
    held = pd.Series(held_val, index=cids)
    univ = list(basket.company_id[basket.in_top50]) + [f"C{i:04d}" for i in range(101, 114)]
    univ = list(dict.fromkeys(univ))
    for c in held.sort_values(ascending=False).index:
        if len(univ) >= pf["calibration"]["universe_size"]:
            break
        if c not in univ:
            univ.append(c)
    basket["in_universe200"] = basket.company_id.isin(univ)
    basket = basket.drop(columns=["rank_key"])
    # holdings table
    rows = []
    for t in range(T):
        mm, nn = np.nonzero(shares[t] > 0)
        rows.append(pd.DataFrame({"manager_id": mgr.manager_id.values[mm],
                                  "manager_quality_half": np.where(bh[mm], "better", "worse"),
                                  "company_id": cids[nn], "shares": shares[t][mm, nn], "month_end": me[t]}))
    hold = pd.concat(rows, ignore_index=True)
    if prices is not None:
        px = prices.copy()
        px["month_end"] = px["date"]
        # month-end close = last trading day <= calendar month-end
        px = px.sort_values("date")
        last = {}
        for m_ in me:
            sub = px[px["date"] <= m_].groupby("company_id").tail(1).set_index("company_id")["close"]
            last[m_] = sub
        hold["value_usd"] = [s * last[m_].get(c, p0.get(c, 25.0)) for s, m_, c in zip(hold.shares, hold.month_end, hold.company_id)]
    else:
        hold["value_usd"] = hold.shares * hold.company_id.map(p0)
    mg = mgr.copy()
    mg["better_half"] = bh
    return mg, hold, basket


# ───────────────────────────────── Quant ─────────────────────────────────
def build_quant(cfg: dict, companies: pd.DataFrame) -> pd.DataFrame:
    r = rng(cfg, 5)
    pf = load_planted()
    me = month_ends(cfg)
    uni = companies[companies.msci_em]
    uni = uni[uni.company_id != pf["quant"]["legacy_id"]["company"]]
    n = len(uni)
    base = np.clip(r.normal(50, 18, n), 3, 97)
    cols = ["value", "momentum", "quality", "revisions", "sentiment"]
    out = []
    score = base.copy()
    for t, m_ in enumerate(me):
        score = np.clip(0.8 * score + 0.2 * base + r.normal(0, 6, n), 2, 99)
        s = score.copy()
        for cid, v in pf["quant"]["planted_scores"].items():
            ix = np.flatnonzero(uni.company_id.values == cid)
            if len(ix):
                if m_ >= d("2026-09-30"):
                    s[ix[0]] = v
        contrib = r.dirichlet(np.ones(5), n) * s[:, None]
        out.append(pd.DataFrame({"company_id": uni.company_id.values, "month_end": m_, "score": s,
                                 "drivers": [dict(zip(cols, map(float, row))) for row in contrib]}))
        # legacy id row for Haitian International (PF-02)
        out.append(pd.DataFrame({"company_id": [pf["quant"]["legacy_id"]["legacy"]], "month_end": [m_],
                                 "score": [float(np.clip(r.normal(55, 10), 20, 90))], "drivers": [dict(zip(cols, [11.0] * 5))]}))
    q = pd.concat(out, ignore_index=True)
    q["quintile"] = np.ceil(q.groupby("month_end")["score"].rank(ascending=False, pct=True, method="first") * 5).astype(int).clip(1, 5)
    return q


# ───────────────────────────────── Prices ─────────────────────────────────
def build_prices(cfg: dict, companies: pd.DataFrame, p0: pd.Series, top50: list[str]) -> pd.DataFrame:
    r = rng(cfg, 6)
    pf = load_planted()
    days = business_days(d(cfg["history_start"]), d(cfg["demo_as_of"]))
    D, N = len(days), len(companies)
    sectors = companies.gics_sector.values
    vol = np.array([SECTOR_VOL.get(s, .016) for s in sectors])
    beta = np.clip(r.normal(1.0, 0.25, N), 0.3, 1.8)
    mkt = r.standard_t(4, D) * 0.010 / np.sqrt(2)
    sec_names = sorted(set(sectors))
    sec_f = {s: r.standard_t(4, D) * 0.006 / np.sqrt(2) for s in sec_names}
    idio = r.standard_t(4, (D, N)) / np.sqrt(2) * vol * 0.8
    ret = beta * mkt[:, None] + np.stack([sec_f[s] for s in sectors], axis=1) + idio
    cids = companies.company_id.values
    ix = {c: i for i, c in enumerate(cids)}
    di = {x: i for i, x in enumerate(days)}
    as_of_i = D - 1
    for ov in pf["prices"]:
        n = ix[ov["company"]]
        if "trend_12m_pct" in ov:
            ret[-21:, n] *= 0.6
            ret[:, n] += (np.log(1 + ov["trend_12m_pct"] / 100) - ret[:, n].sum()) / D
            continue
        i0 = di[d(ov["from"])]
        h = ov["horizon_td"]
        i1 = min(i0 + h, as_of_i)
        seg = slice(i0 + 1, i1 + 1)
        target = np.log(1 + ov["ret_pct"] / 100)
        k = i1 - i0
        e = r.normal(0, vol[n] * 0.35, k)
        ret[seg, n] = target / k + (e - e.mean())
        if h == 1:       # step-down, then range-bound
            ret[i1 + 1:, n] *= 0.5
    # basket average 30d return (non-planted names of the top 50), iterative shift over the last 21 days
    planted_ids = {c["id"] for c in pf["companies"]}
    i_start = di[d("2026-10-21")]
    basket_idx = [ix[c] for c in top50]
    free = [ix[c] for c in top50 if c not in planted_ids]
    for _ in range(4):
        R = np.exp(ret[i_start + 1:, :].sum(axis=0)) - 1
        cur = R[basket_idx].mean()
        gap = np.log(1 + pf["basket_avg_ret_30d_pct"] / 100) - np.log(1 + cur)
        ret[i_start + 1:, free] += gap * len(basket_idx) / len(free) / (as_of_i - i_start)
    logp = np.log(p0.values)[None, :] + np.cumsum(ret, axis=0)
    close = np.exp(logp)
    df = pd.DataFrame({"company_id": np.tile(cids, D), "date": np.repeat(days, N),
                       "close": close.reshape(-1), "ret_1d": np.vstack([np.zeros((1, N)), np.diff(close, axis=0) / close[:-1]]).reshape(-1)})
    return df
