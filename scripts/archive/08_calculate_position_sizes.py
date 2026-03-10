#!/usr/bin/env python3
"""
Script 8: Position Sizer
========================
Calculate volatility-adjusted position sizes for top-N momentum instruments.

Purpose:
    Receives the ranked instrument list from Script 7 and computes a
    precise, rules-based position size for each candidate entry using
    inverse-ATR volatility scaling.  All formula steps are explicit,
    reproducible, and implementation-ready for the downstream scripts:

        Script 9  (09_calculate_stops.py)  â†’ reads position_sizes.json
        Script 11 (11_monthly_rebalancing.py) â†’ reads position_sizes.json

Position Sizing Formula (v3.2 â€” production specification):

    Step 1  Base Risk
            Base_Risk = Account_Equity Ã— Target_Risk_Per_Position
            Target_Risk_Per_Position = 2.0% (constant)

    Step 2  Volatility Adjustment
            Volatility_Multiplier = Median_ATR_Pct / Instrument_ATR_Pct
            â†’ lower-volatility instruments receive proportionally larger
              allocations; higher-volatility instruments receive smaller ones.

    Step 3  Raw Position Value
            Raw_Position_Value = Base_Risk Ã— Volatility_Multiplier

    Step 4  Floor / Ceiling Clip
            Final_Position_Value = CLIP(
                Raw_Position_Value,
                min = MIN_POSITION_PCT Ã— Account_Equity,   # 0.5%
                max = MAX_POSITION_PCT Ã— Account_Equity    # 8.0%
            )

    Step 5  Whole-Share Conversion
            Shares             = floor(Final_Position_Value / Entry_Price)
            Actual_Value       = Shares Ã— Entry_Price

Portfolio Constraints Applied (in order):
    1. Crypto allocation cap  â‰¤ 20% of equity
    2. Single-sector cap      â‰¤ 30% of equity  (warning only)
    3. Cash reserve floor     â‰¥  5% of equity  (warning only)
    4. Top-3 concentration    â‰¤ 30% of equity  (warning only, circuit-breaker territory)
    5. Minimum position threshold: discard symbols that round to 0 shares

Dependencies (run before this script):
    01_download_eodhd_bulk.py
    02_download_yahoo_fundamentals.py
    03_consolidate_validate_data.py
    04_screen_universe.py
    05_calculate_indicators.py
    06_qualify_trends.py
    07_rank_momentum.py   â† THIS SCRIPT'S PRIMARY INPUT

Inputs:
    - data_cache/signals/momentum_ranked.json   (from Script 7)
    - data/portfolio_state.json                 (optional, labels new vs existing)

Outputs:
    - data_cache/portfolio/position_sizes.json      (primary â€” consumed by Script 9 & 11)
    - data_cache/portfolio/sizing_summary.json      (run statistics)
    - reports/portfolio/{YYYYMMDD}_position_sizes.csv  (human-readable)

Execution:
    # Minimal â€” equity and date required; max-positions auto-computed from equity
    python scripts/08_calculate_position_sizes.py \\
        --account-equity 50000 \\
        --as-of-date 2026-01-31

    # Explicit position cap
    python scripts/08_calculate_position_sizes.py \\
        --account-equity 50000 \\
        --as-of-date 2026-01-31 \\
        --max-positions 20

    # Dry run (compute only, no files written)
    python scripts/08_calculate_position_sizes.py \\
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

# ============================================================================
# STRATEGY CONSTANTS  (aligned with Architecture v3.2 / strategy_parameters.json)
# ============================================================================

TARGET_RISK_PER_POSITION: float = 0.02   # 2.0% of equity at risk per position
MIN_POSITION_PCT:          float = 0.005  # 0.5% floor
MAX_POSITION_PCT:          float = 0.08   # 8.0% ceiling

ENTRY_LIMIT_OFFSET:        float = 0.005  # +0.5% above close for limit order

MAX_CRYPTO_ALLOCATION_PCT: float = 0.20   # 20% cap on total crypto exposure
MAX_SECTOR_ALLOCATION_PCT: float = 0.30   # 30% warning threshold per sector
MIN_CASH_RESERVE_PCT:      float = 0.05   # 5% minimum unallocated equity
MAX_TOP3_CONCENTRATION_PCT: float = 0.30  # 30% circuit-breaker threshold (top-3)

# Account-size â†’ max position count  (Architecture Â§3.1)
POSITION_COUNT_SCHEDULE = [
    (25_000,  10),
    (50_000,  15),
    (100_000, 20),
]
DEFAULT_MAX_POSITIONS = 25  # accounts â‰¥ â‚¬100,000

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

    Schedule (Architecture Â§3.1):
        < â‚¬25,000   â†’  10 positions
        < â‚¬50,000   â†’  15 positions
        < â‚¬100,000  â†’  20 positions
        â‰¥ â‚¬100,000  â†’  25 positions

    Rationale: smaller accounts must limit positions to avoid
    disproportionate transaction costs and over-diversification risk.
    """
    for threshold, count in POSITION_COUNT_SCHEDULE:
        if account_equity < threshold:
            return count
    return DEFAULT_MAX_POSITIONS


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
        f"âœ” Loaded {len(ranked)} ranked instruments from {ranked_file}"
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
            "Assuming no existing positions â€” all candidates treated as new entries."
        )
        return {}

    with open(state_file, "r") as f:
        state = json.load(f)

    positions = state.get("positions", state)  # support both nested and flat formats
    logger.info(f"âœ” Loaded {len(positions)} existing positions from {state_file}")
    return positions


