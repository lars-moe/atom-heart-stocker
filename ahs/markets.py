"""Exchanges, opening hours and the trading sessions the team runs each day.

Each session runs once per trading day, about an hour after its markets open,
when the opening auction noise has settled. Times are in each exchange's own
time zone, so summer/winter time shifts are handled automatically.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Market:
    code: str
    name: str
    tz: str
    open: time
    close: time
    yahoo_scale: float = 1.0      # LSE quotes on Yahoo are in pence -> 0.01
    ibkr_exchange: str = "SMART"
    ibkr_primary: str = ""


MARKETS = {
    "OSE": Market("OSE", "Oslo Børs", "Europe/Oslo", time(9, 0), time(16, 20), ibkr_primary="OSE"),
    "STO": Market("STO", "Nasdaq Stockholm", "Europe/Stockholm", time(9, 0), time(17, 30), ibkr_primary="SFB"),
    "CPH": Market("CPH", "Nasdaq Copenhagen", "Europe/Copenhagen", time(9, 0), time(17, 0), ibkr_primary="CPH"),
    "HEL": Market("HEL", "Nasdaq Helsinki", "Europe/Helsinki", time(10, 0), time(18, 30), ibkr_primary="HEX"),
    "XETRA": Market("XETRA", "Xetra (Frankfurt)", "Europe/Berlin", time(9, 0), time(17, 30), ibkr_primary="IBIS"),
    "EURONEXT": Market("EURONEXT", "Euronext (Paris/Amsterdam)", "Europe/Paris", time(9, 0), time(17, 30),
                       ibkr_primary="SBF"),
    "MIL": Market("MIL", "Borsa Italiana", "Europe/Rome", time(9, 0), time(17, 30), ibkr_primary="BVME"),
    "BME": Market("BME", "Bolsa de Madrid", "Europe/Madrid", time(9, 0), time(17, 30), ibkr_primary="BM"),
    "SIX": Market("SIX", "SIX Swiss Exchange", "Europe/Zurich", time(9, 0), time(17, 30), ibkr_primary="EBS"),
    "LSE": Market("LSE", "London Stock Exchange", "Europe/London", time(8, 0), time(16, 30), 0.01, ibkr_primary="LSE"),
    "US": Market("US", "NYSE / Nasdaq", "America/New_York", time(9, 30), time(16, 0)),
}


@dataclass(frozen=True)
class Session:
    name: str
    label: str
    tz: str
    run_at: time
    markets: tuple[str, ...]
    probe: str          # a liquid symbol used to detect holidays (no trade today = market closed)


SESSIONS = {
    "nordic": Session("nordic", "Nordic session (Oslo, Stockholm, Copenhagen, Helsinki)", "Europe/Oslo",
                      time(10, 15), ("OSE", "STO", "CPH", "HEL"), "EQNR.OL"),
    "europe": Session("europe", "European session (Frankfurt, Paris, Amsterdam, Milan, Madrid, Zurich, London)",
                      "Europe/Berlin", time(10, 30), ("XETRA", "EURONEXT", "MIL", "BME", "SIX", "LSE"), "SAP.DE"),
    "us": Session("us", "US session (NYSE, Nasdaq; incl. Asian/LatAm ADRs)", "America/New_York",
                  time(10, 30), ("US",), "SPY"),
}


def session_of_market(code: str) -> str:
    for s in SESSIONS.values():
        if code in s.markets:
            return s.name
    raise KeyError(code)


def is_market_hours(code: str, now: datetime | None = None) -> bool:
    m = MARKETS[code]
    now = (now or datetime.now(ZoneInfo("UTC"))).astimezone(ZoneInfo(m.tz))
    return now.weekday() < 5 and m.open <= now.time() <= m.close


def next_runs(now: datetime | None = None, horizon_days: int = 7) -> list[tuple[datetime, Session]]:
    """Upcoming session start times (UTC), weekdays only, sorted."""
    now = now or datetime.now(ZoneInfo("UTC"))
    out = []
    for s in SESSIONS.values():
        tz = ZoneInfo(s.tz)
        local = now.astimezone(tz)
        for d in range(horizon_days):
            day = (local + timedelta(days=d)).date()
            if day.weekday() >= 5:
                continue
            at = datetime.combine(day, s.run_at, tzinfo=tz)
            if at > now:
                out.append((at.astimezone(ZoneInfo("UTC")), s))
    return sorted(out, key=lambda x: x[0])


def schedule_table() -> list[str]:
    """Human-readable schedule in Oslo time for today (DST-aware)."""
    oslo = ZoneInfo("Europe/Oslo")
    today = datetime.now(oslo).date()
    rows = []
    for s in SESSIONS.values():
        at = datetime.combine(today, s.run_at, tzinfo=ZoneInfo(s.tz)).astimezone(oslo)
        rows.append(f"{at:%H:%M} Oslo time  {s.label}")
    return sorted(rows)
