"""5-year backtest across all markets, using the same agents, rules, cost model
and simulated book (NOK, currency rule) as the live simulator.

Signals are computed from each day's close; orders fill at the next trading
day's open in that stock's market. Buys only fill if the open is at or below the
limit price; sells fill at the open.

Known biases, stated plainly:
* Survivorship/selection: the universe is today's list of well-known names.
* Value fundamentals and the LLM reviewer are switched off (they would leak the future).
* All markets are decided once per day on the latest closes, not session by session.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .book import SimBook
from .brokers import BacktestGateway, Holding
from .config import Config
from .data import Panels
from .features import compute_features
from .engine import prepare_snapshot
from .markets import MARKETS, Session
from .research import build_research
from datetime import time as dtime
from .team import build_team

Y = 252


@dataclass
class BacktestResult:
    portfolio: str
    nav: pd.DataFrame
    trades: pd.DataFrame
    weights: pd.DataFrame
    metrics: dict
    bench: pd.DataFrame
    decisions: list = field(default_factory=list)
    book: SimBook | None = None
    last_prices: dict = field(default_factory=dict)
    last_fx: dict = field(default_factory=dict)
    research: list = field(default_factory=list)
    ics: dict = field(default_factory=dict)


def perf(series: pd.Series) -> dict:
    s = series.dropna()
    r = s.pct_change().dropna()
    yrs = max(len(s) / Y, 1e-9)
    dd = s / s.cummax() - 1
    return {"total_return": s.iloc[-1] / s.iloc[0] - 1,
            "cagr": (s.iloc[-1] / s.iloc[0]) ** (1 / yrs) - 1,
            "vol": r.std() * np.sqrt(Y),
            "sharpe": (r.mean() * Y) / (r.std() * np.sqrt(Y)) if r.std() > 0 else np.nan,
            "max_drawdown": dd.min()}


def run_backtest(cfg: Config, panels: Panels, universe: pd.DataFrame, portfolio: str,
                 fx_panel: pd.DataFrame, years: int = 5, log=print, research_days: int = 0) -> BacktestResult:
    prof = cfg.portfolios[portfolio]
    team = build_team(cfg, portfolio, universe)
    bench = cfg["data"]["benchmark"]
    feats = compute_features(panels, bench, cfg["data"]["lookback_years"])
    close = panels.close
    opn = panels.open.reindex_like(close)
    closes_ff = close.ffill()
    rets = close.pct_change(fill_method=None)
    start = close.index[-1] - pd.DateOffset(years=years)
    days = close.index[close.index >= start]
    every = int(cfg["schedule"]["rebalance_every_days"])
    tradeable = [s for s in close.columns if s in universe.index]
    ccy = universe.currency

    book = SimBook(cash=prof.start_capital_nok, start_capital=prof.start_capital_nok, cost_cfg=cfg["costs"])
    gw = BacktestGateway()
    nav_rows, w_rows, decisions, research = [], [], [], []
    pseudo = Session("backtest", "All markets (backtest day, decided on closing prices)", "Europe/Oslo",
                     dtime(17, 0), tuple(MARKETS), bench)
    for i, t in enumerate(days):
        fx_t = fx_panel.loc[t].to_dict()
        # 1) fill queued orders at today's open in each market (sells first)
        carry = []
        for o in sorted(gw.queue, key=lambda x: x["side"] != "sell"):
            px = opn.at[t, o["symbol"]]
            if pd.isna(px):
                if not o.get("carried"):
                    carry.append({**o, "carried": True})     # that market was closed today
                continue
            c = ccy[o["symbol"]]
            if o["side"] == "sell":
                book.sell(o["symbol"], o["qty"], px, fx_t[c], str(t.date()), o.get("reason", ""))
            else:
                book.buy(o["symbol"], o["qty"], px, c, fx_t[c], str(t.date()), universe.at[o["symbol"], "market"],
                         o["limit"])
        gw.queue = carry

        # 2) mark to market at the close (positions at entry FX, per the currency rule)
        prices = closes_ff.loc[t].dropna().to_dict()
        book.mark({s: close.at[t, s] for s in book.positions})
        equity = book.equity(prices)
        nav_rows.append({"date": t, "equity": equity, "cash": book.cash, "positions": len(book.positions),
                         "costs": sum(book.costs.values())})

        # 3) learn from matured opinions
        team.learner.evaluate(t, close, feats["vol_60"])

        # 4) the team meets
        full = i % every == 0
        snap = prepare_snapshot(feats, t, universe, fx_t, {}).loc[[s for s in tradeable]]
        snap = snap[snap.close.notna()]
        tables = team.coordinator.assess_all(snap)
        if full and i % 5 == 0:
            team.learner.record(t, tables)
        weights = team.learner.weights()
        if i % 5 == 0:
            w_rows.append({"date": t, **weights})
        hv = book.holdings_value(prices)
        holds = {s: Holding(s, p.qty, p.entry_price, p.entry_date, p.high_water, p.currency, p.entry_fx, hv[s], p.market)
                 for s, p in book.positions.items()}
        intents, score = team.coordinator.decide(snap, tables, weights, holds, equity, book.cash, rets.loc[:t], full,
                                                 today=str(t.date()))
        recs = team.broker.execute(intents, gw, equity, book.cash, dict(hv), set(), str(t.date())) if intents else []
        if i >= len(days) - 60:
            decisions.extend((str(t.date()), it) for it in intents)
        if i >= len(days) - research_days:
            research.append(build_research(portfolio, pseudo, str(t.date()), "17:00", snap, tables, score, team, holds,
                                           intents, recs, universe, weights, fx_t, set(snap.index), equity, book.cash))

    nav = pd.DataFrame(nav_rows).set_index("date")
    trades = pd.DataFrame(book.fills)
    weights = pd.DataFrame(w_rows).set_index("date") if w_rows else pd.DataFrame()
    b = pd.DataFrame(index=nav.index)
    for sym in (bench, cfg["data"]["global_benchmark"]):
        if sym in closes_ff:
            s = closes_ff.loc[nav.index, sym]
            b[sym] = s / s.iloc[0] * prof.start_capital_nok      # buy & hold, FX locked at start
    m = perf(nav.equity)
    sells = trades[trades.side == "sell"] if not trades.empty else trades
    m.update({"trades": len(trades), "win_rate": float((sells.return_pct > 0).mean()) if len(sells) else np.nan,
              "costs_nok": sum(book.costs.values()), "cost_breakdown": dict(book.costs),
              "final_equity_nok": nav.equity.iloc[-1], "final_weights": team.learner.weights(),
              "max_single_buy_pct": _max_buy_pct(trades, nav),
              "fx_pnl_realised_nok": float(sells.fx_pnl_nok.sum()) if len(sells) else 0.0,
              "by_market": trades.assign(m=trades.symbol.map(universe.market)).groupby("m").size().to_dict()
              if len(trades) else {}})
    for c in b.columns:
        m[f"bench_{c}"] = perf(b[c])
    log(f"  {portfolio}: CAGR {m['cagr']:.1%}, max DD {m['max_drawdown']:.1%}, Sharpe {m['sharpe']:.2f}, "
        f"{m['trades']} trades")
    return BacktestResult(portfolio, nav, trades, weights, m, b, decisions, book,
                          closes_ff.iloc[-1].dropna().to_dict(), fx_panel.iloc[-1].to_dict(), research[::-1],
                          {a: [float(np.mean(v)), len(v)] for a, v in team.learner.ics.items() if v})


def _max_buy_pct(trades: pd.DataFrame, nav: pd.DataFrame) -> float:
    """Audit of the user rule: largest buy (NOK, incl. costs) vs previous day's portfolio value."""
    if trades.empty:
        return 0.0
    buys = trades[trades.side == "buy"]
    if not len(buys):
        return 0.0
    prev = nav.equity.shift(1).reindex(pd.to_datetime(buys.date)).values
    return float(np.nanmax(buys.value_nok.values / prev))
