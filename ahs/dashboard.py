"""The dashboard: one self-contained HTML page (no internet needed to view it),
regenerated after every session. Tabs: Overview, Holdings, Trades, Research, Agents.

    python main.py dashboard          -> reports/dashboard.html
    python main.py serve              -> http://localhost:8050 (also reachable from your phone on the same Wi-Fi)
"""
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path

import pandas as pd

from .markets import MARKETS, schedule_table


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
    weights = [(r[0][:10], json.loads(r[1])) for r in
               ledger.q("SELECT ts, weights FROM runs WHERE portfolio=? ORDER BY id", (name,))]
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
    return _pack(name, prof.broker, start, equity, cash, holdings, [[d, e] for d, e, _ in nav], bench,
                 realized, [dict(zip(("date", "symbol", "side", "qty", "limit", "value_nok", "currency", "status",
                                      "kind", "reason", "notes"), o)) for o in orders],
                 ledger.research(name, 30), _dedupe_weights(weights), ics, equity - prev, hv.get("costs", {}))


def _dedupe_weights(rows):
    out, last = [], None
    for d, w in rows:
        if last and last[0] == d:
            out[-1] = [d, w]
        else:
            out.append([d, w])
        last = (d, w)
    return out


def _pack(name, broker, start, equity, cash, holdings, nav, bench, realized, orders, research, weights, ics,
          day_change, costs):
    unreal = sum(h["value_nok"] - h["qty"] * h["entry_price"] * h["entry_fx"] for h in holdings)
    return _clean({
        "name": name, "broker": broker, "start": start,
        "kpi": {"equity": equity, "cash": cash, "positions": len(holdings), "ret_total": equity / start - 1,
                "day_change": day_change, "unrealized": unreal,
                "realized_price": sum(r.get("price_pnl_nok", 0) for r in realized),
                "realized_fx": sum(r.get("fx_pnl_nok", 0) for r in realized),
                "fx_not_counted": sum(h.get("fx_effect_not_counted", 0) for h in holdings),
                "costs": sum(costs.values()) if costs else 0, "cost_breakdown": costs},
        "nav": nav, "bench": bench, "holdings": sorted(holdings, key=lambda h: -h["value_nok"]),
        "alloc": _alloc(holdings, equity, cash), "realized": realized[::-1][:300], "orders": orders,
        "research": research, "weights": weights, "ics": ics})


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
                         "sector": universe.at[s, "sector"]})
    bench = {c: [[str(d.date()), v] for d, v in res.bench[c].items()] for c in res.bench.columns}
    orders = [{"date": f["date"], "symbol": f["symbol"], "side": f["side"], "qty": f["qty"], "limit": f["price"],
               "value_nok": f["value_nok"], "currency": f["currency"], "status": "filled",
               "kind": "", "reason": f.get("reason", ""), "notes": ""} for f in res.book.fills[::-1][:300]]
    weights = [[str(d.date()), r.to_dict()] for d, r in res.weights.iterrows()]
    eq = res.nav.equity
    return _pack(res.portfolio, "backtest", cfg.portfolios[res.portfolio].start_capital_nok, float(eq.iloc[-1]),
                 float(res.nav.cash.iloc[-1]), holdings, nav, bench, res.book.realized, orders, research, weights,
                 res.ics, float(eq.iloc[-1] - eq.iloc[-2]), res.book.costs) | {"updated": str(last.date())}


