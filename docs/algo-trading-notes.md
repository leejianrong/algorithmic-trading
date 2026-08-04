# Notes on Algo Trading

Conversation with Google Gemini 3

## Question on Overfitting

Question: Algorithmic trading how do you know when you're overfitting vs actually making something that works?

Response:

You know your strategy is overfit when it excels in a historical backtest but collapses on new data, meaning it memorized random market noise rather than finding a repeatable structural edge. Conversely, a working strategy demonstrates stable performance across unseen datasets and degrades gracefully rather than failing immediately. [1, 2, 3]
To distinguish between the two, you must look for specific failure signals and use robust validation techniques. [2, 4]

### Core Signals of Overfitting

* The Parameter Cliff: If you change a parameter (like moving a 20-period Moving Average to 21) and your returns completely collapse, your model is curve-fit. Robust strategies display smooth changes on a parameter heatmap. [5, 6, 7, 8]
* The "Rule" Explosion: Strategies requiring a long string of specific conditions (e.g., 12 entry filters and 9 exit rules) are almost always capturing historical accidents rather than actual market structure. [9]
* High Parameter-to-Trade Ratio: If your strategy relies on 5 parameters but only generates 40 trades over your entire backtest window, it lacks statistical significance. Aim for a minimum of 30 to 50 trades per free parameter. [1, 5]
* Asset Isolation: If the logic fails completely when applied to a highly correlated asset (e.g., working perfectly on BTC but failing on ETH), it is likely overfit to a single asset's noise. [1]

### How to Prove a Strategy Actually Works

To confirm you have found a genuine market edge, you must strictly implement the following validation methodologies: [10]

#### 1. Out-of-Sample (OOS) and Walk-Forward Testing [11, 12, 13]

* Data Splitting: Divide your historical data into an In-Sample (IS) training set (e.g., 70%) and an Out-of-Sample (OOS) testing set (e.g., 30%). [3]
* Blind Testing: Optimize your parameters exclusively on the IS data, then run the strategy exactly once on the OOS data. [5, 14]
* Walk-Forward Analysis: Roll this train-and-test window forward incrementally through time. If OOS performance degrades heavily compared to IS, reject the strategy. [5, 15]

#### 2. Regime and Parameter Stress Testing

* Regime Breakdown: Separate your backtest data into distinct market states (e.g., high volatility, low volatility, trending, and mean-reverting). A working strategy should perform predictably or sit flat in unfavorable regimes, rather than blowing up. [1, 5]
* Monte Carlo Simulations: Randomly shuffle the order of your historical trade returns thousands of times. If your drawdown levels or Sharpe ratios break down completely under a shuffled sequence, your strategy is highly dependent on sequential path luck. [5, 16]

#### 3. Incubation (Paper and Micro-Live Trading)

* Forward Testing: Before putting significant capital at risk, deploy the system to a paper trading environment or a micro-account with minimal capital.
* Live vs. Simulation Match: Compare live execution metrics to your simulation. If execution metrics diverge wildly due to transaction costs, slippage, or latency, your theoretical edge does not actually exist in production. [6, 14]

### In-Sample vs. Out-of-Sample Comparison

| Metric / Behavior              | Overfit Strategy                       | Robust, Working Strategy              |
| ------------------------------ | -------------------------------------- | ------------------------------------- |
| In-Sample Performance          | Near-flawless, smooth equity curve     | Modest, realistic returns             |
| Out-of-Sample Performance      | Immediate, sharp downward turn         | Performance mirrors the training data |
| Parameter Variations (+/- 10%) | Extreme volatility in net profit       | Stable and consistent returns         |
| Complexity                     | Many rules, indicators, and conditions | Minimal, logically simple rules       |

## Tips and Advice

Question: What else should I look out for if I'm doing daily timeframe? More tips and advice please.

Response:

When building an algorithmic trading system for the daily timeframe, you are essentially managing a portfolio optimization and risk problem, not a speed problem. Because you only get one data point per day, small structural errors in your code or assumptions can take months to show up in your account balance.
To protect your capital and maximize your edge on daily data, integrate these critical engineering and structural guardrails into your system

### 1. Account for Corporate Actions and Dividends

If you are trading stocks or stock indexes, raw price charts are deeply flawed for algorithmic backtesting.

* The Problem: When a company pays a dividend, its stock price artificially drops by the dividend amount overnight. If a stock undergoes a 2-for-1 split, the price cuts in half. A naive algorithm will see these as massive, artificial short signals or stop-loss triggers.
* The Fix: You must exclusively use Total Return / Dividend-Adjusted Data. This adjusts historical prices backward to account for splits and payouts, ensuring your backtest matches reality.

### 2. Standardize Your Execution Clock

Because daily data only updates once every 24 hours, when your algorithm executes its orders changes everything.

* The Problem: If your code triggers an order exactly at the market close, you might get hit with high volatility and poor fills during the "closing auction." If you wait until the next day's open, you face overnight gap risk. [1]
* The Fix: Pick a hard execution rule and stick to it in both backtesting and live trading. The industry standard is to calculate signals using the daily closing price, and then execute Market-on-Open (MOO) orders the following morning, or limit orders slightly below the previous close.

### 3. Implement Sector and Market-Cap Filters

A technical indicator signal on a $500 billion mega-cap stock behaves completely differently than the same signal on a volatile $200 million micro-cap stock.

* Liquidity Guardrails: Filter out any asset that doesn't meet a minimum Average Daily Volume (ADV) threshold (e.g., only trade stocks trading over $20 million in daily volume). This ensures your live orders actually get filled without pushing the market price against you.
* Sector Balance: If your algorithm triggers a "Buy" signal across 15 different technology stocks on the same day, you don't have 15 independent trades. You have one massive, highly leveraged bet on the tech sector. Hardcode a rule that limits maximum exposure to any single industry sector.

### 4. Optimize for Portfolio Turnover, Not High Trade Counts

Because daily bars are slow, a strategy with too many conditions might only trade 5 times a year per asset. [2]

* The Danger: If your strategy has low turnover, your capital sits idle doing nothing, or you run the risk of your sample size being too small to be statistically reliable.
* The Fix: Run your daily algorithm across a broad basket of assets (e.g., the entire S&P 500, or a list of the top 30 liquid crypto pairs) simultaneously. This diversification increases your total trade count to a statistically valid sample size while keeping individual asset risk low.

### 5. Account for Survivorship Bias

If you backtest your daily strategy on the current members of an index (like today's S&P 500), your results will be heavily inflated. [3]

* The Illusion: You are accidentally testing only on the "winners" that survived to the present day. You are ignoring all the companies that went bankrupt or were delisted over the last 10 years. [4]
* The Fix: Use a survivorship-bias-free database for your backtests. Your historical data must include companies that existed in 2015 or 2020 but are no longer trading today.

### Daily Timeframe Verification Checklist

Before deploying a daily asset algorithm live, ensure your script checks every box:

[ ] Data Source        ➔ Is it explicitly split- and dividend-adjusted?
[ ] Volatility Check   ➔ Does it use ATR to adjust position size based on current daily risk?
[ ] Execution Logic    ➔ Is the code executing exactly at the Open or Close, matching the backtest?
[ ] Sector Limits      ➔ Is there a hard ceiling on concurrent trades in the exact same sector?
[ ] Basket Size        ➔ Are you scanning at least 20+ assets to ensure a steady stream of independent setups?

💡 To help refine your risk model, how do you currently determine your position size per trade? Are you putting a fixed dollar amount into every setup, or are you scaling your size based on how volatile the asset is?
