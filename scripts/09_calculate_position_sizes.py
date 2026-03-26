#!/usr/bin/env python3
"""
Script 9: Position Sizer
========================
Calculate volatility-adjusted position sizes for top-N momentum instruments.

Purpose:
    Receives the ranked instrument list from Script 7 and computes a
    precise, rules-based position size for each candidate entry using
    inverse-ATR volatility scaling.  All formula steps are explicit,
    reproducible, and implementation-ready for the downstream scripts:

        Script 10 (10_generate_exit_signals.py)  ÃÂ¢Ã¢ÂÂ Ã¢ÂÂ reads position_sizes.json
        Script 11 (11_monthly_rebalancing.py) ÃÂ¢Ã¢ÂÂ Ã¢ÂÂ reads position_sizes.json

Position Sizing Formula (v3.2 â production specification):

    Step 1  Base Risk
            Base_Risk = Account_Equity ÃÂâ Target_Risk_Per_Position
            Target_Risk_Per_Position = 2.0% (constant)

    Step 2  Volatility Adjustment
            Volatility_Multiplier = Median_ATR_Pct / Instrument_ATR_Pct
            ÃÂ¢Ã¢ÂÂ Ã¢ÂÂ lower-volatility instruments receive proportionally larger
              allocations; higher-volatility instruments receive smaller ones.

    Step 3  Raw Position Value
            Raw_Position_Value = Base_Risk ÃÂâ Volatility_Multiplier

    Step 4  Floor / Ceiling Clip
            Final_Position_Value = CLIP(
                Raw_Position_Value,
                min = MIN_POSITION_PCT ÃÂâ Account_Equity,   # 0.5%
                max = MAX_POSITION_PCT ÃÂâ Account_Equity    # 8.0%
            )

    Step 5  Whole-Share Conversion
            Shares             = floor(Final_Position_Value / Entry_Price)
            Actual_Value       = Shares ÃÂâ Entry_Price

Portfolio Constraints Applied (in order):
    1. Crypto allocation cap  ÃÂ¢Ã¢ÂÂ°ÃÂ¤ 20% of equity
    2. Single-sector cap      ÃÂ¢Ã¢ÂÂ°ÃÂ¤ 30% of equity  (warning only)
    3. Cash reserve floor     ÃÂ¢Ã¢ÂÂ°ÃÂ¥  5% of equity  (warning only)
    4. Top-3 concentration    ÃÂ¢Ã¢ÂÂ°ÃÂ¤ 30% of equity  (warning only, circuit-breaker territory)
    5. Minimum position threshold: discard symbols that round to 0 shares

Dependencies (run before this script):
    01_download_eodhd_bulk.py
    02_download_yahoo_fundamentals.py
    03_consolidate_validate_data.py
    04_screen_universe.py
    05_calculate_indicators.py
    06_qualify_trends.py
    07_rank_momentum.py
    08_calculate_stops.py   â THIS SCRIPT'S PRIMARY INPUT

Inputs:
    - data_cache/portfolio/stop_levels.json     (from Script 8, required)
    - data_cache/signals/momentum_ranked.json   (from Script 7)
    - data/portfolio_state.json                 (optional, labels new vs existing)

Outputs:
    - data_cache/portfolio/position_sizes.json      (primary â consumed by Script 9 & 11)
    - data_cache/portfolio/sizing_summary.json      (run statistics)
    - reports/portfolio/{YYYYMMDD}_position_sizes.csv  (human-readable)

Execution:
    # Minimal â equity and date required; max-positions auto-computed from equity
    python scripts/09_calculate_position_sizes.py \\
        --account-equity 50000 \\
        --as-of-date 2026-01-31

    # Explicit position cap
    python scripts/09_calculate_position_sizes.py \\
        --account-equity 50000 \\
        --as-of-date 2026-01-31 \\
        --max-positions 20

    # Dry run (compute only, no files written)
    python scripts/09_calculate_position_sizes.py \\
        --account-equity 50000 \\
        --as-of-date 2026-01-31 \\
        --dry-run

Architecture: v3.2 (Feb 2026)
"""

import os
import sys
import json
import math
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

PROJECT_ROOT = Path(__file__).parent.parent
DATA_CACHE_DIR = PROJECT_ROOT / "data_cache"
SIGNALS_DIR    = DATA_CACHE_DIR / "signals"
PORTFOLIO_DIR  = DATA_CACHE_DIR / "portfolio"
DATA_DIR       = PROJECT_ROOT / "data"
REPORTS_DIR    = PROJECT_ROOT / "reports" / "portfolio"
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

# Position sizing constants — sourced from config/strategy_parameters.json.
TARGET_RISK_PER_POSITION:   float = P.position_sizing.risk_per_trade
MIN_POSITION_PCT:           float = P.position_sizing.pos_floor_pct
MAX_POSITION_PCT:           float = P.position_sizing.pos_ceil_pct
ENTRY_LIMIT_OFFSET:         float = P.position_sizing.entry_limit_offset
MAX_CRYPTO_ALLOCATION_PCT:  float = P.portfolio_constraints.max_crypto_pct
MAX_SECTOR_ALLOCATION_PCT:  float = P.portfolio_constraints.max_sector_pct
MIN_CASH_RESERVE_PCT:       float = P.portfolio_constraints.min_cash_pct
MAX_TOP3_CONCENTRATION_PCT: float = P.portfolio_constraints.max_top3_concentration_pct
MAX_STOP_DISTANCE_PCT:      float = P.stops.max_stop_distance_pct

