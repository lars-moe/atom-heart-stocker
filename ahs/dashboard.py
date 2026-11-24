"""The dashboard: one self-contained HTML page (no internet needed to view it),
regenerated after every session.

Pages: Overview, Opportunities, Research, Trades, Operations.
Portfolio switch: Both portfolios / MAIN / HIGH RISK.

    python main.py dashboard          -> reports/dashboard.html
    python main.py serve              -> http://localhost:8050
On GitHub Actions it is published to GitHub Pages after every session.
"""
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path

import pandas as pd

from .markets import MARKETS, schedule_table

# ISO market identifier codes (MIC) for the listing column, e.g. XOSL:DNB
MIC = {"OSE": "XOSL", "STO": "XSTO", "CPH": "XCSE", "HEL": "XHEL", "XETRA": "XETR", "MIL": "XMIL",
       "BME": "XMAD", "SIX": "XSWX", "LSE": "XLON", "US": "US"}


def listing(symbol: str, market: str) -> str:
    base = symbol.split(".")[0] if market != "US" else symbol.replace("-", ".")
    mic = MIC.get(market, market)
    if market == "EURONEXT":
        mic = {"AS": "XAMS", "PA": "XPAR", "BR": "XBRU", "LS": "XLIS"}.get(symbol.rsplit(".", 1)[-1], "XPAR")
    return f"{mic}:{base}"


def _clean(o):
    """JSON-safe: NaN/inf -> None, numpy -> python."""
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if hasattr(o, "item"):
        o = o.item()
    if isinstance(o, float) and (math.isnan(o) or math.isinf(o)):
        return None
    if isinstance(o, (pd.Timestamp, datetime)):
        return str(o.date()) if hasattr(o, "date") else str(o)
    return o


def _alloc(holdings: list[dict], equity: float, cash: float) -> dict:
    out = {}
    for key in ("region", "market", "sector"):
        agg: dict[str, float] = {}
        for h in holdings:
            k = MARKETS[h[key]].name if key == "market" and h.get(key) in MARKETS else (h.get(key) or "Other")
            agg[k] = agg.get(k, 0) + h["value_nok"]
        agg["Cash"] = cash
        out[key] = sorted(([k, v / equity if equity else 0] for k, v in agg.items()), key=lambda x: -x[1])
    return out


def portfolio_from_ledger(cfg, ledger, name: str, bench_closes: pd.DataFrame | None) -> dict:
    prof = cfg.portfolios[name]
    hv = json.loads(ledger.get(name, "holdings_view") or "{}")
    nav = ledger.q("SELECT date, equity, cash FROM nav WHERE portfolio=? ORDER BY date", (name,))
    start = prof.start_capital_nok
    equity = hv.get("equity", start)
    cash = hv.get("cash", start)
    holdings = hv.get("holdings", [])
    realized = json.loads(ledger.get(name, "realized") or "[]")
    orders = ledger.q("SELECT date, symbol, side, qty, limit_price, notional_nok, currency, status, kind, reason, notes "
                      "FROM orders WHERE portfolio=? ORDER BY rowid DESC LIMIT 300", (name,))
    runs = ledger.q("SELECT ts, mode, full_review, weights, note FROM runs WHERE portfolio=? ORDER BY id", (name,))
    weights = _dedupe_weights([(r[0][:10], json.loads(r[3])) for r in runs])
    ics = {a: [v, n] for a, v, n in ledger.q("SELECT agent, AVG(ic), COUNT(*) FROM ic_log WHERE portfolio=? "
                                              "GROUP BY agent", (name,))}
    bench = {}
    if bench_closes is not None and nav:
        d0 = pd.Timestamp(nav[0][0])
        for col in bench_closes.columns:
            s = bench_closes[col].dropna()
            s = s[s.index >= d0 - pd.Timedelta(days=5)]
            if len(s) > 1:
                bench[col] = [[str(d.date()), start * v / s.iloc[0]] for d, v in s.items()]
    prev = nav[-2][1] if len(nav) > 1 else start
    last_report = (hv.get("time") or (runs[-1][0].replace("T", " ")[:16] if runs else "")) or "not yet"
    started = runs[0][0][:16].replace("T", " ") if runs else None
    p = _pack(name, prof.broker, start, equity, cash, holdings, [[d, e] for d, e, _ in nav], bench,
              realized, [dict(zip(("date", "symbol", "side", "qty", "limit", "value_nok", "currency", "status",
                                   "kind", "reason", "notes"), o)) for o in orders],
              ledger.research(name, 30), weights, ics, equity - prev, hv.get("costs", {}))
    p["kpi"]["fx_valuation"] = hv.get("fx_valuation", "purchase")
    p.update({"revision": len(runs), "last_report": last_report, "started": started,
              "runs": [{"ts": r[0].replace("T", " ")[:16], "mode": r[1], "full": bool(r[2]), "note": r[4] or ""}
                       for r in runs[::-1][:60]]})
    return p


def _dedupe_weights(rows):
    out = []
    for d, w in rows:
        if out and out[-1][0] == d:
            out[-1] = [d, w]
        else:
            out.append([d, w])
    return out


def _pack(name, broker, start, equity, cash, holdings, nav, bench, realized, orders, research, weights, ics,
          day_change, costs):
    unreal = sum(h["value_nok"] - h["qty"] * h["entry_price"] * h["entry_fx"] for h in holdings)
    for h in holdings:
        h["listing"] = listing(h["symbol"], h.get("market", ""))
    return _clean({
        "name": name, "broker": broker, "start": start,
        "kpi": {"equity": equity, "cash": cash, "fx_valuation": "purchase", "positions": len(holdings), "ret_total": equity / start - 1,
                "day_change": day_change, "unrealized": unreal,
                "realized_price": sum(r.get("price_pnl_nok", 0) for r in realized),
                "realized_fx": sum(r.get("fx_pnl_nok", 0) for r in realized),
                "fx_not_counted": sum(h.get("fx_effect_not_counted", 0) for h in holdings),
                "costs": sum(costs.values()) if costs else 0, "cost_breakdown": costs},
        "nav": nav, "bench": bench, "holdings": sorted(holdings, key=lambda h: -h["value_nok"]),
        "alloc": _alloc(holdings, equity, cash), "realized": realized[::-1][:300], "orders": orders,
        "research": research, "weights": weights, "ics": ics,
        "revision": len(nav), "last_report": nav[-1][0] if nav else "", "started": nav[0][0] if nav else None,
        "runs": []})


