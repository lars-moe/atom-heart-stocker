"""Optional LLM review of proposed buys (live runs only).

Claude reads each candidate's case (every agent's pros and cons, conflicts,
metrics) plus the current portfolio, and answers per candidate with
approve / reduce / reject. It can only shrink or block a trade, never enlarge one,
and its verdict is stored next to the trade in the journal.

Never used in backtests: a language model trained on recent data already "knows"
how 2021-2026 played out, which would make a backtest meaningless.
"""
from __future__ import annotations

import json
import os
import re

import requests

from .agents.broker import TradeIntent

API = "https://api.anthropic.com/v1/messages"

SYSTEM = """You are the devil's-advocate member of an investment committee for a
{portfolio} paper-trading portfolio ({style}). Analyst agents have proposed buys.
For each candidate, weigh the pros and cons critically: look for crowded trades,
concentration, event risk, weak evidence and conflicts between agents. You may
APPROVE, REDUCE (give size_multiplier between 0.25 and 0.9) or REJECT. You may
not increase any position. Reply with JSON only:
{{"reviews":[{{"symbol":"...","verdict":"approve|reduce|reject","size_multiplier":1.0,
"reason":"one or two sentences"}}]}}"""


def review(intents: list[TradeIntent], portfolio: str, style: str, holdings_summary: str,
           model: str, max_candidates: int = 8) -> dict[str, dict]:
    key = os.getenv("ANTHROPIC_API_KEY")
    buys = [i for i in intents if i.side == "buy"][:max_candidates]
    if not key or not buys:
        return {}
    payload = [{"symbol": i.symbol, "proposed_qty": i.qty, "price": i.ref_price, "reason": i.reason,
                "case": i.case} for i in buys]
    msg = (f"Current holdings: {holdings_summary}\n\nCandidates:\n{json.dumps(payload, indent=1)}")
    try:
        r = requests.post(API, timeout=120, headers={
            "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": model, "max_tokens": 2000,
                  "system": SYSTEM.format(portfolio=portfolio, style=style),
                  "messages": [{"role": "user", "content": msg}]})
        r.raise_for_status()
        text = "".join(b.get("text", "") for b in r.json().get("content", []))
        js = json.loads(re.search(r"\{.*\}", text, re.S).group(0))
        return {x["symbol"]: x for x in js.get("reviews", [])}
    except Exception as e:  # noqa: BLE001 - the review is advisory; failure must not stop the run
        return {"_error": {"reason": str(e)[:300]}}


def apply_reviews(intents: list[TradeIntent], reviews: dict[str, dict]) -> list[TradeIntent]:
    out = []
    for i in intents:
        rv = reviews.get(i.symbol)
        if i.side != "buy" or not rv:
            out.append(i)
            continue
        i.case["llm_review"] = rv
        verdict = str(rv.get("verdict", "approve")).lower()
        if verdict == "reject":
            continue
        if verdict == "reduce":
            m = min(0.9, max(0.25, float(rv.get("size_multiplier", 0.5))))
            i.qty = int(i.qty * m)
            if i.qty < 1:
                continue
        out.append(i)
    return out


BRIEF_SYSTEM = """You write the daily research note for a paper-trading portfolio run by a team of
analyst agents. Write 4-6 sentences in plain English for a private investor: what the market
looked like in this session, what the team did and why, and the main risk to watch. Use only
the data given. No hype, no advice to the reader, no invented facts."""


def brief(record: dict, model: str) -> str:
    """Optional plain-language summary by Claude. Returns "" if no API key or on failure."""
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return ""
    slim = {k: record[k] for k in ("session_label", "date", "analysed", "above_200d", "avg_3m", "leaders",
                                   "laggards", "review", "trades", "weights") if k in record}
    slim["ideas"] = [{k: i[k] for k in ("name", "score", "verdict", "summary")} for i in record.get("ideas", [])[:6]]
    try:
        r = requests.post(API, timeout=90, headers={
            "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": model, "max_tokens": 500, "system": BRIEF_SYSTEM,
                  "messages": [{"role": "user", "content": json.dumps(slim, default=str)}]})
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json().get("content", [])).strip()
    except Exception:  # noqa: BLE001
        return ""
