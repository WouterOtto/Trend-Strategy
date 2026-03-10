#!/usr/bin/env python3
"""
Script 3: Data Consolidator & Validator
========================================
Production-grade data consolidation and quality validation

Purpose:
    Merge EODHD bulk data + Yahoo fundamentals into unified per-symbol time series
    Apply comprehensive data quality validation with explicit thresholds
    Flag and report data quality issues

Inputs:
    - data_cache/raw_bulk/{exchange}/{date}.parquet  (EODHD OHLCV)
    - data_cache/fundamentals/company_info.json      (Yahoo metadata)
    - data_cache/corporate_actions/*.parquet         (splits, dividends)

Outputs:
    - data_cache/consolidated/{symbol}.parquet       (per-symbol unified time series)
    - data_cache/metadata/data_quality_report.json   (validation summary)
    - data_cache/metadata/validation_failures.csv    (detailed failures)
    - data_cache/metadata/consolidation_state.json   (incremental optimisation cache)

Execution Modes:
    # FULL - Process only new symbols (skips existing), with validation
    python scripts/03_consolidate_validate_data.py --mode full

    # INCREMENTAL - Smart delta update (appends new dates), with validation
    python scripts/03_consolidate_validate_data.py --mode incremental

    # CONSOLIDATE-ONLY - Fast processing WITHOUT validation
    # Type: full (default) - process new symbols only
    python scripts/03_consolidate_validate_data.py --mode consolidate-only
    python scripts/03_consolidate_validate_data.py --mode consolidate-only --type full

    # Type: incremental - append new dates to existing symbols
    python scripts/03_consolidate_validate_data.py --mode consolidate-only --type incremental

    # FULL --FORCE - Rebuild everything from scratch, with validation
    python scripts/03_consolidate_validate_data.py --mode full --force

    # VALIDATE-ONLY - Check existing files, no processing
    python scripts/03_consolidate_validate_data.py --mode validate-only

    # Filters (work with any mode)
    python scripts/03_consolidate_validate_data.py --mode incremental --exchange NYSE
    python scripts/03_consolidate_validate_data.py --mode consolidate-only --type incremental --symbols AAPL.US,MSFT.US

Design Decisions:
    - adjusted_close is taken directly from EODHD (not recalculated)
    - No FX conversion: prices kept in native currency
    - Symbols with any stock split history are EXCLUDED from the universe
      Rationale: splits introduce complexity in trend signals; cleaner to
      work with a split-free universe and rely on EODHD adjusted_close only
      for symbols without corporate action history.

Performance Optimisations (v3.3):
    OPT-1  Date-major pivot for full build mode
           Reads all date files ONCE per exchange then groupby(symbol).
           O(N_dates) reads vs old O(N_symbols × N_dates).
           Estimated speedup: 50-200x on initial full build.

    OPT-2  ProcessPoolExecutor for validate-only
           Validation is CPU-bound and embarrassingly parallel.
           Scales linearly with available cores.
           Estimated speedup: 4-12x on validate-only.

    OPT-3  PyArrow column pruning on all parquet reads
           Reads only the 8 required OHLCV columns; skips metadata columns.
           Reduces I/O by ~30-40% on every file read.

    OPT-4  Module-level trading calendar cache (lru_cache)
           _get_expected_trading_days() computed once per unique date range,
           not once per symbol. Eliminates 23,000 redundant calendar iterations.

    OPT-5  PyArrow write_table instead of pandas to_parquet
           Leaner write path; skips column statistics; overlaps I/O with CPU
           via a dedicated writer thread in full-build mode.

    OPT-6  O(1) split exclusion set
           Pre-builds a frozenset of split symbol codes once per exchange.
           Replaces O(N_splits) DataFrame filter in the per-symbol hot path.

    OPT-7  Per-symbol last-date in consolidation state cache
           Incremental runs skip symbols with no new data in O(1),
           with zero filesystem I/O per skipped symbol.

Architecture: v3.3 (Mar 2026)
"""

import gc
import os
import sys
import json
import logging
import argparse
import warnings
import threading
import queue
import multiprocessing
from concurrent.futures import ProcessPoolExecutor, as_completed
from functools import lru_cache
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Set, FrozenSet
from dataclasses import dataclass
from collections import defaultdict

import pandas as pd
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from tqdm import tqdm

# Suppress pandas warnings for cleaner output
warnings.filterwarnings('ignore', category=pd.errors.PerformanceWarning)

# ============================================================================
# CONFIGURATION
# ============================================================================

# Project paths: script lives in scripts/, data lives in project root
PROJECT_ROOT          = Path(__file__).parent.parent
CONFIG_DIR            = PROJECT_ROOT / "config"
DATA_CACHE_DIR        = PROJECT_ROOT / "data_cache"
RAW_BULK_DIR          = DATA_CACHE_DIR / "raw_bulk"
FUNDAMENTALS_DIR      = DATA_CACHE_DIR / "fundamentals"
CORPORATE_ACTIONS_DIR = DATA_CACHE_DIR / "corporate_actions"
CONSOLIDATED_DIR      = DATA_CACHE_DIR / "consolidated"
METADATA_DIR          = DATA_CACHE_DIR / "metadata"
LOG_DIR               = PROJECT_ROOT / "logs"

# Metadata cache for incremental optimisation
CONSOLIDATION_STATE_FILE = METADATA_DIR / "consolidation_state.json"

# Validation thresholds
VALIDATION_THRESHOLDS = {
    'PRICE_JUMP_THRESHOLD':     0.25,   # 25% single-day jump flags a warning
    'MAX_MISSING_DATA_PCT':     0.05,   # Allow up to 5% missing trading days
    'ATR_EXPLOSION_MULTIPLIER': 2.0,    # ATR > 2x median signals a data error
    'MAX_DATA_STALENESS_DAYS':  3,      # Warn if last bar is > 3 trading days old
    'MIN_HISTORY_DAYS':         252,    # Require at least 1 year of history
    'MAX_VOLUME_SPIKE':         100.0,  # 100x average volume = data error
    'MAX_ZERO_VOLUME_PCT':      0.01,   # Allow up to 1% zero-volume days
}

# Bulk processing optimisation threshold
# Use optimised bulk incremental processing when processing this many symbols or more
BULK_OPTIMIZATION_THRESHOLD = 100

# OPT-3: Columns to read from raw bulk parquet files (column pruning)
RAW_BULK_COLUMNS = ['code', 'date', 'open', 'high', 'low', 'close', 'volume', 'adjusted_close']

# OPT-1: Max date files to load into memory at once (tune for available RAM)
# 60 files × ~50 MB each ≈ 3 GB peak per exchange chunk — safe for 16 GB+ machines
DATE_FILE_CHUNK_SIZE = 60

# OPT-2: Worker process count for parallel validation (validate-only mode)
MAX_WORKER_PROCESSES = min(multiprocessing.cpu_count() - 1, 8)


# ============================================================================
# LOGGING SETUP
# ============================================================================