def write_dashboard(portfolios: list[dict], out: Path, mode: str = "live", note: str = "") -> Path:
    data = {"generated": datetime.now().strftime("%Y-%m-%d %H:%M"), "mode": mode, "note": note,
            "schedule": schedule_table(), "portfolios": portfolios}
    html = TEMPLATE.replace("__DATA__", json.dumps(_clean(data)).replace("</", "<\\/"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    return out


TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="300">
<title>Atom Heart Stocker</title>
<style>
.viz-root{color-scheme:light;--surface-0:#f4f3f0;--surface-1:#fcfcfb;--line:#e4e2dc;--grid:#ecebe7;
 --text-primary:#0b0b0b;--text-secondary:#52514e;--text-muted:#8a8984;
 --series-1:#2a78d6;--series-2:#eb6834;--series-3:#1baf7a;--good:#0b7d0b;--critical:#c43333;--accent:#2a78d6}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])) .viz-root{color-scheme:dark;
 --surface-0:#121211;--surface-1:#1a1a19;--line:#2f2f2c;--grid:#262624;--text-primary:#f0efec;--text-secondary:#c3c2b7;
 --text-muted:#8f8e86;--series-1:#3987e5;--series-2:#d95926;--series-3:#199e70;--good:#2fbf2f;--critical:#e66767;--accent:#3987e5}}
*{box-sizing:border-box}body{margin:0}
.viz-root{font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;background:var(--surface-0);color:var(--text-primary);min-height:100vh}
header{display:flex;flex-wrap:wrap;gap:12px 24px;align-items:center;justify-content:space-between;padding:16px 24px;background:var(--surface-1);border-bottom:1px solid var(--line)}
h1{font-size:18px;margin:0;font-weight:650;letter-spacing:-.01em}h1 small{font-weight:400;color:var(--text-muted);font-size:12px;margin-left:8px}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:8px;overflow:hidden}
.seg button{border:0;background:transparent;color:var(--text-secondary);padding:6px 14px;font:inherit;cursor:pointer}
.seg button[aria-pressed=true]{background:var(--accent);color:#fff}
nav.tabs{display:flex;gap:4px;padding:0 24px;background:var(--surface-1);border-bottom:1px solid var(--line);overflow-x:auto}
nav.tabs button{border:0;background:none;padding:12px 14px;font:inherit;color:var(--text-secondary);cursor:pointer;border-bottom:2px solid transparent;white-space:nowrap}
nav.tabs button[aria-selected=true]{color:var(--text-primary);border-bottom-color:var(--accent);font-weight:600}
main{max-width:1200px;margin:0 auto;padding:20px 24px 48px}
.banner{background:var(--surface-1);border:1px solid var(--line);border-left:3px solid var(--series-2);padding:10px 14px;border-radius:6px;margin-bottom:16px;color:var(--text-secondary)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-bottom:16px}
.tile{background:var(--surface-1);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.tile .l{color:var(--text-secondary);font-size:12px}.tile .v{font-size:24px;font-weight:650;font-variant-numeric:tabular-nums;margin-top:2px}
.tile .s{font-size:12px;color:var(--text-muted);margin-top:2px}
.card{background:var(--surface-1);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:16px}
.card h2{font-size:15px;margin:0 0 4px}.card .sub{color:var(--text-muted);font-size:12px;margin:0 0 10px}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px}
.pos{color:var(--good)}.neg{color:var(--critical)}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th{font-weight:600;color:var(--text-secondary);font-size:12px;text-align:right;padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:7px 8px;border-bottom:1px solid var(--grid);text-align:right}
th:first-child,td:first-child,th.l,td.l{text-align:left}.tablewrap{overflow-x:auto}
.muted{color:var(--text-muted)}.tag{display:inline-block;font-size:11px;padding:1px 7px;border-radius:10px;border:1px solid var(--line);color:var(--text-secondary)}
.bar{display:grid;grid-template-columns:150px 1fr 56px;gap:8px;align-items:center;margin:5px 0;font-size:13px}
.bar .track{height:10px;background:var(--grid);border-radius:2px;overflow:hidden}.bar .fill{height:100%;background:var(--series-1);border-radius:0 4px 4px 0}
.bar .pct{text-align:right;font-variant-numeric:tabular-nums;color:var(--text-secondary)}
.chart{position:relative}.chart svg{display:block;width:100%;height:auto}
.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:var(--text-secondary);margin:4px 0 6px}
.legend i{display:inline-block;width:14px;height:3px;border-radius:2px;vertical-align:middle;margin-right:6px}
.tip{position:absolute;pointer-events:none;background:var(--surface-1);border:1px solid var(--line);border-radius:6px;padding:6px 9px;font-size:12px;box-shadow:0 2px 8px rgba(0,0,0,.12);display:none;white-space:nowrap;z-index:5}
.note p{margin:0 0 10px;max-width:80ch}.brief{border-left:3px solid var(--series-1);padding-left:12px;margin-bottom:12px}
details{border-top:1px solid var(--grid);padding:8px 0}summary{cursor:pointer}
.pro{color:var(--good)}.con{color:var(--critical)}
.agent{margin:6px 0 8px 14px}.agent b{font-weight:600}
.daypick{display:flex;gap:8px;align-items:center;margin-bottom:12px;flex-wrap:wrap}
select{font:inherit;padding:5px 8px;border-radius:6px;border:1px solid var(--line);background:var(--surface-1);color:var(--text-primary)}
footer{color:var(--text-muted);font-size:12px;text-align:center;padding:20px}
</style></head>
<body><div class="viz-root">
<header><h1>Atom Heart Stocker<small id="gen"></small></h1><div class="seg" id="pfsel" role="group" aria-label="Portfolio"></div></header>
<nav class="tabs" role="tablist" id="tabs"></nav>
<main id="main"></main>
<footer id="foot"></footer>
</div>
<script>
const D = __DATA__;
const TABS = ["Overview","Holdings","Trades","Research","Agents"];
let pf = 0, tab = "Overview";
const $ = s => document.querySelector(s);
const nok = v => v==null?"–":"NOK "+Math.round(v).toLocaleString("nb-NO");
const num = (v,d=0) => v==null?"–":Number(v).toLocaleString("nb-NO",{minimumFractionDigits:d,maximumFractionDigits:d});
const pct = (v,d=1) => v==null?"–":(v>=0?"+":"−")+Math.abs(v*100).toFixed(d)+"%";
const sgn = v => v==null?"":(v>=0?"pos":"neg");
const signed = (v,f) => `<span class="${sgn(v)}">${v>=0?"▲":"▼"} ${f(v)}</span>`;
const esc = s => String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const css = n => getComputedStyle(document.querySelector(".viz-root")).getPropertyValue(n).trim();

function header(){
  $("#gen").textContent = (D.mode==="demo"?"DEMO · ":"")+"updated "+D.generated;
  $("#pfsel").innerHTML = D.portfolios.map((p,i)=>`<button aria-pressed="${i===pf}" data-i="${i}">${esc(p.name.replace("_"," "))}</button>`).join("");
  $("#pfsel").onclick = e=>{ if(e.target.dataset.i){pf=+e.target.dataset.i;render();} };
  $("#tabs").innerHTML = TABS.map(t=>`<button role="tab" aria-selected="${t===tab}" data-t="${t}">${t}</button>`).join("");
  $("#tabs").onclick = e=>{ if(e.target.dataset.t){tab=e.target.dataset.t;render();} };
  $("#foot").innerHTML = "Daily sessions: "+D.schedule.map(esc).join(" · ")+"<br>Values in NOK. Foreign positions are valued at the exchange rate on the day they were bought; currency gains or losses count only when sale proceeds are converted back to NOK.";
}

/* ---------- line chart: thin lines, crosshair + tooltip, legend + end labels ---------- */
function lineChart(el, series, opts={}){
  const W=el.clientWidth||900, H=opts.h||300, m={l:72,r:opts.endLabels===false?16:96,t:12,b:28};
  const all=series.flatMap(s=>s.data.map(d=>d[1])).filter(v=>v!=null);
  if(!all.length){el.innerHTML='<p class="muted">No history yet — the chart fills in after the first sessions.</p>';return;}
  const dates=[...new Set(series.flatMap(s=>s.data.map(d=>d[0])))].sort();
  const xi=new Map(dates.map((d,i)=>[d,i]));
  let lo=Math.min(...all),hi=Math.max(...all);if(opts.zero)lo=Math.min(0,lo);const pad=(hi-lo)*.06||1;lo-=pad;hi+=pad;
  const X=i=>m.l+(W-m.l-m.r)*(dates.length>1?i/(dates.length-1):.5), Y=v=>m.t+(H-m.t-m.b)*(1-(v-lo)/(hi-lo));
  const fmt=opts.fmt||(v=>num(v));
  let g=`<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(opts.label||"chart")}">`;
  for(let k=0;k<=4;k++){const v=lo+(hi-lo)*k/4;g+=`<line x1="${m.l}" x2="${W-m.r}" y1="${Y(v)}" y2="${Y(v)}" stroke="${css("--grid")}"/><text x="${m.l-8}" y="${Y(v)+4}" font-size="11" text-anchor="end" fill="${css("--text-muted")}">${fmt(v)}</text>`;}
  const ticks=Math.min(6,dates.length);for(let k=0;k<ticks;k++){const i=Math.round(k*(dates.length-1)/Math.max(ticks-1,1));g+=`<text x="${X(i)}" y="${H-8}" font-size="11" text-anchor="middle" fill="${css("--text-muted")}">${dates[i]}</text>`;}
  if(opts.ref!=null) g+=`<line x1="${m.l}" x2="${W-m.r}" y1="${Y(opts.ref)}" y2="${Y(opts.ref)}" stroke="${css("--text-muted")}" stroke-dasharray="3 4"/>`;
  series.forEach(s=>{const pts=s.data.filter(d=>d[1]!=null).map(d=>`${X(xi.get(d[0])).toFixed(1)},${Y(d[1]).toFixed(1)}`).join(" ");
    g+=`<polyline fill="none" stroke="${css(s.color)}" stroke-width="2" stroke-linejoin="round" points="${pts}"/>`;
    if(opts.endLabels!==false){const L=s.data.filter(d=>d[1]!=null).at(-1);if(L)g+=`<text x="${X(xi.get(L[0]))+6}" y="${Y(L[1])+4}" font-size="11" fill="${css("--text-secondary")}">${esc(s.name)}</text>`;}});
  g+=`<line id="xh" y1="${m.t}" y2="${H-m.b}" stroke="${css("--text-muted")}" stroke-width="1" visibility="hidden"/>`;
  series.forEach((s,j)=>g+=`<circle id="dot${j}" r="4" fill="${css(s.color)}" stroke="${css("--surface-1")}" stroke-width="2" visibility="hidden"/>`);
  g+=`<rect x="${m.l}" y="${m.t}" width="${W-m.l-m.r}" height="${H-m.t-m.b}" fill="transparent"/></svg>`;
  const leg=series.length>1?`<div class="legend">${series.map(s=>`<span><i style="background:${css(s.color)}"></i>${esc(s.name)}</span>`).join("")}</div>`:"";
  el.innerHTML=leg+`<div class="chart">${g}<div class="tip"></div></div>`;
  const svg=el.querySelector("svg"),tip=el.querySelector(".tip"),maps=series.map(s=>new Map(s.data));
  svg.addEventListener("mousemove",e=>{const r=svg.getBoundingClientRect(),x=(e.clientX-r.left)*W/r.width;
    let i=Math.round((x-m.l)/(W-m.l-m.r)*(dates.length-1));i=Math.max(0,Math.min(dates.length-1,i));const d=dates[i];
    const xh=svg.querySelector("#xh");xh.setAttribute("x1",X(i));xh.setAttribute("x2",X(i));xh.setAttribute("visibility","visible");
    let h=`<b>${d}</b>`;series.forEach((s,j)=>{const v=maps[j].get(d),c=svg.querySelector("#dot"+j);
      if(v!=null){c.setAttribute("cx",X(i));c.setAttribute("cy",Y(v));c.setAttribute("visibility","visible");h+=`<br><i style="display:inline-block;width:8px;height:8px;border-radius:2px;background:${css(s.color)}"></i> ${esc(s.name)}: ${fmt(v)}`;}
      else c.setAttribute("visibility","hidden");});
    tip.innerHTML=h;tip.style.display="block";const px=X(i)*r.width/W;tip.style.left=Math.min(px+12,r.width-tip.offsetWidth-4)+"px";tip.style.top="8px";});
  svg.addEventListener("mouseleave",()=>{tip.style.display="none";svg.querySelectorAll("circle,#xh").forEach(n=>n.setAttribute("visibility","hidden"));});
}
function bars(rows){return rows.map(([k,v])=>`<div class="bar" title="${esc(k)}: ${(v*100).toFixed(1)}%"><span>${esc(k)}</span><div class="track"><div class="fill" style="width:${Math.max(0,v*100)}%"></div></div><span class="pct">${(v*100).toFixed(1)}%</span></div>`).join("");}

/* ---------- tabs ---------- */
function overview(P){
  const k=P.kpi;
  let h = D.note?`<div class="banner">${esc(D.note)}</div>`:"";
  h+=`<div class="kpis">
   <div class="tile"><div class="l">Portfolio value</div><div class="v">${nok(k.equity)}</div><div class="s">start ${nok(P.start)}</div></div>
   <div class="tile"><div class="l">Return since start</div><div class="v">${signed(k.ret_total,v=>pct(v))}</div><div class="s">${nok(k.equity-P.start)}</div></div>
   <div class="tile"><div class="l">Change since last session</div><div class="v">${signed(k.day_change,nok)}</div><div class="s">&nbsp;</div></div>
   <div class="tile"><div class="l">Cash</div><div class="v">${nok(k.cash)}</div><div class="s">${(k.cash/k.equity*100).toFixed(1)}% of portfolio</div></div>
   <div class="tile"><div class="l">Positions</div><div class="v">${k.positions}</div><div class="s">broker: ${esc(P.broker)}</div></div></div>`;
  h+=`<div class="card"><h2>Value over time</h2><p class="sub">NOK, net of all trading costs. Comparison: the same start amount in a global index fund (ACWI) and the S&amp;P 500 (SPY), bought on day one.</p><div id="navc"></div></div>`;
  const other=D.portfolios.length>1?`<div class="card"><h2>Both portfolios</h2><p class="sub">Indexed to 100 at each portfolio's start.</p><div id="both"></div></div>`:"";
  h+=other;
  h+=`<div class="grid2"><div class="card"><h2>Allocation by region</h2>${bars(P.alloc.region)}</div><div class="card"><h2>Allocation by market</h2>${bars(P.alloc.market)}</div></div>`;
  h+=`<div class="grid2"><div class="card"><h2>Allocation by sector</h2>${bars(P.alloc.sector)}</div><div class="card"><h2>Profit and loss</h2><table>
   <tr><td class="l">Unrealised (open positions, at purchase FX)</td><td>${signed(k.unrealized,nok)}</td></tr>
   <tr><td class="l">Realised on price</td><td>${signed(k.realized_price,nok)}</td></tr>
   <tr><td class="l">Realised on currency (at conversion back to NOK)</td><td>${signed(k.realized_fx,nok)}</td></tr>
   <tr><td class="l">Trading costs paid (included above)</td><td>${nok(k.costs)}</td></tr>
   <tr><td class="l muted">Currency effect on open positions (not counted, by rule)</td><td class="muted">${nok(k.fx_not_counted)}</td></tr></table></div></div>`;
  $("#main").innerHTML=h;
  const ser=[{name:P.name.replace("_"," "),color:"--series-1",data:P.nav}];
  if(P.bench.ACWI)ser.push({name:"ACWI",color:"--series-2",data:P.bench.ACWI});
  if(P.bench.SPY)ser.push({name:"SPY",color:"--series-3",data:P.bench.SPY});
  lineChart($("#navc"),ser,{fmt:v=>num(v/1000)+"k",label:"Portfolio value",ref:P.start});
  if(other) lineChart($("#both"),D.portfolios.map((q,i)=>({name:q.name.replace("_"," "),color:"--series-"+(i+1),data:q.nav.map(d=>[d[0],d[1]/q.start*100])})),{fmt:v=>num(v,0),ref:100,label:"Both portfolios indexed"});
}
function holdings(P){
  if(!P.holdings.length){$("#main").innerHTML=`<div class="card"><h2>Holdings</h2><p class="muted">No open positions. Cash: ${nok(P.kpi.cash)}</p></div>`;return;}
  let h=`<div class="card"><h2>Holdings</h2><p class="sub">Value in NOK at the purchase exchange rate. "Return" is the price change in the stock's own currency.</p><div class="tablewrap"><table><tr><th>Name</th><th class="l">Market</th><th>Shares</th><th>Bought at</th><th>Now</th><th>Return</th><th>Value NOK</th><th>Weight</th><th class="l">Since</th></tr>`;
  P.holdings.forEach(x=>{h+=`<tr><td>${esc(x.name)} <span class="muted">${esc(x.symbol)}</span></td><td class="l">${esc(x.market)} <span class="tag">${esc(x.currency)}</span></td><td>${num(x.qty)}</td><td>${num(x.entry_price,2)}</td><td>${num(x.price,2)}</td><td>${signed(x.return_local,v=>pct(v))}</td><td>${num(x.value_nok)}</td><td>${(x.value_nok/P.kpi.equity*100).toFixed(1)}%</td><td class="l">${esc(x.entry_date)}</td></tr>`;});
  h+=`<tr><td><b>Cash</b></td><td></td><td></td><td></td><td></td><td></td><td>${num(P.kpi.cash)}</td><td>${(P.kpi.cash/P.kpi.equity*100).toFixed(1)}%</td><td></td></tr></table></div></div>`;
  $("#main").innerHTML=h;
}
function trades(P){
  let h=`<div class="card"><h2>Closed trades</h2><p class="sub">Result split into price and currency effects.</p><div class="tablewrap"><table><tr><th class="l">Sold</th><th class="l">Name</th><th>Shares</th><th>Return</th><th>Price P/L NOK</th><th>FX P/L NOK</th><th class="l">Held since</th><th class="l">Why sold</th></tr>`;
  (P.realized||[]).slice(0,150).forEach(r=>{h+=`<tr><td class="l">${esc(r.date)}</td><td class="l">${esc(r.symbol)}</td><td>${num(r.qty)}</td><td>${signed(r.return_pct,v=>pct(v))}</td><td>${signed(r.price_pnl_nok,v=>num(v))}</td><td>${signed(r.fx_pnl_nok,v=>num(v))}</td><td class="l">${esc(r.held_since)}</td><td class="l">${esc(r.reason)}</td></tr>`;});
  h+=`</table></div></div><div class="card"><h2>All orders</h2><div class="tablewrap"><table><tr><th class="l">Date</th><th class="l">Side</th><th class="l">Symbol</th><th>Shares</th><th>Price/limit</th><th>Value NOK</th><th class="l">Status</th><th class="l">Reason / broker notes</th></tr>`;
  (P.orders||[]).forEach(o=>{h+=`<tr><td class="l">${esc(o.date)}</td><td class="l">${esc(o.side)}</td><td class="l">${esc(o.symbol)}</td><td>${num(o.qty)}</td><td>${num(o.limit,2)} <span class="muted">${esc(o.currency)}</span></td><td>${num(o.value_nok)}</td><td class="l">${esc(o.status)}</td><td class="l">${esc(o.reason)}${o.notes?` <span class="muted">[${esc(o.notes)}]</span>`:""}</td></tr>`;});
  $("#main").innerHTML=h+`</table></div></div>`;
}
function caseHtml(c){
  let h="";Object.entries(c.agents||{}).forEach(([a,v])=>{h+=`<div class="agent"><b>${esc(a[0].toUpperCase()+a.slice(1))}</b> <span class="muted">stance ${v.stance>=0?"+":""}${v.stance.toFixed(2)}, confidence ${v.confidence.toFixed(2)}</span><br>${v.pros.map(p=>`<span class="pro">+ ${esc(p)}</span>`).join("<br>")}${v.pros.length&&v.cons.length?"<br>":""}${v.cons.map(p=>`<span class="con">− ${esc(p)}</span>`).join("<br>")}</div>`;});
  if((c.conflicts||[]).length)h+=`<div class="agent"><b>Disagreement:</b> ${c.conflicts.map(esc).join("; ")} → position cut by 25%</div>`;
  if(c.llm_review)h+=`<div class="agent"><b>Reviewer (Claude):</b> ${esc(c.llm_review.verdict)} — ${esc(c.llm_review.reason)}</div>`;
  return h;
}
function research(P){
  const R=(P.research||[]);
  if(!R.length){$("#main").innerHTML=`<div class="card"><h2>Daily research</h2><p class="muted">No research yet. Notes appear after each session (Nordic, Europe, US).</p></div>`;return;}
  const days=[...new Set(R.map(r=>r.date))];
  let h=`<div class="daypick"><label for="day">Day</label><select id="day">${days.map(d=>`<option>${d}</option>`).join("")}</select></div><div id="notes"></div>`;
  $("#main").innerHTML=h;
  const show=d=>{let s="";R.filter(r=>r.date===d).sort((a,b)=>(a.time||"").localeCompare(b.time||"")).forEach(r=>{
    if(r.status==="closed"){s+=`<div class="card"><h2>${esc(r.session_label)}</h2><p class="muted">Market closed today (holiday) — no analysis.</p></div>`;return;}
    s+=`<div class="card note"><h2>${esc(r.session_label)}</h2><p class="sub">${esc(r.date)} at ${esc(r.time)} Oslo time · ${r.analysed} stocks analysed · portfolio ${nok(r.equity)}</p>`;
    if(r.brief)s+=`<div class="brief"><p><b>Analyst note.</b> ${esc(r.brief)}</p></div>`;
    s+=(r.text||[]).map(p=>`<p>${esc(p)}</p>`).join("");
    if(Object.keys(r.by_market||{}).length){s+=`<table><tr><th class="l">Market</th><th>Stocks</th><th>In uptrend</th><th>Avg 3-month move</th></tr>`+Object.entries(r.by_market).map(([m,v])=>`<tr><td class="l">${esc(m)}</td><td>${v.n}</td><td>${(v.above_200d*100).toFixed(0)}%</td><td>${pct(v.avg_3m)}</td></tr>`).join("")+`</table>`;}
    s+=`<h3 style="font-size:14px;margin:16px 0 4px">Top ideas and the debate</h3>`;
    (r.ideas||[]).forEach(i=>{s+=`<details><summary><b>${esc(i.name)}</b> <span class="muted">${esc(i.symbol)} · score ${i.score>=0?"+":""}${i.score.toFixed(2)}</span> — ${esc(i.verdict)}</summary><p class="muted" style="margin:6px 0 0 14px">${esc(i.summary)}</p>${caseHtml(i.case||{})}</details>`;});
    s+=`</div>`;});
    $("#notes").innerHTML=s;};
  $("#day").onchange=e=>show(e.target.value);show(days[0]);
}
function agents(P){
  let h=`<div class="card"><h2>How much say each analyst has</h2><p class="sub">Learned from each agent's track record: did the stocks it liked beat the ones it disliked 20 trading days later? Bounded between 10% and 60%.</p><div id="wc"></div></div>`;
  const ics=Object.entries(P.ics||{});
  h+=`<div class="grid2"><div class="card"><h2>Track record</h2>`+(ics.length?`<table><tr><th class="l">Agent</th><th>Average skill (IC)</th><th>Evaluations</th></tr>`+ics.map(([a,[v,n]])=>`<tr><td class="l">${esc(a)}</td><td>${signed(v,x=>x.toFixed(3))}</td><td>${n}</td></tr>`).join("")+`</table><p class="sub">IC above 0 means the agent's rankings were better than chance.</p>`:`<p class="muted">Builds up after the first 20 trading days.</p>`)+`</div>
  <div class="card"><h2>The team and its rules</h2><table>
  <tr><td class="l"><b>Opportunity Screen</b></td><td class="l">trends, momentum, breakouts</td></tr>
  <tr><td class="l"><b>Value</b></td><td class="l">price vs its own 5-year history, quality, fundamentals</td></tr>
  <tr><td class="l"><b>Risk Analysis</b></td><td class="l">volatility, drawdown, vetoes, stops, sizing, correlation</td></tr>
  <tr><td class="l"><b>Portfolio Coordinator</b></td><td class="l">runs the debate, limits per region/sector/position</td></tr>
  <tr><td class="l"><b>Broker</b></td><td class="l">places orders; one buy ≤ 25% of portfolio, no margin, no shorting</td></tr></table></div></div>`;
  $("#main").innerHTML=h;
  const W=P.weights||[];const names=W.length?Object.keys(W.at(-1)[1]):[];
  lineChart($("#wc"),names.map((a,i)=>({name:a,color:"--series-"+(i+1),data:W.map(([d,w])=>[d,w[a]])})),{fmt:v=>(v*100).toFixed(0)+"%",h:240,label:"Agent weights"});
}
function render(){header();const P=D.portfolios[pf];({Overview:overview,Holdings:holdings,Trades:trades,Research:research,Agents:agents})[tab](P);}
render();window.addEventListener("resize",()=>{if(tab==="Overview"||tab==="Agents")render();});
</script></body></html>"""
