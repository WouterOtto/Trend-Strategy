#!/usr/bin/env python3
"""
Script 4: Universe Screener
============================
Apply liquidity and fundamental filters to create qualified universe

Purpose:
    Filter the complete instrument universe down to tradeable candidates
    by applying systematic liquidity, fundamental, and data quality screens

Dependencies:
    - Script 1: EODHD bulk download (must be run first)
    - Script 2: Yahoo fundamentals download (must be run first)  
    - Script 3: Data consolidation & validation (must be run first)

Inputs:
    - data_cache/consolidated/*.parquet (per-symbol time series)
    - data_cache/fundamentals/company_info.json (company metadata)
    - data_cache/corporate_actions/*.parquet (splits/dividends)
    - config/filter_thresholds.json (screening parameters)

Outputs:
    - data_cache/qualified/qualified_symbols.json (list of passing symbols)
    - data_cache/qualified/screening_report.csv (detailed results per symbol)
    - data_cache/qualified/screening_summary.json (aggregate statistics)

Execution:
    python scripts/04_screen_universe.py --as-of-date 2026-01-31

Architecture: v3.2 (Feb 2026)
"""

import os
import sys
import json
import logging
import argparse
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
import pandas as pd
import numpy as np

# ============================================================================
# CONFIGURATION
# ============================================================================

# Project paths
PROJECT_ROOT = Path(__file__).parent.parent
DATA_LOAD_DIR = PROJECT_ROOT.parent / "data_load" / "data_cache"
# Input
CONFIG_DIR = PROJECT_ROOT / "config"
CONSOLIDATED_DIR = DATA_LOAD_DIR / "consolidated"
FUNDAMENTALS_DIR = DATA_LOAD_DIR / "fundamentals"
CORPORATE_ACTIONS_DIR = DATA_LOAD_DIR / "corporate_actions"
# Output
DATA_CACHE_DIR = PROJECT_ROOT / "data_cache"
QUALIFIED_DIR = DATA_CACHE_DIR / "qualified"
LOG_DIR = PROJECT_ROOT / "logs"

# Screening parameters
LOOKBACK_DAYS = 30  # For price and volume calculations
MIN_RECENT_DATA_POINTS = 20  # Minimum bars in lookback period

# ── Company-info key resolution ───────────────────────────────────────────────
# Script 02 stores company_info.json keys as  {raw_symbol}.{exchange_dir}
#   e.g.  "AAPL.NASDAQ",  "BMW.XETRA",  "VOD.LSE"
#
# Script 03 consolidated filenames use EODHD-API suffixes
#   e.g.  "AAPL.US",  "BMW.DE",  "VOD.L"
#
# This map translates the EODHD suffix back to the exchange dir name(s) used
# in Script 02 so that the company_info lookup succeeds.
EODHD_SUFFIX_TO_EXCHANGES: Dict[str, List[str]] = {
    'US': ['NASDAQ', 'NYSE', 'NCM', 'NGM', 'NMS'],
    'DE': ['XETRA'],
    'L':  ['LSE'],
    'PA': ['PA'],
    'AS': ['AS'],
    'CC': ['CC'],
}
# ─────────────────────────────────────────────────────────────────────────────

# ============================================================================
# LOGGING SETUP
# ============================================================================