def setup_logging() -> logging.Logger:
    """Configure logging with both file and console output."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_file  = LOG_DIR / f'consolidate_validate_{timestamp}.log'

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=[
            logging.FileHandler(log_file, encoding='utf-8'),
            logging.StreamHandler(sys.stdout)
        ]
    )

    logger = logging.getLogger(__name__)
    logger.info(f"Log file      : {log_file}")
    logger.info(f"Project root  : {PROJECT_ROOT}")
    logger.info(f"Data cache dir: {DATA_CACHE_DIR}")
    logger.info(f"Raw bulk dir  : {RAW_BULK_DIR}")

    return logger

logger = setup_logging()


# ============================================================================
# OPT-4: MODULE-LEVEL TRADING CALENDAR CACHE
# ============================================================================

@lru_cache(maxsize=512)
def _cached_trading_days(exchange: str, start_str: str, end_str: str) -> FrozenSet:
    """
    Return frozenset of date objects for expected trading days.

    OPT-4: Cached per (exchange, start, end) tuple — computed once per unique
    date range across the entire run, not once per symbol.

    Falls back to Mon-Fri business days if pandas_market_calendars is not
    installed. Install it for exchange-specific holiday awareness:
        pip install pandas-market-calendars
    """
    try:
        import pandas_market_calendars as mcal
        _MCAL_NAMES = {
            'NYSE':   'NYSE',
            'NASDAQ': 'NASDAQ',
            'LSE':    'LSE',
            'XETRA':  'XETR',
            'PA':     'Euronext',
            'AS':     'Euronext',
        }
        cal_name = _MCAL_NAMES.get(exchange, 'NYSE')
        cal      = mcal.get_calendar(cal_name)
        schedule = cal.schedule(start_date=start_str, end_date=end_str)
        idx      = mcal.date_range(schedule, frequency='1D')
        return frozenset(d.date() for d in idx)
    except ImportError:
        # Fallback: Mon-Fri business days (no holiday awareness)
        idx = pd.bdate_range(start=start_str, end=end_str)
        return frozenset(d.date() for d in idx)


# ============================================================================
# DATA VALIDATION CLASSES
# ============================================================================

@dataclass
class ValidationResult:
    """Result of a single validation check."""
    passed:    bool
    message:   str
    severity:  str            = 'ERROR'   # ERROR | WARNING | INFO
    value:     Optional[float] = None
    threshold: Optional[float] = None


class DataValidator:
    """
    Comprehensive data quality validation with explicit thresholds.

    Checks:
      1. Completeness   - missing trading days, NaN values
      2. Price sanity   - OHLC relationship violations, unexplained price jumps
      3. Volume sanity  - negative, zero, and spike detection
      4. ATR explosion  - volatility anomaly detection
      5. Data freshness - staleness relative to today
    """

    def __init__(self, thresholds: Dict = None):
        self.thresholds = thresholds or VALIDATION_THRESHOLDS

    # ------------------------------------------------------------------
    # 1. Completeness
    # ------------------------------------------------------------------

    def validate_completeness(self, df: pd.DataFrame, exchange: str) -> ValidationResult:
        """
        Rules:
          - Missing trading days < 5% of expected (Mon-Fri, excl. holidays)
          - No NaN values in open, high, low, close, volume
        """
        # OPT-4: use cached calendar — no per-symbol iteration
        start_str    = df.index.min().strftime('%Y-%m-%d')
        end_str      = df.index.max().strftime('%Y-%m-%d')
        expected_days = _cached_trading_days(exchange, start_str, end_str)

        actual_days  = set(df.index.date)
        missing_pct  = (
            len(expected_days - actual_days) / len(expected_days)
            if expected_days else 0
        )

        if missing_pct > self.thresholds['MAX_MISSING_DATA_PCT']:
            return ValidationResult(
                passed=False,
                message=(
                    f"Missing {missing_pct:.1%} of trading days "
                    f"(threshold: {self.thresholds['MAX_MISSING_DATA_PCT']:.1%})"
                ),
                severity='ERROR',
                value=missing_pct,
                threshold=self.thresholds['MAX_MISSING_DATA_PCT']
            )

        critical_cols = ['open', 'high', 'low', 'close', 'volume']
        nan_counts    = df[critical_cols].isna().sum()
        if nan_counts.any():
            nan_col = nan_counts[nan_counts > 0].index[0]
            return ValidationResult(
                passed=False,
                message=f"NaN values in '{nan_col}': {nan_counts[nan_col]} rows",
                severity='ERROR'
            )

        return ValidationResult(
            passed=True,
            message=f"Completeness OK (missing: {missing_pct:.1%})",
            severity='INFO',
            value=missing_pct
        )

    # ------------------------------------------------------------------
    # 2. Price sanity
    # ------------------------------------------------------------------

    def validate_price_consistency(self, df: pd.DataFrame) -> ValidationResult:
        """
        Rules:
          - high >= low, open, close  (always)
          - low  <= open, close       (always)
          - Single-day returns < 25%
            (splits are excluded from the universe, so all jumps are suspect)
        """
        violations = (
            (df['high'] < df['low'])   |
            (df['high'] < df['open'])  |
            (df['high'] < df['close']) |
            (df['low']  > df['open'])  |
            (df['low']  > df['close'])
        )

        if violations.any():
            example_date = df[violations].index[0]
            return ValidationResult(
                passed=False,
                message=f"OHLC violations: {violations.sum()} days (first: {example_date.date()})",
                severity='ERROR',
                value=int(violations.sum())
            )

        # All large moves are unexplained (no splits in universe)
        returns = df['close'].pct_change()
        extreme = returns[abs(returns) > self.thresholds['PRICE_JUMP_THRESHOLD']]

        if len(extreme) > 0:
            max_jump = abs(extreme).max()
            return ValidationResult(
                passed=False,
                message=(
                    f"Price jumps >25%: {len(extreme)} days "
                    f"(max: {max_jump:.1%})"
                ),
                severity='WARNING',
                value=max_jump,
                threshold=self.thresholds['PRICE_JUMP_THRESHOLD']
            )

        return ValidationResult(
            passed=True,
            message="Price consistency OK",
            severity='INFO'
        )

    # ------------------------------------------------------------------
    # 3. Volume sanity
    # ------------------------------------------------------------------

    def validate_volume(self, df: pd.DataFrame) -> ValidationResult:
        """
        Rules:
          - No negative volume
          - Zero-volume days < 1%
          - No spikes > 100x rolling 30-day average
        """
        if (df['volume'] < 0).any():
            n = int((df['volume'] < 0).sum())
            return ValidationResult(
                passed=False,
                message=f"Negative volume: {n} days",
                severity='ERROR',
                value=n
            )

        zero_pct = (df['volume'] == 0).sum() / len(df)
        if zero_pct > self.thresholds['MAX_ZERO_VOLUME_PCT']:
            return ValidationResult(
                passed=False,
                message=(
                    f"Zero-volume days: {zero_pct:.1%} "
                    f"(threshold: {self.thresholds['MAX_ZERO_VOLUME_PCT']:.1%})"
                ),
                severity='WARNING',
                value=zero_pct,
                threshold=self.thresholds['MAX_ZERO_VOLUME_PCT']
            )

        if len(df) >= 30:
            avg_vol     = df['volume'].rolling(30, min_periods=20).mean()
            spike_ratio = df['volume'] / avg_vol
            spikes      = spike_ratio > self.thresholds['MAX_VOLUME_SPIKE']
            if spikes.any():
                max_spike = spike_ratio.max()
                return ValidationResult(
                    passed=False,
                    message=(
                        f"Volume spike >{self.thresholds['MAX_VOLUME_SPIKE']:.0f}x avg: "
                        f"{int(spikes.sum())} days (max: {max_spike:.1f}x)"
                    ),
                    severity='WARNING',
                    value=max_spike,
                    threshold=self.thresholds['MAX_VOLUME_SPIKE']
                )

        return ValidationResult(
            passed=True,
            message="Volume sanity OK",
            severity='INFO'
        )

    # ------------------------------------------------------------------
    # 4. ATR explosion
    # ------------------------------------------------------------------

    def validate_atr(self, df: pd.DataFrame) -> ValidationResult:
        """
        Rule:
          - ATR_20 must not exceed 2x its own 200-day rolling median.
        Requires at least 220 rows.
        """
        if len(df) < 220:
            return ValidationResult(
                passed=True,
                message="Insufficient data for ATR validation (need 220 days)",
                severity='INFO'
            )

        prev_close = df['close'].shift(1)
        tr = pd.concat([
            df['high'] - df['low'],
            (df['high'] - prev_close).abs(),
            (df['low']  - prev_close).abs()
        ], axis=1).max(axis=1)

        atr_20     = tr.rolling(20,  min_periods=15).mean()
        median_atr = atr_20.rolling(200, min_periods=150).median()
        ratio      = atr_20 / median_atr
        explosions = ratio > self.thresholds['ATR_EXPLOSION_MULTIPLIER']

        if explosions.any():
            max_ratio = ratio.max()
            return ValidationResult(
                passed=False,
                message=(
                    f"ATR explosion >{self.thresholds['ATR_EXPLOSION_MULTIPLIER']:.1f}x median: "
                    f"{int(explosions.sum())} days (max: {max_ratio:.1f}x)"
                ),
                severity='WARNING',
                value=max_ratio,
                threshold=self.thresholds['ATR_EXPLOSION_MULTIPLIER']
            )

        return ValidationResult(
            passed=True,
            message="ATR sanity OK",
            severity='INFO'
        )

    # ------------------------------------------------------------------
    # 5. Data freshness
    # ------------------------------------------------------------------

    def validate_data_freshness(self, df: pd.DataFrame, exchange: str) -> ValidationResult:
        """
        Rule:
          - Last bar must be within 3 trading days of today.
        """
        last_date = df.index.max().date()
        today     = datetime.now().date()
        stale     = self._count_trading_days(last_date, today, exchange)

        if stale > self.thresholds['MAX_DATA_STALENESS_DAYS']:
            return ValidationResult(
                passed=False,
                message=(
                    f"Data stale: {stale} trading days old "
                    f"(threshold: {self.thresholds['MAX_DATA_STALENESS_DAYS']})"
                ),
                severity='WARNING',
                value=stale,
                threshold=self.thresholds['MAX_DATA_STALENESS_DAYS']
            )

        return ValidationResult(
            passed=True,
            message=f"Data freshness OK (last: {last_date}, age: {stale} trading days)",
            severity='INFO',
            value=stale
        )

    # ------------------------------------------------------------------
    # Master check
    # ------------------------------------------------------------------

    def validate_all(
        self,
        df: pd.DataFrame,
        exchange: str
    ) -> Tuple[bool, List[ValidationResult]]:
        """
        Run all checks. Returns (all_errors_passed, results_list).
        WARNING-level failures do not block the symbol.
        """
        checks = [
            self.validate_completeness(df, exchange),
            self.validate_price_consistency(df),
            self.validate_volume(df),
            self.validate_atr(df),
            self.validate_data_freshness(df, exchange),
        ]

        all_passed = all(
            c.passed or c.severity != 'ERROR'
            for c in checks
        )

        return all_passed, checks

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_expected_trading_days(
        self,
        start_date: pd.Timestamp,
        end_date: pd.Timestamp,
        exchange: str
    ) -> FrozenSet:
        """OPT-4: Delegate to module-level cached function."""
        return _cached_trading_days(
            exchange,
            start_date.strftime('%Y-%m-%d'),
            end_date.strftime('%Y-%m-%d')
        )

    def _count_trading_days(self, start_date, end_date, exchange: str) -> int:
        """Count weekday days between two dates."""
        if start_date >= end_date:
            return 0
        return len(
            _cached_trading_days(
                exchange,
                start_date.strftime('%Y-%m-%d'),
                end_date.strftime('%Y-%m-%d')
            )
        )


# ============================================================================
# CONFIG / METADATA LOADING
# ============================================================================

def load_exchange_config() -> Dict:
    """Load exchange definitions from config/exchanges.json, or use defaults."""
    config_file = CONFIG_DIR / 'exchanges.json'

    if not config_file.exists():
        logger.warning("Exchange config not found, using defaults")
        return {
            "NYSE":   {"currency": "USD", "country": "US"},
            "NASDAQ": {"currency": "USD", "country": "US"},
            "XETRA":  {"currency": "EUR", "country": "DE"},
            "LSE":    {"currency": "GBP", "country": "GB"},
            "PA":     {"currency": "EUR", "country": "FR"},
            "AS":     {"currency": "EUR", "country": "NL"},
        }

    with open(config_file, 'r') as f:
        config = json.load(f)

    return config.get('exchanges', {})


def load_company_info() -> Dict:
    """Load Yahoo Finance company metadata."""
    company_file = FUNDAMENTALS_DIR / 'company_info.json'

    if not company_file.exists():
        logger.warning(f"Company info not found at {company_file}")
        return {}

    with open(company_file, 'r') as f:
        data = json.load(f)

    logger.info(f"Loaded company info for {len(data)} symbols")
    return data


def load_corporate_actions(exchange: str) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Load splits and dividends parquet files for an exchange.

    Returns:
        (splits_df, dividends_df) - empty DataFrames if files not found
    """
    splits    = pd.DataFrame()
    dividends = pd.DataFrame()

    splits_file    = CORPORATE_ACTIONS_DIR / f"{exchange}_splits.parquet"
    dividends_file = CORPORATE_ACTIONS_DIR / f"{exchange}_dividends.parquet"

    if splits_file.exists():
        try:
            splits = pd.read_parquet(splits_file)
            logger.debug(f"  {exchange}: loaded {len(splits)} splits")
        except Exception as e:
            logger.warning(f"  {exchange}: error loading splits - {e}")

    if dividends_file.exists():
        try:
            dividends = pd.read_parquet(dividends_file)
            logger.debug(f"  {exchange}: loaded {len(dividends)} dividends")
        except Exception as e:
            logger.warning(f"  {exchange}: error loading dividends - {e}")

    return splits, dividends


