# Multi-Asset Trend Following Strategy
## Executive Architecture Summary
**February 2026 | Complete System Overview**

---

## TABLE OF CONTENTS

1. [System Overview](#system-overview)
2. [Building Blocks](#building-blocks)
3. [Data Architecture](#data-architecture)
4. [Technical Indicators & Formulas](#technical-indicators--formulas)
5. [Strategy Rules & Formulas](#strategy-rules--formulas)
6. [Risk Management Formulas](#risk-management-formulas)
7. [Backtesting Framework](#backtesting-framework)
8. [Portfolio Performance Management](#portfolio-performance-management)
9. [Operational Workflow](#operational-workflow)
10. [Implementation Roadmap](#implementation-roadmap)

---

# SYSTEM OVERVIEW

## Purpose
A production-grade systematic trend-following strategy that:
- Follows established trends in stocks, ETFs, and crypto
- Uses quantitative rules with zero discretion
- Requires human approval before execution
- Operates on monthly rebalancing cycles
- Manages risk through position sizing and stop-losses

## Target Parameters
- **Account Size:** €10,000 → €100,000+
- **Asset Classes:** US Stocks (NYSE/NASDAQ), EU Stocks (XETRA/LSE/Euronext), Global ETFs, Crypto (BTC/ETH)
- **Holding Period:** Medium to long-term (weeks to months)
- **Rebalancing:** Monthly (last trading day of month)
- **Risk Per Position:** 2% of account equity
- **Portfolio Size:** 10-25 positions depending on account size

## Core Philosophy
**"Follow the trend, cut losses quickly, let winners run."**

The strategy identifies instruments in strong uptrends, enters positions with defined risk, and exits when the trend weakens or stops are hit.

---

# BUILDING BLOCKS

## 1. DATA LAYER (Scripts 1-5)

### Script 1: EODHD Bulk Downloader
**Purpose:** Acquire all market data from EODHD API

**Capabilities:**
- Downloads OHLCV data for 6 exchanges (NYSE, NASDAQ, XETRA, LSE, PA, AS)
- Three modes: INITIAL (setup), INCREMENTAL (production), CUSTOM (backfill)
- Auto-discovers new symbols (IPOs, new listings)
- Flags delisted symbols
- Downloads corporate actions (splits, dividends)
- Self-healing (automatically catches missed days)

**Key Innovation:** INCREMENTAL mode auto-detects gap and fills missing data without manual intervention

### Script 2: Yahoo Finance Fundamentals Downloader
**Purpose:** Fetch company metadata

**Data Acquired:**
- Company name, sector, industry
- Market capitalization
- Currency and exchange
- Employee count

### Script 3: Data Consolidator & Validator
**Purpose:** Merge EODHD + Yahoo data and validate quality

**Validation Rules:**
- Price consistency (High ≥ Low, no 25%+ jumps without corporate actions)
- Completeness (no more than 5% missing bars)
- Volume sanity (no 100× spikes without news)
- ATR explosion check (volatility < 2× median)
- Data freshness (no more than 3 days stale)

**Output:** Unified time series per symbol with quality flags

### Script 4: Universe Screener
**Purpose:** Apply liquidity and fundamental filters

**Filters Applied:**
- Minimum market capitalization (varies by region)
- Minimum average daily volume (liquidity requirement)
- Minimum price (avoid penny stocks)
- Minimum listing history (252 trading days)
- Data quality (must pass all validations)
- No pending corporate actions (splits, mergers)

**Output:** Qualified universe of tradable instruments

### Script 5: Indicator Calculator
**Purpose:** Calculate all technical indicators

**Indicators Computed:**
- Simple Moving Average 50-day (SMA_50)
- Simple Moving Average 200-day (SMA_200)
- Average True Range 20-day (ATR_20)
- Average Directional Index 14-day (ADX_14)

---

## 2. STRATEGY LAYER (Scripts 6-10)

### Script 6: Trend Qualifier
**Purpose:** Identify instruments in confirmed uptrends

**Qualification Criteria (ALL must be true):**
- SMA_50 > SMA_200 (golden cross)
- Close > SMA_50 (price above short-term trend)
- ADX_14 > 20 (sufficient trend strength)

**Output:** List of trend-qualified instruments

### Script 7: Momentum Ranker
**Purpose:** Rank qualified trends by momentum strength

**Ranking Method:**
- Momentum Score = (Close - SMA_200) / SMA_200 × 100
- Sort descending (highest momentum = strongest trend)

**Output:** Ranked list of instruments by momentum

### Script 8: Position Sizer
**Purpose:** Calculate position sizes adjusted for volatility

**Sizing Method:**
- Volatility-normalized allocation
- Floor: 0.5% of account
- Ceiling: 8.0% of account
- Target: 4.0% of account (typical)

**Output:** Share quantities and position values for each instrument

### Script 9: Stop-Loss Calculator
**Purpose:** Calculate initial and trailing stop-loss levels

**Stop Types:**
- Initial Stop: 3× ATR below entry (set once)
- Trailing Stop: 4× ATR below current price (activates at +15% profit, updates weekly)

**Output:** Stop prices for all positions

### Script 10: Exit Signal Generator
**Purpose:** Identify positions that should be exited

**Exit Triggers (Priority Order):**
1. Stop-loss hit (highest priority)
2. Trend reversal (SMA_50 < SMA_200)
3. Trend weakness (ADX < 15 for 3 consecutive days)
4. Rebalancing rotation (dropped from top N)

**Output:** List of positions to exit with reasons

---

## 3. PORTFOLIO LAYER (Scripts 11-12)

### Script 11: Monthly Rebalancer
**Purpose:** Generate monthly rebalancing recommendations

**Process:**
1. Check circuit breakers (halt if extreme conditions)
2. Identify mandatory exits (stop-loss, trend reversal)
3. Identify rotation exits (dropped from top N)
4. Select new entries (top N by momentum)
5. Calculate position sizes for new entries
6. Calculate stops for new positions
7. Generate detailed recommendation report

**Output:** Complete rebalancing plan with exits, entries, and rationale

### Script 12: Recommendation Report Generator
**Purpose:** Create human-readable reports for approval

**Report Sections:**
- Executive Summary (exits, entries, positions to hold)
- Mandatory Exits (with priority, reason, rationale)
- New Entries (with momentum rank, position size, initial stop)
- Positions to Hold (with current P&L, stop levels)
- Portfolio Allocation (by asset class, by sector)
- Risk Metrics (beta, portfolio ATR, concentration)
- Execution Checklist (day-by-day actions)
- Scenarios (bull/bear/sideways cases)
- Approval Section (sign-off required)

**Output:** PDF, HTML, and CSV formats

---

## 4. EXECUTION & MONITORING LAYER (Scripts 13-14)

### Script 13: Execution Logger
**Purpose:** Record actual trade fills and update portfolio state

**Records:**
- Trade execution timestamp
- Fill price and quantity
- Execution fees
- Slippage vs. expected price
- Updated portfolio state

**Output:** Trade log and updated portfolio_state.json

### Script 14: Daily Risk Monitor
**Purpose:** Check portfolio health and trigger alerts

**Monitoring Checks:**
- Stop-loss violations (any position below stop)
- Portfolio drawdown (alert if > 10%)
- Concentration risk (alert if top 3 positions > 30%)
- Correlation spike (alert if top 3 correlation > 0.85)
- Data staleness (alert if data > 3 days old)

**Output:** Daily monitoring report with alerts

---

## 5. VISUALIZATION LAYER (Script 15)

### Script 15: Technical Chart Generator
**Purpose:** Create interactive charts for analysis

**Chart Components:**
- Price evolution (200-day history)
- SMA 50 and SMA 200 overlays
- Volume bars
- ADX trend strength indicator
- ATR volatility indicator

**Features:**
- Interactive navigation (sidebar with search)
- Multiple symbols in single HTML file
- Keyboard shortcuts (arrow keys)
- No external dependencies

**Output:** Self-contained HTML dashboard

---

## 6. BACKTESTING FRAMEWORK (Scripts 16-18)

### Script 16: Backtest Engine
**Purpose:** Test strategy on historical data

**Capabilities:**
- Walk-forward backtesting (no look-ahead bias)
- Monthly rebalancing simulation
- Transaction cost modeling
- Slippage simulation
- Corporate action adjustments
- Multiple time periods (rolling windows)

**Metrics Computed:**
- Total return, annualized return, CAGR
- Sharpe ratio, Sortino ratio
- Maximum drawdown, drawdown duration
- Win rate, profit factor
- Average win/loss, win/loss ratio

**Output:** Backtest results with detailed metrics

### Script 17: Walk-Forward Optimizer
**Purpose:** Optimize parameters without overfitting

**Method:**
- In-sample period: Train (optimize parameters)
- Out-of-sample period: Test (validate performance)
- Rolling forward through time
- Multiple parameter sets tested

**Parameters Optimized:**
- SMA periods (50/200 vs 30/150 vs 100/300)
- ADX threshold (15/20/25)
- ATR multipliers (initial: 2.5/3.0/3.5, trailing: 3.5/4.0/4.5)
- Position count (10/15/20/25)

**Output:** Optimal parameter sets with stability metrics

### Script 18: Monte Carlo Simulator
**Purpose:** Assess strategy robustness to randomness

**Simulation Method:**
- Randomize trade sequence (preserve individual trades)
- Run 10,000 alternative histories
- Calculate confidence intervals

**Metrics Analyzed:**
- Distribution of final equity
- Probability of specific drawdown levels
- Worst-case scenarios (5th percentile)
- Best-case scenarios (95th percentile)

**Output:** Risk distribution and confidence intervals

---

## 7. VALIDATION & DECISION LAYER (Scripts 19-21)

### Script 19: Backtest Validator
**Purpose:** Validate backtest results against acceptance criteria before deployment consideration

**10 Primary Validation Tests (All Must Pass):**

1. **Positive Expectancy Test**
   - Threshold: Total Return >30% over 5-year backtest
   - Rationale: Must beat cost of capital (3% annually) + inflation (3% annually)
   - Pass/Fail: Binary decision

2. **Risk-Adjusted Outperformance Test**
   - Threshold: Strategy Sharpe > Benchmark Sharpe × 1.25
   - Benchmark: SPY (S&P 500) buy-and-hold
   - Rationale: Must provide superior risk-adjusted returns with buffer for implementation risk

3. **Acceptable Drawdown Test**
   - Threshold: Maximum Drawdown ≤ -30% (absolute limit)
   - Target: Maximum Drawdown ≤ -20%
   - Excellent: Maximum Drawdown > -15%
   - Rationale: Losses beyond -30% cause psychological stress and capital flight

4. **Sufficient Trade Count Test**
   - Threshold: ≥100 completed round-trip trades
   - Rationale: Statistical significance requires minimum sample size
   - Fail: <100 trades = results could be luck, not skill

5. **Realistic Win Rate Test**
   - Threshold: 35% ≤ Win Rate ≤ 65%
   - Suspicious: Win Rate >65% (potential overfitting)
   - Fail: Win Rate <35% (too many losses, unsustainable)
   - Rationale: 40-50% typical for trend following

6. **Positive Profit Factor Test**
   - Threshold: Profit Factor ≥ 1.5
   - Good: Profit Factor ≥ 2.0
   - Excellent: Profit Factor ≥ 2.5
   - Rationale: Profit factor <1.5 means small edge, vulnerable to cost increases

7. **Win/Loss Ratio Test**
   - Threshold: Average Win ≥ 2.0 × Average Loss
   - Good: Ratio ≥ 2.5
   - Excellent: Ratio ≥ 3.0
   - Rationale: Trend following should have large winners, small losers

8. **Transaction Cost Sensitivity Test**
   - Method: Re-run backtest with 2× transaction costs (0.2% vs 0.1%)
   - Threshold: Return drops by <50% when costs double
   - Rationale: Strategy must be robust to higher execution costs

9. **Drawdown Recovery Time Test**
   - Threshold: Average recovery ≤12 months, Maximum recovery ≤24 months
   - Good: Average recovery 6-9 months
   - Excellent: Average recovery ≤6 months
   - Rationale: Long recovery times cause investor impatience and capital withdrawal

10. **Annual Consistency Test**
    - Threshold: ≥70% of years positive
    - Good: 70-80% positive years
    - Excellent: ≥80% positive years
    - Rationale: Strategy shouldn't depend on single outlier year

**Performance Scoring System (30 Points Total):**

Each metric scored 0-3 points:
- 3 points: Excellent performance
- 2 points: Good/Target performance
- 1 point: Acceptable/Minimum performance
- 0 points: Below minimum (fail)

| Metric | Minimum (1 pt) | Target (2 pts) | Excellent (3 pts) |
|--------|----------------|----------------|-------------------|
| CAGR | 8% | 12% | 18% |
| Sharpe Ratio | 0.8 | 1.2 | 2.0 |
| Sortino Ratio | 1.0 | 1.5 | 2.5 |
| Calmar Ratio | 0.5 | 1.0 | 2.0 |
| Max Drawdown | -30% | -20% | -15% |
| Win Rate | 35% | 45% | 55% |
| Profit Factor | 1.5 | 2.0 | 2.5 |
| Win/Loss Ratio | 2.0 | 2.5 | 3.5 |
| Positive Years % | 70% | 75% | 85% |
| Avg Recovery | 12 mo | 9 mo | 6 mo |

**Overall Rating Based on Total Score:**
- 27-30 points: EXCELLENT (deploy immediately)
- 23-26 points: GOOD (deploy with standard monitoring)
- 19-22 points: ACCEPTABLE (deploy with enhanced monitoring)
- 15-18 points: MARGINAL (paper trade first)
- <15 points: FAIL (do not deploy)

**7 Critical Red Flags (Instant Disqualification):**

1. **Curve-Fitted Equity Curve**
   - Indicator: >75% positive months, too smooth
   - Cause: Look-ahead bias, overfitting
   - Action: Reject, review code for bugs

2. **Single Trade Dominance**
   - Indicator: >50% of total return from one trade
   - Cause: Luck, not skill; not repeatable
   - Action: Reject strategy

3. **Excessive Win Rate**
   - Indicator: Win rate >70% in trend following
   - Cause: Overfitting to in-sample data
   - Action: Review code, check for data snooping

4. **Extreme Parameter Selection**
   - Indicator: Optimal parameters at grid extremes (edges)
   - Cause: Search space too narrow, true optimum outside range
   - Action: Expand parameter grid, re-optimize

5. **In-Sample vs Out-of-Sample Collapse**
   - Indicator: Out-of-sample Sharpe <50% of in-sample Sharpe
   - Cause: Severe overfitting
   - Action: Reject parameters, use simpler model

6. **Zero Losing Years**
   - Indicator: No down years over 5+ year period
   - Cause: Unrealistic, cherry-picked data period
   - Action: Test on different time periods

7. **Unrealistic Trade Count**
   - Indicator: <100 trades or >2,000 trades over 5 years
   - Cause: Parameters too strict or overtrading
   - Action: Adjust parameters or question strategy logic

**Benchmark Comparisons:**
- Primary: SPY (S&P 500) - must beat on risk-adjusted basis
- Secondary: 60/40 Portfolio (60% stocks, 40% bonds)
- Tertiary: Naive Trend Strategy (simple SMA crossover on SPY)

**Output:** 
- Validation report (PDF) with Pass/Fail for each test
- Overall score (0-30 points) with rating
- Red flag analysis with severity assessment
- Benchmark comparison charts
- Recommendation for next steps

### Script 20: Out-of-Sample Validator
**Purpose:** Validate strategy performance on unseen data to detect overfitting

**Why Out-of-Sample Validation is Critical:**
- In-sample: Parameters optimized on this data (can overfit)
- Out-of-sample: Strategy never saw this data (true test of robustness)
- In-sample results are hypotheses; out-of-sample results are evidence

**Walk-Forward Validation Method:**

**Setup:**
- In-Sample Period: 24 months (optimization window)
- Out-of-Sample Period: 6 months (testing window)
- Roll Forward: 6 months (shift window forward)
- Total timeline: 5 years (60 months) = 6 windows

**Window Structure:**
```
Window 1: Train on months 1-24, test on months 25-30
Window 2: Train on months 7-30, test on months 31-36
Window 3: Train on months 13-36, test on months 37-42
Window 4: Train on months 19-42, test on months 43-48
Window 5: Train on months 25-48, test on months 49-54
Window 6: Train on months 31-54, test on months 55-60
```

**Process for Each Window:**
1. Optimize parameters on in-sample period (find best Sharpe ratio)
2. Lock parameters (no changes allowed)
3. Test on out-of-sample period with locked parameters
4. Record out-of-sample performance
5. Analyze parameter changes between windows

**Key Metrics Calculated:**

**1. Stability Ratio**
```
Stability Ratio = Average OOS Sharpe / Average IS Sharpe

Interpretation:
  Excellent: Ratio >0.8 (minimal degradation)
  Good: Ratio 0.7-0.8
  Acceptable: Ratio 0.6-0.7
  Fail: Ratio <0.6 (overfitted)
```

**2. OOS Consistency**
```
Percentage of OOS windows that are profitable

Target: ≥70% of windows positive
Good: ≥80% of windows positive
Fail: <60% of windows positive
```

**3. Parameter Stability**
```
Coefficient of Variation = Std Dev / Mean

For each parameter (e.g., SMA_fast):
  Calculate CV across all windows
  
Excellent: CV <10% (very stable)
Good: CV 10-20%
Poor: CV >20% (unstable, no clear optimum)
```

**4. Performance Trend Analysis**
```
Plot OOS returns by window
Fit linear regression line

Acceptable: Slope not significantly negative (p >0.1)
Warning: Slope significantly negative (strategy decaying)
```

**Red Flags in Out-of-Sample Validation:**

**Red Flag 1: Cliff Performance Drop**
- In-Sample Sharpe: 2.0
- Out-of-Sample Sharpe: 0.3
- Interpretation: Strategy completely overfitted
- Action: Reject

**Red Flag 2: Parameter Instability**
- Window 1 optimal SMA: 50
- Window 2 optimal SMA: 200
- Window 3 optimal SMA: 30
- Interpretation: No stable optimal parameters exist
- Action: Simplify strategy, reduce parameters

**Red Flag 3: Declining OOS Performance**
- >40% of OOS windows are negative
- Interpretation: Strategy not robust to regime changes
- Action: Add regime filters or reject

**Holdout Period Validation:**
- Reserve last 6-12 months of data (never used in optimization)
- Test on holdout period ONCE after finalizing parameters
- Threshold: Holdout Sharpe ≥0.5 × Backtest Sharpe
- This is the ultimate validation test

**Output:**
- Out-of-sample validation report (PDF)
- Stability ratio for each metric
- OOS consistency percentage
- Parameter convergence charts
- Window-by-window performance table
- Holdout period results
- Pass/Fail recommendation

### Script 21: Deployment Decision Engine
**Purpose:** Make systematic Go/No-Go deployment decision based on all validation results

**Monte Carlo Validation Component:**

**Purpose:** Assess whether backtest result was skill or luck

**Method:**
1. Extract all 500 historical trades from backtest
2. Randomly shuffle trade sequence 10,000 times
3. For each simulation: apply trades in random order, calculate final equity
4. Analyze distribution of 10,000 outcomes

**Key Metrics from Monte Carlo:**

**1. Percentile Rank of Actual Result**
```
Where does actual backtest rank in distribution?

Excellent: 90th+ percentile (very lucky, adjust expectations down)
Good: 70-90th percentile (above average)
Acceptable: 50-70th percentile (average, realistic)
Poor: 30-50th percentile (below average)
Fail: <30th percentile (unlucky or overfitted)
```

**2. 95% Confidence Interval**
```
Range where 95% of outcomes fall

Example: [€48,000, €95,000] from €50,000 starting capital
Interpretation: 95% chance final equity in this range

Narrow range = predictable (good)
Wide range = unpredictable (bad)

Relative Width = (Upper - Lower) / Starting Capital
  Excellent: <50%
  Good: 50-100%
  Poor: >100%
```

**3. Tail Risk Analysis (5th Percentile)**
```
Worst-case scenario (bottom 5% of outcomes)

Acceptable: 5th percentile > -20% loss
Unacceptable: 5th percentile < -30% loss
```

**4. Probability of Target Return**
```
What % of simulations achieved ≥50% return?

Excellent: >75% probability
Good: 60-75% probability
Poor: <50% probability (coin flip)
```

**10-Step Decision Process:**

**Step 1:** Run complete backtest suite
- Script 16: Backtest Engine (5-year historical)
- Script 17: Walk-Forward Optimizer (6 rolling windows)
- Script 18: Monte Carlo Simulator (10,000 runs)

**Step 2:** Apply 10 primary validation tests (Script 19)
- Record Pass/Fail for each test
- If <8 tests pass → STOP, REJECT strategy immediately

**Step 3:** Calculate overall score (Script 19)
- Score each metric 0-3 points
- Sum across all 10 metrics (max 30 points)
- Determine rating: EXCELLENT/GOOD/ACCEPTABLE/MARGINAL/FAIL

**Step 4:** Check for critical red flags (Script 19)
- Review all 7 red flag criteria
- If any red flag present → Investigate thoroughly, likely REJECT

**Step 5:** Benchmark comparison (Script 19)
- Compare to SPY, 60/40 portfolio, naive trend
- Must beat SPY on risk-adjusted basis (Sharpe ratio)
- Count how many benchmarks beaten

**Step 6:** Out-of-sample validation (Script 20)
- Check walk-forward results
- Calculate stability ratio
- Verify OOS consistency
- Analyze parameter stability

**Step 7:** Monte Carlo validation (Script 18 + Script 21)
- Check percentile rank
- Analyze tail risk (5th percentile)
- Verify confidence interval width
- Assess probability of target return

**Step 8:** Apply 5-Tier Decision Matrix
- Synthesize all evidence from Steps 1-7
- Assign strategy to deployment tier

**Step 9:** Generate comprehensive validation report
- Executive summary with tier assignment
- Detailed test results
- Comparative analysis vs benchmarks
- Risk assessment and stress tests
- Recommendation with rationale and caveats

**Step 10:** Obtain required approvals
- Portfolio Manager (strategy logic, expected returns)
- Risk Manager (risk limits, stress test results)
- Compliance (regulatory review)
- All three must approve before deployment

**5-Tier Decision Matrix (Detailed):**

**Tier 1: DEPLOY IMMEDIATELY**
```
Requirements (ALL must be met):
  ✓ All 10 primary tests PASS
  ✓ Overall score ≥23 points (GOOD or better)
  ✓ Beats SPY on risk-adjusted basis
  ✓ No critical red flags
  ✓ Stability ratio >0.7
  ✓ Parameter stability confirmed (CV <20%)
  ✓ Monte Carlo 5th percentile > -20%

Deployment:
  - Full capital allocation to strategy
  - Standard position sizes as calculated
  - Monthly monitoring frequency
  - Quarterly re-validation

Risk Level: LOW
Expected Success Rate: 85-95%
```

**Tier 2: DEPLOY WITH MONITORING**
```
Requirements:
  ✓ All 10 primary tests PASS
  ✓ Overall score 19-22 points (ACCEPTABLE)
  ✓ Beats benchmark on risk-adjusted basis
  ✓ No critical red flags
  ✓ 1-2 warning signs present
  ✓ Stability ratio 0.6-0.7

Deployment Conditions:
  - Reduce initial position sizes by 10-20%
  - Tighten stop-losses by 10%
  - Weekly monitoring (first 3 months)
  - Monthly monitoring thereafter
  - Quarterly re-validation required
  - Conservative ramp-up (increase sizes gradually)

Risk Level: MODERATE
Expected Success Rate: 70-85%
```

**Tier 3: PAPER TRADE FIRST**
```
Requirements:
  ✓ 8-9 out of 10 primary tests PASS
  ✓ Overall score 15-18 points (MARGINAL)
  ✗ Barely beats benchmark OR
  ✗ 2-3 warning signs present OR
  ✗ Stability ratio 0.5-0.6

Deployment:
  - Paper trade for 3-6 months
  - Generate real-time recommendations but don't execute
  - Track how recommendations would perform
  - Monitor slippage vs backtest

Success Criteria for Moving to Tier 2:
  - Paper trading results match backtest within 20%
  - No new red flags emerge
  - All warning signs investigated and explained
  - Sharpe ratio in paper trading ≥0.5

If Successful: Move to Tier 2
If Unsuccessful: Move to Tier 4

Risk Level: MODERATE-HIGH
Expected Success Rate: 50-70%
```

**Tier 4: IMPROVE AND RETEST**
```
Requirements:
  ✗ <8 primary tests pass OR
  ✗ Overall score <15 points OR
  ✗ Does not beat benchmark OR
  ✗ 1+ critical red flags present

Action: DO NOT DEPLOY

Improvement Steps:
  1. Identify root cause of failures
  2. Simplify strategy (reduce parameters)
  3. Expand backtest period (test on more data)
  4. Adjust transaction cost assumptions
  5. Re-optimize with broader parameter ranges
  6. Address specific red flags

Loop: Repeat Steps 1-7 until reaches Tier 3 or higher
Abort: If cannot reach Tier 3 after 3 iterations, abandon strategy

Risk Level: HIGH
Expected Success Rate: 30-50% (after improvements)
```

**Tier 5: REJECT**
```
Requirements:
  ✗ Multiple critical red flags OR
  ✗ Fundamental logic errors OR
  ✗ Cannot be profitable with realistic costs OR
  ✗ Ethical concerns (market manipulation, etc.)

Action: REJECT permanently

Do not attempt to fix. Start over with different approach.
Document reasons for rejection for future reference.

Expected Success Rate: 0% (not fixable)
```

**Required Documentation (5 Reports):**

**1. Backtest Report (PDF)**
- Strategy description, parameters
- All performance metrics
- Equity curve and drawdown charts
- Complete trade list
- Monthly return table

**2. Validation Report (PDF)**
- All 10 primary test results (Pass/Fail)
- Scoring breakdown (30-point system)
- Red flag analysis with severity
- Benchmark comparison charts
- Final recommendation

**3. Parameter Selection Log (CSV + PDF)**
- All parameter combinations tested (972 combinations)
- Optimization results for each
- Selected parameters with justification
- Sensitivity analysis (±20% parameter changes)
- Parameter stability across windows

**4. Approval Record (PDF with digital signatures)**
- Complete validation report attached
- Sign-off from Portfolio Manager
- Sign-off from Risk Manager
- Sign-off from Compliance
- Date of approval
- Conditions and caveats noted
- Reassessment schedule specified

**5. Deployment Checklist (PDF)**
- Pre-deployment verification completed
- Configuration confirmed
- Initial position sizes calculated
- Monitoring setup configured
- Alert thresholds set
- Emergency procedures documented

**Retention Policy:**
- All documents retained indefinitely
- Complete audit trail for regulatory compliance
- Annual audit by external auditor

**Output from Script 21:**
- Deployment Decision Report (PDF, 30-50 pages)
  * Executive summary (2 pages)
  * Tier assignment with detailed rationale
  * Complete validation results
  * Monte Carlo analysis
  * Risk assessment
  * Deployment conditions
  * Required approvals section
  * Sign-off page
- Decision Dashboard (HTML)
  * Interactive visualization of all metrics
  * Traffic light indicators (red/yellow/green)
  * Drill-down capability into each test
  * Comparison charts vs benchmarks

---

## 8. PERFORMANCE MANAGEMENT LAYER (Scripts 22-24)

### Script 22: Performance Attribution
**Purpose:** Decompose returns by source

**Attribution Analysis:**
- Asset class contribution (stocks vs ETFs vs crypto)
- Sector contribution (which sectors added/subtracted value)
- Position-level contribution (which positions drove returns)
- Alpha vs Beta (skill vs market exposure)
- Active return (strategy vs benchmark)

**Output:** Attribution report showing return sources

### Script 23: Risk Analytics
**Purpose:** Measure portfolio risk characteristics

**Risk Metrics:**
- Value at Risk (VaR) - 1-day, 5-day, 20-day
- Conditional Value at Risk (CVaR) - expected loss beyond VaR
- Beta to benchmark (SPY, ACWI)
- Tracking error vs benchmark
- Information ratio (active return / tracking error)
- Concentration metrics (Herfindahl index)
- Correlation matrix (top 10 positions)
- Factor exposures (size, value, momentum, quality)

**Output:** Comprehensive risk report

### Script 24: Performance Dashboard
**Purpose:** Visualize portfolio performance

**Dashboard Components:**
- Equity curve (cumulative return over time)
- Monthly return heatmap (calendar view)
- Drawdown chart (underwater plot)
- Rolling Sharpe ratio (12-month window)
- Asset allocation pie chart
- Sector allocation bar chart
- Position-level P&L table
- Top winners and losers
- Trade history timeline

**Output:** Interactive HTML dashboard

---

# DATA ARCHITECTURE

## Data Flow

```
External Sources
    ↓
[Script 1: EODHD Bulk Download] → Raw OHLCV data
    ↓
[Script 2: Yahoo Fundamentals] → Company metadata
    ↓
[Script 3: Consolidate & Validate] → Unified time series
    ↓
[Script 4: Screen Universe] → Qualified instruments
    ↓
[Script 5: Calculate Indicators] → Technical indicators
    ↓
[Script 6: Qualify Trends] → Trend-qualified instruments
    ↓
[Script 7: Rank Momentum] → Ranked by momentum
    ↓
[Scripts 8-10: Size, Stops, Exits] → Position specifications
    ↓
[Script 11: Monthly Rebalancing] → Recommendations
    ↓
[Script 12: Generate Report] → Human approval
    ↓
[Script 13: Log Execution] → Trade records
    ↓
[Script 14: Daily Monitoring] → Risk alerts
    ↓
[Script 15: Technical Charts] → Visual analysis

BACKTESTING & VALIDATION FLOW (Parallel):
[Scripts 1-5: Historical Data] 
    ↓
[Script 16: Backtest Engine] → Backtest results
    ↓
[Script 17: Walk-Forward Optimizer] → Optimal parameters
    ↓
[Script 18: Monte Carlo Simulator] → Risk distributions
    ↓
[Script 19: Backtest Validator] → Validation results
    ↓
[Script 20: Out-of-Sample Validator] → Stability analysis
    ↓
[Script 21: Deployment Decision Engine] → Go/No-Go decision
    ↓
Human Approval (PM, Risk, Compliance)
    ↓
Deploy to Production OR Reject

PERFORMANCE MANAGEMENT FLOW (Live Trading):
[Script 13: Trade Executions]
    ↓
[Script 22: Performance Attribution] → Return decomposition
    ↓
[Script 23: Risk Analytics] → Risk metrics
    ↓
[Script 24: Performance Dashboard] → Visual reporting
```

## Data Storage Structure

```
data_cache/
├── raw_bulk/{exchange}/{date}.parquet
├── fundamentals/company_info.json
├── consolidated/{symbol}.parquet
├── indicators/{symbol}_indicators.parquet
├── qualified/qualified_symbols.json
├── signals/momentum_ranked.json
├── portfolio/position_sizes.json
├── metadata/
│   ├── last_update.json
│   ├── new_symbols.jsonl
│   └── delisted_symbols.jsonl
└── backtest/
    ├── results/{backtest_id}.json
    ├── trades/{backtest_id}_trades.csv
    ├── equity_curve/{backtest_id}_equity.csv
    ├── walk_forward/{backtest_id}_windows.json
    └── monte_carlo/{backtest_id}_simulations.json

data/
├── portfolio_state.json
├── executions/{date}_trades.json
└── performance/
    ├── attribution/{date}_attribution.json
    ├── risk/{date}_risk.json
    └── dashboard/{date}_dashboard.html

reports/
├── rebalancing/{YYYY-MM}_recommendations.pdf
├── daily/{date}_monitoring.json
├── charts/technical_analysis_{timestamp}.html
├── backtest/
│   ├── backtest_report_{id}.pdf
│   ├── validation_report_{id}.pdf
│   ├── parameter_selection_{id}.csv
│   ├── approval_record_{id}.pdf
│   └── deployment_checklist_{id}.pdf
├── performance/monthly_performance_{YYYY-MM}.pdf
└── validation/
    ├── primary_tests_{id}.json
    ├── oos_validation_{id}.json
    └── deployment_decision_{id}.pdf
```

---

# TECHNICAL INDICATORS & FORMULAS

## 1. Simple Moving Average (SMA)

**Formula:**
```
SMA(n) = (Close[0] + Close[1] + ... + Close[n-1]) / n
```

**Explanation:**
Average of the last n closing prices. Smooths out price action to identify trend direction.

**Parameters Used:**
- SMA_50: 50-day moving average (short-term trend)
- SMA_200: 200-day moving average (long-term trend)

**Interpretation:**
- SMA_50 > SMA_200 → Uptrend (golden cross)
- SMA_50 < SMA_200 → Downtrend (death cross)
- Price > SMA → Bullish momentum
- Price < SMA → Bearish momentum

**Why These Parameters:**
- 50 and 200 days are industry-standard periods
- Well-tested in academic literature (Faber, 2007)
- Balance between responsiveness and stability

---

## 2. Average True Range (ATR)

**Formula:**
```
True Range (TR) = max(
    High - Low,
    |High - Previous Close|,
    |Low - Previous Close|
)

ATR(n) = SMA(TR, n)

ATR_Percentage = (ATR / Close) × 100
```

**Explanation:**
Measures volatility by capturing the largest daily price range, including gaps. Expressed as percentage of price for comparability across instruments.

**Parameters Used:**
- ATR_20: 20-day average true range

**Interpretation:**
- High ATR → High volatility (wider stops needed)
- Low ATR → Low volatility (tighter stops possible)
- ATR_Pct typical ranges: 1-3% (low vol) to 5-10% (high vol)

**Why This Parameter:**
- 20 days captures recent volatility without being too noisy
- Percentage normalization allows cross-instrument comparison
- Used for position sizing and stop-loss placement

---

## 3. Average Directional Index (ADX)

**Formula:**
```
+DM = High[today] - High[yesterday] (if positive, else 0)
-DM = Low[yesterday] - Low[today] (if positive, else 0)

+DI = 100 × EMA(+DM, n) / ATR(n)
-DI = 100 × EMA(-DM, n) / ATR(n)

DX = 100 × |+DI - -DI| / (+DI + -DI)

ADX = EMA(DX, n)
```

**Explanation:**
Measures trend strength regardless of direction. Higher ADX = stronger trend. Does not indicate trend direction, only strength.

**Parameters Used:**
- ADX_14: 14-day average directional index

**Interpretation:**
- ADX > 25 → Strong trend
- ADX 20-25 → Moderate trend
- ADX < 20 → Weak trend or ranging market
- Rising ADX → Trend strengthening
- Falling ADX → Trend weakening

**Why This Parameter:**
- 14 days is Wilder's original parameter (creator of ADX)
- Threshold of 20 filters out weak trends
- Helps avoid whipsaws in ranging markets

---

# STRATEGY RULES & FORMULAS

## 1. Trend Qualification

**Rule:** ALL conditions must be TRUE

```
Condition 1: SMA_50 > SMA_200 (golden cross exists)
Condition 2: Close > SMA_50 (price above short-term trend)
Condition 3: ADX_14 > 20 (sufficient trend strength)
```

**Explanation:**

**Condition 1 (Golden Cross):**
- Ensures long-term trend is up
- SMA_50 crossing above SMA_200 is classic bullish signal
- Filters out instruments in downtrends

**Condition 2 (Price Above SMA_50):**
- Confirms current momentum is bullish
- Avoids entering when price is pulling back
- Ensures we're buying strength, not weakness

**Condition 3 (ADX Threshold):**
- Confirms trend has sufficient strength
- ADX < 20 indicates ranging/choppy market
- Reduces false signals and whipsaws

**Why This Combination:**
- Three independent confirmations reduce false positives
- Each filter addresses different aspect (long-term, short-term, strength)
- Academic support (Kaufman, 2013; Pruitt & Hill, 1992)

---

## 2. Momentum Score

**Formula:**
```
Momentum Score = ((Close - SMA_200) / SMA_200) × 100
```

**Explanation:**
Percentage distance of current price above the 200-day moving average. Higher score = stronger momentum.

**Example:**
- Close = €115
- SMA_200 = €100
- Momentum Score = ((115 - 100) / 100) × 100 = 15.0%

**Interpretation:**
- Score 0-5% → Weak momentum
- Score 5-15% → Moderate momentum
- Score 15-30% → Strong momentum
- Score > 30% → Very strong momentum (potential overextension)

**Why This Formula:**
- Simple and transparent
- Captures long-term trend strength
- Comparable across all instruments
- Validated in academic research (Moskowitz et al., 2012)

**Alternative Considered:**
- Rate of Change (3m, 6m, 12m): Too sensitive to entry timing
- Rejected in favor of SMA_200 distance for stability

---

## 3. Position Sizing

**Formula:**
```
Step 1: Base Risk
Base_Risk = Account_Equity × Target_Risk_Per_Position
where Target_Risk_Per_Position = 2.0%

Step 2: Volatility Adjustment
Volatility_Multiplier = Median_ATR / Instrument_ATR

Step 3: Raw Position Value
Raw_Position_Value = Base_Risk × Volatility_Multiplier

Step 4: Apply Floor and Ceiling
Final_Position_Value = max(
    Account_Equity × 0.5%,  (floor)
    min(
        Raw_Position_Value,
        Account_Equity × 8.0%  (ceiling)
    )
)

Step 5: Convert to Shares
Shares = floor(Final_Position_Value / Instrument_Price)
Actual_Position_Value = Shares × Instrument_Price
```

**Explanation:**

**Step 1 (Base Risk):**
- Target 2% of account equity per position
- Industry standard for trend following
- Example: €50,000 account → €1,000 base risk per position

**Step 2 (Volatility Adjustment):**
- Inverse ATR weighting: Lower volatility → Larger position
- Normalizes risk across instruments
- Example: Median ATR = 2.0%, Instrument ATR = 2.5% → Multiplier = 0.8

**Step 3 (Raw Position Value):**
- Base risk adjusted for instrument's volatility
- Example: €1,000 × 0.8 = €800

**Step 4 (Floor and Ceiling):**
- Floor (0.5%): Prevents dust positions
- Ceiling (8.0%): Prevents concentration risk
- Example: Floor = €250, Ceiling = €4,000

**Step 5 (Shares):**
- Floor operation prevents fractional shares
- Example: €800 / €100 = 8 shares → €800 actual value

**Why This Method:**
- Risk parity across positions (equal volatility-adjusted risk)
- Prevents over-allocation to low-volatility instruments
- Prevents under-allocation to high-volatility instruments
- Floor ensures positions are meaningful
- Ceiling prevents excessive concentration

---

## 4. Initial Stop-Loss

**Formula:**
```
Initial_Stop = Entry_Price - (Stop_Multiplier × ATR)

where Stop_Multiplier = 3.0
```

**Explanation:**
Initial stop is set 3 times the ATR below entry price. This gives the position room to fluctuate while limiting maximum loss.

**Example:**
- Entry Price = €100
- ATR = €2.50
- Initial Stop = €100 - (3.0 × €2.50) = €92.50
- Stop Distance = 7.5%

**Characteristics:**
- Set once at entry, never moves down
- Tight stop (3× ATR vs 4× for trailing)
- Exit triggered if close ≤ stop_price on ANY day

**Why 3× ATR:**
- 2× ATR: Too tight, frequent whipsaws
- 4× ATR: Too loose, large losses
- 3× ATR: Balance between protection and flexibility
- Empirical testing shows optimal tradeoff

---

## 5. Trailing Stop-Loss

**Activation Rule:**
```
Trailing stop activates when:
Current_Price ≥ Entry_Price × 1.15 (i.e., +15% profit)
```

**Calculation Formula:**
```
New_Trailing_Stop = Current_Price - (Trailing_Multiplier × ATR)

where Trailing_Multiplier = 4.0

Final_Trailing_Stop = max(Current_Trailing_Stop, New_Trailing_Stop)
(ratchet mechanism - only moves up)
```

**Update Frequency:**
```
Updated every Friday market close
Exit if daily close < trailing_stop
```

**Explanation:**

**Activation (+15% Profit):**
- Trailing stop not active until position is profitable
- 15% threshold ensures significant profit before switching to trailing mode
- Prevents premature activation during initial volatility

**4× ATR Distance:**
- Wider than initial stop (3× ATR)
- Gives winning trades room to breathe
- Reduces risk of being stopped out during normal pullbacks

**Ratchet Mechanism:**
- Stop only moves UP, never down
- Locks in profits as price rises
- Creates asymmetric risk/reward (limited loss, unlimited gain)

**Weekly Updates:**
- Updated Friday close only (not daily)
- Reduces noise and overtrading
- Aligns with weekly planning cycle

**Example:**
```
Week 1:
- Entry = €100
- Initial Stop = €92.50 (3× ATR)

Week 4:
- Current Price = €120 (+20% profit)
- Trailing activates (>15% profit threshold)
- ATR = €3.00
- Trailing Stop = €120 - (4.0 × €3.00) = €108
- Locked in profit: €108 - €100 = €8 per share

Week 8:
- Current Price = €135
- ATR = €3.50
- New Trailing = €135 - (4.0 × €3.50) = €121
- Final Trailing = max(€108, €121) = €121
- Locked in profit: €121 - €100 = €21 per share
```

**Why This Design:**
- Protects profits without choking winners
- Weekly updates reduce noise
- Wider stop (4× vs 3×) appropriate for trending positions
- Empirical testing shows optimal performance

---

## 6. Entry Timing

**Rule:**
```
Entry occurs when ALL conditions are TRUE:

1. Today is monthly rebalancing date (last trading day of month)
2. Instrument passed trend qualification
3. Instrument is in top N by momentum score
4. Instrument is not already in portfolio
```

**Execution:**
```
Order Type: Limit order
Limit Price = Rebalancing_Date_Close × 1.005 (close + 0.5%)
Order Placed: End of rebalancing day
Expected Execution: First trading day of new month
Expiration: Cancel if not filled within 2 trading days
```

**Explanation:**

**Monthly Rebalancing:**
- Reduces transaction costs (vs daily rebalancing)
- Aligns with monthly data reporting cycles
- Prevents overtrading

**Top N Selection:**
- N varies by account size (10-25 positions)
- Only strongest momentum instruments selected
- Natural diversification through momentum spread

**Limit Order (Close + 0.5%):**
- Protects against gap-ups
- 0.5% buffer allows for normal market movement
- Cancel if price runs away (preserves discipline)

**Why These Rules:**
- Clear, unambiguous entry criteria
- No discretion or interpretation needed
- Monthly frequency reduces costs while capturing trends

---

## 7. Exit Prioritization

**Priority 1 (Highest): Stop-Loss Hit**
```
Rule: Close ≤ Stop_Price
Action: Exit at next market open (market order)
Rationale: Risk management takes absolute priority
```

**Priority 2: Trend Reversal**
```
Rule: SMA_50 < SMA_200 (death cross)
Action: Exit at next market open (market order)
Rationale: Long-term trend has turned bearish
```

**Priority 3: Trend Weakness**
```
Rule: ADX < 15 for 3 consecutive days
Action: Exit at next market open (market order)
Rationale: Trend has weakened significantly
```

**Priority 4 (Lowest): Rebalancing Rotation**
```
Rule: Instrument dropped from top N by momentum
Action: Exit at month-end close (market order)
Rationale: Better opportunities available
```

**Explanation:**

**Why Prioritized:**
- Higher priority = more urgent exit
- Stop-loss never waits (immediate risk)
- Trend signals wait until month-end unless critical
- Rotation exits only at rebalancing (lowest urgency)

**Why These Exit Rules:**
- Stop-loss: Limits losses to predetermined level
- Trend reversal: Exits before significant decline
- Trend weakness: Exits during consolidation (dead money)
- Rotation: Reallocates to stronger opportunities

---

# RISK MANAGEMENT FORMULAS

## 1. Maximum Position Size

**Formula:**
```
Maximum Position Size = Account_Equity × 8.0%
```

**Explanation:**
No single position can exceed 8% of account equity, regardless of volatility adjustment.

**Rationale:**
- Prevents concentration risk
- Typical portfolio: 25 positions × 4% = 100% (target)
- Maximum: 12.5 positions × 8% = 100% (worst case)
- Ensures diversification

---

## 2. Minimum Position Size

**Formula:**
```
Minimum Position Size = Account_Equity × 0.5%
```

**Explanation:**
Position must be at least 0.5% of account or not taken.

**Rationale:**
- Prevents dust positions
- Ensures meaningful impact on portfolio
- Transaction costs proportional to position size
- Minimum: 200 positions × 0.5% = 100% (theoretical max)

---

## 3. Asset Class Limits

**Formula:**
```
Asset Class Allocation Ranges:

Stocks: 0% - 50% (target: 42%)
ETFs: 0% - 45% (target: 38%)
Crypto: 0% - 20% (target: 15%)
Cash: ≥ 5% (reserve)
```

**Explanation:**
Diversification across asset classes reduces correlation and concentration risk.

**Rationale:**
- Stocks: Primary return driver
- ETFs: Diversification, lower volatility
- Crypto: High return potential, limited exposure
- Cash: Liquidity reserve for opportunities

---

## 4. Sector Concentration Limit

**Formula:**
```
Maximum Single Sector Allocation = 30%
```

**Explanation:**
No more than 30% of portfolio in any single sector (Technology, Healthcare, Finance, etc.).

**Rationale:**
- Prevents sector-specific risk
- Example: Tech crash doesn't wipe out portfolio
- Forces diversification across economic sectors

---

## 5. Correlation Warning Threshold

**Formula:**
```
Correlation Warning = Average pairwise correlation of top 3 positions > 0.70
```

**Explanation:**
If the top 3 positions have correlation > 0.70, the system triggers a warning flag.

**Rationale:**
- High correlation = concentrated risk
- Top 3 positions typically represent 12-24% of portfolio
- Correlation > 0.70 means they move together (not diversified)
- Warning prompts review of position selection

---

## 6. Portfolio-Level Circuit Breakers

### Circuit Breaker 1: Drawdown Limit
```
Trigger: (Current_Equity / Peak_Equity - 1) < -15%
Action: Halt all new entries for 5 trading days
Rationale: Large drawdown indicates strategy not working in current regime
```

### Circuit Breaker 2: VIX Spike
```
Trigger: VIX > 40
Action: Halt new entries until VIX < 30 for 3 consecutive days
Rationale: Extreme volatility = unreliable signals
```

### Circuit Breaker 3: Correlation Breakdown
```
Trigger: Pairwise correlation of top 10 positions > 0.85
Action: Halt new entries, flag for review
Rationale: High correlation = concentrated risk, not diversified
```

### Circuit Breaker 4: Concentration Creep
```
Trigger: Top 3 positions > 30% of portfolio
Action: Halt new entries, force rebalancing
Rationale: Excessive concentration violates risk limits
```

### Circuit Breaker 5: Data Staleness
```
Trigger: Any position has data > 3 trading days old
Action: Halt all trading, alert operator
Rationale: Stale data = trading blind
```

**Explanation:**
Circuit breakers prevent system from executing into disaster during extreme conditions. Human override possible with logged rationale.

---

# BACKTESTING FRAMEWORK

## 1. Backtesting Principles

**No Look-Ahead Bias:**
- Only use information available at time of decision
- Calculate indicators using data up to rebalancing date only
- No peeking at future prices

**Walk-Forward Methodology:**
- Start with initial capital (e.g., €50,000)
- Apply strategy rules at each rebalancing date
- Execute simulated trades at realistic prices
- Update portfolio state
- Move forward to next rebalancing date
- Repeat for entire backtest period

**Realistic Execution:**
- Entry: Limit order at close + 0.5% (may not fill)
- Exit: Market order at next open (realistic slippage)
- Transaction costs: 0.1% per trade (conservative)
- Slippage: 0.05% for liquid stocks, 0.1% for ETFs

**Corporate Action Handling:**
- Adjust for stock splits (price and quantity)
- Reinvest dividends (buy more shares)
- Handle delisted stocks (exit at last available price)

---

## 2. Performance Metrics

### Return Metrics

**Total Return:**
```
Total Return = (Final_Equity - Initial_Equity) / Initial_Equity × 100
```

**Annualized Return:**
```
Annualized Return = (Final_Equity / Initial_Equity)^(252 / Trading_Days) - 1 × 100
```

**Compound Annual Growth Rate (CAGR):**
```
CAGR = (Final_Equity / Initial_Equity)^(1 / Years) - 1 × 100
```

### Risk-Adjusted Return Metrics

**Sharpe Ratio:**
```
Sharpe Ratio = (Portfolio_Return - Risk_Free_Rate) / Portfolio_Volatility

where:
Portfolio_Return = annualized return
Risk_Free_Rate = 3-month T-bill rate
Portfolio_Volatility = standard deviation of daily returns (annualized)
```

**Interpretation:**
- Sharpe > 2.0: Excellent
- Sharpe 1.0-2.0: Good
- Sharpe 0.5-1.0: Acceptable
- Sharpe < 0.5: Poor

**Sortino Ratio:**
```
Sortino Ratio = (Portfolio_Return - Risk_Free_Rate) / Downside_Deviation

where:
Downside_Deviation = standard deviation of negative returns only
```

**Interpretation:**
- Similar to Sharpe but penalizes downside volatility only
- More relevant for investors concerned with losses

**Calmar Ratio:**
```
Calmar Ratio = CAGR / Maximum_Drawdown
```

**Interpretation:**
- Measures return per unit of drawdown risk
- Calmar > 1.0: Good
- Calmar > 2.0: Excellent

### Drawdown Metrics

**Maximum Drawdown:**
```
Drawdown[t] = (Equity[t] - Peak_Equity[0:t]) / Peak_Equity[0:t]

Maximum Drawdown = min(Drawdown[t]) for all t
```

**Interpretation:**
- Worst peak-to-trough decline
- Example: -25% max drawdown means portfolio lost 25% from peak

**Average Drawdown:**
```
Average Drawdown = mean of all drawdown periods
```

**Drawdown Duration:**
```
Maximum time from peak to recovery (in days)
```

### Trading Metrics

**Win Rate:**
```
Win Rate = Number_of_Winning_Trades / Total_Trades × 100
```

**Profit Factor:**
```
Profit Factor = Gross_Profit / Gross_Loss
```

**Interpretation:**
- Profit Factor > 2.0: Excellent
- Profit Factor 1.5-2.0: Good
- Profit Factor 1.0-1.5: Acceptable
- Profit Factor < 1.0: Losing system

**Average Win / Average Loss:**
```
Avg Win = mean(profit of winning trades)
Avg Loss = mean(loss of losing trades)
Win/Loss Ratio = Avg Win / Avg Loss
```

**Expectancy:**
```
Expectancy = (Win_Rate × Avg_Win) - (Loss_Rate × Avg_Loss)
```

**Interpretation:**
- Expected profit per trade
- Must be positive for profitable system

---

## 3. Walk-Forward Optimization

**Purpose:**
Optimize parameters on in-sample data, validate on out-of-sample data, to avoid overfitting.

**Process:**

**Step 1: Define Parameter Grid**
```
Parameters to optimize:
- SMA Fast: [30, 50, 100]
- SMA Slow: [150, 200, 300]
- ADX Threshold: [15, 20, 25]
- Initial Stop Multiplier: [2.5, 3.0, 3.5]
- Trailing Stop Multiplier: [3.5, 4.0, 4.5]
- Position Count: [10, 15, 20, 25]

Total combinations: 3 × 3 × 3 × 3 × 3 × 4 = 972 parameter sets
```

**Step 2: Walk-Forward Windows**
```
In-Sample Period: 24 months (optimize on this data)
Out-of-Sample Period: 6 months (test on this data)
Roll Forward: 6 months (shift window forward)

Example:
Window 1:
  In-Sample: Jan 2020 - Dec 2021 (optimize)
  Out-of-Sample: Jan 2022 - Jun 2022 (test)

Window 2:
  In-Sample: Jul 2020 - Jun 2022 (optimize)
  Out-of-Sample: Jul 2022 - Dec 2022 (test)

Continue rolling...
```

**Step 3: Optimize In-Sample**
```
For each parameter set:
  Run backtest on in-sample period
  Calculate Sharpe ratio
Select parameter set with highest in-sample Sharpe
```

**Step 4: Validate Out-of-Sample**
```
Use best parameter set from in-sample
Run backtest on out-of-sample period
Record out-of-sample Sharpe ratio
```

**Step 5: Evaluate Stability**
```
Stability Ratio = Out-of-Sample_Sharpe / In-Sample_Sharpe

Interpretation:
- Ratio > 0.8: Robust (parameters generalize well)
- Ratio 0.5-0.8: Acceptable degradation
- Ratio < 0.5: Overfitting (parameters don't generalize)
```

**Step 6: Select Final Parameters**
```
Aggregate all walk-forward windows
Select parameter set with:
  - Highest median out-of-sample Sharpe
  - Stability ratio > 0.8
  - Consistent performance across all windows
```

**Why This Method:**
- Prevents overfitting by testing on unseen data
- Mimics real-world deployment (optimize → deploy → test)
- Rolling windows test robustness across regimes
- Stability ratio identifies parameters that generalize

---

## 4. Monte Carlo Simulation

**Purpose:**
Assess strategy robustness by randomizing trade sequence while preserving individual trade outcomes.

**Method:**

**Step 1: Collect Historical Trades**
```
Extract all trades from backtest:
- Entry date, exit date
- Entry price, exit price
- Profit/loss (in %)
- Duration (days)

Example: 500 historical trades over 5 years
```

**Step 2: Randomize Trade Sequence**
```
For simulation i (i = 1 to 10,000):
  Randomly shuffle trade order
  Start with initial capital
  For each trade in shuffled order:
    Apply trade return to current equity
    Update current equity
  Record final equity for simulation i
```

**Step 3: Calculate Statistics**
```
From 10,000 simulations:
- Median final equity
- 5th percentile (worst case)
- 95th percentile (best case)
- Standard deviation
- Probability of specific outcomes

Example outcomes:
- Probability (Final Equity > €100,000) = 75%
- Probability (Max Drawdown > 25%) = 15%
- Probability (Negative Return) = 5%
```

**Step 4: Confidence Intervals**
```
95% Confidence Interval for Final Equity:
  Lower Bound = 2.5th percentile
  Upper Bound = 97.5th percentile

Example:
  95% confidence interval: [€75,000, €125,000]
  Interpretation: 95% chance final equity is in this range
```

**Why This Method:**
- Tests sensitivity to trade sequence
- Quantifies luck vs skill
- Provides realistic range of outcomes
- Identifies extreme scenarios (tail risk)

---

## 5. Backtesting Validation & Decision Framework

### Purpose of Validation

Backtesting produces metrics, but **metrics alone don't determine if a strategy should be deployed**. Validation answers five critical questions:

1. **Is the strategy profitable?** (absolute performance)
2. **Is the strategy better than alternatives?** (comparative performance)
3. **Is the strategy robust?** (stability across time and parameters)
4. **Is the strategy realistic?** (accounts for costs and implementation constraints)
5. **Should the strategy be deployed?** (Go/No-Go decision)

**Validation Philosophy:** *"A strategy must perform well consistently, realistically, and for the right reasons."*

---

### 10 Primary Validation Tests (All Must Pass)

Every backtest must pass these tests before deployment consideration:

#### Test 1: Positive Expectancy
```
Requirement: Strategy must beat cost of capital + inflation

Metric: Total Return over backtest period
Threshold: Total Return > 30% for 5-year backtest (6% annually)

✓ PASS: Total Return ≥ 30%
✗ FAIL: Total Return < 30%
```

#### Test 2: Risk-Adjusted Outperformance
```
Requirement: Must beat benchmark on risk-adjusted basis

Metric: Sharpe Ratio
Benchmark: SPY (S&P 500) buy-and-hold
Threshold: Strategy Sharpe > Benchmark Sharpe × 1.25

✓ PASS: Strategy Sharpe ≥ Required threshold
✗ FAIL: Strategy Sharpe < Required threshold
```

#### Test 3: Acceptable Maximum Drawdown
```
Requirement: Worst loss must be tolerable

Metric: Maximum Drawdown
Threshold: Max Drawdown ≤ -30% (absolute limit)

✓ EXCELLENT: Max Drawdown > -15%
✓ GOOD: Max Drawdown -15% to -20%
✓ ACCEPTABLE: Max Drawdown -20% to -25%
✗ WARNING: Max Drawdown -25% to -30%
✗ FAIL: Max Drawdown < -30%
```

#### Test 4: Sufficient Trade Count
```
Requirement: Statistical significance requires minimum sample size

Metric: Total number of completed trades
Threshold: ≥ 100 trades

✓ PASS: Trade count ≥ 100
✗ FAIL: Trade count < 100 (insufficient sample)
```

#### Test 5: Realistic Win Rate
```
Requirement: Win rate must be achievable (not suspiciously high)

Metric: Win Rate (% of profitable trades)
Threshold: 35% ≤ Win Rate ≤ 65%

✗ SUSPICIOUS: Win Rate > 65% (potential overfitting)
✓ PASS: Win Rate 35-65%
✗ FAIL: Win Rate < 35% (too many losses)
```

#### Test 6: Positive Profit Factor
```
Requirement: Gross profits must significantly exceed gross losses

Metric: Profit Factor = Gross Profit / Gross Loss
Threshold: Profit Factor ≥ 1.5

✓ EXCELLENT: Profit Factor ≥ 2.5
✓ GOOD: Profit Factor 2.0-2.5
✓ ACCEPTABLE: Profit Factor 1.5-2.0
✗ FAIL: Profit Factor < 1.5
```

#### Test 7: Sensible Win/Loss Ratio
```
Requirement: Winners larger than losers (trend following characteristic)

Metric: Average Win / Average Loss
Threshold: Ratio ≥ 2.0

✓ EXCELLENT: Ratio ≥ 3.0
✓ GOOD: Ratio 2.5-3.0
✓ ACCEPTABLE: Ratio 2.0-2.5
✗ FAIL: Ratio < 2.0
```

#### Test 8: Transaction Cost Sensitivity
```
Requirement: Strategy must remain profitable with higher costs

Test: Re-run backtest with 2× transaction costs (0.2% vs 0.1%)
Threshold: Return drops by < 50% when costs double

✓ PASS: Performance degradation < 50%
✗ FAIL: Performance degradation ≥ 50%
```

#### Test 9: Drawdown Recovery Time
```
Requirement: Must recover from losses in reasonable time

Metric: Average time from drawdown peak to recovery
Threshold: Average recovery ≤ 12 months

✓ EXCELLENT: Avg recovery ≤ 6 months
✓ GOOD: Avg recovery 6-9 months
✓ ACCEPTABLE: Avg recovery 9-12 months
✗ FAIL: Avg recovery > 12 months
```

#### Test 10: Annual Consistency
```
Requirement: Not dependent on single outlier year

Metric: Percentage of positive years
Threshold: ≥ 70% of years positive

✓ EXCELLENT: ≥ 80% positive years
✓ GOOD: 70-80% positive years
✗ FAIL: < 70% positive years
```

**Decision Rule:** If <8 tests pass → REJECT strategy immediately

---

### Performance Scoring System (30 Points)

Each metric receives 0-3 points based on performance level:

| Metric | Minimum (1 pt) | Target (2 pts) | Excellent (3 pts) |
|--------|----------------|----------------|-------------------|
| CAGR | 8% | 12% | 18% |
| Sharpe Ratio | 0.8 | 1.2 | 2.0 |
| Sortino Ratio | 1.0 | 1.5 | 2.5 |
| Calmar Ratio | 0.5 | 1.0 | 2.0 |
| Max Drawdown | -30% | -20% | -15% |
| Win Rate | 35% | 45% | 55% |
| Profit Factor | 1.5 | 2.0 | 2.5 |
| Win/Loss Ratio | 2.0 | 2.5 | 3.5 |
| Positive Years % | 70% | 75% | 85% |
| Avg Recovery | 12 mo | 9 mo | 6 mo |

**Overall Rating:**
- **27-30 points:** EXCELLENT (deploy immediately)
- **23-26 points:** GOOD (deploy with standard monitoring)
- **19-22 points:** ACCEPTABLE (deploy with enhanced monitoring)
- **15-18 points:** MARGINAL (paper trade first)
- **< 15 points:** FAIL (do not deploy)

---

### Critical Red Flags (Instant Disqualification)

Any of these red flags requires immediate rejection or thorough investigation:

**Red Flag 1: Curve-Fitted Equity Curve**
- Equity curve too smooth, lacks realistic volatility
- >75% positive months (unrealistic consistency)
- **Cause:** Look-ahead bias, overfitting

**Red Flag 2: Single Trade Dominance**
- >50% of total return from one trade
- **Cause:** Luck, not skill; not repeatable

**Red Flag 3: Excessive Win Rate**
- Win rate >70% in trend following strategy
- **Cause:** Overfitting to in-sample data

**Red Flag 4: Extreme Parameter Selection**
- Optimal parameters at edge of tested grid
- **Cause:** Search space too narrow, true optimum outside range

**Red Flag 5: Out-of-Sample Collapse**
- Out-of-sample performance <50% of in-sample
- **Cause:** Severe overfitting

**Red Flag 6: Zero Losing Years**
- No down years over 5+ year period
- **Cause:** Unrealistic, cherry-picked data period

**Red Flag 7: Unrealistic Trade Count**
- <100 trades or >2,000 trades over 5 years
- **Cause:** Parameters too strict or overtrading

**Action:** Any red flag → Investigate thoroughly, likely REJECT

---

### Out-of-Sample Validation Requirements

**In-Sample:** Data used for parameter optimization (can overfit)
**Out-of-Sample:** Data never used in optimization (true test)

**Walk-Forward Validation Method:**
```
Rolling Windows:
  Window 1: Train on 24 months → Test on 6 months
  Window 2: Roll forward 6 months, repeat
  Continue for 6+ windows

Aggregate all out-of-sample periods
Calculate Stability Ratio = OOS Sharpe / IS Sharpe
```

**Validation Thresholds:**
```
Stability Ratio Requirements:
  ✓ EXCELLENT: Ratio > 0.8 (20% or less degradation)
  ✓ GOOD: Ratio 0.7-0.8
  ✓ ACCEPTABLE: Ratio 0.6-0.7
  ✗ FAIL: Ratio < 0.6 (overfitted)

OOS Consistency:
  ✓ PASS: ≥70% of OOS windows profitable
  ✗ FAIL: <70% of OOS windows profitable
```

---

### Monte Carlo Validation Requirements

**Percentile Rank of Actual Result:**
```
Where does actual backtest rank in 10,000 simulations?

✓ EXCELLENT: 90th+ percentile (lucky sequence, adjust expectations down)
✓ GOOD: 70-90th percentile
✓ ACCEPTABLE: 50-70th percentile
✗ POOR: 30-50th percentile
✗ FAIL: <30th percentile (unlucky or overfitted)
```

**Tail Risk Analysis:**
```
5th Percentile (reasonable worst case):
  ✓ ACCEPTABLE: 5th percentile > -20%
  ✗ UNACCEPTABLE: 5th percentile < -30%
```

**Confidence Interval Width:**
```
95% Confidence Interval:
  ✓ EXCELLENT: Relative width < 50% of starting capital
  ✓ GOOD: Relative width 50-100%
  ✗ POOR: Relative width > 100% (highly unpredictable)
```

---

### 5-Tier Decision Matrix

Based on validation results, strategies are assigned to one of five deployment tiers:

**Tier 1: DEPLOY IMMEDIATELY**
```
Requirements (ALL must be met):
  ✓ All 10 primary tests PASS
  ✓ Overall score ≥ 23 points (GOOD or better)
  ✓ Beats SPY on risk-adjusted basis
  ✓ No critical red flags
  ✓ Stability ratio > 0.7
  ✓ Parameter stability confirmed

Action: Deploy to production
Risk Level: LOW
Monitoring: Monthly
```

**Tier 2: DEPLOY WITH MONITORING**
```
Requirements:
  ✓ All 10 primary tests PASS
  ✓ Overall score 19-22 points (ACCEPTABLE)
  ✓ Beats benchmark
  ✓ No critical red flags
  ✓ 1-2 warning signs present
  ✓ Stability ratio 0.6-0.7

Action: Deploy with enhanced monitoring
Risk Level: MODERATE
Monitoring: Weekly (first 3 months), then monthly
Additional: Reduce initial position sizes by 10-20%
```

**Tier 3: PAPER TRADE FIRST**
```
Requirements:
  ✓ 8-9 out of 10 primary tests PASS
  ✓ Overall score 15-18 points (MARGINAL)
  ✗ Barely beats benchmark OR
  ✗ 2-3 warning signs present OR
  ✗ Stability ratio 0.5-0.6

Action: Paper trade for 3-6 months, then re-evaluate
Risk Level: MODERATE-HIGH
Success Criteria: Paper results match backtest within 20%
```

**Tier 4: IMPROVE AND RETEST**
```
Requirements:
  ✗ <8 primary tests pass OR
  ✗ Overall score < 15 points OR
  ✗ Does not beat benchmark OR
  ✗ 1+ critical red flags

Action: DO NOT DEPLOY. Improve strategy:
  1. Simplify (reduce parameters)
  2. Expand backtest period
  3. Adjust cost assumptions
  4. Re-optimize with broader ranges
  5. Retest and re-evaluate

Loop until reaches Tier 3 or higher
Abort if cannot reach Tier 3 after 3 iterations
```

**Tier 5: REJECT**
```
Requirements:
  ✗ Multiple critical red flags OR
  ✗ Fundamental logic errors OR
  ✗ Cannot be profitable with realistic costs

Action: REJECT permanently
Do not attempt to fix - start over with different approach
```

---

### Recommendation Process (10-Step Workflow)

**Step 1:** Run complete backtest suite (Scripts 16-18)

**Step 2:** Apply 10 primary validation tests
- Record results: Pass/Fail for each
- If <8 tests pass → STOP, REJECT strategy

**Step 3:** Calculate overall score (0-30 points)
- Score each metric (0-3 points)
- Sum across all 10 metrics

**Step 4:** Check for critical red flags
- Review all 7 red flag criteria
- If any red flag present → Investigate, likely REJECT

**Step 5:** Benchmark comparison
- Compare to SPY, 60/40 portfolio, naive trend
- Must beat primary benchmark (SPY) on risk-adjusted basis

**Step 6:** Out-of-sample validation
- Check walk-forward results
- Calculate stability ratio
- Verify OOS consistency

**Step 7:** Monte Carlo validation
- Check percentile rank
- Analyze tail risk (5th percentile)
- Verify confidence interval width

**Step 8:** Make final recommendation
- Synthesize all evidence
- Assign to decision tier (1-5)
- Document rationale and caveats

**Step 9:** Create validation report (PDF)
- Executive summary with recommendation
- Detailed test results
- Comparative analysis
- Risk assessment
- Approval section

**Step 10:** Obtain required approvals
- Portfolio Manager (strategy and returns)
- Risk Manager (risk limits and stress tests)
- Compliance (regulatory review)

**Required:** All three approvals before deployment

---

### Mandatory Documentation

**5 Required Reports:**

1. **Backtest Report** (PDF)
   - Strategy description, parameters
   - All performance metrics
   - Equity curve and drawdown charts
   - Complete trade list

2. **Validation Report** (PDF)
   - All 10 primary test results
   - Scoring breakdown
   - Red flag analysis
   - Benchmark comparisons
   - Final recommendation

3. **Parameter Selection Log** (CSV + PDF)
   - All parameter combinations tested
   - Optimization results
   - Selected parameters with justification
   - Sensitivity analysis

4. **Approval Record** (PDF with digital signatures)
   - Validation report
   - Sign-offs from PM, Risk Manager, Compliance
   - Date of approval
   - Conditions and caveats

5. **Deployment Checklist** (PDF)
   - Pre-deployment verification
   - Configuration confirmation
   - Initial position sizes
   - Monitoring setup

**Retention:** All documents retained indefinitely for audit trail

---

### Validation Example

**Sample Backtest Results:**
```
CAGR: 14%
Sharpe: 1.3
Max Drawdown: -22%
Win Rate: 46%
Profit Factor: 2.1
Total Trades: 523
Positive Years: 4/5 (80%)
```

**Validation Process:**

**Primary Tests:**
```
✓ Test 1: Total return 85% > 30% → PASS
✓ Test 2: Sharpe 1.3 > 1.0 → PASS
✓ Test 3: Max DD -22% > -30% → PASS
✓ Test 4: 523 trades > 100 → PASS
✓ Test 5: Win rate 46% in range → PASS
✓ Test 6: Profit factor 2.1 > 1.5 → PASS
✓ Test 7: Win/Loss 2.6 > 2.0 → PASS
✓ Test 8: 2× cost test degradation 35% < 50% → PASS
✓ Test 9: Avg recovery 9mo < 12mo → PASS
✓ Test 10: 80% positive years > 70% → PASS

Result: 10/10 tests passed ✓
```

**Scoring:**
```
CAGR 14%: 2 pts (Good)
Sharpe 1.3: 2 pts (Good)
Max DD -22%: 1 pt (Acceptable)
Win Rate 46%: 2 pts (Good)
...
Total: 19 points → ACCEPTABLE
```

**Red Flags:**
```
✓ No curve-fitting
✓ No single trade dominance
✓ Realistic win rate
✓ Parameters stable
✓ OOS stability ratio 0.74
✓ All checks passed

Result: 0 red flags ✓
```

**Final Decision:**
```
Tier Assignment: TIER 2 (Deploy with Monitoring)

Rationale:
  - All validation tests passed
  - Score in acceptable range
  - No red flags present
  - Beats benchmark
  - OOS validated

Conditions:
  - Weekly monitoring (first 3 months)
  - Reduce initial sizes by 10%
  - Quarterly re-validation required
```

---

# PORTFOLIO PERFORMANCE MANAGEMENT

## 1. Performance Attribution

**Purpose:**
Decompose portfolio returns to understand what drove performance.

### Asset Class Attribution

**Formula:**
```
Asset_Class_Contribution[i] = Weight[i] × Return[i]

where:
i = asset class (stocks, ETFs, crypto)
Weight[i] = average weight of asset class over period
Return[i] = return of asset class over period

Total Portfolio Return = Σ Asset_Class_Contribution[i]
```

**Example:**
```
Stocks: 45% weight × 12% return = +5.4% contribution
ETFs: 40% weight × 8% return = +3.2% contribution
Crypto: 10% weight × 25% return = +2.5% contribution
Cash: 5% weight × 0% return = 0.0% contribution

Total Portfolio Return = 11.1%
```

**Interpretation:**
- Identifies which asset classes added/subtracted value
- Helps decide allocation adjustments

### Sector Attribution

**Formula:**
```
Sector_Contribution[i] = Weight[i] × Return[i]

where:
i = sector (Technology, Healthcare, Financials, etc.)
```

**Example:**
```
Technology: 25% × 15% = +3.75% contribution
Healthcare: 15% × 10% = +1.50% contribution
Financials: 12% × 8% = +0.96% contribution
...
```

**Interpretation:**
- Identifies which sectors drove returns
- Highlights sector concentration risks
- Informs sector rotation decisions

### Position-Level Attribution

**Formula:**
```
Position_Contribution = (Position_Value_End - Position_Value_Start) / Portfolio_Value_Start × 100

Top 10 Contributors: Largest positive contributions
Top 10 Detractors: Largest negative contributions
```

**Example:**
```
Top Contributors:
1. AAPL.US: +2.5% (strong momentum, held full period)
2. NVDA.US: +2.1% (AI rally, trailing stop protected profit)
3. MSFT.US: +1.8% (steady uptrend, full position)

Top Detractors:
1. XYZ.US: -0.8% (stop-loss hit early)
2. ABC.US: -0.5% (trend reversal, timely exit)
3. DEF.US: -0.3% (rotation exit, better opportunity found)
```

**Interpretation:**
- Identifies best and worst performers
- Validates entry/exit decisions
- Highlights winners to potentially scale up

---

## 2. Alpha vs Beta Decomposition

**Purpose:**
Separate skill-based returns (alpha) from market exposure returns (beta).

**Formula:**
```
Portfolio Return = Alpha + (Beta × Benchmark_Return)

where:
Beta = Covariance(Portfolio_Returns, Benchmark_Returns) / Variance(Benchmark_Returns)
Alpha = Portfolio_Return - (Beta × Benchmark_Return)
```

**Benchmark Selection:**
- US Stocks: S&P 500 (SPY)
- Global Stocks: MSCI All-Country World Index (ACWI)
- Mixed Portfolio: 60% ACWI + 40% Aggregate Bond Index

**Example:**
```
Portfolio Return: +12.5%
Benchmark Return (ACWI): +10.0%
Beta: 0.85

Beta Contribution = 0.85 × 10.0% = +8.5%
Alpha = 12.5% - 8.5% = +4.0%
```

**Interpretation:**
- Beta: Market exposure return (passive)
- Alpha: Skill-based return (active management)
- Positive alpha = outperformance vs benchmark
- Target: Alpha > 3% annually

---

## 3. Risk Metrics

### Value at Risk (VaR)

**Formula:**
```
VaR = Portfolio_Value × Percentile(Daily_Returns, confidence_level)

Common confidence levels:
- 95% VaR: Expected loss not exceeded 95% of the time
- 99% VaR: Expected loss not exceeded 99% of the time
```

**Time Horizons:**
```
1-Day VaR: Risk over next trading day
5-Day VaR: Risk over next week
20-Day VaR: Risk over next month
```

**Example:**
```
Portfolio Value: €50,000
Daily Returns: Historical distribution over last 252 days
95% VaR (1-day) = €50,000 × 2.5% = €1,250

Interpretation: 95% confidence that daily loss will not exceed €1,250
```

### Conditional Value at Risk (CVaR)

**Formula:**
```
CVaR = Expected loss beyond VaR threshold

CVaR = E[Loss | Loss > VaR]
```

**Example:**
```
95% VaR = €1,250
CVaR = €1,850

Interpretation: If loss exceeds VaR threshold, expected loss is €1,850
```

**Why CVaR Matters:**
- VaR only tells you the threshold
- CVaR tells you expected loss in tail events
- More informative for risk management

### Tracking Error

**Formula:**
```
Tracking Error = Standard_Deviation(Portfolio_Returns - Benchmark_Returns)

Annualized Tracking Error = Daily_Tracking_Error × sqrt(252)
```

**Example:**
```
Daily tracking error: 0.5%
Annualized tracking error: 0.5% × sqrt(252) = 7.9%

Interpretation: Portfolio deviates from benchmark by ±7.9% annually
```

**Target Ranges:**
```
Tracking Error < 5%: Low active risk (closet indexer)
Tracking Error 5-10%: Moderate active risk
Tracking Error > 10%: High active risk (concentrated bets)
```

### Information Ratio

**Formula:**
```
Information Ratio = (Portfolio_Return - Benchmark_Return) / Tracking_Error
                  = Active_Return / Tracking_Error
```

**Example:**
```
Active Return: +4.0% (alpha)
Tracking Error: 7.9%
Information Ratio: 4.0% / 7.9% = 0.51

Interpretation: 0.51 units of active return per unit of tracking error
```

**Target:**
```
IR > 0.50: Good active management
IR > 0.75: Excellent active management
IR > 1.00: Outstanding (rare)
```

### Concentration Metrics

**Herfindahl Index:**
```
HHI = Σ (Weight[i])^2 for all positions

where Weight[i] = position value / portfolio value
```

**Example:**
```
20 positions, equal weight (5% each):
HHI = 20 × (0.05)^2 = 0.05

10 positions, top position 15%, rest 5-8%:
HHI = (0.15)^2 + (0.12)^2 + ... = 0.08
```

**Interpretation:**
```
HHI = 0.04 (25 positions, equal weight): Diversified
HHI = 0.05-0.08: Moderate concentration
HHI > 0.10: High concentration risk
```

---

## 4. Performance Dashboard Components

### Equity Curve

**Description:**
Line chart showing cumulative portfolio value over time.

**Formula:**
```
Equity[t] = Equity[t-1] × (1 + Return[t])

Starting with initial capital
```

**Visual Elements:**
- X-axis: Time (monthly)
- Y-axis: Portfolio value (€)
- Benchmark overlay (comparison line)
- Drawdown periods shaded

### Monthly Return Heatmap

**Description:**
Calendar view showing returns by month and year.

**Layout:**
```
        Jan   Feb   Mar   Apr   May   Jun   Jul   Aug   Sep   Oct   Nov   Dec
2020   +2.5% +1.8% -3.2% +4.1% +2.7% +1.9% +3.5% -0.8% +2.1% +3.8% +4.2% +1.5%
2021   +3.1% +2.9% +1.5% +2.8% -1.2% +3.7% +2.4% +1.9% -2.1% +3.5% +2.8% +1.8%
2022   -2.5% -1.8% +0.5% -3.1% +1.2% -2.8% +2.1% +1.5% -1.9% +2.8% +3.2% +1.1%
```

**Color Coding:**
- Green: Positive returns (darker = higher)
- Red: Negative returns (darker = larger loss)
- Gray: No data

### Drawdown Chart (Underwater Plot)

**Description:**
Area chart showing portfolio drawdown from peak over time.

**Formula:**
```
Drawdown[t] = (Equity[t] - Peak_Equity[0:t]) / Peak_Equity[0:t] × 100
```

**Visual Elements:**
- X-axis: Time
- Y-axis: Drawdown (%)
- Fill area below zero line
- Marks recovery points (return to 0%)

### Rolling Sharpe Ratio

**Description:**
Line chart showing 12-month rolling Sharpe ratio over time.

**Formula:**
```
For each month t:
  Calculate returns for months [t-11, t]
  Sharpe[t] = (mean(returns) - risk_free_rate) / std(returns) × sqrt(12)
```

**Visual Elements:**
- X-axis: Time
- Y-axis: Sharpe ratio
- Threshold line at Sharpe = 1.0 (acceptable)
- Threshold line at Sharpe = 2.0 (excellent)

### Asset Allocation Pie Chart

**Description:**
Current portfolio allocation by asset class.

**Segments:**
- Stocks (42%)
- ETFs (38%)
- Crypto (15%)
- Cash (5%)

**Color Coding:**
- Blue: Stocks
- Green: ETFs
- Orange: Crypto
- Gray: Cash

### Position-Level P&L Table

**Description:**
Sortable table showing all current positions.

**Columns:**
- Symbol
- Entry Date
- Entry Price
- Current Price
- Shares
- Position Value
- Unrealized P&L (€)
- Unrealized P&L (%)
- Current Stop
- Distance to Stop (%)
- Momentum Rank

**Sorting:**
- Default: By unrealized P&L (%) descending
- User can sort by any column

---

# OPERATIONAL WORKFLOW

## Daily Workflow

**6:00 AM - Data Update**
```
Automated:
  Run Script 1 (INCREMENTAL mode)
  → Downloads yesterday's data (6 API calls, 30 seconds)
  → Auto-discovers new symbols
  → Flags delisted symbols
```

**6:05 AM - Risk Monitoring**
```
Automated:
  Run Script 14 (Daily Risk Monitor)
  → Check stop-loss violations
  → Check portfolio drawdown
  → Check concentration risk
  → Send alerts if thresholds breached
```

**If Alerts:**
```
Manual:
  Review alerts
  Check market conditions
  Execute emergency exits if needed (stop-loss hits)
  Document decisions
```

**6:30 AM - Performance Review (5 minutes)**
```
Manual:
  Review Script 24 dashboard
  → Check yesterday's P&L
  → Review position performance
  → Check trending positions approaching stops
```

---

## Weekly Workflow

**Friday Close - Stop-Loss Updates**
```
Automated:
  Run Script 9 (Calculate Stops)
  → Update trailing stops (for positions with +15% profit)
  → Stops only move up (ratchet mechanism)
  → Log new stop levels
```

**Friday Evening - Weekly Review (15 minutes)**
```
Manual:
  Review Script 24 dashboard
  → Week's performance summary
  → Check rolling Sharpe ratio
  → Review new/delisted symbols log
  → Identify trending positions vs weakening positions
```

---

## Monthly Workflow

**Last Trading Day of Month - Rebalancing**

**5:00 AM - Pre-Rebalancing Data Prep**
```
Automated:
  Run Script 1 (INCREMENTAL mode)
  → Ensure all data is current
  Run Scripts 2-5 (if new symbols detected)
  → Download fundamentals
  → Consolidate and validate
  → Screen universe
  → Calculate indicators
```

**9:00 AM - Generate Rebalancing Recommendations**
```
Automated:
  Run Scripts 6-11
  → Qualify trends
  → Rank by momentum
  → Calculate position sizes
  → Calculate stops
  → Generate exit signals
  → Create rebalancing plan
  Run Script 12
  → Generate recommendation report (PDF + HTML + CSV)
```

**10:00 AM - Human Review (30-60 minutes)**
```
Manual:
  Review recommendation report
  → Check mandatory exits (stop-loss, trend reversal)
  → Review new entries (momentum rank, position size)
  → Validate position sizes (volatility-adjusted)
  → Check portfolio allocation (asset class, sector)
  → Review risk metrics (concentration, correlation)
  → Evaluate scenarios (bull/bear/sideways)
  → Check circuit breakers (not triggered)
  
  Decision: APPROVE / REJECT / MODIFY
  
  If APPROVE:
    Sign report
    Proceed to execution
  
  If REJECT:
    Document reason
    No trades executed
  
  If MODIFY:
    Adjust recommendations
    Re-run Script 11 with adjustments
    Re-review
```

**First Trading Day of New Month - Execution**

**9:30 AM Market Open - Execute Exits**
```
Manual:
  Execute all exit orders
  → Mandatory exits: Market orders at open
  → Rotation exits: Market orders at open
  Log fills in Script 13
```

**3:30 PM Market Close - Execute Entries**
```
Manual:
  Place entry limit orders
  → Limit price = Close + 0.5%
  → Good for 2 trading days
  Monitor fills over next 2 days
  Log fills in Script 13
```

**After Execution - Post-Trade Tasks**
```
Automated:
  Run Script 13 (Execution Logger)
  → Record all fills
  → Update portfolio state
  → Set initial stops for new positions
```

---

## Quarterly Workflow

**End of Quarter - Comprehensive Review**

**Performance Analysis (1-2 hours)**
```
Manual:
  Run Script 22 (Performance Attribution)
  → Analyze return sources (asset class, sector, position)
  → Calculate alpha vs beta
  → Identify best/worst performers
  
  Run Script 23 (Risk Analytics)
  → Review VaR and CVaR
  → Check tracking error and information ratio
  → Analyze correlation matrix
  → Evaluate factor exposures
  
  Run Script 24 (Performance Dashboard)
  → Generate quarterly report
  → Compare vs benchmark
  → Review rolling metrics
```

**Strategy Review (1-2 hours)**
```
Manual:
  Evaluate strategy performance
  → Are we meeting return targets? (Target: 10-15% annual)
  → Is risk within acceptable range? (Target: Max DD < 20%)
  → Are we generating alpha? (Target: >3% vs benchmark)
  
  Review operational efficiency
  → Are circuit breakers functioning?
  → Any data quality issues?
  → Any execution issues (slippage, unfilled orders)?
  
  Document findings
  → What worked well
  → What needs improvement
  → Any parameter adjustments needed
```

**Parameter Review (optional)**
```
If performance below targets for 2+ consecutive quarters:
  Run Script 17 (Walk-Forward Optimizer)
  → Test current parameters on recent data
  → Evaluate alternative parameter sets
  → Check stability ratios
  
  If better parameters found and stable:
    Document proposed changes
    Run Monte Carlo simulation (Script 18)
    If outcomes acceptable:
      Implement new parameters
      Document change in strategy log
```

---

## Annual Workflow

**Year-End - Comprehensive Backtesting & Audit**

**Full System Backtest (2-3 hours)**
```
Automated:
  Run Script 16 (Backtest Engine)
  → Backtest last 5 years of data
  → Compare to benchmark (buy-and-hold SPY)
  → Generate performance metrics
  → Identify regime-specific performance
  
Manual:
  Analyze backtest results
  → Are live results matching backtest expectations?
  → Any significant deviations?
  → Were there regime changes we didn't handle well?
```

**Walk-Forward Optimization (3-4 hours)**
```
Automated:
  Run Script 17 (Walk-Forward Optimizer)
  → Optimize on 5-year rolling windows
  → Test multiple parameter sets
  → Calculate stability ratios
  
Manual:
  Review optimization results
  → Are current parameters still optimal?
  → Do alternative parameters show better stability?
  → What is the degradation from in-sample to out-of-sample?
```

**Monte Carlo Analysis (1-2 hours)**
```
Automated:
  Run Script 18 (Monte Carlo Simulator)
  → 10,000 simulations of trade randomization
  → Calculate outcome distribution
  → Determine confidence intervals
  
Manual:
  Analyze simulation results
  → What is the range of expected outcomes?
  → What is the probability of drawdown > 25%?
  → Are we comfortable with the worst-case scenarios?
```

**Tax Preparation**
```
Manual:
  Run Script 13 report (all executions for year)
  → Export to CSV
  → Calculate realized gains/losses
  → Separate short-term vs long-term
  → Provide to tax accountant
```

**Annual Report**
```
Manual:
  Compile annual performance report
  → Full-year return vs benchmark
  → Risk-adjusted metrics (Sharpe, Sortino, Calmar)
  → Drawdown analysis
  → Attribution analysis
  → Top 10 winners/losers
  → Trade statistics (count, win rate, profit factor)
  → Lessons learned
  → Plan for next year
```

---

# IMPLEMENTATION ROADMAP

## Phase 1: Data Foundation (Weeks 1-2)

**Week 1: Data Acquisition**
- Implement Script 1 (EODHD Bulk Downloader)
  * Set up EODHD API account
  * Create exchanges.json config
  * Test INITIAL mode (full 400-day download)
  * Test INCREMENTAL mode (delta updates)
  * Verify new symbol discovery and delisting detection
- Implement Script 2 (Yahoo Finance Fundamentals)
  * Set up yfinance library
  * Test symbol mapping (EODHD → Yahoo format)
  * Download fundamentals for all symbols

**Week 2: Data Processing**
- Implement Script 3 (Data Consolidator & Validator)
  * Merge EODHD + Yahoo data
  * Implement all validation rules
  * Test with known bad data (edge cases)
  * Generate data quality report
- Implement Script 4 (Universe Screener)
  * Create filter_thresholds.json config
  * Apply all filters (price, volume, market cap, history)
  * Test with multiple dates
  * Verify qualified symbol counts
- Implement Script 5 (Indicator Calculator)
  * Calculate SMA_50, SMA_200, ATR_20, ADX_14
  * Verify calculations against known values
  * Test edge cases (insufficient data, gaps)

**Deliverable:** Data pipeline producing validated, indicator-enriched time series

---

## Phase 2: Strategy Logic (Weeks 3-4)

**Week 3: Signal Generation**
- Implement Script 6 (Trend Qualifier)
  * Apply 3-part qualification (golden cross, price > SMA_50, ADX > 20)
  * Test with various market conditions
  * Verify trend-qualified counts
- Implement Script 7 (Momentum Ranker)
  * Calculate momentum scores
  * Sort descending
  * Test ranking stability over time
  * Verify top N selection

**Week 4: Risk Management**
- Implement Script 8 (Position Sizer)
  * Implement volatility-adjusted sizing
  * Apply floor (0.5%) and ceiling (8.0%)
  * Test with various account sizes
  * Verify share quantity calculations
- Implement Script 9 (Stop-Loss Calculator)
  * Calculate initial stops (3× ATR)
  * Calculate trailing stops (4× ATR)
  * Test activation logic (+15% profit threshold)
  * Verify weekly update logic (Friday only)
- Implement Script 10 (Exit Signal Generator)
  * Implement all 4 exit conditions
  * Test prioritization logic
  * Verify stop-loss checks
  * Test trend reversal and weakness detection

**Deliverable:** Complete signal generation and risk management system

---

## Phase 3: Portfolio Management (Week 5)

**Week 5: Rebalancing & Reporting**
- Implement Script 11 (Monthly Rebalancer)
  * Integrate all previous scripts
  * Implement circuit breaker checks
  * Generate rebalancing recommendations
  * Test with multiple rebalancing dates
  * Verify exit/entry logic
- Implement Script 12 (Recommendation Report Generator)
  * Create PDF report template
  * Implement all report sections
  * Add charts (allocation, risk metrics)
  * Test report generation
  * Verify human-readable format
- Test end-to-end rebalancing workflow
  * Run full pipeline from data download to report
  * Verify all components integrate correctly
  * Check execution timing (fits within market hours)

**Deliverable:** Monthly rebalancing system with human-in-the-loop approval

---

## Phase 4: Execution & Monitoring (Week 6)

**Week 6: Operations**
- Implement Script 13 (Execution Logger)
  * Design portfolio_state.json schema
  * Implement trade logging
  * Update portfolio state after fills
  * Test with simulated trades
- Implement Script 14 (Daily Risk Monitor)
  * Implement all 6 monitoring checks
  * Set up alert system (email/SMS)
  * Test alert triggers
  * Verify daily execution timing
- Implement Script 15 (Technical Chart Generator)
  * Create interactive HTML dashboard
  * Test with multiple symbols
  * Verify 200-day lookback
  * Test sidebar navigation
- Set up automation
  * Create cron jobs (daily data update, daily monitoring)
  * Test automated execution
  * Verify logging and error handling

**Deliverable:** Complete operational system with execution logging and monitoring

---

## Phase 5: Backtesting Framework (Weeks 7-8)

**Week 7: Backtest Engine**
- Implement Script 16 (Backtest Engine)
  * Walk-forward simulation logic
  * Transaction cost and slippage modeling
  * Corporate action handling
  * Performance metrics calculation
  * Test on historical data (2019-2024)
  * Verify no look-ahead bias
- Run initial backtests
  * 5-year backtest (2019-2024)
  * Calculate all performance metrics
  * Compare to benchmark (SPY buy-and-hold)
  * Generate backtest report
  * Analyze results for issues

**Week 8: Optimization & Monte Carlo**
- Implement Script 17 (Walk-Forward Optimizer)
  * Parameter grid definition
  * In-sample optimization logic
  * Out-of-sample validation logic
  * Stability ratio calculation
  * Test with multiple windows
- Implement Script 18 (Monte Carlo Simulator)
  * Trade sequence randomization
  * 10,000 simulation execution
  * Confidence interval calculation
  * Outcome distribution analysis
- Run full optimization and simulation
  * Optimize all parameters
  * Validate stability
  * Run Monte Carlo simulations
  * Generate risk distribution report

**Deliverable:** Validated strategy with optimized parameters and risk assessment

---

## Phase 6: Performance Management (Weeks 9-10)

**Week 9: Attribution & Risk Analytics**
- Implement Script 22 (Performance Attribution)
  * Asset class attribution
  * Sector attribution
  * Position-level attribution
  * Alpha vs beta decomposition
  * Test with historical data
- Implement Script 23 (Risk Analytics)
  * VaR and CVaR calculation
  * Tracking error and information ratio
  * Concentration metrics (Herfindahl index)
  * Correlation analysis
  * Factor exposure analysis
  * Test with various portfolios

**Week 10: Dashboard & Integration**
- Implement Script 24 (Performance Dashboard)
  * Equity curve chart
  * Monthly return heatmap
  * Drawdown chart
  * Rolling Sharpe ratio chart
  * Asset allocation pie chart
  * Position P&L table
  * Interactive features
- Final integration testing
  * Run complete end-to-end workflow
  * Test all scripts in sequence
  * Verify data flows correctly
  * Check performance (execution time)
  * Fix any integration issues

**Deliverable:** Complete performance management system with interactive dashboard

---

## Phase 7: Production Deployment (Weeks 11-12)

**Week 11: Production Setup**
- Server/infrastructure setup
  * Choose hosting (cloud VM, dedicated server)
  * Install all dependencies
  * Set up Python environment
  * Configure database (if needed)
  * Set up automated backups
- Security setup
  * API key management (.env file)
  * File permissions
  * Network security
  * Backup encryption
- Documentation
  * Operator manual (how to run each script)
  * Troubleshooting guide
  * Emergency procedures
  * Contact information

**Week 12: Testing & Launch**
- Paper trading period (1 month)
  * Run system with real data
  * Generate recommendations (but don't execute)
  * Track how recommendations would perform
  * Verify all automations work
  * Fix any issues found
- Final review
  * Check all scripts operational
  * Verify data pipeline stable
  * Test alert system
  * Review operator manual
  * Sign-off from all stakeholders
- Go-live
  * Execute first real rebalancing
  * Monitor closely for first month
  * Document any issues
  * Make adjustments as needed

**Deliverable:** Live production system managing real capital

---

## Success Criteria

**Data Layer:**
- ✓ Data updates automatically every trading day
- ✓ New symbols discovered within 1 day of listing
- ✓ Delisted symbols flagged within 1 day
- ✓ Data validation catches >99% of quality issues
- ✓ No gaps in historical data

**Strategy Layer:**
- ✓ Signals generated correctly (match manual calculation)
- ✓ Position sizing respects floor/ceiling limits
- ✓ Stop-losses calculated accurately
- ✓ Exit signals trigger at correct times

**Portfolio Layer:**
- ✓ Monthly rebalancing completes within 4 hours
- ✓ Reports are human-readable and actionable
- ✓ All trades logged accurately
- ✓ Portfolio state always current

**Backtesting:**
- ✓ Backtest matches forward test (no look-ahead bias)
- ✓ Transaction costs realistic
- ✓ Parameters validated through walk-forward
- ✓ Monte Carlo shows acceptable risk range

**Validation & Decision:**
- ✓ All 10 primary validation tests pass
- ✓ Overall score ≥19 points (ACCEPTABLE or better)
- ✓ No critical red flags present
- ✓ Out-of-sample stability ratio >0.7
- ✓ Beats benchmark on risk-adjusted basis
- ✓ All required approvals obtained (PM, Risk, Compliance)

**Performance Management:**
- ✓ Attribution analysis identifies return sources
- ✓ Risk metrics updated daily
- ✓ Dashboard loads in <5 seconds
- ✓ All charts render correctly

**Operations:**
- ✓ System runs unattended 95%+ of time
- ✓ Alerts trigger appropriately (not too many, not too few)
- ✓ Recovery from errors automatic
- ✓ Execution slippage <0.1% on average

---

# APPENDIX: KEY FORMULAS SUMMARY

## Technical Indicators

1. **SMA:** SMA(n) = Σ Close[i] / n
2. **ATR:** ATR(n) = SMA(TR, n) where TR = max(H-L, |H-C_prev|, |L-C_prev|)
3. **ADX:** ADX = EMA(DX, n) where DX = 100 × |+DI - -DI| / (+DI + -DI)

## Strategy Rules

4. **Trend Qualification:** SMA_50 > SMA_200 AND Close > SMA_50 AND ADX > 20
5. **Momentum Score:** (Close - SMA_200) / SMA_200 × 100
6. **Position Size:** floor(Base_Risk × Vol_Multiplier / Price) where Base_Risk = Equity × 2%
7. **Initial Stop:** Entry_Price - (3.0 × ATR)
8. **Trailing Stop:** max(Current_Stop, Price - (4.0 × ATR)) if Profit > 15%

## Risk Management

9. **Max Position:** Equity × 8.0%
10. **Min Position:** Equity × 0.5%
11. **Drawdown:** (Equity - Peak_Equity) / Peak_Equity
12. **Circuit Breaker:** Halt if Drawdown < -15%

## Performance Metrics

13. **Sharpe Ratio:** (Return - RFR) / Volatility
14. **Max Drawdown:** min(Equity / Peak_Equity - 1)
15. **Win Rate:** Winning_Trades / Total_Trades
16. **Profit Factor:** Gross_Profit / Gross_Loss

## Attribution

17. **Asset Class Contribution:** Weight × Return
18. **Alpha:** Return - (Beta × Benchmark_Return)
19. **Information Ratio:** Active_Return / Tracking_Error
20. **VaR:** Equity × Percentile(Returns, 95%)

---

# END OF DOCUMENT

**Version:** 3.2 Executive Summary (Updated with Validation Framework)
**Date:** February 2026
**Purpose:** Complete system architecture without code - from design to deployment decision
**Status:** Production-Ready Blueprint with Validation Methodology

**Total System Components:**
- 24 Scripts (Data: 5, Strategy: 5, Portfolio: 2, Operations: 3, Backtest: 3, Validation: 3, Performance: 3)
- 20 Key Formulas (all explicitly defined)
- 10 Primary Validation Tests (Go/No-Go criteria)
- 5-Tier Decision Matrix (deployment framework)
- 4-Phase Operational Workflow (daily/weekly/monthly/quarterly)
- 7-Phase Implementation Roadmap (12 weeks)

**Validation Framework Included:**
- 10 mandatory validation tests (all must pass)
- 30-point scoring system (quantitative assessment)
- 7 critical red flags (instant disqualification criteria)
- Out-of-sample validation requirements (stability ratio >0.7)
- Monte Carlo validation thresholds (tail risk assessment)
- 5-tier deployment decision matrix
- 10-step recommendation process
- Complete documentation requirements

**Next Steps:**
1. Review and approve architecture
2. Begin Phase 1 implementation (Data Foundation)
3. Follow 12-week roadmap to production
4. Run backtest suite (Scripts 16-18)
5. Apply validation framework systematically
6. Make Go/No-Go decision using decision matrix
7. Obtain required approvals (PM, Risk, Compliance)
8. Deploy according to tier assignment
9. Paper trade for 1 month before going live