def setup_logging() -> logging.Logger:
    """Configure logging with file and console output"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_file = LOG_DIR / f'screen_universe_{timestamp}.log'
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=[
            logging.FileHandler(log_file, encoding='utf-8'),
            logging.StreamHandler(sys.stdout)
        ]
    )
    
    logger = logging.getLogger(__name__)
    logger.info(f"Log file: {log_file}")
    return logger

logger = setup_logging()

# ============================================================================
# CONFIGURATION LOADING
# ============================================================================

def load_filter_thresholds() -> Dict:
    """
    Load filter thresholds from config/filter_thresholds.json
    
    Returns:
        Dictionary with thresholds per exchange
    """
    config_file = CONFIG_DIR / 'filter_thresholds.json'
    
    if not config_file.exists():
        logger.warning(f"Config file not found at {config_file}, creating default")
        create_default_filter_config()
    
    with open(config_file, 'r') as f:
        config = json.load(f)
    
    logger.info(f"âœ“ Loaded filter thresholds for {len(config)} exchanges")
    return config

def create_default_filter_config():
    """Create default filter_thresholds.json configuration"""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    
    default_config = {
        "NYSE": {
            "min_price_usd": 4.50,
            "min_adv_usd": 5000000,
            "min_mcap_usd": 500000000,
            "min_history_days": 252,
            "currency": "USD"
        },
        "NASDAQ": {
            "min_price_usd": 4.50,
            "min_adv_usd": 5000000,
            "min_mcap_usd": 500000000,
            "min_history_days": 252,
            "currency": "USD"
        },
        "XETRA": {
            "min_price_eur": 5.00,
            "min_adv_eur": 5000000,
            "min_mcap_eur": 200000000,
            "min_history_days": 252,
            "currency": "EUR"
        },
        "LSE": {
            "min_price_gbp": 4.00,
            "min_adv_gbp": 5000000,
            "min_mcap_gbp": 200000000,
            "min_history_days": 252,
            "currency": "GBP"
        },
        "PA": {
            "min_price_eur": 5.00,
            "min_adv_eur": 5000000,
            "min_mcap_eur": 200000000,
            "min_history_days": 252,
            "currency": "EUR"
        },
        "AS": {
            "min_price_eur": 5.00,
            "min_adv_eur": 5000000,
            "min_mcap_eur": 200000000,
            "min_history_days": 252,
            "currency": "EUR"
        }
    }
    
    config_file = CONFIG_DIR / 'filter_thresholds.json'
    with open(config_file, 'w') as f:
        json.dump(default_config, f, indent=2)
    
    logger.info(f"âœ“ Created default filter config at {config_file}")

def load_company_info() -> Dict:
    """
    Load company fundamental data from Script 2 output
    
    Returns:
        Dictionary mapping symbol to company info
    """
    company_file = FUNDAMENTALS_DIR / 'company_info.json'
    
    if not company_file.exists():
        logger.error(f"Company info not found at {company_file}")
        logger.error("Please run Script 2 (02_download_yahoo_fundamentals.py) first")
        sys.exit(1)
    
    with open(company_file, 'r') as f:
        company_info = json.load(f)
    
    logger.info(f"âœ“ Loaded company info for {len(company_info)} symbols")
    return company_info

def get_all_consolidated_symbols() -> List[str]:
    """
    Get list of all symbols with consolidated data
    
    Returns:
        List of symbol codes
    """
    if not CONSOLIDATED_DIR.exists():
        logger.error(f"Consolidated data not found at {CONSOLIDATED_DIR}")
        logger.error("Please run Script 3 (03_consolidate_validate_data.py) first")
        sys.exit(1)
    
    parquet_files = list(CONSOLIDATED_DIR.glob("*.parquet"))
    symbols = [f.stem for f in parquet_files]  # filename without extension
    
    logger.info(f"âœ“ Found {len(symbols)} symbols with consolidated data")
    return symbols

# ============================================================================
# DATA LOADING FUNCTIONS
# ============================================================================

def load_consolidated_data(symbol: str) -> Optional[pd.DataFrame]:
    """
    Load consolidated time series for a symbol
    
    Args:
        symbol: Symbol code (e.g., 'AAPL.US')
    
    Returns:
        DataFrame with OHLCV + metadata, or None if not found
    """
    data_file = CONSOLIDATED_DIR / f"{symbol}.parquet"
    
    if not data_file.exists():
        logger.debug(f"No consolidated data for {symbol}")
        return None
    
    try:
        df = pd.read_parquet(data_file)
        
        # Ensure date index
        if 'date' in df.columns and df.index.name != 'date':
            df = df.set_index('date')
        
        return df
    
    except Exception as e:
        logger.warning(f"Error loading {symbol}: {e}")
        return None

def load_corporate_actions(symbol: str, exchange: str) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Load splits and dividends for a symbol
    
    Args:
        symbol: Symbol code
        exchange: Exchange code
    
    Returns:
        Tuple of (splits_df, dividends_df)
    """
    splits_file = CORPORATE_ACTIONS_DIR / f"{exchange}_splits.parquet"
    dividends_file = CORPORATE_ACTIONS_DIR / f"{exchange}_dividends.parquet"
    
    splits_df = pd.DataFrame()
    dividends_df = pd.DataFrame()
    
    # Load splits
    if splits_file.exists():
        try:
            all_splits = pd.read_parquet(splits_file)
            # Filter for this symbol
            symbol_col = 'code' if 'code' in all_splits.columns else 'symbol'
            splits_df = all_splits[all_splits[symbol_col] == symbol].copy()
        except Exception as e:
            logger.debug(f"Error loading splits for {symbol}: {e}")
    
    # Load dividends
    if dividends_file.exists():
        try:
            all_dividends = pd.read_parquet(dividends_file)
            symbol_col = 'code' if 'code' in all_dividends.columns else 'symbol'
            dividends_df = all_dividends[all_dividends[symbol_col] == symbol].copy()
        except Exception as e:
            logger.debug(f"Error loading dividends for {symbol}: {e}")
    
    return splits_df, dividends_df

def load_portfolio_state(portfolio_file: Optional[Path] = None) -> Dict[str, Dict]:
    """
    Load current portfolio positions
    
    Purpose:
        Any symbol in the current portfolio is automatically qualified for the
        universe, regardless of whether it passes screening filters. This
        implements a "grandfather clause" to maintain position continuity.
    
    Args:
        portfolio_file: Path to portfolio_state.json (optional)
    
    Returns:
        Dictionary keyed by symbol, or {} if file doesn't exist
        
    Example portfolio_state.json format:
        {
            "positions": {
                "AAPL.US": {"shares": 100, "entry_price": 150.00, ...},
                "MSFT.US": {"shares": 50, "entry_price": 300.00, ...}
            }
        }
        OR flat format:
        {
            "AAPL.US": {"shares": 100, ...},
            "MSFT.US": {"shares": 50, ...}
        }
    """
    # Default location
    if portfolio_file is None:
        portfolio_file = PROJECT_ROOT / "data" / "portfolio_state.json"
    
    if not portfolio_file.exists():
        logger.info(f"ℹ️  No portfolio_state.json found at {portfolio_file}")
        logger.info("   All symbols will be evaluated via screening filters only")
        return {}
    
    try:
        with open(portfolio_file, 'r') as f:
            state = json.load(f)
        
        # Support both nested and flat formats
        positions = state.get("positions", state)
        
        # Filter to only symbols with positive shares
        active_positions = {
            symbol: info for symbol, info in positions.items()
            if isinstance(info, dict) and info.get('shares', 0) > 0
        }
        
        logger.info(f"✓ Loaded {len(active_positions)} active positions from {portfolio_file}")
        
        # Log the symbols for transparency
        if active_positions:
            logger.info(f"   Portfolio symbols: {', '.join(sorted(active_positions.keys())[:10])}" + 
                       (f"... (+{len(active_positions)-10} more)" if len(active_positions) > 10 else ""))
        
        return active_positions
    
    except Exception as e:
        logger.warning(f"Error loading portfolio state: {e}")
        logger.warning("Continuing without portfolio qualification")
        return {}

