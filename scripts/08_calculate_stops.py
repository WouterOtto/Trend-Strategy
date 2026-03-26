#!/usr/bin/env python3
"""
Script 8: Stop-Loss Calculator
================================
Calculate initial and trailing stop-loss levels for all sized positions.

Purpose:
    Receives the momentum-ranked universe from Script 7 and computes precise,
    rules-based stop-loss prices for every instrument using ATR-based
    formulas.  Two stop regimes are implemented:

        1. INITIAL STOP  (set once at entry, never adjusted downward)
               Stop = Entry_Price - (3.0 × ATR)
               Active from entry until trailing stop replaces it.

        2. TRAILING STOP  (activates after +15% profit, updates Fridays only)
               Activation : Current_Price >= Entry_Price × 1.15
               New_Stop   = max(Current_Trailing_Stop,
                                Friday_Close - (4.0 × Friday_ATR))
               Ratchet    : Only moves UP, never down.

Stop Update Schedule:
    - New entries      → Initial stop set once at entry (this script run)
    - Existing holds   → Check trailing activation on EVERY run
    - Trailing update  → Written ONLY on Fridays (weekly_friday_close rule)
    - Non-Friday run   → Trailing stop value carried forward unchanged

Logic Flow Per Position:
    âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
    â is_new_entry == True?                                           â
    â   YES → calculate_initial_stop(entry_price, entry_ATR)         â
    â         stop_type = "initial"                                   â
    â                                                                 â
    â   NO  → load original entry_price from portfolio_state         â
    â         load current close & ATR from indicators               â
    â         profit_pct = (current_close - orig_entry) / orig_entry â
    â                                                                 â
    â         profit_pct >= 15%?                                      â
    â           YES → trailing active                                 â
    â                 is_friday?                                      â
    â                   YES → recalculate, ratchet up if higher       â
    â                   NO  → carry forward existing trailing stop    â
    â                                                                 â
    â           NO  → retain initial stop from portfolio_state        â
    âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

Dependencies (run before this script):
    01_download_eodhd_bulk.py
    02_download_yahoo_fundamentals.py
    03_consolidate_validate_data.py
    04_screen_universe.py
    05_calculate_indicators.py
    06_qualify_trends.py
    07_rank_momentum.py
    07_rank_momentum.py              ← THIS SCRIPT'S PRIMARY INPUT

Inputs:
    - data_cache/signals/momentum_ranked.json    (from Script 7, required)
    - data_cache/indicators/{symbol}_indicators.parquet  (for existing positions)
    - data/portfolio_state.json                  (optional – existing stop levels)
    - config/strategy_parameters.json            (optional – override defaults)

Outputs:
    - data_cache/portfolio/stop_levels.json          (primary – consumed by Script 10 & 11)
    - data_cache/portfolio/stop_levels_summary.json  (run statistics)
    - reports/portfolio/{YYYYMMDD}_stop_levels.csv   (human-readable audit trail)
    - logs/stop_levels_{timestamp}.log

Execution:
    # Standard run (required after every rebalancing or position change)
    python scripts/08_calculate_stops.py --as-of-date 2026-01-31

    # Dry run (compute and display only, no files written)
    python scripts/08_calculate_stops.py --as-of-date 2026-01-31 --dry-run

    # Force Friday update even on non-Friday (manual override, use with care)
    python scripts/08_calculate_stops.py --as-of-date 2026-01-31 --force-friday

Architecture: v3.2 (Feb 2026)
"""

import os
import sys
import json
import logging
import argparse
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# ============================================================================
# PATH SETUP
# ============================================================================

PROJECT_ROOT  = Path(__file__).parent.parent
DATA_CACHE_DIR = PROJECT_ROOT / "data_cache"
SIGNALS_DIR    = DATA_CACHE_DIR / "signals"
PORTFOLIO_DIR  = DATA_CACHE_DIR / "portfolio"
INDICATORS_DIR = DATA_CACHE_DIR / "indicators"
DATA_DIR       = PROJECT_ROOT / "data"
REPORTS_DIR    = PROJECT_ROOT / "reports" / "portfolio"
CONFIG_DIR     = PROJECT_ROOT / "config"
LOG_DIR        = PROJECT_ROOT / "logs"

# ---------------------------------------------------------------------------
# Load centralized parameters.
# ---------------------------------------------------------------------------
import sys as _sys
_sys.path.insert(0, str(PROJECT_ROOT))
from config.params import P, ConfigurationError
from config.strategies import resolve_strategies, add_strategy_argument, StrategyDef

# ============================================================================
# STRATEGY CONSTANTS  (aligned with Architecture v3.2 / strategy_parameters.json)
# ============================================================================

# Stop parameters — sourced from config/strategy_parameters.json.
INITIAL_STOP_MULTIPLIER:  float = P.stops.init_stop_mult
TRAILING_STOP_MULTIPLIER: float = P.stops.trail_stop_mult
TRAILING_ACTIVATION_PCT:  float = P.stops.trail_activation
MAX_STOP_DISTANCE_PCT:    float = P.stops.max_stop_distance_pct

# ============================================================================
# LOGGING
# ============================================================================

