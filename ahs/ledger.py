"""SQLite journal: every opinion, decision, order, cost and daily value is kept,
so any trade can be traced back to the arguments that produced it."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY, ts TEXT, portfolio TEXT, mode TEXT, full_review INT,
  weights TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS decisions(run_id INT, portfolio TEXT, date TEXT, symbol TEXT, side TEXT, kind TEXT,
  qty INT, score REAL, reason TEXT, case_json TEXT);
CREATE TABLE IF NOT EXISTS orders(run_id INT, portfolio TEXT, date TEXT, symbol TEXT, side TEXT, qty INT,
  limit_price REAL, notional_nok REAL, currency TEXT, status TEXT, kind TEXT, reason TEXT, notes TEXT, order_id TEXT);
CREATE TABLE IF NOT EXISTS nav(portfolio TEXT, date TEXT, equity REAL, cash REAL, equity_nok REAL,
  cum_cost REAL, PRIMARY KEY(portfolio, date));
CREATE TABLE IF NOT EXISTS position_state(portfolio TEXT, symbol TEXT, entry_price REAL, entry_date TEXT,
  high_water REAL, PRIMARY KEY(portfolio, symbol));
CREATE TABLE IF NOT EXISTS kv(portfolio TEXT, key TEXT, value TEXT, PRIMARY KEY(portfolio, key));
CREATE TABLE IF NOT EXISTS ic_log(portfolio TEXT, date TEXT, agent TEXT, ic REAL);
CREATE TABLE IF NOT EXISTS research(portfolio TEXT, date TEXT, session TEXT, json TEXT,
  PRIMARY KEY(portfolio, date, session));
"""


class Ledger:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)

    def q(self, sql, args=()):
        return self.db.execute(sql, args).fetchall()

    def get(self, portfolio, key, default=None):
        r = self.q("SELECT value FROM kv WHERE portfolio=? AND key=?", (portfolio, key))
        return r[0][0] if r else default

    def put(self, portfolio, key, value):
        self.db.execute("INSERT OR REPLACE INTO kv VALUES(?,?,?)", (portfolio, key, str(value)))
        self.db.commit()

    def new_run(self, portfolio, mode, full_review, weights, note="") -> int:
        cur = self.db.execute("INSERT INTO runs(ts,portfolio,mode,full_review,weights,note) VALUES(?,?,?,?,?,?)",
                              (datetime.now().isoformat(timespec="seconds"), portfolio, mode, int(full_review),
                               json.dumps(weights), note))
        self.db.commit()
        return cur.lastrowid

    def log_decisions(self, run_id, portfolio, date, intents):
        self.db.executemany("INSERT INTO decisions VALUES(?,?,?,?,?,?,?,?,?,?)", [
            (run_id, portfolio, date, i.symbol, i.side, i.kind, i.qty, i.score, i.reason, json.dumps(i.case))
            for i in intents])
        self.db.commit()

    def log_orders(self, run_id, portfolio, date, records):
        self.db.executemany("INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [
            (run_id, portfolio, date, r.symbol, r.side, r.qty, r.limit_price, r.notional, r.currency,
             r.status, r.kind, r.reason, "; ".join(r.broker_notes), r.order_id) for r in records])
        self.db.commit()

    def log_nav(self, portfolio, date, equity, cash, equity_nok, cum_cost):
        self.db.execute("INSERT OR REPLACE INTO nav VALUES(?,?,?,?,?,?)",
                        (portfolio, date, equity, cash, equity_nok, cum_cost))
        self.db.commit()

    def log_ic(self, portfolio, rows):
        self.db.executemany("INSERT INTO ic_log VALUES(?,?,?,?)",
                            [(portfolio, r["date"], r["agent"], r["ic"]) for r in rows])
        self.db.commit()

    def save_research(self, portfolio, date, session, rec: dict, keep_days: int = 60):
        self.db.execute("INSERT OR REPLACE INTO research VALUES(?,?,?,?)",
                        (portfolio, date, session, json.dumps(rec, default=float)))
        self.db.execute("DELETE FROM research WHERE date < date(?, ?)", (date, f"-{keep_days} days"))
        self.db.execute("DELETE FROM decisions WHERE date < date(?, ?)", (date, f"-{keep_days} days"))
        self.db.commit()

    def research(self, portfolio, days: int = 30) -> list[dict]:
        rows = self.q("SELECT json FROM research WHERE portfolio=? ORDER BY date DESC, session LIMIT ?",
                      (portfolio, days * 3))
        return [json.loads(r[0]) for r in rows]

    def bought_on(self, portfolio, date) -> set[str]:
        return {r[0] for r in self.q("SELECT symbol FROM orders WHERE portfolio=? AND date=? AND side='buy' "
                                     "AND status NOT IN ('refused','error','rejected','dry-run','cancelled')", (portfolio, date))}

    def position_state(self, portfolio) -> dict[str, tuple]:
        return {r[0]: (r[1], r[2], r[3]) for r in
                self.q("SELECT symbol, entry_price, entry_date, high_water FROM position_state WHERE portfolio=?",
                       (portfolio,))}

    def set_position_state(self, portfolio, symbol, entry_price, entry_date, high_water):
        self.db.execute("INSERT OR REPLACE INTO position_state VALUES(?,?,?,?,?)",
                        (portfolio, symbol, entry_price, entry_date, high_water))
        self.db.commit()

    def drop_position_state(self, portfolio, keep: set[str]):
        for s in list(self.position_state(portfolio)):
            if s not in keep:
                self.db.execute("DELETE FROM position_state WHERE portfolio=? AND symbol=?", (portfolio, s))
        self.db.commit()
