# Multi-Asset Trend Following Strategy - Project Setup Guide

## Overview

This package contains the complete file structure for the **Multi-Asset Trend Following Strategy v3.1**, a production-ready systematic trading system.

## What's Included

### 1. Setup Script: `setup_project_structure.py`
Automated script that creates:
- ✅ Complete directory structure (21 directories)
- ✅ 3 configuration files with production-ready defaults
- ✅ 15 placeholder Python scripts (ready for implementation)
- ✅ Environment template (.env.template)
- ✅ README.md with documentation
- ✅ requirements.txt with dependencies
- ✅ .gitignore for version control
- ✅ Initial portfolio state file

### 2. Complete Project: `trend_strategy_project.tar.gz`
Pre-built project structure ready to extract and use.

## Quick Start

### Option A: Run Setup Script (Recommended)

```bash
# Run the setup script
python setup_project_structure.py

# This creates a new directory: trend_strategy/
```

### Option B: Extract Pre-Built Archive

```bash
# Extract the archive
tar -xzf trend_strategy_project.tar.gz

# Navigate to project
cd trend_strategy/
```

## After Setup

### 1. Configure API Keys

```bash
cd trend_strategy/
cp .env.template .env

# Edit .env with your actual API keys:
# - EODHD_API_KEY (required for market data)
# - BITVAVO credentials (optional, for crypto)
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Review Configuration Files

**config/exchanges.json**
- Exchange definitions for NYSE, NASDAQ, XETRA, LSE, PA, AS
- Trading hours and currencies

**config/filter_thresholds.json**
- Minimum price, volume, market cap thresholds
- Separate rules for US stocks, EU stocks, ETFs, crypto

**config/strategy_parameters.json**
- Technical indicator settings (SMA 50/200, ATR 20, ADX 14)
- Entry rules (trend qualification, momentum ranking)
- Position sizing (2% risk per position)
- Stop-loss rules (3× ATR initial, 4× ATR trailing)
- Rebalancing schedule (monthly)
- Circuit breakers (safety limits)

## Directory Structure

```
trend_strategy/
├── .env.template              # API key template
├── .gitignore                 # Git ignore rules
├── README.md                  # Project documentation
├── requirements.txt           # Python dependencies
│
├── config/                    # Configuration files
│   ├── exchanges.json         # Exchange definitions
│   ├── filter_thresholds.json # Universe filters
│   └── strategy_parameters.json # Strategy config
│
├── scripts/                   # 15 Python scripts
│   ├── 01_download_eodhd_bulk.py
│   ├── 02_download_yahoo_fundamentals.py
│   ├── 03_consolidate_validate_data.py
│   ├── 04_screen_universe.py
│   ├── 05_calculate_indicators.py
│   ├── 06_qualify_trends.py
│   ├── 07_rank_momentum.py
│   ├── 08_calculate_position_sizes.py
│   ├── 09_calculate_stops.py
│   ├── 10_generate_exit_signals.py
│   ├── 11_monthly_rebalancing.py
│   ├── 12_generate_recommendation_report.py
│   ├── 13_log_execution.py
│   ├── 14_daily_monitoring.py
│   └── 15_generate_technical_charts.py
│
├── data_cache/                # Cached market data
│   ├── raw_bulk/              # Raw OHLCV (by exchange)
│   │   ├── NYSE/
│   │   ├── NASDAQ/
│   │   ├── XETRA/
│   │   ├── LSE/
│   │   ├── PA/
│   │   └── AS/
│   ├── corporate_actions/     # Splits, dividends
│   ├── fundamentals/          # Company metadata
│   ├── consolidated/          # Clean per-symbol data
│   ├── indicators/            # Technical indicators
│   ├── qualified/             # Screened universe
│   ├── signals/               # Trading signals
│   ├── portfolio/             # Position data
│   └── metadata/              # Audit logs
│
├── data/                      # Portfolio state
│   ├── portfolio_state.json   # Current positions
│   └── executions/            # Trade history
│
├── reports/                   # Generated reports
│   ├── rebalancing/           # Monthly recommendations
│   ├── daily/                 # Daily monitoring
│   └── charts/                # Technical charts
│
└── logs/                      # Execution logs
```

## Implementation Workflow

### Phase 1: Data Foundation (Weeks 1-2)

```bash
# Download historical data (400 days)
python scripts/01_download_eodhd_bulk.py --mode full

# Fetch company fundamentals
python scripts/02_download_yahoo_fundamentals.py --mode full

# Validate data quality
python scripts/03_consolidate_validate_data.py
```

### Phase 2: Universe Screening (Week 2)

```bash
# Screen tradeable universe
python scripts/04_screen_universe.py

# Calculate technical indicators
python scripts/05_calculate_indicators.py
```

### Phase 3: Signal Generation (Week 3-4)

```bash
# Identify trend-qualified stocks
python scripts/06_qualify_trends.py

# Rank by momentum
python scripts/07_rank_momentum.py

# Calculate position sizes
python scripts/08_calculate_position_sizes.py