def setup_logging() -> logging.Logger:
    """Configure dual-sink (file + stdout) logging."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file  = LOG_DIR / f"stop_levels_{timestamp}.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    logger = logging.getLogger(__name__)
    logger.info(f"Log file: {log_file}")
    return logger


logger = setup_logging()

# ============================================================================
# HELPERS
# ============================================================================

def validate_date(date_str: str) -> None:
    """Raise ValueError if date_str is not a valid YYYY-MM-DD string."""
    datetime.strptime(date_str, "%Y-%m-%d")


def is_friday(date_str: str) -> bool:
    """Return True if the given date falls on a Friday (weekday == 4)."""
    return pd.to_datetime(date_str).dayofweek == 4


def atr_abs_from_pct(atr_pct: float, price: float) -> float:
    """
    Convert ATR expressed as a percentage of price to an absolute value.

    Formula:
        ATR_abs = (ATR_pct / 100) × Price

    Args:
        atr_pct: ATR as percentage of price (e.g. 2.5 means 2.5%).
        price:   Reference price (entry or current close).

    Returns:
        ATR in the same currency unit as price.
    """
    return (atr_pct / 100.0) * price


# ============================================================================
# STRATEGY PARAMETER LOADER
# ============================================================================

def load_strategy_params() -> Dict:
    """
    Load stop-loss parameters from config/strategy_parameters.json.

    Falls back to module-level constants if the file is absent or the
    'stops' section is missing.  This guarantees the script always runs
    even in a fresh environment that has not yet created the config file.

    Returns:
        Dict with keys: initial_stop_multiplier, trailing_stop_multiplier,
                        trailing_activation_profit_pct, update_frequency.
    """
    defaults = {
        "initial_stop_multiplier":       INITIAL_STOP_MULTIPLIER,
        "trailing_stop_multiplier":      TRAILING_STOP_MULTIPLIER,
        "trailing_activation_profit_pct": TRAILING_ACTIVATION_PCT * 100,  # stored as 15.0
        "update_frequency":              "weekly_friday",
    }

    config_file = CONFIG_DIR / "strategy_parameters.json"
    if not config_file.exists():
        logger.info(
            f"config/strategy_parameters.json not found – using built-in defaults."
        )
        return defaults

    with open(config_file, "r") as f:
        config = json.load(f)

    stops_cfg = config.get("stops", {})
    if not stops_cfg:
        logger.info("No 'stops' section in strategy_parameters.json – using defaults.")
        return defaults

    # Merge: config overrides defaults
    merged = {
        "initial_stop_multiplier":        float(stops_cfg.get(
            "initial_stop_multiplier", defaults["initial_stop_multiplier"]
        )),
        "trailing_stop_multiplier":       float(stops_cfg.get(
            "trailing_stop_multiplier", defaults["trailing_stop_multiplier"]
        )),
        "trailing_activation_profit_pct": float(stops_cfg.get(
            "trailing_activation_profit_pct",
            defaults["trailing_activation_profit_pct"]
        )),
        "update_frequency": stops_cfg.get(
            "update_frequency", defaults["update_frequency"]
        ),
    }

    logger.info(
        f"Stop parameters loaded from config: "
        f"initial×{merged['initial_stop_multiplier']:.1f} ATR | "
        f"trailing×{merged['trailing_stop_multiplier']:.1f} ATR | "
        f"activation at +{merged['trailing_activation_profit_pct']:.0f}%"
    )
    return merged


# ============================================================================
# DATA LOADING
# ============================================================================

def load_momentum_ranked(as_of_date: str) -> Tuple[Dict, Dict]:
    """
    Load the momentum-ranked universe produced by Script 7.

    Expected file: data_cache/signals/momentum_ranked.json

    Returns:
        Tuple of (positions_dict, metadata_dict).
        positions_dict is keyed by symbol; each entry contains
        entry_price (=close), atr_pct, adx and all other
        fields needed to calculate initial stop levels.

    Raises:
        SystemExit on missing file, empty list, or format errors.
    """
    ranked_file = SIGNALS_DIR / "momentum_ranked.json"

    if not ranked_file.exists():
        logger.error(
            f"momentum_ranked.json not found at {ranked_file}.\n"
            "Please run Script 7 (07_rank_momentum.py) first."
        )
        sys.exit(1)

    with open(ranked_file, "r") as f:
        data = json.load(f)

    ranked_list = data.get("ranked", [])
    metadata    = data.get("metadata", {})

    if not ranked_list:
        logger.error(
            "momentum_ranked.json contains no ranked instruments. "
            "Check that Script 7 completed successfully."
        )
        sys.exit(1)

    file_date = metadata.get("as_of_date", "")
    if file_date and file_date != as_of_date:
        logger.warning(
            f"momentum_ranked.json was generated for {file_date}, "
            f"but --as-of-date is {as_of_date}. "
            "Consider re-running Script 7 for the correct date."
        )

    # Build symbol-keyed dict; expose 'entry_price' alias of 'close' for
    # downstream stop calculation functions that expect that key name.
    positions: Dict = {}
    for entry in ranked_list:
        sym = entry.get("symbol")
        if not sym:
            continue
        positions[sym] = {
            **entry,
            "entry_price": entry.get("close"),
            "is_new_entry": True,   # overridden per-symbol after portfolio_state load
        }

    logger.info(f"✔ Loaded {len(positions)} ranked instruments from {ranked_file}")
    return positions, metadata




def load_portfolio_state(path: Optional[Path] = None) -> Dict:
    """
    Optionally load current portfolio positions (existing positions only).

    Used to retrieve the ORIGINAL entry price and current stop levels for
    positions that are being held over from a prior rebalancing cycle.
    If the file does not exist, all positions in position_sizes.json are
    treated as new entries (initial stop only).

    Expected keys per position:
        entry_price, initial_stop_price, trailing_stop_price (or null),
        stop_type ("initial" | "trailing"), stop_last_update_date

    Returns:
        Dict keyed by symbol, or {} if unavailable.
    """
    state_file = path or (DATA_DIR / "portfolio_state.json")

    if not state_file.exists():
        logger.info(
            f"portfolio_state.json not found at {state_file}. "
            "Assuming no existing positions – all treated as new entries."
        )
        return {}

    with open(state_file, "r") as f:
        state = json.load(f)

    # Support both flat format {"symbol": {...}} and nested {"positions": {...}}
    positions = state.get("positions", state)
    logger.info(
        f"â Loaded {len(positions)} existing positions from {state_file}"
    )
    return positions


def load_indicators(symbol: str, as_of_date: str) -> Optional[pd.DataFrame]:
    """
    Load indicator time series for a symbol up to as_of_date.

    Looks for: data_cache/indicators/{symbol}_indicators.parquet

    Returns:
        DataFrame with the most recent row available on or before as_of_date,
        or None if the file is missing or no rows fall within the date range.

    Required columns (set by Script 5):
        close, atr_pct, sma_fast, sma_slow, adx
    """
    indicators_file = INDICATORS_DIR / f"{symbol}_indicators.parquet"

    if not indicators_file.exists():
        return None

    try:
        df = pd.read_parquet(indicators_file)
        df.index = pd.to_datetime(df.index)
        df_filtered = df[df.index <= as_of_date]

        if df_filtered.empty:
            return None

        return df_filtered

    except Exception as exc:
        logger.warning(f"  Could not read indicators for {symbol}: {exc}")
        return None


# ============================================================================
# CORE STOP-LOSS FORMULAS
# ============================================================================

def calculate_initial_stop(
    entry_price: float,
    atr_abs: float,
    multiplier: float = INITIAL_STOP_MULTIPLIER,
) -> Dict:
    """
    Calculate the initial stop-loss placed at position entry.

    Formula:
        Initial_Stop = Entry_Price - (multiplier × ATR_abs)

    Characteristics:
        - Set ONCE at entry; never adjusted downward.
        - Fixed distance based on entry-day ATR.
        - Typical distance: 6-9% below entry for stocks.
        - Superseded by trailing stop once +15% profit is reached.

    Example (Architecture Â§2.7):
        Entry = â¬100, ATR = â¬2.50
        Initial_Stop = 100 - (3.0 × 2.50) = â¬92.50
        Stop Distance = 7.50%

    Args:
        entry_price: Price at which the position was / will be entered.
        atr_abs:     ATR in absolute currency units (not percentage) at entry.
        multiplier:  ATR multiple for stop distance (default 3.0).

    Returns:
        Dict with stop price, diagnostics, and metadata.
    """
    if entry_price <= 0:
        raise ValueError(f"entry_price must be > 0, got {entry_price}")
    if atr_abs <= 0:
        raise ValueError(f"atr_abs must be > 0, got {atr_abs}")

    stop_price        = entry_price - (multiplier * atr_abs)
    stop_distance_abs = entry_price - stop_price
    stop_distance_pct = (stop_distance_abs / entry_price) * 100.0

    # Guard: stop must be > 0 (otherwise instrument has extreme volatility)
    if stop_price <= 0:
        logger.warning(
            f"  Initial stop calculated as ≤ 0 ({stop_price:.4f}). "
            "ATR may be unusually large relative to price. Clamping to 0.01."
        )
        stop_price = 0.01

    return {
        "stop_price":             round(stop_price, 4),
        "stop_distance_abs":      round(stop_distance_abs, 4),
        "stop_distance_pct":      round(stop_distance_pct, 4),
        "atr_multiple":           multiplier,
        "stop_type":              "initial",
        "trailing_active":        False,
        "never_moves_down":       True,
        "update_rule":            "set_once_at_entry",
    }


def calculate_trailing_stop(
    current_price:        float,
    entry_price:          float,
    current_atr_abs:      float,
    existing_trailing_stop: Optional[float] = None,
    activation_pct:       float = TRAILING_ACTIVATION_PCT,
    multiplier:           float = TRAILING_STOP_MULTIPLIER,
) -> Dict:
    """
    Calculate or update the trailing stop-loss.

    Activation Rule:
        Trailing stop activates when:
            Current_Price >= Entry_Price × (1 + activation_pct)
        i.e. by default: profit >= +15% from entry.

    Update Formula:
        New_Trailing_Stop = max(
            existing_trailing_stop,            ← ratchet: never move down
            current_price - (multiplier × ATR) ← new calculated level
        )

    The ratchet ensures the trailing stop only ever rises, locking in
    increasingly more profit as the trend continues.

    Example (Architecture Â§2.7):
        Entry = â¬100, Current_Price = â¬120 (+20%), ATR = â¬3.00
        Existing_Trailing_Stop = â¬110

        New_Trailing_Stop = max(110, 120 â 4.0 × 3.00)
                          = max(110, 108)
                          = â¬110  (unchanged – ratchet prevents step-down)

        Next Friday: Price = â¬125, ATR = â¬3.20
        New_Trailing_Stop = max(110, 125 â 4.0 × 3.20)
                          = max(110, 112.20)
                          = â¬112.20  (ratcheted up, locking in more profit)

    Args:
        current_price:          Current (Friday) close price.
        entry_price:            Original entry price for this position.
        current_atr_abs:        Current ATR in absolute currency units.
        existing_trailing_stop: Trailing stop from last Friday update (or None).
        activation_pct:         Profit required to activate trailing (default 0.15).
        multiplier:             ATR multiple for trailing stop distance (default 4.0).

    Returns:
        Dict with trailing stop status and, if active, the new stop price.
        Key 'active' indicates whether the trailing stop is in force.
    """
    profit_pct = (current_price - entry_price) / entry_price

    # Check activation threshold
    if profit_pct < activation_pct:
        return {
            "active":                   False,
            "profit_pct":               round(profit_pct * 100, 4),
            "activation_threshold_pct": round(activation_pct * 100, 2),
            "activation_price":         round(entry_price * (1.0 + activation_pct), 4),
            "reason":                   (
                f"Profit {profit_pct*100:.2f}% < "
                f"{activation_pct*100:.0f}% activation threshold"
            ),
        }

    # Trailing stop is active – calculate new level
    calculated_stop = current_price - (multiplier * current_atr_abs)

    if existing_trailing_stop is None:
        # First time activating – use calculated stop directly
        new_stop  = calculated_stop
        moved_up  = True   # Newly activated counts as "moved up from None"
    else:
        new_stop  = max(existing_trailing_stop, calculated_stop)
        moved_up  = (new_stop > existing_trailing_stop)

    # Guard: stop must be > 0
    if new_stop <= 0:
        logger.warning(
            f"  Trailing stop calculated as ≤ 0 ({new_stop:.4f}). Clamping to 0.01."
        )
        new_stop = 0.01

    stop_distance_abs = current_price - new_stop
    stop_distance_pct = (stop_distance_abs / current_price) * 100.0

    return {
        "active":                    True,
        "stop_price":                round(new_stop, 4),
        "stop_distance_abs":         round(stop_distance_abs, 4),
        "stop_distance_pct":         round(stop_distance_pct, 4),
        "atr_multiple":              multiplier,
        "stop_type":                 "trailing",
        "trailing_active":           True,
        "only_moves_up":             True,
        "profit_pct":                round(profit_pct * 100, 4),
        "activation_threshold_pct":  round(activation_pct * 100, 2),
        "activation_price":          round(entry_price * (1.0 + activation_pct), 4),
        "calculated_stop_raw":       round(calculated_stop, 4),
        "previous_trailing_stop":    existing_trailing_stop,
        "moved_up":                  moved_up,
        "update_frequency":          "weekly_friday_close",
    }


# ============================================================================
# PER-POSITION STOP ORCHESTRATOR
# ============================================================================

def process_position_stop(
    symbol:           str,
    position:         Dict,
    existing_state:   Optional[Dict],
    as_of_date:       str,
    update_day:       bool,
    params:           Dict,
) -> Dict:
    """
    Determine the correct stop-loss level for a single position.

    Decision logic:
        1. NEW ENTRY (is_new_entry == True):
               → Set initial stop using entry-day ATR from position_sizes.
               → All fields come from the sizing output; no indicator reload needed.

        2. EXISTING HOLD – no portfolio_state entry found:
               → Treat as new entry (conservative fallback; set initial stop).
               → Log a warning so the operator is aware.

        3. EXISTING HOLD – portfolio_state found, trailing NOT yet active:
               → Retain the original initial_stop_price from portfolio_state.
               → Reload current close & ATR from indicators to check activation.
               → If profit >= 15% AND it is an update_day, switch to trailing.

        4. EXISTING HOLD – trailing already active:
               → If update_day (Friday): recalculate trailing stop with ratchet.
               → If NOT update_day: carry forward existing trailing_stop_price.

    Args:
        symbol:         Instrument ticker (e.g. "AAPL.US").
        position:       Position dict from momentum_ranked.json (Script 7 output).
        existing_state: Matching entry from portfolio_state.json, or None.
        as_of_date:     Reference date string "YYYY-MM-DD".
        update_day:     True if trailing stops should be recalculated today.
        params:         Strategy parameters dict (multipliers, activation threshold).

    Returns:
        Complete stop-level dict for this symbol (written to stop_levels.json).
    """
    init_mult    = params["initial_stop_multiplier"]
    trail_mult   = params["trailing_stop_multiplier"]
    activation   = params["trailing_activation_profit_pct"] / 100.0

    # ------------------------------------------------------------------
    # CASE 1: New entry – initial stop from sizing data
    # ------------------------------------------------------------------
    if position.get("is_new_entry", True) or existing_state is None:

        if not position.get("is_new_entry", True) and existing_state is None:
            logger.warning(
                f"  {symbol}: Marked as existing hold but no portfolio_state entry found. "
                "Setting initial stop (conservative fallback)."
            )

        entry_price = position.get("entry_price") or position.get("close_price")
        atr_pct     = position.get("atr_pct")

        if entry_price is None or entry_price <= 0:
            logger.warning(f"  {symbol}: Missing entry_price – SKIPPING stop calculation.")
            return _error_stop(symbol, position, as_of_date, "missing_entry_price")

        if atr_pct is None or atr_pct <= 0:
            logger.warning(f"  {symbol}: Missing or zero atr_pct – SKIPPING.")
            return _error_stop(symbol, position, as_of_date, "missing_atr")

        atr_abs = atr_abs_from_pct(atr_pct, entry_price)
        stop    = calculate_initial_stop(entry_price, atr_abs, multiplier=init_mult)

        return {
            "symbol":                   symbol,
            "stop_price":               stop["stop_price"],
            "stop_type":                "initial",
            "trailing_active":          False,
            # Initial stop anchors
            "initial_stop_price":       stop["stop_price"],
            "initial_stop_set_date":    as_of_date,
            # Trailing stop state (not yet active)
            "trailing_stop_price":      None,
            "trailing_activation_price": round(entry_price * (1.0 + activation), 4),
            # Position context
            "entry_price":              round(entry_price, 4),
            "entry_atr_abs":            round(atr_abs, 4),
            "entry_atr_pct":            round(atr_pct, 4),
            "current_close":            round(entry_price, 4),   # at entry, same as close
            "current_atr_abs":          round(atr_abs, 4),
            "profit_pct":               0.0,
            # Stop diagnostics
            "stop_distance_abs":        stop["stop_distance_abs"],
            "stop_distance_pct":        stop["stop_distance_pct"],
            "atr_multiple_used":        init_mult,
            # Metadata
            "is_new_entry":             True,
            "update_day":               update_day,
            "last_update_date":         as_of_date,
            "next_update":              "trailing_check_on_next_run",
            "moved_up":                 False,
            "calculation_note":         "Initial stop set at entry.",
        }

    # ------------------------------------------------------------------
    # CASE 2 & 3 & 4: Existing hold with portfolio_state data
    # ------------------------------------------------------------------

    # Original entry information (from when the position was first opened)
    orig_entry_price    = float(existing_state.get("entry_price", position["entry_price"]))
    orig_initial_stop   = existing_state.get("initial_stop_price")
    existing_trail_stop = existing_state.get("trailing_stop_price")   # None if not yet active
    prior_stop_type     = existing_state.get("stop_type", "initial")

    # If we never stored an initial stop (older portfolio_state format),
    # reconstruct it from the original entry data that IS available.
    if orig_initial_stop is None:
        entry_atr_pct = float(
            existing_state.get("entry_atr_pct")
            or existing_state.get("atr_pct")
            or position.get("atr_pct", 0)
        )
        if entry_atr_pct > 0:
            entry_atr_abs   = atr_abs_from_pct(entry_atr_pct, orig_entry_price)
            orig_initial_stop = round(
                orig_entry_price - (init_mult * entry_atr_abs), 4
            )
            logger.info(
                f"  {symbol}: Reconstructed initial stop = "
                f"â¬{orig_initial_stop:.4f} (not stored in portfolio_state)."
            )
        else:
            logger.warning(
                f"  {symbol}: Cannot reconstruct initial stop – no ATR data in portfolio_state."
            )

    # Load current indicators (close + ATR for trailing stop calculation)
    ind_df = load_indicators(symbol, as_of_date)

    if ind_df is not None and not ind_df.empty:
        latest       = ind_df.iloc[-1]
        # Use indicator 'close' first; fall back to position dict which may store
        # the price under either 'close_price' (position_sizes format) or 'close'
        # (momentum_ranked format).  A hard key access here caused a KeyError for
        # SHA0.XETRA and any symbol whose position dict uses 'close' not 'close_price'.
        current_close = float(
            latest.get("close")
            or position.get("close_price")
            or position.get("close")
            or position.get("entry_price")
            or 0
        )
        current_atr_pct = float(latest.get("atr_pct", position["atr_pct"]))
    else:
        # Fallback: use values from position_sizes.json (slightly stale but safe)
        logger.warning(
            f"  {symbol}: Indicators file unavailable – "
            "falling back to position_sizes values for current close/ATR."
        )
        current_close   = float(position.get("close_price") or position["entry_price"])
        current_atr_pct = float(position.get("atr_pct", 0))

    current_atr_abs = atr_abs_from_pct(current_atr_pct, current_close)
    profit_pct      = (current_close - orig_entry_price) / orig_entry_price

    # Check if trailing stop activation threshold has been reached
    trailing_check = calculate_trailing_stop(
        current_price=current_close,
        entry_price=orig_entry_price,
        current_atr_abs=current_atr_abs,
        existing_trailing_stop=(
            float(existing_trail_stop) if existing_trail_stop is not None else None
        ),
        activation_pct=activation,
        multiplier=trail_mult,
    )

    # ------------------------------------------------------------------
    # SUB-CASE A: Trailing stop NOT active
    # ------------------------------------------------------------------
    if not trailing_check.get("active", False):
        stop_price  = orig_initial_stop if orig_initial_stop is not None else None

        if stop_price is None:
            logger.warning(
                f"  {symbol}: No initial stop available and trailing not active. "
                "SKIPPING stop level assignment."
            )
            return _error_stop(symbol, position, as_of_date, "no_stop_available")

        return {
            "symbol":                    symbol,
            "stop_price":                round(stop_price, 4),
            "stop_type":                 "initial",
            "trailing_active":           False,
            "initial_stop_price":        round(stop_price, 4),
            "initial_stop_set_date":     existing_state.get("initial_stop_set_date"),
            "trailing_stop_price":       None,
            "trailing_activation_price": trailing_check.get("activation_price"),
            "entry_price":               round(orig_entry_price, 4),
            "entry_atr_abs":             round(
                atr_abs_from_pct(
                    float(existing_state.get("entry_atr_pct") or current_atr_pct),
                    orig_entry_price,
                ), 4
            ),
            "entry_atr_pct":             float(
                existing_state.get("entry_atr_pct") or current_atr_pct
            ),
            "current_close":             round(current_close, 4),
            "current_atr_abs":           round(current_atr_abs, 4),
            "profit_pct":                round(profit_pct * 100, 4),
            "stop_distance_abs":         round(current_close - stop_price, 4),
            "stop_distance_pct":         round(
                (current_close - stop_price) / current_close * 100, 4
            ),
            "atr_multiple_used":         init_mult,
            "is_new_entry":              False,
            "update_day":                update_day,
            "last_update_date":          existing_state.get("last_update_date"),
            "next_update":               "trailing_activation_check_on_next_run",
            "moved_up":                  False,
            "calculation_note":          (
                f"Initial stop retained. "
                f"Trailing activates at â¬{trailing_check.get('activation_price', '?'):.4f} "
                f"(+{activation*100:.0f}% from entry)."
            ),
        }

    # ------------------------------------------------------------------
    # SUB-CASE B: Trailing stop IS active
    # ------------------------------------------------------------------
    if update_day:
        # Friday (or force-friday): recalculate and ratchet
        new_trail_stop  = trailing_check["stop_price"]
        moved_up        = trailing_check.get("moved_up", False)
        last_update     = as_of_date
        calc_note       = (
            f"Trailing stop {'ratcheted up to' if moved_up else 'held at'} "
            f"â¬{new_trail_stop:.4f} on update day."
        )
        next_update = "next_friday"
    else:
        # Non-Friday: carry forward the existing trailing stop unchanged
        new_trail_stop  = (
            float(existing_trail_stop)
            if existing_trail_stop is not None
            else trailing_check["stop_price"]  # First activation on non-Friday: set now
        )
        moved_up        = False
        last_update     = existing_state.get("last_update_date", as_of_date)
        calc_note       = (
            f"Non-update day. Trailing stop carried forward at â¬{new_trail_stop:.4f}. "
            "Next update: Friday close."
        )
        next_update = "next_friday"

    return {
        "symbol":                    symbol,
        "stop_price":                round(new_trail_stop, 4),
        "stop_type":                 "trailing",
        "trailing_active":           True,
        "initial_stop_price":        (
            round(orig_initial_stop, 4) if orig_initial_stop is not None else None
        ),
        "initial_stop_set_date":     existing_state.get("initial_stop_set_date"),
        "trailing_stop_price":       round(new_trail_stop, 4),
        "trailing_activation_price": trailing_check.get("activation_price"),
        "entry_price":               round(orig_entry_price, 4),
        "entry_atr_abs":             round(
            atr_abs_from_pct(
                float(existing_state.get("entry_atr_pct") or current_atr_pct),
                orig_entry_price,
            ), 4
        ),
        "entry_atr_pct":             float(
            existing_state.get("entry_atr_pct") or current_atr_pct
        ),
        "current_close":             round(current_close, 4),
        "current_atr_abs":           round(current_atr_abs, 4),
        "profit_pct":                round(profit_pct * 100, 4),
        "stop_distance_abs":         round(current_close - new_trail_stop, 4),
        "stop_distance_pct":         round(
            (current_close - new_trail_stop) / current_close * 100, 4
        ),
        "atr_multiple_used":         trail_mult,
        "is_new_entry":              False,
        "update_day":                update_day,
        "last_update_date":          last_update,
        "next_update":               next_update,
        "moved_up":                  moved_up,
        "calculation_note":          calc_note,
    }


def _error_stop(symbol: str, position: Dict, as_of_date: str, reason: str) -> Dict:
    """
    Return a sentinel stop record when a stop cannot be calculated.

    The sentinel carries stop_price = None so downstream scripts (Script 10)
    can detect the error state and skip or alert rather than using a
    garbage value.

    Args:
        symbol:     Instrument ticker.
        position:   Position sizing dict.
        as_of_date: Reference date.
        reason:     Short error code for the audit log.

    Returns:
        Minimal error stop dict with stop_price = None.
    """
    return {
        "symbol":           symbol,
        "stop_price":       None,
        "stop_type":        "error",
        "trailing_active":  False,
        "error_reason":     reason,
        "entry_price":      position.get("entry_price"),
        "is_new_entry":     position.get("is_new_entry", True),
        "last_update_date": as_of_date,
        "calculation_note": f"Stop calculation failed: {reason}",
    }


# ============================================================================
# PORTFOLIO-LEVEL STOP ORCHESTRATOR
# ============================================================================

def calculate_all_stops(
    positions:       Dict,
    existing_states: Dict,
    as_of_date:      str,
    update_day:      bool,
    params:          Dict,
) -> Tuple[Dict, Dict]:
    """
    Calculate stop-loss levels for every position in the sized portfolio.

    Iterates all positions, delegates per-position logic to
    process_position_stop(), and assembles both the stops dict and a
    run-level summary.

    Args:
        positions:       Dict of ranked instruments keyed by symbol (Script 7).
        existing_states: Dict of current portfolio positions (portfolio_state.json).
        as_of_date:      Reference date string "YYYY-MM-DD".
        update_day:      True if trailing stops should update (Friday or forced).
        params:          Strategy parameters dict.

    Returns:
        Tuple of:
            stops_dict  – { symbol: stop_record, ... }
            summary     – run-level statistics dict
    """
    stops: Dict   = {}
    errors: List  = []

    new_stops_set         = 0
    trailing_active       = 0
    trailing_updated      = 0
    initial_retained      = 0

    logger.info(f"\nProcessing {len(positions)} positions ...")
    logger.info(f"  Update day (trailing stop recalc): {update_day}")

    for symbol, position in positions.items():
        existing = existing_states.get(symbol)
        position["is_new_entry"] = existing is None  # mark based on portfolio_state

        try:
            stop_record = process_position_stop(
                symbol=symbol,
                position=position,
                existing_state=existing,
                as_of_date=as_of_date,
                update_day=update_day,
                params=params,
            )
        except Exception as exc:
            logger.error(f"  {symbol}: Unexpected error – {exc}", exc_info=True)
            stop_record = _error_stop(symbol, position, as_of_date, f"exception: {exc}")

        stops[symbol] = stop_record

        # Tally statistics
        if stop_record.get("stop_price") is None:
            errors.append(symbol)
        elif stop_record.get("is_new_entry"):
            new_stops_set += 1
        elif stop_record.get("trailing_active"):
            trailing_active += 1
            if stop_record.get("moved_up") or update_day:
                trailing_updated += 1
        else:
            initial_retained += 1

        # Console line
        stop_price    = stop_record.get("stop_price")
        stop_type_str = stop_record.get("stop_type", "?")
        profit_pct    = stop_record.get("profit_pct", 0.0) or 0.0
        new_flag      = "NEW" if stop_record.get("is_new_entry") else "hold"
        moved_flag    = "â" if stop_record.get("moved_up") else " "

        if stop_price is not None:
            logger.info(
                f"  {symbol:<16} "
                f"stop=â¬{stop_price:>9.4f}  "
                f"type={stop_type_str:<8} "
                f"P&L={profit_pct:>+7.2f}%  "
                f"{moved_flag} {new_flag}"
            )
        else:
            logger.warning(
                f"  {symbol:<16} stop=ERROR  reason={stop_record.get('error_reason')}"
            )

    # ── Second pass: portfolio positions NOT in the ranked universe ──────────
    # Script 08 normally sources its position list from momentum_ranked.json.
    # Existing holdings that have dropped out of the ranked universe (e.g. due
    # to a filter change or temporary data gap) receive no stop record, causing
    # Script 10 to skip their stop check entirely — removing all capital
    # protection for those positions.  This pass guarantees every open position
    # always gets a stop record regardless of its ranking status.
    portfolio_only = {
        sym: state
        for sym, state in existing_states.items()
        if sym not in stops
    }

    if portfolio_only:
        logger.info(
            f"\n  [{len(portfolio_only)} portfolio position(s) not in ranked universe "
            f"— running stop maintenance pass]"
        )

    for symbol, existing in portfolio_only.items():
        # Build a minimal position dict from the portfolio_state entry so that
        # process_position_stop() has all the fields it needs.
        entry_px = (
            existing.get("entry_price")
            or existing.get("close_price")
            or existing.get("close")
            or 0
        )
        position = {
            **existing,
            "entry_price": entry_px,
            "close_price": entry_px,
            "close":       entry_px,
            "atr_pct":  existing.get("atr_pct") or existing.get("entry_atr_pct") or 0,
            "is_new_entry": False,
        }

        try:
            stop_record = process_position_stop(
                symbol=symbol,
                position=position,
                existing_state=existing,
                as_of_date=as_of_date,
                update_day=update_day,
                params=params,
            )
        except Exception as exc:
            logger.error(
                f"  {symbol} [portfolio-only]: Unexpected error – {exc}", exc_info=True
            )
            stop_record = _error_stop(symbol, position, as_of_date, f"exception: {exc}")

        stops[symbol] = stop_record

        stop_price = stop_record.get("stop_price")
        if stop_price is not None:
            logger.info(
                f"  {symbol:<16} stop=€{stop_price:>9.4f}  "
                f"type={stop_record.get('stop_type', '?'):<8}  [portfolio-only]"
            )
            initial_retained += 1
        else:
            logger.warning(
                f"  {symbol:<16} stop=ERROR  "
                f"reason={stop_record.get('error_reason')}  [portfolio-only]"
            )
            errors.append(symbol)
    # ────────────────────────────────────────────────────────────────────────

    # Build run summary
    summary = {
        "as_of_date":            as_of_date,
        "is_update_day":         update_day,
        "total_positions":       len(stops),
        "new_stops_set":         new_stops_set,
        "trailing_stops_active": trailing_active,
        "trailing_stops_updated": trailing_updated,
        "initial_stops_retained": initial_retained,
        "errors":                errors,
        "error_count":           len(errors),
        "strategy_params": {
            "initial_stop_multiplier":        params["initial_stop_multiplier"],
            "trailing_stop_multiplier":       params["trailing_stop_multiplier"],
            "trailing_activation_profit_pct": params["trailing_activation_profit_pct"],
            "update_frequency":               params["update_frequency"],
        },
        "generated_at": datetime.now().isoformat(),
    }

    return stops, summary


# ============================================================================
# OUTPUT FUNCTIONS
# ============================================================================

def save_stop_levels(stops: Dict, summary: Dict, output_file: Path) -> None:
    """
    Save stop levels to JSON in the format expected by Scripts 9, 10 and 11.

    Format:
        {
            "metadata": { ...summary stats... },
            "stops": {
                "AAPL.US": { stop_price, stop_type, initial_stop_price, ... },
                ...
            }
        }
    """
    PORTFOLIO_DIR.mkdir(parents=True, exist_ok=True)

    output = {
        "metadata": summary,
        "stops":    stops,
    }

    with open(output_file, "w") as f:
        json.dump(output, f, indent=2, default=str)

    logger.info(f"â Saved {len(stops)} stop levels → {output_file}")


def save_stop_summary(summary: Dict, output_file: Path) -> None:
    """Save run-level statistics to JSON."""
    PORTFOLIO_DIR.mkdir(parents=True, exist_ok=True)

    with open(output_file, "w") as f:
        json.dump(summary, f, indent=2, default=str)

    logger.info(f"â Saved stop summary → {output_file}")


def save_stop_csv(stops: Dict, output_file: Path) -> None:
    """
    Save human-readable CSV for review and audit trail.

    Columns (ordered for readability):
        symbol, stop_price, stop_type, trailing_active,
        entry_price, current_close, profit_pct,
        stop_distance_abs, stop_distance_pct, atr_multiple_used,
        initial_stop_price, trailing_stop_price, trailing_activation_price,
        moved_up, update_day, last_update_date, is_new_entry, calculation_note
    """
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    if not stops:
        logger.warning("No stops to save – skipping CSV export.")
        return

    rows = list(stops.values())
    df   = pd.DataFrame(rows)

    col_order = [
        "symbol", "stop_price", "stop_type", "trailing_active",
        "entry_price", "current_close", "profit_pct",
        "stop_distance_abs", "stop_distance_pct", "atr_multiple_used",
        "initial_stop_price", "trailing_stop_price", "trailing_activation_price",
        "entry_atr_pct", "current_atr_abs",
        "moved_up", "update_day", "last_update_date", "is_new_entry",
        "calculation_note",
    ]
    available = [c for c in col_order if c in df.columns]
    df = df[available]

    df.to_csv(output_file, index=False, float_format="%.4f")
    logger.info(f"â Saved CSV report → {output_file}")


def print_stop_summary(stops: Dict, summary: Dict) -> None:
    """Print a formatted stop table to the console."""
    logger.info(f"\n{'=' * 100}")
    logger.info(
        f"STOP LEVELS  "
        f"(date: {summary.get('as_of_date', 'N/A')}  |  "
        f"update_day: {summary.get('is_update_day')})"
    )
    logger.info(f"{'=' * 100}")

    header = (
        f"{'Symbol':<16} {'Stop â¬':>10}  {'Type':<10} "
        f"{'Initial â¬':>10}  {'Trailing â¬':>11}  "
        f"{'P&L%':>7}  {'Dist%':>6}  {'â':>2}  {'New':>4}"
    )
    logger.info(header)
    logger.info("-" * 100)

    for symbol, rec in sorted(stops.items()):
        sp        = rec.get("stop_price")
        stype     = rec.get("stop_type", "error")
        init_s    = rec.get("initial_stop_price")
        trail_s   = rec.get("trailing_stop_price")
        pnl       = rec.get("profit_pct") or 0.0
        dist      = rec.get("stop_distance_pct") or 0.0
        up_flag   = "â" if rec.get("moved_up") else " "
        new_flag  = "NEW" if rec.get("is_new_entry") else "hold"

        sp_str    = f"â¬{sp:>9.4f}" if sp is not None else f"{'ERROR':>10}"
        init_str  = f"â¬{init_s:>9.4f}" if init_s is not None else f"{'N/A':>10}"
        trail_str = f"â¬{trail_s:>10.4f}" if trail_s is not None else f"{'—':>11}"

        logger.info(
            f"{symbol:<16} {sp_str}  "
            f"{stype:<10} "
            f"{init_str}  "
            f"{trail_str}  "
            f"{pnl:>+7.2f}%  "
            f"{dist:>5.2f}%  "
            f"{up_flag:>2}  "
            f"{new_flag:>4}"
        )

    logger.info(f"\n  Positions processed   : {summary['total_positions']}")
    logger.info(f"  New stops set         : {summary['new_stops_set']}")
    logger.info(f"  Trailing active       : {summary['trailing_stops_active']}")
    logger.info(f"  Trailing updated (â)  : {summary['trailing_stops_updated']}")
    logger.info(f"  Initial stops held    : {summary['initial_stops_retained']}")
    if summary["error_count"] > 0:
        logger.warning(
            f"  â  Errors ({summary['error_count']}): "
            f"{', '.join(summary['errors'])}"
        )


# ============================================================================
# ARGUMENT PARSING
# ============================================================================

def parse_arguments() -> argparse.Namespace:
    """Parse and validate command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Stop-Loss Calculator – Script 8 (v3.2)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Standard run after monthly rebalancing
    python scripts/08_calculate_stops.py --as-of-date 2026-01-31

    # Weekly Friday update (trailing stops recalculated)
    python scripts/08_calculate_stops.py --as-of-date 2026-01-30

    # Dry run: compute and display only – no files written
    python scripts/08_calculate_stops.py --as-of-date 2026-01-31 --dry-run

    # Force trailing stop update on any day (manual override – use with care)
    python scripts/08_calculate_stops.py --as-of-date 2026-01-29 --force-friday

    # Use a custom portfolio state file
    python scripts/08_calculate_stops.py --as-of-date 2026-01-31 \\
        --portfolio-state data/portfolio_state_backup.json