# ============================================================================
# SCREENING FILTERS
# ============================================================================

class ScreeningResult:
    """Container for screening results"""
    
    def __init__(self, symbol: str):
        self.symbol = symbol
        self.passed = False
        self.in_portfolio = False  # NEW: Tracks if symbol is in existing portfolio
        self.qualification_reason = ''  # NEW: 'filters' or 'portfolio' or 'both'
        self.filters = {
            'price': {'passed': False, 'reason': '', 'value': None},
            'volume': {'passed': False, 'reason': '', 'value': None},
            'market_cap': {'passed': False, 'reason': '', 'value': None},
            'history': {'passed': False, 'reason': '', 'value': None},
            'data_quality': {'passed': False, 'reason': '', 'value': None},
            'corporate_actions': {'passed': False, 'reason': '', 'value': None}
        }
        self.metadata = {}
    
    def all_passed(self) -> bool:
        """
        Check if symbol qualifies for universe
        
        Qualification logic:
            1. Passes all 6 filters, OR
            2. Is in current portfolio (grandfather clause)
        """
        filters_passed = all(f['passed'] for f in self.filters.values())
        
        # Determine qualification reason
        if filters_passed and self.in_portfolio:
            self.qualification_reason = 'both'
            return True
        elif filters_passed:
            self.qualification_reason = 'filters'
            return True
        elif self.in_portfolio:
            self.qualification_reason = 'portfolio'
            return True
        else:
            self.qualification_reason = 'none'
            return False
    
    def to_dict(self) -> Dict:
        """Convert to dictionary for output"""
        return {
            'symbol': self.symbol,
            'passed': self.all_passed(),
            'in_portfolio': self.in_portfolio,
            'qualification_reason': self.qualification_reason,
            'filters': self.filters,
            'metadata': self.metadata
        }

def apply_price_filter(
    df: pd.DataFrame,
    config: Dict,
    as_of_date: str,
    result: ScreeningResult
) -> bool:
    """
    Filter 1: Price minimum (30-day average)
    
    Rule:
        Average closing price over last 30 days must exceed minimum threshold
    
    Args:
        df: Consolidated data for symbol
        config: Exchange-specific filter config
        as_of_date: Screening date
        result: ScreeningResult object to update
    
    Returns:
        True if passed, False otherwise
    """
    # Get last 30 days of data
    lookback_df = df[df.index <= as_of_date].tail(LOOKBACK_DAYS)
    
    if len(lookback_df) < MIN_RECENT_DATA_POINTS:
        result.filters['price']['passed'] = False
        result.filters['price']['reason'] = f"Insufficient recent data ({len(lookback_df)} < {MIN_RECENT_DATA_POINTS})"
        result.filters['price']['value'] = None
        return False
    
    # Calculate 30-day average price
    avg_price = lookback_df['close'].mean()
    
    # Get threshold (currency-specific)
    currency = config.get('currency', 'USD')
    threshold_key = f"min_price_{currency.lower()}"
    threshold = config.get(threshold_key)
    
    if threshold is None:
        logger.warning(f"No price threshold found for {currency} in config")
        result.filters['price']['passed'] = False
        result.filters['price']['reason'] = f"No threshold configured for {currency}"
        result.filters['price']['value'] = avg_price
        return False
    
    # Check threshold
    passed = avg_price >= threshold
    
    result.filters['price']['passed'] = passed
    result.filters['price']['value'] = round(avg_price, 2)
    result.filters['price']['threshold'] = threshold
    result.filters['price']['currency'] = currency
    
    if not passed:
        result.filters['price']['reason'] = f"Avg price {avg_price:.2f} < threshold {threshold}"
    else:
        result.filters['price']['reason'] = "OK"
    
    return passed

def apply_volume_filter(
    df: pd.DataFrame,
    config: Dict,
    as_of_date: str,
    result: ScreeningResult
) -> bool:
    """
    Filter 2: Average Daily Volume (30-day)
    
    Rule:
        Average dollar volume over last 30 days must exceed minimum threshold
        Dollar volume = shares traded Ã— average price
    
    Args:
        df: Consolidated data for symbol
        config: Exchange-specific filter config
        as_of_date: Screening date
        result: ScreeningResult object to update
    
    Returns:
        True if passed, False otherwise
    """
    # Get last 30 days of data
    lookback_df = df[df.index <= as_of_date].tail(LOOKBACK_DAYS)
    
    if len(lookback_df) < MIN_RECENT_DATA_POINTS:
        result.filters['volume']['passed'] = False
        result.filters['volume']['reason'] = f"Insufficient recent data ({len(lookback_df)} < {MIN_RECENT_DATA_POINTS})"
        result.filters['volume']['value'] = None
        return False
    
    # Calculate average dollar volume (volume Ã— close price)
    lookback_df = lookback_df.copy()
    lookback_df['dollar_volume'] = lookback_df['volume'] * lookback_df['close']
    avg_dollar_volume = lookback_df['dollar_volume'].mean()
    
    # Get threshold (currency-specific)
    currency = config.get('currency', 'USD')
    threshold_key = f"min_adv_{currency.lower()}"
    threshold = config.get(threshold_key)
    
    if threshold is None:
        logger.warning(f"No volume threshold found for {currency} in config")
        result.filters['volume']['passed'] = False
        result.filters['volume']['reason'] = f"No threshold configured for {currency}"
        result.filters['volume']['value'] = avg_dollar_volume
        return False
    
    # Check threshold
    passed = avg_dollar_volume >= threshold
    
    result.filters['volume']['passed'] = passed
    result.filters['volume']['value'] = round(avg_dollar_volume, 2)
    result.filters['volume']['threshold'] = threshold
    result.filters['volume']['currency'] = currency
    
    if not passed:
        result.filters['volume']['reason'] = f"Avg volume {avg_dollar_volume:,.0f} < threshold {threshold:,.0f}"
    else:
        result.filters['volume']['reason'] = "OK"
    
    return passed