# Position count schedule — sourced from config/strategy_parameters.json.
# Use P.position_sizing.max_positions_for_equity(equity) at call sites.

# ============================================================================
# LOGGING
# ============================================================================

def setup_logging() -> logging.Logger:
    """Configure dual-sink (file + stdout) logging."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp  = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_file   = LOG_DIR / f"position_sizes_{timestamp}.log"

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

def get_max_positions(account_equity: float) -> int:
    """
    Return the maximum number of simultaneous positions allowed for an
    account of the given equity size.

    Schedule is defined in config/strategy_parameters.json under
    position_sizing.position_count_schedule and loaded via P.

    Rationale: smaller accounts must limit positions to avoid
    disproportionate transaction costs and over-diversification risk.
    """
    return P.position_sizing.max_positions_for_equity(account_equity)


def validate_date(date_str: str) -> None:
    """Raise ValueError if date_str is not a valid YYYY-MM-DD string."""
    datetime.strptime(date_str, "%Y-%m-%d")


# ============================================================================
# DATA LOADING
# ============================================================================

def load_momentum_ranked(as_of_date: str) -> List[Dict]:
    """
    Load the ranked instrument list produced by Script 7.

    Expected file: data_cache/signals/momentum_ranked.json

    Returns:
        List of instrument dicts, sorted descending by momentum_score
        (rank=1 is the strongest trend).

    Raises:
        SystemExit on missing file, invalid format, or mismatched as_of_date.
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

    if "ranked" not in data or not data["ranked"]:
        logger.error(
            "momentum_ranked.json exists but contains no ranked entries. "
            "Check that Script 7 completed successfully."
        )
        sys.exit(1)

    ranked: List[Dict] = data["ranked"]

    # Warn if the file was generated for a different as_of_date
    file_date = data.get("metadata", {}).get("as_of_date", "")
    if file_date and file_date != as_of_date:
        logger.warning(
            f"momentum_ranked.json was generated for {file_date}, "
            f"but --as-of-date is {as_of_date}. "
            "Consider re-running Script 7 for the correct date."
        )

    logger.info(
        f"ÃÂ¢ÃÂÃ¢ÂÂ Loaded {len(ranked)} ranked instruments from {ranked_file}"
    )
    return ranked


def load_portfolio_state(path: Optional[Path] = None) -> Dict:
    """
    Optionally load current portfolio positions.

    Used only to tag each sized position as 'new_entry' or 'hold'.
    If the file does not exist the function returns an empty dict
    (all candidates will be labelled as new entries).

    Args:
        path: Override default data/portfolio_state.json location.

    Returns:
        Dict keyed by symbol, or {} if unavailable.
    """
    state_file = path or (DATA_DIR / "portfolio_state.json")

    if not state_file.exists():
        logger.info(
            f"portfolio_state.json not found at {state_file}. "
            "Assuming no existing positions â all candidates treated as new entries."
        )
        return {}

    with open(state_file, "r") as f:
        state = json.load(f)

    positions = state.get("positions", state)  # support both nested and flat formats
    logger.info(f"ÃÂ¢ÃÂÃ¢ÂÂ Loaded {len(positions)} existing positions from {state_file}")
    return positions


# ============================================================================
# CORE SIZING LOGIC
# ============================================================================

def calculate_position_size(
    account_equity:     float,
    stop_distance_pct:  float,
    entry_price:        float,
) -> Dict:
    """
    Compute the stop-distance-based position size for a single instrument.

    Formula (v3.3 — fixed-fractional, canonical trend-following approach):

        Step 1:  Base_Risk         = Account_Equity × TARGET_RISK_PER_POSITION
        Step 2:  Effective_Stop    = min(stop_distance_pct, MAX_STOP_DISTANCE_PCT)
                 (caps impact of very tight stops e.g. 0.4% on near-stop instruments)
        Step 3:  Raw_Value         = Base_Risk / (Effective_Stop / 100)
        Step 4:  Final_Value       = CLIP(Raw_Value, MIN_POSITION_PCT×equity,
                                                      MAX_POSITION_PCT×equity)
        Step 5:  Shares            = floor(Final_Value / Entry_Price)
                 Actual_Value      = Shares × Entry_Price

    This formula sizes each position so that if stopped out, the loss equals
    exactly TARGET_RISK_PER_POSITION of account equity (before the ceiling clip).
    Positions with very wide stops receive proportionally smaller allocations.

    Args:
        account_equity:    Total account equity in EUR.
        stop_distance_pct: Distance from entry to initial stop as % of entry price.
                           Sourced from stop_levels.json (Script 8 output).
        entry_price:       Close price used for whole-share calculation.

    Returns:
        Dict with all sizing components and diagnostics.

    Example (€15,000 account, 21% stop distance, €12.31 entry):
        Base_Risk       = 15,000 × 0.02       = €300
        Effective_Stop  = min(21.0, 40.0)     = 21.0%
        Raw_Value       = 300 / 0.21          = €1,429
        Min             = 15,000 × 0.005      = €75
        Max             = 15,000 × 0.08       = €1,200
        Final_Value     = €1,200  (capped at max)
        Shares          = floor(1200 / 12.31) = 97
        Actual_Value    = 97 × 12.31          = €1,194  (8.0% of equity)
    """
    if stop_distance_pct <= 0:
        raise ValueError(
            f"stop_distance_pct must be > 0, got {stop_distance_pct}"
        )
    if entry_price <= 0:
        raise ValueError(f"entry_price must be > 0, got {entry_price}")

    # Step 1: Fixed monetary risk amount
    base_risk: float = account_equity * TARGET_RISK_PER_POSITION

    # Step 2: Cap stop distance to prevent outsized positions on near-stop instruments
    effective_stop: float = min(stop_distance_pct, MAX_STOP_DISTANCE_PCT * 100)

    # Step 3: Raw position value — size so loss at stop == base_risk
    raw_value: float = base_risk / (effective_stop / 100.0)

    # Step 4: Floor / ceiling clip
    min_value: float  = account_equity * MIN_POSITION_PCT
    max_value: float  = account_equity * MAX_POSITION_PCT
    clipped:   bool   = False
    clip_reason: Optional[str] = None

    if raw_value < min_value:
        final_value = min_value
        clipped     = True
        clip_reason = "at_min_bound"
    elif raw_value > max_value:
        final_value = max_value
        clipped     = True
        clip_reason = "at_max_bound"
    else:
        final_value = raw_value

    # Step 5: Whole-share conversion
    shares: int         = math.floor(final_value / entry_price)
    actual_value: float = shares * entry_price
    position_pct: float = (actual_value / account_equity) * 100

    return {
        # Core sizing output
        "shares":               shares,
        "position_value_eur":   round(actual_value, 2),
        "position_pct":         round(position_pct, 4),
        # Formula components (audit trail)
        "base_risk":            round(base_risk, 2),
        "stop_distance_pct":    round(stop_distance_pct, 4),
        "effective_stop_pct":   round(effective_stop, 4),
        "raw_value":            round(raw_value, 2),
        "min_value":            round(min_value, 2),
        "max_value":            round(max_value, 2),
        "final_value_before_rounding": round(final_value, 2),
        "clipped":              clipped,
        "clip_reason":          clip_reason,
    }