# ============================================================================
# CORE SIZING LOGIC
# ============================================================================

def calculate_position_size(
    account_equity: float,
    instrument_atr_pct: float,
    median_atr_pct: float,
    entry_price: float,
) -> Dict:
    """
    Compute the volatility-adjusted position size for a single instrument.

    Formula (Architecture Â§2.6 â€” explicitly reproduced here):

        Step 1:  Base_Risk   = Account_Equity Ã— 0.02
        Step 2:  Vol_Mult    = Median_ATR_Pct / Instrument_ATR_Pct
        Step 3:  Raw_Value   = Base_Risk Ã— Vol_Mult
        Step 4:  Final_Value = CLIP(Raw_Value, 0.5%Ã—equity, 8.0%Ã—equity)
        Step 5:  Shares      = floor(Final_Value / Entry_Price)
                 Actual_Val  = Shares Ã— Entry_Price

    Args:
        account_equity:     Total account equity in EUR.
        instrument_atr_pct: ATR_20 as percentage of price for this instrument.
        median_atr_pct:     Median ATR_20% across all selected instruments.
        entry_price:        Close price used for whole-share calculation.

    Returns:
        Dict with all sizing components and diagnostics.

    Example (from Architecture):
        account_equity  = 50,000
        instrument_atr  = 2.5%
        median_atr      = 2.0%
        entry_price     = â‚¬100

        Base_Risk       = 50,000 Ã— 0.02        = â‚¬1,000
        Vol_Mult        = 2.0 / 2.5            = 0.80
        Raw_Value       = 1,000 Ã— 0.80         = â‚¬800
        Min             = 50,000 Ã— 0.005       = â‚¬250
        Max             = 50,000 Ã— 0.08        = â‚¬4,000
        Final_Value     = â‚¬800 (within bounds)
        Shares          = floor(800 / 100)     = 8
        Actual_Value    = 8 Ã— 100              = â‚¬800  (1.60% of equity)
    """
    if instrument_atr_pct <= 0:
        raise ValueError(
            f"instrument_atr_pct must be > 0, got {instrument_atr_pct}"
        )
    if entry_price <= 0:
        raise ValueError(f"entry_price must be > 0, got {entry_price}")

    # Step 1: Base risk (currency units)
    base_risk: float = account_equity * TARGET_RISK_PER_POSITION

    # Step 2: Inverse-volatility multiplier
    volatility_multiplier: float = median_atr_pct / instrument_atr_pct

    # Step 3: Raw position value
    raw_value: float = base_risk * volatility_multiplier

    # Step 4: Floor / ceiling clip
    min_value: float = account_equity * MIN_POSITION_PCT
    max_value: float = account_equity * MAX_POSITION_PCT
    clipped:   bool  = False
    clip_reason: Optional[str] = None

    if raw_value < min_value:
        final_value  = min_value
        clipped      = True
        clip_reason  = "at_min_bound"
    elif raw_value > max_value:
        final_value  = max_value
        clipped      = True
        clip_reason  = "at_max_bound"
    else:
        final_value  = raw_value

    # Step 5: Whole-share conversion (no fractional shares)
    shares: int          = math.floor(final_value / entry_price)
    actual_value: float  = shares * entry_price
    position_pct: float  = (actual_value / account_equity) * 100

    return {
        # Core sizing output
        "shares":               shares,
        "position_value_eur":   round(actual_value, 2),
        "position_pct":         round(position_pct, 4),
        # Formula components (audit trail)
        "base_risk":            round(base_risk, 2),
        "volatility_multiplier": round(volatility_multiplier, 4),
        "raw_value":            round(raw_value, 2),
        "min_value":            round(min_value, 2),
        "max_value":            round(max_value, 2),
        "final_value_before_rounding": round(final_value, 2),
        "clipped":              clipped,
        "clip_reason":          clip_reason,
    }