def apply_market_cap_filter(
    company_info: Dict,
    config: Dict,
    result: ScreeningResult
) -> bool:
    """
    Filter 3: Market Capitalization
    
    Rule:
        Market cap must exceed minimum threshold for exchange
    
    Args:
        company_info: Company metadata from Yahoo
        config: Exchange-specific filter config
        result: ScreeningResult object to update
    
    Returns:
        True if passed, False otherwise
    """
    market_cap = company_info.get('market_cap', 0)
    
    if market_cap == 0 or market_cap is None:
        result.filters['market_cap']['passed'] = False
        result.filters['market_cap']['reason'] = "Market cap data missing"
        result.filters['market_cap']['value'] = None
        return False
    
    # Get threshold (currency-specific)
    currency = config.get('currency', 'USD')
    threshold_key = f"min_mcap_{currency.lower()}"
    threshold = config.get(threshold_key)
    
    if threshold is None:
        logger.warning(f"No market cap threshold found for {currency} in config")
        result.filters['market_cap']['passed'] = False
        result.filters['market_cap']['reason'] = f"No threshold configured for {currency}"
        result.filters['market_cap']['value'] = market_cap
        return False
    
    # Note: Assuming market_cap from Yahoo is in native currency
    # If conversion needed, add FX logic here
    
    # Check threshold
    passed = market_cap >= threshold
    
    result.filters['market_cap']['passed'] = passed
    result.filters['market_cap']['value'] = market_cap
    result.filters['market_cap']['threshold'] = threshold
    result.filters['market_cap']['currency'] = currency
    
    if not passed:
        result.filters['market_cap']['reason'] = f"Market cap {market_cap:,.0f} < threshold {threshold:,.0f}"
    else:
        result.filters['market_cap']['reason'] = "OK"
    
    return passed

def apply_history_filter(
    df: pd.DataFrame,
    config: Dict,
    result: ScreeningResult
) -> bool:
    """
    Filter 4: Listing History
    
    Rule:
        Must have at least N days of price history
    
    Args:
        df: Consolidated data for symbol
        config: Exchange-specific filter config
        result: ScreeningResult object to update
    
    Returns:
        True if passed, False otherwise
    """
    history_days = len(df)
    min_history = config.get('min_history_days', 252)
    
    passed = history_days >= min_history
    
    result.filters['history']['passed'] = passed
    result.filters['history']['value'] = history_days
    result.filters['history']['threshold'] = min_history
    
    if not passed:
        result.filters['history']['reason'] = f"History {history_days} days < threshold {min_history} days"
    else:
        result.filters['history']['reason'] = "OK"
    
    return passed

def apply_data_quality_filter(
    df: pd.DataFrame,
    as_of_date: str,
    result: ScreeningResult
) -> bool:
    """
    Filter 5: Data Quality
    
    Rule:
        Most recent data point must have data_quality = True
        (set by Script 3: Data Consolidator & Validator)
    
    Args:
        df: Consolidated data for symbol
        as_of_date: Screening date
        result: ScreeningResult object to update
    
    Returns:
        True if passed, False otherwise
    """
    # Get most recent data point as of screening date
    recent_df = df[df.index <= as_of_date]
    
    if len(recent_df) == 0:
        result.filters['data_quality']['passed'] = False
        result.filters['data_quality']['reason'] = "No data as of screening date"
        result.filters['data_quality']['value'] = None
        return False
    
    latest = recent_df.iloc[-1]
    
    # Check if data_quality column exists
    if 'data_quality' not in df.columns:
        # Column absent means Script 3 did not produce quality flags.
        # We cannot penalise every symbol for a missing upstream column, so we
        # PASS the filter and log a one-time warning.  If you want to enforce
        # this check, ensure Script 3 always writes the 'data_quality' column.
        logger.warning(
            f"data_quality column missing for {result.symbol} — "
            "assuming OK (set Script 3 to emit this column to enforce the check)"
        )
        result.filters['data_quality']['passed'] = True
        result.filters['data_quality']['reason'] = "Column absent — assumed OK"
        result.filters['data_quality']['value'] = None
        return True
    
    data_quality = latest['data_quality']
    validation_issues = latest.get('validation_issues', '')
    
    passed = bool(data_quality)
    
    result.filters['data_quality']['passed'] = passed
    result.filters['data_quality']['value'] = passed
    
    if not passed:
        result.filters['data_quality']['reason'] = f"Data quality failed: {validation_issues}"
    else:
        result.filters['data_quality']['reason'] = "OK"
    
    return passed

