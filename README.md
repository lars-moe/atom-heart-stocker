# Atom Heart Stocker 1.0

Five cooperating agents trade two paper portfolios on **Oslo Børs, the Nordics, Europe and the US**.
Every trading day the team runs a session for each market group during that market's opening hours.
A dashboard shows the portfolios, their history and the day's research in plain language.

| Portfolio | Start | Style |
|---|---|---|
| **MAIN** | NOK 200 000 | Balanced. Requires a long-term uptrend. Max 12 % per position, up to 15 positions, 5 % cash reserve, 15 % hard stop. |
| **HIGH_RISK** | NOK 200 000 | Aggressive and momentum-led. Up to 25 % per position, max 8 positions, no cash reserve, wider stops. |

**Hard rule (both):** one buy can never exceed **25 % of the total portfolio value** (cash + holdings, in NOK, including all costs). The Broker agent enforces this on every order, and also stops repeated buys from stacking a position above 25 %. Other rules: no margin, no short selling, no same-day round trips. The tests check all of these.

## Daily sessions (Oslo time, adjusts automatically for summer/winter time)

| Time | Session | Markets |
|---|---|---|
| 10:15 | Nordic | Oslo Børs, Stockholm, Copenhagen, Helsinki |
| 10:30 | Europe | Xetra, Euronext Paris/Amsterdam, Milan, Madrid, SIX Zurich, London |
| 16:30 (15:30 for a few weeks a year) | US | NYSE/Nasdaq, plus Asian and Latin American ADRs |

In each session the team:
1. refreshes prices (about 15 minutes delayed) and Norges Bank FX rates;
2. assesses every stock in those markets;
3. checks stops on the holdings that trade there;
4. debates and decides, then trades through the portfolio's broker;
5. writes the research note.

On holidays the session sees no trades in a reference stock (Equinor, SAP or SPY) and skips itself. If the computer was asleep at the scheduled time, a missed session runs late, as long as its markets are still open.

## Brokers (per portfolio, in `config.yaml`)

| `broker:` | What it is | Markets |
|---|---|---|
| `sim` **(default)** | Built-in simulator on real market prices. Orders fill at the current price plus costs. Works immediately with no account. | All |
| `ibkr` | Interactive Brokers paper account. TWS or IB Gateway must run on this computer. | All |
| `alpaca` | Alpaca paper account. | US only |

To switch to IBKR:
1. Open an IBKR free-trial or paper account.
2. In TWS: *Edit → Global Configuration → API → Settings*, then enable socket clients. The paper port is 7497.
3. `pip install ib_async`.
4. Set `broker: ibkr`, then run `python main.py init`.
5. Start with `--dry-run`.

The IBKR adapter is written but has not been tested against a live gateway yet. Check the first sessions closely, especially how symbols are mapped and how currencies are converted. If a symbol is wrong, put the right one in the `ibkr_symbol` column of `universe.csv`.

## Currency rule

The book is kept in NOK. Buying a foreign stock converts NOK at that day's rate, and the stock stays valued at that rate while held. Currency gains or losses count only when sale proceeds are converted back to NOK. The dashboard shows them separately, and also shows the not-counted FX effect on open positions as information only. FX conversion costs 0.25 %. All other trading costs (slippage, commission, fees) are deducted too.

## The agents

- **Opportunity Screen** looks for trends, momentum, closeness to the 52-week high, and breakouts on high volume.
- **Value** compares the price with the stock's own 5-year history and its long-run quality. In live sessions it also uses P/E, PEG, free-cash-flow yield and debt.
- **Risk Analysis** can veto a stock (too volatile, illiquid, or a downtrend for MAIN). It also sets position sizes, stops and correlation limits.
- **Portfolio Coordinator** runs the debate: pros and cons from each agent, and a 25 % smaller position when agents disagree. It also applies the region, sector and cash limits. Each stock is held for at least a minimum number of days to avoid churn; only the hard stop can sell earlier.
- **Broker** applies the hard rules and places the orders.
- Optional (with `ANTHROPIC_API_KEY`): Claude reviews proposed buys as a devil's advocate and can only shrink or reject them. Claude can also write a short daily analyst note.

