"""How the team learns: every analyst's stances are saved; `horizon_days` later we
check whether stocks it liked actually did better (risk-adjusted) than stocks it
disliked (rank correlation, a.k.a. information coefficient, IC). Agents with a
good recent record get more say; the weights are bounded so no agent can take
over or be silenced completely.
"""
from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd


class Learner:
    def __init__(self, base_weights: dict[str, float], cfg: dict):
        self.base = dict(base_weights)
        self.h = int(cfg["horizon_days"])
        self.window = int(cfg["window_evaluations"])
        self.lr = float(cfg["learning_rate"])
        self.lo, self.hi = float(cfg["min_weight"]), float(cfg["max_weight"])
        self.pending: list[tuple[pd.Timestamp, dict[str, pd.Series]]] = []
        self.ics: dict[str, list[float]] = {a: [] for a in base_weights}
        self.history: list[dict] = []

    def record(self, t, tables: dict[str, pd.DataFrame]) -> None:
        self.pending.append((pd.Timestamp(t), {a: tbl.stance.dropna() for a, tbl in tables.items()}))

    def evaluate(self, t, close: pd.DataFrame, vol: pd.DataFrame) -> list[dict]:
        """Score every recorded opinion set that has matured by date t."""
        t = pd.Timestamp(t)
        idx = close.index
        if t not in idx:
            return []
        now_i = idx.get_loc(t)
        done, keep = [], []
        for (d, stances) in self.pending:
            if d not in idx or now_i - idx.get_loc(d) < self.h:
                keep.append((d, stances))
                continue
            end = idx[idx.get_loc(d) + self.h]
            fwd = close.loc[end] / close.loc[d] - 1
            fwd_ra = (fwd / vol.loc[d].clip(lower=0.05)).dropna()
            for a, st in stances.items():
                common = st.index.intersection(fwd_ra.index)
                if len(common) < 10:
                    continue
                ic = st[common].rank().corr(fwd_ra[common].rank())
                if pd.notna(ic) and a in self.ics:
                    self.ics[a] = (self.ics[a] + [float(ic)])[-self.window:]
                    done.append({"date": str(d.date()), "agent": a, "ic": float(ic)})
        self.pending = keep
        return done

    def weights(self) -> dict[str, float]:
        raw = {}
        for a, b in self.base.items():
            m = np.mean(self.ics[a]) if self.ics[a] else 0.0
            raw[a] = b * math.exp(self.lr * m)
        # proportional weights with every agent kept inside [min_weight, max_weight]
        fixed: dict[str, float] = {}
        for _ in range(len(raw) + 1):
            free = {a: v for a, v in raw.items() if a not in fixed}
            room = 1.0 - sum(fixed.values())
            s = sum(free.values()) or 1.0
            w = {**fixed, **{a: room * v / s for a, v in free.items()}}
            over = [a for a in free if w[a] > self.hi]
            under = [a for a in free if w[a] < self.lo]
            if not over and not under:
                return w
            for a in (over or under):            # fix one side at a time, then re-share the rest
                fixed[a] = self.hi if over else self.lo
        return w

    # persistence for live runs
    def dump(self) -> str:
        return json.dumps({"ics": self.ics,
                           "pending": [(str(d), {a: s.to_dict() for a, s in st.items()}) for d, st in self.pending]})

    def load(self, blob: str | None) -> None:
        if not blob:
            return
        js = json.loads(blob)
        for a, v in js.get("ics", {}).items():
            if a in self.ics:
                self.ics[a] = v[-self.window:]
        self.pending = [(pd.Timestamp(d), {a: pd.Series(s, dtype=float) for a, s in st.items()})
                        for d, st in js.get("pending", [])]
