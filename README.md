# Latara 🤖📈

Latara is an automated day-trading agent for **Alpaca**. She runs by herself on
**GitHub Actions** every weekday the market is open. **She starts in paper
(practice) mode and stays there until you switch her.**

## What Latara does each day

| Time (ET) | What happens |
|---|---|
| ~9:00 | GitHub wakes Latara. She checks that the market is open (and handles holidays and half days). |
| 9:35 | She scans Alpaca's most-active stocks, top gainers and a 40-stock watchlist for **Stocks in Play**: price over $5, at least 1M shares/day, ATR of $0.50 or more, and first-5-minute volume above its 14-day normal. |
| 9:35–11:30 | She picks the top 2 by relative volume whose first 5-min candle is green. She buys when price breaks that candle's high, and a stop order goes to Alpaca right away. |
| until ~2:40–3:45 | Winners ride (no profit cap). Losers are cut at the stop. |
| before close | She sells everything. **She never holds overnight.** A backup job at 3:50 closes anything left open. |
| after | A daily report appears in the GitHub Actions run summary. A weekly scorecard runs on Fridays. |

### Where the strategy comes from
This is the **5-minute Opening Range Breakout on Stocks in Play** from Zarattini,
Barbon & Aziz (Andrew Aziz is a professional day trader at Peak Capital Trading
and Bear Bull Traders), *"A Profitable Day Trading Strategy for the U.S. Equity
Market"* (SSRN 4729284). They tested it on 7,000+ US stocks from 2016 to 2023.
It is the systematic version of what momentum day traders do by hand: trade only
stocks with unusual volume and news, enter on the opening breakout, and keep
losses small.

Changes made for a $100 cash account:
- **Long only.** Shorting needs a margin account, which requires $2,000.
- **Fixed daily budget from settled cash.** Proceeds from a sale are never reused the same day, so there are no good-faith violations in a cash account.
- **Hard safety rails:** stop for the day after 2 losing trades or a 5% loss, a $1 minimum order, a slippage guard (skip a trade if price has already run past the trigger), and broker-side stop orders.

### Pattern-day-trader rule
FINRA's $25,000 pattern-day-trader rule ended on June 4, 2026, so a small
account can day trade freely.

## Honest math about the $100/day goal
$100/day on $100 means doubling the account **every day**. No tested strategy
does that. Latara's realistic job is to make a small *percentage* per good day
and compound. The `LATARA_DAILY_GOAL` scorecard tracks your progress toward
$100/day, so you can see how much capital the strategy's actual performance
would need to get there.

## Setup (about 15 minutes)

1. **Alpaca:** switch to your **Paper** account. Reset its balance to **$100** so practice matches real life. Then create **paper API keys** (Key + Secret).
2. **GitHub:** create a new repository named `latara`.
   - Public repos get free, unlimited Actions minutes. Latara uses about 6,000+ minutes a month, which is more than the 2,000 free minutes a private repo gets. Your keys stay hidden either way, but run logs are visible on a public repo.
   - Upload every file in this folder, **including the `.github` folder**. On a Mac, press Cmd+Shift+. in Finder to see hidden folders.
3. Go to **Settings → Secrets and variables → Actions → Secrets** and add:
   - `ALPACA_API_KEY`
   - `ALPACA_SECRET_KEY`
4. Open the **Actions** tab and click "I understand… enable workflows".
5. Done. Latara trades on her own every weekday. To test her without trading, set the variable `LATARA_DRY_RUN=true` and click **Run workflow** on "Latara – trading day" during market hours.

## Controls (Settings → Secrets and variables → Actions → **Variables**)
| Variable | Default | What it does |
|---|---|---|
| `LATARA_ENABLED` | true | Set to `false` to pause Latara (kill switch) |
| `LATARA_MAX_CAPITAL` | (unset) | Fix her daily capital yourself (turns off compounding) |
| `LATARA_STARTING_CAPITAL` | 100 | Where her compounding capital starts |
| `LATARA_CAPITAL_CEILING` | 10000 | The most she may ever trade with in a day |
| `LATARA_MAX_POSITIONS` | 2 | Trades per day |
| `LATARA_DAILY_GOAL` | 100 | Scorecard target in dollars |
| `LATARA_REQUIRE_SPY_GREEN` | false | Only trade when the overall market opens green |
| `LATARA_BREAKEVEN_AT_R` | 0 | For example, 1.5 moves the stop to breakeven after +1.5R (0 = off, which matches the research) |
| `LATARA_DRY_RUN` | false | Log signals without placing orders |
| `LATARA_INTRADAY_FEED` | iex | Use `sip` if you pay for Alpaca's full market data |
| `LATARA_MODE` | paper | Live trading needs **both** `LATARA_MODE=live` and `LATARA_CONFIRM_LIVE=i-understand-this-is-real-money`, plus **live** API keys in the secrets |

## How Latara learns (every Saturday)
She can't watch individual traders (their trades aren't public, and copying
people who post wins online is a known trap). Instead she studies the market
the way professional systematic traders do:
1. **Journal** – every trade she makes is saved to `journal/trades.csv`.
2. **Replay** – she downloads ~2 months of 5-minute data and replays 192
   variations of her rules, including filters successful momentum day traders
   use (gap-and-go: only stocks that gapped up; trading with the market: only
   when SPY opens green; moving the stop to breakeven; tighter/wider stops).
3. **Prove it** – she learns on the older weeks and tests on the most recent
   weeks she hasn't seen. A new rule set is adopted only if it made money there
   *and* beat her current rules by at least 1 percentage point.
4. **Remember** – the chosen rules are saved in `latara/brain.json` (with a
   history of past decisions) and used from Monday on. You get the study
   report by email.

She also manages her own money settings:
- **Loss limits** – she learns whether to stop for the day after 1 or 2 losing
  trades, and at a 2% or 5% daily loss, using the same prove-it test.
- **Capital** – her trading capital starts at $100 and compounds with her own
  results: profits grow tomorrow's budget, losses shrink it. Paper and live
  money are tracked separately, so live starts fresh at $100.
- **Automatic limits** – never more than the account's settled cash, never more
  than `LATARA_CAPITAL_CEILING` ($10,000), and a drawdown brake: if her capital
  falls 20% below its best level she trades at half size until she recovers.

**Paper vs live is yours alone** – learning can never switch it. Any repo
Variable you set overrides what she learned (for example `LATARA_MAX_CAPITAL`
fixes her capital and turns compounding off).

## Before considering live money
Let Latara paper trade for at least 20 trading days. Check the Friday
scorecard: green-day %, average day, and worst day. Paper fills are kinder than
real ones, especially at the open, so expect live results to be somewhat worse.

## Run on your own computer (optional)
```
pip install -r requirements.txt
cp .env.example .env      # put your paper keys in .env
python -m latara trade
```

*Latara is software, not financial advice. Day trading carries a real risk of loss.*
