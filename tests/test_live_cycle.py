"""A full live session (data -> agents -> debate -> sim broker -> journal -> research -> dashboard)
with a fake price source, so no network is needed."""
import types
from pathlib import Path

import pandas as pd

from ahs import engine
from ahs.config import load_config
from ahs.data import DataStore, load_universe
from ahs.synthetic import make_market


class FakeYahoo:
    name = "yahoo"

    def __init__(self, panels):
        self.p = panels

    def daily(self, symbols, start):
        return {}

    def latest(self, symbols):
        now = pd.Timestamp.now(tz="UTC")
        return {s: (float(self.p.close[s].iloc[-1]) * 1.001, now) for s in symbols if s in self.p.close}


def _setup(tmp_path, monkeypatch):
    cfg = load_config()
    cfg.raw["fx"]["manual_rates"] = {"USD": 10.0, "EUR": 11.5, "SEK": 0.98, "DKK": 1.54, "GBP": 13.2, "CHF": 12.3}
    cfg.raw["data"]["cache_dir"] = str(tmp_path / "cache")
    cfg.raw["debate"]["use_llm"] = False
    cfg.raw["data"]["use_fundamentals"] = False
    orig = cfg.path
    cfg.path = types.MethodType(lambda self, rel: tmp_path / rel if rel in ("state", "reports") else orig(rel), cfg)
    u = load_universe(orig("universe.csv"))
    yesterday = pd.Timestamp.now().normalize() - pd.Timedelta(days=1)
    p = make_market(list(u.index), years=7)
    for f in ("open", "high", "low", "close", "volume"):
        df = getattr(p, f)
        df.index = pd.bdate_range(end=yesterday, periods=len(df))
    store = DataStore(Path(cfg.raw["data"]["cache_dir"]) / "yahoo")
    for s in p.symbols:
        store.write(s, pd.DataFrame({f: getattr(p, f)[s] for f in ("open", "high", "low", "close", "volume")}))
    monkeypatch.setattr(engine, "YahooSource", lambda scales: FakeYahoo(p))
    monkeypatch.setattr(engine.FxRates, "refresh", lambda self, **k: None)
    return cfg


def test_sessions_trade_and_report(tmp_path, monkeypatch):
    cfg = _setup(tmp_path, monkeypatch)
    out = engine.run_session(cfg, "HIGH_RISK", "nordic", force=True, log=lambda *a: None)
    buys = [r for r in out["orders"] if r.side == "buy" and r.status == "filled"]
    assert buys, "expected buys on fresh cash"
    u = load_universe(cfg.path("universe.csv"))
    assert all(u.at[r.symbol, "market"] in ("OSE", "STO", "CPH", "HEL") for r in out["orders"])
    assert all(r.notional <= 0.25 * 200_000 + 1 for r in buys)
    assert out["research"]["text"] and out["research"]["ideas"]

    out2 = engine.run_session(cfg, "HIGH_RISK", "us", force=True, log=lambda *a: None)
    assert all(u.at[r.symbol, "market"] == "US" for r in out2["orders"])

    from ahs.dashboard import portfolio_from_ledger, write_dashboard
    from ahs.ledger import Ledger
    led = Ledger(tmp_path / "state" / "ledger.sqlite")
    pfs = [portfolio_from_ledger(cfg, led, p, None) for p in cfg.portfolios]
    html = write_dashboard(pfs, tmp_path / "reports" / "dashboard.html").read_text()
    assert "HIGH_RISK" in html and "NaN" not in html
    hv = pfs[1]["kpi"]
    assert hv["positions"] >= 1 and abs(hv["equity"] - 200_000) < 20_000


def test_dry_run_places_nothing(tmp_path, monkeypatch):
    cfg = _setup(tmp_path, monkeypatch)
    out = engine.run_session(cfg, "MAIN", "europe", dry_run=True, force=True, log=lambda *a: None)
    assert all(r.status in ("dry-run", "refused") for r in out["orders"])
    from ahs.ledger import Ledger
    assert Ledger(tmp_path / "state" / "ledger.sqlite").get("MAIN", "sim_book") is None


def test_yahoo_frame_parsing():
    from ahs.sources import _frame_for
    idx = pd.date_range("2026-01-01", periods=3)
    cols = pd.MultiIndex.from_product([["EQNR.OL", "SAP.DE"], ["Open", "High", "Low", "Close", "Volume"]])
    df = pd.DataFrame(1.0, index=idx, columns=cols)
    f = _frame_for(df, "SAP.DE")
    assert list(f.columns) == ["open", "high", "low", "close", "volume"] and len(f) == 3
    cols2 = pd.MultiIndex.from_product([["Open", "High", "Low", "Close", "Volume"], ["EQNR.OL"]])
    assert _frame_for(pd.DataFrame(2.0, index=idx, columns=cols2), "EQNR.OL") is not None