def _load_stop_levels() -> Dict:
    """
    Load stop levels produced by Script 08 (08_calculate_stops.py).

    Expected file: data_cache/portfolio/stop_levels.json
    Key field used for sizing: stop_distance_pct

    Returns:
        Dict keyed by symbol: { symbol: { stop_price, stop_distance_pct, ... } }

    Raises:
        SystemExit if file is missing or empty.
    """
    stops_file = PORTFOLIO_DIR / "stop_levels.json"

    if not stops_file.exists():
        logger.error(
            f"stop_levels.json not found at {stops_file}.\n"
            "Please run Script 08 (08_calculate_stops.py) first."
        )
        sys.exit(1)

    with open(stops_file, "r") as f:
        data = json.load(f)

    stops = data.get("stops", {})

    if not stops:
        logger.error(
            "stop_levels.json contains no stop records. "
            "Check that Script 08 completed successfully."
        )
        sys.exit(1)

    logger.info(f"✔ Loaded {len(stops)} stop levels from {stops_file}")
    return stops


def apply_crypto_cap(
    candidates: List[Dict],
    account_equity: float,
) -> Tuple[List[Dict], List[str]]:
    """
    Enforce the 20% total-portfolio cap on crypto assets.

    Algorithm:
        1. Separate candidates into crypto and non-crypto lists.
        2. Sum all crypto position values.
        3. If sum > 20% ÃÂâ equity, proportionally reduce each crypto
           position's shares until the constraint is satisfied,
           starting from the lowest-momentum crypto positions.
        4. Discard any crypto position that rounds down to 0 shares.

    Args:
        candidates:     List of fully-sized position dicts (from size_portfolio).
        account_equity: Total account equity in EUR.

    Returns:
        Tuple of:
            - adjusted candidates list (crypto values may be reduced)
            - list of warning strings
    """
    warnings: List[str] = []
    max_crypto_value: float = account_equity * MAX_CRYPTO_ALLOCATION_PCT

    crypto_positions = [c for c in candidates if c.get("asset_class") == "crypto"]
    non_crypto       = [c for c in candidates if c.get("asset_class") != "crypto"]

    if not crypto_positions:
        return candidates, warnings

    total_crypto_value = sum(c["position_value_eur"] for c in crypto_positions)

    if total_crypto_value <= max_crypto_value:
        return candidates, warnings

    # Constraint violated â reduce crypto allocations
    warnings.append(
        f"Crypto cap triggered: raw crypto allocation "
        f"ÃÂ¢Ã¢ÂÂÃÂ¬{total_crypto_value:,.0f} ({total_crypto_value / account_equity:.1%}) "
        f"exceeds {MAX_CRYPTO_ALLOCATION_PCT:.0%} limit "
        f"(ÃÂ¢Ã¢ÂÂÃÂ¬{max_crypto_value:,.0f}). Reducing crypto positions."
    )
    logger.warning(warnings[-1])

    # Scale factor to bring total crypto to exactly the cap
    scale_factor = max_crypto_value / total_crypto_value

    adjusted_crypto: List[Dict] = []
    for pos in crypto_positions:
        new_value  = pos["position_value_eur"] * scale_factor
        new_shares = math.floor(new_value / pos["entry_price"])

        if new_shares <= 0:
            warnings.append(
                f"  Crypto position {pos['symbol']} reduced to 0 shares "
                "after cap â excluded from portfolio."
            )
            logger.warning(f"  EXCLUDED (crypto cap, 0 shares): {pos['symbol']}")
            continue

        pos = dict(pos)  # copy so we don't mutate the original
        pos["shares"]             = new_shares
        pos["position_value_eur"] = round(new_shares * pos["entry_price"], 2)
        pos["position_pct"]       = round(
            pos["position_value_eur"] / account_equity * 100, 4
        )
        pos["clipped"]     = True
        pos["clip_reason"] = "crypto_cap"
        adjusted_crypto.append(pos)

    return non_crypto + adjusted_crypto, warnings