# ============================================================================
# OPT-6: SPLIT EXCLUSION SET
# ============================================================================

def build_split_exclusion_set(splits: pd.DataFrame) -> frozenset:
    """
    OPT-6: Pre-build a frozenset of split symbol codes (base code only).

    Replaces the O(N_splits) DataFrame filter in has_splits() with an O(1)
    hash lookup. Build once per exchange before the main processing loop.
    """
    if splits is None or len(splits) == 0:
        return frozenset()
    return frozenset(splits['code'].str.upper().unique())


def symbol_has_split(symbol: str, split_set: frozenset) -> bool:
    """O(1) split exclusion check using pre-built set."""
    return symbol.split('.')[0].upper() in split_set


# Kept for backward compatibility with legacy per-symbol path
def has_splits(splits: pd.DataFrame, symbol: str) -> bool:
    """
    Return True if the symbol has ANY split record.
    Legacy O(N) path — used only when split_set is not available.
    """
    if splits is None or len(splits) == 0:
        return False
    symbol_base = symbol.split('.')[0]
    return len(splits[splits['code'] == symbol_base]) > 0


# ============================================================================
# METADATA CACHE MANAGEMENT
# ============================================================================

def load_consolidation_state() -> Dict:
    """
    Load metadata cache tracking last consolidated date per exchange
    and per-symbol last dates (OPT-7).

    Format:
    {
        "NYSE": {
            "last_consolidated_date": "2024-01-15",
            "last_updated": "2024-01-16T10:30:00",
            "total_symbols": 2891,
            "symbol_dates": {          # OPT-7: per-symbol last bar date
                "AAPL": "2024-01-15",
                "MSFT": "2024-01-15"
            }
        }
    }

    Returns:
        Empty dict if cache doesn't exist (first run)
    """
    if not CONSOLIDATION_STATE_FILE.exists():
        return {}

    try:
        with open(CONSOLIDATION_STATE_FILE, 'r') as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"Error loading consolidation state cache: {e}")
        return {}


def save_consolidation_state(state: Dict):
    """
    Save metadata cache atomically.
    Uses atomic write (write to temp, then rename) to prevent corruption.
    """
    METADATA_DIR.mkdir(parents=True, exist_ok=True)

    temp_file = CONSOLIDATION_STATE_FILE.with_suffix('.tmp')

    try:
        with open(temp_file, 'w') as f:
            json.dump(state, f, indent=2)
        temp_file.replace(CONSOLIDATION_STATE_FILE)
    except Exception as e:
        logger.error(f"Error saving consolidation state: {e}")
        if temp_file.exists():
            temp_file.unlink()


def update_consolidation_state(
    exchange: str,
    last_date: str,
    symbol_count: int,
    symbol_dates: Optional[Dict[str, str]] = None,
    state: Optional[Dict] = None
):
    """
    Update metadata cache for one exchange.

    OPT-7: Accepts an optional symbol_dates dict {symbol_base: last_date}
    to enable per-symbol staleness checks in the incremental path.

    Accepts an optional pre-loaded state dict to avoid redundant file reads
    when updating multiple exchanges in sequence.
    """
    if state is None:
        state = load_consolidation_state()

    entry = {
        'last_consolidated_date': last_date,
        'last_updated':           datetime.now().isoformat(),
        'total_symbols':          symbol_count,
    }

    if symbol_dates is not None:
        # Merge with any existing per-symbol dates (preserve old entries)
        existing_sym_dates = state.get(exchange, {}).get('symbol_dates', {})
        existing_sym_dates.update(symbol_dates)
        entry['symbol_dates'] = existing_sym_dates

    state[exchange] = entry
    save_consolidation_state(state)


def write_last_update(exchange: str, last_date: str) -> None:
    """
    Write (or advance) the last consolidated date for *exchange* into
    data_cache/metadata/last_update.json.

    PURPOSE
    -------
    Keeps last_update.json in sync with the on-disk consolidated parquet
    files so that the Script 11 data-staleness circuit breaker always
    reflects the true state of data rather than relying solely on Script 01
    download metadata, which can diverge if a run was partial or the file
    was seeded from the wrong (start) date.

    SAFETY
    ------
    - Never goes backwards: if the file already contains a newer date for
      this exchange the call is a no-op. This guards against partial runs
      overwriting a good date with an older one.
    - Atomic write (temp-file + os.replace) prevents JSON corruption.
    - Thread-safe for single-process runs; not designed for concurrent use.

    Args:
        exchange:  Exchange code, e.g. "NYSE".
        last_date: Most recent consolidated bar date, "YYYY-MM-DD".
    """
    METADATA_DIR.mkdir(parents=True, exist_ok=True)
    last_update_file = METADATA_DIR / "last_update.json"
    temp_file        = last_update_file.with_suffix(".tmp")

    # Load existing entries — preserve all other exchanges already present
    existing: Dict = {}
    if last_update_file.exists():
        try:
            with open(last_update_file, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                f"write_last_update: could not read last_update.json "
                f"(will overwrite): {exc}"
            )

    current = existing.get(exchange, "1900-01-01")

    if last_date <= current:
        logger.debug(
            f"write_last_update: {exchange} already at {current} "
            f"(candidate {last_date} is not newer — skipped)"
        )
        return

    existing[exchange] = last_date
    try:
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2)
        temp_file.replace(last_update_file)
        logger.debug(
            f"write_last_update: {exchange} → {last_date} (was {current})"
        )
    except OSError as exc:
        logger.error(f"write_last_update: failed to write last_update.json: {exc}")
        if temp_file.exists():
            temp_file.unlink(missing_ok=True)


def get_last_consolidated_date(exchange: str, symbol_to_exchange: Dict[str, str]) -> str:
    """
    Get the latest consolidated date for an exchange.

    Strategy:
      1. Try metadata cache first (fast)
      2. If cache miss or first run: scan consolidated files (slow but accurate)
      3. Update cache with result

    Returns:
        Date string "YYYY-MM-DD" or "1900-01-01" if no consolidated files exist
    """
    state = load_consolidation_state()
    if exchange in state:
        cached_date = state[exchange]['last_consolidated_date']
        logger.debug(f"  {exchange}: using cached date {cached_date}")
        return cached_date

    # Cache miss — scan consolidated files
    logger.info(f"  {exchange}: no cache, scanning consolidated files...")
    max_date     = "1900-01-01"
    symbol_count = 0
    symbol_dates: Dict[str, str] = {}

    for symbol, exch in symbol_to_exchange.items():
        if exch != exchange:
            continue

        file = CONSOLIDATED_DIR / f"{symbol}.parquet"
        if file.exists():
            try:
                # OPT-3: read only the index (no columns needed)
                df   = pq.read_table(str(file), columns=[]).to_pandas()
                date = df.index.max().strftime('%Y-%m-%d')
                max_date = max(max_date, date)
                symbol_count += 1
                symbol_dates[symbol.split('.')[0]] = date   # OPT-7
            except Exception:
                continue

    if symbol_count > 0:
        update_consolidation_state(
            exchange, max_date, symbol_count, symbol_dates=symbol_dates
        )
        logger.info(f"  {exchange}: scanned {symbol_count} files, last date: {max_date}")

    return max_date


def get_new_dates_for_exchange(
    exchange: str,
    symbol_to_exchange: Dict[str, str]
) -> List[str]:
    """
    Find which dates exist in raw_bulk but not yet consolidated.

    Returns:
        Sorted list of new dates ["2024-01-16", "2024-01-17", ...]
    """
    raw_bulk_dir = RAW_BULK_DIR / exchange

    if not raw_bulk_dir.exists():
        return []

    raw_dates = sorted([f.stem for f in raw_bulk_dir.glob('*.parquet')])
    if not raw_dates:
        return []

    last_consolidated = get_last_consolidated_date(exchange, symbol_to_exchange)
    return [d for d in raw_dates if d > last_consolidated]


# ============================================================================
# RAW DATA LOADING
# ============================================================================

def get_all_symbols_from_bulk(exchanges: List[str] = None) -> Dict[str, str]:
    """
    Discover all symbols present in the most recent raw_bulk snapshot.

    Returns:
        {full_symbol: exchange_code}  e.g. {"AAPL.NASDAQ": "NASDAQ", ...}
    """
    if not RAW_BULK_DIR.exists():
        logger.error(f"Raw bulk directory not found: {RAW_BULK_DIR}")
        logger.error("Run Script 1 (01_download_eodhd_bulk.py) first.")
        return {}

    if exchanges is None:
        exchanges = [d.name for d in RAW_BULK_DIR.iterdir() if d.is_dir()]

    if not exchanges:
        logger.error(f"No exchange directories found in {RAW_BULK_DIR}")
        return {}

    symbol_to_exchange: Dict[str, str] = {}
    logger.info("Discovering symbols from bulk cache...")

    for exchange in exchanges:
        exchange_dir  = RAW_BULK_DIR / exchange

        if not exchange_dir.exists():
            logger.warning(f"  {exchange}: directory not found")
            continue

        parquet_files = sorted(exchange_dir.glob('*.parquet'))
        if not parquet_files:
            logger.warning(f"  {exchange}: no parquet files found")
            continue

        try:
            # OPT-3: read only the symbol column for universe discovery
            df         = pq.read_table(str(parquet_files[-1]), columns=['code']).to_pandas()
            symbol_col = 'code'
            symbols    = df[symbol_col].unique()

            for sym in symbols:
                full_symbol = sym if '.' in sym else f"{sym}.{exchange}"
                symbol_to_exchange[full_symbol] = exchange

            logger.info(f"  {exchange}: {len(symbols)} symbols")

        except Exception as e:
            logger.error(f"  {exchange}: error reading latest file - {e}")
            continue

    if not symbol_to_exchange:
        logger.error("No symbols found. Run Script 1 first.")
    else:
        logger.info(
            f"Found {len(symbol_to_exchange)} symbols across "
            f"{len(set(symbol_to_exchange.values()))} exchanges"
        )

    return symbol_to_exchange