# Set stop-loss levels
python scripts/09_calculate_stops.py
```

### Daily Workflow (After Setup)

```bash
# Morning: Update data
python scripts/01_download_eodhd_bulk.py --mode daily

# Check for exit signals
python scripts/10_generate_exit_signals.py

# Monitor risk
python scripts/14_daily_monitoring.py

# Generate charts (optional)
python scripts/15_generate_technical_charts.py
```

### Monthly Workflow (Last Trading Day)

```bash
# Generate rebalancing recommendations
python scripts/11_monthly_rebalancing.py

# Create human-readable report
python scripts/12_generate_recommendation_report.py

# After human approval and execution
python scripts/13_log_execution.py
```

## Strategy Overview

### Entry Conditions
- **Trend Qualification**: SMA(50) > SMA(200), Price > SMA(50), ADX > 20
- **Momentum Ranking**: Top 20 stocks by 60-day rate of change
- **Timing**: Monthly rebalancing (last trading day)

### Position Sizing
- **Risk per position**: 2% of account equity
- **Method**: ATR-based volatility adjustment
- **Limits**: Min 0.5%, Max 8% of portfolio

### Exit Conditions
- **Initial stop**: 3× ATR below entry
- **Trailing stop**: 4× ATR below highest close (after 15% profit)
- **Trend breakdown**: SMA(50) crosses below SMA(200)
- **Update frequency**: Weekly (Friday close)

### Risk Management
- **Max positions**: 20
- **Max drawdown**: -15% (circuit breaker)
- **Max concentration**: Top 3 positions ≤ 30%
- **Max sector exposure**: 40%

### Circuit Breakers (Automatic Halt)
1. Portfolio drawdown > -15%
2. VIX > 40
3. Average correlation > 0.85
4. Top 3 concentration > 30%
5. Data staleness > 3 days

## Key Files to Implement

The following scripts are **placeholders** and need implementation:

**Priority 1 (Data):**
- `01_download_eodhd_bulk.py` - Critical for data acquisition
- `04_screen_universe.py` - Universe filtering
- `05_calculate_indicators.py` - Technical indicators

**Priority 2 (Signals):**
- `06_qualify_trends.py` - Trend qualification
- `07_rank_momentum.py` - Momentum ranking
- `08_calculate_position_sizes.py` - Position sizing

**Priority 3 (Risk):**
- `09_calculate_stops.py` - Stop-loss calculation
- `10_generate_exit_signals.py` - Exit monitoring
- `14_daily_monitoring.py` - Risk monitoring

**Priority 4 (Execution):**
- `11_monthly_rebalancing.py` - Rebalancing logic
- `12_generate_recommendation_report.py` - Report generation
- `13_log_execution.py` - Trade logging

## Data Requirements

### EODHD API
- **Bulk API access required** (premium plan)
- Downloads: ~2,400 API calls for 400-day history
- Data volume: ~3-5 GB (compressed parquet)
- Rate limit: 5 requests/second

### Yahoo Finance
- Free tier sufficient for fundamentals
- Rate limit: 2,000 requests/hour
- Used for: Company name, sector, market cap

## Important Notes

### Script Structure
All placeholder scripts include:
- Proper shebang (`#!/usr/bin/env python3`)
- Import structure for project root
- Main execution function
- Warning that implementation is needed

### Configuration Defaults
Pre-configured for **conservative** trading:
- 2% risk per position (standard Kelly fraction)
- Monthly rebalancing (reduces turnover)
- 3×/4× ATR stops (reasonable noise tolerance)
- 20 max positions (adequate diversification)

### Safety Features
- **Human-in-the-loop**: All orders require manual approval
- **Circuit breakers**: Automatic halt on extreme conditions
- **Data validation**: Strict quality checks
- **Audit trail**: Complete decision history

## Testing Recommendations

### Before Live Trading
1. **Backtest** with historical data (2+ years)
2. **Paper trade** for 3-6 months
3. **Start small** (€1,000-€5,000)
4. **Scale gradually** after proven results

### Key Metrics to Track
- Win rate (target: 40-50%)
- Average win/loss ratio (target: >2:1)
- Maximum drawdown (limit: <15%)
- Sharpe ratio (target: >1.0)
- Correlation to S&P 500 (target: <0.7)

## Getting Help

### Documentation
- Full architecture: `Architecture_v3_1_Production_Ready_Fixed.md`
- Formula details: See PART 2 and PART 3 in architecture
- Implementation phases: See PART 5 in architecture

### Common Issues
- **API rate limits**: Use bulk downloads, add delays
- **Missing data**: Check exchange holidays, data staleness
- **Invalid symbols**: Verify symbol format (AAPL.US not AAPL)
- **Stop-loss slippage**: Use limit orders with appropriate offsets

## License & Disclaimer

**MIT License** - See LICENSE file for details

⚠️ **IMPORTANT DISCLAIMER**:
- This is **educational software** for learning systematic trading
- Always **review all trades** before execution
- **Past performance does not guarantee future results**
- Trading involves **risk of loss**
- Consult a financial advisor before trading real money

---

**Version**: 3.1  
**Date**: February 2026  
**Status**: Production-Ready Structure (Implementation Required)
