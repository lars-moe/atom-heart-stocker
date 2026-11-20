"""Turns a session's analysis into a research note: structured data for the
dashboard plus a plain-language write-up anyone can read."""
from __future__ import annotations

import pandas as pd

from .markets import MARKETS


def _name(u, s):
    return u.at[s, "name"] if s in u.index else s


def _nok(x):
    return f"NOK {x:,.0f}".replace(",", " ")


def _pct(x):
    return f"{x * 100:+.1f}%" if pd.notna(x) else "n/a"


def _agent_line(case: dict) -> str:
    """One readable sentence per agent: its strongest argument either way."""
    parts = []
    labels = {"opportunity": "Opportunity", "value": "Value", "risk": "Risk"}
    for a, v in (case.get("agents") or {}).items():
        tone = "likes it" if v["stance"] > 0.25 else "is sceptical" if v["stance"] < -0.25 else "is neutral"
        arg = (v["pros"][:1] if v["stance"] >= 0 else v["cons"][:1]) or v["pros"][:1] or v["cons"][:1]
        parts.append(f"{labels.get(a, a)} {tone}" + (f" ({arg[0].lower()})" if arg else ""))
    return "; ".join(parts)


def build_research(portfolio, session, today, now_oslo, snap, tables, score, team, holdings,
                   intents, records, universe, weights, fx_now, session_syms, equity, cash, brief="") -> dict:
    u = universe
    s = snap.loc[[x for x in snap.index if x in session_syms]]
    above = (s.close > s.sma200).mean() if len(s) else float("nan")
    mom = s.mom_3m.dropna().sort_values()
    by_market = {}
    for m in session.markets:
        sub = s[[u.at[x, "market"] == m for x in s.index]]
        if len(sub):
            by_market[MARKETS[m].name] = {"n": int(len(sub)), "above_200d": float((sub.close > sub.sma200).mean()),
                                          "avg_3m": float(sub.mom_3m.mean())}
    sc = score.reindex(s.index).dropna().sort_values(ascending=False)
    veto = tables["risk"].veto.reindex(s.index).fillna(True)
    bought = {r.symbol: r for r in records if r.side == "buy" and r.status not in ("refused", "rejected", "error")}
    proposed = {i.symbol: i for i in intents if i.side == "buy"}

    ideas = []
    for sym in [x for x in sc.index if x not in holdings or x in bought][:8]:
        if sym in bought:
            verdict = f"BOUGHT {bought[sym].qty} shares"
        elif veto.get(sym, False):
            verdict = "not bought: risk veto - " + (team.risk.veto_reason(snap.loc[sym]) or "risk limits")
        elif sc[sym] < team.coordinator.p.min_score_to_buy:
            verdict = f"not bought: score below the {team.coordinator.p.min_score_to_buy:+.2f} buy threshold"
        elif sym in proposed and sym not in bought:
            llm = proposed[sym].case.get("llm_review", {})
            verdict = "not bought: " + (f"reviewer {llm.get('verdict')}" if llm else "broker refused (rules/cash)")
        else:
            verdict = "not bought: " + team.coordinator.skips.get(sym, team.coordinator.skips.get("*", "lower ranked"))
        case = proposed[sym].case if sym in proposed else team.coordinator.debate(sym, snap, tables)
        ideas.append({"symbol": sym, "name": _name(u, sym), "market": u.at[sym, "market"], "score": round(float(sc[sym]), 3),
                      "verdict": verdict, "case": case, "summary": _agent_line(case)})

    review = []
    for sym, h in holdings.items():
        if sym not in session_syms or sym not in snap.index:
            continue
        r = snap.loc[sym]
        act = next((i for i in intents if i.symbol == sym and i.side == "sell"), None)
        review.append({"symbol": sym, "name": _name(u, sym), "score": round(float(score.get(sym, 0)), 3),
                       "return_local": float(r.close / h.entry_price - 1), "action": act.kind if act else "hold",
                       "reason": act.reason if act else "trend and risk checks passed"})

    rec = {"portfolio": portfolio, "date": today, "time": now_oslo, "session": session.name,
           "session_label": session.label, "analysed": int(len(s)), "above_200d": float(above),
           "avg_3m": float(s.mom_3m.mean()) if len(s) else float("nan"),
           "leaders": [(x, _name(u, x), float(mom[x])) for x in mom.index[-5:][::-1]],
           "laggards": [(x, _name(u, x), float(mom[x])) for x in mom.index[:5]],
           "by_market": by_market, "weights": weights, "fx": fx_now, "ideas": ideas, "review": review,
           "trades": [{"symbol": r.symbol, "name": _name(u, r.symbol), "side": r.side, "qty": r.qty,
                       "status": r.status, "kind": r.kind, "reason": r.reason, "notes": r.broker_notes,
                       "value_nok": r.notional} for r in records],
           "equity": equity, "cash": cash, "brief": brief}
    rec["text"] = narrative(rec)
    return rec


def narrative(r: dict) -> list[str]:
    """Plain-language paragraphs."""
    p = []
    breadth = r["above_200d"]
    tone = ("broadly healthy - most stocks are in long-term uptrends" if breadth > 0.6 else
            "mixed - roughly half the market is trending up" if breadth > 0.4 else
            "weak - most stocks are below their long-term trend")
    p.append(f"{r['session_label']}, {r['date']} at {r['time']} (Oslo time). The team analysed {r['analysed']} stocks. "
             f"Market tone is {tone} ({breadth:.0%} above their 200-day average; average 3-month move "
             f"{_pct(r['avg_3m'])}).")
    if r["leaders"]:
        p.append("Strongest over 3 months: " + ", ".join(f"{n} ({_pct(m)})" for _, n, m in r["leaders"][:3])
                 + ". Weakest: " + ", ".join(f"{n} ({_pct(m)})" for _, n, m in r["laggards"][:3]) + ".")
    if r["review"]:
        sells = [x for x in r["review"] if x["action"] != "hold"]
        p.append(f"Holdings in these markets: {len(r['review'])} reviewed. "
                 + ("All kept: trends intact and no stop-loss triggered." if not sells else
                    " ".join(f"{x['name']}: {x['action']} - {x['reason']}." for x in sells)))
    done = [t for t in r["trades"] if t["status"] not in ("refused", "rejected", "error")]
    if done:
        lines = []
        for t in done:
            idea = next((i for i in r["ideas"] if i["symbol"] == t["symbol"]), None)
            why = f" The team's view: {idea['summary']}." if idea and t["side"] == "buy" else f" Reason: {t['reason']}."
            lines.append(f"{'Bought' if t['side'] == 'buy' else 'Sold'} {t['qty']} {t['name']} "
                         f"(about {_nok(t['value_nok'])}).{why}")
        p.append("Trades: " + " ".join(lines))
    else:
        p.append("No trades this session.")
    passed = [i for i in r["ideas"] if i["verdict"].startswith("not bought")][:3]
    if passed:
        p.append("Considered but not bought: " + "; ".join(f"{i['name']} ({i['verdict'][12:]})" for i in passed) + ".")
    w = r["weights"]
    p.append("Current say of each analyst (learned from their track record): "
             + ", ".join(f"{k} {v:.0%}" for k, v in w.items()) + ".")
    return p