def load_raw_bulk_data(
    symbol: str,
    exchange: str,
    start_date: Optional[str] = None
) -> pd.DataFrame:
    """
    Load OHLCV data for one symbol from the raw_bulk parquet files.

    OPT-3: Uses PyArrow with predicate pushdown and column pruning.

    Args:
        symbol:     Full symbol string, e.g. "AAPL.NASDAQ"
        exchange:   Exchange code, e.g. "NASDAQ"
        start_date: Optional ISO date string "YYYY-MM-DD"; only load
                    files on or after this date (used for incremental mode)

    Returns:
        DataFrame indexed by date, columns include open/high/low/close/
        volume/adjusted_close (as supplied by EODHD)
    """
    exchange_dir  = RAW_BULK_DIR / exchange

    if not exchange_dir.exists():
        raise FileNotFoundError(f"No data cache for exchange: {exchange}")

    parquet_files = sorted(exchange_dir.glob('*.parquet'))
    if not parquet_files:
        raise FileNotFoundError(f"No parquet files for exchange: {exchange}")

    if start_date:
        start_dt      = datetime.strptime(start_date, '%Y-%m-%d').date()
        parquet_files = [
            f for f in parquet_files
            if datetime.strptime(f.stem, '%Y-%m-%d').date() >= start_dt
        ]

    symbol_base = symbol.split('.')[0]
    frames: List[pd.DataFrame] = []

    for pf in parquet_files:
        try:
            # OPT-3: predicate pushdown + column pruning
            available_cols = pq.read_schema(str(pf)).names
            read_cols      = [c for c in RAW_BULK_COLUMNS if c in available_cols]
            table          = pq.read_table(
                str(pf),
                columns=read_cols,
                filters=[('code', '==', symbol_base)]
            )
            if table.num_rows > 0:
                frames.append(table.to_pandas())
        except Exception as e:
            logger.warning(f"  Error reading {pf.name}: {e}")

    if not frames:
        raise ValueError(f"No data found for {symbol} in {exchange}")

    combined = pd.concat(frames, ignore_index=True)
    combined['date'] = pd.to_datetime(combined['date'])
    combined = (
        combined
        .sort_values('date')
        .drop_duplicates(subset=['date'], keep='last')
        .set_index('date')
    )

    return combined


def get_last_date_in_raw_bulk(symbol: str, exchange: str) -> Optional[str]:
    """
    Scan the most recent raw_bulk files (up to 30) to find the latest
    date for which data exists for a given symbol.

    Returns:
        "YYYY-MM-DD" string, or None if not found
    """
    exchange_dir = RAW_BULK_DIR / exchange
    if not exchange_dir.exists():
        return None

    parquet_files = sorted(exchange_dir.glob('*.parquet'))
    if not parquet_files:
        return None

    symbol_base = symbol.split('.')[0]

    for pf in reversed(parquet_files[-30:]):
        try:
            # OPT-3: read only code column to check symbol presence
            available_cols = pq.read_schema(str(pf)).names
            if 'code' not in available_cols:
                continue
            table = pq.read_table(
                str(pf),
                columns=['code'],
                filters=[('code', '==', symbol_base)]
            )
            if table.num_rows > 0:
                return pf.stem   # filename is the date
        except Exception:
            continue

    return None


# ============================================================================
# METADATA HELPER
# ============================================================================

def add_metadata_columns(
    df: pd.DataFrame,
    symbol: str,
    exchange: str,
    currency: str,
    company_info: Dict
) -> pd.DataFrame:
    """
    Attach static metadata columns to a price DataFrame.
    Prices remain in native currency — no FX conversion applied.
    """
    info = company_info.get(symbol, {})

    df['symbol']     = symbol
    df['exchange']   = exchange
    df['currency']   = currency
    df['name']       = info.get('name', '')
    df['sector']     = info.get('sector', '')
    df['industry']   = info.get('industry', '')
    df['market_cap'] = info.get('market_cap', 0)

    return df


# ============================================================================
# OPT-5: PYARROW WRITER THREAD
# ============================================================================

_WRITE_SENTINEL = None   # signals writer thread to stop


def _writer_thread_fn(write_queue: queue.Queue):
    """
    OPT-5: Dedicated I/O thread that drains a write queue.

    Overlaps CPU work (validation in main thread / pool) with disk writes.
    Each item is (output_path_str, pyarrow_table).
    """
    while True:
        item = write_queue.get()
        if item is _WRITE_SENTINEL:
            write_queue.task_done()
            break
        path, table = item
        try:
            pq.write_table(
                table,
                path,
                compression='snappy',
                write_statistics=False,
            )
        except Exception as e:
            logger.error(f"Writer thread error writing {path}: {e}")
        finally:
            write_queue.task_done()


def make_writer_thread() -> Tuple[queue.Queue, threading.Thread]:
    """Spawn the background writer thread and return (queue, thread)."""
    wq = queue.Queue(maxsize=500)
    wt = threading.Thread(target=_writer_thread_fn, args=(wq,), daemon=True)
    wt.start()
    return wq, wt


def enqueue_write(wq: queue.Queue, output_file: Path, df: pd.DataFrame):
    """Convert df to Arrow table and push to the write queue (non-blocking)."""
    try:
        table = pa.Table.from_pandas(df, preserve_index=True)
        wq.put((str(output_file), table))
    except Exception as e:
        # Fallback: write synchronously if conversion fails
        logger.warning(f"Arrow conversion failed for {output_file.name}, writing sync: {e}")
        df.to_parquet(output_file, compression='snappy')


def flush_writer(wq: queue.Queue, wt: threading.Thread):
    """Wait for all queued writes to complete, then stop the writer thread."""
    wq.put(_WRITE_SENTINEL)
    wq.join()
    wt.join()


# ============================================================================
# OPT-1: DATE-MAJOR FULL BUILD
# ============================================================================

def _prepare_symbol_df(
    symbol_base: str,
    grp: pd.DataFrame,
    exchange: str,
    split_set: frozenset,
    company_info: Dict,
    exchange_config: Dict,
    validator: DataValidator,
    skip_validation: bool,
    force: bool,
) -> Optional[Tuple[str, pd.DataFrame, Dict]]:
    """
    Prepare one symbol's DataFrame from an already-grouped slice.
    Called from build_exchange_date_major() for each symbol group.

    Returns:
        (symbol, prepared_df, stats_dict) or None if the symbol should be skipped.
    """
    symbol      = f"{symbol_base}.{exchange}"
    output_file = CONSOLIDATED_DIR / f"{symbol}.parquet"

    stats = {
        'symbol':            symbol,
        'exchange':          exchange,
        'status':            'unknown',
        'records':           0,
        'new_records':       0,
        'validation_passed': False,
        'validation_issues': []
    }

    # Skip if already consolidated and not force
    if output_file.exists() and not force:
        stats['status'] = 'skipped'
        return symbol, None, stats

    # OPT-6: O(1) split exclusion
    if symbol_has_split(symbol, split_set):
        stats['status'] = 'excluded_split'
        return symbol, None, stats

    # Clean and sort
    grp = (
        grp
        .sort_values('date')
        .drop_duplicates('date', keep='last')
        .set_index('date')
    )

    if len(grp) < VALIDATION_THRESHOLDS['MIN_HISTORY_DAYS']:
        stats['status'] = 'insufficient_history'
        stats['records'] = len(grp)
        return symbol, None, stats

    if 'adjusted_close' not in grp.columns:
        grp['adjusted_close'] = grp['close']

    currency = exchange_config.get(exchange, {}).get('currency', 'USD')
    grp      = add_metadata_columns(grp, symbol, exchange, currency, company_info)

    if skip_validation:
        grp['data_quality']      = True
        grp['validation_issues'] = ''
        stats['records']           = len(grp)
        stats['validation_passed'] = True
        stats['status']            = 'success'
    else:
        all_passed, results = validator.validate_all(grp, exchange)
        issues = [r.message for r in results if not r.passed]
        grp['data_quality']      = all_passed
        grp['validation_issues'] = ', '.join(issues)
        stats['records']           = len(grp)
        stats['validation_passed'] = all_passed
        stats['validation_issues'] = issues
        stats['status']            = 'success'

    return symbol, grp, stats


def build_exchange_date_major(
    exchange: str,
    split_set: frozenset,
    company_info: Dict,
    exchange_config: Dict,
    validator: DataValidator,
    skip_validation: bool,
    force: bool,
) -> List[Dict]:
    """
    OPT-1: Date-major full build for one exchange.

    Reads all date files ONCE (one pass), then groupby(symbol) and writes.
    O(N_date_files) reads vs old O(N_symbols × N_date_files).

    To keep peak memory bounded, processes date files in chunks of
    DATE_FILE_CHUNK_SIZE, accumulating per-symbol DataFrames across chunks.

    Returns:
        List of stats dicts, one per symbol.
    """
    exchange_dir  = RAW_BULK_DIR / exchange
    date_files    = sorted(exchange_dir.glob('*.parquet'))

    if not date_files:
        logger.warning(f"  {exchange}: no date files found")
        return []

    logger.info(
        f"  {exchange}: date-major build — "
        f"{len(date_files)} date files, chunk size {DATE_FILE_CHUNK_SIZE}"
    )

    CONSOLIDATED_DIR.mkdir(parents=True, exist_ok=True)

    # Accumulate per-symbol data across chunks: {symbol_base: [df, df, ...]}
    symbol_chunks: Dict[str, List[pd.DataFrame]] = defaultdict(list)

    # OPT-3: determine available columns from the first file
    try:
        schema        = pq.read_schema(str(date_files[0]))
        available     = set(schema.names)
        read_cols     = [c for c in RAW_BULK_COLUMNS if c in available]
    except Exception:
        read_cols = RAW_BULK_COLUMNS

    # ── Phase 1: Read all date files in chunks ──────────────────────
    n_chunks = (len(date_files) + DATE_FILE_CHUNK_SIZE - 1) // DATE_FILE_CHUNK_SIZE

    for chunk_idx in tqdm(
        range(n_chunks),
        desc=f"  {exchange} reading",
        unit="chunk",
        leave=False
    ):
        chunk = date_files[chunk_idx * DATE_FILE_CHUNK_SIZE:
                           (chunk_idx + 1) * DATE_FILE_CHUNK_SIZE]
        frames = []
        for f in chunk:
            try:
                # OPT-3: column pruning only (no per-file predicate — we need all symbols)
                table = pq.read_table(str(f), columns=read_cols)
                frames.append(table.to_pandas())
            except Exception as e:
                logger.warning(f"  {exchange}: error reading {f.name}: {e}")

        if not frames:
            continue

        batch = pd.concat(frames, ignore_index=True)
        batch['date'] = pd.to_datetime(batch['date'])

        for sym_base, grp in batch.groupby('code', sort=False):
            symbol_chunks[sym_base].append(grp)

        del frames, batch
        gc.collect()

    # ── Phase 2: Assemble, validate, write ──────────────────────────
    all_stats: List[Dict] = []

    # OPT-5: background writer thread
    wq, wt = make_writer_thread()

    # OPT-7: collect per-symbol dates for state cache update
    symbol_dates: Dict[str, str] = {}

    for sym_base, parts in tqdm(
        symbol_chunks.items(),
        desc=f"  {exchange} writing",
        unit="sym",
        leave=False
    ):
        full_grp = pd.concat(parts, ignore_index=True)

        symbol, df, stats = _prepare_symbol_df(
            symbol_base=sym_base,
            grp=full_grp,
            exchange=exchange,
            split_set=split_set,
            company_info=company_info,
            exchange_config=exchange_config,
            validator=validator,
            skip_validation=skip_validation,
            force=force,
        )

        if df is not None:
            output_file = CONSOLIDATED_DIR / f"{symbol}.parquet"
            enqueue_write(wq, output_file, df)           # OPT-5
            symbol_dates[sym_base] = df.index.max().strftime('%Y-%m-%d')  # OPT-7

            if not stats['validation_passed']:
                for r in (validator.validate_all(df, exchange)[1]
                          if not skip_validation else []):
                    if not r.passed:
                        logger.warning(f"  {symbol}: [{r.severity}] {r.message}")

        all_stats.append(stats)
        del full_grp

    flush_writer(wq, wt)
    gc.collect()

    return all_stats, symbol_dates


