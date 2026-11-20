"""Common agent vocabulary.

Every analyst agent returns, for every symbol in the universe:
  stance      -1 (strong sell) .. +1 (strong buy)
  confidence   0 .. 1
and can explain any single symbol as a list of pros and cons. The coordinator
weighs these opinions; the broker enforces the hard rules.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd


@dataclass
class Opinion:
    agent: str
    symbol: str
    stance: float
    confidence: float
    pros: list[str] = field(default_factory=list)
    cons: list[str] = field(default_factory=list)


class AnalystAgent:
    name = "analyst"

    def assess(self, snap: pd.DataFrame) -> pd.DataFrame:
        """Return DataFrame indexed by symbol with columns stance, confidence."""
        raise NotImplementedError

    def explain(self, symbol: str, row: pd.Series) -> tuple[list[str], list[str]]:
        raise NotImplementedError

    def opinion(self, symbol: str, snap: pd.DataFrame, table: pd.DataFrame) -> Opinion:
        pros, cons = self.explain(symbol, snap.loc[symbol])
        r = table.loc[symbol]
        return Opinion(self.name, symbol, float(r.stance), float(r.confidence), pros, cons)


def xrank(s: pd.Series) -> pd.Series:
    """Cross-sectional percentile rank mapped to -1..+1 (NaN stays NaN)."""
    return s.rank(pct=True) * 2 - 1


def clip(x, lo=-1.0, hi=1.0):
    return np.clip(x, lo, hi)


def pct(x: float) -> str:
    return f"{x * 100:+.1f}%" if pd.notna(x) else "n/a"
