"""Minimal Alpaca REST client (Trading API v2 + Market Data v2) built on `requests`.

Kept deliberately small and explicit so every call the system makes is visible.
Refuses to talk to the live trading endpoint unless the owner has opted in twice.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass

import requests

LIVE_URL = "https://api.alpaca.markets"


class AlpacaError(RuntimeError):
    pass


@dataclass
class Credentials:
    key: str
    secret: str

    @classmethod
    def from_env(cls, prefix: str) -> "Credentials":
        key, secret = os.getenv(f"{prefix}_KEY"), os.getenv(f"{prefix}_SECRET")
        if not key or not secret:
            raise AlpacaError(f"Missing {prefix}_KEY / {prefix}_SECRET in .env")
        return cls(key, secret)


class AlpacaClient:
    def __init__(self, creds: Credentials, trading_url: str, data_url: str,
                 allow_live: bool = False, feed: str = "sip"):
        trading_url = trading_url.rstrip("/")
        if trading_url == LIVE_URL:
            if not (allow_live and os.getenv("AHS_I_UNDERSTAND_REAL_MONEY") == "yes"):
                raise AlpacaError("Live trading URL refused. Set alpaca.allow_live AND "
                                  "AHS_I_UNDERSTAND_REAL_MONEY=yes to trade real money.")
        self.trading_url, self.data_url, self.feed = trading_url, data_url.rstrip("/"), feed
        self.s = requests.Session()
        self.s.headers.update({"APCA-API-KEY-ID": creds.key, "APCA-API-SECRET-KEY": creds.secret})

    # ---------- low level ----------
    def _req(self, method: str, url: str, **kw):
        for attempt in range(5):
            r = self.s.request(method, url, timeout=30, **kw)
            if r.status_code == 429:               # rate limit (free plan: 200/min)
                time.sleep(2 + attempt * 3)
                continue
            if r.status_code >= 400:
                raise AlpacaError(f"{method} {url} -> {r.status_code}: {r.text[:300]}")
            return r.json() if r.text else {}
        raise AlpacaError(f"Rate limited repeatedly: {url}")

    def _t(self, method, path, **kw):
        return self._req(method, f"{self.trading_url}{path}", **kw)

    # ---------- trading ----------
    def account(self) -> dict:
        return self._t("GET", "/v2/account")

    def clock(self) -> dict:
        return self._t("GET", "/v2/clock")

    def positions(self) -> list[dict]:
        return self._t("GET", "/v2/positions")

    def open_orders(self) -> list[dict]:
        return self._t("GET", "/v2/orders", params={"status": "open", "limit": 500})

    def orders_since(self, after_iso: str) -> list[dict]:
        return self._t("GET", "/v2/orders", params={"status": "all", "after": after_iso, "limit": 500})

    def asset(self, symbol: str) -> dict | None:
        try:
            return self._t("GET", f"/v2/assets/{symbol}")
        except AlpacaError:
            return None

    def submit_order(self, symbol: str, qty: int, side: str, limit_price: float | None,
                     client_order_id: str, tif: str = "day") -> dict:
        """Buys are limit orders (caps the cost, so the 25 % rule holds at fill).
        Sells are market orders so a stop-loss exit is never stuck on a gap down."""
        body = {"symbol": symbol, "qty": str(int(qty)), "side": side, "time_in_force": tif,
                "client_order_id": client_order_id[:48]}
        if limit_price is not None:
            body.update(type="limit", limit_price=f"{limit_price:.2f}")
        else:
            body["type"] = "market"
        return self._t("POST", "/v2/orders", json=body)

    # ---------- market data ----------
    def daily_bars(self, symbols: list[str], start: str, end: str | None = None) -> dict[str, list]:
        """Split/dividend-adjusted daily bars. Returns {symbol: [bar, ...]}."""
        out: dict[str, list] = {s: [] for s in symbols}
        for i in range(0, len(symbols), 50):
            chunk = symbols[i:i + 50]
            params = {"symbols": ",".join(chunk), "timeframe": "1Day", "start": start,
                      "adjustment": "all", "limit": 10000, "feed": self.feed}
            if end:
                params["end"] = end
            token = None
            while True:
                if token:
                    params["page_token"] = token
                try:
                    js = self._req("GET", f"{self.data_url}/v2/stocks/bars", params=params)
                except AlpacaError as e:
                    if self.feed == "sip" and ("403" in str(e) or "subscription" in str(e).lower()):
                        self.feed = params["feed"] = "iex"   # free-plan fallback
                        continue
                    raise
                for sym, bars in (js.get("bars") or {}).items():
                    out.setdefault(sym, []).extend(bars)
                token = js.get("next_page_token")
                if not token:
                    break
        return out

    def latest_trades(self, symbols: list[str]) -> dict[str, float]:
        out = {}
        for i in range(0, len(symbols), 100):
            js = self._req("GET", f"{self.data_url}/v2/stocks/trades/latest",
                           params={"symbols": ",".join(symbols[i:i + 100]), "feed": "iex"})
            for sym, t in (js.get("trades") or {}).items():
                out[sym] = float(t["p"])
        return out