# ============================================================================
# CONSOLIDATION FUNCTIONS (LEGACY PER-SYMBOL PATH)
# ============================================================================

def consolidate_symbol(
    symbol: str,
    exchange: str,
    company_info: Dict,
    splits: pd.DataFrame,
    dividends: pd.DataFrame,
    validator: DataValidator,
    exchange_config: Dict,
    force: bool = False,
    skip_validation: bool = False,
    split_set: Optional[frozenset] = None,
) -> Tuple[Optional[pd.DataFrame], Dict]:
    """
    Full consolidation for a single symbol (used by --mode full legacy path
    and as fallback for new symbols in incremental mode).

    Steps:
      1. Skip if already consolidated (unless force=True)
      2. Exclude if symbol has split history
      3. Load all OHLCV data from raw_bulk
      4. Check minimum history requirement
      5. Use EODHD-provided adjusted_close directly (no recalculation)
      6. Attach metadata columns (no FX conversion)
      7. Validate data quality (unless skip_validation=True)
      8. Save to consolidated/

    Args:
        split_set:        Pre-built frozenset for O(1) lookup (OPT-6).
                          Falls back to has_splits() if not provided.
        skip_validation:  If True, skip all validation checks (faster)

    Returns:
        (df_or_None, stats_dict)
    """
    stats = {
        'symbol':            symbol,
        'exchange':          exchange,
        'status':            'unknown',
        'records':           0,
        'new_records':       0,
        'validation_passed': False,
        'validation_issues': []
    }

    try:
        output_file = CONSOLIDATED_DIR / f"{symbol}.parquet"

        # 1. Skip if already done
        if output_file.exists() and not force:
            stats['status'] = 'skipped'
            logger.debug(f"  {symbol}: already consolidated (--force to reprocess)")
            return None, stats

        # 2. Exclude symbols with splits (OPT-6)
        if split_set is not None:
            excluded = symbol_has_split(symbol, split_set)
        else:
            excluded = has_splits(splits, symbol)

        if excluded:
            stats['status'] = 'excluded_split'
            logger.debug(f"  {symbol}: excluded (has splits)")
            return None, stats

        # 3. Load raw data
        df = load_raw_bulk_data(symbol, exchange)

        # 4. Minimum history check
        if len(df) < VALIDATION_THRESHOLDS['MIN_HISTORY_DAYS']:
            stats['status'] = 'insufficient_history'
            stats['records'] = len(df)
            logger.debug(
                f"  {symbol}: insufficient history "
                f"({len(df)} < {VALIDATION_THRESHOLDS['MIN_HISTORY_DAYS']} days)"
            )
            return None, stats

        # 5. Use EODHD adjusted_close directly (no recalculation)
        if 'adjusted_close' not in df.columns:
            logger.warning(f"  {symbol}: no adjusted_close column, falling back to close")
            df['adjusted_close'] = df['close']

        # 6. Attach metadata (prices in native currency, no FX conversion)
        currency = exchange_config.get(exchange, {}).get('currency', 'USD')
        df = add_metadata_columns(df, symbol, exchange, currency, company_info)

        # 7. Validate (unless skip_validation=True)
        if skip_validation:
            df['data_quality']      = True
            df['validation_issues'] = ''
            stats['records']           = len(df)
            stats['validation_passed'] = True
            stats['validation_issues'] = []
        else:
            all_passed, validation_results = validator.validate_all(df, exchange)
            issues = [r.message for r in validation_results if not r.passed]
            df['data_quality']      = all_passed
            df['validation_issues'] = ', '.join(issues)
            stats['records']           = len(df)
            stats['validation_passed'] = all_passed
            stats['validation_issues'] = issues

            if not all_passed:
                for r in validation_results:
                    if not r.passed:
                        logger.warning(f"  {symbol}: [{r.severity}] {r.message}")

        # 8. Save (OPT-5: PyArrow write)
        CONSOLIDATED_DIR.mkdir(parents=True, exist_ok=True)
        pq.write_table(
            pa.Table.from_pandas(df, preserve_index=True),
            str(output_file),
            compression='snappy',
            write_statistics=False,
        )

        stats['status'] = 'success'
        logger.debug(f"  {symbol}: consolidated {len(df)} records")

        return df, stats

    except (FileNotFoundError, ValueError) as e:
        stats['status'] = 'no_data'
        logger.debug(f"  {symbol}: no data - {e}")
        return None, stats

    except Exception as e:
        stats['status'] = 'error'
        stats['error_message'] = str(e)
        logger.error(f"  {symbol}: error - {e}")
        return None, stats


# ============================================================================
# OPTIMISED INCREMENTAL PROCESSING (BY DATE, NOT BY SYMBOL)
# ============================================================================

def process_incremental_bulk_optimized(
    exchange: str,
    symbol_to_exchange: Dict[str, str],
    company_info: Dict,
    splits: pd.DataFrame,
    dividends: pd.DataFrame,
    validator: DataValidator,
    exchange_config: Dict,
    skip_validation: bool,
    split_set: Optional[frozenset] = None,
) -> List[Dict]:
    """
    Optimised incremental processing: process by date, not by symbol.

    Strategy:
      1. Find which dates in raw_bulk/ are newer than consolidated/
      2. For each new date, load the bulk file (contains all symbols)
      3. Process only symbols that traded on those dates
      4. Append efficiently to existing consolidated files

    OPT-7: Uses per-symbol state cache to skip symbols already up-to-date
    in O(1), without reading any filesystem.

    Returns:
        List of stats dicts (one per symbol processed)
    """
    if split_set is None:
        split_set = build_split_exclusion_set(splits)

    all_stats: List[Dict] = []

    # OPT-7: load per-symbol dates from state cache
    state               = load_consolidation_state()
    cached_sym_dates    = state.get(exchange, {}).get('symbol_dates', {})

    new_dates = get_new_dates_for_exchange(exchange, symbol_to_exchange)

    if not new_dates:
        logger.debug(f"  {exchange}: no new dates to process")
        return []

    logger.info(f"  {exchange}: found {len(new_dates)} new dates: {new_dates[0]} -> {new_dates[-1]}")

    # Track per-symbol updates for state cache
    updated_symbol_dates: Dict[str, str] = {}

    for new_date in tqdm(new_dates, desc=f"  {exchange}", unit="date", leave=False):
        date_file = RAW_BULK_DIR / exchange / f"{new_date}.parquet"

        try:
            # OPT-3: column pruning on date file read
            available_cols = pq.read_schema(str(date_file)).names
            read_cols      = [c for c in RAW_BULK_COLUMNS if c in available_cols]
            df_date        = pq.read_table(str(date_file), columns=read_cols).to_pandas()
            symbol_col     = 'code' if 'code' in df_date.columns else 'symbol'
            symbols_that_traded = df_date[symbol_col].unique()

            for symbol_base in symbols_that_traded:
                # OPT-7: skip if already up-to-date per cached state
                if cached_sym_dates.get(symbol_base, '1900-01-01') >= new_date:
                    continue

                # OPT-6: O(1) split exclusion
                if symbol_base.upper() in split_set:
                    continue

                symbol = f"{symbol_base}.{exchange}"
                stats  = append_date_to_consolidated(
                    symbol=symbol,
                    symbol_base=symbol_base,
                    exchange=exchange,
                    new_date=new_date,
                    df_date=df_date,
                    company_info=company_info,
                    splits=splits,
                    validator=validator,
                    exchange_config=exchange_config,
                    skip_validation=skip_validation,
                )

                if stats:
                    all_stats.append(stats)
                    if stats.get('status') == 'updated':
                        updated_symbol_dates[symbol_base] = new_date

        except Exception as e:
            logger.error(f"  {exchange}/{new_date}: error loading date file - {e}")
            continue

    # OPT-7: persist updated per-symbol dates back to state cache
    if new_dates:
        symbols_processed = len(set(
            s['symbol'] for s in all_stats
            if s['status'] in ('success', 'updated')
        ))
        cached_sym_dates.update(updated_symbol_dates)
        update_consolidation_state(
            exchange, new_dates[-1], symbols_processed,
            symbol_dates=cached_sym_dates, state=state
        )
        # Fix-3: keep last_update.json in sync with consolidated truth
        write_last_update(exchange, new_dates[-1])

    return all_stats


