#!/usr/bin/env python3
"""
Script 10: Exit Signal Generator
==================================
Identify all positions that meet a rule-based exit condition and output
a prioritised, machine-readable signal file consumed by Script 11.

Purpose:
    Systematically evaluate every current portfolio holding against four
    independent, hierarchically ordered exit rules.  No discretion, no
    overrides â the rules fire whenever their thresholds are breached.

    The script compares:
        â¢ STOP_LEVELS  (Script 9 output)  â effective stop price per position
        â¢ INDICATORS   (Script 5 output)  â SMA-50, SMA-200, ADX-14 per symbol
        â¢ MOMENTUM     (Script 7 output)  â current momentum rank per symbol
        â¢ PORTFOLIO    (runtime state)    â current holdings, entry prices

Exit Rules (evaluated in priority order):
âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
  PRIORITY 1 â STOP-LOSS HIT  (hard, immediate)
      Condition : close_price <= effective_stop_price
      Trigger   : ANY trading day (checked daily)
      Action    : Market order at NEXT session open
      Reason    : Capital preservation â non-negotiable

  PRIORITY 2 â TREND REVERSAL  (hard, immediate)
      Condition : SMA_50 < SMA_200  (death cross)
      Trigger   : ANY trading day
      Action    : Market order at NEXT session open
      Reason    : Uptrend has structurally reversed â primary trend lost

  PRIORITY 3 â TREND WEAKNESS  (soft, immediate)
      Condition : ADX_14 < 15 for â¥ 3 consecutive trading days
      Trigger   : ANY trading day
      Action    : Market order at NEXT session open
      Reason    : Trend momentum dissipated â no edge in holding

  PRIORITY 4 â REBALANCING ROTATION  (soft, month-end)
      Condition : Symbol NOT in top-N momentum-ranked universe
      Trigger   : Monthly rebalancing date only (requires --check-rotation)
      Action    : Market order at next session CLOSE (graceful exit)
      Reason    : Better risk-adjusted opportunity elsewhere

Exit priority semantics:
    Priority 1–3 exits are MANDATORY â they override any other consideration.
    Priority 4 exits are DISCRETIONARY at rebalancing â handled in Script 11
    unless --check-rotation is passed to pre-populate the exit signal file.

Data staleness guard:
    If the most-recent indicator bar is > MAX_DATA_STALENESS_DAYS old the
    symbol is flagged with reason "data_stale" and a WARNING is emitted.
    The stop check still fires if the stop price is available, but trend
    checks are suppressed to avoid acting on stale data.

Urgency classification:
    "immediate" → priority 1-3: execute at next session open
    "monthly"   → priority 4:   execute at next session close (rebalancing)
    "none"      → no exit triggered

âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

Dependencies (run in order before this script):
    01  02  03  04  05  06  07  08_calculate_stops.py  09_calculate_position_sizes.py  ← required input

Inputs:
    data/portfolio_state.json                       (current positions)
    data_cache/portfolio/stop_levels.json           (Script 8 output, required)
    data_cache/indicators/{symbol}_indicators.parquet  (Script 5 output, required)
    data_cache/signals/momentum_ranked.json         (Script 7 output, optional)
    config/strategy_parameters.json                 (optional parameter overrides)

Outputs:
    data_cache/signals/exit_signals.json            (primary â consumed by Script 11)
    data_cache/signals/exit_signals_summary.json    (run statistics)
    reports/signals/{YYYYMMDD}_exit_signals.csv     (human-readable audit trail)
    logs/exit_signals_{timestamp}.log

Execution:
    # Standard daily / rebalancing run
    python scripts/10_generate_exit_signals.py --as-of-date 2026-01-31

    # Include rebalancing rotation check (requires momentum_ranked.json)
    python scripts/10_generate_exit_signals.py --as-of-date 2026-01-31 \\
        --check-rotation --max-positions 20

    # Dry run â compute signals but write no output files
    python scripts/10_generate_exit_signals.py --as-of-date 2026-01-31 --dry-run

    # Override portfolio state file path
    python scripts/10_generate_exit_signals.py --as-of-date 2026-01-31 \\
        --portfolio-state data/portfolio_state_backup.json

Architecture: v3.2 (Feb 2026)
"""

import os
import sys
import json
import csv
import logging
import argparse
from pathlib import Path
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# ============================================================================
# PATH SETUP
# ============================================================================

PROJECT_ROOT   = Path(__file__).parent.parent
DATA_CACHE_DIR = PROJECT_ROOT / "data_cache"
SIGNALS_DIR    = DATA_CACHE_DIR / "signals"
INDICATORS_DIR = DATA_CACHE_DIR / "indicators"
PORTFOLIO_DIR  = DATA_CACHE_DIR / "portfolio"
DATA_DIR       = PROJECT_ROOT / "data"
REPORTS_DIR    = PROJECT_ROOT / "reports" / "signals"
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

# Exit Rule 1 â Stop-loss: no configurable parameters; uses stop_levels.json directly.

# Exit signal constants — sourced from config/strategy_parameters.json.
SMA_FAST_PERIOD:         int   = P.indicators.sma_fast
SMA_SLOW_PERIOD:         int   = P.indicators.sma_slow
ADX_PERIOD:              int   = P.indicators.adx_period
ADX_WEAKNESS_THRESHOLD:  float = P.trend_qualification.adx_weak
ADX_CONSECUTIVE_DAYS:    int   = P.trend_qualification.adx_weakness_days
MAX_DATA_STALENESS_DAYS: int   = P.circuit_breakers.cb_data_staleness_days

# ============================================================================
# LOGGING
# ============================================================================

