"""Synthetic market for offline tests and the `demo` command. NOT real data."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .data import Panels


def make_market(symbols: list[str], years: int = 10, seed: int = 7) -> Panels:
    rng = np.random.default_rng(seed)
    n = years * 252
    idx = pd.bdate_range(end="2026-11-19", periods=n)
    market = rng.normal(0.0003, 0.010, n)
    # regime shifts so trend-following has something to find
    regime = np.repeat(rng.choice([-1, 1], size=n // 120 + 1, p=[0.35, 0.65]), 120)[:n]
    closes, highs, lows, opens, vols = {}, {}, {}, {}, {}
    for s in symbols:
        beta = rng.uniform(0.5, 1.6)
        drift = rng.normal(0.0002, 0.0004)
        idio = rng.uniform(0.008, 0.03)
        trend = np.cumsum(rng.normal(0, 0.00015, n))          # slowly changing personal trend
        r = beta * market + drift + trend * 0.05 + regime * 0.0004 * beta + rng.normal(0, idio, n)
        c = 50 * np.exp(np.cumsum(r))
        o = c * np.exp(rng.normal(0, idio / 3, n))
        closes[s] = c
        opens[s] = o
        highs[s] = np.maximum(c, o) * (1 + np.abs(rng.normal(0, idio / 2, n)))
        lows[s] = np.minimum(c, o) * (1 - np.abs(rng.normal(0, idio / 2, n)))
        vols[s] = rng.lognormal(14, 0.4, n)
    f = lambda d: pd.DataFrame(d, index=idx)
    return Panels(f(opens), f(highs), f(lows), f(closes), f(vols))


def make_fx(index: pd.DatetimeIndex, ccys, seed: int = 11) -> pd.DataFrame:
    """Synthetic NOK-per-unit exchange rates (random walks around realistic levels)."""
    rng = np.random.default_rng(seed)
    levels = {"NOK": 1.0, "USD": 10.0, "EUR": 11.5, "SEK": 0.98, "DKK": 1.54, "GBP": 13.2, "CHF": 12.3}
    out = {}
    for c in set(ccys):
        if c == "NOK":
            out[c] = np.ones(len(index))
        else:
            out[c] = levels.get(c, 1.0) * np.exp(np.cumsum(rng.normal(0, 0.004, len(index))))
    return pd.DataFrame(out, index=index)