def apply_corporate_actions_filter(
    symbol: str,
    exchange: str,
    as_of_date: str,
    result: ScreeningResult
) -> bool:
    """
    Filter 6: No Pending Corporate Actions
    
    Rule:
        No splits or major dividends announced for next 30 days
    
    Args:
        symbol: Symbol code
        exchange: Exchange code
        as_of_date: Screening date
        result: ScreeningResult object to update
    
    Returns:
        True if passed (no pending actions), False otherwise
    """
    # Load corporate actions
    splits_df, dividends_df = load_corporate_actions(symbol, exchange)
    
    # Calculate 30-day forward window
    as_of_dt = datetime.strptime(as_of_date, '%Y-%m-%d')
    forward_end = as_of_dt + timedelta(days=30)
    
    # Check for pending splits
    pending_splits = 0
    if len(splits_df) > 0 and 'date' in splits_df.columns:
        # Convert date column to datetime if needed
        if not pd.api.types.is_datetime64_any_dtype(splits_df['date']):
            splits_df['date'] = pd.to_datetime(splits_df['date'])
        
        # Filter for future splits
        pending_splits_df = splits_df[
            (splits_df['date'] > as_of_dt) &
            (splits_df['date'] <= forward_end)
        ]
        pending_splits = len(pending_splits_df)
    
    # Check for pending dividends (optional - could be excluded if dividends are OK)
    # For now, we'll allow dividends but flag splits
    
    passed = (pending_splits == 0)
    
    result.filters['corporate_actions']['passed'] = passed
    result.filters['corporate_actions']['value'] = pending_splits
    
    if not passed:
        result.filters['corporate_actions']['reason'] = f"{pending_splits} pending split(s) in next 30 days"
    else:
        result.filters['corporate_actions']['reason'] = "OK"
    
    return passed

# ============================================================================
# MAIN SCREENING FUNCTION
# ============================================================================

def screen_symbol(
    symbol: str,
    company_info_dict: Dict,
    filter_thresholds: Dict,
    as_of_date: str,
    portfolio_positions: Dict
) -> ScreeningResult:
    """
    Apply all screening filters to a single symbol
    
    Args:
        symbol: Symbol code
        company_info_dict: Dictionary of all company info
        filter_thresholds: Dictionary of filter configs per exchange
        as_of_date: Screening date
        portfolio_positions: Dictionary of current portfolio positions
    
    Returns:
        ScreeningResult object with pass/fail for each filter
    """
    result = ScreeningResult(symbol)
    
    # Initialize metadata with symbol early (in case of early returns)
    result.metadata = {
        'symbol': symbol,
        'name': '',
        'exchange': '',
        'sector': '',
        'industry': '',
        'country': '',
        'screened_date': as_of_date
    }
    
    # ══════════════════════════════════════════════════════════════════════════
    # PORTFOLIO CONTINUITY CHECK (Grandfather Clause)
    # ══════════════════════════════════════════════════════════════════════════
    # If symbol is in current portfolio, it automatically qualifies regardless
    # of filter results. This prevents forced exits due to temporary liquidity
    # drops or borderline filter values.
    # ══════════════════════════════════════════════════════════════════════════
    
    if symbol in portfolio_positions:
        result.in_portfolio = True
        logger.debug(f"✓ {symbol} - IN PORTFOLIO (auto-qualified)")
    
    # ══════════════════════════════════════════════════════════════════════════
    
    # Load consolidated data
    df = load_consolidated_data(symbol)
    if df is None:
        result.filters['price']['reason'] = "No consolidated data"
        return result

    # ── Key normalisation ────────────────────────────────────────────────────
    # Consolidated filenames use EODHD format:  SYMBOL.SUFFIX    (e.g. AAPL.US)
    # company_info.json uses dot separator:      SYMBOL.EXCHANGE  (e.g. AAPL.NASDAQ)
    # filter_thresholds.json uses logical keys:  EXCHANGE         (e.g. NYSE / US)
    #
    # CRITICAL: EODHD uses ".US" as the suffix for ALL US-listed equities
    # (both NYSE and NASDAQ), so "US" never appears as a key in
    # filter_thresholds.json.  The alias map below translates raw EODHD
    # exchange suffixes to their filter_thresholds key equivalents.
    # Add further mappings here as new exchanges are onboarded.
    EXCHANGE_ALIAS: Dict[str, str] = {
        # EODHD suffix → filter_thresholds key
        'US':       'NYSE',     # All US equities (NYSE + NASDAQ combined threshold)
        'F':        'XETRA',    # Frankfurt → XETRA thresholds
        'DE':       'XETRA',    # Generic German suffix
        'L':        'LSE',      # London Stock Exchange short suffix
        'PA':       'PA',       # Euronext Paris (already matches)
        'AS':       'AS',       # Euronext Amsterdam (already matches)
        'NYSE':     'NYSE',
        'NASDAQ':   'NASDAQ',
        'XETRA':    'XETRA',
        'LSE':      'LSE',
    }

    if '.' in symbol:
        base_ticker, eodhd_exchange = symbol.rsplit('.', 1)
    else:
        base_ticker, eodhd_exchange = symbol, None
    # ────────────────────────────────────────────────────────────────────────

    # Get company info — Script 02 stores keys as {raw_symbol}.{exchange_dir}
    # e.g. "AAPL.NASDAQ", "BMW.XETRA".  Consolidated filenames use EODHD-API
    # suffixes ("AAPL.US", "BMW.DE"), so we reverse-map the suffix back to
    # every possible exchange-dir name and try each in order.
    possible_exchanges = EODHD_SUFFIX_TO_EXCHANGES.get(eodhd_exchange, [eodhd_exchange])
    company_info = {}
    for exc in possible_exchanges:
        candidate_key = f"{base_ticker}.{exc}"
        if candidate_key in company_info_dict:
            company_info = company_info_dict[candidate_key]
            break

    # Note: We don't return early if company_info is missing
    # The market_cap filter will handle this and fail gracefully
    # This allows symbols to still pass via other filters
    
    # Resolve EODHD exchange suffix → filter_thresholds key via alias map.
    # Fall back to Yahoo exchange code only if the EODHD suffix is unavailable.
    raw_exchange = eodhd_exchange or company_info.get('exchange', None)
    if not raw_exchange:
        result.filters['price']['reason'] = "Exchange unknown (no suffix, no company info)"
        return result

    exchange = EXCHANGE_ALIAS.get(raw_exchange, raw_exchange)  # identity fallback

    # Get filter config for this exchange
    config = filter_thresholds.get(exchange)
    if not config:
        logger.warning(
            f"No filter config for exchange '{exchange}' "
            f"(raw EODHD suffix: '{raw_exchange}'), skipping {symbol}. "
            f"Add an entry for '{exchange}' to config/filter_thresholds.json "
            f"or extend EXCHANGE_ALIAS in screen_symbol()."
        )
        result.filters['price']['reason'] = f"No config for exchange '{exchange}'"
        return result
    
    # Update metadata with complete information
    result.metadata.update({
        'symbol': symbol,           # Full EODHD key  e.g. XRAY.NASDAQ
        'base_ticker': base_ticker, # Bare ticker     e.g. XRAY
        'name': company_info.get('name', ''),
        'exchange': exchange,       # EODHD exchange  e.g. NASDAQ
        'sector': company_info.get('sector', ''),
        'industry': company_info.get('industry', ''),
        'country': company_info.get('country', ''),
        'screened_date': as_of_date
    })
    
    # Apply all filters (order matters for short-circuit efficiency)
    # 1. Data quality (fast, eliminates bad data early)
    apply_data_quality_filter(df, as_of_date, result)
    
    # 2. History (fast, eliminates young listings)
    apply_history_filter(df, config, result)
    
    # 3. Price (moderate, eliminates penny stocks)
    apply_price_filter(df, config, as_of_date, result)
    
    # 4. Volume (moderate, eliminates illiquid instruments)
    apply_volume_filter(df, config, as_of_date, result)
    
    # 5. Market cap (fast, eliminates micro caps)
    apply_market_cap_filter(company_info, config, result)
    
    # 6. Corporate actions (moderate, eliminates pending events)
    apply_corporate_actions_filter(symbol, exchange, as_of_date, result)
    
    return result