def setup_logging() -> logging.Logger:
    """Configure dual-sink (file + stdout) logging."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file  = LOG_DIR / f"exit_signals_{timestamp}.log"

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


def data_staleness_days(df_filtered: pd.DataFrame, as_of_date: str) -> int:
    """
    Calculate how many calendar days old the most-recent indicator bar is.

    Args:
        df_filtered: Indicator DataFrame already filtered to â¤ as_of_date.
        as_of_date:  Reference date string "YYYY-MM-DD".

    Returns:
        Integer number of calendar days between latest bar and as_of_date.
        Returns 9999 if the dataframe is empty.
    """
    if df_filtered.empty:
        return 9999
    latest_bar_date = df_filtered.index[-1].date()
    ref_date        = pd.to_datetime(as_of_date).date()
    return (ref_date - latest_bar_date).days


# ============================================================================
# CONFIGURATION LOADER
# ============================================================================

def load_strategy_params() -> Dict:
    """
    Load exit-relevant parameters from config/strategy_parameters.json.

    Falls back to module-level constants if the file is absent or the
    relevant sections are missing.

    Returns:
        Dict with keys: adx_weakness_threshold, adx_consecutive_days,
                        sma_fast_period, sma_slow_period,
                        max_data_staleness_days.
    """
    defaults = {
        "adx_weakness_threshold":  ADX_WEAKNESS_THRESHOLD,
        "adx_consecutive_days":    ADX_CONSECUTIVE_DAYS,
        "sma_fast_period":         SMA_FAST_PERIOD,
        "sma_slow_period":         SMA_SLOW_PERIOD,
        "max_data_staleness_days": MAX_DATA_STALENESS_DAYS,
    }

    config_file = CONFIG_DIR / "strategy_parameters.json"
    if not config_file.exists():
        logger.info(
            "config/strategy_parameters.json not found â using built-in defaults."
        )
        return defaults

    try:
        with open(config_file, "r") as f:
            config = json.load(f)
    except json.JSONDecodeError as exc:
        logger.warning(f"Failed to parse strategy_parameters.json: {exc}. Using defaults.")
        return defaults

    indicators_cfg = config.get("indicators", {})
    circuit_cfg    = config.get("circuit_breakers", {})

    merged = {
        "adx_weakness_threshold":  defaults["adx_weakness_threshold"],
        "adx_consecutive_days":    defaults["adx_consecutive_days"],
        "sma_fast_period":         int(indicators_cfg.get("sma_fast", defaults["sma_fast_period"])),
        "sma_slow_period":         int(indicators_cfg.get("sma_slow", defaults["sma_slow_period"])),
        "max_data_staleness_days": int(circuit_cfg.get(
            "max_data_staleness_days", defaults["max_data_staleness_days"]
        )),
    }

    logger.info(
        f"Strategy params loaded: "
        f"SMA{merged['sma_fast_period']}/{merged['sma_slow_period']} | "
        f"ADX_weakness < {merged['adx_weakness_threshold']} "
        f"for {merged['adx_consecutive_days']} days | "
        f"max_staleness {merged['max_data_staleness_days']} days"
    )
    return merged


# ============================================================================
# DATA LOADERS
# ============================================================================

def load_portfolio_state(path: Optional[Path] = None) -> Dict:
    """
    Load current portfolio positions.

    Supports both flat format {"AAPL.US": {...}} and nested format
    {"positions": {"AAPL.US": {...}}}.

    Returns:
        Dict keyed by symbol.  Empty dict if file is absent (no positions).

    Raises:
        SystemExit if the file exists but cannot be parsed.
    """
    state_file = path or (DATA_DIR / "portfolio_state.json")

    if not state_file.exists():
        logger.warning(
            f"portfolio_state.json not found at {state_file}. "
            "Assuming no open positions â 0 exit signals will be generated."
        )
        return {}

    with open(state_file, "r") as f:
        try:
            state = json.load(f)
        except json.JSONDecodeError as exc:
            logger.error(f"Cannot parse portfolio_state.json: {exc}")
            sys.exit(1)

    positions = state.get("positions", state)

    # Guard: reject if every value is also a dict-of-dicts (likely wrong file)
    if not isinstance(positions, dict):
        logger.error(
            "portfolio_state.json has unexpected structure. "
            "Expected a dict keyed by symbol."
        )
        sys.exit(1)

    logger.info(f"â Loaded {len(positions)} positions from {state_file}")
    return positions


def load_stop_levels() -> Dict:
    """
    Load stop-loss levels produced by Script 9.

    Expected file: data_cache/portfolio/stop_levels.json

    Returns:
        Dict keyed by symbol.

    Raises:
        SystemExit if the file is missing (Script 9 has not been run).
    """
    stop_file = PORTFOLIO_DIR / "stop_levels.json"

    if not stop_file.exists():
        logger.error(
            f"stop_levels.json not found at {stop_file}.\n"
            "Please run Script 8 (08_calculate_stops.py) first."
        )
        sys.exit(1)

    with open(stop_file, "r") as f:
        data = json.load(f)

    stops = data.get("stops", data)  # Support both wrapped and flat formats

    if not stops:
        logger.error(
            "stop_levels.json is empty. "
            "Check that Script 8 completed successfully."
        )
        sys.exit(1)

    logger.info(f"â Loaded {len(stops)} stop records from {stop_file}")
    return stops


def load_indicators(symbol: str, as_of_date: str) -> Optional[pd.DataFrame]:
    """
    Load indicator time series for a symbol, filtered to â¤ as_of_date.

    File: data_cache/indicators/{symbol}_indicators.parquet

    Required columns (set by Script 5):
        close, sma_fast, sma_slow, adx

    Returns:
        Filtered DataFrame, or None if the file is missing or no rows match.
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


def load_momentum_ranked(as_of_date: str) -> Optional[List[Dict]]:
    """
    Load momentum-ranked universe produced by Script 7.

    File: data_cache/signals/momentum_ranked.json

    Returns:
        Ordered list of symbol dicts (rank 1 = highest momentum), or None
        if the file is absent.  A warning is emitted if the file's as_of_date
        does not match the requested date.
    """
    ranked_file = SIGNALS_DIR / "momentum_ranked.json"

    if not ranked_file.exists():
        logger.warning(
            "momentum_ranked.json not found. "
            "Rotation check (priority 4) will be skipped."
        )
        return None

    with open(ranked_file, "r") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as exc:
            logger.warning(f"Cannot parse momentum_ranked.json: {exc}. Skipping rotation check.")
            return None

    # Support both list format and {"ranked": [...]} wrapped format
    ranked = data if isinstance(data, list) else data.get("ranked", [])

    file_date = data.get("as_of_date", "") if isinstance(data, dict) else ""
    if file_date and file_date != as_of_date:
        logger.warning(
            f"momentum_ranked.json was generated for {file_date}, "
            f"but --as-of-date is {as_of_date}. "
            "Consider re-running Script 7 for the correct date."
        )

    logger.info(f"â Loaded {len(ranked)} ranked symbols from {ranked_file}")
    return ranked