def append_date_to_consolidated(
    symbol: str,
    symbol_base: str,
    exchange: str,
    new_date: str,
    df_date: pd.DataFrame,
    company_info: Dict,
    splits: pd.DataFrame,
    validator: DataValidator,
    exchange_config: Dict,
    skip_validation: bool
) -> Optional[Dict]:
    """
    Append one date's worth of data to a consolidated file.

    Called from process_incremental_bulk_optimized for each symbol
    that traded on a new date.

    Returns:
        Stats dict or None if skipped
    """
    stats = {
        'symbol':            symbol,
        'exchange':          exchange,
        'status':            'unknown',
        'records':           0,
        'new_records':       0,
        'validation_passed': False,
        'validation_issues': []
    }

    try:
        output_file = CONSOLIDATED_DIR / f"{symbol}.parquet"

        # Extract this symbol's row from the date file
        symbol_col = 'code' if 'code' in df_date.columns else 'symbol'
        new_row    = df_date[df_date[symbol_col] == symbol_base].copy()

        if len(new_row) == 0:
            return None

        new_row['date'] = pd.to_datetime(new_row['date'])
        new_row = new_row.set_index('date')

        if 'adjusted_close' not in new_row.columns:
            new_row['adjusted_close'] = new_row['close']

        currency = exchange_config.get(exchange, {}).get('currency', 'USD')
        new_row  = add_metadata_columns(new_row, symbol, exchange, currency, company_info)

        if not output_file.exists():
            stats['status'] = 'needs_full_build'
            return stats

        # OPT-3: read existing consolidated file
        existing_df = pq.read_table(str(output_file)).to_pandas()
        existing_df.index = pd.to_datetime(existing_df.index)

        # Idempotency check
        if new_date in existing_df.index.strftime('%Y-%m-%d').values:
            stats['status'] = 'already_exists'
            return None

        # Append
        combined_df = (
            pd.concat([existing_df, new_row])
            .sort_index()
            .loc[lambda d: ~d.index.duplicated(keep='last')]
        )

        if skip_validation:
            combined_df['data_quality']      = True
            combined_df['validation_issues'] = ''
            stats['validation_passed'] = True
        else:
            all_passed, validation_results = validator.validate_all(combined_df, exchange)
            issues = [r.message for r in validation_results if not r.passed]
            combined_df['data_quality']      = all_passed
            combined_df['validation_issues'] = ', '.join(issues)
            stats['validation_passed'] = all_passed
            stats['validation_issues'] = issues

        # OPT-5: PyArrow write
        pq.write_table(
            pa.Table.from_pandas(combined_df, preserve_index=True),
            str(output_file),
            compression='snappy',
            write_statistics=False,
        )

        stats['status']      = 'updated'
        stats['records']     = len(combined_df)
        stats['new_records'] = 1

        return stats

    except Exception as e:
        stats['status']        = 'error'
        stats['error_message'] = str(e)
        logger.debug(f"  {symbol}: error appending date - {e}")
        return stats


# ============================================================================
# INCREMENTAL PER-SYMBOL (LEGACY PATH — used when --symbols filter active)
# ============================================================================

def update_symbol_incremental(
    symbol: str,
    exchange: str,
    company_info: Dict,
    splits: pd.DataFrame,
    dividends: pd.DataFrame,
    validator: DataValidator,
    exchange_config: Dict,
    skip_validation: bool = False,
    split_set: Optional[frozenset] = None,
) -> Tuple[Optional[pd.DataFrame], Dict]:
    """
    Incremental update for a single symbol (used by --mode incremental
    legacy path when --symbols filter restricts to < BULK_OPTIMIZATION_THRESHOLD).

    Logic:
      - No consolidated file  -> full consolidation (new symbol)
      - Consolidated exists:
          a. Exclude if symbol now has splits (delete existing file)
          b. Compare last consolidated date vs. last raw_bulk date
          c. If raw_bulk is newer -> append new rows only
          d. Re-validate entire combined dataset (unless skip_validation=True)
          e. Save updated file
    """
    if split_set is None:
        split_set = build_split_exclusion_set(splits)

    stats = {
        'symbol':            symbol,
        'exchange':          exchange,
        'status':            'unknown',
        'records':           0,
        'new_records':       0,
        'validation_passed': False,
        'validation_issues': []
    }

    try:
        output_file = CONSOLIDATED_DIR / f"{symbol}.parquet"

        if not output_file.exists():
            logger.debug(f"  {symbol}: not yet consolidated, running full build")
            return consolidate_symbol(
                symbol, exchange, company_info, splits, dividends,
                validator, exchange_config, force=False,
                skip_validation=skip_validation, split_set=split_set
            )

        # OPT-6: O(1) split check
        if symbol_has_split(symbol, split_set):
            output_file.unlink(missing_ok=True)
            stats['status'] = 'excluded_split'
            logger.warning(f"  {symbol}: excluded and removed (split detected)")
            return None, stats

        # OPT-3: read existing consolidated
        existing_df            = pq.read_table(str(output_file)).to_pandas()
        existing_df.index      = pd.to_datetime(existing_df.index)
        last_date_consolidated = existing_df.index.max().strftime('%Y-%m-%d')

        last_date_raw = get_last_date_in_raw_bulk(symbol, exchange)

        if last_date_raw is None:
            stats['status'] = 'no_new_data'
            stats['records'] = len(existing_df)
            logger.debug(f"  {symbol}: no data in raw_bulk")
            return None, stats

        if last_date_raw <= last_date_consolidated:
            stats['status'] = 'up_to_date'
            stats['records'] = len(existing_df)
            logger.debug(f"  {symbol}: up to date (last: {last_date_consolidated})")
            return None, stats

        next_date = (
            datetime.strptime(last_date_consolidated, '%Y-%m-%d') + timedelta(days=1)
        ).strftime('%Y-%m-%d')

        logger.debug(f"  {symbol}: appending {next_date} -> {last_date_raw}")
        new_df = load_raw_bulk_data(symbol, exchange, start_date=next_date)

        if len(new_df) == 0:
            stats['status'] = 'no_new_data'
            stats['records'] = len(existing_df)
            logger.debug(f"  {symbol}: no new trading days found")
            return None, stats

        if 'adjusted_close' not in new_df.columns:
            logger.warning(f"  {symbol}: no adjusted_close in new data, falling back to close")
            new_df['adjusted_close'] = new_df['close']

        currency = exchange_config.get(exchange, {}).get('currency', 'USD')
        new_df   = add_metadata_columns(new_df, symbol, exchange, currency, company_info)

        combined_df = (
            pd.concat([existing_df, new_df])
            .sort_index()
            .loc[lambda d: ~d.index.duplicated(keep='last')]
        )

        if skip_validation:
            combined_df['data_quality']      = True
            combined_df['validation_issues'] = ''
            stats['status']            = 'updated'
            stats['records']           = len(combined_df)
            stats['new_records']       = len(new_df)
            stats['validation_passed'] = True
            stats['validation_issues'] = []
        else:
            all_passed, validation_results = validator.validate_all(combined_df, exchange)
            issues = [r.message for r in validation_results if not r.passed]
            combined_df['data_quality']      = all_passed
            combined_df['validation_issues'] = ', '.join(issues)
            stats['status']            = 'updated'
            stats['records']           = len(combined_df)
            stats['new_records']       = len(new_df)
            stats['validation_passed'] = all_passed
            stats['validation_issues'] = issues

            if not all_passed:
                for r in validation_results:
                    if not r.passed:
                        logger.warning(f"  {symbol}: [{r.severity}] {r.message}")

        # OPT-5: PyArrow write
        pq.write_table(
            pa.Table.from_pandas(combined_df, preserve_index=True),
            str(output_file),
            compression='snappy',
            write_statistics=False,
        )
        logger.debug(
            f"  {symbol}: updated (+{len(new_df)} rows, total: {len(combined_df)})"
        )

        return combined_df, stats

    except (FileNotFoundError, ValueError) as e:
        stats['status'] = 'no_data'
        logger.debug(f"  {symbol}: no data - {e}")
        return None, stats

    except Exception as e:
        stats['status'] = 'error'
        stats['error_message'] = str(e)
        logger.error(f"  {symbol}: error - {e}")
        return None, stats


# ============================================================================
# OPT-2: PARALLEL VALIDATE-ONLY
# ============================================================================

def _validate_symbol_worker(args: Tuple) -> Dict:
    """
    OPT-2: Worker function for ProcessPoolExecutor in validate-only mode.

    Instantiates DataValidator locally (not picklable across processes if
    created in parent) and reads/validates one consolidated file.

    Args:
        args: (symbol, exchange, file_path_str)

    Returns:
        stats dict
    """
    symbol, exchange, file_path_str = args

    stats = {
        'symbol':            symbol,
        'exchange':          exchange,
        'status':            'unknown',
        'records':           0,
        'new_records':       0,
        'validation_passed': False,
        'validation_issues': []
    }

    consolidated_file = Path(file_path_str)

    if not consolidated_file.exists():
        stats['status']            = 'not_consolidated'
        stats['validation_issues'] = ['File does not exist']
        return stats

    try:
        # OPT-3: PyArrow read
        df = pq.read_table(str(consolidated_file)).to_pandas()
        df.index = pd.to_datetime(df.index)

        # Local validator instantiation (required for subprocess safety)
        validator = DataValidator()
        ok, results = validator.validate_all(df, exchange)
        issues = [r.message for r in results if not r.passed]

        stats['status']            = 'validated'
        stats['records']           = len(df)
        stats['validation_passed'] = ok
        stats['validation_issues'] = issues

    except Exception as e:
        stats['status']            = 'error'
        stats['validation_issues'] = [str(e)]

    return stats


def run_validate_only_parallel(
    symbol_to_exchange: Dict[str, str],
    n_workers: int = MAX_WORKER_PROCESSES,
) -> List[Dict]:
    """
    OPT-2: Parallelised validate-only using ProcessPoolExecutor.

    Each worker reads and validates one consolidated file independently.
    Scales linearly with available CPU cores.
    """
    work_items = [
        (symbol, exchange, str(CONSOLIDATED_DIR / f"{symbol}.parquet"))
        for symbol, exchange in symbol_to_exchange.items()
    ]

    logger.info(
        f"Validate-only: {len(work_items)} symbols, "
        f"{n_workers} parallel workers"
    )

    all_stats: List[Dict] = []

    with ProcessPoolExecutor(max_workers=n_workers) as pool:
        futures = {
            pool.submit(_validate_symbol_worker, item): item[0]
            for item in work_items
        }
        with tqdm(total=len(futures), desc="Validating", unit="sym") as pbar:
            for future in as_completed(futures):
                pbar.set_description(futures[future])
                all_stats.append(future.result())
                pbar.update(1)

    return all_stats


