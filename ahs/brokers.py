"""Broker backends behind one interface. All money is reported in NOK under the
project's currency rule (positions at entry FX).

  sim     built-in simulator on real prices. All markets, works today, default.
  ibkr    Interactive Brokers paper account via TWS / IB Gateway (pip install ib_async).
  alpaca  Alpaca paper account. US-listed symbols only.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass

from .book import SimBook


@dataclass
class Holding:
    symbol: str
    qty: int
    entry_price: float       # local currency
    entry_date: str
    high_water: float        # local currency
    currency: str = "NOK"
    entry_fx: float = 1.0
    value_base: float = 0.0  # NOK, at entry FX
    market: str = ""


@dataclass
class AccountView:
    equity: float
    cash: float
    holdings: dict


class SimGateway:
    """Live simulator: an order fills immediately at the current (delayed) market price."""
    name = "sim"

    def __init__(self, book: SimBook, prices: dict, fx_now: dict, universe, today: str, fx_current: bool = False):
        self.book, self.prices, self.fx, self.u, self.today = book, prices, fx_now, universe, today
        self.mark_fx = fx_now if fx_current else None     # value holdings at today's FX if configured

    def account(self) -> AccountView:
        hv = self.book.holdings_value(self.prices, self.mark_fx)
        holds = {s: Holding(s, p.qty, p.entry_price, p.entry_date, p.high_water, p.currency, p.entry_fx, hv[s], p.market)
                 for s, p in self.book.positions.items()}
        return AccountView(self.book.equity(self.prices, self.mark_fx), self.book.cash, holds)

    def submit(self, symbol, qty, side, limit, client_order_id, reason=""):
        px = self.prices.get(symbol)
        if px is None:
            return {"status": "rejected", "id": "no live price"}
        ccy = self.u.at[symbol, "currency"]
        if side == "buy":
            f = self.book.buy(symbol, qty, px, ccy, self.fx[ccy], self.today, self.u.at[symbol, "market"], limit)
            return {"status": "filled", "id": f"sim-{len(self.book.fills)}", "fill": f} if f else \
                {"status": "rejected", "id": "limit not reached or no cash"}
        f = self.book.sell(symbol, qty, px, self.fx[ccy], self.today, reason)
        return {"status": "filled", "id": f"sim-{len(self.book.fills)}", "fill": f} if f else \
            {"status": "rejected", "id": "no position"}

    def wait_for_sells(self):
        pass


class BacktestGateway:
    """Queues orders; the backtest fills them at the next day's open."""
    name = "backtest"

    def __init__(self):
        self.queue = []

    def submit(self, symbol, qty, side, limit, client_order_id, reason=""):
        self.queue.append({"symbol": symbol, "qty": qty, "side": side, "limit": limit, "reason": reason})
        return {"status": "queued"}


