"""Assembles the five-agent team for one portfolio."""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .agents.broker import BrokerAgent
from .agents.coordinator import PortfolioCoordinatorAgent
from .agents.opportunity import OpportunityScreenAgent
from .agents.risk import RiskAnalysisAgent
from .agents.value import ValueAgent
from .config import Config
from .learning import Learner


@dataclass
class Team:
    opportunity: OpportunityScreenAgent
    value: ValueAgent
    risk: RiskAnalysisAgent
    coordinator: PortfolioCoordinatorAgent
    broker: BrokerAgent
    learner: Learner


def build_team(cfg: Config, portfolio: str, universe: pd.DataFrame, fundamentals: dict | None = None) -> Team:
    prof = cfg.portfolios[portfolio]
    uni = cfg["universe"]
    opp = OpportunityScreenAgent()
    val = ValueAgent(fundamentals)
    risk = RiskAnalysisAgent(prof, uni["min_price_usd"], uni["min_avg_dollar_volume_usd"])
    coord = PortfolioCoordinatorAgent(prof, [opp, val, risk], risk, universe)
    broker = BrokerAgent(cfg["hard_rules"], cfg["costs"], cfg["alpaca"]["limit_buffer_bps"])
    learner = Learner(prof.agent_weights, cfg["learning"])
    return Team(opp, val, risk, coord, broker, learner)