Dependencies (run in order):
    01  02  03  04  05  06  07_rank_momentum.py  ← then this script
        """
    )

    parser.add_argument(
        "--as-of-date",
        required=True,
        metavar="YYYY-MM-DD",
        help="Reference date for stop calculation (use last trading day of month for rebalancing)",
    )
    parser.add_argument(
        "--portfolio-state",
        type=Path,
        default=None,
        metavar="PATH",
        help="Path to data/portfolio_state.json (optional; defaults to data/portfolio_state.json)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute and display stop levels but do not write any output files",
    )
    parser.add_argument(
        "--force-friday",
        action="store_true",
        help=(
            "Force trailing stop recalculation even if today is not Friday. "
            "Use only for manual corrections or testing."
        ),
    )
    parser.add_argument(
        "--account-equity",
        type=float,
        default=None,
        dest="account_equity",
        metavar="EUR",
        help=(
            "Total account equity in EUR (forwarded by the pipeline runner; "
            "not used by this script — position sizing is handled in Script 9)."
        ),
    )

    add_strategy_argument(parser)
    return parser.parse_args()


# ============================================================================
# MAIN
# ============================================================================

def _run_for_strategy(strategy: "StrategyDef", args) -> int:
    """Run stop-loss calculation for one strategy, with namespaced I/O paths."""
    global SIGNALS_DIR, PORTFOLIO_DIR, REPORTS_DIR

    strat_signals   = strategy.signals_dir(DATA_CACHE_DIR)
    strat_portfolio = strategy.portfolio_dir(DATA_CACHE_DIR)
    strat_reports   = strategy.reports_dir(PROJECT_ROOT, "portfolio")
    strat_signals.mkdir(parents=True, exist_ok=True)
    strat_portfolio.mkdir(parents=True, exist_ok=True)
    strat_reports.mkdir(parents=True, exist_ok=True)

    logger.info(f"\n[{strategy.name}] -- {strategy.label} ({'LIVE' if strategy.deployed else 'PAPER'}) --")
    logger.info(f"[{strategy.name}] Signals dir   : {strat_signals}")
    logger.info(f"[{strategy.name}] Portfolio dir  : {strat_portfolio}")

    # Temporarily redirect module-level path globals so all load functions
    # read from the correct strategy namespace automatically.
    _orig_sig, _orig_port, _orig_rep = SIGNALS_DIR, PORTFOLIO_DIR, REPORTS_DIR
    SIGNALS_DIR   = strat_signals
    PORTFOLIO_DIR = strat_portfolio
    REPORTS_DIR   = strat_reports
    try:
        return _run_core(args, strategy.name)
    finally:
        SIGNALS_DIR, PORTFOLIO_DIR, REPORTS_DIR = _orig_sig, _orig_port, _orig_rep


def _run_core(args, strategy_name: str = "") -> int:
    """Core stop-loss logic — called by _run_for_strategy or directly by main()."""
    tag = f"[{strategy_name}] " if strategy_name else ""
def _run_core(args, strategy_name: str = '') -> int:
    tag = f'[{strategy_name}] ' if strategy_name else ''
    start_time = datetime.now()

    logger.info("=" * 70)
    logger.info("STOP-LOSS CALCULATOR – Script 8")
    logger.info("Architecture v3.2 (Feb 2026)")
    logger.info("=" * 70)

    # ââ Parse & validate arguments âââââââââââââââââââââââââââââââââââââââââ
    try:
        validate_date(args.as_of_date)
    except ValueError:
        logger.error(
            f"Invalid date format: '{args.as_of_date}'.  Expected YYYY-MM-DD."
        )
        return 1

    # ââ Determine if this is a trailing-stop update day ââââââââââââââââââââ
    today_is_friday = is_friday(args.as_of_date)
    update_day      = today_is_friday or args.force_friday

    if args.force_friday and not today_is_friday:
        logger.warning(
            f"â   --force-friday active: {args.as_of_date} is NOT a Friday. "
            "Trailing stops will be recalculated regardless."
        )

    logger.info(f"\nAs-of date     : {args.as_of_date}")
    logger.info(f"Day of week    : {pd.to_datetime(args.as_of_date).day_name()}")
    logger.info(f"Is Friday      : {today_is_friday}")
    logger.info(f"Update day     : {update_day} (trailing stops {'WILL' if update_day else 'will NOT'} be recalculated)")
    logger.info(f"Dry run        : {args.dry_run}")
    logger.info(f"Force Friday   : {args.force_friday}")

    # ââ Load strategy parameters âââââââââââââââââââââââââââââââââââââââââââ
    params = load_strategy_params()

    logger.info(f"\nStop parameters:")
    logger.info(f"  Initial stop   : Entry - {params['initial_stop_multiplier']:.1f} × ATR")
    logger.info(
        f"  Trailing stop  : Close - {params['trailing_stop_multiplier']:.1f} × ATR "
        f"(activates at +{params['trailing_activation_profit_pct']:.0f}% profit)"
    )
    logger.info(f"  Update freq    : {params['update_frequency']}")

    # ââ Load inputs ââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
    try:
        positions, pos_metadata = load_momentum_ranked(args.as_of_date)
    except SystemExit:
        return 1

    existing_states = load_portfolio_state(args.portfolio_state)

    new_count  = sum(1 for p in positions.values() if p.get("is_new_entry", True))
    hold_count = len(positions) - new_count
    logger.info(
        f"\nPositions loaded : {len(positions)}  "
        f"(new: {new_count}  |  hold: {hold_count})"
    )

    # ââ Calculate stops ââââââââââââââââââââââââââââââââââââââââââââââââââââ
    try:
        stops, summary = calculate_all_stops(
            positions=positions,
            existing_states=existing_states,
            as_of_date=args.as_of_date,
            update_day=update_day,
            params=params,
        )
    except Exception as exc:
        logger.error(f"Fatal error during stop calculation: {exc}", exc_info=True)
        return 1

    # ââ Console summary ââââââââââââââââââââââââââââââââââââââââââââââââââââ
    print_stop_summary(stops, summary)

    # ââ Save outputs âââââââââââââââââââââââââââââââââââââââââââââââââââââââ
    if args.dry_run:
        logger.info("\nâ   Dry-run mode – output files NOT written")
    else:
        date_tag = args.as_of_date.replace("-", "")
        try:
            save_stop_levels(
                stops,
                summary,
                PORTFOLIO_DIR / "stop_levels.json",
            )
            save_stop_summary(
                summary,
                PORTFOLIO_DIR / "stop_levels_summary.json",
            )
            save_stop_csv(
                stops,
                REPORTS_DIR / f"{date_tag}_stop_levels.csv",
            )
        except Exception as exc:
            logger.error(f"Error writing output files: {exc}", exc_info=True)
            return 1

    # ââ Timing & next-step hint ââââââââââââââââââââââââââââââââââââââââââââ
    elapsed = datetime.now() - start_time

    logger.info(f"\n{'=' * 70}")
    logger.info("STOP CALCULATION COMPLETE")
    logger.info(f"{'=' * 70}")
    logger.info(f"Duration              : {elapsed}")
    logger.info(f"Positions processed   : {summary['total_positions']}")
    logger.info(f"New stops set         : {summary['new_stops_set']}")
    logger.info(f"Trailing stops active : {summary['trailing_stops_active']}")
    logger.info(f"Trailing stops updated: {summary['trailing_stops_updated']}")
    logger.info(f"Initial stops held    : {summary['initial_stops_retained']}")
    if summary["error_count"] > 0:
        logger.warning(
            f"  â  {summary['error_count']} stop(s) could not be calculated: "
            f"{', '.join(summary['errors'])}"
        )
    if not args.dry_run:
        logger.info("Outputs:")
        logger.info(f"  - {PORTFOLIO_DIR / 'stop_levels.json'}")
        logger.info(f"  - {PORTFOLIO_DIR / 'stop_levels_summary.json'}")
        logger.info(
            f"  - {REPORTS_DIR / f'{date_tag}_stop_levels.csv'}"
        )
    logger.info(
        f"Next step : python scripts/09_calculate_position_sizes.py "
        f"--as-of-date {args.as_of_date}"
    )
    logger.info("=" * 70)

    # Partial failures (some symbols could not be calculated) are warnings only —
    # the output files are still valid and the pipeline can continue.
    # Only return 1 if ALL positions failed (nothing written for downstream scripts).
    total = summary["total_positions"]
    errors = summary["error_count"]
    if errors > 0 and errors < total:
        logger.warning(
            f"  Partial failure: {errors}/{total} stops could not be calculated. "
            "Pipeline continues — check logs for skipped symbols."
        )
        return 0
    elif errors > 0 and errors >= total:
        logger.error("All stop calculations failed. Aborting pipeline.")
        return 1
    return 0


def main() -> int:
    args = parse_arguments()
    logger.info("=" * 70)
    logger.info("STOP-LOSS CALCULATOR -- Script 8")
    logger.info("Architecture v3.9 (Mar 2026)")
    logger.info("=" * 70)

    try:
        strategies = resolve_strategies(args.strategy, project_root=PROJECT_ROOT)
    except (FileNotFoundError, ValueError) as exc:
        logger.error(f"Strategy resolution failed: {exc}")
        return 1

    logger.info(f"Strategies : {[s.name for s in strategies]}")
    start = __import__("datetime").datetime.now()
    failed = []
    for strategy in strategies:
        rc = _run_for_strategy(strategy, args)
        if rc != 0:
            failed.append(strategy.name)

    logger.info(f"Duration: {__import__('datetime').datetime.now() - start} | Failed: {failed or 'none'}")
    return 1 if failed else 0



if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        logger.warning("\n\nInterrupted by user")
        sys.exit(1)
    except Exception as exc:
        logger.error(f"Unhandled exception: {exc}", exc_info=True)
        sys.exit(1)