def check_portfolio_constraints(
    sized_positions: List[Dict],
    account_equity: float,
) -> Tuple[float, List[str]]:
    """
    Evaluate portfolio-level constraint warnings after sizing.

    Checks performed:
        1. Cash reserve    ÃÂ¢Ã¢ÂÂ°ÃÂ¥ 5%  (warning if breached)
        2. Single-sector   ÃÂ¢Ã¢ÂÂ°ÃÂ¤ 30% (warning per sector if breached)
        3. Top-3 concentration ÃÂ¢Ã¢ÂÂ°ÃÂ¤ 30% (warning â circuit-breaker territory)

    Args:
        sized_positions: List of position dicts post-sizing.
        account_equity:  Total account equity in EUR.

    Returns:
        Tuple of (total_allocated_value, list_of_warning_strings).
    """
    warnings: List[str] = []

    total_allocated = sum(p["position_value_eur"] for p in sized_positions)
    allocated_pct   = total_allocated / account_equity
    cash_reserve    = account_equity - total_allocated
    cash_pct        = cash_reserve / account_equity

    # 1. Cash reserve check
    if cash_pct < MIN_CASH_RESERVE_PCT:
        warnings.append(
            f"LOW CASH RESERVE: {cash_pct:.1%} "
            f"(ÃÂ¢Ã¢ÂÂÃÂ¬{cash_reserve:,.0f}) â minimum is {MIN_CASH_RESERVE_PCT:.0%}. "
            "Consider reducing position count or sizes."
        )

    # 2. Sector concentration
    sector_totals: Dict[str, float] = {}
    for pos in sized_positions:
        sector = pos.get("sector") or "Unknown"
        sector_totals[sector] = sector_totals.get(sector, 0.0) + pos["position_value_eur"]

    for sector, value in sector_totals.items():
        sector_pct = value / account_equity
        if sector_pct > MAX_SECTOR_ALLOCATION_PCT:
            warnings.append(
                f"SECTOR CONCENTRATION: '{sector}' = {sector_pct:.1%} "
                f"(ÃÂ¢Ã¢ÂÂÃÂ¬{value:,.0f}) exceeds {MAX_SECTOR_ALLOCATION_PCT:.0%} limit."
            )

    # 3. Top-3 concentration (circuit-breaker territory)
    sorted_values = sorted(
        [p["position_value_eur"] for p in sized_positions], reverse=True
    )
    top3_value = sum(sorted_values[:3]) if len(sorted_values) >= 3 else sum(sorted_values)
    top3_pct   = top3_value / account_equity

    if top3_pct > MAX_TOP3_CONCENTRATION_PCT:
        warnings.append(
            f"TOP-3 CONCENTRATION: {top3_pct:.1%} "
            f"(ÃÂ¢Ã¢ÂÂÃÂ¬{top3_value:,.0f}) exceeds {MAX_TOP3_CONCENTRATION_PCT:.0%}. "
            "Circuit-breaker may trigger in Script 14 daily monitoring."
        )

    if allocated_pct > 0.95:
        warnings.append(
            f"HIGH ALLOCATION: {allocated_pct:.1%} of equity deployed "
            f"(ÃÂ¢Ã¢ÂÂÃÂ¬{total_allocated:,.0f}). Less than 5% remains unallocated."
        )

    return total_allocated, warnings


# ============================================================================
# PORTFOLIO-LEVEL SIZING ORCHESTRATOR
# ============================================================================