def apply_crypto_cap(
    candidates: List[Dict],
    account_equity: float,
) -> Tuple[List[Dict], List[str]]:
    """
    Enforce the 20% total-portfolio cap on crypto assets.

    Algorithm:
        1. Separate candidates into crypto and non-crypto lists.
        2. Sum all crypto position values.
        3. If sum > 20% Ã— equity, proportionally reduce each crypto
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

    # Constraint violated â€” reduce crypto allocations
    warnings.append(
        f"Crypto cap triggered: raw crypto allocation "
        f"â‚¬{total_crypto_value:,.0f} ({total_crypto_value / account_equity:.1%}) "
        f"exceeds {MAX_CRYPTO_ALLOCATION_PCT:.0%} limit "
        f"(â‚¬{max_crypto_value:,.0f}). Reducing crypto positions."
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
                "after cap â€” excluded from portfolio."
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
        1. Cash reserve    â‰¥ 5%  (warning if breached)
        2. Single-sector   â‰¤ 30% (warning per sector if breached)
        3. Top-3 concentration â‰¤ 30% (warning â€” circuit-breaker territory)

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
            f"(â‚¬{cash_reserve:,.0f}) â€” minimum is {MIN_CASH_RESERVE_PCT:.0%}. "
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
                f"(â‚¬{value:,.0f}) exceeds {MAX_SECTOR_ALLOCATION_PCT:.0%} limit."
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
            f"(â‚¬{top3_value:,.0f}) exceeds {MAX_TOP3_CONCENTRATION_PCT:.0%}. "
            "Circuit-breaker may trigger in Script 14 daily monitoring."
        )

    if allocated_pct > 0.95:
        warnings.append(
            f"HIGH ALLOCATION: {allocated_pct:.1%} of equity deployed "
            f"(â‚¬{total_allocated:,.0f}). Less than 5% remains unallocated."
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
        3. Size each position using the inverse-ATR formula.
        4. Discard any instrument that rounds to 0 shares (price too high
           for account size at current risk parameters).
        5. Apply crypto allocation cap (≤ 20%).
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
    atr_values = [c["atr_20_pct"] for c in candidates if c.get("atr_20_pct") and c["atr_20_pct"] > 0]

    if not atr_values:
        logger.error(
            "No valid atr_20_pct values found in candidate list. "
            "Ensure Script 5 (indicator calculator) populated this field."
        )
        sys.exit(1)

    median_atr_pct: float = float(np.median(atr_values))

    logger.info(
        f"  Median ATR (selected set) : {median_atr_pct:.4f}%  "
        f"(range: {min(atr_values):.2f}% â€“ {max(atr_values):.2f}%)"
    )

    # -------------------------------------------------------------------------
    # Step 3: Size each candidate
    # -------------------------------------------------------------------------
    sized: List[Dict] = []
    zero_share_skipped: List[str] = []

    for candidate in candidates:
        symbol     = candidate["symbol"]
        close      = candidate.get("close")
        atr_pct    = candidate.get("atr_20_pct")

        if close is None or close <= 0:
            logger.warning(f"  SKIP {symbol}: missing or zero close price")
            continue

        if atr_pct is None or atr_pct <= 0:
            logger.warning(f"  SKIP {symbol}: missing or zero atr_20_pct")
            continue

        try:
            sizing = calculate_position_size(
                account_equity=account_equity,
                instrument_atr_pct=atr_pct,
                median_atr_pct=median_atr_pct,
                entry_price=close,
            )
        except ValueError as exc:
            logger.warning(f"  SKIP {symbol}: sizing error â€” {exc}")
            continue

        # Discard positions that round to 0 shares (price > final_value)
        if sizing["shares"] == 0:
            zero_share_skipped.append(symbol)
            logger.warning(
                f"  SKIP {symbol}: rounds to 0 shares "
                f"(price â‚¬{close:.2f} > final_value â‚¬{sizing['final_value_before_rounding']:.2f}). "
                "Account size too small for this instrument at current risk parameters."
            )
            continue

        # Limit order price = close + 0.5%  (Architecture Â§2.5)
        limit_price = round(close * (1 + ENTRY_LIMIT_OFFSET), 4)

        position_entry: Dict = {
            # Identification
            "rank":         candidate["rank"],
            "symbol":       symbol,
            "name":         candidate.get("name", ""),
            "exchange":     candidate.get("exchange", ""),
            "sector":       candidate.get("sector", ""),
            "asset_class":  candidate.get("asset_class", ""),
            # Pricing
            "close_price":  round(close, 4),
            "entry_price":  round(close, 4),      # Alias used by Script 9
            "limit_price":  limit_price,
            # Sizing output
            **sizing,
            # Trend metrics (from Script 6/7 â€” stored for transparency)
            "momentum_score":    candidate.get("momentum_score"),
            "sma_50":            candidate.get("sma_50"),
            "sma_200":           candidate.get("sma_200"),
            "adx_14":            candidate.get("adx_14"),
            "atr_20_pct":        atr_pct,
            # ROC supplementary (not used for sizing)
            "roc_20d":           candidate.get("roc_20d"),
            "roc_60d":           candidate.get("roc_60d"),
            "roc_120d":          candidate.get("roc_120d"),
            # Audit
            "median_atr_pct":   round(median_atr_pct, 4),
            "as_of_date":       candidate.get("as_of_date"),
            "is_new_entry":     symbol not in existing_positions,
            "sizing_formula":   (
                "Base_Risk Ã— (Median_ATR / Instrument_ATR) â†’ CLIP(0.5%, 8.0%) â†’ floor(Ã·price)"
            ),
        }

        sized.append(position_entry)

    if not sized:
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
        logger.warning(f"  âš   {w}")

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
        "as_of_date":            sized[0]["as_of_date"] if sized else None,
        "account_equity":        round(account_equity, 2),
        "max_positions":         max_positions,
        "total_candidates":      len(ranked_symbols),
        "total_sized":           len(sized),
        "zero_share_skipped":    zero_share_skipped,
        "median_atr_pct":        round(median_atr_pct, 4),
        "target_risk_per_pos":   TARGET_RISK_PER_POSITION,
        "total_allocated_eur":   round(total_allocated, 2),
        "total_allocated_pct":   round(total_allocated / account_equity * 100, 2),
        "cash_reserve_eur":      round(cash_reserve, 2),
        "cash_reserve_pct":      round(cash_reserve_pct, 2),
        "top3_concentration_pct": top3_pct,
        "asset_class_allocation": asset_alloc,
        "sector_allocation":      sector_alloc,
        "warnings":               all_warnings,
        "generated_at":           datetime.now().isoformat(),
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
    Save position sizes to JSON in the format expected by Script 9 and Script 11.

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

    logger.info(f"âœ” Saved {len(positions_dict)} position sizes â†’ {output_file}")


def save_sizing_summary(summary: Dict, output_file: Path) -> None:
    """Save run-level statistics to JSON."""
    PORTFOLIO_DIR.mkdir(parents=True, exist_ok=True)

    with open(output_file, "w") as f:
        json.dump(summary, f, indent=2, default=str)

    logger.info(f"âœ” Saved sizing summary â†’ {output_file}")


def save_sizing_csv(sized_positions: List[Dict], output_file: Path) -> None:
    """
    Save human-readable CSV for review and audit trail.

    Columns (ordered for readability):
        rank, symbol, name, exchange, sector, asset_class,
        shares, close_price, limit_price, position_value_eur, position_pct,
        atr_20_pct, volatility_multiplier, base_risk, raw_value,
        clipped, clip_reason, momentum_score, adx_14,
        sma_50, sma_200, roc_20d, roc_60d, roc_120d, as_of_date
    """
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    if not sized_positions:
        logger.warning("No positions to save â€” skipping CSV export")
        return

    df = pd.DataFrame(sized_positions)

    col_order = [
        "rank", "symbol", "name", "exchange", "sector", "asset_class",
        "shares", "close_price", "limit_price",
        "position_value_eur", "position_pct",
        "atr_20_pct", "volatility_multiplier",
        "base_risk", "raw_value", "clipped", "clip_reason",
        "momentum_score", "adx_14",
        "sma_50", "sma_200",
        "roc_20d", "roc_60d", "roc_120d",
        "is_new_entry", "as_of_date",
    ]
    available = [c for c in col_order if c in df.columns]
    df = df[available]

    df.to_csv(output_file, index=False, float_format="%.4f")
    logger.info(f"âœ” Saved CSV report â†’ {output_file}")


def print_sizing_summary(sized_positions: List[Dict], summary: Dict, top_n: int = 25) -> None:
    """Print a formatted sizing table to the console."""
    logger.info(f"\n{'=' * 90}")
    logger.info(
        f"POSITION SIZES  "
        f"(account: â‚¬{summary['account_equity']:,.0f}  |  "
        f"date: {summary.get('as_of_date', 'N/A')})"
    )
    logger.info(f"{'=' * 90}")

    header = (
        f"{'Rnk':>4}  {'Symbol':<14} {'Shr':>5}  "
        f"{'â‚¬ Value':>9}  {'% Eq':>6}  "
        f"{'ATR%':>6}  {'VolMult':>7}  "
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
            f"â‚¬{pos['position_value_eur']:>8,.0f}  "
            f"{pos['position_pct']:>5.2f}%  "
            f"{pos['atr_20_pct']:>6.2f}  "
            f"{pos['volatility_multiplier']:>7.3f}  "
            f"{clipped_flag:>5}  "
            f"{pos['momentum_score']:>+8.2f}  "
            f"{new_flag:>4}"
        )
        logger.info(line)

    if len(sized_positions) > top_n:
        logger.info(f"\n  ... and {len(sized_positions) - top_n} more (see CSV report)")

    logger.info(f"\n  Total allocated : â‚¬{summary['total_allocated_eur']:>12,.2f}  "
                f"({summary['total_allocated_pct']:.1f}% of equity)")
    logger.info(f"  Cash reserve    : â‚¬{summary['cash_reserve_eur']:>12,.2f}  "
                f"({summary['cash_reserve_pct']:.1f}%)")
    logger.info(f"  Top-3 conc.     : {summary['top3_concentration_pct']:.1f}%")
    logger.info(f"  Median ATR      : {summary['median_atr_pct']:.4f}%")
    logger.info(f"  Positions sized : {summary['total_sized']}")
    if summary.get("zero_share_skipped"):
        logger.info(
            f"  Skipped (0 shr) : {', '.join(summary['zero_share_skipped'])}"
        )
    if summary.get("warnings"):
        logger.info(f"\n  Warnings ({len(summary['warnings'])}):")
        for w in summary["warnings"]:
            logger.warning(f"    âš   {w}")

    # Asset class summary
    if summary.get("asset_class_allocation"):
        logger.info("\n  Asset class allocation:")
        for ac, info in summary["asset_class_allocation"].items():
            logger.info(
                f"    {ac:<12}: â‚¬{info['value_eur']:>10,.0f}  "
                f"({info['pct']:.1f}%)  [{info['count']} positions]"
            )


# ============================================================================
# ARGUMENT PARSING
# ============================================================================

def parse_arguments() -> argparse.Namespace:
    """Parse and validate command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Position Sizer â€” Script 8 (v3.2)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Minimal: auto-determine max positions from equity
    python scripts/08_calculate_position_sizes.py \\
        --account-equity 50000 --as-of-date 2026-01-31

    # Explicit position cap (overrides equity-based schedule)
    python scripts/08_calculate_position_sizes.py \\
        --account-equity 50000 --as-of-date 2026-01-31 --max-positions 20

    # Dry run: compute and display only â€” no files written
    python scripts/08_calculate_position_sizes.py \\
        --account-equity 50000 --as-of-date 2026-01-31 --dry-run

    # With existing portfolio state (tags new vs hold positions)
    python scripts/08_calculate_position_sizes.py \\
        --account-equity 50000 --as-of-date 2026-01-31 \\
        --portfolio-state data/portfolio_state.json