def test_norges_bank_csv_parsing():
    from ahs.fx import _parse_nb_csv
    txt = ("FREQ;BASE_CUR;QUOTE_CUR;TENOR;DECIMALS;CALCULATED;UNIT_MULT;COLLECTION;TIME_PERIOD;OBS_VALUE\n"
           "B;USD;NOK;SP;4;false;0;C;2026-11-19;10.1234\n"
           "B;SEK;NOK;SP;4;false;2;C;2026-11-19;97.50\n")
    df = _parse_nb_csv(txt)
    assert abs(df.loc["2026-11-19", "USD"] - 10.1234) < 1e-9 and abs(df.loc["2026-11-19", "SEK"] - 0.975) < 1e-9


def test_schedule_is_dst_aware():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from ahs.markets import next_runs
    # 2026-03-16: US already on summer time, Europe not yet -> US session at 15:30 Oslo
    runs = next_runs(datetime(2026, 3, 16, 5, 0, tzinfo=ZoneInfo("UTC")), 1)
    us = [t for t, s in runs if s.name == "us"][0].astimezone(ZoneInfo("Europe/Oslo"))
    assert (us.hour, us.minute) == (15, 30)


def test_due_sessions_follow_local_exchange_time():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from ahs.scheduler import due_sessions
    allx = ["nordic", "europe", "us"]
    utc = ZoneInfo("UTC")
    # Friday 20 Nov 2026 (winter): 08:37 UTC = 09:37 Oslo -> nothing yet
    assert due_sessions(allx, datetime(2026, 11, 20, 8, 37, tzinfo=utc)) == []
    # 09:37 UTC = 10:37 Oslo -> Nordic and Europe due
    names = {s.name for s, _ in due_sessions(allx, datetime(2026, 11, 20, 9, 37, tzinfo=utc))}
    assert names == {"nordic", "europe"}
    # 15:47 UTC = 10:47 New York -> US due; Nordic too late (Oslo closes 16:20 local);
    # Europe still inside its catch-up window (London/Frankfurt close 16:30 UTC)
    names = {s.name for s, _ in due_sessions(allx, datetime(2026, 11, 20, 15, 47, tzinfo=utc))}
    assert names == {"us", "europe"}
    # Summer (June): 08:37 UTC = 10:37 Oslo -> Nordic and Europe due
    names = {s.name for s, _ in due_sessions(allx, datetime(2026, 6, 15, 8, 37, tzinfo=utc))}
    assert names == {"nordic", "europe"}
    # Saturday -> nothing
    assert due_sessions(allx, datetime(2026, 11, 21, 9, 37, tzinfo=utc)) == []


def test_run_due_runs_each_session_once(tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from ahs.ledger import Ledger
    from ahs.scheduler import run_due
    cfg = load_config()
    led = Ledger(tmp_path / "l.sqlite")
    calls = []
    now = datetime(2026, 11, 20, 9, 37, tzinfo=ZoneInfo("UTC"))
    run_due(cfg, led, lambda p, s: calls.append((p, s)), log=lambda *a: None, now=now)
    run_due(cfg, led, lambda p, s: calls.append((p, s)), log=lambda *a: None, now=now)
    assert sorted(calls) == sorted([(p, s) for p in cfg.portfolios for s in ("nordic", "europe")])


def test_run_due_retries_failures(tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from ahs.ledger import Ledger
    from ahs.scheduler import run_due
    cfg = load_config()
    led = Ledger(tmp_path / "l.sqlite")
    now = datetime(2026, 11, 20, 9, 37, tzinfo=ZoneInfo("UTC"))
    calls = []

    def flaky(p, s):
        calls.append((p, s))
        if p == "HIGH_RISK" and s == "nordic" and calls.count((p, s)) == 1:
            raise RuntimeError("rate limited")
    run_due(cfg, led, flaky, log=lambda *a: None, now=now)
    run_due(cfg, led, flaky, log=lambda *a: None, now=now)
    run_due(cfg, led, flaky, log=lambda *a: None, now=now)
    assert calls.count(("HIGH_RISK", "nordic")) == 2      # retried once, then succeeded
    assert calls.count(("MAIN", "nordic")) == 1           # not re-run