**Learning:** each analyst's calls are scored 20 trading days later. Agents with a better record get more say, with weights bounded between 10 % and 60 %.

## Dashboard

`reports/dashboard.html` is rebuilt after every session. It is self-contained, refreshes itself every 5 minutes, and works on a phone.

| Tab | Shows |
|---|---|
| Overview | value, return, cash, value over time vs ACWI and S&P 500, both portfolios, allocation, P/L split into price, FX and costs |
| Holdings | positions, return, value in NOK, weight |
| Trades | closed trades with price and FX P/L and the reason sold; all orders with broker notes |
| Research | each session's plain-language note, market breadth per exchange, top ideas with every agent's pros and cons, why things were or weren't bought |
| Agents | how each agent's influence has changed, and its track record |

`python main.py serve` shows the dashboard at http://localhost:8050/dashboard.html. Other devices on the same network can use the computer's IP address.

## Run it in the cloud (recommended): GitHub Actions

Free for public repositories, and no computer needs to be on. GitHub runs `python main.py due` several times
each weekday. The program works out which session is due in local exchange time and runs each one once a day,
with automatic catch-up and retries. Portfolio state is saved to the `state` branch, and the dashboard is published
on GitHub Pages. **Step-by-step guide: [SETUP_GITHUB.md](SETUP_GITHUB.md).**

This works for the `sim` broker. IBKR needs a logged-in gateway, so for IBKR use a small always-on machine
(a Raspberry Pi or a cheap cloud server) running `python main.py scheduler`.

## Setup on your own computer (optional, for testing and backtests)

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env            # optional: ANTHROPIC_API_KEY, Alpaca keys
python main.py init             # FX, data source, broker check, today's plan
python main.py fetch            # ~10 years of daily prices for all ~160 stocks (a few minutes)
python main.py backtest         # 5-year backtest -> reports/backtest_dashboard.html
python main.py session -s nordic --dry-run --force    # see what the team would do, no orders
python main.py scheduler        # only if NOT using GitHub Actions: trades every session, every trading day
```

Don't run the local scheduler and GitHub Actions at the same time. They would keep two separate sets of portfolios.

Run the scheduler automatically at login:
- **Windows:** use `run_scheduler.bat` in Task Scheduler with the trigger "At log on".
- **macOS/Linux:** use `run_scheduler.sh` with `@reboot` in cron.

The computer must be awake from about 10:00 to 17:00 Oslo time.

## Honest limitations

- Prices come from Yahoo Finance via `yfinance`, which is free but unofficial. It sometimes breaks or changes, so stocks with no data are logged and skipped. For something more robust, a paid source (for example EODHD) or IBKR's own data can be plugged into `ahs/sources.py`.
- Simulator fills are idealised (delayed price plus estimated costs), and real fills would differ. The backtest is flattering: it uses today's well-known names, and fundamentals and the reviewer are switched off.
- The default costs (0.10 % slippage, 0.05 % commission, 0.25 % FX) are estimates. Set them to your future broker's actual prices.
- Going live with real money should wait for several months of paper results that beat ACWI after costs, through at least one market decline. Check aksjesparekonto (ASK) eligibility and taxes first.

## Files

```
main.py               CLI (init, fetch, backtest, session, scheduler, due, dashboard, serve, demo)
.github/workflows/    trade (sessions + dashboard), keepalive, tests
SETUP_GITHUB.md       cloud setup guide
config.yaml           all settings (brokers, profiles, costs, learning, hard rules)
universe.csv          ~160 stocks: symbol, market, currency, region, sector (+ broker symbol overrides)
ahs/markets.py        exchanges, opening hours, sessions
ahs/agents/           the five agents
ahs/book.py           simulated NOK book with the currency rule
ahs/brokers.py        sim / IBKR / Alpaca backends
ahs/engine.py         one session
ahs/scheduler.py      runs sessions at market times
ahs/research.py       research notes in plain language
ahs/dashboard.py      the dashboard
ahs/backtest.py       5-year multi-market backtest
tests/                rules, currency rule, full session with fake data, DST schedule
```
