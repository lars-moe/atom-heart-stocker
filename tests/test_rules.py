"""Run with:  python -m pytest -q"""
import pytest

from ahs.agents.broker import BrokerAgent, TradeIntent
from ahs.backtest import run_backtest
from ahs.book import SimBook
from ahs.config import load_config
from ahs.data import load_universe
from ahs.learning import Learner
from ahs.synthetic import make_fx, make_market

HR = {"max_single_buy_pct_of_equity": 0.25, "max_position_pct_of_equity": 0.25,
      "allow_margin": False, "avoid_same_day_round_trip": True}
COSTS = {"slippage_bps": 10, "commission_bps": 5, "sell_regulatory_bps": 0.3, "fx_conversion_bps": 25}


class Recorder:
    def __init__(self):
        self.sent = []

    def submit(self, *a):
        self.sent.append(a)
        return {"status": "accepted"}


def broker():
    return BrokerAgent(HR, COSTS, 30)


def test_single_buy_never_exceeds_25pct_in_nok():
    b, gw = broker(), Recorder()
    # EUR stock: wants 1000 x EUR 10 x 11.5 = NOK 115 000 of a NOK 200 000 portfolio
    it = TradeIntent("SAP.DE", "buy", 1000, 10.0, "entry", "test", currency="EUR", fx=11.5)
    recs = b.execute([it], gw, equity=200_000, cash=200_000, position_values={}, bought_today=set(), tag="t")
    assert recs[0].notional <= 0.25 * 200_000
    assert any("25%" in n for n in recs[0].broker_notes)


def test_position_cap_counts_existing_holding():
    b, gw = broker(), Recorder()
    it = TradeIntent("EQNR.OL", "buy", 300, 250.0, "entry", "test")
    recs = b.execute([it], gw, 200_000, 200_000, {"EQNR.OL": 45_000}, set(), "t")
    assert 45_000 + recs[0].notional <= 0.25 * 200_000 + 1e-6


def test_no_margin_and_shared_cash():
    b, gw = broker(), Recorder()
    its = [TradeIntent(s, "buy", 400, 100.0, "entry", "t") for s in ("A", "B", "C")]
    recs = b.execute(its, gw, 200_000, 50_000, {}, set(), "t")
    assert sum(r.notional for r in recs) <= 50_000


def test_same_day_round_trip_blocked():
    b, gw = broker(), Recorder()
    recs = b.execute([TradeIntent("AAA", "sell", 10, 10.0, "exit", "t")], gw, 20_000, 0, {}, {"AAA"}, "t")
    assert recs[0].status == "refused" and not gw.sent


def test_config_cannot_loosen_hard_rule(tmp_path):
    import yaml
    from ahs.config import ROOT
    raw = yaml.safe_load((ROOT / "config.yaml").read_text())
    raw["hard_rules"]["max_single_buy_pct_of_equity"] = 0.40
    f = tmp_path / "c.yaml"
    f.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError):
        load_config(f)


def test_currency_rule_in_book():
    """FX moves do not change the value of a held position; they are realised on sale."""
    bk = SimBook(cash=250_000, start_capital=250_000, cost_cfg={})
    bk.buy("SAP.DE", 100, 200.0, "EUR", 11.0, "2026-01-02")          # costs NOK 220 000
    assert bk.equity({"SAP.DE": 200.0}) == pytest.approx(250_000)
    # EUR strengthens to 12: value unchanged while held (rule), FX effect only shown as information
    assert bk.unrealised_fx_effect({"SAP.DE": 200.0}, {"EUR": 12.0}) == pytest.approx(20_000)
    assert bk.equity({"SAP.DE": 200.0}) == pytest.approx(250_000)
    rec = bk.sell("SAP.DE", 100, 200.0, 12.0, "2026-02-02")
    assert rec["fx_pnl_nok"] == pytest.approx(100 * 200 * 1.0)      # realised only now
    assert rec["price_pnl_nok"] == pytest.approx(0)
    assert bk.cash == pytest.approx(270_000)


def test_book_never_borrows():
    bk = SimBook(cash=10_000, start_capital=10_000, cost_cfg=COSTS)
    bk.buy("X", 1000, 50.0, "NOK", 1.0, "2026-01-02")
    assert bk.cash >= 0


def test_learner_weights_bounded():
    lr = Learner({"a": 0.5, "b": 0.3, "c": 0.2}, {"horizon_days": 5, "window_evaluations": 10,
                                                  "learning_rate": 50, "min_weight": 0.1, "max_weight": 0.6})
    lr.ics = {"a": [0.9] * 10, "b": [-0.9] * 10, "c": [-0.9] * 10}
    w = lr.weights()
    assert abs(sum(w.values()) - 1) < 1e-9
    assert all(0.1 - 1e-9 <= v <= 0.6 + 1e-9 for v in w.values())


@pytest.mark.parametrize("pf", ["MAIN", "HIGH_RISK"])
def test_backtest_respects_rules(pf):
    cfg = load_config()
    u = load_universe(cfg.path(cfg["universe"]["file"]))
    syms = list(u.index[::3]) + ["SPY", "ACWI"]
    p = make_market(syms, years=7, seed=3)
    r = run_backtest(cfg, p, u, pf, make_fx(p.dates, u.currency), years=2, log=lambda *a: None)
    assert r.metrics["max_single_buy_pct"] <= 0.25 + 1e-6
    assert (r.nav.cash >= -1e-6).all()
    assert r.nav.positions.max() <= cfg.portfolios[pf].max_positions
    assert r.metrics["trades"] > 0