def get_effective_stop_price(stop_record: Dict) -> Optional[float]:
    """
    Extract the single active stop price from a stop_levels record.

    Script 8 stores either `stop_price` (unified) or separate
    `initial_stop_price` / `trailing_stop_price` depending on the
    position state.  This function resolves the correct active price.

    Priority:
        1. `stop_price`          â present for all valid records from Script 8
        2. `trailing_stop_price` â fallback if trailing is active
        3. `initial_stop_price`  â last-resort fallback

    Returns:
        Active stop price as float, or None if the record is in error state.
    """
    if stop_record.get("stop_type") == "error":
        return None

    # Preferred: unified stop_price set by Script 9
    stop_price = stop_record.get("stop_price")
    if stop_price is not None:
        return float(stop_price)

    # Fallback for any custom / legacy format
    if stop_record.get("trailing_active") and stop_record.get("trailing_stop_price"):
        return float(stop_record["trailing_stop_price"])

    if stop_record.get("initial_stop_price"):
        return float(stop_record["initial_stop_price"])

    return None


# ============================================================================
# EXIT RULE CHECKERS
# ============================================================================

def check_stop_loss(
    symbol: str,
    close_price: float,
    stop_price: float,
) -> Optional[Dict]:
    """
    EXIT RULE 1 â STOP-LOSS HIT

    Condition : close_price <= stop_price
    Priority  : 1 (highest â checked first, supersedes all others)
    Urgency   : immediate
    Order     : market order at next session open

    Formula:
        EXIT  if  Close_t  â¤  Stop_Price_t

    Args:
        symbol:      Instrument ticker.
        close_price: Most-recent close on or before as_of_date.
        stop_price:  Effective stop price from stop_levels.json.

    Returns:
        Exit signal dict if condition met, else None.
    """
    if close_price <= stop_price:
        breach_pct = ((stop_price - close_price) / stop_price) * 100.0
        return {
            "exit":       True,
            "reason":     "stop_loss_hit",
            "priority":   1,
            "urgency":    "immediate",
            "exit_type":  "market_order_next_open",
            "close_price": round(close_price, 4),
            "stop_price":  round(stop_price, 4),
            "breach_pct":  round(breach_pct, 4),
            "detail": (
                f"Close {close_price:.4f} â¤ Stop {stop_price:.4f} "
                f"(breach: {breach_pct:.2f}%)"
            ),
        }
    return None


def check_trend_reversal(
    symbol:  str,
    sma_fast: float,
    sma_slow: float,
) -> Optional[Dict]:
    """
    EXIT RULE 2 â TREND REVERSAL (DEATH CROSS)

    Condition : SMA_50 < SMA_200 on the most-recent bar
    Priority  : 2
    Urgency   : immediate
    Order     : market order at next session open

    Rationale:
        The death cross is the canonical end-of-uptrend signal in
        medium-to-long-term trend following.  When the fast SMA falls
        below the slow SMA the primary trend assumption is invalidated
        and the position must be closed.

    Note: A single-day cross is sufficient to trigger.  We do NOT
    require the cross to persist for N days â that would introduce
    unnecessary lag and increase loss relative to the stop.

    Args:
        symbol:   Instrument ticker.
        sma_fast: SMA_50 value at as_of_date.
        sma_slow: SMA_200 value at as_of_date.

    Returns:
        Exit signal dict if condition met, else None.
    """
    if sma_fast < sma_slow:
        spread_pct = ((sma_slow - sma_fast) / sma_slow) * 100.0
        return {
            "exit":      True,
            "reason":    "trend_reversal",
            "priority":  2,
            "urgency":   "immediate",
            "exit_type": "market_order_next_open",
            "sma_fast":  round(sma_fast, 4),
            "sma_slow":  round(sma_slow, 4),
            "spread_pct": round(spread_pct, 4),
            "detail": (
                f"SMA{SMA_FAST_PERIOD} {sma_fast:.4f} < "
                f"SMA{SMA_SLOW_PERIOD} {sma_slow:.4f} "
                f"(death cross, spread {spread_pct:.2f}%)"
            ),
        }
    return None


def check_trend_weakness(
    symbol:          str,
    adx_series:      pd.Series,
    threshold:       float = ADX_WEAKNESS_THRESHOLD,
    consecutive_days: int  = ADX_CONSECUTIVE_DAYS,
) -> Optional[Dict]:
    """
    EXIT RULE 3 â TREND WEAKNESS (ADX COLLAPSE)

    Condition : ADX_14 < threshold for â¥ consecutive_days in a row
    Default   : ADX_14 < 15 for â¥ 3 consecutive trading days
    Priority  : 3
    Urgency   : immediate
    Order     : market order at next session open

    Rationale:
        ADX measures trend strength independent of direction.  An ADX
        reading below 15 signals that no meaningful trend exists and the
        instrument is in a ranging / choppy regime â an environment with
        no edge for a trend-following strategy.

        Requiring N consecutive bars prevents premature exits on a
        single-day noise spike.

    Formula:
        EXIT  if  ADX_t  < threshold  AND
                  ADX_{t-1} < threshold  AND
                  ADX_{t-2} < threshold

    Args:
        symbol:          Instrument ticker (for logging).
        adx_series:      ADX time series (tail used for consecutive check).
        threshold:       ADX value below which trend is considered weak.
        consecutive_days: Number of consecutive bars that must all be below threshold.

    Returns:
        Exit signal dict if condition met, else None.
    """
    if len(adx_series) < consecutive_days:
        return None  # Not enough history to confirm

    recent = adx_series.tail(consecutive_days)

    if (recent < threshold).all():
        adx_values = [round(v, 4) for v in recent.tolist()]
        avg_adx    = round(float(recent.mean()), 4)
        return {
            "exit":            True,
            "reason":          "trend_weakness",
            "priority":        3,
            "urgency":         "immediate",
            "exit_type":       "market_order_next_open",
            "adx_values":      adx_values,
            "adx_threshold":   threshold,
            "adx_avg":         avg_adx,
            "consecutive_days": consecutive_days,
            "detail": (
                f"ADX_14 < {threshold} for {consecutive_days} consecutive days: "
                f"{adx_values} (avg {avg_adx:.2f})"
            ),
        }
    return None


