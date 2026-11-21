"""One trading session (Nordic / Europe / US) for one portfolio.

Runs about an hour after the session's markets open: refresh prices and FX, let
the analysts assess every stock in those markets, run the debate, check stops on
the holdings that trade there, place orders through the portfolio's broker, then
write the research note and update the journal.
"""
from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from .alpaca_client import AlpacaClient, Credentials
from .book import SimBook
from .brokers import AlpacaGateway, IBKRGateway, SimGateway
from .config import Config
from .data import DataStore, load_universe, yahoo_scales
from .debate import apply_reviews, brief, review
from .features import compute_features, snapshot
from .fundamentals import load_fundamentals
from .fx import FxRates, StartLock
from .ledger import Ledger
from .markets import MARKETS, SESSIONS
from .research import build_research
from .sources import AlpacaSource, YahooSource
from .team import build_team

STYLE = {"MAIN": "balanced: trend-following with value and risk discipline",
         "HIGH_RISK": "aggressive: momentum-led, concentrated, accepts large swings"}


def make_alpaca(cfg: Config, portfolio: str) -> AlpacaClient:
    a = cfg["alpaca"]
    creds = Credentials.from_env(cfg.portfolios[portfolio].env_prefix)
    return AlpacaClient(creds, a["trading_url"], a["data_url"], a.get("allow_live", False), a["data_feed"])


def load_book(cfg: Config, ledger: Ledger, portfolio: str) -> SimBook:
    blob = ledger.get(portfolio, "sim_book")
    if blob:
        return SimBook.loads(blob, cfg["costs"])
    cap = cfg.portfolios[portfolio].start_capital_nok
    return SimBook(cash=cap, start_capital=cap, cost_cfg=cfg["costs"])


def prepare_snapshot(feats, t, universe, fx_now: dict, live: dict[str, float]) -> pd.DataFrame:
    snap = snapshot(feats, t)
    snap = snap.loc[[s for s in snap.index if s in universe.index]].copy()
    for s, px in live.items():
        if s in snap.index:
            snap.at[s, "close"] = px
    ccy = universe.currency.reindex(snap.index)
    snap["fx"] = ccy.map(lambda c: fx_now.get(c, float("nan"))).astype(float)
    usd = fx_now.get("USD", 10.0)
    snap["close_usd"] = snap.close * snap.fx / usd
    snap["adv20_usd"] = snap.adv20 * snap.fx / usd
    return snap


