"""Currency rates and the project's currency rule.

Rule (from the owner): currency effects are NOT counted until money is actually
exchanged after a sale. Implementation:
  * Buying a foreign stock converts NOK -> foreign currency at that day's rate.
    That rate is stored with the position ("entry FX").
  * While held, the position is valued at local price x entry FX. Currency moves
    do not change the reported value.
  * On a sale, proceeds are converted back to NOK at that day's rate. Only then
    is the currency gain or loss realised, and it is reported separately.

Rates: Norges Bank's official daily rates (NOK per 1 unit; SEK/DKK are quoted per
100 and normalised here). History is cached for backtests.
"""
from __future__ import annotations

import csv
import io
import json
from datetime import date
from pathlib import Path

import pandas as pd
import requests

NB = "https://data.norges-bank.no/api/data/EXR/B.{ccys}.NOK.SP"
CCYS = ("USD", "EUR", "SEK", "DKK", "GBP", "CHF")


def _num(x) -> float:
    """Parse '10.12', '10,12', '1,027.94' or '1.027,94' (the last separator is the decimal one)."""
    t = str(x).strip().replace(" ", "").replace("\u00a0", "")
    if "," in t and "." in t:
        dec = "," if t.rfind(",") > t.rfind(".") else "."
        t = t.replace("." if dec == "," else ",", "").replace(",", ".")
    else:
        t = t.replace(",", ".")
    return float(t)


def _parse_nb_csv(text: str) -> pd.DataFrame:
    text = text.lstrip("\ufeff")
    first = text.splitlines()[0] if text else ""
    delim = ";" if first.count(";") > first.count(",") else ","     # decide on the header line only
    rows = []
    for r in csv.DictReader(io.StringIO(text), delimiter=delim):
        um = str(r.get("UNIT_MULT") or "0").strip()
        mult = int(um) if um.lstrip("-").isdigit() else 0
        rows.append((r["TIME_PERIOD"], r["BASE_CUR"], _num(r["OBS_VALUE"]) / 10 ** mult))
    df = pd.DataFrame(rows, columns=["date", "ccy", "rate"])
    out = df.pivot(index="date", columns="ccy", values="rate")
    out.index = pd.to_datetime(out.index)
    return out.sort_index()


def fetch_norges_bank(start: str | None = None, last_n: int | None = None, ccys=CCYS) -> pd.DataFrame:
    params = {"format": "csv", "locale": "en"}
    if start:
        params["startPeriod"] = start
    if last_n:
        params["lastNObservations"] = last_n
    r = requests.get(NB.format(ccys="+".join(ccys)), params=params, timeout=30)
    r.raise_for_status()
    try:
        df = _parse_nb_csv(r.text)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"unexpected Norges Bank format ({e}); first lines: {r.text[:400]!r}") from e
    bad = [c for c in df.columns if not (0.05 < df[c].dropna().median() < 50)]
    if bad:
        raise ValueError(f"implausible Norges Bank rates for {bad}; first lines: {r.text[:400]!r}")
    return df


def fetch_yahoo_fx(start: str, ccys=CCYS) -> pd.DataFrame:
    """Fallback source: Yahoo Finance daily closes for XXXNOK=X."""
    import yfinance as yf  # type: ignore
    out = {}
    for c in ccys:
        df = yf.download(f"{c}NOK=X", start=start, interval="1d", auto_adjust=False, progress=False)
        if df is None or df.empty:
            continue
        close = df["Close"]
        if isinstance(close, pd.DataFrame):
            close = close.iloc[:, 0]
        close.index = pd.to_datetime(close.index).tz_localize(None).normalize()
        out[c] = close.astype(float)
    if not out:
        raise RuntimeError("Yahoo FX returned no data")
    return pd.DataFrame(out).sort_index()


class FxRates:
    """NOK per unit of foreign currency; NOK itself is 1."""

    def __init__(self, state_dir: Path, manual: dict | None = None):
        self.file = state_dir / "fx_history.csv"
        self.manual = manual or {}
        self.hist = pd.read_csv(self.file, index_col=0, parse_dates=True) if self.file.exists() else pd.DataFrame()

    def refresh(self, start: str = "2015-06-01", log=print) -> None:
        since = start if self.hist.empty else (self.hist.index[-1] - pd.Timedelta(days=10)).strftime("%Y-%m-%d")
        try:
            try:
                new = fetch_norges_bank(start=since)
            except Exception as e:  # noqa: BLE001
                log(f"  ! Norges Bank FX failed ({e}); using Yahoo FX instead")
                new = fetch_yahoo_fx(since)
            self.hist = pd.concat([self.hist[~self.hist.index.isin(new.index)], new]).sort_index()
            self.file.parent.mkdir(parents=True, exist_ok=True)
            self.hist.to_csv(self.file)
        except Exception as e:  # noqa: BLE001
            if self.hist.empty and not self.manual:
                raise RuntimeError(f"No FX rates: Norges Bank unreachable ({e}). Set fx.manual_rates in config.yaml.") from e
            log(f"  ! FX refresh failed ({e}); using cached/manual rates")

    def rate(self, ccy: str, on=None) -> float:
        if ccy == "NOK":
            return 1.0
        if not self.hist.empty and ccy in self.hist:
            s = self.hist[ccy].dropna()
            s = s.loc[:pd.Timestamp(on)] if on is not None else s
            if len(s):
                return float(s.iloc[-1])
        if ccy in self.manual:
            return float(self.manual[ccy])
        raise KeyError(f"No FX rate for {ccy}")

    def panel(self, index: pd.DatetimeIndex, ccys) -> pd.DataFrame:
        """Daily rates aligned to a trading calendar (forward-filled), for backtests."""
        df = pd.DataFrame(index=index)
        for c in set(ccys):
            if c == "NOK":
                df[c] = 1.0
            elif not self.hist.empty and c in self.hist:
                df[c] = self.hist[c].reindex(self.hist.index.union(index)).ffill().reindex(index).bfill()
            else:
                df[c] = float(self.manual.get(c, 1.0))
        return df


class StartLock:
    """For Alpaca (USD-only) accounts: the NOK->USD start conversion is locked once."""

    def __init__(self, state_dir: Path):
        self.file = state_dir / "fx_lock.json"
        self.data = json.loads(self.file.read_text()) if self.file.exists() else {}

    def lock(self, portfolio: str, start_nok: float, usdnok: float) -> dict:
        if portfolio not in self.data:
            self.data[portfolio] = {"usdnok": usdnok, "date": str(date.today()), "start_capital_nok": start_nok,
                                    "start_capital_usd": round(start_nok / usdnok, 2)}
            self.file.parent.mkdir(parents=True, exist_ok=True)
            self.file.write_text(json.dumps(self.data, indent=2))
        return self.data[portfolio]
