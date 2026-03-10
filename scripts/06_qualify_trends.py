#!/usr/bin/env python3
"""
Script 6: Trend Qualifier
==========================
Identify instruments in confirmed uptrends by applying the three-condition
trend qualification filter to all screened symbols.

Purpose:
    Reduce the screened universe to instruments that are in a verified,
    rule-based uptrend. This is the gating step before momentum ranking.
    Only instruments that pass ALL three conditions are forwarded to Script 7.

Trend Qualification Rules (ALL must be TRUE simultaneously):
    Rule 1 - Golden Cross:     SMA_50  > SMA_200        (long-term alignment)
    Rule 2 - Price Alignment:  Close   > SMA_50         (price above short-term trend)
    Rule 3 - Trend Strength:   ADX_14  > 20             (direction confirmed, not ranging)

    Rationale:
        - SMA_50 > SMA_200: Filters out instruments in long-term downtrends.
          A market cannot be in a valid uptrend if the short-term average is
          below the long-term average (Faber 2007, Moskowitz et al. 2012).
        - Close > SMA_50: Ensures the current price is participating in the
          trend, not just that a crossover occurred in the past.
        - ADX_14 > 20: Confirms that directional movement is present. ADX < 20
          indicates a ranging, directionless market where trend-following
          strategies underperform (Wilder 1978, industry standard threshold).

    All three conditions must be TRUE on the as-of-date (last available bar).
    Partial qualification is not accepted.

Dependencies:
    - Script 4: Universe screener (must be run first)
    - Script 5: Indicator calculator (must be run first)

Inputs:
    - data_cache/qualified/qualified_symbols.json     (screened symbol list)
    - data_cache/indicators/{symbol}_indicators.parquet (per-symbol indicators)
    - config/strategy_parameters.json                 (ADX threshold etc.)

Outputs:
    - data_cache/signals/qualified_trends.json         (passing symbols — input to Script 7)
    - data_cache/signals/trend_qualification_report.csv (all symbols, reasons)
    - data_cache/signals/trend_qualification_summary.json (aggregate statistics)

Execution:
    python scripts/06_qualify_trends.py --as-of-date 2026-01-31
    python scripts/06_qualify_trends.py --as-of-date 2026-01-31 --max-symbols 200
    python scripts/06_qualify_trends.py --as-of-date 2026-01-31 --min-adx 25

Architecture: v3.2 (Feb 2026)
"""

import os
import sys
import json
import logging
import argparse
from dataclasses import dataclass, field, asdict
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
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_CACHE_DIR = PROJECT_ROOT / "data_cache"
QUALIFIED_DIR = DATA_CACHE_DIR / "qualified"
INDICATORS_DIR = DATA_CACHE_DIR / "indicators"
SIGNALS_DIR = DATA_CACHE_DIR / "signals"
LOG_DIR = PROJECT_ROOT / "logs"

# Default thresholds (overridable via config or CLI)
DEFAULT_ADX_THRESHOLD = 20          # ADX > 20 = trending (Wilder standard)
DEFAULT_MIN_DATA_POINTS = 200       # Minimum rows needed to have valid SMA_200


# ============================================================================
# LOGGING SETUP
# ============================================================================

