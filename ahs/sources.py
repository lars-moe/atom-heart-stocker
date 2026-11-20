"""Price sources behind one interface.

* YahooSource: all markets (Oslo, Nordics, Europe, US), split/dividend-adjusted
  daily history and ~15-minute-delayed intraday prices, free, via `yfinance`.
  It is unofficial and occasionally breaks. Failures are logged and the symbol
  is skipped for that run.
* AlpacaSource: US only, for portfolios that trade through Alpaca.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pandas as pd

FIELDS = ["open", "high", "low", "close", "volume"]


def _frame_for(df: pd.DataFrame, sym: str) -> pd.DataFrame | None:
    if isinstance(df.columns, pd.MultiIndex):
        lvl0 = df.columns.get_level_values(0)
        if sym in lvl0:
            sub = df[sym]
        elif sym in df.columns.get_level_values(1):
            sub = df.xs(sym, axis=1, level=1)
        else:
            return None
    else:
        sub = df
    sub = sub.rename(columns=str.lower)
    if "close" not in sub:
        return None
    sub = sub[[c for c in FIELDS if c in sub]].dropna(subset=["close"])
    return sub if len(sub) else None


class YahooSource:
    name = "yahoo"

    def __init__(self, scale: dict[str, float] | None = None):
        self.scale = scale or {}

    def _yf(self):
        try:
            import yfinance as yf  # type: ignore
        except ImportError as e:
            raise RuntimeError("pip install yfinance  (needed for Oslo/European prices)") from e
        return yf

    def _download(self, chunk, **kw):
        """yf.download with polite pacing and back-off (Yahoo rate-limits busy cloud IPs)."""
        yf = self._yf()
        df = None
        for attempt in range(4):
            df = yf.download(chunk, auto_adjust=True, actions=False, group_by="ticker",
                             progress=False, threads=False, **kw)
            if df is not None and not df.empty and df.notna().any().any():
                break
            time.sleep(15 * (attempt + 1))
        time.sleep(2)
        return df if df is not None else pd.DataFrame()

    def daily(self, symbols: list[str], start: str) -> dict[str, pd.DataFrame]:
        out = {}
        for i in range(0, len(symbols), 40):
            chunk = symbols[i:i + 40]
            df = self._download(chunk, start=start, interval="1d")
            for s in chunk:
                f = _frame_for(df, s)
                if f is None:
                    continue
                f.index = pd.to_datetime(f.index).tz_localize(None).normalize()
                k = self.scale.get(s, 1.0)
                f[["open", "high", "low", "close"]] *= k
                out[s] = f
        return out

    def latest(self, symbols: list[str]) -> dict[str, tuple[float, pd.Timestamp]]:
        """Last traded price (delayed) and its timestamp, from 5-minute bars."""
        out = {}
        for i in range(0, len(symbols), 40):
            chunk = symbols[i:i + 40]
            df = self._download(chunk, period="2d", interval="5m")
            for s in chunk:
                f = _frame_for(df, s)
                if f is None:
                    continue
                ts = pd.Timestamp(f.index[-1])
                out[s] = (float(f["close"].iloc[-1]) * self.scale.get(s, 1.0), ts)
        return out


class AlpacaSource:
    name = "alpaca"

    def __init__(self, client, symbol_map: dict[str, str]):
        self.c = client
        self.map = symbol_map                       # our symbol -> Alpaca symbol
        self.rev = {v: k for k, v in symbol_map.items()}

    def daily(self, symbols: list[str], start: str) -> dict[str, pd.DataFrame]:
        end = (datetime.now(timezone.utc) - timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
        syms = [self.map[s] for s in symbols if s in self.map]
        bars = self.c.daily_bars(syms, start=start, end=end)
        out = {}
        for a, rows in bars.items():
            if not rows:
                continue
            f = pd.DataFrame(rows)
            f["date"] = (pd.to_datetime(f["t"]).dt.tz_convert("America/New_York")
                         .dt.normalize().dt.tz_localize(None))
            f = f.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
            out[self.rev.get(a, a)] = f.set_index("date")[FIELDS]
        return out

    def latest(self, symbols: list[str]) -> dict[str, tuple[float, pd.Timestamp]]:
        syms = [self.map[s] for s in symbols if s in self.map]
        now = pd.Timestamp.now(tz="UTC")
        return {self.rev.get(a, a): (p, now) for a, p in self.c.latest_trades(syms).items()}