def size_portfolio(
    ranked_symbols: List[Dict],
    account_equity: float,
    max_positions: int,
    existing_positions: Optional[Dict] = None,
    sizing_pool_size: Optional[int] = None,
) -> Tuple[List[Dict], Dict]:
    """
    Calculate position sizes for qualified instruments and apply all
    portfolio-level constraints.

    Process:
        1. Select candidates (default: ALL qualified symbols, or limit if specified).
        2. Calculate median ATR across the selected set.
        3. Size each position using the stop-distance formula (Script 08 stop levels).
        4. Discard any instrument that rounds to 0 shares (price too high
           for account size at current risk parameters).
        5. Apply crypto allocation cap (â¤ 20%).
        6. Evaluate portfolio-level constraint warnings.
        7. Tag each position as 'new_entry' or 'hold'.

    Args:
        ranked_symbols:      Full ranked list from Script 7.
        account_equity:      Account equity in EUR.
        max_positions:       Portfolio limit (metadata for Script 11, not used in sizing).
        existing_positions:  Optional dict of current portfolio positions.
        sizing_pool_size:    Number of positions to size (default: None = size ALL)

    Returns:
        Tuple of:
            - List of fully-sized position dicts (ordered by rank)
            - Dict of portfolio-level summary statistics
    """
    existing_positions = existing_positions or {}

    # -------------------------------------------------------------------------
    # Step 1: Select candidates (default: ALL qualified symbols)
    # -------------------------------------------------------------------------
    if sizing_pool_size is None:
        candidates = ranked_symbols  # Size ALL qualified symbols
        logger.info(
            f"Sizing ALL {len(candidates)} qualified instruments"
        )
    else:
        candidates = ranked_symbols[:sizing_pool_size]
        logger.info(
            f"Sizing {len(candidates)} positions "
            f"(top {sizing_pool_size} of {len(ranked_symbols)} qualified instruments)"
        )

    if not candidates:
        logger.error("No ranked instruments available for sizing.")
        sys.exit(1)

    logger.info(
        f"\nSizing {len(candidates)} positions "
        f"(top {max_positions} of {len(ranked_symbols)} qualified instruments)"
    )

    # -------------------------------------------------------------------------
    # Step 2: Median ATR across the selected set
    # -------------------------------------------------------------------------
    # Step 2: Load stop levels from Script 08 output
    # -------------------------------------------------------------------------
    stop_levels = _load_stop_levels()

    logger.info(f"  Stop levels loaded: {len(stop_levels)} instruments")

    # -------------------------------------------------------------------------
    # Step 3: Size each candidate using stop-distance formula
    # -------------------------------------------------------------------------
    sized: List[Dict] = []
    zero_share_skipped: List[str] = []
    high_unit_price_skipped: List[Dict] = []

    for candidate in candidates:
        symbol = candidate["symbol"]
        close  = candidate.get("close")

        if close is None or close <= 0:
            logger.warning(f"  SKIP {symbol}: missing or zero close price")
            continue

        # Get stop distance from Script 08 output
        stop_rec = stop_levels.get(symbol)
        if stop_rec is None:
            logger.warning(f"  SKIP {symbol}: no stop level found in stop_levels.json — run Script 08 first")
            continue

        stop_dist_pct = stop_rec.get("stop_distance_pct")
        if stop_dist_pct is None or stop_dist_pct <= 0:
            logger.warning(f"  SKIP {symbol}: stop_distance_pct missing or zero")
            continue

        try:
            sizing = calculate_position_size(
                account_equity=account_equity,
                stop_distance_pct=stop_dist_pct,
                entry_price=close,
            )
        except ValueError as exc:
            logger.warning(f"  SKIP {symbol}: sizing error — {exc}")
            continue

        if sizing["shares"] == 0:
            zero_share_skipped.append(symbol)
            min_tradeable_equity = round(close / MAX_POSITION_PCT, 0)
            if close > MAX_POSITION_PCT * account_equity:
                high_unit_price_skipped.append({        # ← add this line
                    "symbol": symbol,                   # ← add this line
                    "price_eur": round(close, 2),       # ← add this line
                    "min_account_equity_eur": round(close / MAX_POSITION_PCT, 0),  # ← add
                })  
                logger.info(
                    f"  SKIP {symbol}: untradeable at current account size "
                    f"(price €{close:,.2f} exceeds max position €{MAX_POSITION_PCT * account_equity:,.0f}). "
                    f"Tradeable when account equity ≥ €{min_tradeable_equity:,.0f}."
                )
            else:
                logger.warning(
                    f"  SKIP {symbol}: rounds to 0 shares "
                    f"(price €{close:.2f} > final_value €{sizing['final_value_before_rounding']:.2f}). "
                    "Account size too small for this instrument at current risk parameters."
                )
            continue

        atr_pct     = candidate.get("atr_pct")
        limit_price = round(close * (1 + ENTRY_LIMIT_OFFSET), 4)

        position_entry: Dict = {
            # Identification
            "rank":             candidate["rank"],
            "symbol":           symbol,
            "name":             candidate.get("name", ""),
            "exchange":         candidate.get("exchange", ""),
            "sector":           candidate.get("sector", ""),
            "asset_class":      candidate.get("asset_class", ""),
            # Pricing
            "close_price":      round(close, 4),
            "entry_price":      round(close, 4),      # Alias used by Scripts 10 and 11
            "limit_price":      limit_price,
            # Sizing output
            **sizing,
            # Trend metrics (from Script 7 — stored for transparency)
            "momentum_score":   candidate.get("momentum_score"),
            "sma_fast":           candidate.get("sma_fast"),
            "sma_slow":          candidate.get("sma_slow"),
            "adx":           candidate.get("adx"),
            "atr_pct":       atr_pct,
            # ROC supplementary (not used for sizing)
            "roc_20d":          candidate.get("roc_20d"),
            "roc_60d":          candidate.get("roc_60d"),
            "roc_120d":         candidate.get("roc_120d"),
            # Audit
            "as_of_date":       candidate.get("as_of_date"),
            "is_new_entry":     symbol not in existing_positions,
            "sizing_formula":   (
                "Base_Risk / Stop_Distance_Pct → CLIP(0.5%, 8.0%) → floor(÷price)"
            ),
        }

        sized.append(position_entry)

    if not sized:
        logger.error("No valid positions could be sized. Aborting.")
        sys.exit(1)

        logger.error("No valid positions could be sized. Aborting.")
        sys.exit(1)

    # -------------------------------------------------------------------------
    # Step 5: Crypto allocation cap
    # -------------------------------------------------------------------------
    sized, crypto_warnings = apply_crypto_cap(sized, account_equity)

    # -------------------------------------------------------------------------
    # Step 6: Portfolio constraint checks
    # -------------------------------------------------------------------------
    total_allocated, constraint_warnings = check_portfolio_constraints(
        sized, account_equity
    )

    all_warnings = crypto_warnings + constraint_warnings
    for w in all_warnings:
        logger.warning(f"  ÃÂ¢ÃÂ¡ÃÂ   {w}")

    # -------------------------------------------------------------------------
    # Step 7: Build summary statistics
    # -------------------------------------------------------------------------
    cash_reserve     = account_equity - total_allocated
    cash_reserve_pct = (cash_reserve / account_equity) * 100

    # Sector allocation breakdown
    sector_alloc: Dict[str, Dict] = {}
    for pos in sized:
        sector = pos.get("sector") or "Unknown"
        if sector not in sector_alloc:
            sector_alloc[sector] = {"value_eur": 0.0, "pct": 0.0, "count": 0}
        sector_alloc[sector]["value_eur"] += pos["position_value_eur"]
        sector_alloc[sector]["count"]     += 1

    for sector in sector_alloc:
        sector_alloc[sector]["pct"] = round(
            sector_alloc[sector]["value_eur"] / account_equity * 100, 2
        )
        sector_alloc[sector]["value_eur"] = round(sector_alloc[sector]["value_eur"], 2)

    # Asset class allocation breakdown
    asset_alloc: Dict[str, Dict] = {}
    for pos in sized:
        ac = pos.get("asset_class") or "unknown"
        if ac not in asset_alloc:
            asset_alloc[ac] = {"value_eur": 0.0, "pct": 0.0, "count": 0}
        asset_alloc[ac]["value_eur"] += pos["position_value_eur"]
        asset_alloc[ac]["count"]     += 1

    for ac in asset_alloc:
        asset_alloc[ac]["pct"] = round(
            asset_alloc[ac]["value_eur"] / account_equity * 100, 2
        )
        asset_alloc[ac]["value_eur"] = round(asset_alloc[ac]["value_eur"], 2)

    # Top-3 concentration
    sorted_vals = sorted([p["position_value_eur"] for p in sized], reverse=True)
    top3_pct    = round(sum(sorted_vals[:3]) / account_equity * 100, 2) if sorted_vals else 0.0

    summary: Dict = {
        "as_of_date":                   sized[0]["as_of_date"] if sized else None,
        "account_equity":               round(account_equity, 2),
        "max_positions":                max_positions,
        "total_candidates":             len(ranked_symbols),
        "total_sized":                  len(sized),
        "zero_share_skipped":           zero_share_skipped,
        "untradeable_high_unit_price":  high_unit_price_skipped,
        "sizing_formula":        "Base_Risk / Stop_Distance_Pct",
        "target_risk_per_pos":          TARGET_RISK_PER_POSITION,
        "total_allocated_eur":          round(total_allocated, 2),
        "total_allocated_pct":          round(total_allocated / account_equity * 100, 2),
        "cash_reserve_eur":             round(cash_reserve, 2),
        "cash_reserve_pct":             round(cash_reserve_pct, 2),
        "top3_concentration_pct":       top3_pct,
        "asset_class_allocation":       asset_alloc,
        "sector_allocation":            sector_alloc,
        "warnings":                     all_warnings,
        "generated_at":                 datetime.now().isoformat(),
    }

    return sized, summary