def screen_universe(
    symbols: List[str],
    company_info: Dict,
    filter_thresholds: Dict,
    as_of_date: str,
    portfolio_positions: Dict
) -> Tuple[List[Dict], List[ScreeningResult]]:
    """
    Screen entire universe of symbols
    
    Args:
        symbols: List of symbol codes to screen
        company_info: Dictionary of company metadata
        filter_thresholds: Dictionary of filter configs per exchange
        as_of_date: Screening date
        portfolio_positions: Current portfolio positions (for auto-qualification)
    
    Returns:
        Tuple of (qualified_list, all_results)
        - qualified_list: List of symbols that passed all filters or in portfolio
        - all_results: List of all ScreeningResult objects
    """
    # ── Guarantee portfolio symbols are always in the screening list ─────────
    # Bug-guard: symbols comes from CONSOLIDATED_DIR/*.parquet.  A held position
    # that has no parquet file would never be enumerated, so the grandfather
    # clause in all_passed() would never fire and the symbol would be silently
    # dropped.  We inject any missing portfolio symbols here so they are always
    # processed (they will fail every filter but pass via in_portfolio=True).
    symbols_set = set(symbols)
    missing_portfolio_symbols = [s for s in portfolio_positions if s not in symbols_set]
    if missing_portfolio_symbols:
        logger.warning(
            f"⚠️  {len(missing_portfolio_symbols)} portfolio symbol(s) have no "
            f"consolidated data file and will be auto-qualified via grandfather clause: "
            f"{missing_portfolio_symbols}"
        )
        symbols = list(symbols) + missing_portfolio_symbols
    # ─────────────────────────────────────────────────────────────────────────

    logger.info(f"\n{'='*70}")
    logger.info("SCREENING UNIVERSE")
    logger.info(f"{'='*70}")
    logger.info(f"Total symbols: {len(symbols)}")
    logger.info(f"Portfolio positions: {len(portfolio_positions)}")
    logger.info(f"As-of date: {as_of_date}")
    
    all_results = []
    qualified_list = []
    
    for idx, symbol in enumerate(symbols, 1):
        if idx % 100 == 0:
            logger.info(f"Progress: {idx}/{len(symbols)} ({idx/len(symbols)*100:.1f}%)")
        
        result = screen_symbol(symbol, company_info, filter_thresholds, as_of_date, portfolio_positions)
        all_results.append(result)
        
        if result.all_passed():
            # Safeguard: only append if metadata has symbol key
            if result.metadata.get('symbol'):
                qualified_list.append(result.metadata)
            else:
                logger.warning(f"⚠️  {symbol} - PASSED but metadata incomplete, skipping")
            logger.debug(f"✓ {symbol} - PASSED ({result.qualification_reason})")
            logger.debug(f"âœ“ {symbol} - PASSED")
        else:
            # Log first failure reason
            first_failure = next((f for f in result.filters.values() if not f['passed']), None)
            if first_failure:
                logger.debug(f"âœ— {symbol} - FAILED: {first_failure['reason']}")
    
    logger.info(f"\n{'='*70}")
    logger.info(f"SCREENING COMPLETE")
    logger.info(f"{'='*70}")
    logger.info(f"Total screened: {len(symbols)}")
    logger.info(f"Qualified: {len(qualified_list)}")
    logger.info(f"Rejection rate: {(1 - len(qualified_list)/len(symbols))*100:.1f}%")
    
    return qualified_list, all_results

