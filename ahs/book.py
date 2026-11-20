"""The simulated brokerage book: NOK cash plus positions in any currency.

Used by the live simulator AND the backtest, so both follow identical rules:
  * cost model: slippage + commission (bps of trade value) + fees on sales
    + currency conversion spread on foreign trades
  * currency rule: positions are valued at local price x entry FX; currency
    gains or losses are realised only when sale proceeds are converted to NOK
  * cash only (no margin), whole shares, long only
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field


@dataclass
class Position:
    symbol: str
    qty: int
    currency: str
    entry_price: float      # local currency, average incl. slippage
    entry_fx: float         # NOK per 1 unit of currency, effective incl. conversion cost
    entry_date: str
    high_water: float       # highest close since entry (local), for trailing stops
    market: str = ""

    def value(self, price: float) -> float:
        return self.qty * price * self.entry_fx


@dataclass
class SimBook:
    cash: float
    start_capital: float
    positions: dict = field(default_factory=dict)
    realized: list = field(default_factory=list)
    fills: list = field(default_factory=list)
    costs: dict = field(default_factory=lambda: {"slippage": 0.0, "commission": 0.0, "fx": 0.0, "fees": 0.0})
    cost_cfg: dict = field(default_factory=dict)

    # ---------- valuation ----------
    def equity(self, prices: dict[str, float]) -> float:
        return self.cash + sum(p.value(prices.get(s, p.entry_price)) for s, p in self.positions.items())

    def holdings_value(self, prices: dict[str, float]) -> dict[str, float]:
        return {s: p.value(prices.get(s, p.entry_price)) for s, p in self.positions.items()}

    def unrealised_fx_effect(self, prices: dict[str, float], fx_now: dict[str, float]) -> float:
        """Information only (not counted, per the currency rule)."""
        return sum(p.qty * prices.get(s, p.entry_price) * (fx_now.get(p.currency, p.entry_fx) - p.entry_fx)
                   for s, p in self.positions.items())

    # ---------- trading ----------
    def _bps(self, k):
        return float(self.cost_cfg.get(k, 0)) / 1e4

    def buy(self, symbol: str, qty: int, price: float, currency: str, fx: float, date: str,
            market: str = "", limit: float | None = None) -> dict | None:
        if qty < 1 or (limit is not None and price > limit):
            return None
        fill = price * (1 + self._bps("slippage_bps"))
        fx_eff = fx * (1 + (self._bps("fx_conversion_bps") if currency != "NOK" else 0))
        unit = fill * fx_eff * (1 + self._bps("commission_bps"))
        qty = min(qty, math.floor(self.cash / unit))           # never borrow
        if qty < 1:
            return None
        gross_nok = qty * fill * fx_eff
        commission = gross_nok * self._bps("commission_bps")
        self.cash -= gross_nok + commission
        self.costs["slippage"] += qty * (fill - price) * fx
        self.costs["fx"] += qty * fill * (fx_eff - fx)
        self.costs["commission"] += commission
        p = self.positions.get(symbol)
        if p:
            tot_local = p.qty * p.entry_price + qty * fill
            p.entry_fx = (p.qty * p.entry_price * p.entry_fx + qty * fill * fx_eff) / tot_local
            p.entry_price = tot_local / (p.qty + qty)
            p.qty += qty
        else:
            self.positions[symbol] = Position(symbol, qty, currency, fill, fx_eff, date, price, market)
        rec = {"date": date, "symbol": symbol, "side": "buy", "qty": qty, "price": fill, "currency": currency,
               "fx": fx_eff, "value_nok": gross_nok + commission}
        self.fills.append(rec)
        return rec

    def sell(self, symbol: str, qty: int, price: float, fx: float, date: str, reason: str = "") -> dict | None:
        p = self.positions.get(symbol)
        if not p or qty < 1:
            return None
        qty = min(qty, p.qty)
        fill = price * (1 - self._bps("slippage_bps"))
        local = qty * fill
        fees_local = local * self._bps("sell_regulatory_bps")
        fx_eff = fx * (1 - (self._bps("fx_conversion_bps") if p.currency != "NOK" else 0))
        proceeds_nok = (local - fees_local) * fx_eff
        commission = proceeds_nok * self._bps("commission_bps")
        self.cash += proceeds_nok - commission
        self.costs["slippage"] += qty * (price - fill) * fx
        self.costs["fees"] += fees_local * fx
        self.costs["fx"] += (local - fees_local) * (fx - fx_eff)
        self.costs["commission"] += commission
        cost_nok = qty * p.entry_price * p.entry_fx
        price_pnl = (local - fees_local) * p.entry_fx - cost_nok - commission     # at entry FX
        fx_pnl = (local - fees_local) * (fx_eff - p.entry_fx)                      # realised now
        rec = {"date": date, "symbol": symbol, "side": "sell", "qty": qty, "price": fill, "currency": p.currency,
               "fx": fx_eff, "value_nok": proceeds_nok - commission, "price_pnl_nok": price_pnl,
               "fx_pnl_nok": fx_pnl, "return_pct": (fill / p.entry_price - 1), "held_since": p.entry_date,
               "reason": reason}
        self.fills.append(rec)
        self.realized.append(rec)
        p.qty -= qty
        if p.qty <= 0:
            del self.positions[symbol]
        return rec

    def mark(self, closes: dict[str, float]) -> None:
        for s, p in self.positions.items():
            c = closes.get(s)
            if c is not None and c == c:
                p.high_water = max(p.high_water, c)

    # ---------- persistence ----------
    def dumps(self) -> str:
        d = asdict(self)
        d["positions"] = {k: asdict(v) for k, v in self.positions.items()}
        d["fills"] = self.fills[-2000:]
        return json.dumps(d)

    @classmethod
    def loads(cls, blob: str, cost_cfg: dict) -> "SimBook":
        d = json.loads(blob)
        d["positions"] = {k: Position(**v) for k, v in d["positions"].items()}
        d["cost_cfg"] = cost_cfg
        return cls(**d)