def check_rebalancing_rotation(
    symbol:        str,
    ranked_symbols: List[str],
    max_positions: int,
) -> Optional[Dict]:
    """
    EXIT RULE 4 â REBALANCING ROTATION (not in top-N momentum)

    Condition : Symbol is NOT in top-max_positions of momentum-ranked universe
    Priority  : 4 (lowest â soft exit at month-end)
    Urgency   : monthly
    Order     : market order at next session CLOSE (graceful, not stop-out)

    Rationale:
        At monthly rebalancing, capital should be deployed in the highest-
        momentum assets.  Any current position that has dropped out of
        the top-N should be rotated out and the capital redeployed.

        This is a SOFT exit â it does not override risk-management exits
        (priorities 1-3) and is only relevant at the rebalancing cycle.

    Args:
        symbol:         Instrument ticker.
        ranked_symbols: Ordered list of tickers by momentum rank (rank 1 = best).
        max_positions:  Maximum portfolio size (top-N threshold).

    Returns:
        Exit signal dict if condition met, else None.
    """
    top_n = ranked_symbols[:max_positions]

    if symbol not in top_n:
        rank = None
        if symbol in ranked_symbols:
            rank = ranked_symbols.index(symbol) + 1  # 1-based rank

        return {
            "exit":          True,
            "reason":        "rebalancing_rotation",
            "priority":      4,
            "urgency":       "monthly",
            "exit_type":     "market_order_next_close",
            "current_rank":  rank,
            "max_positions": max_positions,
            "detail": (
                f"Not in top-{max_positions} by momentum "
                f"(current rank: {rank if rank else 'unranked'})"
            ),
        }
    return None


# ============================================================================
# PER-POSITION EXIT ORCHESTRATOR
# ============================================================================

def evaluate_position_exits(
    symbol:         str,
    position:       Dict,
    stop_record:    Optional[Dict],
    df:             Optional[pd.DataFrame],
    as_of_date:     str,
    params:         Dict,
    ranked_symbols: Optional[List[str]] = None,
    max_positions:  Optional[int]       = None,
) -> Dict:
    """
    Evaluate all exit conditions for a single position.

    Checks are performed in priority order.  Once a higher-priority
    condition fires the lower-priority checks are still evaluated to
    provide full diagnostic information, but the reported primary
    exit reason is always the highest-priority rule that fired.

    Decision matrix (in execution order):
        1. Validate inputs (missing data, staleness)
        2. Check stop-loss (priority 1)
        3. Check trend reversal â death cross (priority 2)
        4. Check trend weakness â ADX collapse (priority 3)
        5. Check rotation (priority 4) â only if ranked_symbols provided
        6. Select the highest-priority fired rule as the primary signal

    Args:
        symbol:         Instrument ticker.
        position:       Position dict from portfolio_state.json.
        stop_record:    Stop-level record from stop_levels.json (or None).
        df:             Indicator DataFrame filtered to â¤ as_of_date.
        as_of_date:     Reference date string "YYYY-MM-DD".
        params:         Strategy parameters dict.
        ranked_symbols: Ordered list of momentum-ranked tickers (or None).
        max_positions:  Top-N threshold for rotation check (or None).

    Returns:
        Complete exit evaluation dict for this symbol.  Always contains
        at minimum: symbol, exit, reason, priority, urgency, timestamp.
    """
    base = {
        "symbol":       symbol,
        "exit":         False,
        "reason":       "holding",
        "priority":     None,
        "urgency":      "none",
        "exit_type":    None,
        "as_of_date":   as_of_date,
        "entry_price":  position.get("entry_price"),
        "warnings":     [],
        "rules_checked": [],
        "timestamp":    datetime.now().isoformat(),
    }

    # ââ Guard: stop_record missing or in error state âââââââââââââââââââââââ
    if stop_record is None:
        base["warnings"].append("stop_record_missing: Script 8 has no record for this symbol")
        logger.warning(f"  {symbol}: No stop record found â stop check skipped")
    elif stop_record.get("stop_type") == "error":
        base["warnings"].append(
            f"stop_record_error: {stop_record.get('error_reason', 'unknown')}"
        )
        logger.warning(
            f"  {symbol}: Stop record is in error state "
            f"({stop_record.get('error_reason')}) â stop check skipped"
        )

    # ââ Guard: indicator data missing âââââââââââââââââââââââââââââââââââââ
    if df is None:
        base["warnings"].append("indicator_data_missing: no parquet file found")
        logger.warning(f"  {symbol}: No indicator data â trend checks skipped")

    # ââ Guard: data staleness ââââââââââââââââââââââââââââââââââââââââââââââ
    stale_days       = data_staleness_days(df, as_of_date) if df is not None else 9999
    max_stale        = params["max_data_staleness_days"]
    data_is_stale    = stale_days > max_stale
    base["data_staleness_days"] = stale_days

    if data_is_stale:
        base["warnings"].append(
            f"data_stale: latest bar is {stale_days} days old "
            f"(threshold: {max_stale} days)"
        )
        logger.warning(
            f"  {symbol}: Data is {stale_days} days old "
            f"(threshold: {max_stale}) â trend checks suppressed"
        )

    # ââ Collect all triggered rules ââââââââââââââââââââââââââââââââââââââââ
    triggered: List[Dict] = []

    # ââ RULE 1: Stop-loss hit ââââââââââââââââââââââââââââââââââââââââââââââ
    stop_price = (
        get_effective_stop_price(stop_record)
        if stop_record and stop_record.get("stop_type") != "error"
        else None
    )
    base["stop_price"]      = stop_price
    base["stop_type"]       = stop_record.get("stop_type") if stop_record else None
    base["trailing_active"] = stop_record.get("trailing_active", False) if stop_record else False

    if stop_price is not None and df is not None and not df.empty:
        latest_close = float(df["close"].iloc[-1])
        base["close_price"] = round(latest_close, 4)
        base["rules_checked"].append("stop_loss")

        signal = check_stop_loss(symbol, latest_close, stop_price)
        if signal:
            triggered.append(signal)
            logger.info(
                f"  {symbol}: â  STOP-LOSS HIT â "
                f"close {latest_close:.4f} â¤ stop {stop_price:.4f}"
            )

    elif df is not None and not df.empty:
        base["close_price"] = round(float(df["close"].iloc[-1]), 4)

    # ââ RULE 2: Trend reversal (death cross) ââââââââââââââââââââââââââââââ
    if df is not None and not df.empty and not data_is_stale:
        sma_col_fast = "sma_fast"
        sma_col_slow = "sma_slow"

        has_sma = (sma_col_fast in df.columns) and (sma_col_slow in df.columns)

        if has_sma:
            latest_sma_fast = df[sma_col_fast].iloc[-1]
            latest_sma_slow = df[sma_col_slow].iloc[-1]

            if pd.notna(latest_sma_fast) and pd.notna(latest_sma_slow):
                base["sma_fast"]     = round(float(latest_sma_fast), 4)
                base["sma_slow"]     = round(float(latest_sma_slow), 4)
                base["sma_spread_pct"] = round(
                    (float(latest_sma_fast) - float(latest_sma_slow))
                    / float(latest_sma_slow) * 100, 4
                )
                base["rules_checked"].append("trend_reversal")

                signal = check_trend_reversal(
                    symbol,
                    float(latest_sma_fast),
                    float(latest_sma_slow),
                )
                if signal:
                    triggered.append(signal)
                    logger.info(
                        f"  {symbol}: â  DEATH CROSS â "
                        f"SMA50 {latest_sma_fast:.2f} < SMA200 {latest_sma_slow:.2f}"
                    )
            else:
                base["warnings"].append("sma_values_nan: SMA_50 or SMA_200 is NaN")
        else:
            base["warnings"].append(
                f"sma_columns_missing: expected {sma_col_fast}, {sma_col_slow}"
            )

    # ââ RULE 3: Trend weakness (ADX collapse) âââââââââââââââââââââââââââââ
    if df is not None and not df.empty and not data_is_stale:
        adx_col = "adx"

        if adx_col in df.columns:
            adx_series = df[adx_col].dropna()

            if not adx_series.empty:
                base["adx_latest"] = round(float(adx_series.iloc[-1]), 4)
                base["rules_checked"].append("trend_weakness")

                signal = check_trend_weakness(
                    symbol,
                    adx_series,
                    threshold=params["adx_weakness_threshold"],
                    consecutive_days=params["adx_consecutive_days"],
                )
                if signal:
                    triggered.append(signal)
                    logger.info(
                        f"  {symbol}: â  ADX COLLAPSE â "
                        f"ADX {base['adx_latest']:.2f} < {params['adx_weakness_threshold']} "
                        f"for {params['adx_consecutive_days']} days"
                    )
            else:
                base["warnings"].append("adx_series_empty: ADX_14 column has no valid values")
        else:
            base["warnings"].append(f"adx_column_missing: expected {adx_col}")

    # ââ RULE 4: Rebalancing rotation ââââââââââââââââââââââââââââââââââââââ
    if ranked_symbols is not None and max_positions is not None:
        base["rules_checked"].append("rebalancing_rotation")

        signal = check_rebalancing_rotation(symbol, ranked_symbols, max_positions)
        if signal:
            triggered.append(signal)
            logger.info(
                f"  {symbol}: â ROTATION â not in top-{max_positions} by momentum"
            )

    # ââ Select primary signal (lowest priority number = highest priority) ââ
    if triggered:
        # Sort by priority (ascending) → index 0 is highest priority
        triggered_sorted = sorted(triggered, key=lambda x: x["priority"])
        primary = triggered_sorted[0]

        base.update(primary)  # Overwrite exit/reason/priority/urgency from primary
        base["all_triggered_rules"] = [t["reason"] for t in triggered_sorted]

        if len(triggered_sorted) > 1:
            base["secondary_signals"] = triggered_sorted[1:]
            logger.info(
                f"  {symbol}: Multiple rules fired: "
                f"{[t['reason'] for t in triggered_sorted]}"
            )
    else:
        base["all_triggered_rules"] = []
        base["secondary_signals"]   = []

    return base