def run_session(cfg: Config, portfolio: str, session_name: str, dry_run: bool = False,
                force: bool = False, log=print) -> dict:
    prof = cfg.portfolios[portfolio]
    session = SESSIONS[session_name]
    state = cfg.path("state")
    ledger = Ledger(state / "ledger.sqlite")
    oslo_now = datetime.now(ZoneInfo("Europe/Oslo"))
    today = oslo_now.strftime("%Y-%m-%d")
    universe = load_universe(cfg.path(cfg["universe"]["file"]))
    if prof.broker == "alpaca" and session_name != "us":
        return {"portfolio": portfolio, "skipped": "Alpaca trades US symbols only"}

    session_syms = [s for s in universe.index if universe.at[s, "market"] in session.markets]
    bench = cfg["data"]["benchmark"]

    # --- currency rates
    fx = FxRates(state, cfg["fx"].get("manual_rates"))
    fx.refresh(log=log)
    fx_now = {c: fx.rate(c) for c in set(universe.currency) | {"USD"}}

    # --- prices
    if prof.broker == "alpaca":
        client = make_alpaca(cfg, portfolio)
        amap = {s: (a or s) for s, a in universe.alpaca_symbol.items() if universe.at[s, "market"] == "US"}
        source, cache = AlpacaSource(client, amap), cfg.path(cfg["data"]["cache_dir"]) / "alpaca"
    else:
        source, cache = YahooSource(yahoo_scales(universe)), cfg.path(cfg["data"]["cache_dir"]) / "yahoo"
    store = DataStore(cache)
    log(f"[{portfolio}/{session_name}] refreshing {len(session_syms)} symbols ...")
    extra = {bench, cfg["data"]["global_benchmark"]} if prof.broker != "alpaca" else {bench}
    missing = store.update(source, sorted(set(session_syms) | extra), cfg["data"]["history_start"], log)
    if missing:
        log(f"  no data for: {', '.join(missing[:15])}{' ...' if len(missing) > 15 else ''}")
    try:
        live_raw = source.latest(session_syms)
    except Exception as e:  # noqa: BLE001
        log(f"  ! live prices unavailable ({e})")
        live_raw = {}

    # --- is the market actually open today? (holiday detection)
    probe = live_raw.get(session.probe)
    tz = ZoneInfo(MARKETS[session.markets[0]].tz)
    traded_today = probe is not None and _local_date(probe[1], tz) == datetime.now(tz).date()
    if not traded_today and not force:
        log(f"[{portfolio}/{session_name}] no trades today in {session.probe} - market closed/holiday; skipping.")
        ledger.save_research(portfolio, today, session_name, {"status": "closed", "session_label": session.label,
                                                              "date": today, "time": oslo_now.strftime("%H:%M")})
        return {"portfolio": portfolio, "skipped": "market closed"}
    live = {s: p for s, (p, _) in live_raw.items()}

    # --- features on completed daily bars (today's partial bar excluded), then live price on top
    all_syms = [s for s in universe.index if store.read(s) is not None]
    panels = store.panels(all_syms, before=pd.Timestamp(today))
    feats = compute_features(panels, bench, cfg["data"]["lookback_years"])
    t = panels.dates[-1]
    snap = prepare_snapshot(feats, t, universe, fx_now, live)
    last_close = panels.close.ffill().iloc[-1].to_dict()
    prices = {**{k: v for k, v in last_close.items() if v == v}, **live}

    # --- broker
    book = None
    if prof.broker == "sim":
        book = load_book(cfg, ledger, portfolio)
        book.mark({s: last_close.get(s) for s in book.positions})
        gw = SimGateway(book, prices, fx_now, universe, today)
    elif prof.broker == "ibkr":
        gw = IBKRGateway(cfg["ibkr"], universe, prices, fx_now, ledger, portfolio, today, MARKETS, dry_run)
    else:
        lock = StartLock(state).lock(portfolio, prof.start_capital_nok, fx_now["USD"])
        gw = AlpacaGateway(client, universe, lock["usdnok"], ledger, portfolio, today, dry_run)
    acct = gw.account()

    # --- the team
    fund = (load_fundamentals([s for s in session_syms if s in snap.index], state / f"fundamentals_{session_name}.json", log)
            if cfg["data"].get("use_fundamentals", True) else {})
    team = build_team(cfg, portfolio, universe, fund)
    team.learner.load(ledger.get(portfolio, "learner"))
    ics = team.learner.evaluate(t, panels.close, feats["vol_60"])
    if ics:
        ledger.log_ic(portfolio, ics)
    weights = team.learner.weights()

    review_key = f"last_review_{session_name}"
    last = ledger.get(portfolio, review_key)
    full = force or not last or (pd.Timestamp(today) - pd.Timestamp(last)).days >= int(cfg["schedule"]["rebalance_every_days"])

    analyse = snap.loc[[s for s in snap.index if s in set(session_syms) | set(acct.holdings)]]
    tables = team.coordinator.assess_all(analyse)
    rets = panels.close.pct_change(fill_method=None)
    intents, score = team.coordinator.decide(analyse, tables, weights, acct.holdings, acct.equity, acct.cash,
                                             rets, full, set(session_syms), today)

    review_note = ""
    if full and cfg["debate"].get("use_llm") and any(i.side == "buy" for i in intents):
        hs = ", ".join(f"{s} {h.value_base / acct.equity:.0%}" for s, h in acct.holdings.items()) or "none"
        rv = review(intents, portfolio, STYLE.get(portfolio, ""), hs, cfg["debate"]["model"],
                    cfg["debate"]["max_candidates"])
        review_note = rv.get("_error", {}).get("reason", "") if "_error" in rv else (f"{len(rv)} LLM reviews" if rv else "")
        intents = apply_reviews(intents, rv)

    run_id = ledger.new_run(portfolio, f"{prof.broker}{'-dry' if dry_run else ''}/{session_name}", full, weights,
                            review_note)
    ledger.log_decisions(run_id, portfolio, today, intents)

    if dry_run and prof.broker == "sim":
        gw = _DryGateway()
    pos_val = {s: h.value_base for s, h in acct.holdings.items()}
    bought_today = ledger.bought_on(portfolio, today)
    sells = [i for i in intents if i.side == "sell"]
    buys = [i for i in intents if i.side == "buy"]
    records = team.broker.execute(sells, gw, acct.equity, acct.cash, pos_val, bought_today, today)
    if sells and buys and hasattr(gw, "wait_for_sells"):
        gw.wait_for_sells()
        acct = gw.account()
        pos_val = {s: h.value_base for s, h in acct.holdings.items()}
    records += team.broker.execute(buys, gw, acct.equity, acct.cash, pos_val, bought_today, today)
    ledger.log_orders(run_id, portfolio, today, records)

    if full:
        ledger.put(portfolio, review_key, today)
    learn_key = f"last_learn_{session_name}"
    last_learn = ledger.get(portfolio, learn_key)
    if not dry_run and (not last_learn or (pd.Timestamp(today) - pd.Timestamp(last_learn)).days >= 7):
        team.learner.record(t, tables)          # weekly snapshot per session keeps the track record unbiased
        ledger.put(portfolio, learn_key, today)
    ledger.put(portfolio, "learner", team.learner.dump())
    ledger.put(portfolio, "weights", json.dumps(weights))
    if book is not None and not dry_run:
        ledger.put(portfolio, "sim_book", book.dumps())

    acct = gw.account() if not isinstance(gw, _DryGateway) else acct
    _snapshot_portfolio(ledger, portfolio, today, acct, prices, fx_now, universe, book)

    text_brief = ""
    rec = build_research(portfolio, session, today, oslo_now.strftime("%H:%M"), analyse, tables, score, team,
                         acct.holdings, intents, records, universe, weights, fx_now, set(session_syms),
                         acct.equity, acct.cash)
    if cfg["debate"].get("daily_brief") and not dry_run:
        text_brief = brief(rec, cfg["debate"]["model"])
        rec["brief"] = text_brief
    ledger.save_research(portfolio, today, session_name, rec)
    _export_benchmarks(store, cfg, state)

    log(f"[{portfolio}/{session_name}] value NOK {acct.equity:,.0f}  cash NOK {acct.cash:,.0f}  "
        f"{len(records)} orders{' (dry run)' if dry_run else ''}")
    for r in records:
        log(f"   {r.side.upper():4} {r.qty:>6} {r.symbol:<11} {r.status:<9} {r.reason}"
            + (f"  [{'; '.join(r.broker_notes)}]" if r.broker_notes else ""))
    return {"portfolio": portfolio, "session": session_name, "equity": acct.equity, "orders": records,
            "intents": intents, "research": rec}