def setup_logging() -> logging.Logger:
    """Configure logging with both file and console output."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_file = LOG_DIR / f'qualify_trends_{timestamp}.log'

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
# DATA CLASSES
# ============================================================================

@dataclass
class TrendCheckResult:
    """
    Captures the outcome of evaluating a single instrument.

    Attributes:
        symbol:             Instrument identifier (e.g., 'AAPL.US')
        qualified:          True only when all three conditions pass
        rule_golden_cross:  SMA_50 > SMA_200
        rule_price_above:   Close > SMA_50
        rule_adx_strength:  ADX_14 > adx_threshold
        failure_reason:     Human-readable reason for first failing condition
        sma_50:             Value of SMA_50 on as-of-date
        sma_200:            Value of SMA_200 on as-of-date
        close:              Adjusted close on as-of-date
        adx_14:             ADX_14 value on as-of-date
        atr_20_pct:         ATR_20 as % of close on as-of-date
        sma_spread_pct:     (SMA_50 - SMA_200) / SMA_200 * 100 — trend margin
        price_vs_sma50_pct: (Close - SMA_50) / SMA_50 * 100 — price headroom
        trend_days:         Consecutive calendar days all 3 rules have been TRUE
        name:               Company name (if available in indicators file)
        exchange:           Exchange code
        sector:             Sector (if available)
        as_of_date:         The evaluation date
        error:              Error message if processing failed
    """
    symbol: str
    qualified: bool = False
    rule_golden_cross: Optional[bool] = None
    rule_price_above: Optional[bool] = None
    rule_adx_strength: Optional[bool] = None
    failure_reason: str = ""
    sma_50: Optional[float] = None
    sma_200: Optional[float] = None
    close: Optional[float] = None
    adx_14: Optional[float] = None
    atr_20_pct: Optional[float] = None
    sma_spread_pct: Optional[float] = None      # (SMA_50 - SMA_200) / SMA_200 * 100
    price_vs_sma50_pct: Optional[float] = None  # (Close - SMA_50) / SMA_50 * 100
    trend_days: Optional[int] = None            # Consecutive days in qualified state
    name: str = ""
    exchange: str = ""
    sector: str = ""
    as_of_date: str = ""
    error: str = ""


# ============================================================================
# CONFIGURATION LOADING
# ============================================================================

def load_strategy_parameters() -> Dict:
    """
    Load strategy parameters from config/strategy_parameters.json.

    Falls back to built-in defaults if the file does not exist,
    so the script runs standalone without requiring full config setup.

    Returns:
        Dictionary of strategy parameters
    """
    config_file = CONFIG_DIR / 'strategy_parameters.json'

    if not config_file.exists():
        logger.warning(
            f"strategy_parameters.json not found at {config_file}. "
            f"Using built-in defaults (ADX threshold = {DEFAULT_ADX_THRESHOLD})."
        )
        return {
            "indicators": {"adx_period": 14},
            "entry": {
                "trend_qualification": {
                    "sma_50_above_sma_200": True,
                    "close_above_sma_50": True,
                    "adx_minimum": DEFAULT_ADX_THRESHOLD
                }
            }
        }

    with open(config_file, 'r') as f:
        params = json.load(f)

    adx_min = params.get("entry", {}).get("trend_qualification", {}).get("adx_minimum", DEFAULT_ADX_THRESHOLD)
    logger.info(f"✓ Loaded strategy parameters  |  ADX minimum = {adx_min}")
    return params


def load_qualified_symbols() -> List[str]:
    """
    Load the list of symbols that passed universe screening (Script 4).

    Returns:
        List of symbol codes

    Exits:
        Immediately if the qualified_symbols.json file is missing.
    """
    symbols_file = QUALIFIED_DIR / 'qualified_symbols.json'

    if not symbols_file.exists():
        logger.error(f"qualified_symbols.json not found at {symbols_file}")
        logger.error("Please run Script 4 (04_screen_universe.py) first.")
        sys.exit(1)

    with open(symbols_file, 'r') as f:
        data = json.load(f)

    # Support both formats: plain list or wrapped dict
    if isinstance(data, list):
        symbols = [item['symbol'] if isinstance(item, dict) else item for item in data]
    elif isinstance(data, dict) and 'symbols' in data:
        symbols = [item['symbol'] if isinstance(item, dict) else item for item in data['symbols']]
    else:
        logger.error(f"Unexpected format in qualified_symbols.json: {type(data)}")
        sys.exit(1)

    logger.info(f"✓ Loaded {len(symbols):,} symbols from qualified_symbols.json")
    return symbols


# ============================================================================
# INDICATOR LOADING
# ============================================================================

def load_indicators(symbol: str) -> Optional[pd.DataFrame]:
    """
    Load the pre-calculated indicators parquet for a symbol (Script 5 output).

    Args:
        symbol: Symbol code (e.g., 'AAPL.US')

    Returns:
        DataFrame indexed by date, or None if file not found / unreadable.
    """
    indicator_file = INDICATORS_DIR / f"{symbol}_indicators.parquet"

    if not indicator_file.exists():
        logger.debug(f"No indicators file for {symbol}")
        return None

    try:
        df = pd.read_parquet(indicator_file)

        # Normalise index to DatetimeIndex
        if 'date' in df.columns and df.index.name != 'date':
            df = df.set_index('date')

        if not isinstance(df.index, pd.DatetimeIndex):
            df.index = pd.to_datetime(df.index)

        df.sort_index(inplace=True)
        return df

    except Exception as e:
        logger.warning(f"Could not load indicators for {symbol}: {e}")
        return None


def get_latest_row(df: pd.DataFrame, as_of_date: str) -> Optional[pd.Series]:
    """
    Return the most recent row on or before as_of_date.

    Args:
        df:          Indicator DataFrame with DatetimeIndex
        as_of_date:  Date string 'YYYY-MM-DD'

    Returns:
        Last row as pd.Series, or None if no data exists on/before the date.
    """
    cutoff = pd.Timestamp(as_of_date)
    mask = df.index <= cutoff
    if not mask.any():
        return None
    return df.loc[mask].iloc[-1]


# ============================================================================
# TREND QUALIFICATION LOGIC
# ============================================================================

def evaluate_trend(
    symbol: str,
    df: pd.DataFrame,
    as_of_date: str,
    adx_threshold: float
) -> TrendCheckResult:
    """
    Apply all three trend qualification rules to a single instrument.

    Rules (ALL must be TRUE):
        1. SMA_50 > SMA_200   — Golden cross (long-term alignment)
        2. Close  > SMA_50    — Price is above the short-term trend
        3. ADX_14 > threshold — Sufficient directional movement (default: 20)

    Diagnostics computed for passing instruments:
        - sma_spread_pct:     How far SMA_50 is above SMA_200 (trend margin)
        - price_vs_sma50_pct: How far Close is above SMA_50 (price headroom)
        - trend_days:         Consecutive days all three rules have been TRUE

    Args:
        symbol:        Instrument identifier
        df:            Indicator DataFrame indexed by date
        as_of_date:    Evaluation date (YYYY-MM-DD)
        adx_threshold: Minimum ADX value required

    Returns:
        Populated TrendCheckResult dataclass
    """
    result = TrendCheckResult(symbol=symbol, as_of_date=as_of_date)

    # ----------------------------------------------------------------
    # Validate required columns exist
    # ----------------------------------------------------------------
    required_cols = ['sma_50', 'sma_200', 'adx_14', 'atr_20_pct']
    close_col = 'adjusted_close' if 'adjusted_close' in df.columns else 'close'
    required_cols.append(close_col)

    missing_cols = [c for c in required_cols if c not in df.columns]
    if missing_cols:
        result.error = f"Missing columns: {', '.join(missing_cols)}"
        result.failure_reason = result.error
        return result

    # ----------------------------------------------------------------
    # Get the most-recent row on or before as_of_date
    # ----------------------------------------------------------------
    latest = get_latest_row(df, as_of_date)
    if latest is None:
        result.error = f"No data on or before {as_of_date}"
        result.failure_reason = result.error
        return result

    # ----------------------------------------------------------------
    # Extract indicator values and check for NaN
    # ----------------------------------------------------------------
    sma_50   = latest.get('sma_50')
    sma_200  = latest.get('sma_200')
    close    = latest.get(close_col)
    adx      = latest.get('adx_14')
    atr_pct  = latest.get('atr_20_pct')

    if any(pd.isna(v) for v in [sma_50, sma_200, close, adx]):
        missing = [n for n, v in [('sma_50', sma_50), ('sma_200', sma_200),
                                   ('close', close), ('adx_14', adx)] if pd.isna(v)]
        result.error = f"NaN values in required indicators: {', '.join(missing)}"
        result.failure_reason = result.error
        return result

    # Store raw values
    result.sma_50    = round(float(sma_50),  4)
    result.sma_200   = round(float(sma_200), 4)
    result.close     = round(float(close),   4)
    result.adx_14    = round(float(adx),     2)
    result.atr_20_pct = round(float(atr_pct), 4) if not pd.isna(atr_pct) else None

    # ----------------------------------------------------------------
    # Populate optional metadata (best-effort from indicator file)
    # ----------------------------------------------------------------
    for meta_col, attr in [('name', 'name'), ('exchange', 'exchange'), ('sector', 'sector')]:
        if meta_col in latest.index and not pd.isna(latest[meta_col]):
            setattr(result, attr, str(latest[meta_col]))

    # ----------------------------------------------------------------
    # Rule 1: Golden Cross  —  SMA_50 > SMA_200
    # ----------------------------------------------------------------
    result.rule_golden_cross = bool(sma_50 > sma_200)
    if not result.rule_golden_cross:
        result.failure_reason = (
            f"FAIL Rule 1 — Golden cross: SMA_50 ({sma_50:.2f}) ≤ SMA_200 ({sma_200:.2f})"
        )
        return result

    # ----------------------------------------------------------------
    # Rule 2: Price above SMA_50  —  Close > SMA_50
    # ----------------------------------------------------------------
    result.rule_price_above = bool(close > sma_50)
    if not result.rule_price_above:
        result.failure_reason = (
            f"FAIL Rule 2 — Price alignment: Close ({close:.2f}) ≤ SMA_50 ({sma_50:.2f})"
        )
        return result

    # ----------------------------------------------------------------
    # Rule 3: Trend strength  —  ADX_14 > adx_threshold
    # ----------------------------------------------------------------
    result.rule_adx_strength = bool(adx > adx_threshold)
    if not result.rule_adx_strength:
        result.failure_reason = (
            f"FAIL Rule 3 — ADX strength: ADX_14 ({adx:.1f}) ≤ {adx_threshold:.0f}"
        )
        return result

    # ----------------------------------------------------------------
    # All rules passed — compute diagnostic metrics
    # ----------------------------------------------------------------
    result.qualified = True
    result.failure_reason = "PASS — All three conditions met"

    # How far SMA_50 is above SMA_200 (trend margin)
    result.sma_spread_pct = round(((sma_50 - sma_200) / sma_200) * 100, 2)

    # How far close is above SMA_50 (price headroom / momentum proximity)
    result.price_vs_sma50_pct = round(((close - sma_50) / sma_50) * 100, 2)

    # ----------------------------------------------------------------
    # Consecutive qualifying days
    # Scan backward from as_of_date to count how many consecutive bars
    # have also passed all three rules.  Capped at 252 bars for speed.
    # ----------------------------------------------------------------
    result.trend_days = _count_consecutive_qualifying_days(
        df, as_of_date, adx_threshold, close_col, max_lookback=252
    )

    return result


def _count_consecutive_qualifying_days(
    df: pd.DataFrame,
    as_of_date: str,
    adx_threshold: float,
    close_col: str,
    max_lookback: int = 252
) -> int:
    """
    Count consecutive calendar days (bars) ending on as_of_date during which
    all three trend qualification conditions were continuously satisfied.

    This gives a sense of how mature or fresh the trend is:
        - 1 day:   trend just started — interpret with caution
        - 20+ days: trend has persisted through at least one month

    Args:
        df:           Indicator DataFrame
        as_of_date:   End date for evaluation
        adx_threshold: ADX minimum
        close_col:    Column name for close price
        max_lookback: Maximum bars to look back (default 252 = 1 year)

    Returns:
        Number of consecutive qualifying days (minimum 1 if qualified today)
    """
    cutoff = pd.Timestamp(as_of_date)
    window = df.loc[df.index <= cutoff].tail(max_lookback)

    if window.empty:
        return 0

    # Build boolean series: True where ALL three rules pass simultaneously
    cond = (
        (window['sma_50'] > window['sma_200']) &
        (window[close_col] > window['sma_50']) &
        (window['adx_14'] > adx_threshold)
    )

    # Count consecutive True values from the tail (most recent) backward
    consecutive = 0
    for val in reversed(cond.values):
        if val:
            consecutive += 1
        else:
            break

    return consecutive


# ============================================================================
# BATCH PROCESSING
# ============================================================================

def qualify_all_trends(
    symbols: List[str],
    as_of_date: str,
    adx_threshold: float,
    max_symbols: Optional[int] = None
) -> Tuple[List[TrendCheckResult], List[TrendCheckResult]]:
    """
    Run trend qualification for all symbols in the screened universe.

    Args:
        symbols:       List of screened symbol codes (from Script 4)
        as_of_date:    Evaluation date (YYYY-MM-DD)
        adx_threshold: ADX minimum threshold
        max_symbols:   Cap on symbols processed (for testing)

    Returns:
        Tuple of (qualified_results, all_results)
            - qualified_results: Only instruments where qualified == True
            - all_results:       Every instrument (pass and fail), for audit trail
    """
    if max_symbols:
        symbols = symbols[:max_symbols]
        logger.warning(f"⚠  Testing mode: processing only first {max_symbols} symbols")

    total = len(symbols)
    logger.info(f"\nProcessing {total:,} symbols as of {as_of_date}")
    logger.info(f"ADX threshold: {adx_threshold}")
    logger.info("-" * 60)

    all_results: List[TrendCheckResult] = []
    qualified_results: List[TrendCheckResult] = []

    errors = 0
    missing_indicators = 0

    for i, symbol in enumerate(symbols, 1):
        # Progress logging every 500 symbols
        if i % 500 == 0:
            pct = (i / total) * 100
            q_so_far = len(qualified_results)
            logger.info(f"  Progress: {i:,}/{total:,} ({pct:.0f}%)  |  Qualified so far: {q_so_far:,}")

        # Load indicators
        df = load_indicators(symbol)

        if df is None:
            missing_indicators += 1
            result = TrendCheckResult(
                symbol=symbol,
                as_of_date=as_of_date,
                error="Indicators file not found — run Script 5 first",
                failure_reason="ERROR — No indicator data"
            )
            all_results.append(result)
            continue

        # Check sufficient history for SMA_200
        if len(df) < DEFAULT_MIN_DATA_POINTS:
            result = TrendCheckResult(
                symbol=symbol,
                as_of_date=as_of_date,
                error=f"Insufficient data: {len(df)} rows (need {DEFAULT_MIN_DATA_POINTS})",
                failure_reason=f"SKIP — Only {len(df)} bars available"
            )
            all_results.append(result)
            continue

        # Evaluate trend qualification
        try:
            result = evaluate_trend(symbol, df, as_of_date, adx_threshold)
        except Exception as e:
            errors += 1
            logger.debug(f"Exception evaluating {symbol}: {e}")
            result = TrendCheckResult(
                symbol=symbol,
                as_of_date=as_of_date,
                error=str(e),
                failure_reason=f"ERROR — {e}"
            )

        all_results.append(result)

        if result.qualified:
            qualified_results.append(result)

    logger.info("-" * 60)
    logger.info(f"Symbols processed:     {total:,}")
    logger.info(f"Missing indicators:    {missing_indicators:,}")
    logger.info(f"Processing errors:     {errors:,}")
    logger.info(f"Qualified:             {len(qualified_results):,}")
    logger.info(f"Qualification rate:    {len(qualified_results)/total*100:.1f}%")

    return qualified_results, all_results


# ============================================================================
# OUTPUT WRITERS
# ============================================================================

def save_qualified_trends(
    qualified_results: List[TrendCheckResult],
    as_of_date: str,
    adx_threshold: float,
    output_file: Path
):
    """
    Save the qualified trends to JSON.

    This is the primary output consumed by Script 7 (momentum ranker).

    Format:
    {
        "as_of_date": "2026-01-31",
        "adx_threshold": 20,
        "count": 312,
        "generated_at": "<iso timestamp>",
        "symbols": {
            "AAPL.US": {
                "symbol": "AAPL.US",
                "qualified": true,
                "sma_50": 225.14,
                ...
            },
            ...
        }
    }

    Args:
        qualified_results: List of TrendCheckResult where qualified == True
        as_of_date:        Evaluation date
        adx_threshold:     ADX threshold applied
        output_file:       Target path
    """
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)

    symbols_dict = {}
    for r in qualified_results:
        symbols_dict[r.symbol] = {
            "symbol":              r.symbol,
            "qualified":           r.qualified,
            "sma_50":              r.sma_50,
            "sma_200":             r.sma_200,
            "close":               r.close,
            "adx_14":              r.adx_14,
            "atr_20_pct":          r.atr_20_pct,
            "sma_spread_pct":      r.sma_spread_pct,
            "price_vs_sma50_pct":  r.price_vs_sma50_pct,
            "trend_days":          r.trend_days,
            "name":                r.name,
            "exchange":            r.exchange,
            "sector":              r.sector,
            "qualified_date":      as_of_date,
        }

    payload = {
        "as_of_date":      as_of_date,
        "adx_threshold":   adx_threshold,
        "count":           len(qualified_results),
        "generated_at":    datetime.now().isoformat(),
        "symbols":         symbols_dict
    }

    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(payload, f, indent=2)

    logger.info(f"✓ Saved qualified_trends.json  |  {len(qualified_results):,} symbols  →  {output_file}")


def save_qualification_report(
    all_results: List[TrendCheckResult],
    output_file: Path
):
    """
    Save a CSV report covering every symbol — both passing and failing.

    This is the full audit trail. Every symbol that was evaluated appears
    here with its exact values and the specific reason it passed or failed.

    Columns:
        symbol, qualified, failure_reason,
        rule_golden_cross, rule_price_above, rule_adx_strength,
        sma_50, sma_200, sma_spread_pct, close, price_vs_sma50_pct,
        adx_14, atr_20_pct, trend_days,
        name, exchange, sector, as_of_date, error

    Args:
        all_results:  Every TrendCheckResult (pass + fail + error)
        output_file:  Target path
    """
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)

    rows = []
    for r in all_results:
        rows.append({
            'symbol':               r.symbol,
            'qualified':            r.qualified,
            'failure_reason':       r.failure_reason,
            'rule_golden_cross':    r.rule_golden_cross,
            'rule_price_above':     r.rule_price_above,
            'rule_adx_strength':    r.rule_adx_strength,
            'sma_50':               r.sma_50,
            'sma_200':              r.sma_200,
            'sma_spread_pct':       r.sma_spread_pct,
            'close':                r.close,
            'price_vs_sma50_pct':   r.price_vs_sma50_pct,
            'adx_14':               r.adx_14,
            'atr_20_pct':           r.atr_20_pct,
            'trend_days':           r.trend_days,
            'name':                 r.name,
            'exchange':             r.exchange,
            'sector':               r.sector,
            'as_of_date':           r.as_of_date,
            'error':                r.error,
        })

    df = pd.DataFrame(rows)

    # Sort: qualified first, then by symbol name
    df = df.sort_values(['qualified', 'symbol'], ascending=[False, True])

    df.to_csv(output_file, index=False)
    logger.info(f"✓ Saved trend_qualification_report.csv  |  {len(rows):,} rows  →  {output_file}")


def save_qualification_summary(
    qualified_results: List[TrendCheckResult],
    all_results: List[TrendCheckResult],
    as_of_date: str,
    adx_threshold: float,
    output_file: Path
):
    """
    Save aggregate statistics to JSON and print a human-readable table to the log.

    Includes:
        - Overall pass rate
        - Breakdown by failure rule (how many failed each specific condition)
        - Qualified breakdown by exchange
        - Qualified breakdown by sector
        - Trend maturity distribution (how long trends have been running)
        - ADX distribution statistics for qualified instruments

    Args:
        qualified_results: Passing instruments
        all_results:       All instruments
        as_of_date:        Evaluation date
        adx_threshold:     ADX threshold applied
        output_file:       Target path
    """
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)

    total = len(all_results)
    qualified = len(qualified_results)

    # ----------------------------------------------------------------
    # Failure analysis — first rule that failed for each instrument
    # ----------------------------------------------------------------
    fail_golden_cross  = sum(1 for r in all_results if not r.error and not r.qualified and r.rule_golden_cross is False)
    fail_price_above   = sum(1 for r in all_results if not r.error and not r.qualified and r.rule_golden_cross and r.rule_price_above is False)
    fail_adx_strength  = sum(1 for r in all_results if not r.error and not r.qualified and r.rule_golden_cross and r.rule_price_above and r.rule_adx_strength is False)
    skipped_or_error   = sum(1 for r in all_results if bool(r.error))

    # ----------------------------------------------------------------
    # Qualified breakdown by exchange
    # ----------------------------------------------------------------
    exchange_counts: Dict[str, int] = {}
    for r in qualified_results:
        k = r.exchange or 'Unknown'
        exchange_counts[k] = exchange_counts.get(k, 0) + 1

    # ----------------------------------------------------------------
    # Qualified breakdown by sector
    # ----------------------------------------------------------------
    sector_counts: Dict[str, int] = {}
    for r in qualified_results:
        k = r.sector or 'Unknown'
        sector_counts[k] = sector_counts.get(k, 0) + 1

    # ----------------------------------------------------------------
    # Trend maturity buckets (only qualified)
    # ----------------------------------------------------------------
    trend_days_vals = [r.trend_days for r in qualified_results if r.trend_days is not None]
    maturity_buckets = {
        "1_to_5_days":   sum(1 for d in trend_days_vals if 1 <= d <= 5),
        "6_to_20_days":  sum(1 for d in trend_days_vals if 6 <= d <= 20),
        "21_to_63_days": sum(1 for d in trend_days_vals if 21 <= d <= 63),
        "64_to_126_days":sum(1 for d in trend_days_vals if 64 <= d <= 126),
        "over_126_days": sum(1 for d in trend_days_vals if d > 126),
    }

    # ----------------------------------------------------------------
    # ADX distribution for qualified instruments
    # ----------------------------------------------------------------
    adx_vals = [r.adx_14 for r in qualified_results if r.adx_14 is not None]
    adx_stats = {}
    if adx_vals:
        adx_arr = np.array(adx_vals)
        adx_stats = {
            "min":    round(float(adx_arr.min()), 1),
            "p25":    round(float(np.percentile(adx_arr, 25)), 1),
            "median": round(float(np.median(adx_arr)), 1),
            "p75":    round(float(np.percentile(adx_arr, 75)), 1),
            "max":    round(float(adx_arr.max()), 1),
        }

    # ----------------------------------------------------------------
    # SMA spread distribution for qualified instruments
    # ----------------------------------------------------------------
    spread_vals = [r.sma_spread_pct for r in qualified_results if r.sma_spread_pct is not None]
    spread_stats = {}
    if spread_vals:
        sp_arr = np.array(spread_vals)
        spread_stats = {
            "min":    round(float(sp_arr.min()), 2),
            "median": round(float(np.median(sp_arr)), 2),
            "max":    round(float(sp_arr.max()), 2),
        }

    summary = {
        "as_of_date":             as_of_date,
        "adx_threshold":          adx_threshold,
        "total_symbols_evaluated":total,
        "qualified_count":        qualified,
        "qualification_rate_pct": round(qualified / total * 100, 2) if total > 0 else 0.0,
        "failure_breakdown": {
            "failed_rule1_golden_cross":  fail_golden_cross,
            "failed_rule2_price_above":   fail_price_above,
            "failed_rule3_adx_strength":  fail_adx_strength,
            "skipped_or_error":           skipped_or_error,
        },
        "qualified_by_exchange":  dict(sorted(exchange_counts.items(), key=lambda x: x[1], reverse=True)),
        "qualified_by_sector":    dict(sorted(sector_counts.items(),   key=lambda x: x[1], reverse=True)),
        "trend_maturity_buckets": maturity_buckets,
        "adx_distribution":       adx_stats,
        "sma_spread_distribution":spread_stats,
        "generated_at":           datetime.now().isoformat(),
    }

    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(summary, f, indent=2)

    logger.info(f"✓ Saved trend_qualification_summary.json  →  {output_file}")

    # ----------------------------------------------------------------
    # Human-readable console summary
    # ----------------------------------------------------------------
    logger.info(f"\n{'='*70}")
    logger.info("TREND QUALIFICATION SUMMARY")
    logger.info(f"{'='*70}")
    logger.info(f"  Date:                {as_of_date}")
    logger.info(f"  ADX threshold:       > {adx_threshold:.0f}")
    logger.info(f"  Total evaluated:     {total:,}")
    logger.info(f"  Qualified:           {qualified:,}  ({summary['qualification_rate_pct']:.1f}%)")

    logger.info(f"\n  Failure Breakdown:")
    logger.info(f"    Rule 1 — Golden cross failed:  {fail_golden_cross:5,}  (SMA_50 ≤ SMA_200)")
    logger.info(f"    Rule 2 — Price above failed:   {fail_price_above:5,}  (Close ≤ SMA_50)")
    logger.info(f"    Rule 3 — ADX strength failed:  {fail_adx_strength:5,}  (ADX ≤ {adx_threshold:.0f})")
    logger.info(f"    Skipped / errors:              {skipped_or_error:5,}")

    if exchange_counts:
        logger.info(f"\n  Qualified by Exchange:")
        for exch, cnt in sorted(exchange_counts.items(), key=lambda x: x[1], reverse=True):
            bar = '█' * min(cnt // 5, 40)
            logger.info(f"    {exch:10s}  {cnt:5,}  {bar}")

    if sector_counts:
        logger.info(f"\n  Top Sectors (qualified):")
        for sec, cnt in sorted(sector_counts.items(), key=lambda x: x[1], reverse=True)[:12]:
            logger.info(f"    {sec:30s}  {cnt:5,}")

    if maturity_buckets:
        logger.info(f"\n  Trend Maturity Distribution (qualified):")
        for bucket, cnt in maturity_buckets.items():
            logger.info(f"    {bucket:20s}  {cnt:5,}")

    if adx_stats:
        logger.info(f"\n  ADX Distribution (qualified):")
        logger.info(f"    Min={adx_stats['min']}  P25={adx_stats['p25']}  "
                    f"Median={adx_stats['median']}  P75={adx_stats['p75']}  Max={adx_stats['max']}")

    logger.info(f"{'='*70}")


# ============================================================================
# ARGUMENT PARSING
# ============================================================================

def parse_arguments() -> argparse.Namespace:
    """Parse and validate command-line arguments."""
    parser = argparse.ArgumentParser(
        description='Trend Qualifier — Script 6 (Architecture v3.2)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Standard monthly rebalancing run
  python scripts/06_qualify_trends.py --as-of-date 2026-01-31

  # Use current date
  python scripts/06_qualify_trends.py --as-of-date $(date +%Y-%m-%d)

  # Override ADX threshold (stricter)
  python scripts/06_qualify_trends.py --as-of-date 2026-01-31 --min-adx 25

  # Test with limited symbols
  python scripts/06_qualify_trends.py --as-of-date 2026-01-31 --max-symbols 100

Pipeline (run in order):
  1. 01_download_eodhd_bulk.py   --mode incremental
  2. 02_download_yahoo_fundamentals.py
  3. 03_consolidate_validate_data.py --mode full
  4. 04_screen_universe.py       --as-of-date YYYY-MM-DD
  5. 05_calculate_indicators.py  --as-of-date YYYY-MM-DD
  6. 06_qualify_trends.py        --as-of-date YYYY-MM-DD  ← THIS SCRIPT
  7. 07_rank_momentum.py         --as-of-date YYYY-MM-DD
        """
    )

    parser.add_argument(
        '--as-of-date',
        required=True,
        metavar='YYYY-MM-DD',
        help='Evaluation date. Use last trading day of month for rebalancing.'
    )

    parser.add_argument(
        '--min-adx',
        type=float,
        default=None,
        metavar='N',
        help=(
            'Override minimum ADX threshold (default: from strategy_parameters.json '
            f'or built-in default of {DEFAULT_ADX_THRESHOLD}). '
            'Higher values = stricter trend filter.'
        )
    )

    parser.add_argument(
        '--max-symbols',
        type=int,
        default=None,
        metavar='N',
        help='Limit number of symbols processed (for testing only).'
    )

    return parser.parse_args()


