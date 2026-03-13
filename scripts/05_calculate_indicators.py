#!/usr/bin/env python3
"""
Script 5: Technical Indicator Calculator
=========================================
Calculate SMA, ATR, and ADX for all qualified symbols

Purpose:
    Calculate technical indicators required for trend qualification and momentum ranking
    using academically-validated formulas with precise implementation

Dependencies:
    - Script 1: EODHD bulk download (must be run first)
    - Script 2: Yahoo fundamentals download (must be run first)
    - Script 3: Data consolidation & validation (must be run first)
    - Script 4: Universe screening (must be run first)

Inputs:
    - data_cache/qualified/qualified_symbols.json (screened universe)
    - data_cache/consolidated/*.parquet (per-symbol time series)

Outputs:
    - data_cache/indicators/{symbol}_indicators.parquet (indicators per symbol)
    - data_cache/metadata/indicator_calculation_log.json (audit trail)

Execution:
    # Calculate indicators as of specific date
    python scripts/05_calculate_indicators.py --as-of-date 2026-01-31
    
    # Calculate for specific symbols only
    python scripts/05_calculate_indicators.py --symbols AAPL.US,MSFT.US
    
    # Force recalculation (overwrite existing)
    python scripts/05_calculate_indicators.py --force

Indicators Calculated:
    1. SMA_50: 50-period Simple Moving Average (adjusted close)
    2. SMA_200: 200-period Simple Moving Average (adjusted close)
    3. ATR_20_pct: 20-period Average True Range (percentage)
    4. ADX_14: 14-period Average Directional Index (Wilder's method)

Architecture: v3.2 (Feb 2026)
"""

import os
import sys
import json
import logging
import argparse
import warnings
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass

import pandas as pd
import numpy as np
from tqdm import tqdm

# Suppress pandas warnings
warnings.filterwarnings('ignore', category=pd.errors.PerformanceWarning)
warnings.filterwarnings('ignore', category=RuntimeWarning)

# ============================================================================
# CONFIGURATION
# ============================================================================

# Project paths
PROJECT_ROOT = Path(__file__).parent.parent
DATA_CACHE_DIR = PROJECT_ROOT / "data_cache"
DATA_LOAD_DIR = PROJECT_ROOT.parent / "data_load" / "data_cache"
# Input
QUALIFIED_DIR = DATA_CACHE_DIR / "qualified"
CONSOLIDATED_DIR = DATA_LOAD_DIR / "consolidated"
# Output
INDICATORS_DIR = DATA_CACHE_DIR / "indicators"
METADATA_DIR = DATA_CACHE_DIR / "metadata"
LOG_DIR = PROJECT_ROOT / "logs"

# Indicator parameters (from Architecture v3.2)
INDICATOR_PARAMS = {
    'sma_fast': 50,      # Fast moving average period
    'sma_slow': 200,     # Slow moving average period
    'atr_period': 20,    # Average True Range period
    'adx_period': 14,    # Average Directional Index period
}

# Validation thresholds
#
# MIN_HISTORY_DAYS = 252 (Architecture v3.2 standard, matches Script 4 screening filter)
#   - Script 1 initial mode downloads ~400 calendar days = ~275 trading days
#   - 252 trading days = 1 year; sufficient to compute SMA_200 with a small buffer
#   - DO NOT raise above 275 or every symbol from an initial download will be rejected
#
# MIN_VALID_INDICATORS = 20 (minimum rows where all four indicators are non-NaN)
#   - SMA_200 needs 200 bars before producing values; with 275 total bars only
#     ~75 rows will have valid SMA_200 (275 - 200 + 1)
#   - Downstream scripts only use the LATEST row; 20 ensures a meaningful
#     recent window without over-constraining the dataset
MIN_HISTORY_DAYS = 252
MIN_VALID_INDICATORS = 20

# ============================================================================
# LOGGING SETUP
# ============================================================================