# ============================================================================
# OUTPUT FUNCTIONS
# ============================================================================

def save_position_sizes(
    sized_positions: List[Dict],
    summary: Dict,
    output_file: Path,
) -> None:
    """
    Save position sizes to JSON in the format expected by Scripts 10 and 11.

    Format:
        {
            "metadata": { ... summary stats ... },
            "positions": {
                "AAPL.US": { rank, symbol, shares, position_value_eur, ... },
                ...
            }
        }
    """
    PORTFOLIO_DIR.mkdir(parents=True, exist_ok=True)

    # Build symbol-keyed dict for O(1) downstream lookup
    positions_dict = {p["symbol"]: p for p in sized_positions}

    output = {
        "metadata": summary,
        "positions": positions_dict,
    }

    with open(output_file, "w") as f:
        json.dump(output, f, indent=2, default=str)

    logger.info(f"ÃÂ¢ÃÂÃ¢ÂÂ Saved {len(positions_dict)} position sizes ÃÂ¢Ã¢ÂÂ Ã¢ÂÂ {output_file}")


def save_sizing_summary(summary: Dict, output_file: Path) -> None:
    """Save run-level statistics to JSON."""
    PORTFOLIO_DIR.mkdir(parents=True, exist_ok=True)

    with open(output_file, "w") as f:
        json.dump(summary, f, indent=2, default=str)

    logger.info(f"ÃÂ¢ÃÂÃ¢ÂÂ Saved sizing summary ÃÂ¢Ã¢ÂÂ Ã¢ÂÂ {output_file}")


def save_sizing_csv(sized_positions: List[Dict], output_file: Path) -> None:
    """
    Save human-readable CSV for review and audit trail.

    Columns (ordered for readability):
        rank, symbol, name, exchange, sector, asset_class,
        shares, close_price, limit_price, position_value_eur, position_pct,
        atr_pct, stop_distance_pct, effective_stop_pct, base_risk, raw_value,
        clipped, clip_reason, momentum_score, adx,
        sma_fast, sma_slow, roc_20d, roc_60d, roc_120d, as_of_date
    """
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    if not sized_positions:
        logger.warning("No positions to save â skipping CSV export")
        return

    df = pd.DataFrame(sized_positions)

    col_order = [
        "rank", "symbol", "name", "exchange", "sector", "asset_class",
        "shares", "close_price", "limit_price",
        "position_value_eur", "position_pct",
        "atr_pct", "stop_distance_pct", "effective_stop_pct",
        "base_risk", "raw_value", "clipped", "clip_reason",
        "momentum_score", "adx",
        "sma_fast", "sma_slow",
        "roc_20d", "roc_60d", "roc_120d",
        "is_new_entry", "as_of_date",
    ]
    available = [c for c in col_order if c in df.columns]
    df = df[available]

    df.to_csv(output_file, index=False, float_format="%.4f")
    logger.info(f"ÃÂ¢ÃÂÃ¢ÂÂ Saved CSV report ÃÂ¢Ã¢ÂÂ Ã¢ÂÂ {output_file}")