def portfolio_from_backtest(cfg, res, universe, research: list[dict]) -> dict:
    """Demo/backtest data in the same shape as live data."""
    nav = [[str(d.date()), v] for d, v in res.nav.equity.items()]
    last = res.nav.index[-1]
    holdings = []
    for s, p in res.book.positions.items():
        px = float(res.last_prices.get(s, p.entry_price))
        holdings.append({"symbol": s, "name": universe.at[s, "name"], "market": p.market, "currency": p.currency,
                         "qty": p.qty, "entry_price": p.entry_price, "price": px, "entry_fx": p.entry_fx,
                         "value_nok": p.qty * px * p.entry_fx, "return_local": px / p.entry_price - 1,
                         "fx_effect_not_counted": p.qty * px * (res.last_fx.get(p.currency, p.entry_fx) - p.entry_fx),
                         "entry_date": p.entry_date, "region": universe.at[s, "region"],
                         "sector": universe.at[s, "sector"], "marked": f"close {last.date()}"})
    bench = {c: [[str(d.date()), v] for d, v in res.bench[c].items()] for c in res.bench.columns}
    orders = [{"date": f["date"], "symbol": f["symbol"], "side": f["side"], "qty": f["qty"], "limit": f["price"],
               "value_nok": f["value_nok"], "currency": f["currency"], "status": "filled",
               "kind": "", "reason": f.get("reason", ""), "notes": ""} for f in res.book.fills[::-1][:300]]
    weights = [[str(d.date()), r.to_dict()] for d, r in res.weights.iterrows()]
    eq = res.nav.equity
    p = _pack(res.portfolio, "backtest", cfg.portfolios[res.portfolio].start_capital_nok, float(eq.iloc[-1]),
              float(res.nav.cash.iloc[-1]), holdings, nav, bench, res.book.realized, orders, research, weights,
              res.ics, float(eq.iloc[-1] - eq.iloc[-2]), res.book.costs)
    p["last_report"] = f"{last.date()} 17:00"
    return p


def _sidebar_image(out_dir: Path) -> str | None:
    """Optional picture for the sidebar: put your own image in assets/ as sidebar.jpg/.png/.webp."""
    import shutil
    assets = Path(__file__).resolve().parent.parent / "assets"
    for ext in ("jpg", "jpeg", "png", "webp"):
        src = assets / f"sidebar.{ext}"
        if src.exists():
            out_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy(src, out_dir / src.name)
            return src.name
    return None


def _names() -> dict:
    """Company name and listing per symbol, so tables can show names instead of bare tickers."""
    try:
        from .data import load_universe
        u = load_universe(Path(__file__).resolve().parent.parent / "universe.csv")
        return {s: [r["name"], listing(s, r["market"]), r["currency"]] for s, r in u.iterrows()}
    except Exception:  # noqa: BLE001
        return {}


