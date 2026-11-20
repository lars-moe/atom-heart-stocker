"""Indicators. All are causal (only use data up to and including the row's date),
so the same panel serves both the 5-year backtest and live trading."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .data import Panels

Y = 252  # trading days per year


def rsi(close: pd.DataFrame, n: int = 14) -> pd.DataFrame:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def compute_features(p: Panels, benchmark: str = "SPY", lookback_years: int = 5) -> dict[str, pd.DataFrame]:
    c, h, l, v = p.close, p.high.reindex_like(p.close), p.low.reindex_like(p.close), p.volume.reindex_like(p.close)
    lb = lookback_years * Y
    ret = c.pct_change(fill_method=None)
    logp = np.log(c)
    f: dict[str, pd.DataFrame] = {"close": c}

    # --- trend / momentum (Opportunity agent)
    f["sma50"] = c.rolling(50, min_periods=40).mean()
    f["sma200"] = c.rolling(200, min_periods=160).mean()
    f["mom_12_1"] = c.shift(21) / c.shift(Y) - 1
    f["mom_3m"] = c / c.shift(63) - 1
    f["prox_52w_high"] = c / c.rolling(Y, min_periods=200).max()
    f["breakout_55"] = (c >= c.rolling(55, min_periods=50).max()).astype(float)
    f["vol_surge"] = v.rolling(5).mean() / v.rolling(50, min_periods=40).mean()
    f["rsi14"] = rsi(c)

    # --- risk
    prev_c = c.shift(1)
    tr = np.maximum(h - l, np.maximum((h - prev_c).abs(), (l - prev_c).abs()))
    f["atr14"] = tr.rolling(14, min_periods=10).mean()
    f["vol_60"] = ret.rolling(60, min_periods=50).std() * np.sqrt(Y)
    f["vol_1y"] = ret.rolling(Y, min_periods=200).std() * np.sqrt(Y)
    dd = c / c.rolling(Y, min_periods=200).max() - 1
    f["maxdd_1y"] = dd.rolling(Y, min_periods=200).min()
    if benchmark in c.columns:
        b = ret[benchmark]
        cov = ret.rolling(Y, min_periods=200).cov(b)
        f["beta_1y"] = cov.div(b.rolling(Y, min_periods=200).var(), axis=0)
    else:
        f["beta_1y"] = pd.DataFrame(np.nan, index=c.index, columns=c.columns)
    f["adv20"] = (c * v).rolling(20, min_periods=15).mean()

    # --- long-horizon value / quality (5-year lookback, Value agent)
    f["dd_5y"] = c / c.rolling(lb, min_periods=2 * Y).max() - 1
    mu = logp.rolling(lb, min_periods=2 * Y).mean()
    sd = logp.rolling(lb, min_periods=2 * Y).std()
    f["z_5y"] = (logp - mu) / sd
    cagr5 = (c / c.shift(lb)) ** (1 / 5) - 1
    cagr3 = (c / c.shift(3 * Y)) ** (1 / 3) - 1
    f["cagr_lb"] = cagr5.fillna(cagr3)
    f["sharpe_lb"] = (ret.rolling(lb, min_periods=2 * Y).mean() * Y) / (ret.rolling(lb, min_periods=2 * Y).std() * np.sqrt(Y))
    f["history_days"] = c.notna().cumsum()
    return f


def snapshot(feats: dict[str, pd.DataFrame], t) -> pd.DataFrame:
    """Cross-section at date t: rows = symbols, columns = features."""
    return pd.DataFrame({k: df.loc[t] for k, df in feats.items()})
