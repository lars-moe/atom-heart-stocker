"""Runs every session at its time, every trading day, for every portfolio.

    python main.py scheduler        (leave it running; Ctrl+C to stop)

The computer must be awake during market hours (about 10:15-17:00 Oslo time).
If a session is missed (computer asleep), it is run late as long as that
session's markets are still open; otherwise it is skipped.
"""
from __future__ import annotations

import time
import traceback
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .markets import MARKETS, SESSIONS, next_runs, schedule_table


def due_sessions(enabled, now=None):
    """Sessions that should run right now: weekday, after their start time, and at least
    20 minutes before the FIRST of their markets closes (so every market is still open)."""
    now = now or datetime.now(ZoneInfo("UTC"))
    out = []
    for s in (SESSIONS[n] for n in enabled):
        tz = ZoneInfo(s.tz)
        local = now.astimezone(tz)
        if local.weekday() >= 5:
            continue
        start = datetime.combine(local.date(), s.run_at, tzinfo=tz)
        first_close = min(datetime.combine(local.date(), MARKETS[m].close, tzinfo=ZoneInfo(MARKETS[m].tz))
                          for m in s.markets)
        if start <= now <= first_close - timedelta(minutes=20):
            out.append((s, local.strftime("%Y-%m-%d")))
    return out


def run_due(cfg, ledger, run_one, log=print, now=None) -> list[str]:
    """One-shot runner for cron/GitHub Actions: runs each due session once per day.
    Safe to call many times; already-done sessions are skipped."""
    ran = []
    for s, day in due_sessions(cfg["schedule"].get("sessions", list(SESSIONS)), now):
        key = f"done_{s.name}"
        if ledger.get("_scheduler", key) == day:
            continue
        tries_key = f"tries_{s.name}_{day}"
        tries = int(ledger.get("_scheduler", tries_key) or 0) + 1
        ledger.put("_scheduler", tries_key, tries)
        log(f"=== {s.label} - {day} (attempt {tries}) ===")
        ok = True
        for p in cfg.portfolios:
            if ledger.get("_scheduler", f"ok_{p}_{s.name}") == day:
                continue                          # this portfolio already done today
            try:
                run_one(p, s.name)
                ledger.put("_scheduler", f"ok_{p}_{s.name}", day)
            except Exception:  # noqa: BLE001
                ok = False
                log(f"[{p}/{s.name}] FAILED:\n{traceback.format_exc()}")
        if ok or tries >= 3:                      # failed runs are retried on the next trigger (max 3)
            ledger.put("_scheduler", key, day)
        ran.append(s.name)
    if not ran:
        log("No session due right now.")
    return ran


def run_forever(cfg, run_one, after_each, log=print):
    enabled = cfg["schedule"].get("sessions", list(SESSIONS))
    log("Atom Heart Stocker scheduler started. Daily plan:")
    for row in schedule_table():
        log("   " + row)
    done: set[tuple[str, str]] = set()
    last_msg = ""
    while True:
        now = datetime.now(ZoneInfo("UTC"))
        # catch up on today's sessions that were missed but whose markets are still open
        for s in (SESSIONS[n] for n in enabled):
            tz = ZoneInfo(s.tz)
            local = now.astimezone(tz)
            key = (s.name, local.strftime("%Y-%m-%d"))
            start = datetime.combine(local.date(), s.run_at, tzinfo=tz)
            close = max(datetime.combine(local.date(), MARKETS[m].close, tzinfo=ZoneInfo(MARKETS[m].tz))
                        for m in s.markets)
            if key not in done and local.weekday() < 5 and start <= now <= close - timedelta(minutes=20):
                done.add(key)
                log(f"\n=== {s.label} - {local:%a %d %b %H:%M} ({s.tz}) ===")
                for p in cfg.portfolios:
                    try:
                        run_one(p, s.name)
                    except Exception:  # noqa: BLE001 - one failure must not stop the scheduler
                        log(f"[{p}/{s.name}] FAILED:\n{traceback.format_exc()}")
                try:
                    after_each()
                except Exception:  # noqa: BLE001
                    log(traceback.format_exc())
        upcoming = [(t, s) for t, s in next_runs(now) if s.name in enabled]
        if upcoming:
            t, s = upcoming[0]
            wait = (t - datetime.now(ZoneInfo("UTC"))).total_seconds()
            msg = f"next: {s.name} at {t.astimezone(ZoneInfo('Europe/Oslo')):%a %H:%M} Oslo time"
            if wait > 60 and msg != last_msg:
                log(msg)
                last_msg = msg
        time.sleep(60)