# ============================================================================
# PORTFOLIO-LEVEL ORCHESTRATOR
# ============================================================================

def generate_all_exit_signals(
    current_positions: Dict,
    stop_levels:       Dict,
    as_of_date:        str,
    params:            Dict,
    ranked_symbols:    Optional[List[str]] = None,
    max_positions:     Optional[int]       = None,
) -> Tuple[Dict, Dict]:
    """
    Evaluate exit conditions for every current portfolio position.

    Iterates all positions, delegates per-position logic to
    evaluate_position_exits(), and assembles both the full signals dict
    and a run-level summary.

    Args:
        current_positions: Dict keyed by symbol from portfolio_state.json.
        stop_levels:       Dict keyed by symbol from stop_levels.json.
        as_of_date:        Reference date string "YYYY-MM-DD".
        params:            Strategy parameters dict.
        ranked_symbols:    Ordered list for rotation check (or None).
        max_positions:     Top-N threshold for rotation check (or None).

    Returns:
        Tuple of:
            signals_dict â { symbol: evaluation_record, ... } (all positions)
            summary      â run-level statistics dict
    """
    signals: Dict = {}

    # Counters for the run summary
    stop_exits        = 0
    reversal_exits    = 0
    weakness_exits    = 0
    rotation_exits    = 0
    data_stale_warns  = 0
    skipped_errors    = 0
    holding_count     = 0
    multi_rule_count  = 0

    check_rotation = ranked_symbols is not None and max_positions is not None

    logger.info(f"\nEvaluating exit conditions for {len(current_positions)} positions ...")
    logger.info(f"  ADX weakness threshold : < {params['adx_weakness_threshold']}")
    logger.info(f"  ADX consecutive days   : {params['adx_consecutive_days']}")
    logger.info(f"  Data staleness limit   : {params['max_data_staleness_days']} days")
    logger.info(f"  Rotation check active  : {check_rotation}")
    if check_rotation:
        logger.info(f"  Max positions (top-N)  : {max_positions}")

    for symbol, position in current_positions.items():
        stop_record = stop_levels.get(symbol)
        df          = load_indicators(symbol, as_of_date)

        result = evaluate_position_exits(
            symbol=symbol,
            position=position,
            stop_record=stop_record,
            df=df,
            as_of_date=as_of_date,
            params=params,
            ranked_symbols=ranked_symbols,
            max_positions=max_positions,
        )

        signals[symbol] = result

        # Tally statistics
        if result.get("data_staleness_days", 0) > params["max_data_staleness_days"]:
            data_stale_warns += 1

        if result.get("exit"):
            reason = result.get("reason", "")
            if reason == "stop_loss_hit":
                stop_exits += 1
            elif reason == "trend_reversal":
                reversal_exits += 1
            elif reason == "trend_weakness":
                weakness_exits += 1
            elif reason == "rebalancing_rotation":
                rotation_exits += 1

            if len(result.get("all_triggered_rules", [])) > 1:
                multi_rule_count += 1

        elif result.get("stop_type") == "error":
            skipped_errors += 1
        else:
            holding_count += 1

        # Single-line console output per position
        exit_flag   = "ð´ EXIT" if result.get("exit") else "ð¢ HOLD"
        reason_str  = result.get("reason", "holding")
        prio_str    = f"P{result.get('priority')}" if result.get("priority") else "P-"
        stale_flag  = " â STALE" if result.get("data_staleness_days", 0) > params["max_data_staleness_days"] else ""
        close_str   = (
            f"close={result.get('close_price', 'N/A'):>10}"
            if result.get("close_price") is not None
            else "close=         N/A"
        )
        stop_str    = (
            f"stop={result.get('stop_price', 'N/A'):>10}"
            if result.get("stop_price") is not None
            else "stop=          N/A"
        )

        logger.info(
            f"  {symbol:<16} {exit_flag} [{prio_str}] "
            f"{reason_str:<22} {close_str}  {stop_str}{stale_flag}"
        )

    total_exits     = stop_exits + reversal_exits + weakness_exits + rotation_exits
    mandatory_exits = stop_exits + reversal_exits + weakness_exits

    summary = {
        "as_of_date":           as_of_date,
        "generated_at":         datetime.now().isoformat(),
        "total_positions":      len(current_positions),
        "total_exits":          total_exits,
        "mandatory_exits":      mandatory_exits,
        "holding_count":        holding_count,

        # Breakdown by exit reason
        "stop_loss_exits":      stop_exits,
        "trend_reversal_exits": reversal_exits,
        "trend_weakness_exits": weakness_exits,
        "rotation_exits":       rotation_exits,

        # Quality flags
        "data_stale_warnings":  data_stale_warns,
        "skipped_errors":       skipped_errors,
        "multi_rule_exits":     multi_rule_count,
        "rotation_check_used":  check_rotation,
        "max_positions":        max_positions,

        "strategy_params": {
            "adx_weakness_threshold":  params["adx_weakness_threshold"],
            "adx_consecutive_days":    params["adx_consecutive_days"],
            "sma_fast_period":         params["sma_fast_period"],
            "sma_slow_period":         params["sma_slow_period"],
            "max_data_staleness_days": params["max_data_staleness_days"],
        },
    }

    return signals, summary


