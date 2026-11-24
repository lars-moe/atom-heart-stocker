"""Configuration loading. Hard rules are validated so a typo can never loosen them."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
USER_HARD_CAP = 0.25  # the project owner's rule; config may be stricter, never looser


@dataclass
class PortfolioProfile:
    name: str
    env_prefix: str
    broker: str
    start_capital_nok: float
    max_position_pct: float
    max_positions: int
    min_cash_pct: float
    max_region_pct: float
    max_sector_pct: float
    max_annual_vol: float
    ref_vol: float
    hard_stop_pct: float
    trailing_atr_mult: float
    require_uptrend: bool
    min_score_to_buy: float
    sell_score: float
    max_corr_with_holdings: float
    min_hold_days: int = 7
    min_order_pct: float = 0.01
    min_fill_of_target: float = 0.5
    rotation_margin: float = 0.30
    max_rotations_per_session: int = 1
    agent_weights: dict = field(default_factory=dict)


@dataclass
class Config:
    raw: dict
    portfolios: dict

    def __getitem__(self, key):
        return self.raw[key]

    @property
    def root(self) -> Path:
        return ROOT

    def path(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else ROOT / p


def _load_dotenv() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_config(path: str | Path | None = None) -> Config:
    _load_dotenv()
    path = Path(path) if path else ROOT / "config.yaml"
    raw = yaml.safe_load(path.read_text())

    hr = raw["hard_rules"]
    for key in ("max_single_buy_pct_of_equity", "max_position_pct_of_equity"):
        if not (0 < float(hr[key]) <= USER_HARD_CAP):
            raise ValueError(f"hard_rules.{key} must be in (0, {USER_HARD_CAP}]; got {hr[key]}")
    if hr.get("allow_short"):
        raise ValueError("Short selling is not supported by this system.")

    portfolios = {}
    for name, p in raw["portfolios"].items():
        if p.get("broker", "sim") not in ("sim", "ibkr", "alpaca"):
            raise ValueError(f"portfolios.{name}.broker must be sim, ibkr or alpaca")
        p.setdefault("broker", "sim")
        prof = PortfolioProfile(name=name, **p)
        prof.max_position_pct = min(prof.max_position_pct, float(hr["max_position_pct_of_equity"]))
        s = sum(prof.agent_weights.values())
        prof.agent_weights = {k: v / s for k, v in prof.agent_weights.items()}
        portfolios[name] = prof
    return Config(raw=raw, portfolios=portfolios)