def setup_logging() -> logging.Logger:
    """Configure logging with file and console output"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_file = LOG_DIR / f'calculate_indicators_{timestamp}.log'
    
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
    logger.info(f"Project root: {PROJECT_ROOT}")
    logger.info(f"Indicators directory: {INDICATORS_DIR}")
    
    return logger

logger = setup_logging()

# ============================================================================
# INDICATOR CALCULATION CLASSES
# ============================================================================

@dataclass
class IndicatorResult:
    """Result of indicator calculation for a symbol"""
    symbol: str
    status: str  # 'success', 'insufficient_data', 'error', 'skipped'
    records_total: int = 0
    records_valid: int = 0
    indicators_calculated: List[str] = None
    error_message: Optional[str] = None
    
    def __post_init__(self):
        if self.indicators_calculated is None:
            self.indicators_calculated = []

class IndicatorCalculator:
    """
    Production-grade technical indicator calculator
    
    Implements academically-validated formulas from Architecture v3.2:
    - Simple Moving Average (SMA)
    - Average True Range (ATR) percentage
    - Average Directional Index (ADX) using Wilder's method
    
    All calculations use vectorized pandas operations for performance
    """
    
    def __init__(self, params: Dict = None):
        """
        Initialize calculator with indicator parameters
        
        Args:
            params: Dictionary of indicator parameters (uses defaults if None)
        """
        self.params = params or INDICATOR_PARAMS
        logger.info(f"Initialized IndicatorCalculator with params: {self.params}")
    
    def calculate_sma(self, df: pd.DataFrame, period: int, price_col: str = 'adjusted_close') -> pd.Series:
        """
        Simple Moving Average
        
        Formula:
            SMA(n) = (Price[0] + Price[1] + ... + Price[n-1]) / n
        
        Args:
            df: Price dataframe
            period: Number of periods for moving average
            price_col: Column to use for calculation (default: adjusted_close)
        
        Returns:
            Series with SMA values (NaN for first n-1 periods)
        
        Notes:
            - Uses adjusted_close to account for splits/dividends
            - First (period - 1) values will be NaN
            - Robust to missing data (min_periods=period ensures no partial calculations)
        """
        if price_col not in df.columns:
            raise ValueError(f"Column '{price_col}' not found in dataframe")
        
        sma = df[price_col].rolling(window=period, min_periods=period).mean()
        
        return sma
    
    def calculate_atr_pct(self, df: pd.DataFrame, period: int = 20) -> pd.Series:
        """
        Average True Range (percentage of close)
        
        Formula:
            TR = max(High - Low, |High - Close_prev|, |Low - Close_prev|)
            ATR(n) = SMA(TR, n)
            ATR_pct = (ATR / Close) x 100
        
        Args:
            df: OHLC dataframe
            period: Number of periods for ATR calculation
        
        Returns:
            Series with ATR as percentage of close price
        
        Notes:
            - Uses actual OHLC prices (not adjusted) for true range
            - Percentage normalization allows comparison across instruments
            - First period values will be NaN
        """
        required_cols = ['high', 'low', 'close']
        for col in required_cols:
            if col not in df.columns:
                raise ValueError(f"Column '{col}' not found in dataframe")
        
        high = df['high']
        low = df['low']
        close = df['close']
        prev_close = close.shift(1)
        
        # Calculate true range components
        tr1 = high - low
        tr2 = abs(high - prev_close)
        tr3 = abs(low - prev_close)
        
        # True range is maximum of the three components
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        
        # Average true range (simple moving average of TR)
        atr = tr.rolling(window=period, min_periods=period).mean()
        
        # Convert to percentage of close price
        atr_pct = (atr / close) * 100
        
        return atr_pct
    
    def calculate_adx(self, df: pd.DataFrame, period: int = 14) -> pd.Series:
        """
        Average Directional Index (Wilder's method)
        
        Formula:
            +DM = High[t] - High[t-1] if positive, else 0
            -DM = Low[t-1] - Low[t] if positive, else 0
            
            TR = max(High - Low, |High - Close_prev|, |Low - Close_prev|)
            
            Smoothed +DM = EMA(+DM, period, alpha=1/period)
            Smoothed -DM = EMA(-DM, period, alpha=1/period)
            Smoothed TR = EMA(TR, period, alpha=1/period)
            
            +DI = 100 x (Smoothed +DM / Smoothed TR)
            -DI = 100 x (Smoothed -DM / Smoothed TR)
            
            DX = 100 x |+DI - -DI| / (+DI + -DI)
            ADX = EMA(DX, period, alpha=1/period)
        
        Args:
            df: OHLC dataframe
            period: Number of periods for ADX calculation (default 14)
        
        Returns:
            Series with ADX values (0-100 scale)
        
        Notes:
            - Uses Wilder's smoothing (EMA with alpha = 1/period)
            - ADX > 20 indicates trending market
            - ADX > 40 indicates strong trend
            - Direction-neutral (measures trend strength, not direction)
        """
        required_cols = ['high', 'low', 'close']
        for col in required_cols:
            if col not in df.columns:
                raise ValueError(f"Column '{col}' not found in dataframe")
        
        high = df['high']
        low = df['low']
        close = df['close']
        
        # Calculate directional movement
        plus_dm = high.diff()
        minus_dm = -low.diff()
        
        # Keep only positive movements
        plus_dm[plus_dm < 0] = 0
        minus_dm[minus_dm < 0] = 0
        
        # Calculate true range
        prev_close = close.shift(1)
        tr1 = high - low
        tr2 = abs(high - prev_close)
        tr3 = abs(low - prev_close)
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        
        # Wilder's smoothing (EMA with alpha = 1/period)
        alpha = 1.0 / period
        
        # Smooth the directional movements and true range
        smoothed_plus_dm = plus_dm.ewm(alpha=alpha, adjust=False).mean()
        smoothed_minus_dm = minus_dm.ewm(alpha=alpha, adjust=False).mean()
        smoothed_tr = tr.ewm(alpha=alpha, adjust=False).mean()
        
        # Calculate directional indicators
        plus_di = 100 * smoothed_plus_dm / smoothed_tr
        minus_di = 100 * smoothed_minus_dm / smoothed_tr
        
        # Calculate directional index
        dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di)
        
        # Replace inf/nan from division by zero
        dx = dx.replace([np.inf, -np.inf], np.nan)
        
        # Smooth to get ADX
        adx = dx.ewm(alpha=alpha, adjust=False).mean()
        
        return adx
    
    def calculate_all_indicators(
        self,
        df: pd.DataFrame,
        as_of_date: Optional[str] = None
    ) -> pd.DataFrame:
        """
        Calculate all required technical indicators
        
        Args:
            df: Consolidated price dataframe
            as_of_date: Optional cutoff date (YYYY-MM-DD) - only calculate up to this date
        
        Returns:
            DataFrame with original data plus indicator columns:
                - sma_50: 50-period SMA
                - sma_200: 200-period SMA
                - atr_20_pct: 20-period ATR (percentage)
                - adx_14: 14-period ADX
        
        Raises:
            ValueError: If insufficient data for calculations
        """
        # Validate minimum history requirement (252 trading days = 1 year)
        # Script 1 initial mode delivers ~275 trading days (400 calendar days).
        # The SMA_200 will produce NaN for the first 199 rows, leaving ~75
        # valid rows on a fresh download â€” that is intentional and sufficient.
        if len(df) < MIN_HISTORY_DAYS:
            raise ValueError(
                f"Insufficient history: {len(df)} days < {MIN_HISTORY_DAYS} required"
            )
        
        # Create copy to avoid modifying original
        result_df = df.copy()
        
        # Calculate each indicator
        logger.debug(f"  Calculating SMA_50...")
        result_df['sma_50'] = self.calculate_sma(result_df, self.params['sma_fast'])
        
        logger.debug(f"  Calculating SMA_200...")
        result_df['sma_200'] = self.calculate_sma(result_df, self.params['sma_slow'])
        
        logger.debug(f"  Calculating ATR_20_pct...")
        result_df['atr_20_pct'] = self.calculate_atr_pct(result_df, self.params['atr_period'])
        
        logger.debug(f"  Calculating ADX_14...")
        result_df['adx_14'] = self.calculate_adx(result_df, self.params['adx_period'])
        
        # Filter to as_of_date if specified
        if as_of_date:
            as_of_dt = pd.to_datetime(as_of_date)
            result_df = result_df[result_df.index <= as_of_dt]
        
        # Validate sufficient valid indicators after calculation
        # (200 SMA needs 200 bars, so first 200 rows will have NaN)
        valid_rows = result_df[['sma_50', 'sma_200', 'atr_20_pct', 'adx_14']].dropna()
        
        if len(valid_rows) < MIN_VALID_INDICATORS:
            raise ValueError(
                f"Insufficient valid indicators: {len(valid_rows)} rows < {MIN_VALID_INDICATORS} required"
            )
        
        logger.debug(f"  [OK] Calculated {len(result_df)} rows, {len(valid_rows)} with valid indicators")
        
        return result_df

# ============================================================================
# DATA LOADING FUNCTIONS
# ============================================================================

def load_qualified_symbols() -> List[str]:
    """
    Load list of qualified symbols from screening output

    Script 4 saves qualified_symbols.json as a list of dicts:
        [{"symbol": "AAPL.US", "name": "Apple Inc.", "exchange": "NASDAQ", ...}, ...]

    This function extracts only the symbol string from each entry, and also
    handles a plain list-of-strings format for forward compatibility.

    Returns:
        List of symbol code strings (e.g., ['AAPL.US', 'MSFT.US', ...])
    """
    qualified_file = QUALIFIED_DIR / 'qualified_symbols.json'

    if not qualified_file.exists():
        logger.error(f"Qualified symbols file not found: {qualified_file}")
        logger.error("Please run Script 4 (04_screen_universe.py) first to screen universe")
        return []

    try:
        with open(qualified_file, 'r') as f:
            raw = json.load(f)

        if not isinstance(raw, list) or len(raw) == 0:
            logger.error("qualified_symbols.json is empty or not a list")
            return []

        # Script 4 stores list-of-dicts; extract the 'symbol' key from each
        first = raw[0]
        if isinstance(first, dict):
            if 'symbol' not in first:
                logger.error(
                    f"Unexpected dict format in qualified_symbols.json "
                    f"(keys: {list(first.keys())}). Expected 'symbol' key."
                )
                return []
            symbols = [item['symbol'] for item in raw if isinstance(item, dict) and 'symbol' in item]
            logger.info(f"[OK] Loaded {len(symbols)} qualified symbols (parsed from list-of-dicts)")
        elif isinstance(first, str):
            # Plain list-of-strings format
            symbols = raw
            logger.info(f"[OK] Loaded {len(symbols)} qualified symbols (plain string list)")
        else:
            logger.error(f"Unrecognised format in qualified_symbols.json: {type(first)}")
            return []

        return symbols

    except Exception as e:
        logger.error(f"Error loading qualified symbols: {e}")
        return []

def load_consolidated_data(symbol: str) -> pd.DataFrame:
    """
    Load consolidated price data for a symbol
    
    Args:
        symbol: Symbol code (e.g., 'AAPL.US')
    
    Returns:
        DataFrame with OHLCV data and metadata
    
    Raises:
        FileNotFoundError: If consolidated file doesn't exist
    """
    consolidated_file = CONSOLIDATED_DIR / f"{symbol}.parquet"
    
    if not consolidated_file.exists():
        raise FileNotFoundError(f"Consolidated data not found for {symbol}")
    
    df = pd.read_parquet(consolidated_file)
    
    # Ensure date index is datetime
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index)
    
    # Sort by date
    df = df.sort_index()
    
    return df

# ============================================================================
# INDICATOR CALCULATION WORKFLOW
# ============================================================================

def calculate_indicators_for_symbol(
    symbol: str,
    calculator: IndicatorCalculator,
    as_of_date: Optional[str] = None,
    force: bool = False
) -> IndicatorResult:
    """
    Calculate all indicators for a single symbol
    
    Args:
        symbol: Symbol code
        calculator: IndicatorCalculator instance
        as_of_date: Optional cutoff date
        force: If True, overwrite existing indicator file
    
    Returns:
        IndicatorResult with calculation status
    """
    result = IndicatorResult(symbol=symbol, status='unknown')
    
    try:
        # Check if indicators already calculated (unless force=True)
        output_file = INDICATORS_DIR / f"{symbol}_indicators.parquet"
        
        if output_file.exists() and not force:
            logger.debug(f"  {symbol}: Indicators already calculated (use --force to recalculate)")
            result.status = 'skipped'
            return result
        
        # Load consolidated data
        logger.debug(f"  {symbol}: Loading consolidated data...")
        df = load_consolidated_data(symbol)
        result.records_total = len(df)
        
        # Calculate indicators
        logger.debug(f"  {symbol}: Calculating indicators...")
        df_with_indicators = calculator.calculate_all_indicators(df, as_of_date)
        
        # Count valid indicator rows
        valid_indicators = df_with_indicators[['sma_50', 'sma_200', 'atr_20_pct', 'adx_14']].dropna()
        result.records_valid = len(valid_indicators)
        result.indicators_calculated = ['sma_50', 'sma_200', 'atr_20_pct', 'adx_14']
        
        # Save to parquet
        INDICATORS_DIR.mkdir(parents=True, exist_ok=True)
        df_with_indicators.to_parquet(output_file, compression='snappy')
        
        result.status = 'success'
        logger.info(f"  {symbol}: [OK] Calculated indicators ({result.records_valid} valid rows)")
        
        return result
    
    except FileNotFoundError as e:
        result.status = 'no_data'
        result.error_message = str(e)
        logger.warning(f"  {symbol}: No consolidated data found")
        return result
    
    except ValueError as e:
        result.status = 'insufficient_data'
        result.error_message = str(e)
        logger.warning(f"  {symbol}: {e}")
        return result
    
    except Exception as e:
        result.status = 'error'
        result.error_message = str(e)
        logger.error(f"  {symbol}: Error - {e}")
        return result

def calculate_indicators_batch(
    symbols: List[str],
    as_of_date: Optional[str] = None,
    force: bool = False
) -> Dict:
    """
    Calculate indicators for multiple symbols with progress tracking
    
    Args:
        symbols: List of symbol codes
        as_of_date: Optional cutoff date
        force: If True, recalculate all indicators
    
    Returns:
        Dictionary with calculation statistics
    """
    logger.info(f"\n{'='*80}")
    logger.info(f"INDICATOR CALCULATION BATCH")
    logger.info(f"{'='*80}")
    logger.info(f"Symbols to process: {len(symbols)}")
    logger.info(f"As-of date: {as_of_date or 'Latest available'}")
    logger.info(f"Force recalculation: {force}")
    logger.info(f"Indicator parameters: {INDICATOR_PARAMS}")
    logger.info("")
    
    # Initialize calculator
    calculator = IndicatorCalculator(INDICATOR_PARAMS)
    
    # Process each symbol
    results = []
    
    logger.info("Processing symbols...")
    for symbol in tqdm(symbols, desc="Calculating indicators", unit="symbol"):
        result = calculate_indicators_for_symbol(symbol, calculator, as_of_date, force)
        results.append(result)
    
    # Aggregate statistics
    stats = {
        'total_symbols': len(symbols),
        'successful': sum(1 for r in results if r.status == 'success'),
        'skipped': sum(1 for r in results if r.status == 'skipped'),
        'insufficient_data': sum(1 for r in results if r.status == 'insufficient_data'),
        'no_data': sum(1 for r in results if r.status == 'no_data'),
        'errors': sum(1 for r in results if r.status == 'error'),
        'calculation_date': datetime.now().isoformat(),
        'as_of_date': as_of_date,
        'force_recalculation': force,
        'parameters': INDICATOR_PARAMS,
        'results': [
            {
                'symbol': r.symbol,
                'status': r.status,
                'records_total': r.records_total,
                'records_valid': r.records_valid,
                'indicators': r.indicators_calculated,
                'error': r.error_message
            }
            for r in results
        ]
    }
    
    # Save calculation log
    METADATA_DIR.mkdir(parents=True, exist_ok=True)
    log_file = METADATA_DIR / 'indicator_calculation_log.json'
    
    with open(log_file, 'w') as f:
        json.dump(stats, f, indent=2)
    
    # Print summary
    logger.info(f"\n{'='*80}")
    logger.info(f"CALCULATION SUMMARY")
    logger.info(f"{'='*80}")
    logger.info(f"Total symbols:        {stats['total_symbols']}")
    logger.info(f"Successfully calculated: {stats['successful']}")
    logger.info(f"Skipped (existing):   {stats['skipped']}")
    logger.info(f"Insufficient data:    {stats['insufficient_data']}")
    logger.info(f"No data available:    {stats['no_data']}")
    logger.info(f"Errors:               {stats['errors']}")
    logger.info(f"\nCalculation log saved: {log_file}")
    
    # List symbols with errors or insufficient data
    error_symbols = [r.symbol for r in results if r.status in ['error', 'insufficient_data']]
    if error_symbols:
        logger.warning(f"\nSymbols with errors/insufficient data ({len(error_symbols)}):")
        for sym in error_symbols[:10]:
            match = next((r for r in results if r.symbol == sym), None)
            detail = match.error_message if match else ''
            logger.warning(f"  {sym}: {detail}")
        if len(error_symbols) > 10:
            logger.warning(f"  ... and {len(error_symbols) - 10} more (see indicator_calculation_log.json)")

    # List a sample of "no data" symbols to help diagnose path mismatches
    no_data_symbols = [r.symbol for r in results if r.status == 'no_data']
    if no_data_symbols:
        logger.warning(f"\nSymbols with no consolidated data ({len(no_data_symbols)}):")
        logger.warning(f"  Sample (first 5): {no_data_symbols[:5]}")
        logger.warning(f"  Expected location: {CONSOLIDATED_DIR}/<symbol>.parquet")
        logger.warning(f"  Available files:   {len(list(CONSOLIDATED_DIR.glob('*.parquet')))} files in {CONSOLIDATED_DIR}")
    
    return stats

# ============================================================================
# VALIDATION FUNCTIONS
# ============================================================================

def validate_environment() -> bool:
    """
    Validate that all required directories and files exist
    
    Returns:
        True if environment is valid, False otherwise
    """
    logger.info("Validating environment...")
    
    all_valid = True
    
    # Check qualified symbols file
    qualified_file = QUALIFIED_DIR / 'qualified_symbols.json'
    if not qualified_file.exists():
        logger.error(f"[FAIL] Qualified symbols not found: {qualified_file}")
        logger.error("   Run Script 4 (04_screen_universe.py) first")
        all_valid = False
    else:
        logger.info(f"[OK] Qualified symbols file exists")
    
    # Check consolidated directory
    if not CONSOLIDATED_DIR.exists():
        logger.error(f"[FAIL] Consolidated data directory not found: {CONSOLIDATED_DIR}")
        logger.error("   Run Script 3 (03_consolidate_validate_data.py) first")
        all_valid = False
    else:
        parquet_count = len(list(CONSOLIDATED_DIR.glob('*.parquet')))
        if parquet_count == 0:
            logger.error(f"[FAIL] No consolidated data files found in {CONSOLIDATED_DIR}")
            logger.error("   Run Script 3 (03_consolidate_validate_data.py) first")
            all_valid = False
        else:
            logger.info(f"[OK] Found {parquet_count} consolidated data files")
    
    return all_valid

# ============================================================================
# MAIN EXECUTION
# ============================================================================

def parse_arguments():
    """Parse command-line arguments"""
    parser = argparse.ArgumentParser(
        description='Calculate technical indicators for qualified universe',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Calculate indicators as of specific date
  python scripts/05_calculate_indicators.py --as-of-date 2026-01-31
  
  # Calculate for specific symbols only
  python scripts/05_calculate_indicators.py --symbols AAPL.US,MSFT.US
  
  # Force recalculation (overwrite existing)
  python scripts/05_calculate_indicators.py --force
  
  # Combine options
  python scripts/05_calculate_indicators.py --as-of-date 2026-01-31 --force
        """
    )
    
    parser.add_argument(
        '--as-of-date',
        type=str,
        help='Calculate indicators only up to this date (YYYY-MM-DD). If not specified, uses latest available data.'
    )
    
    parser.add_argument(
        '--symbols',
        type=str,
        help='Comma-separated list of symbols to process (e.g., AAPL.US,MSFT.US). If not specified, processes all qualified symbols.'
    )
    
    parser.add_argument(
        '--force',
        action='store_true',
        help='Force recalculation even if indicator files already exist'
    )
    
    return parser.parse_args()