# ============================================================================
# OUTPUT FUNCTIONS
# ============================================================================

def save_exit_signals(
    signals:     Dict,
    summary:     Dict,
    output_file: Path,
) -> None:
    """
    Save complete exit signal evaluation to JSON.

    Format:
        {
            "metadata": { ...run statistics... },
            "signals": {
                "AAPL.US": { exit, reason, priority, urgency, ... },
                ...
            }
        }

    This is the primary output consumed by Script 11 (monthly rebalancer).
    Script 11 reads signals['symbol']['exit'] and ['priority'] to determine
    mandatory vs rotation exits.

    Args:
        signals:     Dict of per-symbol evaluation records.
        summary:     Run-level statistics dict.
        output_file: Destination path.
    """
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)

    output = {
        "metadata": summary,
        "signals":  signals,
    }

    with open(output_file, "w") as f:
        json.dump(output, f, indent=2, default=str)

    logger.info(f"â Saved {len(signals)} exit evaluations → {output_file}")


def save_exit_summary(summary: Dict, output_file: Path) -> None:
    """Save run-level statistics to a separate JSON file."""
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)

    with open(output_file, "w") as f:
        json.dump(summary, f, indent=2, default=str)

    logger.info(f"â Saved exit summary → {output_file}")


def save_exit_csv(signals: Dict, output_file: Path) -> None:
    """
    Save human-readable CSV for manual review and audit trail.

    Columns: symbol, exit, reason, priority, urgency, exit_type,
             close_price, stop_price, sma_fast, sma_slow, adx_latest,
             stop_type, trailing_active, warnings, detail.
    """
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    COLUMNS = [
        "symbol", "exit", "reason", "priority", "urgency", "exit_type",
        "close_price", "stop_price", "stop_type", "trailing_active",
        "sma_fast", "sma_slow", "sma_spread_pct", "adx_latest",
        "entry_price", "data_staleness_days", "all_triggered_rules",
        "warnings", "detail", "as_of_date",
    ]

    rows = []
    for symbol, rec in signals.items():
        row = {}
        for col in COLUMNS:
            val = rec.get(col, "")
            if isinstance(val, list):
                val = "; ".join(str(v) for v in val)
            row[col] = val
        rows.append(row)

    # Sort: exits first (by priority), then holds alphabetically
    rows.sort(key=lambda r: (
        not r.get("exit", False),   # exits come first (False < True numerically)
        r.get("priority") or 99,    # lower priority number = higher urgency
        str(r.get("symbol", ""))
    ))

    with open(output_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    logger.info(f"â Saved exit signals CSV → {output_file}")


def print_exit_summary(signals: Dict, summary: Dict) -> None:
    """
    Print a formatted terminal summary of exit signals.

    Format mirrors the stop-levels summary in Script 9 for visual consistency.
    """
    total          = summary["total_positions"]
    total_exits    = summary["total_exits"]
    mandatory      = summary["mandatory_exits"]
    rotation       = summary["rotation_exits"]
    holding        = summary["holding_count"]
    stale_warns    = summary["data_stale_warnings"]
    skip_errors    = summary["skipped_errors"]

    print("\n" + "=" * 72)
    print("  EXIT SIGNAL SUMMARY")
    print("=" * 72)
    print(f"  As-of date             : {summary['as_of_date']}")
    print(f"  Positions evaluated    : {total}")
    print(f"  Total exit signals     : {total_exits}  "
          f"(mandatory: {mandatory}  |  rotation: {rotation})")
    print(f"  Holding (no exit)      : {holding}")
    print()

    if summary["stop_loss_exits"] > 0:
        print(f"  â Stop-loss exits     : {summary['stop_loss_exits']}  [P1] immediate â market order next open")
    if summary["trend_reversal_exits"] > 0:
        print(f"  ð Death-cross exits   : {summary['trend_reversal_exits']}  [P2] immediate â market order next open")
    if summary["trend_weakness_exits"] > 0:
        print(f"  ð ADX-collapse exits  : {summary['trend_weakness_exits']}  [P3] immediate â market order next open")
    if rotation > 0:
        print(f"  ð Rotation exits      : {rotation}  [P4] monthly   â market order next close")
    if stale_warns > 0:
        print(f"  â   Stale-data warnings : {stale_warns}  (trend checks suppressed, stop checks retained)")
    if skip_errors > 0:
        print(f"  â Skipped (errors)    : {skip_errors}  (missing/errored stop record)")
    print()

    if summary["multi_rule_exits"] > 0:
        print(
            f"  â¹  {summary['multi_rule_exits']} position(s) triggered multiple rules â "
            "highest-priority rule reported"
        )
        print()

    # Enumerate mandatory exits explicitly (operators need these)
    mandatory_list = [
        (sym, rec) for sym, rec in signals.items()
        if rec.get("exit") and rec.get("priority", 99) <= 3
    ]
    if mandatory_list:
        print("  MANDATORY EXITS (execute at next open):")
        for sym, rec in sorted(mandatory_list, key=lambda x: x[1].get("priority", 99)):
            print(
                f"    {sym:<16}  [{rec['reason']:<22}]  "
                f"close={rec.get('close_price', 'N/A')}  "
                f"stop={rec.get('stop_price', 'N/A')}"
            )
        print()

    # Enumerate rotation exits
    rotation_list = [
        (sym, rec) for sym, rec in signals.items()
        if rec.get("exit") and rec.get("reason") == "rebalancing_rotation"
    ]
    if rotation_list:
        print(f"  ROTATION EXITS (execute at month-end close):")
        for sym, rec in sorted(rotation_list, key=lambda x: x[0]):
            print(f"    {sym:<16}  rank={rec.get('current_rank', 'N/A')}")
        print()

    print("=" * 72)


# ============================================================================
# CLI
# ============================================================================

def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="10_generate_exit_signals.py",
        description="""
Exit Signal Generator â Script 10 of the Multi-Asset Trend Following Strategy.

Evaluates every current portfolio position against four rule-based exit
conditions and outputs a prioritised JSON signal file for Script 11.

Exit Rules (in order of priority):
  [P1] Stop-loss hit         â Close â¤ stop_price  →  immediate exit
  [P2] Trend reversal        â SMA_50 < SMA_200    →  immediate exit
  [P3] Trend weakness        â ADX < 15 for 3 days →  immediate exit
  [P4] Rebalancing rotation  â Not in top-N        →  monthly exit

        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--as-of-date",
        required=True,
        metavar="YYYY-MM-DD",
        help=(
            "Reference date for exit evaluation. "
            "Use the last trading day of the month for rebalancing runs."
        ),
    )
    parser.add_argument(
        "--portfolio-state",
        type=Path,
        default=None,
        metavar="PATH",
        help="Path to portfolio_state.json (default: data/portfolio_state.json)",
    )
    parser.add_argument(
        "--check-rotation",
        action="store_true",
        help=(
            "Include rebalancing rotation check (priority 4). "
            "Requires momentum_ranked.json and --max-positions."
        ),
    )
    parser.add_argument(
        "--max-positions",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Maximum portfolio size for rotation check (top-N threshold). "
            "Required when --check-rotation is set."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute exit signals but do not write any output files.",
    )

    add_strategy_argument(parser)
    return parser.parse_args()


# ============================================================================
# MAIN
# ============================================================================

def _run_for_strategy(strategy: "StrategyDef", args) -> int:
    """Run for one strategy with namespaced I/O paths."""
    global SIGNALS_DIR, PORTFOLIO_DIR, REPORTS_DIR

    strat_signals   = strategy.signals_dir(DATA_CACHE_DIR)
    strat_portfolio = strategy.portfolio_dir(DATA_CACHE_DIR)
    strat_reports   = strategy.reports_dir(PROJECT_ROOT, "signals")
    strat_signals.mkdir(parents=True, exist_ok=True)
    strat_portfolio.mkdir(parents=True, exist_ok=True)
    strat_reports.mkdir(parents=True, exist_ok=True)

    logger.info(f"\n[{strategy.name}] -- {strategy.label} ({'LIVE' if strategy.deployed else 'PAPER'}) --")
    logger.info(f"[{strategy.name}] Signals   : {strat_signals}")
    logger.info(f"[{strategy.name}] Portfolio : {strat_portfolio}")

    _orig = (SIGNALS_DIR, PORTFOLIO_DIR, REPORTS_DIR)
    SIGNALS_DIR   = strat_signals
    PORTFOLIO_DIR = strat_portfolio
    REPORTS_DIR   = strat_reports
    try:
        return _run_core(args, strategy.name)
    finally:
        SIGNALS_DIR, PORTFOLIO_DIR, REPORTS_DIR = _orig


def _run_core(args, strategy_name: str = '') -> int:
    start_time = datetime.now()

    logger.info("=" * 70)
    logger.info("EXIT SIGNAL GENERATOR â Script 10")
    logger.info("Architecture v3.2 (Feb 2026)")
    logger.info("=" * 70)

    # ââ Parse & validate arguments ââââââââââââââââââââââââââââââââââââââââââ

    try:
        validate_date(args.as_of_date)
    except ValueError:
        logger.error(
            f"Invalid date format: '{args.as_of_date}'.  Expected YYYY-MM-DD."
        )
        return 1

    # Validate rotation arguments
    if args.check_rotation and args.max_positions is None:
        logger.error(
            "--check-rotation requires --max-positions N to be specified."
        )
        return 1

    if args.max_positions is not None and args.max_positions < 1:
        logger.error(
            f"--max-positions must be â¥ 1, got {args.max_positions}."
        )
        return 1

    logger.info(f"\nAs-of date        : {args.as_of_date}")
    logger.info(f"Day of week       : {pd.to_datetime(args.as_of_date).day_name()}")
    logger.info(f"Check rotation    : {args.check_rotation}")
    logger.info(f"Max positions     : {args.max_positions}")
    logger.info(f"Dry run           : {args.dry_run}")

    # ââ Load strategy parameters âââââââââââââââââââââââââââââââââââââââââââââ
    params = load_strategy_params()

    # ââ Load required inputs âââââââââââââââââââââââââââââââââââââââââââââââââ
    current_positions = load_portfolio_state(args.portfolio_state)

    if not current_positions:
        logger.warning("No current positions found â no exit signals to generate.")
        # Write empty output files (so downstream scripts don't fail on missing files)
        empty_signals  = {}
        empty_summary  = {
            "as_of_date": args.as_of_date,
            "generated_at": datetime.now().isoformat(),
            "total_positions": 0,
            "total_exits": 0,
            "mandatory_exits": 0,
            "holding_count": 0,
            "stop_loss_exits": 0,
            "trend_reversal_exits": 0,
            "trend_weakness_exits": 0,
            "rotation_exits": 0,
            "data_stale_warnings": 0,
            "skipped_errors": 0,
            "multi_rule_exits": 0,
            "rotation_check_used": args.check_rotation,
            "max_positions": args.max_positions,
            "note": "No open positions in portfolio_state.json",
        }

        if not args.dry_run:
            date_tag = args.as_of_date.replace("-", "")
            save_exit_signals(empty_signals, empty_summary, SIGNALS_DIR / "exit_signals.json")
            save_exit_summary(empty_summary, SIGNALS_DIR / "exit_signals_summary.json")
            save_exit_csv(empty_signals, REPORTS_DIR / f"{date_tag}_exit_signals.csv")

        return 0

    stop_levels = load_stop_levels()

    # ââ Optionally load momentum ranking ââââââââââââââââââââââââââââââââââââ
    ranked_symbols: Optional[List[str]] = None
    if args.check_rotation:
        ranked_data = load_momentum_ranked(args.as_of_date)
        if ranked_data is None:
            logger.error(
                "Rotation check requested but momentum_ranked.json is unavailable. "
                "Run Script 7 (07_rank_momentum.py) first, or omit --check-rotation."
            )
            return 1
        # Extract ordered symbol list from ranked_data entries
        ranked_symbols = [
            entry["symbol"] for entry in ranked_data if "symbol" in entry
        ]
        if not ranked_symbols:
            logger.error(
                "momentum_ranked.json loaded but no 'symbol' keys found in entries. "
                "Check Script 7 output format."
            )
            return 1
        logger.info(
            f"Rotation check active: {len(ranked_symbols)} symbols ranked, "
            f"top-{args.max_positions} threshold"
        )

    logger.info(
        f"\nInputs loaded: "
        f"{len(current_positions)} positions | "
        f"{len(stop_levels)} stop records | "
        f"ranked_symbols={'yes' if ranked_symbols else 'no'}"
    )

    # ââ Generate exit signals ââââââââââââââââââââââââââââââââââââââââââââââââ
    signals, summary = generate_all_exit_signals(
        current_positions=current_positions,
        stop_levels=stop_levels,
        as_of_date=args.as_of_date,
        params=params,
        ranked_symbols=ranked_symbols,
        max_positions=args.max_positions,
    )

    # ââ Console summary ââââââââââââââââââââââââââââââââââââââââââââââââââââââ
    print_exit_summary(signals, summary)

    # ââ Save outputs âââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
    if args.dry_run:
        logger.info("\nâ   Dry-run mode â output files NOT written")
    else:
        date_tag = args.as_of_date.replace("-", "")
        try:
            save_exit_signals(
                signals,
                summary,
                SIGNALS_DIR / "exit_signals.json",
            )
            save_exit_summary(
                summary,
                SIGNALS_DIR / "exit_signals_summary.json",
            )
            save_exit_csv(
                signals,
                REPORTS_DIR / f"{date_tag}_exit_signals.csv",
            )
        except Exception as exc:
            logger.error(f"Error writing output files: {exc}", exc_info=True)
            return 1

    # ââ Timing & next-step hint ââââââââââââââââââââââââââââââââââââââââââââââ
    elapsed        = datetime.now() - start_time
    total_exits    = summary["total_exits"]
    mandatory_exits = summary["mandatory_exits"]

    logger.info(f"\n{'=' * 70}")
    logger.info("EXIT SIGNAL GENERATION COMPLETE")
    logger.info(f"{'=' * 70}")
    logger.info(f"Duration              : {elapsed}")
    logger.info(f"Positions evaluated   : {summary['total_positions']}")
    logger.info(f"Exit signals          : {total_exits}  "
                f"(mandatory: {mandatory_exits} | rotation: {summary['rotation_exits']})")
    logger.info(f"  Stop-loss exits     : {summary['stop_loss_exits']}")
    logger.info(f"  Death-cross exits   : {summary['trend_reversal_exits']}")
    logger.info(f"  ADX-collapse exits  : {summary['trend_weakness_exits']}")
    logger.info(f"  Rotation exits      : {summary['rotation_exits']}")
    logger.info(f"Holding (no exit)     : {summary['holding_count']}")
    if summary["data_stale_warnings"] > 0:
        logger.warning(
            f"  â   {summary['data_stale_warnings']} stale-data warning(s) â "
            "verify data freshness before acting"
        )
    if summary["skipped_errors"] > 0:
        logger.warning(
            f"  â   {summary['skipped_errors']} position(s) skipped due to "
            "missing/errored stop records"
        )
    if not args.dry_run:
        logger.info("Outputs:")
        logger.info(f"  - {SIGNALS_DIR / 'exit_signals.json'}")
        logger.info(f"  - {SIGNALS_DIR / 'exit_signals_summary.json'}")
        logger.info(f"  - {REPORTS_DIR / f'{date_tag}_exit_signals.csv'}")
    logger.info(
        f"Next step: python scripts/11_monthly_rebalancing.py "
        f"--as-of-date {args.as_of_date}"
    )
    logger.info("=" * 70)

    # Return 1 if any mandatory exit exists (useful for CI / alerting systems)
    return 1 if mandatory_exits > 0 else 0


def main() -> int:
    args = parse_arguments()
    logger.info("=" * 70)
    logger.info("EXIT SIGNAL GENERATOR -- Script 10")
    logger.info("Architecture v3.9 (Mar 2026)")
    logger.info("=" * 70)

    try:
        strategies = resolve_strategies(getattr(args, "strategy", None), project_root=PROJECT_ROOT)
    except (FileNotFoundError, ValueError) as exc:
        logger.error(f"Strategy resolution failed: {exc}")
        return 1

    logger.info(f"Strategies : {[s.name for s in strategies]}")
    from datetime import datetime as _dt
    _start = _dt.now()
    failed = []
    for strategy in strategies:
        rc = _run_for_strategy(strategy, args)
        if rc != 0:
            failed.append(strategy.name)

    logger.info(f"Duration: {_dt.now() - _start} | Strategies: {len(strategies)} | Failed: {failed or 'none'}")
    logger.info(f"Next step: python scripts/11_monthly_rebalancing.py")
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
