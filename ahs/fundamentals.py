"""Optional current fundamentals for the Value agent (live runs only).

Uses `yfinance` if it is installed (pip install yfinance). It is an unofficial
source that sometimes breaks; when it does, the Value agent simply falls back to
its price-based 5-year view. Results are cached for a week.
"""
from __future__ import annotations

import json
import time
from pathlib import Path


def load_fundamentals(symbols: list[str], cache: Path, log=print) -> dict[str, dict]:
    data = json.loads(cache.read_text()) if cache.exists() else {}
    if data and time.time() - data.get("_ts", 0) < 7 * 86400:
        return {k: v for k, v in data.items() if not k.startswith("_")}
    try:
        import yfinance as yf  # type: ignore
    except ImportError:
        log("  (yfinance not installed: Value agent uses price-based view only)")
        return {}
    out = {}
    for s in symbols:
        try:
            info = yf.Ticker(s.replace(".", "-")).info
            if info.get("quoteType") == "ETF":
                continue
            mc, fcf = info.get("marketCap"), info.get("freeCashflow")
            out[s] = {"forward_pe": info.get("forwardPE"), "peg": info.get("pegRatio") or info.get("trailingPegRatio"),
                      "fcf_yield": (fcf / mc) if fcf and mc else None, "debt_to_equity": info.get("debtToEquity")}
        except Exception:  # noqa: BLE001
            continue
    cache.write_text(json.dumps({**out, "_ts": time.time()}))
    return out