# ============================================================================
# MAIN
# ============================================================================

def main() -> int:
    """Main execution function. Returns 0 on success, 1 on fatal error."""
    start_time = datetime.now()

    logger.info("=" * 70)
    logger.info("TREND QUALIFIER — Script 6")
    logger.info("Architecture v3.2 (Feb 2026)")
    logger.info("=" * 70)

    # ----------------------------------------------------------------
    # 1. Parse arguments
    # ----------------------------------------------------------------
    args = parse_arguments()
    as_of_date = args.as_of_date

    # Validate date format
    try:
        datetime.strptime(as_of_date, '%Y-%m-%d')
    except ValueError:
        logger.error(f"Invalid date format: '{as_of_date}'  —  expected YYYY-MM-DD")
        return 1

    logger.info(f"\nEvaluation date: {as_of_date}")

    # ----------------------------------------------------------------
    # 2. Load configuration
    # ----------------------------------------------------------------
    try:
        strategy_params = load_strategy_parameters()
    except Exception as e:
        logger.error(f"Failed to load strategy parameters: {e}")
        return 1

    # Determine ADX threshold (CLI overrides config)
    config_adx = (
        strategy_params
        .get("entry", {})
        .get("trend_qualification", {})
        .get("adx_minimum", DEFAULT_ADX_THRESHOLD)
    )
    adx_threshold = float(args.min_adx) if args.min_adx is not None else float(config_adx)

    logger.info(f"ADX threshold: {adx_threshold} (source: {'CLI' if args.min_adx else 'config'})")

    # ----------------------------------------------------------------
    # 3. Load qualified symbol list from Script 4
    # ----------------------------------------------------------------
    try:
        symbols = load_qualified_symbols()
    except SystemExit:
        raise
    except Exception as e:
        logger.error(f"Failed to load qualified symbols: {e}")
        return 1

    if len(symbols) == 0:
        logger.error("Qualified symbol list is empty — nothing to evaluate.")
        return 1

    # ----------------------------------------------------------------
    # 4. Run trend qualification
    # ----------------------------------------------------------------
    try:
        qualified_results, all_results = qualify_all_trends(
            symbols=symbols,
            as_of_date=as_of_date,
            adx_threshold=adx_threshold,
            max_symbols=args.max_symbols
        )
    except Exception as e:
        logger.error(f"Fatal error during trend qualification: {e}", exc_info=True)
        return 1

    if len(qualified_results) == 0:
        logger.warning(
            "⚠  No instruments qualified. Check your as-of-date, "
            "indicators data, or consider lowering --min-adx."
        )
        # Still write outputs (empty) so downstream scripts fail gracefully

    # ----------------------------------------------------------------
    # 5. Save outputs
    # ----------------------------------------------------------------
    try:
        save_qualified_trends(
            qualified_results=qualified_results,
            as_of_date=as_of_date,
            adx_threshold=adx_threshold,
            output_file=SIGNALS_DIR / 'qualified_trends.json'
        )

        save_qualification_report(
            all_results=all_results,
            output_file=SIGNALS_DIR / 'trend_qualification_report.csv'
        )

        save_qualification_summary(
            qualified_results=qualified_results,
            all_results=all_results,
            as_of_date=as_of_date,
            adx_threshold=adx_threshold,
            output_file=SIGNALS_DIR / 'trend_qualification_summary.json'
        )

    except Exception as e:
        logger.error(f"Error saving outputs: {e}", exc_info=True)
        return 1

    # ----------------------------------------------------------------
    # 6. Final log
    # ----------------------------------------------------------------
    duration = datetime.now() - start_time

    logger.info(f"\n{'='*70}")
    logger.info("TREND QUALIFICATION COMPLETE")
    logger.info(f"{'='*70}")
    logger.info(f"  Duration:         {duration}")
    logger.info(f"  Symbols evaluated:{len(all_results):>8,}")
    logger.info(f"  Qualified:        {len(qualified_results):>8,}")
    logger.info(f"  Outputs:")
    logger.info(f"    → {SIGNALS_DIR / 'qualified_trends.json'}")
    logger.info(f"    → {SIGNALS_DIR / 'trend_qualification_report.csv'}")
    logger.info(f"    → {SIGNALS_DIR / 'trend_qualification_summary.json'}")
    logger.info(f"\n  Next step: python scripts/07_rank_momentum.py --as-of-date {as_of_date}")
    logger.info("=" * 70)

    return 0


# ============================================================================
# ENTRY POINT
# ============================================================================

if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        logger.warning("\n\nInterrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Unhandled fatal error: {e}", exc_info=True)
        sys.exit(1)