def _export_benchmarks(store, cfg, state):
    """Index prices for comparison, saved with the state so reviews can read them without internet."""
    cols = {}
    for b in (cfg["data"]["global_benchmark"], cfg["data"]["benchmark"]):
        df = store.read(b)
        if df is not None:
            cols[b] = df.close.iloc[-800:]
    if cols:
        pd.DataFrame(cols).to_csv(state / "benchmarks.csv")


def _local_date(ts, tz):
    ts = pd.Timestamp(ts)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.tz_convert(tz).date()


class _DryGateway:
    name = "dry"

    def submit(self, *a, **k):
        return {"status": "dry-run"}


def _snapshot_portfolio(ledger, portfolio, today, acct, prices, fx_now, universe, book):
    hold = []
    for s, h in acct.holdings.items():
        px = prices.get(s, h.entry_price)
        hold.append({"symbol": s, "name": universe.at[s, "name"] if s in universe.index else s,
                     "market": h.market, "currency": h.currency, "qty": h.qty, "entry_price": h.entry_price,
                     "price": px, "entry_fx": h.entry_fx, "value_nok": h.value_base,
                     "return_local": px / h.entry_price - 1 if h.entry_price else 0,
                     "fx_effect_not_counted": h.qty * px * (fx_now.get(h.currency, h.entry_fx) - h.entry_fx),
                     "entry_date": h.entry_date,
                     "region": universe.at[s, "region"] if s in universe.index else "",
                     "sector": universe.at[s, "sector"] if s in universe.index else ""})
    costs = book.costs if book is not None else {}
    ledger.put(portfolio, "holdings_view", json.dumps({"date": today, "equity": acct.equity, "cash": acct.cash,
                                                        "holdings": hold, "costs": costs}))
    ledger.log_nav(portfolio, today, acct.equity, acct.cash, acct.equity, sum(costs.values()) if costs else 0.0)
    if book is not None:
        ledger.put(portfolio, "realized", json.dumps(book.realized[-500:]))
