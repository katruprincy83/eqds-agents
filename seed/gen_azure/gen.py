"""Azure SQL synthetic datasets: strategies, accounts, trades, positions, research, feeds (design §14.5).
SYNTHETIC DEMO DATA. Pure pandas/numpy; the classic seed job only adds the JDBC write layer.

Planted facts are built exactly (flows spec), then PF-13 is calibrated by adding adjustment trades so the book-wide
strong conflict / confirm counts and totals hit the targets exactly.
"""
from __future__ import annotations

import datetime as dt
import math

import numpy as np
import pandas as pd

from seed.common import FX, business_days, d, load_planted, month_ends, rng
from seed.gen_docs.text import research_note

BROKERS_EXEC = ["Cardinal Execution", "Pinnacle Markets", "Seabird Trading", "Orchid Securities", "Northstar Dealing", "Lotus Prime", "Halcyon Agency"]


def build_accounts(cfg: dict):
    strat = pd.DataFrame([{"strategy_id": s["strategy_id"], "name": s["name"], "style": s["style"]} for s in cfg["strategies"]])
    rows, k = [], 1
    for s in cfg["strategies"]:
        for _ in range(s["accounts"]):
            rows.append(dict(account_id=f"ACC-{k:03d}", strategy_id=s["strategy_id"], fund_code=s["fund"],
                             mandate=f"{s['name']} mandate {k:02d}", opened_on=dt.date(2015 + k % 8, 1 + k % 12, 1 + k % 27)))
            k += 1
    return strat, pd.DataFrame(rows)


def _rebalance_days(days: list[dt.date]) -> dict[dt.date, int]:
    """month-end x4, quarter-end x8 (last business day of the month)."""
    out = {}
    by_m = {}
    for x in days:
        by_m[(x.year, x.month)] = x
    for (y, m), last in by_m.items():
        out[last] = 8 if m in (3, 6, 9, 12) else 4
    return out


def _price_lookup(prices: pd.DataFrame):
    P = prices.pivot(index="date", columns="company_id", values="close")
    return P


