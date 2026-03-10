# Multi-Asset Trend Following Strategy
## Production-Ready Implementation v3.1

A fully systematic, rule-based trend following strategy for equities, ETFs, and cryptocurrencies.

### Features

- ✅ **Fully Systematic**: Zero discretion, all rules precisely defined
- ✅ **Multi-Asset**: US/EU stocks, ETFs, crypto (BTC/ETH)
- ✅ **Risk-Managed**: Position sizing, stop-losses, circuit breakers
- ✅ **Human-in-the-Loop**: System recommends, human approves
- ✅ **Bulk Data**: Efficient EODHD bulk API integration
- ✅ **Modular**: 15 independent scripts for each step

### Quick Start

1. **Install dependencies**:
```bash
pip install -r requirements.txt
```

2. **Configure API keys**:
```bash
cp .env.template .env
# Edit .env with your EODHD API key
```

3. **Download initial data** (takes ~15 minutes):
```bash
python scripts/01_download_eodhd_bulk.py --mode full
python scripts/02_download_yahoo_fundamentals.py --mode full
```

4. **Run daily workflow**:
```bash
# Download latest data
python scripts/01_download_eodhd_bulk.py --mode daily

# Screen universe
python scripts/04_screen_universe.py

# Calculate indicators
python scripts/05_calculate_indicators.py

# Check for exit signals (daily)
python scripts/10_generate_exit_signals.py

# Daily monitoring
python scripts/14_daily_monitoring.py
```

5. **Monthly rebalancing** (last trading day):
```bash
python scripts/11_monthly_rebalancing.py
python scripts/12_generate_recommendation_report.py
```

### Directory Structure

```
trend_strategy/
├── config/                    # Configuration files
├── scripts/                   # 15 numbered scripts
├── data_cache/               # Cached market data
├── data/                     # Portfolio state
├── reports/                  # Generated reports
└── logs/                     # Execution logs
```

### Strategy Parameters

- **Trend Qualification**: SMA(50) > SMA(200), Price > SMA(50), ADX > 20
- **Position Sizing**: 2% risk per position (ATR-based)
- **Stop-Loss**: 3× ATR initial, 4× ATR trailing
- **Rebalancing**: Monthly (last trading day)
- **Max Positions**: 20
- **Circuit Breakers**: -15% drawdown, VIX > 40

### Scripts Overview

1. `01_download_eodhd_bulk.py` - Download bulk OHLCV data
2. `02_download_yahoo_fundamentals.py` - Fetch company metadata
3. `03_consolidate_validate_data.py` - Validate data quality
4. `04_screen_universe.py` - Filter tradeable universe
5. `05_calculate_indicators.py` - Calculate SMA, ATR, ADX
6. `06_qualify_trends.py` - Identify trend-qualified stocks
7. `07_rank_momentum.py` - Rank by 60-day momentum
8. `08_calculate_position_sizes.py` - Size positions (ATR-based)
9. `09_calculate_stops.py` - Calculate stop-loss levels
10. `10_generate_exit_signals.py` - Check exit conditions (daily)
11. `11_monthly_rebalancing.py` - Generate rebalancing orders
12. `12_generate_recommendation_report.py` - Create human-readable report
13. `13_log_execution.py` - Log executed trades
14. `14_daily_monitoring.py` - Monitor risk/alerts
15. `15_generate_technical_charts.py` - Generate analysis charts

### Documentation

- Full architecture: `Architecture_v3_1_Production_Ready_Fixed.md`
- Implementation order: See PART 5 in architecture doc
- Formula details: See PART 2 and PART 3 in architecture doc

### Safety Features

- **Circuit Breakers**: Automatic halt on extreme conditions
- **Data Validation**: Strict quality checks before trading
- **Human Approval**: All orders reviewed before execution
- **Stop-Losses**: Mandatory on all positions
- **Position Limits**: Maximum size and concentration caps

### License

MIT License - See LICENSE file

### Disclaimer

This is educational software. Always review all trades before execution.
Past performance does not guarantee future results.
Trading involves risk of loss.
