"""Value agent: is the price attractive relative to its own 5-year history and quality?

Two layers:
 1. Price-based value & quality over the 5-year lookback (used in live AND backtest).
 2. Optional fundamentals (P/E, PEG, FCF yield, debt) in LIVE runs only, via
    `ahs.fundamentals`. They are never used in backtests because free sources only
    give today's numbers, which would leak the future into the past.
"""
from __future__ import annotations

import pandas as pd

from .base import AnalystAgent, clip, pct


class ValueAgent(AnalystAgent):
    name = "value"

    def __init__(self, fundamentals: dict | None = None):
        self.fund = fundamentals or {}

    def assess(self, s: pd.DataFrame) -> pd.DataFrame:
        cheap = clip(-s.z_5y / 2)                         # below own 5y average = cheaper
        quality = clip(s.sharpe_lb / 0.8)                 # steady long-run compounding
        dd = s.dd_5y
        on_sale = ((dd < -0.15) & (dd > -0.45) & (s.cagr_lb > 0.05)).astype(float) * 0.5
        broken = (dd < -0.60).astype(float) * 0.6
        stance = clip(0.40 * cheap + 0.40 * quality + on_sale - broken)
        fadj = pd.Series({k: self._fund_score(v) for k, v in self.fund.items()}, dtype=float)
        if not fadj.empty:
            stance = clip(stance.add(fadj.reindex(stance.index).fillna(0) * 0.35))
        conf = (0.35 + 0.35 * (s.history_days.clip(upper=1260) / 1260) + 0.3 * fadj.reindex(s.index).notna())
        return pd.DataFrame({"stance": stance, "confidence": conf.where(stance.notna())})

    @staticmethod
    def _fund_score(f: dict) -> float:
        sc, n = 0.0, 0
        pe, peg, fcf, de = f.get("forward_pe"), f.get("peg"), f.get("fcf_yield"), f.get("debt_to_equity")
        if pe and pe > 0:
            sc += 1 if pe < 15 else 0.3 if pe < 25 else -0.5 if pe < 45 else -1; n += 1
        if peg and peg > 0:
            sc += 1 if peg < 1 else 0 if peg < 2 else -1; n += 1
        if fcf is not None:
            sc += 1 if fcf > 0.05 else 0.3 if fcf > 0.02 else -0.5 if fcf > 0 else -1; n += 1
        if de is not None:
            sc += 0.5 if de < 50 else 0 if de < 150 else -0.7; n += 1
        return sc / n if n else 0.0

    def explain(self, sym, r):
        pros, cons = [], []
        if pd.notna(r.z_5y):
            if r.z_5y < -0.5:
                pros.append(f"Priced below its 5-year average (z = {r.z_5y:.1f})")
            elif r.z_5y > 1.5:
                cons.append(f"Stretched vs its 5-year average (z = {r.z_5y:.1f})")
        if pd.notna(r.cagr_lb):
            (pros if r.cagr_lb > 0.08 else cons).append(f"Long-run annual growth {pct(r.cagr_lb)}")
        if pd.notna(r.sharpe_lb):
            if r.sharpe_lb > 0.7:
                pros.append(f"High-quality compounding (5y Sharpe {r.sharpe_lb:.2f})")
            elif r.sharpe_lb < 0.2:
                cons.append(f"Poor risk-adjusted history (5y Sharpe {r.sharpe_lb:.2f})")
        if pd.notna(r.dd_5y):
            if -0.45 < r.dd_5y < -0.15 and r.cagr_lb > 0.05:
                pros.append(f"Quality name on sale: {pct(r.dd_5y)} from 5y high")
            elif r.dd_5y < -0.60:
                cons.append(f"Possibly broken story: {pct(r.dd_5y)} from 5y high")
        f = self.fund.get(sym)
        if f:
            if f.get("forward_pe"):
                (pros if f["forward_pe"] < 18 else cons).append(f"Forward P/E {f['forward_pe']:.1f}")
            if f.get("fcf_yield") is not None:
                (pros if f["fcf_yield"] > 0.03 else cons).append(f"Free-cash-flow yield {pct(f['fcf_yield'])}")
        return pros, cons