def build_trades(cfg: dict, companies: pd.DataFrame, prices: pd.DataFrame, holdings: pd.DataFrame, basket: pd.DataFrame, accounts: pd.DataFrame):
    r = rng(cfg, 20)
    pf = load_planted()
    as_of = d(cfg["demo_as_of"])
    days = business_days(d(cfg["history_start"]), as_of)
    P = _price_lookup(prices)
    day_idx = {x: i for i, x in enumerate(days)}
    comp = companies.set_index("company_id")
    cids = companies.company_id.values
    em_ids = companies[companies.msci_em].company_id.values
    jp_ids = companies[companies.country == "Japan"].company_id.values
    apac_ids = companies[companies.country.isin(["Japan", "Australia", "Singapore", "China", "India", "Korea"])].company_id.values
    planted_ids = [c["id"] for c in pf["companies"]]
    hard_planted = [f"C{n:04d}" for n in range(101, 114)]
    acct_by_strat = accounts.groupby("strategy_id").account_id.apply(list).to_dict()
    hold = {f: holdings[holdings.fund == f].company_id.values for f in holdings.fund.unique()}
    rb = _rebalance_days(days)

    # ── planted trades (exact) ──
    prow = []
    seq = 0

    def add_rows(company_id, side, total_usd, accounts_by_strat, share, dates, reb_strats, reb_date):
        nonlocal seq
        for s, sh in share.items():
            accs = accounts_by_strat[s]
            amt = total_usd * sh
            use_reb = s in (reb_strats or [])
            ds = [reb_date] if use_reb else dates
            n = max(len(accs), len(ds))
            w = r.dirichlet(np.ones(n) * 4)
            vals = np.round(amt * w, 2)
            vals[-1] = round(amt - vals[:-1].sum(), 2)
            for i in range(n):
                prow.append(dict(account_id=accs[i % len(accs)], strategy_id=s, company_id=company_id, side=side, value_usd=float(vals[i]),
                                 trade_date=d(ds[i % len(ds)]), is_rebalance=int(use_reb)))
    for f in pf["flows"]:
        for side, key in (("S", "sells"), ("B", "buys")):
            if key in f:
                x = f[key]
                add_rows(f["company"], side, x["total_usd"], x["accounts"], x["strategy_share"], x["dates"], x.get("rebalance_strategies"), x.get("rebalance_date"))
    planted = pd.DataFrame(prow)

    # ── base trades ──
    n_base_target = cfg["n_trades"] - len(planted) - 1500            # leave room for calibration trades
    mix = {s["strategy_id"]: s["count_share"] for s in cfg["strategies"]}
    cnt = {k: int(round(v / sum(mix.values()) * n_base_target)) for k, v in mix.items()}
    scfg = {s["strategy_id"]: s for s in cfg["strategies"]}
    frames = []
    ndays = len(days)
    wd = np.ones(ndays)
    for s, n in cnt.items():
        w = wd.copy()
        if s == "NCREP":
            for i, x in enumerate(days):
                if x in rb:
                    w[i] = rb[x]
        w = w / w.sum()
        di = r.choice(ndays, size=n, p=w)
        accs = np.array(acct_by_strat[s])
        acc = accs[r.integers(0, len(accs), n)]
        if s == "NCREP":
            p = companies.set_index("company_id").loc[em_ids, "market_cap_usd"].values
            co = r.choice(em_ids, n, p=p / p.sum())
        elif s == "JPA":
            co = r.choice(jp_ids, n)
        elif s == "APINC":
            co = r.choice(apac_ids, n)
        elif s == "EMQ":
            co = r.choice(em_ids, n)
        else:
            fund = scfg[s]["fund"]
            inh = r.random(n) < 0.6
            co = np.where(inh, r.choice(hold[fund], n), r.choice(em_ids, n))
        val = r.lognormal(np.log(scfg[s]["median_usd"]), scfg[s]["sigma"], n)
        frames.append(pd.DataFrame({"strategy_id": s, "account_id": acc, "company_id": co, "di": di, "value_usd": val,
                                    "side": np.where(r.random(n) < 0.5, "B", "S")}))
    base = pd.concat(frames, ignore_index=True)
    base["trade_date"] = [days[i] for i in base.di]
    base["is_rebalance"] = ((base.strategy_id == "NCREP") & base.trade_date.isin(list(rb))).astype(int)
    # planted names carry no base trades from 2026-09-15 on (their window is fully specified)
    cut = d("2026-09-15")
    base = base[~(base.company_id.isin(hard_planted) & (base.trade_date >= cut))].drop(columns="di")

    # ── PF-13 calibration ──
    cal = pf["calibration"]
    win_start = as_of + dt.timedelta(days=int(pf["window"]["start"][:-1]))
    allt = pd.concat([base, planted], ignore_index=True)
    inwin = allt[(allt.trade_date >= win_start) & (allt.trade_date <= as_of)].copy()
    inwin["signed"] = np.where(inwin.side == "B", inwin.value_usd, -inwin.value_usd)
    net = inwin.groupby("company_id").signed.sum()
    univ = basket[basket.in_universe200].company_id.tolist()
    top50 = set(basket[basket.in_top50].company_id)
    strong = cal["strong_musd"] * 1e6
    pl_net = {c: net.get(c, 0.0) for c in hard_planted if c in univ}
    pl_conf = {c: v for c, v in pl_net.items() if v <= -strong}
    pl_conf_ok = {c: v for c, v in pl_net.items() if v >= strong}
    free = [c for c in univ if c not in hard_planted]
    free_top = [c for c in free if c in top50]
    free_oth = [c for c in free if c not in top50]
    n_conf = cal["conflicts"]["count"] - len(pl_conf)
    n_conf_ok = cal["confirms"]["count"] - len(pl_conf_ok)
    conf_total = cal["conflicts"]["total_musd"] * 1e6 - sum(abs(v) for v in pl_conf.values())
    ok_total = cal["confirms"]["total_musd"] * 1e6 - sum(pl_conf_ok.values())
    k_top = min(len(free_top) // 2, n_conf // 2)
    conf_names = list(r.choice(free_top, k_top, replace=False)) + list(r.choice(free_oth, n_conf - k_top, replace=False))
    rest = [c for c in free_oth if c not in conf_names]
    ok_names = list(r.choice(rest, n_conf_ok, replace=False))

    def split(total, k, floor=1.05 * strong):
        w = r.lognormal(0, 0.6, k)
        extra = total - floor * k
        return floor + extra * w / w.sum()
    target = {}
    for c, v in zip(conf_names, split(conf_total, len(conf_names))):
        target[c] = -v
    for c, v in zip(ok_names, split(ok_total, len(ok_names))):
        target[c] = v
    for c in free:
        if c not in target:
            target[c] = r.uniform(-0.4, 0.4) * strong
    win_days = [x for x in days if win_start <= x <= as_of]
    active_accts = acct_by_strat["EMA"] + acct_by_strat["GEMF"]
    adj = []
    for c, tgt in target.items():
        delta = tgt - float(net.get(c, 0.0))
        k = max(2, int(math.ceil(abs(delta) / 9e6)) + 1)
        w = r.dirichlet(np.ones(k) * 3)
        vals = np.round(abs(delta) * w, 2)
        vals[-1] = round(abs(delta) - vals[:-1].sum(), 2)
        side = "B" if delta > 0 else "S"
        for v in vals:
            a = active_accts[r.integers(0, len(active_accts))]
            adj.append(dict(account_id=a, strategy_id="EMA" if a in acct_by_strat["EMA"] else "GEMF", company_id=c, side=side, value_usd=float(v),
                            trade_date=win_days[r.integers(0, len(win_days))], is_rebalance=0))
    trades = pd.concat([base, planted, pd.DataFrame(adj)], ignore_index=True)

    # ── finish rows: id, ts, price, qty ──
    trades = trades.sort_values(["trade_date", "account_id", "company_id"], kind="mergesort").reset_index(drop=True)
    n = len(trades)
    trades["trade_id"] = np.arange(1, n + 1, dtype=np.int64)
    secs = r.integers(9 * 3600, 16 * 3600, n)
    trades["trade_ts"] = pd.to_datetime(trades.trade_date) + pd.to_timedelta(secs, unit="s")
    trades["booked_at"] = trades.trade_ts + pd.to_timedelta(r.integers(30, 600, n), unit="s")
    ci = comp.loc[trades.company_id]
    px = P.stack().rename("close")
    key = pd.MultiIndex.from_arrays([trades.trade_date.values, trades.company_id.values])
    trades["close_usd"] = px.reindex(key).values
    trades["close_usd"] = trades.close_usd.fillna(25.0)
    trades["quantity"] = np.maximum(1, np.round(trades.value_usd / trades.close_usd)).astype(np.int64)
    fx = ci.currency.map(FX).fillna(1.0).values
    trades["price_local"] = np.round(trades.close_usd.values * fx, 4)
    trades["ticker"] = ci.ticker.values
    trades["exec_broker"] = r.choice(BROKERS_EXEC, n)
    trades["pm_name"] = None                                    # PF-18: deliberately NULL everywhere
    trades["value_usd"] = trades.value_usd.round(2)
    cols = ["trade_id", "trade_ts", "trade_date", "account_id", "strategy_id", "company_id", "ticker", "side", "quantity", "price_local",
            "value_usd", "exec_broker", "is_rebalance", "pm_name", "booked_at"]
    return trades[cols], universe_info(univ, top50, target, pl_net)


def universe_info(univ, top50, target, pl_net):
    return {"universe200": univ, "top50": sorted(top50), "targets": {k: float(v) for k, v in target.items()}, "planted_net": {k: float(v) for k, v in pl_net.items()}}


def build_positions(cfg: dict, trades: pd.DataFrame, prices: pd.DataFrame, holdings: pd.DataFrame, accounts: pd.DataFrame, companies: pd.DataFrame):
    r = rng(cfg, 21)
    as_of = d(cfg["demo_as_of"])
    days = business_days(d(cfg["history_start"]), as_of)
    pdays = days[-cfg["positions_business_days"]:]
    P = _price_lookup(prices)
    hold = {f: holdings[holdings.fund == f].set_index("company_id").weight for f in holdings.fund.unique()}
    t = trades[trades.trade_date >= pdays[0]].copy()
    t["nq"] = np.where(t.side == "B", t.quantity, -t.quantity)
    daily = t.groupby(["account_id", "company_id", "trade_date"]).nq.sum()
    fund_of = accounts.set_index("account_id").fund_code.to_dict()
    strat_of = accounts.set_index("account_id").strategy_id.to_dict()
    dm_ids = companies[companies.country == "Japan"].company_id.values
    # base holdings per account
    pairs = {}
    for a in accounts.account_id:
        f = fund_of[a]
        if f in hold:
            names = list(hold[f].index)
            w = hold[f].values
        else:
            names = list(r.choice(dm_ids, 40, replace=False))
            w = r.dirichlet(np.ones(40) * 2)
        aum = 5e8 * (1 + 0.1 * (int(a[-3:]) % 7))
        for nme, wi in list(zip(names, w))[:30]:
            px0 = P.loc[pdays[0], nme]
            pairs[(a, nme)] = max(1.0, round(wi * aum / px0))
    # traded pairs are tracked when material: planted names, any trade >= USD 2m, or net window flow >= USD 1m
    hard = {f"C{n:04d}" for n in range(101, 114)}
    big = t[(t.value_usd >= 5e6) | t.company_id.isin(hard)]
    traded_all = set(daily.index.droplevel(2))
    keep_pairs = set(zip(big.account_id, big.company_id))
    traded = {k for k in traded_all if k in keep_pairs or k in pairs}
    for k in traded:
        pairs.setdefault(k, 0.0)
    rows = []
    bench = companies.set_index("company_id").market_cap_usd
    bench = bench[companies.set_index("company_id").msci_em] if False else bench
    bench_w = (bench / bench.sum() * 1e4)
    dayidx = {x: i for i, x in enumerate(pdays)}
    for (a, c), base_q in pairs.items():
        dq = np.zeros(len(pdays))
        if (a, c) in traded:
            sub = daily.loc[(a, c)]
            for dd, q in sub.items():
                dq[dayidx[dd]] += q
        cum = np.cumsum(dq)
        init = base_q + max(0.0, -cum.min())
        qty = init + cum
        keep = qty != 0
        if not keep.any():
            continue
        px = P[c].reindex(pdays).values
        rows.append(pd.DataFrame({"as_of": np.array(pdays)[keep], "account_id": a, "company_id": c, "quantity": qty[keep],
                                  "market_value_usd": np.round(qty[keep] * px[keep], 2)}))
    pos = pd.concat(rows, ignore_index=True)
    aum = pos.groupby(["as_of", "account_id"]).market_value_usd.transform("sum")
    pos["weight_bps"] = np.round(pos.market_value_usd / aum * 1e4).astype(int)
    active = pos.account_id.map(strat_of).isin(["EMA", "GEMF", "JPA", "APINC"])
    pos["active_weight_bps"] = np.where(active, (pos.weight_bps - pos.company_id.map(bench_w).fillna(0).round()).astype("Int64"), pd.NA)
    pos["quantity"] = pos.quantity.round(0)
    return pos


# ───────────────────────────── research & feeds ─────────────────────────────
def build_research(cfg: dict, companies: pd.DataFrame):
    r = rng(cfg, 22)
    pf = load_planted()
    as_of = d(cfg["demo_as_of"])
    an = cfg["analysts"]
    comp = companies.set_index("company_id")
    cover = []
    pool = list(companies.company_id)
    for a in an:
        k = int(r.integers(30, 61))
        cover += [(a, c) for c in r.choice(pool, k, replace=False)]
    views, notes = [], []
    ratings = r.choice(["OW", "N", "UW"], 10000, p=[.35, .45, .20])
    aws = [-60, -30, 0, 30, 60, 90]
    ri = 0
    for a, c in cover:
        if c == "C0102":
            continue
        t = as_of - dt.timedelta(days=int(r.integers(0, 120)))
        for _ in range(int(r.poisson(6.9)) + 1):
            if (as_of - t).days > 540:
                break
            rt = ratings[ri % len(ratings)]
            ri += 1
            aw = int(r.choice(aws)) if rt != "N" else int(r.choice([-30, 0, 30]))
            views.append(dict(company_id=c, analyst=a, rating=rt, active_weight_bps=aw, effective_date=t,
                              rationale=f"{a.split()[0]}: {rt} on {comp.loc[c, 'name']} given {r.choice(['earnings revisions', 'valuation', 'positioning', 'channel checks', 'balance-sheet strength'])}."))
            t -= dt.timedelta(days=int(r.exponential(120)) + 14)
    for v in pf["research"]["views"]:
        views.append(dict(company_id=v["company"], analyst=v["analyst"], rating=v["rating"], active_weight_bps=v["active_weight_bps"],
                          effective_date=d(v["effective_date"]), rationale=v["rationale"]))
    vdf = pd.DataFrame(views)
    # notes: 70% tied to a view change
    tied = vdf.sample(frac=1, random_state=cfg["seed"]).head(int(cfg["n_notes"] * 0.7))
    for _, v in tied.iterrows():
        notes.append(dict(company_id=v.company_id, analyst=v.analyst, published_at=dt.datetime.combine(v.effective_date, dt.time(8, int(r.integers(0, 59)))) + dt.timedelta(days=int(r.integers(0, 3))),
                          title=f"{comp.loc[v.company_id, 'name']}: {v.rating} view, {v.active_weight_bps:+d} bps active weight"))
    others = cfg["n_notes"] - len(notes)
    for _ in range(others):
        a, c = cover[int(r.integers(0, len(cover)))]
        notes.append(dict(company_id=c, analyst=a, published_at=dt.datetime.combine(as_of - dt.timedelta(days=int(r.integers(0, 400))), dt.time(int(r.integers(7, 18)), int(r.integers(0, 59)))),
                          title=f"{comp.loc[c, 'name']}: {r.choice(['quarterly update', 'model refresh', 'meeting notes', 'sector read-across'])}"))
    ndf = pd.DataFrame(notes)
    ndf = ndf[~((ndf.company_id == "C0102") & (ndf.published_at >= dt.datetime(2026, 9, 1)))]
    for n_ in pf["research"]["notes"]:
        ndf = pd.concat([ndf, pd.DataFrame([dict(company_id=n_["company"], analyst=n_["analyst"], published_at=dt.datetime.fromisoformat(n_["published_at"]), title=n_["title"])])], ignore_index=True)
    ndf = ndf.sort_values("published_at").reset_index(drop=True)
    ndf["note_id"] = [f"RN-{i + 1:06d}" for i in range(len(ndf))]
    fixed = {"weichai_note_1": "We cut Weichai Power A to 30 bps overweight (from 60 bps). A US order banning Chinese equipment in data centres creates a pause in data-centre genset orders, which has been the key growth driver. We keep an overweight because the valuation is undemanding and the order book is still strong.",
             "weichai_note_2": "Following our 15 September change, we assess exposure to the US order: North America is about 14% of revenue and rising, mostly data-centre gensets. The US ruling is expected in late December and is the key catalyst; a ban would remove the incremental demand while a delay would restore it."}
    key = {n_["title"]: n_["body_key"] for n_ in pf["research"]["notes"]}
    bodies = []
    for _, n_ in ndf.iterrows():
        bodies.append(research_note(r, comp.loc[n_.company_id].to_dict(), n_.analyst, n_.title, str(n_.published_at), fixed.get(key.get(n_.title)),
                                    words=int(r.integers(300, 700))))
    ndf["body"] = bodies
    return vdf.sort_values(["effective_date"]).reset_index(drop=True), ndf[["note_id", "company_id", "analyst", "published_at", "title", "body"]]


HEAD = ["{n} shares {mv} after {ev}", "{n} reports {ev}", "{n}: analysts {act} targets ahead of results", "{n} wins {ev2}", "{n} flags {risk}",
        "{n} announces {ev2}", "{n} says {ctx}"]
EV = ["third-quarter profit beats estimates", "stronger-than-expected order intake", "weaker margin outlook", "guidance upgrade", "capacity expansion plan"]
EV2 = ["new supply contract", "strategic partnership", "regulatory approval", "buyback programme", "dividend increase"]
ACT = ["raise", "trim", "reiterate", "cut"]
RISK = ["currency headwinds", "input-cost pressure", "supply-chain delays", "slowing demand in export markets"]
CTX = ["demand remains resilient", "inventory is normalising", "pricing is stable", "capex will rise next year"]


def build_news(cfg: dict, companies: pd.DataFrame, earnings: pd.DataFrame):
    r = rng(cfg, 23)
    pf = load_planted()
    as_of = d(cfg["demo_as_of"])
    end = dt.datetime.combine(as_of, dt.time(8, 0))
    n = cfg["n_news"]
    cid = companies.company_id.values
    w = companies.market_cap_usd.values ** 0.7 * r.lognormal(0, 0.5, len(cid))
    w = w / w.sum()
    ts = end - pd.to_timedelta(r.random(n) * 90 * 86400, unit="s")
    co = r.choice(cid, n, p=w)
    macro = r.random(n) < 0.30
    names = companies.set_index("company_id").name
    tmpl = r.integers(0, len(HEAD), n)
    head = []
    for i in range(n):
        t = HEAD[tmpl[i]]
        nm = names[co[i]]
        head.append(t.format(n=nm, mv=r.choice(["jump", "slip", "edge higher", "fall"]), ev=r.choice(EV), ev2=r.choice(EV2), act=r.choice(ACT), risk=r.choice(RISK), ctx=r.choice(CTX)))
    macro_h = ["Asian equities mixed as investors weigh rate outlook", "Regional PMI readings point to steady manufacturing growth", "Oil prices steady ahead of OPEC meeting",
               "Yuan holds range as policy support continues", "Chip equipment orders rebound in latest industry data", "Emerging-market funds see fourth week of inflows"]
    df = pd.DataFrame({"published_at": ts.floor("s"), "company_id": np.where(macro, None, co),
                       "headline": [r.choice(macro_h) if m else h for m, h in zip(macro, head)],
                       "source": r.choice(cfg["wires"], n), "tags": np.where(macro, "macro", "company")})
    df["body"] = "SYNTHETIC DEMO DATA. " + df.headline + ". " + r.choice(["Analysts said the move reflected positioning ahead of results.", "Traders cited thin liquidity.", "The company declined to comment."], n)
    for x in pf["news"]:
        df = pd.concat([df, pd.DataFrame([dict(published_at=pd.Timestamp(x["published_at"]), company_id=x["company"], headline=x["headline"], source=x["source"],
                                               tags=x["tags"], body="SYNTHETIC DEMO DATA. " + x["headline"] + ". " + NEWS_BODIES[x["body_key"]])])], ignore_index=True)
    df = df.sort_values("published_at").reset_index(drop=True)
    df["news_id"] = np.arange(1, len(df) + 1, dtype=np.int64)
    return df[["news_id", "published_at", "company_id", "headline", "source", "body", "tags"]]


NEWS_BODIES = {
    "us_ruling": "A ruling by US authorities on imports of Chinese power equipment, including gensets used in data centres, is expected in late December, according to people familiar with the matter.",
    "trucks": "Heavy-truck sales in China fell 13% in October from a year earlier as construction demand weakened, industry data showed.",
    "kioxia_capacity": "An executive at Kioxia said NAND capacity is sold out through 2027 as data-centre customers pre-book supply.",
}


def build_web_index(cfg: dict, companies: pd.DataFrame):
    r = rng(cfg, 24)
    as_of = d(cfg["demo_as_of"])
    names = companies.set_index("company_id").name
    rows = []
    sites = ["marketwatch", "regionbiz", "industrynotes", "techbrief", "energydesk"]
    for i in range(cfg["n_web"] - 6):
        c = companies.company_id.values[int(r.integers(0, len(companies)))]
        t = f"{names[c]}: {r.choice(['outlook', 'earnings preview', 'sector analysis', 'valuation snapshot'])}"
        rows.append(dict(url=f"https://{r.choice(sites)}.example.com/{c.lower()}/{i}", title=t,
                         snippet=f"SYNTHETIC DEMO DATA. {t}. " + r.choice(["Shares have traded in a range.", "Analysts expect steady growth.", "Margins are expected to stabilise."]),
                         published_at=dt.datetime.combine(as_of - dt.timedelta(days=int(r.integers(0, 120))), dt.time(9)), company_id=c, keywords=f"{names[c].lower()},outlook"))
    planted = [("C0102", "Weichai genset shipments climb from 500 to 1,300 units on data-centre demand", "weichai,genset,data-centre,shipments"),
               (None, "US ruling on Chinese power equipment expected in late December", "us,china,power equipment,ruling,december"),
               ("C0102", "Four brokers cut Weichai targets after US order on Chinese equipment", "weichai,targets,brokers,us order"),
               (None, "China heavy-truck sales fall 13% in October", "china,heavy truck,sales,october"),
               ("C0101", "Kioxia NAND capacity sold out through CY27", "kioxia,nand,capacity"),
               ("C0102", "Weichai trades on about 9x 2027 earnings with a 5% yield", "weichai,valuation,yield")]
    for j, (c, t, kw) in enumerate(planted):
        rows.append(dict(url=f"https://industrynotes.example.com/planted/{j}", title=t, snippet=f"SYNTHETIC DEMO DATA. {t}.",
                         published_at=dt.datetime.combine(as_of - dt.timedelta(days=3 + j), dt.time(8)), company_id=c, keywords=kw))
    df = pd.DataFrame(rows)
    df.insert(0, "doc_id", np.arange(1, len(df) + 1))
    return df