# ============================================================================
# OUTPUT FUNCTIONS
# ============================================================================

def save_qualified_symbols(qualified_list: List[Dict], output_file: Path):
    """
    Save list of qualified symbols to JSON
    
    Format:
        [
            {
                "symbol": "AAPL.US",
                "name": "Apple Inc.",
                "exchange": "NASDAQ",
                "sector": "Technology",
                "screened_date": "2026-01-31"
            },
            ...
        ]
    """
    QUALIFIED_DIR.mkdir(parents=True, exist_ok=True)
    
    with open(output_file, 'w') as f:
        json.dump(qualified_list, f, indent=2)
    
    logger.info(f"âœ“ Saved qualified symbols to {output_file}")

def save_screening_report(all_results: List[ScreeningResult], output_file: Path):
    """
    Save detailed screening report to CSV
    
    Columns:
        - symbol
        - passed (overall)
        - filter_price (passed)
        - filter_volume (passed)
        - filter_market_cap (passed)
        - filter_history (passed)
        - filter_data_quality (passed)
        - filter_corporate_actions (passed)
        - price_value, price_reason
        - volume_value, volume_reason
        - etc.
    """
    QUALIFIED_DIR.mkdir(parents=True, exist_ok=True)
    
    rows = []
    for result in all_results:
        row = {
            'symbol': result.symbol,
            'passed_overall': result.all_passed(),
            'in_portfolio': result.in_portfolio,
            'qualification_reason': result.qualification_reason,
            'name': result.metadata.get('name', ''),
            'exchange': result.metadata.get('exchange', ''),
            'sector': result.metadata.get('sector', ''),
        }
        
        # Add filter results
        for filter_name, filter_data in result.filters.items():
            row[f'filter_{filter_name}_passed'] = filter_data['passed']
            row[f'filter_{filter_name}_value'] = filter_data.get('value')
            row[f'filter_{filter_name}_reason'] = filter_data.get('reason', '')
        
        rows.append(row)
    
    df = pd.DataFrame(rows)
    df.to_csv(output_file, index=False)
    
    logger.info(f"âœ“ Saved screening report to {output_file}")

def save_screening_summary(
    qualified_list: List[Dict],
    all_results: List[ScreeningResult],
    as_of_date: str,
    output_file: Path
):
    """
    Save screening summary statistics to JSON
    
    Includes:
        - Total symbols screened
        - Qualified count
        - Rejection rate
        - Filter-specific rejection counts
        - Breakdown by exchange, sector
    """
    QUALIFIED_DIR.mkdir(parents=True, exist_ok=True)
    
    # Calculate filter rejection counts
    filter_rejections = {
        'price': 0,
        'volume': 0,
        'market_cap': 0,
        'history': 0,
        'data_quality': 0,
        'corporate_actions': 0
    }
    
    for result in all_results:
        for filter_name, filter_data in result.filters.items():
            if not filter_data['passed']:
                filter_rejections[filter_name] += 1
    
    # Breakdown by exchange
    exchange_counts = {}
    for item in qualified_list:
        exchange = item.get('exchange', 'Unknown')
        exchange_counts[exchange] = exchange_counts.get(exchange, 0) + 1
    
    # Breakdown by sector
    sector_counts = {}
    for item in qualified_list:
        sector = item.get('sector', 'Unknown')
        sector_counts[sector] = sector_counts.get(sector, 0) + 1
    
    # Qualification method breakdown
    qualification_stats = {
        'filters_only': sum(1 for r in all_results if r.qualification_reason == 'filters'),
        'portfolio_only': sum(1 for r in all_results if r.qualification_reason == 'portfolio'),
        'both': sum(1 for r in all_results if r.qualification_reason == 'both')
    }
    
    summary = {
        'screening_date': as_of_date,
        'total_symbols_screened': len(all_results),
        'qualified_symbols': len(qualified_list),
        'rejection_rate_pct': round((1 - len(qualified_list)/len(all_results))*100, 2) if len(all_results) > 0 else 0,
        'qualification_breakdown': qualification_stats,
        'filter_rejections': filter_rejections,
        'qualified_by_exchange': exchange_counts,
        'qualified_by_sector': sector_counts,
        'timestamp': datetime.now().isoformat()
    }
    
    with open(output_file, 'w') as f:
        json.dump(summary, f, indent=2)
    
    logger.info(f"âœ“ Saved screening summary to {output_file}")
    
    # Print summary to console
    logger.info(f"\n{'='*70}")
    logger.info("SCREENING SUMMARY")
    logger.info(f"{'='*70}")
    logger.info(f"Date: {as_of_date}")
    logger.info(f"Total screened: {summary['total_symbols_screened']:,}")
    logger.info(f"Qualified: {summary['qualified_symbols']:,}")
    logger.info(f"Rejection rate: {summary['rejection_rate_pct']:.1f}%")
    
    logger.info(f"\nQualification Breakdown:")
    logger.info(f"  Via filters only    : {qualification_stats['filters_only']:5,}")
    logger.info(f"  Via portfolio only  : {qualification_stats['portfolio_only']:5,}")
    logger.info(f"  Via both            : {qualification_stats['both']:5,}")
    
    logger.info(f"\nFilter Rejections:")
    for filter_name, count in filter_rejections.items():
        pct = (count / len(all_results)) * 100 if len(all_results) > 0 else 0
        logger.info(f"  {filter_name:20s}: {count:5,} ({pct:5.1f}%)")
    
    logger.info(f"\nQualified by Exchange:")
    for exchange, count in sorted(exchange_counts.items(), key=lambda x: x[1], reverse=True):
        logger.info(f"  {exchange:10s}: {count:5,}")
    
    logger.info(f"\nTop Sectors:")
    for sector, count in sorted(sector_counts.items(), key=lambda x: x[1], reverse=True)[:10]:
        logger.info(f"  {sector:30s}: {count:5,}")