def write_dashboard(portfolios: list[dict], out: Path, mode: str = "live", note: str = "") -> Path:
    data = {"generated": datetime.now().strftime("%Y-%m-%d %H:%M"), "mode": mode, "note": note,
            "schedule": schedule_table(), "portfolios": portfolios, "sidebar_image": _sidebar_image(out.parent),
            "names": _names()}
    html = TEMPLATE.replace("__DATA__", json.dumps(_clean(data)).replace("</", "<\\/"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    return out


TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="600">
<title>Atom Heart Stocker</title>
<style>
:root{color-scheme:light;
 --side:#1d2b27;--side-2:#2a3b36;--side-text:#c9d3cf;--side-muted:#7f918b;
 --bg:#f4f2ee;--card:#ffffff;--line:#e3dfd7;--grid:#eeebe5;--head:#f6f4f0;
 --ink:#191a18;--ink-2:#55574f;--muted:#8b8c84;
 --main:#2f5d50;--risk:#b5643c;
 --s-main:#178a5e;--s-risk:#d65a24;--s-bench:#4f7fd0;--s-spy:#8b8c84;
 --up:#2e6b45;--up-bg:#e3efe6;--down:#b23a2a;--down-bg:#fbe4df;
 --serif:"Iowan Old Style","Palatino Linotype",Palatino,"Book Antiqua",Georgia,"Times New Roman",serif;
 --sans:Inter,"Segoe UI",system-ui,-apple-system,Roboto,Helvetica,Arial,sans-serif}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 var(--sans)}
.app{display:grid;grid-template-columns:240px 1fr;min-height:100vh}
aside{background:var(--side);color:var(--side-text);padding:44px 24px 28px;display:flex;flex-direction:column;position:sticky;top:0;height:100vh}
.brand{display:flex;align-items:center;gap:14px;padding:0 8px;margin-bottom:64px}
.brand .mark{font:400 34px/1 var(--serif);color:#f2efe8;letter-spacing:-.04em}
.brand .mark i{font-style:normal;color:#c9a46a}
.brand .name{font:700 10.5px/1.6 var(--sans);letter-spacing:.14em;color:#f2efe8;text-transform:uppercase}
.navlabel{font:600 10.5px var(--sans);letter-spacing:.2em;color:var(--side-muted);text-transform:uppercase;padding:0 12px;margin-bottom:14px}
nav.side button{display:block;width:100%;text-align:left;background:none;border:0;color:var(--side-text);font:15px var(--sans);padding:14px 14px;border-radius:8px;cursor:pointer;margin-bottom:6px}
nav.side button:hover{background:rgba(255,255,255,.04)}
nav.side button[aria-current=page]{background:var(--side-2);color:#fff;font-weight:600}
.sidefoot{margin-top:auto;padding:0 12px;font-size:12px;color:var(--side-text)}
.sidefoot .dot{display:inline-block;width:6px;height:6px;border-radius:50%;background:#9db8ad;margin-right:8px;vertical-align:middle}
.emblem{margin:18px 0 14px;border-radius:6px;background:#24342f;padding:14px;display:flex;justify-content:center}
main{padding:52px 48px 64px;max-width:1500px;width:100%}
.eyebrow{font:600 11px var(--sans);letter-spacing:.22em;color:var(--ink-2);text-transform:uppercase}
.titlerow{display:flex;justify-content:space-between;align-items:flex-start;gap:16px}
h1{font:400 40px/1.15 var(--serif);margin:18px 0 14px;letter-spacing:-.01em}
.lede{color:var(--ink-2);font-size:15px;margin:0 0 40px}
.btn{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:10px 16px;font:14px var(--sans);color:var(--ink);cursor:pointer}
.btn:hover{border-color:#cfc9bd}
.toolbar{display:flex;justify-content:space-between;align-items:center;margin-bottom:22px;gap:12px;flex-wrap:wrap}
.seg{display:inline-flex;background:#e9e6df;border-radius:8px;padding:4px}
.seg button{border:0;background:none;padding:9px 18px;border-radius:6px;font:13px var(--sans);color:var(--ink-2);cursor:pointer}
.seg button[aria-pressed=true]{background:var(--card);color:var(--ink);box-shadow:0 1px 2px rgba(0,0,0,.08)}
.status{font:600 11px var(--sans);letter-spacing:.14em;color:var(--ink-2);text-transform:uppercase}
.status::before{content:"";display:inline-block;width:7px;height:7px;border-radius:50%;background:#c9a46a;margin-right:8px;vertical-align:1px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:22px;margin-bottom:24px}
.card{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:28px 26px}
.pcard{border-top:3px solid var(--main)}.pcard.risk{border-top-color:var(--risk)}
.pcard .top{display:flex;justify-content:space-between;align-items:baseline}
.label{font:700 11px var(--sans);letter-spacing:.16em;text-transform:uppercase}
.small{font-size:12.5px;color:var(--ink-2)}
.klabel{font:500 10.5px var(--sans);letter-spacing:.16em;text-transform:uppercase;color:var(--ink-2);margin-top:34px}
.big{font:400 40px/1.2 var(--serif);margin:12px 0 14px;font-variant-numeric:lining-nums;letter-spacing:-.01em}
.badge{display:inline-block;font:600 12.5px var(--sans);padding:4px 8px;border-radius:3px;margin-right:10px}
.badge.up{background:var(--up-bg);color:var(--up)}.badge.down{background:var(--down-bg);color:var(--down)}
.since{font-size:12.5px;color:var(--ink-2)}
.kv{display:grid;grid-template-columns:1.6fr .8fr 1fr;border-top:1px solid var(--line);margin-top:26px;padding-top:22px;gap:12px}
.kv .k{font-size:11.5px;color:var(--ink-2);margin-bottom:8px}.kv .v{font-size:14px;font-variant-numeric:tabular-nums}
.meter{height:4px;background:#e7ebe5;border-radius:2px;margin:26px 0 10px;overflow:hidden}
.meter>div{height:100%;background:#b9c9bd}.risk .meter>div{background:#e2c0ab}
.foot{display:flex;justify-content:space-between;font-size:10.5px;color:var(--ink-2)}
h2{font:400 26px/1.2 var(--serif);margin:0}
.cardhead{display:flex;justify-content:space-between;align-items:center;margin-bottom:22px;gap:12px}
.sub{color:var(--ink-2);font-size:13px;margin:6px 0 0}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
thead th{background:var(--head);font:500 10.5px var(--sans);letter-spacing:.14em;text-transform:uppercase;color:var(--ink-2);text-align:left;padding:14px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
tbody td{padding:16px 8px;border-bottom:1px solid var(--grid);vertical-align:middle}
td.n,th.n{text-align:right}
.co b{font-weight:600}.co small{display:block;color:var(--ink-2);font-size:11px;margin-top:4px;letter-spacing:.02em}
.tw{overflow-x:auto}
.up{color:var(--up)}.down{color:var(--down)}.muted{color:var(--muted)}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(380px,1fr));gap:22px;margin-top:24px}
.bar{display:grid;grid-template-columns:170px 1fr 54px;gap:10px;align-items:center;margin:9px 0;font-size:13px}
.bar .t{height:8px;background:var(--grid);border-radius:2px;overflow:hidden}.bar .f{height:100%;background:var(--main);border-radius:0 3px 3px 0}
.bar .p{text-align:right;color:var(--ink-2);font-variant-numeric:tabular-nums}
.chart{position:relative}.chart svg{display:block;width:100%;height:auto}
.legend{display:flex;gap:18px;flex-wrap:wrap;font-size:12px;color:var(--ink-2);margin:0 0 8px}
.legend i{display:inline-block;width:16px;height:3px;border-radius:2px;margin-right:6px;vertical-align:middle}
.tip{position:absolute;pointer-events:none;background:#fff;border:1px solid var(--line);border-radius:6px;padding:7px 10px;font-size:12px;box-shadow:0 4px 14px rgba(0,0,0,.08);display:none;white-space:nowrap;z-index:5}
.note p{margin:0 0 12px;max-width:85ch;line-height:1.6}
.brief{border-left:3px solid var(--main);padding:2px 0 2px 14px;margin:0 0 14px}
details{border-top:1px solid var(--grid);padding:12px 0}td details{border:0;padding:0}summary{cursor:pointer;list-style:none}summary::-webkit-details-marker{display:none}
summary::before{content:"▸";display:inline-block;width:16px;color:var(--muted)}details[open] summary::before{content:"▾"}
.agent{margin:10px 0 4px 16px;font-size:13px}.agent b{font-weight:600}
.pro{color:var(--up)}.con{color:var(--down)}
.chip{display:inline-block;font-size:11px;padding:2px 8px;border-radius:10px;border:1px solid var(--line);color:var(--ink-2);margin-left:6px;white-space:nowrap}
.chip.main{border-color:#b9c9bd;color:var(--main)}.chip.risk{border-color:#e2c0ab;color:var(--risk)}
.chip.ok{background:var(--up-bg);border-color:transparent;color:var(--up)}.chip.no{background:#f1efe9;border-color:transparent}
.banner{background:#fbf6ea;border:1px solid #eadfc5;color:#6d5a2c;padding:12px 16px;border-radius:6px;margin-bottom:22px;font-size:13px}
.empty{color:var(--ink-2);padding:8px 0}
select{font:inherit;padding:8px 10px;border-radius:6px;border:1px solid var(--line);background:#fff}
@media (max-width:900px){.app{grid-template-columns:1fr}aside{position:static;height:auto;padding:18px 16px}
 .brand{margin-bottom:14px}.navlabel,.sidefoot{display:none}nav.side{display:flex;overflow-x:auto;gap:4px}
 nav.side button{width:auto;white-space:nowrap;padding:9px 12px;margin:0}main{padding:28px 16px 48px}
 .cards{grid-template-columns:1fr}.big{font-size:32px}h1{font-size:30px}.kv{grid-template-columns:1fr 1fr 1fr}}
</style></head>
<body><div class="app">
<aside>
 <div class="brand"><div class="mark">a<i>h</i>s</div><div class="name">Atom Heart<br>Stocker 1.0</div></div>
 <div class="navlabel">Investment lab</div>
 <nav class="side" id="nav"></nav>
 <div class="sidefoot">
  <div><span class="dot"></span>Private · Paper investing</div>
  <div class="emblem" id="emblem" aria-hidden="true"><svg width="120" height="96" viewBox="0 0 120 96">
   <g fill="none" stroke="#c9a46a" stroke-width="1.3" opacity=".9">
    <ellipse cx="60" cy="48" rx="46" ry="15"/><ellipse cx="60" cy="48" rx="46" ry="15" transform="rotate(60 60 48)"/>
    <ellipse cx="60" cy="48" rx="46" ry="15" transform="rotate(-60 60 48)"/></g>
   <path d="M60 58c-9-6-14-10-14-16a7 7 0 0 1 14-2 7 7 0 0 1 14 2c0 6-5 10-14 16z" fill="#c9a46a"/></svg></div>
  <div>Two independent books</div>
 </div>
</aside>
<main id="main"></main>
</div>
<script>
const D=__DATA__;
const PAGES=["Overview","Opportunities","Research","Trades","Operations"];
const LEDE={Overview:"Find opportunities. Assess value. Execute supported paper orders.",
 Opportunities:"What the team liked in the latest sessions, and why each idea was or wasn't bought.",
 Research:"The daily research notes, session by session, in plain language.",
 Trades:"Every order and every closed position, with the reason behind it.",
 Operations:"Sessions, schedule, learning and rules: how the machine is running."};
let page="Overview", view="both";
const $=s=>document.querySelector(s);
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const f2=v=>v==null?"–":Number(v).toLocaleString("en-US",{minimumFractionDigits:2,maximumFractionDigits:2});
const f0=v=>v==null?"–":Number(v).toLocaleString("en-US",{maximumFractionDigits:0});
const nok=v=>v==null?"–":"NOK "+f2(v);
const pct=(v,d=2)=>v==null?"–":(v>=0?"":"-")+Math.abs(v*100).toFixed(d)+"%";
const cls=v=>v==null?"":(v>=0?"up":"down");
const pname=n=>n.replace("_","\u00a0");
const fmtTs=t=>{if(!t)return"–";if(String(t).length===10)return fmtD(t);const d=new Date(t.replace(" ","T"));return isNaN(d)?esc(t):d.toLocaleString("en-GB",{day:"numeric",month:"short",year:"numeric",hour:"2-digit",minute:"2-digit"}).replace(" at",",");};
const fmtD=t=>{if(!t)return"–";const d=new Date(t+(t.length===10?"T12:00":""));return isNaN(d)?esc(t):d.toLocaleDateString("en-GB",{day:"numeric",month:"short",year:"numeric"});};
const css=n=>getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const shown=()=>D.portfolios.filter(p=>view==="both"||p.name===view);
const isRisk=p=>/RISK/.test(p.name);
const co=s=>{const n=(D.names||{})[s];return n?`<b>${esc(n[0])}</b><small>${esc(n[1])} · ${esc(n[2])}</small>`:`<b>${esc(s)}</b>`;};
const order=n=>D.portfolios.findIndex(p=>p.name===n);
const mark=m=>!m?"–":String(m).startsWith("close ")?"Close "+fmtD(String(m).slice(6)):fmtTs(m);

function shell(){
 $("#nav").innerHTML=PAGES.map(p=>`<button aria-current="${p===page?"page":"false"}" data-p="${p}">${p}</button>`).join("");
 $("#nav").onclick=e=>{if(e.target.dataset.p){page=e.target.dataset.p;render();window.scrollTo(0,0);}};
}
function head(){
 const seg=[["both","Both portfolios"],...D.portfolios.map(p=>[p.name,pname(p.name)])];
 return `<div class="eyebrow">Investment lab / ${esc(page)}</div>
 <div class="titlerow"><h1>Atom Heart Stocker</h1><button class="btn" onclick="location.reload()">↻ Refresh</button></div>
 <p class="lede">${LEDE[page]}</p>
 ${D.note?`<div class="banner">${esc(D.note)}</div>`:""}
 <div class="toolbar"><div class="seg" id="seg">${seg.map(([k,l])=>`<button aria-pressed="${k===view}" data-v="${k}">${esc(l)}</button>`).join("")}</div>
 <span class="status">${D.mode==="live"?"Active":esc(D.mode)}</span></div>`;
}
function wireSeg(){$("#seg").onclick=e=>{if(e.target.dataset.v){view=e.target.dataset.v;render();}};}

/* ---------- line chart: 2px lines, legend + end labels, crosshair tooltip ---------- */
function lineChart(el,series,o={}){
 const W=el.clientWidth||900,H=o.h||300,m={l:76,r:110,t:12,b:30};
 const all=series.flatMap(s=>s.data.map(d=>d[1])).filter(v=>v!=null);
 if(all.length<2){el.innerHTML=`<p class="empty">The chart fills in after the first sessions.</p>`;return;}
 const dates=[...new Set(series.flatMap(s=>s.data.map(d=>d[0])))].sort(),xi=new Map(dates.map((d,i)=>[d,i]));
 let lo=Math.min(...all),hi=Math.max(...all);if(o.ref!=null){lo=Math.min(lo,o.ref);hi=Math.max(hi,o.ref);}const pad=(hi-lo)*.08||1;lo-=pad;hi+=pad;
 const X=i=>m.l+(W-m.l-m.r)*(dates.length>1?i/(dates.length-1):.5),Y=v=>m.t+(H-m.t-m.b)*(1-(v-lo)/(hi-lo)),fmt=o.fmt||f0;
 let g=`<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(o.label||"chart")}">`;
 for(let k=0;k<=4;k++){const v=lo+(hi-lo)*k/4;g+=`<line x1="${m.l}" x2="${W-m.r}" y1="${Y(v)}" y2="${Y(v)}" stroke="${css("--grid")}"/><text x="${m.l-10}" y="${Y(v)+4}" font-size="11" text-anchor="end" fill="${css("--muted")}">${fmt(v)}</text>`;}
 const nt=Math.min(6,dates.length);for(let k=0;k<nt;k++){const i=Math.round(k*(dates.length-1)/Math.max(nt-1,1));g+=`<text x="${X(i)}" y="${H-8}" font-size="11" text-anchor="middle" fill="${css("--muted")}">${fmtD(dates[i])}</text>`;}
 if(o.ref!=null)g+=`<line x1="${m.l}" x2="${W-m.r}" y1="${Y(o.ref)}" y2="${Y(o.ref)}" stroke="${css("--muted")}" stroke-dasharray="3 4"/>`;
 series.forEach(s=>{const pts=s.data.filter(d=>d[1]!=null).map(d=>`${X(xi.get(d[0])).toFixed(1)},${Y(d[1]).toFixed(1)}`).join(" ");
  g+=`<polyline fill="none" stroke="${css(s.color)}" stroke-width="2" stroke-linejoin="round" points="${pts}"/>`;
  const L=s.data.filter(d=>d[1]!=null).at(-1);if(L)g+=`<text x="${X(xi.get(L[0]))+8}" y="${Y(L[1])+4}" font-size="11.5" fill="${css("--ink-2")}">${esc(s.name)}</text>`;});
 g+=`<line class="xh" y1="${m.t}" y2="${H-m.b}" stroke="${css("--muted")}" visibility="hidden"/>`;
 series.forEach((s,j)=>g+=`<circle class="d${j}" r="4" fill="${css(s.color)}" stroke="#fff" stroke-width="2" visibility="hidden"/>`);
 g+=`<rect x="${m.l}" y="${m.t}" width="${W-m.l-m.r}" height="${H-m.t-m.b}" fill="transparent"/></svg>`;
 el.innerHTML=(series.length>1?`<div class="legend">${series.map(s=>`<span><i style="background:${css(s.color)}"></i>${esc(s.name)}</span>`).join("")}</div>`:"")+`<div class="chart">${g}<div class="tip"></div></div>`;
 const svg=el.querySelector("svg"),tip=el.querySelector(".tip"),maps=series.map(s=>new Map(s.data));
 svg.addEventListener("mousemove",e=>{const r=svg.getBoundingClientRect(),x=(e.clientX-r.left)*W/r.width;
  let i=Math.round((x-m.l)/(W-m.l-m.r)*(dates.length-1));i=Math.max(0,Math.min(dates.length-1,i));const d=dates[i],xh=svg.querySelector(".xh");
  xh.setAttribute("x1",X(i));xh.setAttribute("x2",X(i));xh.setAttribute("visibility","visible");let h=`<b>${fmtD(d)}</b>`;
  series.forEach((s,j)=>{const v=maps[j].get(d),c=svg.querySelector(".d"+j);if(v!=null){c.setAttribute("cx",X(i));c.setAttribute("cy",Y(v));c.setAttribute("visibility","visible");
   h+=`<br><i style="display:inline-block;width:8px;height:8px;border-radius:2px;background:${css(s.color)}"></i> ${esc(s.name)}: ${fmt(v)}`;}else c.setAttribute("visibility","hidden");});
  tip.innerHTML=h;tip.style.display="block";const px=X(i)*r.width/W;tip.style.left=Math.min(px+14,r.width-tip.offsetWidth-4)+"px";tip.style.top="8px";});
 svg.addEventListener("mouseleave",()=>{tip.style.display="none";svg.querySelectorAll("circle,.xh").forEach(n=>n.setAttribute("visibility","hidden"));});
}
const bars=rows=>rows.map(([k,v])=>`<div class="bar" title="${esc(k)}: ${(v*100).toFixed(1)}%"><span>${esc(k)}</span><div class="t"><div class="f" style="width:${Math.max(0,v*100)}%"></div></div><span class="p">${(v*100).toFixed(1)}%</span></div>`).join("");

/* ---------- pages ---------- */
function pcard(p){const k=p.kpi,cashPct=k.equity?k.cash/k.equity:0;
 return `<div class="card pcard ${isRisk(p)?"risk":""}"><div class="top"><span class="label">${esc(pname(p.name))}</span><span class="small">Session ${p.revision}</span></div>
 <div class="klabel">Portfolio value</div><div class="big">${nok(k.equity)}</div>
 <span class="badge ${cls(k.ret_total)}">${pct(k.ret_total)}</span><span class="since">${p.started?"since "+fmtTs(p.started):"since start"}</span>
 ${k.fx_valuation!=="current"&&Math.abs(k.fx_not_counted||0)>=1?`<div class="since" style="margin-top:8px">At today's exchange rates: ${nok(k.equity+k.fx_not_counted)} <span class="muted">(currency effect ${k.fx_not_counted>=0?"+":""}${f0(k.fx_not_counted)}, counted only when converted back)</span></div>`:""}
 <div class="kv"><div><div class="k">Cash</div><div class="v">${nok(k.cash)}</div></div><div><div class="k">Holdings</div><div class="v">${k.positions}</div></div><div><div class="k">Costs to date</div><div class="v">${nok(k.costs)}</div></div></div>
 <div class="meter"><div style="width:${(cashPct*100).toFixed(1)}%"></div></div>
 <div class="foot"><span>${(cashPct*100).toFixed(1)}% cash</span><span>Last report ${fmtTs(p.last_report)}</span></div></div>`;}
function holdingsTable(ps){const rows=ps.flatMap(p=>p.holdings.map(h=>({...h,book:p.name})));
 if(!rows.length)return `<p class="empty">No open positions yet.</p>`;
 return `<div class="tw"><table><thead><tr><th>Company / listing</th><th>Book</th><th class="n">Shares</th><th class="n">Return</th><th class="n">Value · NOK</th><th class="n" title="Value if converted at today's exchange rate (information only)">At today's FX · NOK</th><th>Mark as of</th></tr></thead><tbody>`+
 rows.sort((a,b)=>order(a.book)-order(b.book)||b.value_nok-a.value_nok).map(h=>`<tr><td class="co"><b>${esc(h.name)}</b><small>${esc(h.listing)} · ${esc(h.currency)}</small></td>
 <td>${esc(pname(h.book))}</td><td class="n">${f0(h.qty)}</td><td class="n ${cls(h.return_local)}">${pct(h.return_local,1)}</td><td class="n">${nok(h.value_nok)}</td><td class="n muted">${h.currency==="NOK"?"–":nok(h.value_nok+(h.fx_effect_not_counted||0))}</td><td>${mark(h.marked)}</td></tr>`).join("")+`</tbody></table></div>`;}
function overview(){const ps=shown();
 let h=`<div class="cards">${ps.map(pcard).join("")}</div>`;
 h+=`<div class="card"><div class="cardhead"><h2>Current holdings</h2><button class="btn" style="font-size:12px;padding:7px 12px" onclick="document.getElementById('hist').scrollIntoView({behavior:'smooth'})">Continuing history</button></div>${holdingsTable(ps)}</div>`;
 h+=`<div class="card" id="hist" style="margin-top:24px"><div class="cardhead"><div><h2>Value over time</h2><p class="sub">NOK after all costs. ${ps.length>1?"Both portfolios indexed to 100 at start, against a global index fund (ACWI).":"Compared with the same start amount in ACWI and the S&amp;P 500, bought on day one."}</p></div></div><div id="navc"></div></div>`;
 if(ps.length===1){const p=ps[0],k=p.kpi;
  h+=`<div class="grid2"><div class="card"><h2 style="font-size:20px;margin-bottom:12px">Allocation by market</h2>${bars(p.alloc.market)}</div><div class="card"><h2 style="font-size:20px;margin-bottom:12px">Allocation by sector</h2>${bars(p.alloc.sector)}</div></div>
  <div class="grid2"><div class="card"><h2 style="font-size:20px;margin-bottom:12px">Allocation by region</h2>${bars(p.alloc.region)}</div><div class="card"><h2 style="font-size:20px;margin-bottom:12px">Profit and loss</h2><table><tbody>
  <tr><td>Open positions, at purchase FX</td><td class="n ${cls(k.unrealized)}">${nok(k.unrealized)}</td></tr>
  <tr><td>Realised on price</td><td class="n ${cls(k.realized_price)}">${nok(k.realized_price)}</td></tr>
  <tr><td>Realised on currency (when converted back to NOK)</td><td class="n ${cls(k.realized_fx)}">${nok(k.realized_fx)}</td></tr>
  <tr><td>Trading costs paid (included above)</td><td class="n">${nok(k.costs)}</td></tr>
  <tr><td class="muted">Currency effect on open positions (not counted, by rule)</td><td class="n muted">${nok(k.fx_not_counted)}</td></tr></tbody></table></div></div>`;}
 $("#main").innerHTML=head()+h;wireSeg();
 if(ps.length>1){const s=ps.map(p=>({name:pname(p.name),color:isRisk(p)?"--s-risk":"--s-main",data:p.nav.map(d=>[d[0],d[1]/p.start*100])}));
  const b=ps[0].bench.ACWI;if(b)s.push({name:"ACWI",color:"--s-bench",data:b.map(d=>[d[0],d[1]/ps[0].start*100])});
  lineChart($("#navc"),s,{fmt:v=>f0(v),ref:100,label:"Portfolios indexed"});}
 else{const p=ps[0],s=[{name:pname(p.name),color:isRisk(p)?"--s-risk":"--s-main",data:p.nav}];
  if(p.bench.ACWI)s.push({name:"ACWI",color:"--s-bench",data:p.bench.ACWI});if(p.bench.SPY)s.push({name:"S&P 500",color:"--s-spy",data:p.bench.SPY});
  lineChart($("#navc"),s,{fmt:v=>f0(v/1000)+"k",ref:p.start,label:"Portfolio value"});}
}
function caseHtml(c){let h="";Object.entries(c.agents||{}).forEach(([a,v])=>{h+=`<div class="agent"><b>${esc(a[0].toUpperCase()+a.slice(1))}</b> <span class="muted">stance ${v.stance>=0?"+":""}${v.stance.toFixed(2)} · confidence ${v.confidence.toFixed(2)}</span><br>${v.pros.map(x=>`<span class="pro">+ ${esc(x)}</span>`).join("<br>")}${v.pros.length&&v.cons.length?"<br>":""}${v.cons.map(x=>`<span class="con">− ${esc(x)}</span>`).join("<br>")}</div>`;});
 if((c.conflicts||[]).length)h+=`<div class="agent"><b>Disagreement:</b> ${c.conflicts.map(esc).join("; ")} → position cut by 25%</div>`;
 if(c.llm_review)h+=`<div class="agent"><b>Reviewer:</b> ${esc(c.llm_review.verdict)} — ${esc(c.llm_review.reason)}</div>`;return h;}
function opportunities(){const ps=shown();let h="";
 ps.forEach(p=>{const R=(p.research||[]).filter(r=>r.status!=="closed");if(!R.length){h+=`<div class="card" style="margin-bottom:22px"><h2>${esc(pname(p.name))}</h2><p class="empty">No sessions yet.</p></div>`;return;}
  const day=R[0].date,rs=R.filter(r=>r.date===day);
  const ideas=rs.flatMap(r=>(r.ideas||[]).map(i=>({...i,session:r.session_label.split(" (")[0]}))).sort((a,b)=>b.score-a.score);
  h+=`<div class="card" style="margin-bottom:22px"><div class="cardhead"><div><h2>${esc(pname(p.name))}</h2><p class="sub">Top ideas from ${fmtD(day)} · ${rs.length} session${rs.length>1?"s":""}</p></div></div><div class="tw"><table><thead><tr><th>Company</th><th>Session</th><th class="n">Team score</th><th>Outcome</th></tr></thead><tbody>`+
  ideas.map(i=>`<tr><td class="co"><details><summary><b>${esc(i.name)}</b> <span class="muted">${esc(i.symbol)}</span></summary><p class="small" style="margin:8px 0 0 16px">${esc(i.summary)}</p>${caseHtml(i.case||{})}</details></td><td>${esc(i.session)}</td><td class="n">${i.score>=0?"+":""}${i.score.toFixed(2)}</td><td><span class="chip ${/^BOUGHT/.test(i.verdict)?"ok":"no"}">${esc(i.verdict.replace(/^not bought: /,"passed: "))}</span></td></tr>`).join("")+`</tbody></table></div></div>`;});
 $("#main").innerHTML=head()+h;wireSeg();}
function research(){const ps=shown();const R=ps.flatMap(p=>(p.research||[]).map(r=>({...r,book:p.name})));
 if(!R.length){$("#main").innerHTML=head()+`<div class="card"><p class="empty">Research notes appear after each session (Nordic, Europe, US).</p></div>`;wireSeg();return;}
 const days=[...new Set(R.map(r=>r.date))].sort().reverse();
 $("#main").innerHTML=head()+`<div style="margin-bottom:18px"><label class="small" for="day">Day&nbsp;</label><select id="day">${days.map(d=>`<option value="${d}">${fmtD(d)}</option>`).join("")}</select></div><div id="notes"></div>`;wireSeg();
 const show=d=>{$("#notes").innerHTML=R.filter(r=>r.date===d).sort((a,b)=>(a.time||"").localeCompare(b.time||"")||a.book.localeCompare(b.book)).map(r=>{
  if(r.status==="closed")return `<div class="card" style="margin-bottom:22px"><span class="label">${esc(pname(r.book))}</span><h2 style="margin-top:8px">${esc(r.session_label)}</h2><p class="empty">Market closed (holiday): no analysis.</p></div>`;
  let s=`<div class="card note" style="margin-bottom:22px;border-top:3px solid var(${isRisk({name:r.book})?"--risk":"--main"})"><span class="label">${esc(pname(r.book))}</span><h2 style="margin:8px 0 4px">${esc(r.session_label)}</h2><p class="sub" style="margin-bottom:16px">${fmtD(r.date)} at ${esc(r.time)} Oslo time · ${r.analysed} stocks analysed · portfolio ${nok(r.equity)}</p>`;
  if(r.brief)s+=`<div class="brief"><p><b>Analyst note.</b> ${esc(r.brief)}</p></div>`;
  s+=(r.text||[]).map(t=>`<p>${esc(t)}</p>`).join("");
  if(Object.keys(r.by_market||{}).length)s+=`<div class="tw" style="margin-top:8px"><table><thead><tr><th>Market</th><th class="n">Stocks</th><th class="n">In uptrend</th><th class="n">Avg 3-month move</th></tr></thead><tbody>`+Object.entries(r.by_market).map(([m,v])=>`<tr><td>${esc(m)}</td><td class="n">${v.n}</td><td class="n">${(v.above_200d*100).toFixed(0)}%</td><td class="n ${cls(v.avg_3m)}">${pct(v.avg_3m,1)}</td></tr>`).join("")+`</tbody></table></div>`;
  return s+`</div>`;}).join("");};
 $("#day").onchange=e=>show(e.target.value);show(days[0]);}
function trades(){const ps=shown();
 const closed=ps.flatMap(p=>(p.realized||[]).map(r=>({...r,book:p.name}))).sort((a,b)=>b.date.localeCompare(a.date));
 const orders=ps.flatMap(p=>(p.orders||[]).map(o=>({...o,book:p.name}))).sort((a,b)=>b.date.localeCompare(a.date));
 let h=`<div class="card"><div class="cardhead"><div><h2>Closed positions</h2><p class="sub">Result split into price and currency effects.</p></div></div>`+(closed.length?`<div class="tw"><table><thead><tr><th>Sold</th><th>Company</th><th>Book</th><th class="n">Shares</th><th class="n">Return</th><th class="n">Price P/L · NOK</th><th class="n">FX P/L · NOK</th><th>Why sold</th></tr></thead><tbody>`+
 closed.slice(0,200).map(r=>`<tr><td>${fmtD(r.date)}</td><td class="co">${co(r.symbol)}</td><td>${esc(pname(r.book))}</td><td class="n">${f0(r.qty)}</td><td class="n ${cls(r.return_pct)}">${pct(r.return_pct,1)}</td><td class="n ${cls(r.price_pnl_nok)}">${f2(r.price_pnl_nok)}</td><td class="n ${cls(r.fx_pnl_nok)}">${f2(r.fx_pnl_nok)}</td><td class="small">${esc(r.reason)}</td></tr>`).join("")+`</tbody></table></div>`:`<p class="empty">No positions closed yet.</p>`)+`</div>`;
 h+=`<div class="card" style="margin-top:24px"><div class="cardhead"><h2>All orders</h2></div>`+(orders.length?`<div class="tw"><table><thead><tr><th>Date</th><th>Book</th><th>Side</th><th>Company</th><th class="n">Shares</th><th class="n">Price</th><th class="n">Value · NOK</th><th>Status</th><th>Reason · broker notes</th></tr></thead><tbody>`+
 orders.slice(0,300).map(o=>`<tr><td>${fmtD(o.date)}</td><td>${esc(pname(o.book))}</td><td class="${o.side==="buy"?"up":"down"}">${esc(o.side)}</td><td class="co">${co(o.symbol)}</td><td class="n">${f0(o.qty)}</td><td class="n">${f2(o.limit)} <span class="muted">${esc(o.currency)}</span></td><td class="n">${f2(o.value_nok)}</td><td><span class="chip">${esc(o.status)}</span></td><td class="small">${esc(o.reason)}${o.notes?` <span class="muted">[${esc(o.notes)}]</span>`:""}</td></tr>`).join("")+`</tbody></table></div>`:`<p class="empty">No orders yet.</p>`)+`</div>`;
 $("#main").innerHTML=head()+h;wireSeg();}
function operations(){const ps=shown();
 let h=`<div class="grid2" style="margin-top:0"><div class="card"><h2 style="font-size:20px;margin-bottom:12px">Daily schedule</h2><table><tbody>${D.schedule.map(r=>{const [t,...rest]=r.split("  ");return `<tr><td style="white-space:nowrap">${esc(t)}</td><td>${esc(rest.join(" ").trim())}</td></tr>`;}).join("")}</tbody></table><p class="sub">Runs on GitHub Actions on weekdays. Holidays are skipped automatically. Dashboard generated ${fmtTs(D.generated)}.</p></div>
 <div class="card"><h2 style="font-size:20px;margin-bottom:12px">Rules</h2><table><tbody>
 <tr><td>One buy</td><td>never more than 25% of the portfolio (cash + holdings, NOK, costs included)</td></tr>
 <tr><td>Positions</td><td>max 25% each · no margin · no short selling · no same-day round trips</td></tr>
 <tr><td>Currency</td><td>valued at the purchase-day rate; FX gain/loss counts when converted back to NOK</td></tr>
 <tr><td>Costs booked</td><td>slippage, commission, fees on sales, 0.25% FX conversion</td></tr></tbody></table></div></div>`;
 ps.forEach(p=>{const ics=Object.entries(p.ics||{}),cb=p.kpi.cost_breakdown||{};
  h+=`<div class="card" style="margin-top:24px;border-top:3px solid var(${isRisk(p)?"--risk":"--main"})"><div class="cardhead"><div><span class="label">${esc(pname(p.name))}</span><h2 style="margin-top:8px">How much say each analyst has</h2><p class="sub">Learned from each agent's track record: did the stocks it liked beat the ones it disliked 20 trading days later? Bounded between 10% and 60%.</p></div></div><div id="w_${p.name}"></div>
  <div class="grid2"><div><h2 style="font-size:18px;margin-bottom:8px">Track record</h2>${ics.length?`<table><thead><tr><th>Agent</th><th class="n">Average skill (IC)</th><th class="n">Evaluations</th></tr></thead><tbody>${ics.map(([a,[v,n]])=>`<tr><td>${esc(a)}</td><td class="n ${cls(v)}">${v>=0?"+":""}${v.toFixed(3)}</td><td class="n">${n}</td></tr>`).join("")}</tbody></table>`:`<p class="empty">Builds up after the first 20 trading days.</p>`}</div>
  <div><h2 style="font-size:18px;margin-bottom:8px">Costs by type</h2><table><tbody>${Object.entries(cb).map(([k,v])=>`<tr><td>${esc(k)}</td><td class="n">${nok(v)}</td></tr>`).join("")||`<tr><td class="empty">None yet</td></tr>`}</tbody></table></div></div>
  <h2 style="font-size:18px;margin:22px 0 8px">Recent sessions</h2>${(p.runs||[]).length?`<div class="tw"><table><thead><tr><th>Time</th><th>Broker / session</th><th>Review</th><th>Note</th></tr></thead><tbody>${p.runs.slice(0,30).map(r=>`<tr><td>${fmtTs(r.ts)}</td><td>${esc(r.mode)}</td><td>${r.full?"full":"risk check"}</td><td class="small">${esc(r.note)}</td></tr>`).join("")}</tbody></table></div>`:`<p class="empty">No live sessions recorded${D.mode!=="live"?" (backtest data)":""}.</p>`}</div>`;});
 $("#main").innerHTML=head()+h;wireSeg();
 ps.forEach(p=>{const W=p.weights||[],names=W.length?Object.keys(W.at(-1)[1]):[],cols=["--s-main","--s-risk","--s-bench"];
  lineChart(document.getElementById("w_"+p.name),names.map((a,i)=>({name:a,color:cols[i%3],data:W.map(([d,w])=>[d,w[a]])})),{fmt:v=>(v*100).toFixed(0)+"%",h:220,label:"Agent weights"});});
}
if(D.sidebar_image){const e=document.getElementById("emblem");e.style.padding="0";e.style.background="none";
 e.innerHTML=`<img src="${esc(D.sidebar_image)}" alt="" style="width:100%;border-radius:6px;display:block">`;}
function render(){shell();({Overview:overview,Opportunities:opportunities,Research:research,Trades:trades,Operations:operations})[page]();}
render();let rt;window.addEventListener("resize",()=>{clearTimeout(rt);rt=setTimeout(render,150);});
</script></body></html>"""
