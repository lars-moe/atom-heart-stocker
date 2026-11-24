#!/usr/bin/env python3
"""Atom Heart Stocker 1.0 - cooperating trading agents, paper money, Oslo + Europe + US.

  python main.py init                       check FX, data source and brokers
  python main.py fetch                      download/refresh daily history for all markets
  python main.py backtest [-p MAIN]         5-year backtest -> reports/backtest_dashboard.html
  python main.py session -s nordic|europe|us [-p MAIN] [--dry-run] [--force]
                                            run one session now
  python main.py scheduler                  run all sessions at their market times, every trading day
  python main.py due                        run whichever session is due now (for cron / GitHub Actions)
  python main.py dashboard                  rebuild reports/dashboard.html
  python main.py serve                      show the dashboard at http://localhost:8050
  python main.py demo                       offline demo on SYNTHETIC data -> reports/demo_dashboard.html
"""
from __future__ import annotations

import argparse
import sys

from ahs.config import load_config
from ahs.data import DataStore, load_universe, yahoo_scales


def pnames(cfg, p):
    return list(cfg.portfolios) if p in (None, "all") else [p]


def cmd_init(cfg, args):
    from ahs.fx import FxRates
    from ahs.markets import schedule_table
    fx = FxRates(cfg.path("state"), cfg["fx"].get("manual_rates"))
    fx.refresh()
    print("FX (NOK per unit, Norges Bank):", {c: round(fx.rate(c), 4) for c in ("USD", "EUR", "SEK", "DKK", "GBP", "CHF")})
    try:
        import yfinance  # noqa: F401
        print("yfinance installed: OK")
    except ImportError:
        print("! pip install yfinance  (needed for Oslo/European prices)")
    for p, prof in cfg.portfolios.items():
        print(f"[{p}] broker = {prof.broker}, start capital NOK {prof.start_capital_nok:,.0f}")
        if prof.broker == "ibkr":
            try:
                from ib_async import IB  # type: ignore
                ib = IB()
                ib.connect(cfg["ibkr"]["host"], int(cfg["ibkr"]["port"]), clientId=int(cfg["ibkr"]["client_id"]) + 50,
                           timeout=10)
                print("  IBKR connected. Accounts:", ib.managedAccounts())
                ib.disconnect()
            except Exception as e:  # noqa: BLE001
                print(f"  ! IBKR not reachable ({e}). Start TWS/IB Gateway (paper), enable API, check port.")
        if prof.broker == "alpaca":
            from ahs.engine import make_alpaca
            try:
                a = make_alpaca(cfg, p).account()
                print(f"  Alpaca equity ${float(a['equity']):,.2f}; should be ~${prof.start_capital_nok / fx.rate('USD'):,.0f}")
            except Exception as e:  # noqa: BLE001
                print(f"  ! Alpaca: {e}")
    print("Daily plan:\n   " + "\n   ".join(schedule_table()))


def cmd_fetch(cfg, args):
    from ahs.sources import YahooSource
    u = load_universe(cfg.path(cfg["universe"]["file"]))
    store = DataStore(cfg.path(cfg["data"]["cache_dir"]) / "yahoo")
    missing = store.update(YahooSource(yahoo_scales(u)), list(u.index), cfg["data"]["history_start"])
    p = store.panels([s for s in u.index if s not in missing])
    print(f"Cached {len(p.symbols)} symbols, {p.dates[0].date()} -> {p.dates[-1].date()}")
    if missing:
        print("No data (check ticker or remove from universe.csv):", ", ".join(missing))


def _backtest(cfg, args, panels, fx_panel, out_name, mode, note):
    from ahs.backtest import run_backtest
    from ahs.dashboard import portfolio_from_backtest, write_dashboard
    u = load_universe(cfg.path(cfg["universe"]["file"]))
    pfs, results = [], []
    for p in pnames(cfg, args.portfolio):
        r = run_backtest(cfg, panels, u, p, fx_panel, args.years, research_days=10)
        results.append(r)
        pfs.append(portfolio_from_backtest(cfg, r, u, r.research))
        m = r.metrics
        print(f"     largest single buy {m['max_single_buy_pct']:.1%} of portfolio (rule: <= 25%), "
              f"costs NOK {m['costs_nok']:,.0f}, trades by market {m['by_market']}")
    f = write_dashboard(pfs, cfg.path("reports") / out_name, mode, note)
    print(f"Dashboard: {f}")
    return results


def cmd_backtest(cfg, args):
    from ahs.fx import FxRates
    u = load_universe(cfg.path(cfg["universe"]["file"]))
    store = DataStore(cfg.path(cfg["data"]["cache_dir"]) / "yahoo")
    panels = store.panels(list(u.index))
    fx = FxRates(cfg.path("state"), cfg["fx"].get("manual_rates"))
    fx.refresh()
    return _backtest(cfg, args, panels, fx.panel(panels.dates, u.currency), "backtest_dashboard.html", "backtest",
                     "BACKTEST on real historical prices. Flattering by nature (today's well-known names; no "
                     "fundamentals or reviewer). Use it to check behaviour, not as a forecast.")


def cmd_demo(cfg, args):
    from ahs.synthetic import make_fx, make_market
    u = load_universe(cfg.path(cfg["universe"]["file"]))
    panels = make_market(list(u.index))
    return _backtest(cfg, args, panels, make_fx(panels.dates, u.currency), "demo_dashboard.html", "demo",
                     "DEMO on SYNTHETIC random prices - shows how the dashboard works; says nothing about markets.")