def main():
    """Main execution function"""
    # Parse arguments
    args = parse_arguments()
    
    # Log execution parameters
    logger.info(f"\n{'='*80}")
    logger.info(f"TECHNICAL INDICATOR CALCULATOR - Script 5")
    logger.info(f"{'='*80}")
    logger.info(f"Execution time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"Parameters:")
    logger.info(f"  As-of date: {args.as_of_date or 'Latest available'}")
    logger.info(f"  Symbols filter: {args.symbols or 'All qualified symbols'}")
    logger.info(f"  Force recalculation: {args.force}")
    logger.info("")
    
    # Validate environment
    if not validate_environment():
        logger.error("\nEnvironment validation failed. Cannot proceed.")
        logger.error("Please ensure you have run Scripts 1-4 successfully.")
        sys.exit(1)
    
    # Load symbols to process
    if args.symbols:
        # Use specified symbols
        symbols = [s.strip() for s in args.symbols.split(',')]
        logger.info(f"Processing {len(symbols)} specified symbols")
    else:
        # Load all qualified symbols
        symbols = load_qualified_symbols()
        
        if len(symbols) == 0:
            logger.error("No qualified symbols found. Cannot proceed.")
            logger.error("Please run Script 4 (04_screen_universe.py) first.")
            sys.exit(1)
    
    # Validate as-of-date format if provided
    if args.as_of_date:
        try:
            datetime.strptime(args.as_of_date, '%Y-%m-%d')
            logger.info(f"[OK] As-of date format validated: {args.as_of_date}")
        except ValueError:
            logger.error(f"Invalid as-of-date format: {args.as_of_date}")
            logger.error("Expected format: YYYY-MM-DD (e.g., 2026-01-31)")
            sys.exit(1)
    
    # Calculate indicators
    try:
        stats = calculate_indicators_batch(
            symbols=symbols,
            as_of_date=args.as_of_date,
            force=args.force
        )
        
        # Check for critical failures
        if stats['errors'] > len(symbols) * 0.1:  # More than 10% errors
            logger.warning(f"\n[WARN] Warning: {stats['errors']} symbols failed calculation")
            logger.warning("This is more than 10% of the total. Please investigate.")
        
        # 'skipped' means the indicator file already exists from a prior run
        # and --force was not set — that is a valid outcome, the data is still
        # present and downstream scripts will read it normally.
        usable = stats['successful'] + stats['skipped']
        if usable == 0:
            logger.error("\n[FAIL] No symbols were successfully processed!")
            logger.error(
                "All symbols were either errored or had insufficient data. "
                "Re-run with --force to recalculate existing indicator files."
            )
            logger.error("Please check the logs for errors and ensure data quality.")
            sys.exit(1)

        if stats['skipped'] > 0 and stats['successful'] == 0:
            logger.info(
                f"\n[OK] All {stats['skipped']} indicator files already up-to-date "
                f"(use --force to recalculate)."
            )
        
        logger.info(f"\n[OK] Script completed successfully")
        logger.info(f"Indicators calculated for {stats['successful']} symbols")
        logger.info(f"Output directory: {INDICATORS_DIR}")
        
    except Exception as e:
        logger.error(f"\n[FAIL] Fatal error during execution: {e}")
        logger.exception("Full traceback:")
        sys.exit(1)

if __name__ == '__main__':
    main()
