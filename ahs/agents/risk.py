"""Risk Analysis agent: scores risk, vetoes unsuitable names, sizes positions,
sets stops and checks how a candidate fits with what the portfolio already holds."""
from __future__ import annotations

import pandas as pd

from ..config import PortfolioProfile
from .base import AnalystAgent, clip, pct, xrank


class RiskAnalysisAgent(AnalystAgent):
    name = "risk"

    def __init__(self, profile: PortfolioProfile, min_price: float, min_adv: float):
        self.p, self.min_price, self.min_adv = profile, min_price, min_adv

    def assess(self, s: pd.DataFrame) -> pd.DataFrame:
        calm = -xrank(s.vol_60)
        shallow = clip(1 + s.maxdd_1y / 0.25)            # -25 % drawdown -> 0, -50 % -> -1
        beta = clip(1 - (s.beta_1y.fillna(1) - 1))       # beta 1 -> 0 penalty
        stance = clip(0.45 * calm + 0.35 * shallow + 0.20 * beta)
        px_usd = s["close_usd"] if "close_usd" in s else s.close          # thresholds are in USD
        adv_usd = s["adv20_usd"] if "adv20_usd" in s else s.adv20
        veto = ((s.vol_60 > self.p.max_annual_vol) | (px_usd < self.min_price) |
                (adv_usd < self.min_adv) | s.vol_60.isna() | s.sma200.isna())
        if self.p.require_uptrend:
            veto |= s.close < s.sma200
        conf = pd.Series(0.7, index=s.index).where(stance.notna())
        return pd.DataFrame({"stance": stance, "confidence": conf, "veto": veto})

    def veto_reason(self, r: pd.Series) -> str | None:
        if pd.isna(r.vol_60) or pd.isna(r.sma200):
            return "insufficient price history"
        if r.get("close_usd", r.close) < self.min_price:
            return f"share price below ${self.min_price}"
        if r.get("adv20_usd", r.adv20) < self.min_adv:
            return "too little trading (illiquid)"
        if r.vol_60 > self.p.max_annual_vol:
            return f"volatility {r.vol_60:.0%} above {self.p.name} limit {self.p.max_annual_vol:.0%}"
        if self.p.require_uptrend and r.close < r.sma200:
            return "not in a long-term uptrend (profile requires it)"
        return None

    def explain(self, sym, r):
        pros, cons = [], []
        (pros if r.vol_60 < 0.30 else cons).append(f"Annualised volatility {r.vol_60:.0%}")
        if pd.notna(r.maxdd_1y):
            (pros if r.maxdd_1y > -0.20 else cons).append(f"Worst 1-year drawdown {pct(r.maxdd_1y)}")
        if pd.notna(r.beta_1y):
            (pros if r.beta_1y < 1.1 else cons).append(f"Beta vs S&P 500 {r.beta_1y:.2f}")
        why = self.veto_reason(r)
        if why:
            cons.append(f"VETO: {why}")
        return pros, cons

    # ---------- sizing & stops ----------
    def target_weight(self, r: pd.Series, conviction: float) -> float:
        """Volatility-scaled weight, capped by profile. conviction in 0..1."""
        vol = max(float(r.vol_60), 0.05)
        scale = min(1.0, max(0.3, self.p.ref_vol / vol))
        return self.p.max_position_pct * scale * (0.5 + 0.5 * conviction)

    def stop_triggered(self, r: pd.Series, entry: float, high_water: float) -> str | None:
        if r.close <= entry * (1 - self.p.hard_stop_pct):
            return f"hard stop: {pct(r.close / entry - 1)} from entry"
        trail = high_water - self.p.trailing_atr_mult * r.atr14
        if pd.notna(trail) and r.close < trail and high_water > entry:
            return f"trailing stop: closed below {trail:.2f} ({self.p.trailing_atr_mult}x ATR under peak)"
        if self.p.require_uptrend and pd.notna(r.sma200) and r.close < r.sma200 * 0.98:
            return "long-term trend broken (below 200-day average)"
        return None

    def portfolio_fit(self, sym: str, held: list[str], returns: pd.DataFrame) -> tuple[bool, str]:
        if not held or sym not in returns:
            return True, ""
        win = returns.iloc[-120:]
        corr = win[held].corrwith(win[sym]).dropna()
        if corr.empty:
            return True, ""
        top = corr.idxmax()
        if corr.max() > self.p.max_corr_with_holdings:
            return False, f"too correlated with {top} ({corr.max():.2f})"
        return True, f"max correlation with holdings {corr.max():.2f} ({top})"