def print_sizing_summary(sized_positions: List[Dict], summary: Dict, top_n: int = 25) -> None:
    """Print a formatted sizing table to the console."""
    logger.info(f"\n{'=' * 90}")
    logger.info(
        f"POSITION SIZES  "
        f"(account: ÃÂ¢Ã¢ÂÂÃÂ¬{summary['account_equity']:,.0f}  |  "
        f"date: {summary.get('as_of_date', 'N/A')})"
    )
    logger.info(f"{'=' * 90}")

    header = (
        f"{'Rnk':>4}  {'Symbol':<14} {'Shr':>5}  "
        f"{'ÃÂ¢Ã¢ÂÂÃÂ¬ Value':>9}  {'% Eq':>6}  "
        f"{'ATR%':>6}  {'StopDst':>7}  "
        f"{'Clpd':>5}  {'Score':>8}  {'New':>4}"
    )
    logger.info(header)
    logger.info("-" * 90)

    for pos in sized_positions[:top_n]:
        clipped_flag = "YES" if pos.get("clipped") else "no"
        new_flag     = "NEW" if pos.get("is_new_entry") else "hold"
        line = (
            f"{pos['rank']:>4}  "
            f"{pos['symbol']:<14} "
            f"{pos['shares']:>5}  "
            f"ÃÂ¢Ã¢ÂÂÃÂ¬{pos['position_value_eur']:>8,.0f}  "
            f"{pos['position_pct']:>5.2f}%  "
            f"{pos['atr_pct']:>6.2f}  "
            f"{pos.get('stop_distance_pct', 0):>6.1f}%  "
            f"{clipped_flag:>5}  "
            f"{pos['momentum_score']:>+8.2f}  "
            f"{new_flag:>4}"
        )
        logger.info(line)

    if len(sized_positions) > top_n:
        logger.info(f"\n  ... and {len(sized_positions) - top_n} more (see CSV report)")

    logger.info(f"\n  Total allocated : ÃÂ¢Ã¢ÂÂÃÂ¬{summary['total_allocated_eur']:>12,.2f}  "
                f"({summary['total_allocated_pct']:.1f}% of equity)")
    logger.info(f"  Cash reserve    : ÃÂ¢Ã¢ÂÂÃÂ¬{summary['cash_reserve_eur']:>12,.2f}  "
                f"({summary['cash_reserve_pct']:.1f}%)")
    logger.info(f"  Top-3 conc.     : {summary['top3_concentration_pct']:.1f}%")
    logger.info(f"  Sizing formula  : {summary['sizing_formula']}")
    logger.info(f"  Positions sized : {summary['total_sized']}")
    if summary.get("zero_share_skipped"):
        logger.info(
            f"  Skipped (0 shr) : {', '.join(summary['zero_share_skipped'])}"
        )
    if summary.get("warnings"):
        logger.info(f"\n  Warnings ({len(summary['warnings'])}):")
        for w in summary["warnings"]:
            logger.warning(f"    ÃÂ¢ÃÂ¡ÃÂ   {w}")

    # Asset class summary
    if summary.get("asset_class_allocation"):
        logger.info("\n  Asset class allocation:")
        for ac, info in summary["asset_class_allocation"].items():
            logger.info(
                f"    {ac:<12}: ÃÂ¢Ã¢ÂÂÃÂ¬{info['value_eur']:>10,.0f}  "
                f"({info['pct']:.1f}%)  [{info['count']} positions]"
            )


# ============================================================================
# ARGUMENT PARSING
# ============================================================================

