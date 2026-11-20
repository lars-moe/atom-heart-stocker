"""Historical market data: download via a price source, cache to CSV, expose wide panels."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from .markets import MARKETS

FIELDS = ("open", "high", "low", "close", "volume")


def load_universe(path: Path) -> pd.DataFrame:
    u = pd.read_csv(path, dtype=str, keep_default_na=False)
    u["symbol"] = u["symbol"].str.strip()
    u = u.set_index("symbol")
    unknown = set(u.market) - set(MARKETS)
    if unknown:
        raise ValueError(f"universe.csv: unknown market codes {unknown}")
    return u


def yahoo_scales(u: pd.DataFrame) -> dict[str, float]:
    return {s: MARKETS[m].yahoo_scale for s, m in u.market.items() if MARKETS[m].yahoo_scale != 1.0}


@dataclass
class Panels:
    """Wide DataFrames (index = date, columns = symbols), prices in local currency."""
    open: pd.DataFrame
    high: pd.DataFrame
    low: pd.DataFrame
    close: pd.DataFrame
    volume: pd.DataFrame

    @property
    def dates(self) -> pd.DatetimeIndex:
        return self.close.index

    @property
    def symbols(self) -> list[str]:
        return list(self.close.columns)


class DataStore:
    def __init__(self, cache_dir: Path):
        self.dir = cache_dir
        self.dir.mkdir(parents=True, exist_ok=True)

    def _file(self, sym: str) -> Path:
        return self.dir / f"{sym.replace('/', '_').replace('.', '_')}.csv"

    def read(self, sym: str) -> pd.DataFrame | None:
        f = self._file(sym)
        if not f.exists():
            return None
        return pd.read_csv(f, index_col=0, parse_dates=True)

    def write(self, sym: str, df: pd.DataFrame) -> None:
        df.sort_index().to_csv(self._file(sym))

    def update(self, source, symbols: list[str], history_start: str, log=print) -> list[str]:
        """Incremental download; full refresh weekly (adjusted prices change after splits/dividends).
        Returns symbols that delivered no data (e.g. delisted or wrong ticker)."""
        groups: dict[str, list[str]] = {}
        for s in symbols:
            df = self.read(s)
            start = history_start
            if df is not None and not df.empty:
                age = datetime.now() - datetime.fromtimestamp(self._file(s).stat().st_mtime)
                weekly_full = datetime.now().weekday() == 5 and age > timedelta(days=1)
                if not weekly_full:
                    start = (df.index[-1] - pd.Timedelta(days=10)).strftime("%Y-%m-%d")
            groups.setdefault(start, []).append(s)
        missing = []
        for start, syms in groups.items():
            log(f"  {source.name}: {len(syms)} symbols from {start}")
            try:
                got = source.daily(syms, start)
            except Exception as e:  # noqa: BLE001 - one failed batch must not stop the session
                log(f"  ! download failed: {e}")
                got = {}
            for s in syms:
                new = got.get(s)
                if new is None or new.empty:
                    missing.append(s)
                    continue
                old = self.read(s) if start != history_start else None
                df = new if old is None else pd.concat([old[~old.index.isin(new.index)], new])
                self.write(s, df)
        return missing

    def panels(self, symbols: list[str], before: pd.Timestamp | None = None) -> Panels:
        """Panels from cache. `before` drops rows on/after that date (e.g. today's partial bar)."""
        frames = {s: self.read(s) for s in symbols}
        frames = {s: f for s, f in frames.items() if f is not None and not f.empty}
        if not frames:
            raise RuntimeError("No cached data. Run `python main.py fetch` first.")
        out = {}
        for fld in FIELDS:
            df = pd.DataFrame({s: f[fld] for s, f in frames.items()}).sort_index()
            if before is not None:
                df = df[df.index < before]
            out[fld] = df
        out["close"] = out["close"].ffill(limit=5)       # markets have different holidays
        return Panels(**out)