def cmd_session(cfg, args):
    from ahs.engine import run_session
    for p in pnames(cfg, args.portfolio):
        run_session(cfg, p, args.session, dry_run=args.dry_run, force=args.force)
    cmd_dashboard(cfg, args)


def cmd_dashboard(cfg, args):
    import pandas as pd
    from ahs.dashboard import portfolio_from_ledger, write_dashboard
    from ahs.ledger import Ledger
    ledger = Ledger(cfg.path("state") / "ledger.sqlite")
    store = DataStore(cfg.path(cfg["data"]["cache_dir"]) / "yahoo")
    bench = {}
    for b in (cfg["data"]["global_benchmark"], cfg["data"]["benchmark"]):
        df = store.read(b)
        if df is not None:
            bench[b] = df.close
    bc = pd.DataFrame(bench) if bench else None
    pfs = [portfolio_from_ledger(cfg, ledger, p, bc) for p in cfg.portfolios]
    f = write_dashboard(pfs, cfg.path("reports") / "dashboard.html", "live")
    print(f"Dashboard: {f}")


def cmd_scheduler(cfg, args):
    from ahs.engine import run_session
    from ahs.scheduler import run_forever
    run_forever(cfg, lambda p, s: run_session(cfg, p, s, dry_run=args.dry_run), lambda: cmd_dashboard(cfg, args))


def cmd_due(cfg, args):
    import os
    from ahs.engine import run_session
    from ahs.ledger import Ledger
    from ahs.scheduler import run_due
    ledger = Ledger(cfg.path("state") / "ledger.sqlite")
    ran = run_due(cfg, ledger, lambda p, s: run_session(cfg, p, s, dry_run=args.dry_run))
    if ran or not (cfg.path("reports") / "dashboard.html").exists():
        cmd_dashboard(cfg, args)
        import shutil
        shutil.copy(cfg.path("reports") / "dashboard.html", cfg.path("reports") / "index.html")
    if os.getenv("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"ran={'true' if ran else 'false'}\n")


def cmd_diagnose(cfg, args):
    """Print data-alignment facts used to debug indicators (no trading)."""
    import numpy as np
    import pandas as pd
    from ahs.features import compute_features
    u = load_universe(cfg.path(cfg["universe"]["file"]))
    store = DataStore(cfg.path(cfg["data"]["cache_dir"]) / "yahoo")
    syms = [s for s in u.index if store.read(s) is not None]
    print(f"cached symbols: {len(syms)}")
    for s in ["SPY", "XOM", "KO", "AAPL", "EQNR.OL", "SAP.DE"]:
        df = store.read(s)
        if df is None:
            print(s, "no data"); continue
        idx = df.index
        print(f"{s:8} rows={len(df)} first={idx[0]} last={idx[-1]} dup={idx.duplicated().sum()} "
              f"weekend={(idx.dayofweek >= 5).sum()} times={sorted(set(idx.strftime('%H:%M')))[:3]} last5={list(idx[-5:].strftime('%a %m-%d'))}")
    p = store.panels(syms, before=pd.Timestamp.today().normalize())
    print("union dates:", len(p.dates), "weekend dates in union:", int((p.dates.dayofweek >= 5).sum()))
    f = compute_features(p, "SPY", 5)
    raw = {s: store.read(s).close for s in ["SPY", "XOM", "KO", "AAPL"] if store.read(s) is not None}
    j = pd.DataFrame(raw).dropna().iloc[-260:].pct_change().dropna()
    for s in ["XOM", "KO", "AAPL"]:
        if s in j:
            direct = j[s].cov(j["SPY"]) / j["SPY"].var()
            lag = j[s].cov(j["SPY"].shift(1)) / j["SPY"].var()
            print(f"{s}: beta (2y weekly, adjusted)={f['beta'][s].dropna().iloc[-1]:.2f}  daily 1y raw={direct:.2f}  vs SPY lagged 1 day={lag:.2f}")


def cmd_serve(cfg, args):
    import functools
    import http.server
    cmd_dashboard(cfg, args)
    d = str(cfg.path("reports"))
    h = functools.partial(http.server.SimpleHTTPRequestHandler, directory=d)
    print(f"Dashboard at http://localhost:{args.port}/dashboard.html  (Ctrl+C to stop)")
    http.server.ThreadingHTTPServer(("0.0.0.0", args.port), h).serve_forever()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["init", "fetch", "backtest", "session", "scheduler", "due", "dashboard", "serve", "demo",
                             "diagnose"])
    ap.add_argument("-p", "--portfolio", default="all", help="MAIN, HIGH_RISK or all")
    ap.add_argument("-s", "--session", default="nordic", choices=["nordic", "europe", "us"])
    ap.add_argument("--dry-run", action="store_true", help="decide and log, but place no orders")
    ap.add_argument("--force", action="store_true", help="run even if the market looks closed")
    ap.add_argument("--years", type=int, default=5)
    ap.add_argument("--port", type=int, default=8050)
    ap.add_argument("--config", default=None)
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    {"init": cmd_init, "fetch": cmd_fetch, "backtest": cmd_backtest, "session": cmd_session,
     "scheduler": cmd_scheduler, "due": cmd_due, "diagnose": cmd_diagnose, "dashboard": cmd_dashboard, "serve": cmd_serve, "demo": cmd_demo}[args.command](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