def parse_arguments() -> argparse.Namespace:
    """Parse and validate command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Position Sizer â Script 9 (v3.2)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Minimal: auto-determine max positions from equity
    python scripts/09_calculate_position_sizes.py \\
        --account-equity 50000 --as-of-date 2026-01-31

    # Explicit position cap (overrides equity-based schedule)
    python scripts/09_calculate_position_sizes.py \\
        --account-equity 50000 --as-of-date 2026-01-31 --max-positions 20

    # Dry run: compute and display only â no files written
    python scripts/09_calculate_position_sizes.py \\
        --account-equity 50000 --as-of-date 2026-01-31 --dry-run

    # With existing portfolio state (tags new vs hold positions)
    python scripts/09_calculate_position_sizes.py \\
        --account-equity 50000 --as-of-date 2026-01-31 \\
        --portfolio-state data/portfolio_state.json

Dependencies (run in order):
    01  02  03  04  05  06  07  08_calculate_stops.py  â then this script
        """
    )

    parser.add_argument(
        "--account-equity",
        type=float,
        required=True,
        metavar="AMOUNT",
        help="Total account equity in EUR (e.g. 50000)",
    )
    parser.add_argument(
        "--as-of-date",
        required=True,
        metavar="YYYY-MM-DD",
        help="Rebalancing reference date",
    )
    parser.add_argument(
        "--max-positions",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Override maximum number of positions "
            "(default: auto-computed from account equity size schedule). "
            "NOTE: This is portfolio limit metadata only - use --sizing-pool to limit which positions are sized."
        ),
    )
    parser.add_argument(
        "--sizing-pool",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Number of positions to size "
            "(default: size ALL qualified symbols from Script 7). "
            "Use this to limit sizing for testing/debugging."
        ),
    )
    parser.add_argument(
        "--portfolio-state",
        type=Path,
        default=None,
        metavar="PATH",
        help="Path to data/portfolio_state.json (optional)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute and display sizes but do not write any output files",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=25,
        metavar="N",
        help="Number of rows to display in console table (default: 25)",
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
    strat_reports   = strategy.reports_dir(PROJECT_ROOT, "portfolio")
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
    logger.info("POSITION SIZER â Script 9")
    logger.info("Architecture v3.2 (Feb 2026)")
    logger.info("=" * 70)

    # ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ Parse & validate arguments ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬

    try:
        validate_date(args.as_of_date)
    except ValueError:
        logger.error(
            f"Invalid date format: '{args.as_of_date}'.  Expected YYYY-MM-DD."
        )
        return 1

    if args.account_equity <= 0:
        logger.error(f"--account-equity must be > 0, got {args.account_equity}")
        return 1

    # Determine max positions
    max_positions: int = (
        args.max_positions
        if args.max_positions is not None
        else get_max_positions(args.account_equity)
    )

    if max_positions <= 0:
        logger.error(f"max_positions must be > 0, got {max_positions}")
        return 1

    logger.info(f"\nAs-of date       : {args.as_of_date}")
    logger.info(f"Account equity   : ÃÂ¢Ã¢ÂÂÃÂ¬{args.account_equity:,.2f}")
    logger.info(f"Max positions    : {max_positions}"
                + (" (auto)" if args.max_positions is None else " (manual override)"))
    logger.info(f"Sizing pool      : {'ALL qualified symbols' if args.sizing_pool is None else f'Top {args.sizing_pool}'}")
    logger.info(f"Dry run          : {args.dry_run}")
    logger.info(f"Target risk/pos  : {TARGET_RISK_PER_POSITION:.1%}")
    logger.info(f"Min position     : {MIN_POSITION_PCT:.1%} of equity")
    logger.info(f"Max position     : {MAX_POSITION_PCT:.1%} of equity")
    logger.info(f"Crypto cap       : {MAX_CRYPTO_ALLOCATION_PCT:.0%} of equity")

    # ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ Load inputs ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬
    try:
        ranked_list = load_momentum_ranked(args.as_of_date)
    except SystemExit:
        return 1

    existing_positions = load_portfolio_state(args.portfolio_state)

    # ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ Compute position sizes ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬
    try:
        sized_positions, summary = size_portfolio(
            ranked_symbols=ranked_list,
            account_equity=args.account_equity,
            max_positions=max_positions,
            existing_positions=existing_positions,
            sizing_pool_size=args.sizing_pool,
        )
    except SystemExit:
        return 1
    except Exception as exc:
        logger.error(f"Fatal error during position sizing: {exc}", exc_info=True)
        return 1

    # ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ Console output ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬
    print_sizing_summary(sized_positions, summary, top_n=args.top)

    # ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ Save outputs ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬
    if args.dry_run:
        logger.info("\nÃÂ¢ÃÂ¡ÃÂ   Dry-run mode â output files NOT written")
    else:
        date_tag = args.as_of_date.replace("-", "")
        try:
            save_position_sizes(
                sized_positions,
                summary,
                PORTFOLIO_DIR / "position_sizes.json",
            )
            save_sizing_summary(
                summary,
                PORTFOLIO_DIR / "sizing_summary.json",
            )
            save_sizing_csv(
                sized_positions,
                REPORTS_DIR / f"{date_tag}_position_sizes.csv",
            )
        except Exception as exc:
            logger.error(f"Error writing output files: {exc}", exc_info=True)
            return 1

    # ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ Timing & next-step hint ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬ÃÂ¢Ã¢ÂÂÃ¢ÂÂ¬
    elapsed = datetime.now() - start_time
    new_count  = sum(1 for p in sized_positions if p.get("is_new_entry"))
    hold_count = len(sized_positions) - new_count

    logger.info(f"\n{'=' * 70}")
    logger.info("POSITION SIZING COMPLETE")
    logger.info(f"{'=' * 70}")
    logger.info(f"Duration         : {elapsed}")
    logger.info(f"Positions sized  : {len(sized_positions)}  "
                f"(new: {new_count}  |  hold: {hold_count})")
    logger.info(f"Total allocated  : ÃÂ¢Ã¢ÂÂÃÂ¬{summary['total_allocated_eur']:,.2f} "
                f"({summary['total_allocated_pct']:.1f}%)")
    if not args.dry_run:
        logger.info("Outputs:")
        logger.info(f"  - {PORTFOLIO_DIR / 'position_sizes.json'}")
        logger.info(f"  - {PORTFOLIO_DIR / 'sizing_summary.json'}")
        date_tag = args.as_of_date.replace("-", "")
        logger.info(f"  - {REPORTS_DIR / f'{date_tag}_position_sizes.csv'}")
    logger.info(
        f"Next step : python scripts/10_generate_exit_signals.py "
        f"--as-of-date {args.as_of_date}"
    )
    logger.info("=" * 70)

    return 0


def main() -> int:
    args = parse_arguments()
    logger.info("=" * 70)
    logger.info("POSITION SIZER -- Script 09")
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
    logger.info(f"Next step: python scripts/10_generate_exit_signals.py")
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
