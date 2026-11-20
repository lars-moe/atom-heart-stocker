"""Portfolio Coordinator agent: runs the discussion and turns it into trade intents.

For each candidate it gathers every analyst's stance, confidence, pros and cons,
notes where agents disagree (disagreement = uncertainty = smaller position),
applies portfolio-level limits (positions, region, sector, correlation, cash
reserve) and hands intents to the Broker. It never places orders itself.
"""
from __future__ import annotations

import math

import pandas as pd

from ..brokers import Holding  # noqa: F401  (re-exported for convenience)
from ..config import PortfolioProfile
from .base import AnalystAgent, pct
from .broker import TradeIntent
from .risk import RiskAnalysisAgent


class PortfolioCoordinatorAgent:
    name = "coordinator"

    def __init__(self, profile: PortfolioProfile, analysts: list[AnalystAgent],
                 risk: RiskAnalysisAgent, universe: pd.DataFrame):
        self.p, self.analysts, self.risk, self.u = profile, analysts, risk, universe

    # ---------- discussion ----------
    def assess_all(self, snap: pd.DataFrame) -> dict[str, pd.DataFrame]:
        return {a.name: a.assess(snap) for a in self.analysts}

    @staticmethod
    def combined_score(tables: dict[str, pd.DataFrame], weights: dict[str, float]) -> pd.Series:
        parts = [weights.get(n, 0) * t.stance * (0.5 + 0.5 * t.confidence) for n, t in tables.items()]
        return sum(parts) / max(sum(weights.get(n, 0) for n in tables), 1e-9)

    def debate(self, sym: str, snap: pd.DataFrame, tables: dict[str, pd.DataFrame]) -> dict:
        case = {"agents": {}, "conflicts": []}
        for a in self.analysts:
            op = a.opinion(sym, snap, tables[a.name])
            case["agents"][a.name] = {"stance": round(op.stance, 3), "confidence": round(op.confidence, 3),
                                      "pros": op.pros, "cons": op.cons}
        st = {n: v["stance"] for n, v in case["agents"].items()}
        names = list(st)
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                if st[a] * st[b] < 0 and abs(st[a]) > 0.3 and abs(st[b]) > 0.3:
                    case["conflicts"].append(f"{a} ({st[a]:+.2f}) disagrees with {b} ({st[b]:+.2f})")
        return case

    # ---------- decisions ----------
    def decide(self, snap: pd.DataFrame, tables: dict[str, pd.DataFrame], weights: dict[str, float],
               holdings: dict, equity: float, cash: float, returns: pd.DataFrame,
               full_review: bool, session_symbols: set[str] | None = None,
               today: str | None = None) -> tuple[list[TradeIntent], pd.Series]:
        """`snap` must carry a column `fx` (NOK per unit of the symbol's currency).
        `session_symbols` limits exits and entries to the markets of the current session."""
        self.skips: dict[str, str] = {}
        score = self.combined_score(tables, weights)
        veto = tables["risk"].veto if "risk" in tables else pd.Series(False, index=snap.index)
        intents: list[TradeIntent] = []
        exiting: set[str] = set()

        # 1) exits and trims (checked on every run)
        for sym, h in holdings.items():
            if sym not in snap.index or pd.isna(snap.at[sym, "close"]):
                continue
            if session_symbols is not None and sym not in session_symbols:
                continue                       # only act on markets that are open now
            r = snap.loc[sym]
            held_days = (pd.Timestamp(today) - pd.Timestamp(h.entry_date)).days if today else 999
            why = self.risk.stop_triggered(r, h.entry_price, h.high_water)
            if why and not why.startswith("hard stop") and held_days < self.p.min_hold_days:
                why = None                     # anti-churn: only the hard stop applies in the first days
            if not why and held_days >= self.p.min_hold_days and full_review and pd.notna(score.get(sym)) and score[sym] < self.p.sell_score:
                why = f"team score turned negative ({score[sym]:+.2f})"
            if why:
                intents.append(TradeIntent(sym, "sell", h.qty, float(r.close), "exit", why,
                                           float(score.get(sym, 0)), self.debate(sym, snap, tables),
                                           h.currency, float(r.fx), h.market))
                exiting.add(sym)
                continue
            w = h.value_base / equity
            if w > self.p.max_position_pct * 1.35 and h.qty > 0:
                per_share = h.value_base / h.qty
                sell_qty = h.qty - math.floor(self.p.max_position_pct * equity / per_share)
                if sell_qty > 0:
                    intents.append(TradeIntent(sym, "sell", sell_qty, float(r.close), "trim",
                                               f"position grew to {w:.0%}; trimming to {self.p.max_position_pct:.0%}",
                                               float(score.get(sym, 0)), {}, h.currency, float(r.fx), h.market))
        if not full_review:
            return intents, score

        # 2) entries
        kept = [s for s in holdings if s not in exiting]
        slots = self.p.max_positions - len(kept)
        if slots <= 0:
            self.skips["*"] = f"portfolio full ({self.p.max_positions} positions)"
            return intents, score
        proceeds = sum(i.qty * i.ref_price * i.fx for i in intents if i.side == "sell")
        budget = cash + proceeds - self.p.min_cash_pct * equity
        expo = {"region": {}, "sector": {}}
        for s in kept:
            v = holdings[s].value_base / equity
            for k in ("region", "sector"):
                key = self.u.at[s, k] if s in self.u.index else "?"
                expo[k][key] = expo[k].get(key, 0) + v

        cands = score[(score >= self.p.min_score_to_buy) & (~veto.reindex(score.index).fillna(True))]
        cands = cands.drop([s for s in cands.index if s in holdings or
                            (session_symbols is not None and s not in session_symbols)],
                           errors="ignore").sort_values(ascending=False)
        held_now = list(kept)
        for sym in cands.index:
            if slots <= 0 or budget <= 0:
                self.skips[sym] = "no free position slots" if slots <= 0 else "no cash left after reserve"
                continue
            r = snap.loc[sym]
            region = self.u.at[sym, "region"] if sym in self.u.index else "?"
            sector = self.u.at[sym, "sector"] if sym in self.u.index else "?"
            ok, fit_note = self.risk.portfolio_fit(sym, held_now, returns)
            if not ok:
                self.skips[sym] = fit_note
                continue
            conviction = min(1.0, max(0.0, (cands[sym] - self.p.min_score_to_buy) / (0.8 - self.p.min_score_to_buy)))
            case = self.debate(sym, snap, tables)
            weight = self.risk.target_weight(r, conviction) * (0.75 if case["conflicts"] else 1.0)
            if expo["region"].get(region, 0) + weight > self.p.max_region_pct:
                self.skips[sym] = f"{region} already near the {self.p.max_region_pct:.0%} region limit"
                continue
            if expo["sector"].get(sector, 0) + weight > self.p.max_sector_pct:
                self.skips[sym] = f"{sector} already near the {self.p.max_sector_pct:.0%} sector limit"
                continue
            notional = min(weight * equity, budget)
            qty = math.floor(notional / (r.close * r.fx))
            if qty < 1:
                self.skips[sym] = "one share costs more than the position budget"
                continue
            case["coordinator"] = {"combined_score": round(float(cands[sym]), 3), "target_weight": round(weight, 4),
                                   "conviction": round(conviction, 2), "portfolio_fit": fit_note,
                                   "region": region, "sector": sector}
            reason = (f"score {cands[sym]:+.2f}, target {weight:.1%} of portfolio; "
                      f"12-1m momentum {pct(r.mom_12_1)}, vol {r.vol_60:.0%}")
            ccy = self.u.at[sym, "currency"] if sym in self.u.index else "NOK"
            mkt = self.u.at[sym, "market"] if sym in self.u.index else ""
            intents.append(TradeIntent(sym, "buy", qty, float(r.close), "entry", reason, float(cands[sym]), case,
                                       ccy, float(r.fx), mkt))
            budget -= qty * r.close * r.fx
            expo["region"][region] = expo["region"].get(region, 0) + weight
            expo["sector"][sector] = expo["sector"].get(sector, 0) + weight
            held_now.append(sym)
            slots -= 1
        return intents, score