# ============================================================================
# REPORTING
# ============================================================================

def generate_quality_report(all_stats: List[Dict]) -> Dict:
    """Aggregate per-symbol stats into a summary quality report."""
    report = {
        'generated_at':  datetime.now().isoformat(),
        'total_symbols': len(all_stats),
        'by_status':     defaultdict(int),
        'by_exchange':   defaultdict(lambda: {
            'total': 0, 'success': 0, 'updated': 0,
            'excluded_split': 0, 'failed': 0, 'validation_passed': 0
        }),
        'validation_summary': {
            'total_symbols': 0,
            'passed':        0,
            'failed':        0,
            'pass_rate':     0.0
        },
        'common_issues': defaultdict(int),
        'incremental_stats': {
            'symbols_updated':    0,
            'symbols_up_to_date': 0,
            'total_new_records':  0
        }
    }

    for s in all_stats:
        status   = s['status']
        exchange = s['exchange']

        report['by_status'][status] += 1
        report['by_exchange'][exchange]['total'] += 1

        if status in ('success', 'updated', 'validated'):
            report['by_exchange'][exchange][status] = (
                report['by_exchange'][exchange].get(status, 0) + 1
            )

            if s['validation_passed']:
                report['by_exchange'][exchange]['validation_passed'] += 1
            else:
                report['by_exchange'][exchange]['failed'] += 1

            report['validation_summary']['total_symbols'] += 1
            if s['validation_passed']:
                report['validation_summary']['passed'] += 1
            else:
                report['validation_summary']['failed'] += 1
                for issue in s['validation_issues']:
                    report['common_issues'][issue] += 1

        elif status == 'excluded_split':
            report['by_exchange'][exchange]['excluded_split'] += 1

        else:
            report['by_exchange'][exchange]['failed'] += 1

        if status == 'updated':
            report['incremental_stats']['symbols_updated']   += 1
            report['incremental_stats']['total_new_records'] += s.get('new_records', 0)
        elif status == 'up_to_date':
            report['incremental_stats']['symbols_up_to_date'] += 1

    if report['validation_summary']['total_symbols'] > 0:
        report['validation_summary']['pass_rate'] = (
            report['validation_summary']['passed'] /
            report['validation_summary']['total_symbols']
        )

    report['by_status']     = dict(report['by_status'])
    report['by_exchange']   = dict(report['by_exchange'])
    report['common_issues'] = dict(
        sorted(report['common_issues'].items(), key=lambda x: x[1], reverse=True)
    )

    return report


def save_quality_report(report: Dict):
    """Write quality report to JSON."""
    METADATA_DIR.mkdir(parents=True, exist_ok=True)
    report_file = METADATA_DIR / 'data_quality_report.json'
    with open(report_file, 'w') as f:
        json.dump(report, f, indent=2)
    logger.info(f"Quality report saved : {report_file}")


def save_validation_failures(all_stats: List[Dict]):
    """Write per-symbol validation failures to CSV."""
    METADATA_DIR.mkdir(parents=True, exist_ok=True)
    failures_file = METADATA_DIR / 'validation_failures.csv'

    rows = [
        {
            'symbol':   s['symbol'],
            'exchange': s['exchange'],
            'records':  s['records'],
            'issue':    issue
        }
        for s in all_stats
        if s['status'] in ('success', 'updated', 'validated') and not s['validation_passed']
        for issue in s['validation_issues']
    ]

    if not rows:
        logger.info("No validation failures to report")
        return

    pd.DataFrame(rows).to_csv(failures_file, index=False)
    logger.info(f"Validation failures  : {failures_file} ({len(rows)} issues)")


# ============================================================================
# ARGUMENT PARSING
# ============================================================================