# ============================================================================
# MAIN EXECUTION
# ============================================================================

def parse_arguments():
    """Parse command-line arguments"""
    parser = argparse.ArgumentParser(
        description='Universe Screener - Script 4 (v3.2)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Screen universe as of month-end
    python scripts/04_screen_universe.py --as-of-date 2026-01-31
    
    # Screen with current date
    python scripts/04_screen_universe.py --as-of-date $(date +%Y-%m-%d)
    
    # Test with limited symbols
    python scripts/04_screen_universe.py --as-of-date 2026-01-31 --max-symbols 100

Dependencies:
    Must run in order:
        1. Script 1: Download EODHD bulk data
        2. Script 2: Download Yahoo fundamentals
        3. Script 3: Consolidate & validate data
        4. Script 4: Screen universe (THIS SCRIPT)
        """
    )
    
    parser.add_argument(
        '--as-of-date',
        required=True,
        help='Screening date (YYYY-MM-DD)'
    )
    
    parser.add_argument(
        '--max-symbols',
        type=int,
        help='Maximum symbols to process (for testing)'
    )
    
    parser.add_argument(
        '--portfolio-state',
        type=str,
        help='Path to portfolio_state.json (optional, for grandfather clause)'
    )
    
    return parser.parse_args()

def main():
    """Main execution function"""
    start_time = datetime.now()
    
    logger.info("="*70)
    logger.info("UNIVERSE SCREENER - Script 4")
    logger.info("Architecture v3.2 (Feb 2026)")
    logger.info("="*70)
    
    # Parse arguments
    args = parse_arguments()
    as_of_date = args.as_of_date
    
    # Validate date format
    try:
        datetime.strptime(as_of_date, '%Y-%m-%d')
    except ValueError:
        logger.error(f"Invalid date format: {as_of_date}. Use YYYY-MM-DD")
        sys.exit(1)
    
    logger.info(f"\nScreening date: {as_of_date}")
    
    # Load configuration
    try:
        filter_thresholds = load_filter_thresholds()
        company_info = load_company_info()
        symbols = get_all_consolidated_symbols()
        
        # Load current portfolio (if exists) - for grandfather clause
        portfolio_file = getattr(args, 'portfolio_state', None)
        if portfolio_file:
            portfolio_file = Path(portfolio_file)
        portfolio_positions = load_portfolio_state(portfolio_file)
        
        # Apply max_symbols limit if specified
        if args.max_symbols:
            symbols = symbols[:args.max_symbols]
            logger.info(f"âš ï¸  Testing mode: Limited to {args.max_symbols} symbols")
        
    except Exception as e:
        logger.error(f"Configuration error: {e}")
        sys.exit(1)
    
    # Run screening
    try:
        qualified_list, all_results = screen_universe(
            symbols,
            company_info,
            filter_thresholds,
            as_of_date,
            portfolio_positions
        )
    except Exception as e:
        logger.error(f"Error during screening: {e}", exc_info=True)
        sys.exit(1)
    
    # Save outputs
    try:
        output_base = as_of_date.replace('-', '')
        
        save_qualified_symbols(
            qualified_list,
            QUALIFIED_DIR / 'qualified_symbols.json'
        )
        
        save_screening_report(
            all_results,
            QUALIFIED_DIR / 'screening_report.csv'
        )
        
        save_screening_summary(
            qualified_list,
            all_results,
            as_of_date,
            QUALIFIED_DIR / 'screening_summary.json'
        )
        
    except Exception as e:
        logger.error(f"Error saving outputs: {e}", exc_info=True)
        sys.exit(1)
    
    # Final summary
    end_time = datetime.now()
    duration = end_time - start_time
    
    logger.info(f"\n{'='*70}")
    logger.info("SCREENING COMPLETE")
    logger.info(f"{'='*70}")
    logger.info(f"Duration: {duration}")
    logger.info(f"Symbols screened: {len(all_results):,}")
    logger.info(f"Qualified: {len(qualified_list):,}")
    logger.info(f"Outputs:")
    logger.info(f"  - {QUALIFIED_DIR / 'qualified_symbols.json'}")
    logger.info(f"  - {QUALIFIED_DIR / 'screening_report.csv'}")
    logger.info(f"  - {QUALIFIED_DIR / 'screening_summary.json'}")
    logger.info("="*70)
    
    return 0

if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        logger.warning("\n\nScreening interrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)