# --------------------------------------------------------------------------- IBKR
class IBKRGateway:
    """Interactive Brokers paper account through TWS or IB Gateway running on this computer.
    Paper ports: TWS 7497, IB Gateway 4002. Untested against a live gateway from here:
    always start with `--dry-run` and watch the first sessions."""
    name = "ibkr"

    def __init__(self, cfg: dict, universe, prices: dict, fx_now: dict, ledger, portfolio: str,
                 today: str, markets: dict, dry_run=False, fx_current: bool = False):
        from ib_async import IB  # type: ignore  (pip install ib_async)
        self.ib = IB()
        self.ib.connect(cfg.get("host", "127.0.0.1"), int(cfg.get("port", 7497)),
                        clientId=int(cfg.get("client_id", 17)), timeout=20)
        acct = cfg.get("account") or ""
        self.acct = acct
        self.u, self.prices, self.fx, self.ledger, self.pf = universe, prices, fx_now, ledger, portfolio
        self.today, self.markets, self.dry = today, markets, dry_run
        self.fx_current = fx_current
        self.meta = json.loads(ledger.get(portfolio, "ibkr_entry") or "{}")
        self._contracts = {}

    def _isym(self, sym):
        row = self.u.loc[sym]
        if row.get("ibkr_symbol"):
            return row["ibkr_symbol"]
        base = sym if row.market == "US" else sym.split(".")[0]
        return base.replace("-", " ") if row.market == "US" else base.replace("-", ".")

    def _contract(self, sym):
        if sym in self._contracts:
            return self._contracts[sym]
        from ib_async import Stock  # type: ignore
        row = self.u.loc[sym]
        m = self.markets[row.market]
        c = Stock(self._isym(sym), "SMART", row.currency, primaryExchange=m.ibkr_primary or "")
        q = self.ib.qualifyContracts(c)
        self._contracts[sym] = q[0] if q else None
        return self._contracts[sym]

    def _convert(self, ccy: str, amount_ccy: float, to_foreign: bool):
        """Exchange NOK <-> foreign currency with a market FX order (cash only, no FX loans)."""
        if ccy == "NOK" or amount_ccy <= 0 or self.dry:
            return
        from ib_async import Forex, MarketOrder  # type: ignore
        for pair, foreign_is_base in ((ccy + "NOK", True), ("NOK" + ccy, False)):
            q = self.ib.qualifyContracts(Forex(pair))
            if not q:
                continue
            if foreign_is_base:
                order = MarketOrder("BUY" if to_foreign else "SELL", round(amount_ccy))
            else:
                nok = amount_ccy * self.fx[ccy]
                order = MarketOrder("SELL" if to_foreign else "BUY", round(nok))
            self.ib.placeOrder(q[0], order)
            self.ib.sleep(3)
            return

    def account(self) -> AccountView:
        cash = 0.0
        for v in self.ib.accountValues(self.acct):
            if v.tag == "CashBalance" and v.currency not in ("BASE", ""):
                cash += float(v.value) * self.fx.get(v.currency, 1.0)
        holds = {}
        lookup = {(self._isym(s), self.u.at[s, "currency"]): s for s in self.u.index}
        for p in self.ib.positions(self.acct):
            c = p.contract
            sym = lookup.get((c.symbol, c.currency), c.symbol)
            m = self.meta.get(sym, {})
            ccy = c.currency
            entry_fx = m.get("entry_fx", self.fx.get(ccy, 1.0))
            px = self.prices.get(sym, float(p.avgCost))
            hw = max(m.get("high_water", px), px)
            mark = self.fx.get(ccy, entry_fx) if self.fx_current else entry_fx
            holds[sym] = Holding(sym, int(p.position), float(p.avgCost), m.get("entry_date", self.today), hw,
                                 ccy, entry_fx, int(p.position) * px * mark,
                                 self.u.at[sym, "market"] if sym in self.u.index else "")
            self.meta[sym] = {"entry_fx": entry_fx, "entry_date": m.get("entry_date", self.today), "high_water": hw}
        self.ledger.put(self.pf, "ibkr_entry", json.dumps(self.meta))
        return AccountView(cash + sum(h.value_base for h in holds.values()), cash, holds)

    def submit(self, symbol, qty, side, limit, client_order_id, reason=""):
        from ib_async import LimitOrder, MarketOrder  # type: ignore
        c = self._contract(symbol)
        if c is None:
            return {"status": "rejected", "id": "contract not found at IBKR"}
        if self.dry:
            return {"status": "dry-run"}
        ccy = self.u.at[symbol, "currency"]
        if side == "buy":
            self._convert(ccy, qty * limit * 1.002, to_foreign=True)
            order = LimitOrder("BUY", qty, round(limit, 2), orderRef=client_order_id[:40], tif="DAY")
        else:
            order = MarketOrder("SELL", qty, orderRef=client_order_id[:40], tif="DAY")
        tr = self.ib.placeOrder(c, order)
        self.ib.sleep(5)
        st = tr.orderStatus.status
        if side == "buy":
            self.meta[symbol] = {"entry_fx": self.fx.get(ccy, 1.0), "entry_date": self.today,
                                 "high_water": self.prices.get(symbol, limit)}
        elif st == "Filled":
            self._convert(ccy, qty * (tr.orderStatus.avgFillPrice or self.prices.get(symbol, 0)), to_foreign=False)
            self.meta.pop(symbol, None)
        self.ledger.put(self.pf, "ibkr_entry", json.dumps(self.meta))
        return {"status": st.lower(), "id": str(tr.order.orderId)}

    def wait_for_sells(self):
        for _ in range(18):
            if not any(t.order.action == "SELL" and not t.isDone() for t in self.ib.openTrades()):
                return
            self.ib.sleep(10)


# --------------------------------------------------------------------------- Alpaca
class AlpacaGateway:
    """Alpaca paper account (USD). NOK values use the start rate locked at setup."""
    name = "alpaca"

    def __init__(self, client, universe, usdnok_locked: float, ledger, portfolio, today, dry_run=False):
        self.c, self.u, self.r, self.ledger, self.pf, self.today, self.dry = \
            client, universe, usdnok_locked, ledger, portfolio, today, dry_run
        self.amap = {s: (a or s) for s, a in universe.alpaca_symbol.items() if universe.at[s, "market"] == "US"}
        self.rev = {v: k for k, v in self.amap.items()}

    def account(self) -> AccountView:
        a = self.c.account()
        cash = min(float(a["cash"]), float(a.get("non_marginable_buying_power") or a["cash"]), float(a["equity"]))
        meta = json.loads(self.ledger.get(self.pf, "alpaca_entry") or "{}")
        holds = {}
        for p in self.c.positions():
            sym = self.rev.get(p["symbol"], p["symbol"])
            m = meta.get(sym, {})
            px = float(p["current_price"])
            hw = max(m.get("high_water", px), px)
            holds[sym] = Holding(sym, int(float(p["qty"])), float(p["avg_entry_price"]), m.get("entry_date", self.today),
                                 hw, "USD", self.r, float(p["market_value"]) * self.r, "US")
            meta[sym] = {"entry_date": m.get("entry_date", self.today), "high_water": hw}
        self.ledger.put(self.pf, "alpaca_entry", json.dumps({k: v for k, v in meta.items() if k in holds}))
        return AccountView(float(a["equity"]) * self.r, cash * self.r, holds)

    def submit(self, symbol, qty, side, limit, client_order_id, reason=""):
        if symbol not in self.amap:
            return {"status": "rejected", "id": "not tradable on Alpaca (US only)"}
        if self.dry:
            return {"status": "dry-run"}
        try:
            o = self.c.submit_order(self.amap[symbol], qty, side, limit if side == "buy" else None, client_order_id)
            return {"status": o.get("status", "submitted"), "id": o.get("id", "")}
        except Exception as e:  # noqa: BLE001
            return {"status": "error", "id": str(e)[:200]}

    def wait_for_sells(self):
        if self.dry or not self.c.clock().get("is_open"):
            return
        for _ in range(18):
            if not any(o["side"] == "sell" for o in self.c.open_orders()):
                return
            time.sleep(10)