def parse_arguments():
    """Parse and validate CLI arguments."""
    parser = argparse.ArgumentParser(
        description='Script 3: Data Consolidator & Validator (v3.3)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Modes
-----
  --mode full            Process NEW symbols only; skip existing files.
                         Uses date-major pivot (OPT-1) for high performance.
                         Use for initial setup with validation.

  --mode incremental     Append new dates to existing files; full-build
                         new symbols automatically. With validation.
                         Use for daily operations after Script 1 incremental.

  --mode consolidate-only  Fast consolidation WITHOUT validation.
                           Supports two types:

                           --type full (default)
                             Process NEW symbols only, skip existing.
                             Uses date-major pivot (OPT-1).

                           --type incremental
                             Append new dates to existing, process new symbols.
                             Use for fast daily updates without validation.

  --mode full --force    Rebuild EVERY symbol from scratch with validation.
                         Use after bug fixes or logic changes.

  --mode validate-only   Check quality of existing consolidated files;
                         no data is written. Runs in parallel (OPT-2).

Filters (combine with any mode)
-------------------------------
  --exchange NYSE        Restrict to one exchange.
  --symbols A.US,B.US    Restrict to a comma-separated list of symbols.
  --max-symbols 100      Cap number of symbols (useful for testing).

Examples
--------
  # Standard modes (with validation)
  python scripts/03_consolidate_validate_data.py --mode full
  python scripts/03_consolidate_validate_data.py --mode incremental
  python scripts/03_consolidate_validate_data.py --mode full --force
  python scripts/03_consolidate_validate_data.py --mode validate-only

  # Fast modes (no validation)
  python scripts/03_consolidate_validate_data.py --mode consolidate-only
  python scripts/03_consolidate_validate_data.py --mode consolidate-only --type full
  python scripts/03_consolidate_validate_data.py --mode consolidate-only --type incremental
  python scripts/03_consolidate_validate_data.py --mode consolidate-only --force

  # With filters
  python scripts/03_consolidate_validate_data.py --mode incremental --exchange NYSE
        """
    )

    parser.add_argument(
        '--mode',
        choices=['full', 'incremental', 'consolidate-only', 'validate-only'],
        default='full',
        help='Execution mode (default: full)'
    )
    parser.add_argument(
        '--exchange',
        help='Restrict to one exchange, e.g. NYSE'
    )
    parser.add_argument(
        '--symbols',
        help='Comma-separated list of symbols to process'
    )
    parser.add_argument(
        '--force',
        action='store_true',
        help='Rebuild all files from scratch (valid with --mode full or --mode consolidate-only)'
    )
    parser.add_argument(
        '--type',
        choices=['full', 'incremental'],
        default='full',
        help='Type for consolidate-only mode (default: full)'
    )
    parser.add_argument(
        '--max-symbols',
        type=int,
        help='Process at most N symbols (for testing)'
    )

    args = parser.parse_args()

    if args.force and args.mode not in ('full', 'consolidate-only'):
        parser.error("--force can only be used with --mode full or --mode consolidate-only")

    if args.type != 'full' and args.mode != 'consolidate-only':
        parser.error("--type can only be used with --mode consolidate-only")

    return args


# ============================================================================
# MAIN
# ============================================================================

def main() -> int:
    start_time = datetime.now()

    logger.info("=" * 70)
    logger.info("DATA CONSOLIDATOR & VALIDATOR - Script 3")
    logger.info("Architecture v3.3 (Mar 2026)")
    logger.info("=" * 70)

    args = parse_arguments()

    logger.info(f"Mode      : {args.mode.upper()}{' --force' if args.force else ''}")
    if args.mode == 'consolidate-only':
        logger.info(f"Type      : {args.type.upper()}")
    if args.exchange:
        logger.info(f"Exchange  : {args.exchange}")
    if args.symbols:
        logger.info(f"Symbols   : {args.symbols}")
    logger.info(f"Workers   : {MAX_WORKER_PROCESSES} (validate-only parallelism)")
    logger.info(f"Chunk size: {DATE_FILE_CHUNK_SIZE} date files per memory chunk (full build)")

    # ── Load shared config (once per run) ──────────────────────────
    exchange_config = load_exchange_config()
    company_info    = load_company_info()
    validator       = DataValidator()
    logger.info("Data validator initialised")

    # ── Resolve symbol list ─────────────────────────────────────────
    if args.symbols:
        symbol_list        = [s.strip() for s in args.symbols.split(',')]
        symbol_to_exchange = {}
        for sym in symbol_list:
            exc = sym.split('.')[-1] if '.' in sym else list(exchange_config.keys())[0]
            symbol_to_exchange[sym] = exc
    elif args.exchange:
        symbol_to_exchange = get_all_symbols_from_bulk([args.exchange])
    else:
        symbol_to_exchange = get_all_symbols_from_bulk()

    if not symbol_to_exchange:
        logger.error("No symbols to process. Run Script 1 first.")
        return 1

    if args.max_symbols:
        symbol_to_exchange = dict(list(symbol_to_exchange.items())[:args.max_symbols])
        logger.info(f"Capped at {args.max_symbols} symbols for testing")

    logger.info(f"\n{'=' * 70}")
    mode_display = args.mode.upper()
    if args.mode == 'consolidate-only':
        mode_display += f" ({args.type.upper()})"
    logger.info(f"PROCESSING {len(symbol_to_exchange)} SYMBOLS  |  MODE: {mode_display}")
    logger.info(f"{'=' * 70}")

    # ── Pre-load corporate actions (once per exchange, not per symbol) ──
    exchange_splits:    Dict[str, pd.DataFrame] = {}
    exchange_dividends: Dict[str, pd.DataFrame] = {}
    exchange_split_sets: Dict[str, frozenset]   = {}   # OPT-6

    for exc in set(symbol_to_exchange.values()):
        splits, dividends           = load_corporate_actions(exc)
        exchange_splits[exc]        = splits
        exchange_dividends[exc]     = dividends
        exchange_split_sets[exc]    = build_split_exclusion_set(splits)   # OPT-6

    # ── Group symbols by exchange ───────────────────────────────────
    symbols_by_exchange: Dict[str, List[str]] = defaultdict(list)
    for symbol, exchange in symbol_to_exchange.items():
        symbols_by_exchange[exchange].append(symbol)

    # ── Main processing ─────────────────────────────────────────────
    all_stats:  List[Dict] = []
    all_symbol_dates: Dict[str, Dict[str, str]] = {}   # OPT-7: {exchange: {sym_base: date}}

    # ── VALIDATE-ONLY: parallel reads, no writes ────────────────────
    if args.mode == 'validate-only':
        all_stats = run_validate_only_parallel(symbol_to_exchange)

    # ── INCREMENTAL or CONSOLIDATE-ONLY INCREMENTAL ─────────────────
    elif (
        args.mode == 'incremental' or
        (args.mode == 'consolidate-only' and args.type == 'incremental')
    ):
        skip_validation     = (args.mode == 'consolidate-only')
        use_bulk_opt        = len(symbol_to_exchange) >= BULK_OPTIMIZATION_THRESHOLD
        symbols_filter_mode = bool(args.symbols)   # --symbols forces per-symbol path

        if use_bulk_opt and not symbols_filter_mode:
            # ── OPTIMISED BULK INCREMENTAL PATH ────────────────────
            logger.info(
                f"Using optimised bulk incremental processing "
                f"({len(symbol_to_exchange)} symbols)"
            )

            for exchange, sym_list in symbols_by_exchange.items():
                logger.info(
                    f"\nProcessing exchange: {exchange} ({len(sym_list)} symbols)"
                )
                splits    = exchange_splits.get(exchange, pd.DataFrame())
                split_set = exchange_split_sets.get(exchange, frozenset())

                exchange_stats = process_incremental_bulk_optimized(
                    exchange=exchange,
                    symbol_to_exchange=symbol_to_exchange,
                    company_info=company_info,
                    splits=splits,
                    dividends=exchange_dividends.get(exchange, pd.DataFrame()),
                    validator=validator,
                    exchange_config=exchange_config,
                    skip_validation=skip_validation,
                    split_set=split_set,
                )

                # Handle new symbols discovered during incremental
                symbols_needing_full_build = [
                    s['symbol'] for s in exchange_stats
                    if s['status'] == 'needs_full_build'
                ]

                if symbols_needing_full_build:
                    logger.info(
                        f"  {exchange}: {len(symbols_needing_full_build)} "
                        f"new symbols need full build"
                    )
                    for symbol in tqdm(
                        symbols_needing_full_build,
                        desc=f"  {exchange} new",
                        leave=False
                    ):
                        _, stats = consolidate_symbol(
                            symbol=symbol,
                            exchange=exchange,
                            company_info=company_info,
                            splits=splits,
                            dividends=exchange_dividends.get(exchange, pd.DataFrame()),
                            validator=validator,
                            exchange_config=exchange_config,
                            force=False,
                            skip_validation=skip_validation,
                            split_set=split_set,
                        )
                        # Replace the needs_full_build placeholder
                        for i, s in enumerate(exchange_stats):
                            if s['symbol'] == symbol:
                                exchange_stats[i] = stats
                                break

                all_stats.extend(exchange_stats)

            # Mark symbols with no new dates as up_to_date
            processed_symbols = set(s['symbol'] for s in all_stats)
            for symbol, exchange in symbol_to_exchange.items():
                if symbol not in processed_symbols:
                    if (CONSOLIDATED_DIR / f"{symbol}.parquet").exists():
                        all_stats.append({
                            'symbol':            symbol,
                            'exchange':          exchange,
                            'status':            'up_to_date',
                            'records':           0,
                            'new_records':       0,
                            'validation_passed': True,
                            'validation_issues': []
                        })

        else:
            # ── LEGACY PER-SYMBOL INCREMENTAL PATH ─────────────────
            # Used when --symbols filter is active or < BULK_OPTIMIZATION_THRESHOLD
            logger.info(
                f"Using per-symbol incremental processing ({len(symbol_to_exchange)} symbols)"
            )

            with tqdm(total=len(symbol_to_exchange), desc="Processing", unit="sym") as pbar:
                for symbol, exchange in symbol_to_exchange.items():
                    pbar.set_description(symbol)
                    split_set = exchange_split_sets.get(exchange, frozenset())

                    _, stats = update_symbol_incremental(
                        symbol=symbol,
                        exchange=exchange,
                        company_info=company_info,
                        splits=exchange_splits.get(exchange, pd.DataFrame()),
                        dividends=exchange_dividends.get(exchange, pd.DataFrame()),
                        validator=validator,
                        exchange_config=exchange_config,
                        skip_validation=skip_validation,
                        split_set=split_set,
                    )
                    all_stats.append(stats)
                    pbar.update(1)

    # ── FULL or CONSOLIDATE-ONLY FULL ───────────────────────────────
    else:
        skip_validation  = (args.mode == 'consolidate-only')
        symbols_filter   = bool(args.symbols)

        if not symbols_filter and len(symbol_to_exchange) >= BULK_OPTIMIZATION_THRESHOLD:
            # ── OPT-1: DATE-MAJOR FULL BUILD ───────────────────────
            logger.info(
                f"Using date-major full build (OPT-1) — "
                f"{len(symbol_to_exchange)} symbols"
            )

            # OPT-7: collect state to write once after all exchanges
            master_state: Dict = load_consolidation_state()

            for exchange, sym_list in symbols_by_exchange.items():
                logger.info(
                    f"\nProcessing exchange: {exchange} ({len(sym_list)} symbols)"
                )
                split_set = exchange_split_sets.get(exchange, frozenset())

                exchange_stats, symbol_dates = build_exchange_date_major(
                    exchange=exchange,
                    split_set=split_set,
                    company_info=company_info,
                    exchange_config=exchange_config,
                    validator=validator,
                    skip_validation=skip_validation,
                    force=args.force,
                )

                # OPT-7: accumulate symbol dates for batch state write
                if symbol_dates:
                    last_date = max(symbol_dates.values())
                    update_consolidation_state(
                        exchange=exchange,
                        last_date=last_date,
                        symbol_count=sum(
                            1 for s in exchange_stats if s['status'] == 'success'
                        ),
                        symbol_dates=symbol_dates,
                        state=master_state,   # pass state to avoid repeated file reads
                    )
                    # Fix-3: keep last_update.json in sync with consolidated truth
                    write_last_update(exchange, last_date)

                all_stats.extend(exchange_stats)

        else:
            # ── LEGACY PER-SYMBOL FULL PATH ─────────────────────────
            # Used when --symbols filter is active or < threshold
            logger.info(
                f"Using per-symbol full processing ({len(symbol_to_exchange)} symbols)"
            )

            with tqdm(total=len(symbol_to_exchange), desc="Processing", unit="sym") as pbar:
                for symbol, exchange in symbol_to_exchange.items():
                    pbar.set_description(symbol)
                    split_set = exchange_split_sets.get(exchange, frozenset())

                    _, stats = consolidate_symbol(
                        symbol=symbol,
                        exchange=exchange,
                        company_info=company_info,
                        splits=exchange_splits.get(exchange, pd.DataFrame()),
                        dividends=exchange_dividends.get(exchange, pd.DataFrame()),
                        validator=validator,
                        exchange_config=exchange_config,
                        force=args.force,
                        skip_validation=skip_validation,
                        split_set=split_set,
                    )
                    all_stats.append(stats)
                    pbar.update(1)

    # ── Sync last_update.json (safety net for legacy paths) ─────────────────
    # The optimised incremental and OPT-1 full-build paths already call
    # write_last_update() inline.  This loop covers the legacy per-symbol
    # paths (incremental or full) where no inline call was made, and also
    # acts as a final safety net for any future path additions.
    # get_last_consolidated_date() uses the consolidation_state cache as an
    # O(1) fast path and only falls back to a filesystem scan on a cache miss.
    processed_exchanges = set(
        s['exchange'] for s in all_stats
        if s.get('status') in ('success', 'updated', 'up_to_date')
    )
    if processed_exchanges:
        logger.info("Syncing last_update.json with consolidated data...")
        synced: list = []
        for exch in sorted(processed_exchanges):
            last_date = get_last_consolidated_date(exch, symbol_to_exchange)
            if last_date > '1900-01-01':
                write_last_update(exch, last_date)
                synced.append(f"{exch}→{last_date}")
        if synced:
            logger.info(f"  last_update.json synced: {', '.join(synced)}")

    # ── Reports ─────────────────────────────────────────────────────
    logger.info(f"\n{'=' * 70}")
    logger.info("GENERATING REPORTS")
    logger.info(f"{'=' * 70}")

    report = generate_quality_report(all_stats)
    save_quality_report(report)
    save_validation_failures(all_stats)

    # ── Summary ─────────────────────────────────────────────────────
    duration = datetime.now() - start_time

    logger.info(f"\n{'=' * 70}")
    logger.info("COMPLETE")
    logger.info(f"{'=' * 70}")
    logger.info(f"Duration      : {duration}")
    logger.info(f"Total symbols : {len(all_stats)}")
    logger.info(f"\nStatus breakdown:")
    for status, count in sorted(report['by_status'].items()):
        logger.info(f"  {status:<24}: {count:>6}")

    if args.mode == 'incremental':
        inc = report['incremental_stats']
        logger.info(f"\nIncremental stats:")
        logger.info(f"  Symbols updated     : {inc['symbols_updated']}")
        logger.info(f"  New records added   : {inc['total_new_records']}")
        logger.info(f"  Already up-to-date  : {inc['symbols_up_to_date']}")

    vs = report['validation_summary']
    logger.info(f"\nValidation (processed symbols only):")
    logger.info(f"  Validated   : {vs['total_symbols']}")
    logger.info(f"  Passed      : {vs['passed']}")
    logger.info(f"  Failed      : {vs['failed']}")
    logger.info(f"  Pass rate   : {vs['pass_rate']:.1%}")

    if report['common_issues']:
        logger.info(f"\nTop issues:")
        for issue, count in list(report['common_issues'].items())[:5]:
            logger.info(f"  [{count:>4}x] {issue}")

    logger.info(f"\nOutputs:")
    logger.info(f"  Consolidated : {CONSOLIDATED_DIR}")
    logger.info(f"  Quality rpt  : {METADATA_DIR / 'data_quality_report.json'}")
    logger.info(f"  Failures CSV : {METADATA_DIR / 'validation_failures.csv'}")
    logger.info(f"  State cache  : {CONSOLIDATION_STATE_FILE}")
    logger.info("=" * 70)

    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        logger.warning("\nInterrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