Dependencies (run in order):
    01  02  03  04  05  06  07_rank_momentum.py  â† then this script
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

    return parser.parse_args()


# ============================================================================
# MAIN
# ============================================================================

def main() -> int:
    start_time = datetime.now()

    logger.info("=" * 70)
    logger.info("POSITION SIZER â€” Script 8")
    logger.info("Architecture v3.2 (Feb 2026)")
    logger.info("=" * 70)

    # â”€â”€ Parse & validate arguments â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    args = parse_arguments()

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
    logger.info(f"Account equity   : â‚¬{args.account_equity:,.2f}")
    logger.info(f"Max positions    : {max_positions}"
                + (" (auto)" if args.max_positions is None else " (manual override)"))
    logger.info(f"Sizing pool      : {'ALL qualified symbols' if args.sizing_pool is None else f'Top {args.sizing_pool}'}")
    logger.info(f"Dry run          : {args.dry_run}")
    logger.info(f"Target risk/pos  : {TARGET_RISK_PER_POSITION:.1%}")
    logger.info(f"Min position     : {MIN_POSITION_PCT:.1%} of equity")
    logger.info(f"Max position     : {MAX_POSITION_PCT:.1%} of equity")
    logger.info(f"Crypto cap       : {MAX_CRYPTO_ALLOCATION_PCT:.0%} of equity")

    # â”€â”€ Load inputs â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    try:
        ranked_list = load_momentum_ranked(args.as_of_date)
    except SystemExit:
        return 1

    existing_positions = load_portfolio_state(args.portfolio_state)

    # â”€â”€ Compute position sizes â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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

    # â”€â”€ Console output â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    print_sizing_summary(sized_positions, summary, top_n=args.top)

    # â”€â”€ Save outputs â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    if args.dry_run:
        logger.info("\nâš   Dry-run mode â€” output files NOT written")
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

    # â”€â”€ Timing & next-step hint â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    elapsed = datetime.now() - start_time
    new_count  = sum(1 for p in sized_positions if p.get("is_new_entry"))
    hold_count = len(sized_positions) - new_count

    logger.info(f"\n{'=' * 70}")
    logger.info("POSITION SIZING COMPLETE")
    logger.info(f"{'=' * 70}")
    logger.info(f"Duration         : {elapsed}")
    logger.info(f"Positions sized  : {len(sized_positions)}  "
                f"(new: {new_count}  |  hold: {hold_count})")
    logger.info(f"Total allocated  : â‚¬{summary['total_allocated_eur']:,.2f} "
                f"({summary['total_allocated_pct']:.1f}%)")
    if not args.dry_run:
        logger.info("Outputs:")
        logger.info(f"  - {PORTFOLIO_DIR / 'position_sizes.json'}")
        logger.info(f"  - {PORTFOLIO_DIR / 'sizing_summary.json'}")
        date_tag = args.as_of_date.replace("-", "")
        logger.info(f"  - {REPORTS_DIR / f'{date_tag}_position_sizes.csv'}")
    logger.info(
        f"Next step : python scripts/09_calculate_stops.py "
        f"--as-of-date {args.as_of_date}"
    )
    logger.info("=" * 70)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        logger.warning("\n\nInterrupted by user")
        sys.exit(1)
    except Exception as exc:
        logger.error(f"Unhandled exception: {exc}", exc_info=True)
        sys.exit(1)
