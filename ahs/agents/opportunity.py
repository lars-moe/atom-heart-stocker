"""Opportunity Screen agent: finds trends, momentum and breakouts."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .base import AnalystAgent, clip, pct, xrank


class OpportunityScreenAgent(AnalystAgent):
    name = "opportunity"

    def assess(self, s: pd.DataFrame) -> pd.DataFrame:
        c, s50, s200 = s.close, s.sma50, s.sma200
        trend = np.select(
            [(c > s50) & (s50 > s200), c > s200, (c < s50) & (s50 < s200)],
            [1.0, 0.4, -1.0], default=-0.4)
        trend = pd.Series(trend, index=s.index).where(s200.notna())
        mom = xrank(s.mom_12_1)
        m3 = xrank(s.mom_3m)
        prox = clip((s.prox_52w_high - 0.80) / 0.20 * 2 - 1)
        breakout = ((s.breakout_55 > 0) & (s.vol_surge > 1.3)).astype(float) * 0.25
        overheat = (s.rsi14 > 80).astype(float) * 0.20
        comps = pd.DataFrame({"trend": trend, "mom": mom, "m3": m3, "prox": prox})
        stance = clip(0.35 * comps.trend + 0.30 * comps.mom + 0.15 * comps.m3 + 0.20 * comps.prox
                      + breakout - overheat)
        agree = np.sign(comps).sum(axis=1).abs() / 4          # components pointing the same way
        conf = (0.4 + 0.6 * agree).where(stance.notna())
        return pd.DataFrame({"stance": stance, "confidence": conf})

    def explain(self, sym, r):
        pros, cons = [], []
        if r.close > r.sma50 > r.sma200:
            pros.append("Clear uptrend: price > 50-day > 200-day average")
        elif r.close < r.sma200:
            cons.append(f"Below 200-day average ({pct(r.close / r.sma200 - 1)})")
        if pd.notna(r.mom_12_1):
            (pros if r.mom_12_1 > 0.10 else cons if r.mom_12_1 < 0 else pros).append(
                f"12-1 month momentum {pct(r.mom_12_1)}")
        if pd.notna(r.mom_3m):
            (pros if r.mom_3m > 0 else cons).append(f"3-month return {pct(r.mom_3m)}")
        if r.prox_52w_high > 0.95:
            pros.append("Trading within 5 % of its 52-week high")
        elif r.prox_52w_high < 0.75:
            cons.append(f"{pct(1 - r.prox_52w_high)} below its 52-week high")
        if r.breakout_55 > 0 and r.vol_surge > 1.3:
            pros.append(f"55-day breakout on {r.vol_surge:.1f}x normal volume")
        if r.rsi14 > 80:
            cons.append(f"Overheated short term (RSI {r.rsi14:.0f})")
        return pros, cons
