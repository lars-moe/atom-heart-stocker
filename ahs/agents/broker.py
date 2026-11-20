"""Broker agent: the only component that places orders, and the guardian of the
hard rules. Whatever the other agents propose, an order that breaks a rule is
shrunk until it complies, or refused.

USER RULE: one stock buy can never exceed 25 % of the total portfolio value
(available cash + holdings), measured in NOK, including all trading costs.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class TradeIntent:
    symbol: str
    side: str              # "buy" | "sell"
    qty: int
    ref_price: float       # local currency
    kind: str              # entry | exit | trim
    reason: str
    score: float = 0.0
    case: dict = field(default_factory=dict)   # the debate record: per-agent pros/cons
    currency: str = "NOK"
    fx: float = 1.0        # NOK per 1 unit of currency at decision time
    market: str = ""


@dataclass
class OrderRecord:
    symbol: str
    side: str
    qty: int
    limit_price: float
    notional: float        # NOK, worst case incl. costs
    status: str
    kind: str
    reason: str
    broker_notes: list[str] = field(default_factory=list)
    order_id: str = ""
    currency: str = "NOK"
    market: str = ""


class BrokerAgent:
    name = "broker"

    def __init__(self, hard_rules: dict, costs: dict, limit_buffer_bps: float):
        self.max_buy = float(hard_rules["max_single_buy_pct_of_equity"])
        self.max_pos = float(hard_rules["max_position_pct_of_equity"])
        self.allow_margin = bool(hard_rules.get("allow_margin", False))
        self.no_round_trip = bool(hard_rules.get("avoid_same_day_round_trip", True))
        self.costs = costs
        self.buf = limit_buffer_bps / 1e4

    def limit_price(self, side: str, price: float) -> float:
        p = price * (1 + self.buf) if side == "buy" else price * (1 - self.buf)
        return round(p, 4 if p < 10 else 2)

    def unit_cost_nok(self, it: TradeIntent) -> float:
        """Worst-case NOK cost of one share at the limit price, all costs included."""
        c = self.costs
        fx_cost = c.get("fx_conversion_bps", 0) / 1e4 if it.currency != "NOK" else 0
        return (self.limit_price("buy", it.ref_price) * (1 + c.get("slippage_bps", 0) / 1e4)
                * it.fx * (1 + fx_cost) * (1 + c.get("commission_bps", 0) / 1e4))

    def check_buy(self, it: TradeIntent, equity: float, cash: float, position_value: float) -> tuple[int, list[str]]:
        notes, qty, unit = [], int(it.qty), self.unit_cost_nok(it)
        caps = {f"single buy <= {self.max_buy:.0%} of portfolio": self.max_buy * equity,
                f"position <= {self.max_pos:.0%} of portfolio": self.max_pos * equity - position_value}
        if not self.allow_margin:
            caps["no margin: cash only"] = cash
        for label, cap in caps.items():
            allowed = max(0, math.floor(cap / unit)) if unit > 0 else 0
            if allowed < qty:
                notes.append(f"shrunk {qty}->{allowed} by rule '{label}'")
                qty = allowed
        return qty, notes

    def execute(self, intents: list[TradeIntent], gateway, equity: float, cash: float,
                position_values: dict[str, float], bought_today: set[str], tag: str) -> list[OrderRecord]:
        records: list[OrderRecord] = []
        for it in sorted(intents, key=lambda x: x.side != "sell"):   # sells first
            notes: list[str] = []
            if it.side == "sell":
                if self.no_round_trip and it.symbol in bought_today:
                    records.append(OrderRecord(it.symbol, "sell", it.qty, 0, 0, "refused", it.kind, it.reason,
                                               ["bought today: same-day round trip blocked"], "", it.currency, it.market))
                    continue
                qty, unit = it.qty, it.ref_price * it.fx
            else:
                qty, notes = self.check_buy(it, equity, cash, position_values.get(it.symbol, 0.0))
                unit = self.unit_cost_nok(it)
                if qty < 1:
                    records.append(OrderRecord(it.symbol, "buy", 0, 0, 0, "refused", it.kind, it.reason,
                                               notes or ["quantity below one share"], "", it.currency, it.market))
                    continue
            lp = self.limit_price(it.side, it.ref_price)
            res = gateway.submit(it.symbol, qty, it.side, lp, f"ahs-{tag}-{it.symbol}-{it.side}", it.reason)
            rec = OrderRecord(it.symbol, it.side, qty, lp, qty * unit, res.get("status", "submitted"), it.kind,
                              it.reason, notes, str(res.get("id", "")), it.currency, it.market)
            records.append(rec)
            if it.side == "buy" and rec.status not in ("rejected", "error", "refused"):
                cash -= rec.notional
                position_values[it.symbol] = position_values.get(it.symbol, 0.0) + rec.notional
        return records
