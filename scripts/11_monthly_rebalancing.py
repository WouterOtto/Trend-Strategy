#!/usr/bin/env python3
"""
Script 11: Monthly Rebalancer
==============================
Generate complete monthly rebalancing recommendations for human review and approval.

Purpose:
    The monthly rebalancer is the central decision hub of the strategy.  It
    aggregates the outputs of all upstream scripts (07 â†’ 10) and produces a
    single, human-readable action plan covering:

        - MANDATORY EXITS  (stop-loss hit, trend reversal, trend weakness)
        - ROTATION EXITS   (position dropped from top-N momentum ranking)
        - NEW ENTRIES      (new top-N candidates not already held)
        - HOLDS            (positions already held that remain in top-N)

    No position sizing or stop-loss calculation is performed here.  Those
    values are read directly from Script 08 and Script 09 output files.
    This script purely assembles, validates, and presents recommendations.

    âš   ALL RECOMMENDATIONS REQUIRE HUMAN APPROVAL BEFORE EXECUTION.
       The system recommends; the human executes.

Rebalancing Schedule:
    Frequency:  Monthly
    Anchor:     Last trading day of each calendar month (market close)
    Execution:  First trading day of the following month (market open/limit)

    Order priority on execution day:
        1. Execute all exits first (free up capital, reduce risk)
        2. Execute new entries with freed capital (limit orders: close + 0.5%)
        3. Set / update stop-loss orders for all positions

Circuit Breakers (automatic halt â€“ checked FIRST):
    1. Portfolio drawdown < âˆ’15%   â†’ halt entries, allow exits only
    2. VIX > 40                    â†’ halt entries until VIX < 30 for 3 days
    3. Max pairwise correlation > 0.85 (top-10 positions) â†’ halt entries
    4. Top-3 concentration > 30%   â†’ halt entries, force rebalance
    5. Data staleness > 3 days     â†’ halt all trading

    Human can override with --override-circuit-breaker "rationale".
    Override is logged and expires after 1 trading day.

Dependencies (must be run before this script, in order):
    01_download_eodhd_bulk.py         (data current)
    02_download_yahoo_fundamentals.py
    03_consolidate_validate_data.py
    04_screen_universe.py
    05_calculate_indicators.py
    06_qualify_trends.py
    07_rank_momentum.py              â† momentum_ranked.json
    08_calculate_position_sizes.py   â† position_sizes.json
    09_calculate_stops.py            â† stop_levels.json
    10_generate_exit_signals.py      â† exit_signals.json

Inputs:
    data/portfolio_state.json                    (current holdings, can be empty/absent)
    data_cache/signals/momentum_ranked.json      (Script 07)
    data_cache/portfolio/position_sizes.json     (Script 08)
    data_cache/portfolio/stop_levels.json        (Script 09)
    data_cache/signals/exit_signals.json         (Script 10)
    data_cache/metadata/last_update.json         (data freshness check)
    config/strategy_parameters.json              (optional, overrides defaults)

Outputs:
    reports/rebalancing/{YYYY-MM}_recommendations.json  (machine-readable, full detail)
    reports/rebalancing/{YYYY-MM}_recommendations.csv   (human-readable action table)
    logs/monthly_rebalancing_{timestamp}.log

Execution:
    # Standard monthly run
    python scripts/11_monthly_rebalancing.py \
        --account-equity 50000 \
        --rebalance-date 2026-01-31

    # With VIX for circuit-breaker check
    python scripts/11_monthly_rebalancing.py \
        --account-equity 50000 \
        --rebalance-date 2026-01-31 \
        --vix 22.5

    # Override a triggered circuit breaker (use with care)
    python scripts/11_monthly_rebalancing.py \
        --account-equity 50000 \
        --rebalance-date 2026-01-31 \
        --override-circuit-breaker "VIX spike is data error, manually verified"

    # Dry run (compute only, no files written)
    python scripts/11_monthly_rebalancing.py \
        --account-equity 50000 \
        --rebalance-date 2026-01-31 \
        --dry-run

Architecture: v3.2 (Feb 2026)
"""

import os
import sys
import json
import logging
import argparse
import calendar
import csv
from copy import deepcopy
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import numpy as np

# ============================================================================
# ENCODING CONFIGURATION
# ============================================================================

# Ensure UTF-8 encoding for terminal output (fixes € and box drawing characters)
if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except AttributeError:
        # Python < 3.7 fallback
        import codecs
        sys.stdout = codecs.getwriter('utf-8')(sys.stdout.buffer, 'strict')

# ============================================================================
# PATH CONFIGURATION
# ============================================================================

PROJECT_ROOT     = Path(__file__).parent.parent
DATA_DIR         = PROJECT_ROOT / "data"
DATA_CACHE_DIR   = PROJECT_ROOT / "data_cache"
DATA_LOAD_DIR    = PROJECT_ROOT.parent / "data_load" / "data_cache"
# Input
SIGNALS_DIR      = DATA_CACHE_DIR / "signals"
PORTFOLIO_DIR    = DATA_CACHE_DIR / "portfolio"
INDICATORS_DIR   = DATA_CACHE_DIR / "indicators"
CONSOLIDATED_DIR = DATA_CACHE_DIR / "consolidated"
METADATA_DIR     = DATA_LOAD_DIR / "metadata"
# Output
REPORTS_DIR      = PROJECT_ROOT / "reports" / "rebalancing"
LOG_DIR          = PROJECT_ROOT / "logs"
CONFIG_DIR       = PROJECT_ROOT / "config"

# ============================================================================
# STRATEGY CONSTANTS  (match architecture v3.2 â€“ do NOT change without review)
# ============================================================================

# Position count thresholds by account size (EUR)
POSITION_COUNT_SCHEDULE: List[Tuple[float, int]] = [
    (25_000,  10),
    (50_000,  15),
    (100_000, 20),
    (float("inf"), 25),
]

# Asset class allocation targets (by portfolio value %)
ASSET_CLASS_TARGETS = {
    "etf":    {"min": 0,  "target": 38, "max": 45},
    "stock":  {"min": 0,  "target": 42, "max": 50},
    "crypto": {"min": 0,  "target": 15, "max": 20},
}

# Tolerance for target deviation before rebalancing (percentage points)
ALLOCATION_TOLERANCE = 10.0  # e.g., 38% target Â± 10pp = 28-48% acceptable range

# Circuit-breaker thresholds
CB_MAX_DRAWDOWN_PCT      = -0.15   # âˆ’15%
CB_VIX_HALT_LEVEL        = 40.0
CB_VIX_RESUME_LEVEL      = 30.0
CB_MAX_CORRELATION       = 0.85
CB_MAX_TOP3_CONCENTRATION = 0.30   # 30%
CB_MAX_DATA_STALENESS_DAYS = 3

# Exit priority thresholds
MANDATORY_EXIT_PRIORITIES = {1, 2, 3}   # stop-loss, reversal, weakness
ROTATION_EXIT_PRIORITY    = 4

# Order types (documentation strings, not executable)
ORDER_MANDATORY_EXIT = "market_order_at_open"
ORDER_ROTATION_EXIT  = "market_order_at_close"
ORDER_NEW_ENTRY      = "limit_order_close_plus_0.5pct"

# Data staleness warning (days)
DATA_STALENESS_WARN_DAYS = 3

# ============================================================================
# LOGGING
# ============================================================================

def setup_logging(rebalance_date: str) -> logging.Logger:
    """Configure file + console logging for this run."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file  = LOG_DIR / f"monthly_rebalancing_{rebalance_date}_{timestamp}.log"

    logger = logging.getLogger("monthly_rebalancing")
    logger.setLevel(logging.DEBUG)

    # File handler â€“ full detail
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))

    # Console handler â€“ INFO and above
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(levelname)-8s | %(message)s"))

    logger.addHandler(fh)
    logger.addHandler(ch)

    logger.info(f"Log file: {log_file}")
    return logger


# Module-level logger (populated in main)
logger: logging.Logger = logging.getLogger("monthly_rebalancing")


# ============================================================================
# HELPERS â€“ DATE & TRADING CALENDAR
# ============================================================================

def _is_weekend(d: date) -> bool:
    return d.weekday() >= 5


# Approximate US/EU major holiday dates (extend as needed)
_FIXED_HOLIDAYS: Dict[str, List[Tuple[int, int]]] = {
    # (month, day)  â€“ year-independent
    "US": [
        (1,  1),   # New Year's Day
        (7,  4),   # Independence Day
        (11, 11),  # Veterans Day
        (12, 25),  # Christmas Day
        (12, 26),  # Boxing Day (observed)
    ],
    "EU": [
        (1,  1),
        (5,  1),   # Labour Day
        (12, 25),
        (12, 26),
    ],
}


def is_trading_day(d: date, region: str = "US") -> bool:
    """
    Lightweight trading-day check.
    Returns False for weekends and known fixed holidays.
    For production use with pandas_market_calendars if available.
    """
    if _is_weekend(d):
        return False
    holidays = _FIXED_HOLIDAYS.get(region, [])
    if (d.month, d.day) in holidays:
        return False
    return True


def get_last_trading_day_of_month(year: int, month: int) -> date:
    """
    Return the last trading day of the given month.

    Walk backwards from the last calendar day until a trading day is found.

    Example:
        January 2026 â†’ January 31, 2026 (Saturday) â†’ January 29, 2026 (Thursday)
    """
    last_cal_day = calendar.monthrange(year, month)[1]
    candidate = date(year, month, last_cal_day)
    while not is_trading_day(candidate):
        candidate -= timedelta(days=1)
    return candidate


def get_execution_date(rebalance_date: date) -> date:
    """
    First trading day of the month following rebalance_date.
    """
    # Move one calendar day forward, then keep advancing until a trading day
    next_day = rebalance_date + timedelta(days=1)
    while not is_trading_day(next_day):
        next_day += timedelta(days=1)
    return next_day


def validate_rebalance_date(rebalance_date_str: str) -> date:
    """
    Parse and validate the rebalance date.

    Rules:
      - Must be parseable as YYYY-MM-DD
      - Must not be in the future
      - Warns if not the last trading day of its month (e.g. early run)

    Returns: date object
    """
    try:
        rd = datetime.strptime(rebalance_date_str, "%Y-%m-%d").date()
    except ValueError:
        logger.error(f"Invalid rebalance date format '{rebalance_date_str}'. Expected YYYY-MM-DD.")
        sys.exit(1)

    if rd > date.today():
        logger.error(f"Rebalance date {rd} is in the future. Aborting.")
        sys.exit(1)

    expected_last_td = get_last_trading_day_of_month(rd.year, rd.month)
    if rd != expected_last_td:
        logger.warning(
            f"Rebalance date {rd} is not the last trading day of "
            f"{rd.strftime('%B %Y')} (expected {expected_last_td}). "
            "Proceeding anyway â€” confirm this is intentional."
        )

    return rd


# ============================================================================
# HELPERS â€“ MAX POSITIONS
# ============================================================================

def get_max_positions(account_equity: float) -> int:
    """
    Determine maximum number of simultaneous positions based on account size.

    Scale:
        < â‚¬25 000  â†’ 10 positions
        < â‚¬50 000  â†’ 15 positions
        < â‚¬100 000 â†’ 20 positions
        â‰¥ â‚¬100 000 â†’ 25 positions
    """
    for threshold, count in POSITION_COUNT_SCHEDULE:
        if account_equity < threshold:
            return count
    return POSITION_COUNT_SCHEDULE[-1][1]


# ============================================================================
# DATA LOADERS
# ============================================================================

def load_portfolio_state() -> Dict:
    """
    Load current portfolio holdings from data/portfolio_state.json.

    Format expected:
        {
            "AAPL.US": {
                "entry_price":           150.0,
                "entry_date":            "2025-12-01",
                "shares":                10,
                "current_value":         1600.0,
                "unrealized_pnl":        100.0,
                "unrealized_pnl_pct":    6.67,
                "current_stop_price":    135.0,
                "initial_stop_price":    135.0,
                "trailing_stop_price":   null,
                "stop_type":             "initial",
                "stop_last_update_date": "2025-12-29",
                "data_last_update":      "2026-01-31T00:00:00",
                "is_new_entry":          false
            },
            ...
        }

    Returns empty dict if file does not exist (first run).
    """
    state_file = DATA_DIR / "portfolio_state.json"

    if not state_file.exists():
        logger.info("No portfolio_state.json found â€” assuming empty portfolio (first run).")
        return {}

    try:
        with open(state_file, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error(f"Failed to load portfolio state: {exc}")
        sys.exit(1)

    # â”€â”€ Normalise: accept several portfolio_state.json layouts â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    #
    # Layout A (canonical â€“ dict of symbol â†’ position dict):
    #   { "AAPL.US": { "entry_price": ..., "shares": ... }, ... }
    #
    # Layout B (nested under a "positions" key):
    #   { "positions": { "AAPL.US": { ... } }, "_meta": { ... } }
    #
    # Layout C (flat list of symbols â€“ minimal state, no details):
    #   { "AAPL.US": "active", "MSFT.US": "active", ... }
    #
    # In all cases we return a dict of  symbol â†’ dict  (possibly empty dict
    # for entries that carry no detail).

    if not isinstance(raw, dict):
        logger.warning(
            f"portfolio_state.json has unexpected top-level type "
            f"({type(raw).__name__}). Treating as empty portfolio."
        )
        return {}

    # Layout B: if the file has a "positions" key whose value is a dict,
    # use that sub-dict as the positions map.
    if "positions" in raw and isinstance(raw.get("positions"), dict):
        raw = raw["positions"]

    # Normalise: skip metadata keys (_meta, etc.) and coerce non-dict
    # values (strings, numbers) to empty dicts so downstream code always
    # receives  {symbol: dict}.
    normalised: Dict = {}
    skipped_meta = 0
    coerced_flat = 0

    for key, value in raw.items():
        if key.startswith("_"):          # e.g. "_meta", "_version"
            skipped_meta += 1
            continue
        if isinstance(value, dict):
            normalised[key] = value
        else:
            # Value is a string/number (e.g. "active", 1) â€” treat as a
            # minimal position record with no detail fields.
            normalised[key] = {}
            coerced_flat += 1

    if skipped_meta:
        logger.debug(f"  Skipped {skipped_meta} metadata key(s) from portfolio_state.json.")
    if coerced_flat:
        logger.warning(
            f"  {coerced_flat} position(s) in portfolio_state.json have no detail fields "
            f"(e.g. entry_price, shares). They will be treated as bare symbol entries. "
            f"Consider enriching portfolio_state.json with full position data."
        )

    logger.info(f"Loaded portfolio state: {len(normalised)} positions from {state_file}")
    return normalised


def load_momentum_ranked(as_of_date: str) -> List[Dict]:
    """
    Load the momentum-ranked universe from Script 07 output.

    File: data_cache/signals/momentum_ranked.json
    Format: {"metadata": {...}, "ranked": [...]}

    Returns: ranked list (sorted descending by momentum_score, rank already assigned)
    Exits if file missing or date mismatch.
    """
    ranked_file = SIGNALS_DIR / "momentum_ranked.json"

    if not ranked_file.exists():
        logger.error(f"momentum_ranked.json not found at {ranked_file}. Run Script 07 first.")
        sys.exit(1)

    try:
        with open(ranked_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error(f"Failed to load momentum_ranked.json: {exc}")
        sys.exit(1)

    ranked_list = data.get("ranked", [])
    meta        = data.get("metadata", {})
    file_date   = meta.get("as_of_date", "")

    if not ranked_list:
        logger.error("momentum_ranked.json contains no ranked instruments. Aborting.")
        sys.exit(1)

    if file_date and file_date != as_of_date:
        logger.warning(
            f"momentum_ranked.json as_of_date ({file_date}) â‰  rebalance_date ({as_of_date}). "
            "Ensure Script 07 was run with the same --as-of-date."
        )

    logger.info(f"Loaded {len(ranked_list)} ranked instruments (as_of: {file_date})")
    return ranked_list


def load_position_sizes() -> Dict:
    """
    Load pre-calculated position sizes from Script 08 output.

    File: data_cache/portfolio/position_sizes.json
    Format: {"metadata": {...}, "positions": {symbol: {...}}}

    Returns: positions dict (symbol â†’ sizing details)
    """
    sizes_file = PORTFOLIO_DIR / "position_sizes.json"

    if not sizes_file.exists():
        logger.error(f"position_sizes.json not found at {sizes_file}. Run Script 08 first.")
        sys.exit(1)

    try:
        with open(sizes_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error(f"Failed to load position_sizes.json: {exc}")
        sys.exit(1)

    positions = data.get("positions", {})
    meta      = data.get("metadata", {})

    if not positions:
        logger.error("position_sizes.json contains no positions. Run Script 08 first.")
        sys.exit(1)

    logger.info(
        f"Loaded {len(positions)} sized positions "
        f"(account_equity: â‚¬{meta.get('account_equity', 0):,.0f}, "
        f"as_of: {meta.get('as_of_date', 'N/A')})"
    )
    return positions


def load_stop_levels() -> Dict:
    """
    Load pre-calculated stop levels from Script 09 output.

    File: data_cache/portfolio/stop_levels.json
    Format: {"metadata": {...}, "stops": {symbol: {...}}}

    Returns: stops dict (symbol â†’ stop details)
    """
    stops_file = PORTFOLIO_DIR / "stop_levels.json"

    if not stops_file.exists():
        logger.error(f"stop_levels.json not found at {stops_file}. Run Script 09 first.")
        sys.exit(1)

    try:
        with open(stops_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error(f"Failed to load stop_levels.json: {exc}")
        sys.exit(1)

    stops = data.get("stops", {})

    if not stops:
        logger.error("stop_levels.json contains no stop levels. Run Script 09 first.")
        sys.exit(1)

    logger.info(f"Loaded {len(stops)} stop levels from {stops_file}")
    return stops


def load_exit_signals() -> Dict:
    """
    Load exit signals from Script 10 output.

    File: data_cache/signals/exit_signals.json
    Format:
        {
            "metadata": {...},
            "exit_signals": {
                "AAPL.US": {
                    "exit": true,
                    "reason": "stop_loss_hit",
                    "priority": 1,
                    "stop_price": 135.0,
                    "close_price": 132.5,
                    "exit_type": "market_order_next_open",
                    "detail": "..."
                }
            }
        }

    Returns empty dict if no exits are signalled or file is absent.
    Script 10 only writes symbols that triggered an exit condition.
    """
    signals_file = SIGNALS_DIR / "exit_signals.json"

    if not signals_file.exists():
        logger.info("No exit_signals.json found â€” assuming zero active exit signals.")
        return {}

    try:
        with open(signals_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(f"Failed to load exit_signals.json ({exc}) â€” treating as no exits.")
        return {}

    signals = data.get("exit_signals", {})
    logger.info(f"Loaded {len(signals)} exit signal(s) from {signals_file}")
    return signals


def load_last_update_dates() -> Dict:
    """
    Load last data update dates per exchange from metadata.

    File: data_cache/metadata/last_update.json
    Format: {"NYSE": "2026-01-31", "NASDAQ": "2026-01-31", ...}
    """
    meta_file = METADATA_DIR / "last_update.json"

    if not meta_file.exists():
        logger.warning("last_update.json not found â€” skipping data freshness check.")
        return {}

    try:
        with open(meta_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(f"Failed to load last_update.json: {exc}")
        return {}


def load_strategy_parameters() -> Dict:
    """
    Load strategy parameters from config file (optional).
    Returns empty dict if absent (all callers use constants as fallback).
    """
    config_file = CONFIG_DIR / "strategy_parameters.json"

    if not config_file.exists():
        return {}

    try:
        with open(config_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def load_active_exchanges() -> set:
    """
    Return the set of exchange codes whose data freshness should be monitored
    by the staleness circuit breaker.

    An exchange is considered "active" only when Script 03 has actually
    produced consolidated data for it.  Exchanges that are configured in
    exchanges.json but have not yet been consolidated must NOT trigger the
    staleness breaker — they have no consolidated data to be stale.

    Resolution order (first source that yields a non-empty set wins):

    1. consolidation_state.json  (written by Script 03 after each run)
       This is the ground truth: it contains exactly the exchanges that have
       consolidated parquet files on disk.  New exchanges appear here
       automatically the first time Script 03 processes them — no manual
       config edit required.

    2. exchanges.json "enabled": true  (manual override)
       Used when consolidation_state.json is absent (e.g. first run, or the
       file was deleted).  Set "enabled": true on each exchange manually
       once you know its data is loaded and consolidated.

    3. Empty set (safe fallback)
       No false positives: the staleness breaker does not fire rather than
       halting on phantom stale data.

    Returns:
        Set of uppercase exchange code strings, e.g. {"NYSE"}.
    """
    # ── Source 1: consolidation_state.json ───────────────────────────────────
    consolidation_state_file = METADATA_DIR / "consolidation_state.json"

    if consolidation_state_file.exists():
        try:
            with open(consolidation_state_file, "r", encoding="utf-8") as f:
                state = json.load(f)

            # An exchange must have a real consolidated date (not the 1900 sentinel)
            consolidated = {
                code
                for code, entry in state.items()
                if isinstance(entry, dict)
                and entry.get("last_consolidated_date", "1900-01-01") > "1900-01-01"
            }

            if consolidated:
                # ── Intersect with exchanges.json consolidation_ready flag ────
                # An exchange may have a past consolidated date in state (from a
                # previous load cycle) but still be mid-reload and not yet ready
                # for staleness monitoring.  The "consolidation_ready": false flag
                # in exchanges.json is the operator signal for this condition.
                # Only exchanges explicitly marked ready (or with no flag, for
                # backwards compatibility) are subject to the staleness breaker.
                try:
                    with open(CONFIG_DIR / "exchanges.json", "r", encoding="utf-8") as f:
                        exc_cfg = json.load(f).get("exchanges", {})

                    not_ready = {
                        code for code, cfg in exc_cfg.items()
                        if cfg.get("consolidation_ready") is False
                    }
                    if not_ready:
                        logger.info(
                            f"Staleness check — skipping exchanges marked "
                            f"consolidation_ready=false: {sorted(not_ready)}"
                        )
                    consolidated -= not_ready

                    pending = set(exc_cfg.keys()) - consolidated - not_ready
                    if pending:
                        logger.debug(
                            f"Staleness check — not yet consolidated (skipped): "
                            f"{sorted(pending)}"
                        )
                except (json.JSONDecodeError, OSError, FileNotFoundError):
                    pass
                # ─────────────────────────────────────────────────────────────

                logger.debug(
                    f"Staleness check — active exchanges "
                    f"(from consolidation_state.json): {sorted(consolidated)}"
                )
                return consolidated

            logger.debug(
                "consolidation_state.json exists but contains no consolidated "
                "exchanges — falling back to exchanges.json"
            )

        except (json.JSONDecodeError, OSError, KeyError) as exc:
            logger.warning(
                f"load_active_exchanges: could not read consolidation_state.json "
                f"({exc}) — falling back to exchanges.json"
            )

    # ── Source 2: exchanges.json "enabled" flag ───────────────────────────────
    config_file = CONFIG_DIR / "exchanges.json"

    if config_file.exists():
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                config = json.load(f)

            all_exchanges = config.get("exchanges", {})
            active = {
                code
                for code, entry in all_exchanges.items()
                if entry.get("enabled", False)
            }

            if active:
                logger.debug(
                    f"Staleness check — active exchanges "
                    f"(from exchanges.json enabled flag): {sorted(active)}"
                )
                return active

            logger.warning(
                "load_active_exchanges: consolidation_state.json absent and no "
                "exchange has 'enabled': true in exchanges.json. "
                "Staleness circuit breaker will not fire. "
                "Run Script 03 to populate consolidation_state.json automatically."
            )
            return set()

        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                f"load_active_exchanges: could not read exchanges.json: {exc}"
            )

    # ── Source 3: safe empty fallback ────────────────────────────────────────
    logger.warning(
        "load_active_exchanges: no exchange source found — "
        "staleness check will not fire."
    )
    return set()


def _count_trading_days(start_dt: datetime, end_dt: datetime, exchange: str = "") -> int:
    """
    Count the number of business days (Mon–Fri) strictly between
    *start_dt* (exclusive) and *end_dt* (inclusive).

    Used by the data-staleness circuit breaker so that a dataset last
    updated on Friday is correctly seen as 1 trading day stale on the
    following Monday, not 3 calendar days stale.

    Crypto exchanges (CC) trade every calendar day; for those the caller
    receives the raw calendar-day count instead.

    Args:
        start_dt:  Baseline datetime (last known data date).
        end_dt:    Reference datetime (rebalance date).
        exchange:  Exchange code — used to detect crypto (all-day) markets.

    Returns:
        Non-negative integer count of intervening trading (or calendar) days.
    """
    if end_dt <= start_dt:
        return 0

    is_crypto = exchange.upper() in {"CC", "CRYPTO"}
    if is_crypto:
        return (end_dt - start_dt).days

    # pd.bdate_range includes both endpoints; subtract 1 to exclude start_dt
    return max(0, len(pd.bdate_range(start=start_dt, end=end_dt)) - 1)


def load_peak_equity() -> Optional[float]:
    """
    Load peak equity from portfolio state history or state file.

    Looks for 'peak_equity' in data/portfolio_state.json or
    data/executions/ trade logs.  Returns None if unavailable
    (circuit breaker drawdown check will be skipped).
    """
    state_file = DATA_DIR / "portfolio_state.json"

    if not state_file.exists():
        return None

    try:
        with open(state_file, "r", encoding="utf-8") as f:
            state = json.load(f)
        # Check for top-level peak_equity metadata key
        if isinstance(state, dict) and "_meta" in state:
            return state["_meta"].get("peak_equity")
    except (json.JSONDecodeError, OSError):
        pass

    return None


def load_return_series_for_correlation(
    symbols: List[str],
    as_of_date: str,
    lookback_days: int = 60
) -> Optional[pd.DataFrame]:
    """
    Load daily close returns for a list of symbols to compute correlation.

    Args:
        symbols:       Symbol list (EODHD format, e.g. "AAPL.US")
        as_of_date:    Reference date (YYYY-MM-DD)
        lookback_days: Number of trading days to use for correlation estimate

    Returns:
        DataFrame with symbols as columns and dates as index, or None if data
        is unavailable for enough symbols.
    """
    end_dt  = datetime.strptime(as_of_date, "%Y-%m-%d")

    series: Dict[str, pd.Series] = {}

    for sym in symbols:
        ind_file = INDICATORS_DIR / f"{sym}_indicators.parquet"
        if not ind_file.exists():
            continue
        try:
            df = pd.read_parquet(ind_file, columns=["close"])
            df.index = pd.to_datetime(df.index)
            df = df[df.index <= end_dt].tail(lookback_days + 1)
            if len(df) < 20:
                continue
            returns = df["close"].pct_change().dropna()
            series[sym] = returns
        except Exception:
            continue

    if len(series) < 2:
        return None

    return pd.DataFrame(series)


# ============================================================================
# ASSET CLASS BALANCING
# ============================================================================

def balance_asset_allocation(
    ranked_universe: List[Dict],
    max_positions:   int,
    position_sizes:  Dict,
    candidate_pool_size: int = None,
    scenario_name: str = "Default",
    diversification_mode: bool = False,  # ADD THIS LINE
) -> List[str]:
    """
    Select top-N positions with asset class balance enforcement.

    Process:
        1. Start with pure top-N by momentum from candidate pool
        2. Calculate asset class allocation (by value, from position_sizes)
        3. If allocation violates max limits or is far from targets:
           - Swap lowest-momentum position in over-represented class
           - With highest-momentum position (not selected) in under-represented class
        4. Repeat until balanced or no beneficial swaps remain

    Args:
        ranked_universe: Full momentum-ranked list (sorted desc by score)
        max_positions:   Maximum number of positions
        position_sizes:  Position sizing dict from Script 08 (contains value_eur)
        candidate_pool_size: Size of candidate pool to search (default: max_positions * 2)
        scenario_name:   Name for logging (e.g., "Pure Momentum", "Force Diversity")

    Returns:
        List of selected symbols (length â‰¤ max_positions), balanced by asset class

    Constraints (enforced in order):
        1. HARD: Respect max limits (45% ETF, 50% stock, 20% crypto)
        2. SOFT: Approach targets (38% ETF, 42% stock, 15% crypto)
        3. TIE-BREAK: Prefer higher momentum when multiple swaps are equivalent
    """
    # Default candidate pool size
    if candidate_pool_size is None:
        candidate_pool_size = max_positions * 2
    
    # â”€â”€ Step 1: Initial selection (pure momentum) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    candidates = ranked_universe[:candidate_pool_size]

    if diversification_mode:
        # DIVERSIFICATION MODE: Apply bonuses for low volatility + ETF/crypto + capital deployment
        logger.info(f"    [{scenario_name}] Applying diversification-aware ranking...")
        
        diversity_ranked = []
        for c in candidates:
            sym = c["symbol"]
            sizing = position_sizes.get(sym, {})
            
            momentum_score = c.get("momentum_score", 0)
            atr_pct = sizing.get("atr_pct", 10.0)
            asset_class = (sizing.get("asset_class") or "stock").lower()
            position_value = sizing.get("position_value_eur", 0)
            
            # AGGRESSIVE bonuses to force ETF inclusion
            volatility_bonus = max(0, (10 - atr_pct) * 15)  # Triple strength
            
            if asset_class == "etf":
                asset_bonus = 150  # Strong boost
            elif asset_class == "crypto":
                asset_bonus = 120
            else:
                asset_bonus = 0
            
            if position_value >= 500:
                capital_bonus = 60  # Triple strength
            elif position_value >= 300:
                capital_bonus = 30
            else:
                capital_bonus = 0
            
            diversity_score = momentum_score + volatility_bonus + asset_bonus + capital_bonus
            
            diversity_ranked.append({
                **c,
                "diversity_score": diversity_score,
                "volatility_bonus": volatility_bonus,
                "asset_bonus": asset_bonus,
                "capital_bonus": capital_bonus,
            })
        
        diversity_ranked.sort(key=lambda x: x["diversity_score"], reverse=True)
        
        logger.info(f"    [{scenario_name}] Top 15 after diversity adjustment:")
        for i, c in enumerate(diversity_ranked[:15], 1):
            sym = c["symbol"]
            sizing = position_sizes.get(sym, {})
            ac = (sizing.get("asset_class") or "?")[:3].upper()
            logger.info(
                f"      {i:>2}. {sym:<15} {ac:<4} "
                f"div_score={c['diversity_score']:>6.1f} "
                f"(mom={c.get('momentum_score', 0):>5.1f} "
                f"+vol={c['volatility_bonus']:>4.1f} "
                f"+ac={c['asset_bonus']:>3.1f} "
                f"+cap={c['capital_bonus']:>3.1f})"
            )
        
        candidates = diversity_ranked
        selected_symbols = [c["symbol"] for c in candidates[:max_positions]]
        logger.info(f"    [{scenario_name}] Initial: top {max_positions} from pool (diversity-adjusted)")
    else:
        selected_symbols = [c["symbol"] for c in candidates[:max_positions]]
        logger.info(f"    [{scenario_name}] Initial: top {max_positions} from pool of {candidate_pool_size}")

    # â”€â”€ Helper: compute allocation percentages â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    def compute_allocation(symbols: List[str]) -> Dict[str, float]:
        """Return {asset_class: value_pct} for given symbol list."""
        total_value = 0.0
        class_values: Dict[str, float] = {"etf": 0.0, "stock": 0.0, "crypto": 0.0}

        for sym in symbols:
            sizing = position_sizes.get(sym)
            if not sizing:
                continue
            value = sizing.get("position_value_eur", 0)
            ac    = (sizing.get("asset_class") or "unknown").lower()
            if ac in class_values:
                class_values[ac] += value
                total_value += value

        if total_value == 0:
            return {k: 0.0 for k in class_values}

        return {k: (v / total_value * 100) for k, v in class_values.items()}

    # â”€â”€ Helper: check if allocation violates hard max limits â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    def violates_max_limits(alloc: Dict[str, float]) -> bool:
        for ac, pct in alloc.items():
            if pct > ASSET_CLASS_TARGETS[ac]["max"]:
                return True
        return False

    # â”€â”€ Helper: compute deviation from targets (lower is better) â”€â”€â”€â”€â”€â”€â”€â”€â”€
    def deviation_from_targets(alloc: Dict[str, float]) -> float:
        """Sum of squared deviations from target percentages."""
        return sum(
            (alloc.get(ac, 0) - targets["target"]) ** 2
            for ac, targets in ASSET_CLASS_TARGETS.items()
        )

    # â”€â”€ Step 2: Iterative rebalancing (up to 50 swaps max) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    MAX_SWAPS = 50
    swaps_made = 0

    for iteration in range(MAX_SWAPS):
        current_alloc = compute_allocation(selected_symbols)
        current_dev   = deviation_from_targets(current_alloc)

        # Check hard constraint violations first
        if violates_max_limits(current_alloc):
            logger.debug(f"  Iteration {iteration+1}: Hard limit violated, forcing swap.")
            force_swap = True
        # Check soft constraint (target deviation beyond tolerance)
        elif current_dev > (ALLOCATION_TOLERANCE ** 2):
            logger.debug(
                f"  Iteration {iteration+1}: Deviation {current_dev:.1f} "
                f"exceeds tolerance {ALLOCATION_TOLERANCE**2:.1f}, attempting swap."
            )
            force_swap = False
        else:
            # Allocation is acceptable â€” stop
            logger.debug(
                f"  Iteration {iteration+1}: Allocation within tolerance. "
                f"Stopping (swaps made: {swaps_made})."
            )
            break

        # â”€â”€ Identify over-/under-represented classes â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        over_repr: List[str] = []
        under_repr: List[str] = []

        for ac, targets in ASSET_CLASS_TARGETS.items():
            pct = current_alloc.get(ac, 0)
            if pct > targets["max"] or pct > targets["target"] + ALLOCATION_TOLERANCE:
                over_repr.append(ac)
            elif pct < targets["target"] - ALLOCATION_TOLERANCE:
                under_repr.append(ac)

        if not over_repr or not under_repr:
            logger.debug(
                f"  Iteration {iteration+1}: No clear over/under classes. Stopping."
            )
            break

        # â”€â”€ Find best swap â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        best_swap: Optional[Tuple[str, str, float]] = None  # (out_sym, in_sym, new_dev)

        # For each over-represented class, try swapping its lowest-momentum member
        for over_class in over_repr:
            # Find selected positions in over_class, sorted by momentum (asc)
            over_positions = [
                sym for sym in selected_symbols
                if (position_sizes.get(sym, {}).get("asset_class") or "").lower() == over_class
            ]
            if not over_positions:
                continue

            # Get momentum rank lookup
            rank_map = {c["symbol"]: c.get("momentum_score", 0) for c in candidates}
            over_positions.sort(key=lambda s: rank_map.get(s, 0))

            out_sym = over_positions[0]  # Lowest momentum in over-class

            # For each under-represented class, try swapping in its highest-momentum candidate
            for under_class in under_repr:
                # Find unselected candidates in under_class, sorted by momentum (desc)
                under_candidates = [
                    c["symbol"] for c in candidates
                    if c["symbol"] not in selected_symbols
                    and (position_sizes.get(c["symbol"], {}).get("asset_class") or "").lower() == under_class
                ]
                # When a hard limit is violated and the candidate pool has no representatives
                # of the under-represented class (e.g., Scenario 1's narrow top-20 pool
                # contains only stocks), expand the search to the full ranked_universe so
                # the constraint can still be corrected.
                if not under_candidates and force_swap:
                    under_candidates = [
                        c["symbol"] for c in ranked_universe
                        if c["symbol"] not in selected_symbols
                        and (position_sizes.get(c["symbol"], {}).get("asset_class") or "").lower() == under_class
                    ]
                    if under_candidates:
                        logger.debug(
                            f"  [{scenario_name}] Pool has no {under_class.upper()} candidates; "
                            f"expanding to full universe for hard-limit correction."
                        )
                if not under_candidates:
                    continue

                in_sym = under_candidates[0]  # Highest momentum not selected

                # Simulate swap
                test_symbols = [s if s != out_sym else in_sym for s in selected_symbols]
                test_alloc   = compute_allocation(test_symbols)
                test_dev     = deviation_from_targets(test_alloc)

                # Check if swap improves allocation.
                # When a HARD limit is violated (force_swap=True), accept any improvement
                # even if the result still violates — otherwise the algorithm can never
                # escape 100%-stock portfolios where each individual swap still leaves
                # stock > 50%.  For soft-constraint swaps keep the strict check.
                if force_swap:
                    swap_improves = test_dev < current_dev
                else:
                    swap_improves = test_dev < current_dev and not violates_max_limits(test_alloc)

                if swap_improves:
                    if best_swap is None or test_dev < best_swap[2]:
                        best_swap = (out_sym, in_sym, test_dev)

        # â”€â”€ Execute best swap if found â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        if best_swap:
            out_sym, in_sym, new_dev = best_swap
            selected_symbols = [s if s != out_sym else in_sym for s in selected_symbols]
            swaps_made += 1
            logger.debug(
                f"  Swap {swaps_made}: OUT {out_sym} "
                f"({position_sizes.get(out_sym, {}).get('asset_class')}) â†’ "
                f"IN {in_sym} "
                f"({position_sizes.get(in_sym, {}).get('asset_class')}). "
                f"Deviation: {current_dev:.1f} â†’ {new_dev:.1f}"
            )
        else:
            logger.debug(
                f"  Iteration {iteration+1}: No beneficial swap found. Stopping."
            )
            break

    # â”€â”€ Step 3: Final allocation report â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    final_alloc = compute_allocation(selected_symbols)
    final_dev   = deviation_from_targets(final_alloc)

    logger.info(
        f"    [{scenario_name}] Complete: {swaps_made} swaps, deviation {final_dev:.1f}"
    )
    logger.debug(f"    [{scenario_name}] Final allocation:")
    for ac in ["etf", "stock", "crypto"]:
        target = ASSET_CLASS_TARGETS[ac]["target"]
        actual = final_alloc.get(ac, 0)
        delta  = actual - target
        logger.debug(
            f"      {ac.upper():<7}: {actual:>5.1f}% (target {target}%, "
            f"Î” {delta:+.1f}pp)"
        )

    if violates_max_limits(final_alloc):
        logger.warning(
            f"    [{scenario_name}] âš   Final allocation VIOLATES hard max limits"
        )

    return selected_symbols


# ============================================================================
# CIRCUIT BREAKERS
# ============================================================================

class CircuitBreaker:
    """
    Check all five circuit-breaker conditions before generating recommendations.

    Any HIGH or CRITICAL breaker halts new entries.
    CRITICAL breakers halt ALL trading.
    """

    def check_all(
        self,
        account_equity:   float,
        peak_equity:      Optional[float],
        current_positions: Dict,
        rebalance_date:   str,
        vix_level:        Optional[float] = None,
    ) -> Dict:
        """
        Run all circuit-breaker checks.

        Returns:
            {
                "halted":        bool,
                "halt_entries":  bool,
                "halt_all":      bool,
                "breakers":      [list of triggered breakers],
                "timestamp":     str,
            }
        """
        triggered = []

        # â”€â”€ Breaker 1: Portfolio Drawdown â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        if peak_equity is not None and peak_equity > 0:
            drawdown = (account_equity / peak_equity) - 1.0
            if drawdown < CB_MAX_DRAWDOWN_PCT:
                triggered.append({
                    "breaker":   "portfolio_drawdown",
                    "severity":  "HIGH",
                    "value":     round(drawdown * 100, 2),
                    "threshold": CB_MAX_DRAWDOWN_PCT * 100,
                    "unit":      "pct",
                    "action":    "halt_entries_5_trading_days",
                    "detail":    (
                        f"Portfolio drawdown {drawdown:.1%} exceeds âˆ’15% threshold. "
                        f"Current equity â‚¬{account_equity:,.0f} vs peak â‚¬{peak_equity:,.0f}."
                    ),
                })
        else:
            logger.debug("Drawdown check skipped â€” peak equity not available.")

        # â”€â”€ Breaker 2: VIX Spike â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        if vix_level is not None:
            if vix_level > CB_VIX_HALT_LEVEL:
                triggered.append({
                    "breaker":   "vix_spike",
                    "severity":  "MEDIUM",
                    "value":     vix_level,
                    "threshold": CB_VIX_HALT_LEVEL,
                    "unit":      "points",
                    "action":    f"halt_entries_until_vix_below_{CB_VIX_RESUME_LEVEL}_for_3_days",
                    "detail":    (
                        f"VIX {vix_level:.1f} exceeds halt level {CB_VIX_HALT_LEVEL}. "
                        "Wait for VIX < 30 on 3 consecutive trading days before resuming entries."
                    ),
                })
        else:
            logger.debug("VIX check skipped â€” no VIX level provided (use --vix <value>).")

        # â”€â”€ Breaker 3: Correlation Breakdown â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        if len(current_positions) >= 10:
            top_10_symbols = sorted(
                current_positions.keys(),
                key=lambda s: (
                    current_positions[s].get("current_value", 0)
                    if isinstance(current_positions[s], dict)
                    else 0
                ),
                reverse=True,
            )[:10]

            returns_df = load_return_series_for_correlation(
                top_10_symbols, rebalance_date, lookback_days=60
            )

            if returns_df is not None and len(returns_df.columns) >= 2:
                corr_matrix = returns_df.corr()
                np.fill_diagonal(corr_matrix.values, np.nan)
                max_corr = float(np.nanmax(corr_matrix.values))

                if max_corr > CB_MAX_CORRELATION:
                    # Find the pair with highest correlation
                    corr_upper = corr_matrix.where(
                        np.triu(np.ones(corr_matrix.shape), k=1).astype(bool)
                    )
                    pair_idx = corr_upper.stack().idxmax()
                    triggered.append({
                        "breaker":   "correlation_breakdown",
                        "severity":  "MEDIUM",
                        "value":     round(max_corr, 4),
                        "threshold": CB_MAX_CORRELATION,
                        "unit":      "correlation",
                        "action":    "halt_entries_review_required",
                        "detail":    (
                            f"Max pairwise correlation {max_corr:.2f} among top-10 positions "
                            f"exceeds {CB_MAX_CORRELATION} threshold. "
                            f"Highest correlated pair: {pair_idx[0]} / {pair_idx[1]}."
                        ),
                    })
            else:
                logger.debug(
                    "Correlation check skipped â€” insufficient indicator data for top-10 positions."
                )
        else:
            logger.debug(
                f"Correlation check skipped â€” only {len(current_positions)} positions "
                f"(need â‰¥ 10)."
            )

        # â”€â”€ Breaker 4: Concentration Creep â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        if current_positions and account_equity > 0:
            position_values = [
                p.get("current_value", 0) if isinstance(p, dict) else 0
                for p in current_positions.values()
            ]
            position_values.sort(reverse=True)
            top3_value = sum(position_values[:3])
            top3_pct   = top3_value / account_equity

            if top3_pct > CB_MAX_TOP3_CONCENTRATION:
                symbols_top3 = sorted(
                    current_positions,
                    key=lambda s: current_positions[s].get("current_value", 0),
                    reverse=True,
                )[:3]
                triggered.append({
                    "breaker":   "concentration_creep",
                    "severity":  "HIGH",
                    "value":     round(top3_pct * 100, 2),
                    "threshold": CB_MAX_TOP3_CONCENTRATION * 100,
                    "unit":      "pct",
                    "action":    "halt_entries_force_rebalancing",
                    "detail":    (
                        f"Top-3 positions ({', '.join(symbols_top3)}) represent "
                        f"{top3_pct:.1%} of portfolio, exceeding 30% limit. "
                        "Exits must be executed before new entries are permitted."
                    ),
                })

        # â”€â”€ Breaker 5: Data Staleness â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        # Fix-1: only check exchanges that are active in config/exchanges.json.
        #        Inactive / historical entries in last_update.json must not
        #        trigger a halt — they are irrelevant to the current strategy.
        # Fix-2: measure staleness in *trading* days (Mon–Fri), not calendar
        #        days.  Without this, a dataset last updated on Friday shows
        #        3 calendar days stale on Monday and trips the breaker falsely.
        rebalance_dt     = datetime.strptime(rebalance_date, "%Y-%m-%d")
        last_updates     = load_last_update_dates()
        active_exchanges = load_active_exchanges()

        stale_exchanges = []
        for exchange, last_date_str in last_updates.items():

            # Fix-1: skip exchanges not present in exchanges.json
            if active_exchanges and exchange not in active_exchanges:
                logger.debug(
                    f"Staleness check: skipping inactive exchange {exchange!r} "
                    f"(not in exchanges.json)"
                )
                continue

            try:
                last_dt    = datetime.strptime(last_date_str, "%Y-%m-%d")
                # Fix-2: trading-day count (crypto uses calendar days)
                days_stale = _count_trading_days(last_dt, rebalance_dt, exchange)

                if days_stale > CB_MAX_DATA_STALENESS_DAYS:
                    stale_exchanges.append((exchange, last_date_str, days_stale))
            except ValueError:
                continue

        if stale_exchanges:
            detail_parts = [
                f"{exch} ({last} â€” {days}d stale)" for exch, last, days in stale_exchanges
            ]
            triggered.append({
                "breaker":   "data_staleness",
                "severity":  "CRITICAL",
                "value":     max(days for _, _, days in stale_exchanges),
                "threshold": CB_MAX_DATA_STALENESS_DAYS,
                "unit":      "days",
                "action":    "halt_all_trading",
                "detail":    (
                    f"Stale data detected for exchanges: {', '.join(detail_parts)}. "
                    "Run Script 01 (--mode incremental) and rerun Scripts 03â€“10 before proceeding."
                ),
            })

        # â”€â”€ Summary â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        halt_all     = any(b["severity"] == "CRITICAL" for b in triggered)
        halt_entries = halt_all or any(b["severity"] in {"HIGH", "MEDIUM"} for b in triggered)

        result = {
            "halted":       halt_all or halt_entries,
            "halt_entries": halt_entries,
            "halt_all":     halt_all,
            "breakers":     triggered,
            "timestamp":    datetime.now().isoformat(),
        }

        if triggered:
            logger.warning(
                f"âš   {len(triggered)} circuit breaker(s) triggered. "
                f"halt_all={halt_all}, halt_entries={halt_entries}."
            )
            for b in triggered:
                logger.warning(
                    f"   [{b['severity']}] {b['breaker']}: {b['detail']}"
                )
        else:
            logger.info("âœ“ Circuit breakers: all clear.")

        return result


# ============================================================================
# CORE REBALANCING LOGIC
# ============================================================================

def identify_mandatory_exits(
    current_positions: Dict,
    exit_signals:      Dict,
) -> List[Dict]:
    """
    Extract priority 1â€“3 exit signals for symbols currently held.

    Priority map:
        1 â†’ stop_loss_hit
        2 â†’ trend_reversal  (SMA death cross)
        3 â†’ trend_weakness  (ADX < 15 for 3 consecutive days)

    Returns list of exit action dicts, sorted by priority ascending.
    """
    mandatory: List[Dict] = []

    for symbol, signal in exit_signals.items():
        if symbol not in current_positions:
            logger.debug(f"Exit signal for {symbol} ignored â€” not in current portfolio.")
            continue

        priority = signal.get("priority")
        if priority not in MANDATORY_EXIT_PRIORITIES:
            continue

        position = current_positions[symbol]
        mandatory.append({
            "action":         "EXIT",
            "exit_type":      "mandatory",
            "symbol":         symbol,
            "reason":         signal.get("reason", "unknown"),
            "priority":       priority,
            "detail":         signal.get("detail", ""),
            "order_type":     ORDER_MANDATORY_EXIT,
            # Position context
            "shares":         position.get("shares", 0),
            "entry_price":    position.get("entry_price"),
            "entry_date":     position.get("entry_date"),
            "current_value":  position.get("current_value"),
            "unrealized_pnl": position.get("unrealized_pnl"),
            "current_stop":   position.get("current_stop_price"),
        })

    mandatory.sort(key=lambda x: x["priority"])
    return mandatory


def _load_indicators_from_parquet(symbol: str, as_of_date: str) -> Dict:
    """
    Fallback: read the last available indicators for a symbol that has dropped
    out of the qualified universe.

    Lookup order:
      1. Indicator parquet (data_cache/indicators/{symbol}_indicators.parquet)
         Written by Script 05; has pre-computed adx_14 and atr_20_pct.
      2. Consolidated parquet (data_cache/consolidated/{symbol}.parquet)
         Written by Script 03; has raw OHLCV. ATR is computed on-the-fly;
         ADX is left as None (requires full DI calculation, not worthwhile here).

    Returns an empty dict if neither file is found or readable.
    """
    as_of_dt = pd.Timestamp(as_of_date)

    # ── Primary: indicator parquet ─────────────────────────────────────────
    ind_file = INDICATORS_DIR / f"{symbol}_indicators.parquet"
    if ind_file.exists():
        try:
            # Include sma_200 so we can recompute the same momentum score formula
            # used by Script 07: momentum_score = ((close - sma_200) / sma_200) * 100
            df = pd.read_parquet(ind_file, columns=["close", "sma_200", "adx_14", "atr_20_pct"])
            df.index = pd.to_datetime(df.index)
            df = df[df.index <= as_of_dt]
            if not df.empty:
                last = df.iloc[-1]
                close_val  = last.get("close")
                sma200_val = last.get("sma_200")
                # Recompute momentum_score using Script 07's primary formula
                if (close_val is not None and not pd.isna(close_val) and
                        sma200_val is not None and not pd.isna(sma200_val) and
                        float(sma200_val) != 0):
                    momentum_score = round(
                        ((float(close_val) - float(sma200_val)) / float(sma200_val)) * 100, 4
                    )
                else:
                    momentum_score = None
                return {
                    "adx_14":         None if pd.isna(last.get("adx_14"))     else float(last["adx_14"]),
                    "atr_20_pct":     None if pd.isna(last.get("atr_20_pct")) else float(last["atr_20_pct"]),
                    "momentum_score": momentum_score,
                    "close":          None if (close_val is None or pd.isna(close_val)) else float(close_val),
                }
        except Exception:
            pass  # fall through to consolidated

    # ── Fallback: consolidated OHLCV parquet — compute ATR on-the-fly ─────
    con_file = CONSOLIDATED_DIR / f"{symbol}.parquet"
    if con_file.exists():
        try:
            df = pd.read_parquet(con_file, columns=["high", "low", "close"])
            df.index = pd.to_datetime(df.index)
            df = df[df.index <= as_of_dt].tail(60)  # 60 bars is enough for 20-period ATR
            if len(df) >= 20:
                # True Range
                prev_close = df["close"].shift(1)
                tr = pd.concat([
                    df["high"] - df["low"],
                    (df["high"] - prev_close).abs(),
                    (df["low"]  - prev_close).abs(),
                ], axis=1).max(axis=1)
                atr_20 = tr.rolling(20).mean().iloc[-1]
                close  = df["close"].iloc[-1]
                if close > 0 and not pd.isna(atr_20):
                    return {
                        "adx_14":         None,   # Not computable from OHLCV without full DI series
                        "atr_20_pct":     round(float(atr_20 / close * 100), 4),
                        "momentum_score": None,
                        "close":          round(float(close), 4),
                    }
        except Exception:
            pass

    return {}


def identify_rotation_exits(
    current_positions:      Dict,
    top_n_symbols:          List[str],
    mandatory_exit_symbols: List[str],
    ranked_universe:        List[Dict] = None,
    rebalance_date:         str = None,
) -> List[Dict]:
    """
    Identify positions that have dropped out of the top-N momentum ranking.

    These are positions that:
      - Are currently held
      - Are NOT in the new top-N list
      - Do NOT have a mandatory exit signal (already captured in mandatory exits)

    Order type: market_order_at_close (less urgent than mandatory exits).

    Args:
        ranked_universe:  Full momentum-ranked list; used to enrich exit records
                          with current momentum_score, adx_14, and atr_20_pct.
        rebalance_date:   ISO date string (YYYY-MM-DD); used as the as-of date
                          when falling back to per-symbol parquet files for
                          symbols that have dropped out of the qualified universe.
    """
    top_n_set          = set(top_n_symbols)
    mandatory_exit_set = set(mandatory_exit_symbols)
    rotation_exits: List[Dict] = []

    # Primary lookup: symbols still present in the ranked/qualified universe
    indicator_lookup: Dict[str, Dict] = {}
    if ranked_universe:
        for row in ranked_universe:
            sym = row.get("symbol")
            if sym:
                indicator_lookup[sym] = row

    for symbol, position in current_positions.items():
        if symbol in top_n_set:
            continue   # Still in top-N → hold
        if symbol in mandatory_exit_set:
            continue   # Already captured as mandatory exit

        # Primary: look up from ranked universe
        ind = indicator_lookup.get(symbol)

        # Fallback: symbol has left the qualified universe entirely; read parquet
        if ind is None and rebalance_date:
            ind = _load_indicators_from_parquet(symbol, rebalance_date)
        else:
            ind = ind or {}

        # Compute current value & P&L from latest close (ranked universe or parquet)
        shares      = position.get("shares", 0)
        entry_price = position.get("entry_price")
        current_close = ind.get("close")   # None if not available
        if current_close and shares and entry_price:
            current_value  = round(current_close * shares, 2)
            unrealized_pnl = round((current_close - entry_price) * shares, 2)
        else:
            # Fall back to portfolio_state values (may be stale)
            current_value  = position.get("current_value")
            unrealized_pnl = position.get("unrealized_pnl")

        # Derive current price per share (needed for the exits report table)
        # current_close is the authoritative source; fall back to current_value / shares
        price_is_stale = False
        if current_close is not None:
            current_price_per_share = current_close
        elif current_value is not None and shares and shares > 0:
            current_price_per_share = round(current_value / shares, 4)
            # Flag as stale: derived from portfolio_state value, not a live market close.
            # This typically produces curr_px == entry_px when portfolio_state hasn't
            # been refreshed, causing the misleading "Buy Px = Curr Px" appearance.
            price_is_stale = True
        else:
            current_price_per_share = None

        rotation_exits.append({
            "action":          "EXIT",
            "exit_type":       "rotation",
            "symbol":          symbol,
            "reason":          "dropped_from_top_n",
            "priority":        ROTATION_EXIT_PRIORITY,
            "detail":          f"No longer ranked in top-{len(top_n_symbols)} by momentum score.",
            "order_type":      ORDER_ROTATION_EXIT,
            # Position context
            "shares":          shares,
            "entry_price":     entry_price,
            "entry_date":      position.get("entry_date"),
            # current_price: price per share at rebalance date (for report column Curr Px)
            "current_price":          current_price_per_share,
            # Flag stale price: True when live close was unavailable and value was derived
            # from portfolio_state (may equal entry_price if state was never refreshed).
            "current_price_is_stale": price_is_stale,
            "current_value":   current_value,
            "unrealized_pnl":  unrealized_pnl,
            "current_stop":    position.get("current_stop_price"),
            # Current technical indicators (ranked universe or parquet fallback)
            "momentum_score":  ind.get("momentum_score"),
            "adx_14":          ind.get("adx_14"),
            "atr_20_pct":      ind.get("atr_20_pct"),
        })

    return rotation_exits


def identify_new_entries(
    top_n_symbols:     List[str],
    current_positions: Dict,
    all_exit_symbols:  List[str],
    position_sizes:    Dict,
    stop_levels:       Dict,
    ranked_universe:   List[Dict],
) -> List[Dict]:
    """
    Identify new positions to open this rebalancing cycle.

    A symbol is a new entry if:
      - It is in the top-N momentum list
      - It is NOT currently held
      - It has been sized by Script 08 (in position_sizes)
      - It has a stop level from Script 09 (in stop_levels)

    Returns list of entry dicts, sorted ascending by momentum rank.
    """
    current_set  = set(current_positions.keys())
    exit_set     = set(all_exit_symbols)

    # Build rank lookup from ranked universe
    rank_lookup: Dict[str, Dict] = {r["symbol"]: r for r in ranked_universe}

    new_entries: List[Dict] = []

    for symbol in top_n_symbols:
        if symbol in current_set and symbol not in exit_set:
            continue   # Already held with no exit signal â†’ hold, not new entry

        if symbol not in position_sizes:
            logger.warning(
                f"New entry candidate {symbol} not found in position_sizes.json â€” skipping. "
                "Ensure Scripts 08/09 were run with the same --as-of-date and --account-equity."
            )
            continue

        sizing = position_sizes[symbol]
        stop   = stop_levels.get(symbol, {})
        rank   = rank_lookup.get(symbol, {})

        new_entries.append({
            "action":          "BUY",
            "symbol":          symbol,
            "name":            sizing.get("name", ""),
            "exchange":        sizing.get("exchange", ""),
            "sector":          sizing.get("sector", ""),
            "asset_class":     sizing.get("asset_class", ""),
            # Momentum context
            "momentum_rank":   sizing.get("rank"),
            "momentum_score":  sizing.get("momentum_score"),
            "roc_20d":         sizing.get("roc_20d"),
            "roc_60d":         sizing.get("roc_60d"),
            "roc_120d":        sizing.get("roc_120d"),
            # Position sizing (from Script 08)
            "shares":          sizing.get("shares"),
            "entry_price":     sizing.get("entry_price"),
            "limit_price":     sizing.get("limit_price"),
            "position_value_eur": sizing.get("position_value_eur"),
            "position_pct":    sizing.get("position_pct"),
            "atr_20_pct":      sizing.get("atr_20_pct"),
            # Stop levels (from Script 09)
            "initial_stop":    stop.get("stop_price"),
            "stop_type":       stop.get("type", "initial"),
            "stop_distance_pct": stop.get("stop_distance_pct"),
            # Technical context
            "sma_50":          sizing.get("sma_50"),
            "sma_200":         sizing.get("sma_200"),
            "adx_14":          sizing.get("adx_14"),
            # Order instruction
            "order_type":      ORDER_NEW_ENTRY,
            "order_note":      "Place limit order at limit_price on execution day (first trading day of next month).",
        })

    new_entries.sort(key=lambda x: (x.get("momentum_rank") or 9999))
    return new_entries


def identify_holds(
    current_positions: Dict,
    top_n_symbols:     List[str],
    all_exit_symbols:  List[str],
    stop_levels:       Dict,
) -> List[Dict]:
    """
    Identify positions that should be held unchanged.

    A symbol is a hold if:
      - It is currently held
      - It remains in the top-N list
      - It has no mandatory exit signal

    Returns list of hold dicts sorted by current_value descending.
    """
    exit_set = set(all_exit_symbols)
    top_set  = set(top_n_symbols)
    holds: List[Dict] = []

    for symbol, position in current_positions.items():
        if symbol not in top_set:
            continue   # Not in top-N â†’ rotation exit
        if symbol in exit_set:
            continue   # Has exit signal

        stop = stop_levels.get(symbol, {})

        holds.append({
            "action":             "HOLD",
            "symbol":             symbol,
            "shares":             position.get("shares", 0),
            "entry_price":        position.get("entry_price"),
            "entry_date":         position.get("entry_date"),
            "current_value":      position.get("current_value"),
            "unrealized_pnl":     position.get("unrealized_pnl"),
            "unrealized_pnl_pct": position.get("unrealized_pnl_pct"),
            "current_stop":       stop.get("stop_price") or position.get("current_stop_price"),
            "stop_type":          stop.get("type", "initial"),
            "trailing_active":    stop.get("active", False) if stop.get("type") == "trailing" else False,
        })

    holds.sort(key=lambda x: (x.get("current_value") or 0), reverse=True)
    return holds


def compute_capital_summary(
    account_equity:   float,
    holds:            List[Dict],
    new_entries:      List[Dict],
    mandatory_exits:  List[Dict],
    rotation_exits:   List[Dict],
) -> Dict:
    """
    Compute post-rebalancing capital allocation summary.

    Approximates capital freed by exits and consumed by new entries.
    Actual values depend on fill prices and are indicative only.
    """
    # Capital currently deployed in holds (approximate â€” uses current_value)
    holds_value = sum(h.get("current_value") or 0 for h in holds)

    # Capital freed by exits (approximate â€” uses current_value)
    exits_freed = sum(
        e.get("current_value") or (
            (e.get("shares") or 0) * (e.get("current_stop") or e.get("entry_price") or 0)
        )
        for e in mandatory_exits + rotation_exits
    )

    # Capital required for new entries (approximate â€” uses position_value_eur)
    entries_required = sum(e.get("position_value_eur") or 0 for e in new_entries)

    cash_before  = account_equity - holds_value - exits_freed  # rough estimate
    cash_after   = cash_before + exits_freed - entries_required

    total_deployed = holds_value + entries_required
    total_pct      = (total_deployed / account_equity * 100) if account_equity > 0 else 0.0
    cash_pct       = (cash_after / account_equity * 100)    if account_equity > 0 else 0.0

    return {
        "account_equity_eur":        round(account_equity, 2),
        "holds_value_eur":           round(holds_value, 2),
        "exits_freed_eur_approx":    round(exits_freed, 2),
        "entries_required_eur":      round(entries_required, 2),
        "total_deployed_after_eur":  round(total_deployed, 2),
        "total_deployed_after_pct":  round(total_pct, 2),
        "cash_remaining_approx_eur": round(cash_after, 2),
        "cash_remaining_approx_pct": round(cash_pct, 2),
        "note": (
            "Capital estimates are approximations based on close prices. "
            "Actual execution values will differ. Always verify before placing orders."
        ),
    }


def compute_asset_class_breakdown(
    holds:       List[Dict],
    new_entries: List[Dict],
    account_equity: float,
) -> Dict:
    """
    Compute post-rebalancing allocation by asset class.
    """
    breakdown: Dict[str, Dict] = {}

    all_positions = [
        {"asset_class": h.get("asset_class", "unknown"), "value": h.get("current_value") or 0}
        for h in holds
    ] + [
        {"asset_class": e.get("asset_class", "unknown"), "value": e.get("position_value_eur") or 0}
        for e in new_entries
    ]

    for item in all_positions:
        ac = item["asset_class"] or "unknown"
        if ac not in breakdown:
            breakdown[ac] = {"value_eur": 0.0, "pct": 0.0, "count": 0}
        breakdown[ac]["value_eur"] += item["value"]
        breakdown[ac]["count"]     += 1

    for ac in breakdown:
        breakdown[ac]["value_eur"] = round(breakdown[ac]["value_eur"], 2)
        breakdown[ac]["pct"]       = round(
            breakdown[ac]["value_eur"] / account_equity * 100, 2
        ) if account_equity > 0 else 0.0

    return breakdown


# ============================================================================
# MAIN ORCHESTRATOR
# ============================================================================

def generate_rebalancing_recommendations(
    account_equity:             float,
    rebalance_date:             str,
    vix_level:                  Optional[float],
    override_circuit_breaker:   Optional[str],
    dry_run:                    bool,
) -> Dict:
    """
    Generate the complete monthly rebalancing recommendation package.

    Returns the full recommendations dict.
    """
    logger.info("=" * 70)
    logger.info("MONTHLY REBALANCER  â€“  v3.2  (Architecture Feb 2026)")
    logger.info("=" * 70)
    logger.info(f"  Rebalance date  : {rebalance_date}")
    logger.info(f"  Account equity  : â‚¬{account_equity:,.2f}")
    logger.info(f"  VIX level       : {vix_level if vix_level is not None else 'not provided'}")
    logger.info(f"  Dry run         : {dry_run}")
    logger.info("=" * 70)

    # â”€â”€ Step 0: Validate rebalance date â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    rd = validate_rebalance_date(rebalance_date)
    execution_date = get_execution_date(rd)
    month_label    = rd.strftime("%Y-%m")

    logger.info(f"Execution date (first trading day of next month): {execution_date}")

    # â”€â”€ Step 1: Load all inputs â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    logger.info("\n[1/8] Loading input filesâ€¦")
    current_positions = load_portfolio_state()
    ranked_universe   = load_momentum_ranked(rebalance_date)
    position_sizes    = load_position_sizes()
    stop_levels       = load_stop_levels()
    exit_signals      = load_exit_signals()
    peak_equity       = load_peak_equity()

    logger.info(
        f"  Portfolio: {len(current_positions)} positions | "
        f"Ranked universe: {len(ranked_universe)} instruments | "
        f"Sized: {len(position_sizes)} | "
        f"Stops: {len(stop_levels)} | "
        f"Exit signals: {len(exit_signals)}"
    )

    # â”€â”€ Step 2: Circuit breaker check â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    logger.info("\n[2/8] Running circuit breaker checksâ€¦")
    cb    = CircuitBreaker()
    cb_status = cb.check_all(
        account_equity    = account_equity,
        peak_equity       = peak_equity,
        current_positions = current_positions,
        rebalance_date    = rebalance_date,
        vix_level         = vix_level,
    )

    cb_override_log = None
    if cb_status["halted"] and override_circuit_breaker:
        cb_override_log = {
            "rationale":  override_circuit_breaker,
            "overridden_at": datetime.now().isoformat(),
            "overridden_by": "human_operator",
            "expires_after": "1_trading_day",
        }
        logger.warning(
            f"âš   CIRCUIT BREAKER OVERRIDE by human operator. "
            f"Rationale: '{override_circuit_breaker}'. Override expires after 1 trading day."
        )
        cb_status["halted"]       = False
        cb_status["halt_entries"] = False
        cb_status["halt_all"]     = False

    if cb_status["halt_all"]:
        logger.error(
            "ðŸ›‘ HALT_ALL triggered. All trading is suspended. "
            "Resolve data staleness issues and rerun. "
            "Use --override-circuit-breaker only if manually verified."
        )
        return {
            "status":              "HALTED_ALL_TRADING",
            "rebalance_date":      rebalance_date,
            "execution_date":      str(execution_date),
            "account_equity":      account_equity,
            "circuit_breakers":    cb_status,
            "message":             "All trading suspended â€” critical circuit breaker triggered.",
            "action_required":     "Fix data staleness, then rerun Scripts 01â€“10 and Script 11.",
            "generated_at":        datetime.now().isoformat(),
        }

    if cb_status["halt_entries"]:
        logger.warning(
            "â›” HALT_ENTRIES triggered. No new entries permitted this cycle. "
            "Exit signals will still be processed. "
            "Use --override-circuit-breaker to force entries if manually verified."
        )

    # â”€â”€ Step 3: Generate three portfolio scenarios â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    logger.info("\n[3/8] Generating three portfolio scenariosâ€¦")
    max_positions  = get_max_positions(account_equity)
    
    logger.info(f"  Max positions for â‚¬{account_equity:,.0f} account: {max_positions}")
    
    # Scenario 1: Pure Momentum (top 2Ã— only)
    logger.info("\n  Scenario 1: Pure Momentum")
    logger.info("    Strategy: Select top instruments by momentum rank only")
    logger.info(f"    Candidate pool: Top {max_positions * 2} (narrow focus)")
    
    scenario1_symbols = balance_asset_allocation(
        ranked_universe = ranked_universe,
        max_positions   = max_positions,
        position_sizes  = position_sizes,
        candidate_pool_size = max_positions * 2,
        scenario_name = "Pure Momentum"
    )
    
    # Scenario 2: Force Diversity (ALL qualified)
    logger.info("\n  Scenario 2: Force Diversity")
    logger.info("    Strategy: Enforce asset class targets regardless of momentum rank")
    logger.info(f"    Candidate pool: ALL {len(ranked_universe)} qualified (full universe search)")
    
    scenario2_symbols = balance_asset_allocation(
        ranked_universe = ranked_universe,
        max_positions   = max_positions,
        position_sizes  = position_sizes,
        candidate_pool_size = len(ranked_universe),
        scenario_name = "Force Diversity",
        diversification_mode = True  # ADD THIS LINE
    )
    
    # Scenario 3: Balanced Approach (top 200)
    logger.info("\n  Scenario 3: Momentum + Diversification")
    logger.info("    Strategy: Balance between momentum strength and diversification")
    logger.info(f"    Candidate pool: Top 200 (expanded search)")
    
    scenario3_symbols = balance_asset_allocation(
        ranked_universe = ranked_universe,
        max_positions   = max_positions,
        position_sizes  = position_sizes,
        candidate_pool_size = 200,
        scenario_name = "Momentum + Diversification"
    )
    
    logger.info(f"\n  âœ“ Three scenarios generated from {len(ranked_universe)} qualified instruments")

    # â”€â”€ Step 4: Process all three scenarios â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    logger.info("\n[4/8] Processing three scenarios (exits/holds/entries for each)â€¦")
    
    scenarios = {}
    
    for scenario_num, (scenario_name, top_n_symbols) in enumerate([
        ("scenario1_pure_momentum", scenario1_symbols),
        ("scenario2_force_diversity", scenario2_symbols),
        ("scenario3_balanced", scenario3_symbols),
    ], 1):
        
        logger.info(f"\n  Processing Scenario {scenario_num}: {scenario_name.replace('_', ' ').title()}")
        
        # Mandatory exits (same for all scenarios - based on current portfolio)
        mandatory_exits = identify_mandatory_exits(current_positions, exit_signals)
        mandatory_exit_symbols = [e["symbol"] for e in mandatory_exits]
        
        # Rotation exits (varies by scenario - depends on top_n_symbols)
        rotation_exits = identify_rotation_exits(
            current_positions, top_n_symbols, mandatory_exit_symbols,
            ranked_universe=ranked_universe,
            rebalance_date=rebalance_date,
        )
        rotation_exit_symbols = [e["symbol"] for e in rotation_exits]
        all_exit_symbols = mandatory_exit_symbols + rotation_exit_symbols
        
        # New entries (varies by scenario)
        if cb_status["halt_entries"]:
            new_entries = []
            logger.warning(f"    New entries SUPPRESSED by circuit breaker")
        else:
            new_entries = identify_new_entries(
                top_n_symbols   = top_n_symbols,
                current_positions = current_positions,
                all_exit_symbols  = all_exit_symbols,
                position_sizes    = position_sizes,
                stop_levels       = stop_levels,
                ranked_universe   = ranked_universe,
            )
        
        # Holds (varies by scenario)
        holds = identify_holds(
            current_positions = current_positions,
            top_n_symbols     = top_n_symbols,
            all_exit_symbols  = all_exit_symbols,
            stop_levels       = stop_levels,
        )
        
        logger.info(f"    Exits: {len(mandatory_exits)} mandatory + {len(rotation_exits)} rotation")
        logger.info(f"    New entries: {len(new_entries)}")
        logger.info(f"    Holds: {len(holds)}")
        
        # Store scenario results
        scenarios[scenario_name] = {
            "symbols": top_n_symbols,
            "mandatory_exits": mandatory_exits,
            "rotation_exits": rotation_exits,
            "new_entries": new_entries,
            "holds": holds,
        }

    # â”€â”€ Step 5: Compute summaries for all scenarios â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    logger.info("\n[5/8] Computing capital and allocation summaries for each scenarioâ€¦")
    
    for scenario_name, scenario_data in scenarios.items():
        capital_summary = compute_capital_summary(
            account_equity, 
            scenario_data["holds"], 
            scenario_data["new_entries"],
            scenario_data["mandatory_exits"],
            scenario_data["rotation_exits"]
        )
        asset_breakdown = compute_asset_class_breakdown(
            scenario_data["holds"],
            scenario_data["new_entries"],
            account_equity
        )
        
        scenario_data["capital_summary"] = capital_summary
        scenario_data["asset_breakdown"] = asset_breakdown

    # â”€â”€ Step 6: Compile three-scenario recommendation package â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    logger.info("\n[6/8] Assembling three-scenario recommendation packageâ€¦")
    
    # ── Step 5b: Build ranked candidates export for PDF decision table ─────────
    logger.info(
        f"\n[5b] Building Top-50 ranked candidates export "
        f"({min(50, len(ranked_universe))} of {len(ranked_universe)} qualified)…"
    )
    ranked_candidates_export = build_ranked_candidates_export(
        ranked_universe    = ranked_universe,
        position_sizes     = position_sizes,
        stop_levels        = stop_levels,
        max_candidates     = 50,
        account_equity     = account_equity,
        risk_pct_per_trade = 1.0,          # Default: risk 1% of equity per trade
    )
    logger.info(f"  ✓ Exported {len(ranked_candidates_export)} candidate records")

    # Common metadata (same for all scenarios)
    common_metadata = {
        "status":              "READY_FOR_REVIEW" if not cb_status["halted"] else "CIRCUIT_BREAKER_TRIGGERED",
        "rebalance_date":      rebalance_date,
        "execution_date":      str(execution_date),
        "month":               month_label,
        "account_equity":      round(account_equity, 2),
        "max_positions":       max_positions,
        "generated_at":        datetime.now().isoformat(),
        "dry_run":             dry_run,
        "architecture_version": "v3.2",
        "circuit_breakers": {
            "status":       "TRIGGERED" if cb_status["breakers"] else "ALL_CLEAR",
            "halt_entries": cb_status["halt_entries"],
            "halt_all":     cb_status["halt_all"],
            "breakers":     cb_status["breakers"],
            "override":     cb_override_log,
        },
        "universe": {
            "total_qualified":   len(ranked_universe),
            "top_n_selected":    max_positions,
        },
        # ── Top-50 candidates for PDF decision table (Script 12) ──────────────
        # Full ranked list (sorted by momentum_score desc) with all order
        # details pre-computed so Script 12 can render the table without
        # accessing any upstream data files directly.
        "ranked_candidates":   ranked_candidates_export,
        "execution_checklist": [
            f"1. CHOOSE ONE SCENARIO from the three options below",
            f"2. Review all recommendations for chosen scenario before placing orders",
            f"3. Verify data freshness (run Script 01 --mode incremental if needed)",
            f"4. On {execution_date}: execute exit order(s) at market open",
            f"5. On {execution_date}: place limit buy order(s) at close + 0.5%",
            f"6. After fills: confirm stop-loss orders are live for all new positions",
            f"7. Log executions using Script 13 (13_log_execution.py)",
            f"8. Verify stop-loss orders every Friday using Script 09",
        ],
    }
    
    # Build scenario-specific recommendations
    recommendations = {
        **common_metadata,
        
        "scenario_1_pure_momentum": {
            "name": "Pure Momentum",
            "description": "Follow strongest trends. Highest momentum, may sacrifice diversification.",
            "candidate_pool": max_positions * 2,
            "philosophy": "Trend is king. Accept concentration if that's where momentum is.",
            
            "exits": {
                "mandatory": scenarios["scenario1_pure_momentum"]["mandatory_exits"],
                "rotation":  scenarios["scenario1_pure_momentum"]["rotation_exits"],
                "total": (len(scenarios["scenario1_pure_momentum"]["mandatory_exits"]) + 
                         len(scenarios["scenario1_pure_momentum"]["rotation_exits"])),
            },
            "entries": {
                "new": scenarios["scenario1_pure_momentum"]["new_entries"],
                "total": len(scenarios["scenario1_pure_momentum"]["new_entries"]),
            },
            "holds": {
                "positions": scenarios["scenario1_pure_momentum"]["holds"],
                "total": len(scenarios["scenario1_pure_momentum"]["holds"]),
            },
            "capital_summary": scenarios["scenario1_pure_momentum"]["capital_summary"],
            "asset_breakdown": scenarios["scenario1_pure_momentum"]["asset_breakdown"],
            "warnings": _collect_warnings(
                cb_status, 
                scenarios["scenario1_pure_momentum"]["capital_summary"],
                scenarios["scenario1_pure_momentum"]["asset_breakdown"],
                scenarios["scenario1_pure_momentum"]["new_entries"],
                scenarios["scenario1_pure_momentum"]["holds"]
            ),
        },
        
        "scenario_2_force_diversity": {
            "name": "Force Diversity",
            "description": "Enforce asset class targets. Lower momentum, higher diversification.",
            "candidate_pool": len(ranked_universe),  # ALL qualified symbols
            "philosophy": "Diversification over momentum. Accept lower returns for balance.",
            
            "exits": {
                "mandatory": scenarios["scenario2_force_diversity"]["mandatory_exits"],
                "rotation":  scenarios["scenario2_force_diversity"]["rotation_exits"],
                "total": (len(scenarios["scenario2_force_diversity"]["mandatory_exits"]) + 
                         len(scenarios["scenario2_force_diversity"]["rotation_exits"])),
            },
            "entries": {
                "new": scenarios["scenario2_force_diversity"]["new_entries"],
                "total": len(scenarios["scenario2_force_diversity"]["new_entries"]),
            },
            "holds": {
                "positions": scenarios["scenario2_force_diversity"]["holds"],
                "total": len(scenarios["scenario2_force_diversity"]["holds"]),
            },
            "capital_summary": scenarios["scenario2_force_diversity"]["capital_summary"],
            "asset_breakdown": scenarios["scenario2_force_diversity"]["asset_breakdown"],
            "warnings": _collect_warnings(
                cb_status, 
                scenarios["scenario2_force_diversity"]["capital_summary"],
                scenarios["scenario2_force_diversity"]["asset_breakdown"],
                scenarios["scenario2_force_diversity"]["new_entries"],
                scenarios["scenario2_force_diversity"]["holds"]
            ),
        },
        
        "scenario_3_balanced": {
            "name": "Momentum + Diversification",
            "description": "Balance momentum and diversification. Moderate on both dimensions.",
            "candidate_pool": 200,  # Top 200 qualified
            "philosophy": "Best of both worlds. Strong trends with reasonable diversity.",
            
            "exits": {
                "mandatory": scenarios["scenario3_balanced"]["mandatory_exits"],
                "rotation":  scenarios["scenario3_balanced"]["rotation_exits"],
                "total": (len(scenarios["scenario3_balanced"]["mandatory_exits"]) + 
                         len(scenarios["scenario3_balanced"]["rotation_exits"])),
            },
            "entries": {
                "new": scenarios["scenario3_balanced"]["new_entries"],
                "total": len(scenarios["scenario3_balanced"]["new_entries"]),
            },
            "holds": {
                "positions": scenarios["scenario3_balanced"]["holds"],
                "total": len(scenarios["scenario3_balanced"]["holds"]),
            },
            "capital_summary": scenarios["scenario3_balanced"]["capital_summary"],
            "asset_breakdown": scenarios["scenario3_balanced"]["asset_breakdown"],
            "warnings": _collect_warnings(
                cb_status, 
                scenarios["scenario3_balanced"]["capital_summary"],
                scenarios["scenario3_balanced"]["asset_breakdown"],
                scenarios["scenario3_balanced"]["new_entries"],
                scenarios["scenario3_balanced"]["holds"]
            ),
        },
    }

    return recommendations


def build_ranked_candidates_export(
    ranked_universe:    List[Dict],
    position_sizes:     Dict,
    stop_levels:        Dict,
    max_candidates:     int = 50,
    account_equity:     float = 0.0,
    risk_pct_per_trade: float = 1.0,
) -> List[Dict]:
    """
    Build the top-N ranked candidates list for the PDF decision table.

    Merges the momentum-ranked universe (Script 07) with position sizing
    (Script 08) and stop levels (Script 09) so Script 12 can render the
    full Top-50 table with all order details — even for symbols not
    selected in any scenario.

    For candidates not covered by Script 08/09 (e.g. filtered out by
    minimum-lot or max-position-value constraints), fallback values are
    estimated from available indicator data and flagged with
    sizing_estimated=True so the report can mark them clearly.

    Args:
        ranked_universe:    Full ranked list from load_momentum_ranked()
        position_sizes:     Dict keyed by symbol from load_position_sizes()
        stop_levels:        Dict keyed by symbol from load_stop_levels()
        max_candidates:     How many rows to export (default: 50)
        account_equity:     Account equity in EUR; used to estimate shares
                            for candidates lacking Script 09 sizing data.
        risk_pct_per_trade: % of equity risked per trade for share estimate
                            (default 1.0 %).

    Returns:
        List of enriched candidate dicts, sorted by momentum_score desc.
    """
    export: List[Dict] = []

    for rank_entry in ranked_universe[:max_candidates]:
        sym    = rank_entry.get("symbol", "")
        sizing = position_sizes.get(sym, {})
        stop   = stop_levels.get(sym, {})

        # Return fields — use ROC metrics as proxies for period returns
        # Script 07 stores: roc_20d (~1M), roc_60d (~3M), roc_120d (~6M)
        # We add 12M as momentum_score / 100 * 100 = momentum_score itself
        roc_20d  = rank_entry.get("roc_20d")
        roc_60d  = rank_entry.get("roc_60d")
        roc_120d = rank_entry.get("roc_120d")

        # Convert raw ratios to percentage if needed (Script 07 may store as 0.05 or 5.0)
        def _to_pct(v):
            if v is None:
                return None
            v = float(v)
            # If absolute value < 2.0, assume it's a ratio (0.05 → 5.0)
            return round(v * 100, 2) if abs(v) < 2.0 else round(v, 2)

        # Prefer sizing data for order fields (Script 08 has limit_price = close + 0.5%)
        entry_price = sizing.get("limit_price") or sizing.get("entry_price") or rank_entry.get("close")
        stop_price  = stop.get("stop_price")
        stop_dst    = stop.get("stop_distance_pct")
        shares      = sizing.get("shares")
        val_eur     = sizing.get("position_value_eur")
        sizing_estimated = False  # True when Script 08/09 data is absent

        # If sizing has no limit_price but we have close, compute it
        if not entry_price and rank_entry.get("close"):
            entry_price = round(float(rank_entry["close"]) * 1.005, 4)

        # ── Fallback stop: estimated from 2x ATR when Script 08 data is absent ──────
        # This mirrors the conservative default used by Script 08 (initial stop = 2 * ATR below entry).
        if stop_price is None and entry_price and rank_entry.get("atr_20_pct"):
            try:
                atr_pct = float(rank_entry["atr_20_pct"])
                if atr_pct > 0:
                    stop_price       = round(float(entry_price) * (1.0 - 2.0 * atr_pct / 100.0), 4)
                    stop_dst         = round(2.0 * atr_pct, 2)
                    sizing_estimated = True
            except (TypeError, ValueError):
                pass

        # ── Fallback shares / position value when Script 09 data is absent ──────────
        # Uses the standard fixed-fractional formula: risk_amount / risk_per_share
        # where risk_per_share = entry_price - stop_price.
        if shares is None and account_equity > 0 and entry_price and stop_price:
            try:
                risk_per_share = float(entry_price) - float(stop_price)
                if risk_per_share > 0:
                    risk_amount    = account_equity * risk_pct_per_trade / 100.0
                    shares         = max(1, int(risk_amount / risk_per_share))
                    val_eur        = round(shares * float(entry_price), 2)
                    sizing_estimated = True
            except (TypeError, ValueError):
                pass

        export.append({
            # Identity
            "symbol":          sym,
            "name":            rank_entry.get("name", sizing.get("name", "")),
            "exchange":        rank_entry.get("exchange", sizing.get("exchange", "")),
            "sector":          rank_entry.get("sector",   sizing.get("sector", "")),
            "asset_class":     rank_entry.get("asset_class", sizing.get("asset_class", "")),
            # Ranking
            "rank":            rank_entry.get("rank"),
            "momentum_score":  rank_entry.get("momentum_score"),
            # Technical indicators
            "adx_14":          rank_entry.get("adx_14"),
            "atr_20_pct":      rank_entry.get("atr_20_pct"),
            "close":           rank_entry.get("close"),
            "sma_50":          rank_entry.get("sma_50"),
            "sma_200":         rank_entry.get("sma_200"),
            # Return metrics (percentage)
            "return_1m":       _to_pct(roc_20d),
            "return_3m":       _to_pct(roc_60d),
            "return_6m":       _to_pct(roc_120d),
            # Order details (from Scripts 08 & 09; or estimated fallback when absent)
            "limit_price":     entry_price,
            "shares":          shares,
            "position_value_eur": val_eur,
            "position_pct":    sizing.get("position_pct"),
            "initial_stop":    stop_price,
            "stop_distance_pct": stop_dst,
            # Flag: True when stop/shares were estimated (Script 08/09 data absent)
            "sizing_estimated": sizing_estimated,
        })

    return export


def _collect_warnings(
    cb_status:       Dict,
    capital_summary: Dict,
    asset_breakdown: Dict,
    new_entries:     List[Dict],
    holds:           List[Dict],
) -> List[str]:
    """
    Aggregate all non-critical warnings into a single advisory list.
    """
    warnings: List[str] = []

    # Capital warnings
    if capital_summary["cash_remaining_approx_pct"] < 5.0:
        warnings.append(
            f"LOW CASH RESERVE: Estimated {capital_summary['cash_remaining_approx_pct']:.1f}% "
            f"(â‚¬{capital_summary['cash_remaining_approx_eur']:,.0f}) remaining after rebalancing. "
            "Minimum recommended: 5%."
        )

    if capital_summary["total_deployed_after_pct"] > 95.0:
        warnings.append(
            f"HIGH DEPLOYMENT: {capital_summary['total_deployed_after_pct']:.1f}% of equity "
            "will be deployed after rebalancing."
        )

    # Asset class allocation warnings (compare to targets)
    for ac, targets in ASSET_CLASS_TARGETS.items():
        actual = asset_breakdown.get(ac, {}).get("pct", 0)
        target = targets["target"]
        max_limit = targets["max"]
        
        # Hard limit violation
        if actual > max_limit:
            warnings.append(
                f"ASSET CLASS LIMIT: {ac.upper()} allocation {actual:.1f}% "
                f"exceeds maximum {max_limit}%. Reduce {ac} positions before adding more."
            )
        # Significant deviation from target (beyond tolerance)
        elif abs(actual - target) > ALLOCATION_TOLERANCE:
            warnings.append(
                f"ALLOCATION DEVIATION: {ac.upper()} allocation {actual:.1f}% "
                f"deviates from target {target}% by {actual - target:+.1f}pp. "
                f"Consider adjusting position selection in next cycle."
            )

    # Circuit breaker advisory
    for b in cb_status.get("breakers", []):
        if b["severity"] == "MEDIUM":
            warnings.append(f"CB-ADVISORY [{b['breaker']}]: {b['detail']}")

    return warnings


# ============================================================================
# OUTPUT FUNCTIONS
# ============================================================================

def save_recommendations_json(
    recommendations: Dict,
    output_file:     Path,
) -> None:
    """
    Write the full recommendations dict to a JSON file.

    Format is machine-readable and suitable for consumption by
    Script 12 (report generator) and Script 13 (execution logger).
    """
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    try:
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(recommendations, f, indent=2, default=str)
        logger.info(f"âœ“ JSON saved â†’ {output_file}")
    except OSError as exc:
        logger.error(f"Failed to write JSON: {exc}")


def save_recommendations_csv(
    recommendations: Dict,
    base_output_path: Path,
) -> None:
    """
    Write three flat, human-readable CSV action tables (one per scenario).

    Creates files:
      - {month}_scenario1_pure_momentum.csv
      - {month}_scenario2_force_diversity.csv
      - {month}_scenario3_balanced.csv

    Columns: action, priority, symbol, name, exchange, sector,
             asset_class, shares, entry_or_limit_price, position_value_eur,
             position_pct, initial_stop, momentum_rank, momentum_score,
             order_type, reason_or_note
    """
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    scenarios = [
        ("scenario_1_pure_momentum", "scenario1_pure_momentum"),
        ("scenario_2_force_diversity", "scenario2_force_diversity"),
        ("scenario_3_balanced", "scenario3_balanced"),
    ]
    
    for scenario_key, filename_part in scenarios:
        scenario_data = recommendations.get(scenario_key, {})
        
        # Generate filename
        base_name = base_output_path.stem.replace("_recommendations", "")
        output_file = base_output_path.parent / f"{base_name}_{filename_part}.csv"
        
        rows: List[Dict] = []

        # Mandatory exits (same for all scenarios)
        for e in scenario_data.get("exits", {}).get("mandatory", []):
            rows.append({
                "action":               "EXIT-MANDATORY",
                "priority":             e.get("priority"),
                "symbol":               e.get("symbol"),
                "name":                 "",
                "exchange":             "",
                "sector":               "",
                "asset_class":          "",
                "shares":               e.get("shares"),
                "entry_or_limit_price": e.get("entry_price"),
                "position_value_eur":   e.get("current_value"),
                "position_pct":         "",
                "initial_stop":         e.get("current_stop"),
                "momentum_rank":        "",
                "momentum_score":       "",
                "order_type":           e.get("order_type"),
                "reason_or_note":       f"{e.get('reason')}: {e.get('detail', '')}",
            })

        # Rotation exits (varies by scenario)
        for e in scenario_data.get("exits", {}).get("rotation", []):
            rows.append({
                "action":               "EXIT-ROTATION",
                "priority":             ROTATION_EXIT_PRIORITY,
                "symbol":               e.get("symbol"),
                "name":                 "",
                "exchange":             "",
                "sector":               "",
                "asset_class":          "",
                "shares":               e.get("shares"),
                "entry_or_limit_price": e.get("entry_price"),
                "position_value_eur":   e.get("current_value"),
                "position_pct":         "",
                "initial_stop":         e.get("current_stop"),
                "momentum_rank":        "",
                "momentum_score":       "",
                "order_type":           e.get("order_type"),
                "reason_or_note":       e.get("reason"),
            })

        # New entries (varies by scenario)
        for e in scenario_data.get("entries", {}).get("new", []):
            rows.append({
                "action":               "BUY-NEW",
                "priority":             "",
                "symbol":               e.get("symbol"),
                "name":                 e.get("name"),
                "exchange":             e.get("exchange"),
                "sector":               e.get("sector"),
                "asset_class":          e.get("asset_class"),
                "shares":               e.get("shares"),
                "entry_or_limit_price": e.get("limit_price"),
                "position_value_eur":   e.get("position_value_eur"),
                "position_pct":         e.get("position_pct"),
                "initial_stop":         e.get("initial_stop"),
                "momentum_rank":        e.get("momentum_rank"),
                "momentum_score":       e.get("momentum_score"),
                "order_type":           e.get("order_type"),
                "reason_or_note":       f"Rank {e.get('momentum_rank')} | Score {e.get('momentum_score')}",
            })

        # Holds (varies by scenario)
        for h in scenario_data.get("holds", {}).get("positions", []):
            rows.append({
                "action":               "HOLD",
                "priority":             "",
                "symbol":               h.get("symbol"),
                "name":                 "",
                "exchange":             "",
                "sector":               "",
                "asset_class":          "",
                "shares":               h.get("shares"),
                "entry_or_limit_price": h.get("entry_price"),
                "position_value_eur":   h.get("current_value"),
                "position_pct":         "",
                "initial_stop":         h.get("current_stop"),
                "momentum_rank":        h.get("momentum_rank"),
                "momentum_score":       h.get("momentum_score"),
                "order_type":           "",
                "reason_or_note":       "Continue holding",
            })

        # Write CSV
        if rows:
            try:
                df = pd.DataFrame(rows)
                df.to_csv(output_file, index=False)
                logger.info(f"âœ“ CSV saved â†’ {output_file}")
            except OSError as exc:
                logger.error(f"Failed to write CSV {output_file}: {exc}")
        else:
            logger.warning(f"No rows for scenario {scenario_key}, skipping CSV")


# ============================================================================
# HUMAN-IN-THE-LOOP DISPLAY
# ============================================================================

def print_human_approval_summary(rec: Dict) -> None:
    """
    Print a formatted comparison of three portfolio scenarios.

    This is the primary decision-support tool for the human operator.
    Shows three complete scenarios side-by-side for easy comparison.
    """
    W  = 100   # line width (expanded for three columns)

    def hdr(title: str) -> str:
        return f"\n{'â•' * W}\n  {title}\n{'â•' * W}"

    def fmt_eur(v) -> str:
        return f"â‚¬{float(v):>9,.0f}" if v not in (None, "") else "       N/A"

    def fmt_pct(v) -> str:
        return f"{float(v):>5.1f}%" if v not in (None, "") else "  N/A"

    border = "=" * W

    # â”€â”€ Header (always present) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    print(f"\n{border}")
    print(f"  MONTHLY REBALANCING RECOMMENDATION â€” THREE SCENARIOS")
    print(f"  Rebalance date : {rec['rebalance_date']}  |  Execution date : {rec['execution_date']}")
    print(f"  Account equity : â‚¬{rec['account_equity']:,.2f}  |  Max positions : {rec.get('max_positions', 'N/A')}")
    print(f"  Generated      : {rec['generated_at']}")
    print(f"  Status         : {rec['status']}")
    print(border)

    # â”€â”€ Early exit if HALTED â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    if "HALTED" in rec.get("status", ""):
        print(hdr("CIRCUIT BREAKERS â€” TRADING HALTED"))
        cb = rec.get("circuit_breakers", {})
        breakers = cb.get("breakers", [])
        
        if breakers:
            for b in breakers:
                sev_icon = "ðŸ›‘" if b["severity"] == "CRITICAL" else ("â›”" if b["severity"] == "HIGH" else "âš  ")
                print(f"  {sev_icon}  [{b['severity']}] {b['breaker']}: {b['detail']}")
        
        print(f"\n{border}")
        print(f"  ðŸ›‘  ALL TRADING SUSPENDED")
        print(f"  {rec.get('message', 'Circuit breaker triggered.')}")
        print(f"  Action required: {rec.get('action_required', 'Resolve issues and rerun.')}")
        print(f"{border}\n")
        return

    # â”€â”€ Three-scenario comparison â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    scenarios = [
        ("scenario_1_pure_momentum", "1. PURE MOMENTUM"),
        ("scenario_2_force_diversity", "2. FORCE DIVERSITY"),
        ("scenario_3_balanced", "3. MOMENTUM + DIVERSIFICATION"),
    ]
    
    print(hdr("SCENARIO COMPARISON"))
    print(f"\n  {'Metric':<25} {'Scenario 1':<25} {'Scenario 2':<25} {'Scenario 3':<25}")
    print(f"  {'â”€'*25} {'â”€'*25} {'â”€'*25} {'â”€'*25}")
    
    # Strategy description
    for i, (key, name) in enumerate(scenarios):
        s = rec.get(key, {})
        if i == 0:
            print(f"  {'Strategy':<25} {s.get('name', 'N/A'):<25}", end="")
        else:
            print(f" {s.get('name', 'N/A'):<25}", end="")
    print()
    
    # Candidate pool
    for i, (key, name) in enumerate(scenarios):
        s = rec.get(key, {})
        pool = s.get('candidate_pool', 'N/A')
        # Show "ALL {n}" if pool size equals full universe, otherwise "Top {n}"
        total_qualified = rec.get('universe', {}).get('total_qualified', pool)
        if pool == total_qualified and isinstance(pool, int):
            pool_str = f"ALL {pool}"
        elif isinstance(pool, int):
            pool_str = f"Top {pool}"
        else:
            pool_str = str(pool)
        
        if i == 0:
            print(f"  {'Candidate pool':<25} {pool_str:<25}", end="")
        else:
            print(f" {pool_str:<25}", end="")
    print()
    
    print(f"  {'â”€'*25} {'â”€'*25} {'â”€'*25} {'â”€'*25}")
    
    # Position counts
    for i, (key, name) in enumerate(scenarios):
        s = rec.get(key, {})
        new = s.get('entries', {}).get('total', 0)
        holds = s.get('holds', {}).get('total', 0)
        exits = s.get('exits', {}).get('total', 0)
        
        if i == 0:
            print(f"  {'New entries':<25} {new:<25}", end="")
        else:
            print(f" {new:<25}", end="")
    print()
    
    for i, (key, name) in enumerate(scenarios):
        s = rec.get(key, {})
        holds = s.get('holds', {}).get('total', 0)
        if i == 0:
            print(f"  {'Holds':<25} {holds:<25}", end="")
        else:
            print(f" {holds:<25}", end="")
    print()
    
    for i, (key, name) in enumerate(scenarios):
        s = rec.get(key, {})
        exits = s.get('exits', {}).get('total', 0)
        if i == 0:
            print(f"  {'Exits':<25} {exits:<25}", end="")
        else:
            print(f" {exits:<25}", end="")
    print()
    
    print(f"  {'â”€'*25} {'â”€'*25} {'â”€'*25} {'â”€'*25}")
    
    # Asset class allocation
    print(f"  {'ASSET ALLOCATION':<25} {'':25} {'':25} {'':25}")
    for asset_class in ['etf', 'stock', 'crypto']:
        for i, (key, name) in enumerate(scenarios):
            s = rec.get(key, {})
            ab = s.get('asset_breakdown', {})
            ac_data = ab.get(asset_class, {})
            pct = ac_data.get('pct', 0)
            target = ASSET_CLASS_TARGETS[asset_class]['target']
            delta = pct - target
            
            if i == 0:
                print(f"  {asset_class.upper():<25} {f'{pct:.1f}% (Î”{delta:+.0f}pp)':<25}", end="")
            else:
                print(f" {f'{pct:.1f}% (Î”{delta:+.0f}pp)':<25}", end="")
        print()
    
    print(f"  {'â”€'*25} {'â”€'*25} {'â”€'*25} {'â”€'*25}")
    
    # Capital deployment
    for i, (key, name) in enumerate(scenarios):
        s = rec.get(key, {})
        cs = s.get('capital_summary', {})
        deployed_pct = cs.get('total_deployed_after_pct', 0)
        
        if i == 0:
            print(f"  {'Capital deployed':<25} {f'{deployed_pct:.1f}%':<25}", end="")
        else:
            print(f" {f'{deployed_pct:.1f}%':<25}", end="")
    print()
    
    for i, (key, name) in enumerate(scenarios):
        s = rec.get(key, {})
        cs = s.get('capital_summary', {})
        cash_pct = cs.get('cash_remaining_approx_pct', 0)
        
        if i == 0:
            print(f"  {'Cash reserve':<25} {f'{cash_pct:.1f}%':<25}", end="")
        else:
            print(f" {f'{cash_pct:.1f}%':<25}", end="")
    print()
    
    print(f"  {'â”€'*25} {'â”€'*25} {'â”€'*25} {'â”€'*25}")
    
    # Warnings count
    for i, (key, name) in enumerate(scenarios):
        s = rec.get(key, {})
        warnings = s.get('warnings', [])
        warning_count = len(warnings)
        
        if i == 0:
            print(f"  {'Warnings':<25} {warning_count:<25}", end="")
        else:
            print(f" {warning_count:<25}", end="")
    print()
    
    print()
    
    # â”€â”€ Detailed view of each scenario â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    for key, name in scenarios:
        s = rec.get(key, {})
        
        print(hdr(name))
        print(f"\n  Philosophy: {s.get('philosophy', 'N/A')}")
        print(f"  Description: {s.get('description', 'N/A')}")
        
        # New entries
        new_entries = s.get('entries', {}).get('new', [])
        print(f"\n  New Entries ({len(new_entries)} positions):")
        if new_entries:
            print(f"    {'#':>3}  {'Symbol':<14} {'Name':<20} {'AC':<6} {'Shs':>4}  "
                  f"{'Value':>10}  {'%Eq':>5}  {'Score':>7}")
            print(f"    {'â”€'*3}  {'â”€'*14} {'â”€'*20} {'â”€'*6} {'â”€'*4}  {'â”€'*10}  {'â”€'*5}  {'â”€'*7}")
            for i, e in enumerate(new_entries[:10], 1):
                print(f"    {e.get('rank', i):>3}  {e['symbol']:<14} "
                      f"{e.get('name', '')[:20]:<20} "
                      f"{e.get('asset_class', ''):<6} "
                      f"{e.get('shares', 0):>4}  "
                      f"â‚¬{e.get('position_value_eur', 0):>9,.0f}  "
                      f"{e.get('pct_of_equity', 0):>5.1f}%  "
                      f"{e.get('momentum_score', 0):>7.1f}")
        else:
            print("    None")
        
        # Asset allocation
        ab = s.get('asset_breakdown', {})
        print(f"\n  Asset Allocation:")
        print(f"    {'Class':<8} {'Value':<12} {'% Equity':>10} {'vs Target':>12}")
        print(f"    {'â”€'*8} {'â”€'*12} {'â”€'*10} {'â”€'*12}")
        for ac in ['etf', 'stock', 'crypto']:
            ac_data = ab.get(ac, {})
            value = ac_data.get('value_eur', 0)
            pct = ac_data.get('pct', 0)
            target = ASSET_CLASS_TARGETS[ac]['target']
            delta = pct - target
            print(f"    {ac.upper():<8} â‚¬{value:>10,.0f} {pct:>9.1f}% {delta:>11.1f}pp")
        
        # Warnings
        warnings = s.get('warnings', [])
        if warnings:
            print(f"\n  Warnings:")
            for w in warnings[:5]:
                print(f"    âš   {w}")
    
    # â”€â”€ Execution instructions â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    print(hdr("EXECUTION INSTRUCTIONS"))
    print(f"\n  STEP 1: Choose ONE scenario from the three options above")
    print(f"  STEP 2: Generate charts for your chosen scenario:")
    print(f"          python scripts/15_generate_technical_charts.py --scenario 1  (for Pure Momentum)")
    print(f"          python scripts/15_generate_technical_charts.py --scenario 2  (for Force Diversity)")
    print(f"          python scripts/15_generate_technical_charts.py --scenario 3  (for Balanced)")
    print(f"  STEP 3: Review charts and finalize decision")
    print(f"  STEP 4: Execute trades on {rec['execution_date']}")
    print(f"  STEP 5: Log executions with Script 13")
    
    print(f"\n{border}")
    print(f"  âš   HUMAN DECISION REQUIRED: SELECT ONE SCENARIO BEFORE TRADING")
    print(f"  Execution date: {rec['execution_date']}")
    print(f"{border}\n")
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Script 11: Monthly Rebalancer â€” generates rebalancing recommendations.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Standard monthly run
  python scripts/11_monthly_rebalancing.py \\
      --account-equity 50000 --rebalance-date 2026-01-31

  # Include VIX for circuit-breaker check
  python scripts/11_monthly_rebalancing.py \\
      --account-equity 50000 --rebalance-date 2026-01-31 --vix 22.5

  # Override circuit breaker (use only if manually verified)
  python scripts/11_monthly_rebalancing.py \\
      --account-equity 50000 --rebalance-date 2026-01-31 \\
      --override-circuit-breaker "VIX spike verified as data error"

  # Dry run (no files written)
  python scripts/11_monthly_rebalancing.py \\
      --account-equity 50000 --rebalance-date 2026-01-31 --dry-run
        """,
    )

    parser.add_argument(
        "--account-equity",
        type=float,
        required=True,
        metavar="EUR",
        help="Current account equity in EUR (e.g. 50000).",
    )
    parser.add_argument(
        "--rebalance-date",
        type=str,
        required=True,
        metavar="YYYY-MM-DD",
        help="Rebalancing reference date (last trading day of month).",
    )
    parser.add_argument(
        "--vix",
        type=float,
        default=None,
        metavar="LEVEL",
        help=(
            "Current VIX level for circuit-breaker check (optional). "
            "If not provided, VIX circuit-breaker is skipped."
        ),
    )
    parser.add_argument(
        "--override-circuit-breaker",
        type=str,
        default=None,
        metavar="RATIONALE",
        help=(
            "Override a triggered circuit breaker. Requires a rationale string. "
            "Override is logged and expires after 1 trading day. "
            "Cannot override CRITICAL (data staleness) breakers."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Compute and display recommendations without writing any output files.",
    )

    return parser.parse_args()


# ============================================================================
# MAIN
# ============================================================================

def main() -> None:
    args = parse_args()

    global logger
    logger = setup_logging(args.rebalance_date)

    logger.info(f"Script 11 | Monthly Rebalancer | Architecture v3.2")
    logger.info(f"Args: equity={args.account_equity}, date={args.rebalance_date}, "
                f"vix={args.vix}, dry_run={args.dry_run}, "
                f"cb_override={'yes' if args.override_circuit_breaker else 'no'}")

    # â”€â”€ Run rebalancer â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    recommendations = generate_rebalancing_recommendations(
        account_equity           = args.account_equity,
        rebalance_date           = args.rebalance_date,
        vix_level                = args.vix,
        override_circuit_breaker = args.override_circuit_breaker,
        dry_run                  = args.dry_run,
    )

    # â”€â”€ Human-in-the-loop display â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    logger.info("\n[7/8] Printing human approval summaryâ€¦")
    print_human_approval_summary(recommendations)

    # â”€â”€ Save outputs â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    if args.dry_run:
        logger.info("[8/8] DRY RUN â€” no output files written.")
        print("DRY RUN complete. No files written.")
    else:
        logger.info("[8/8] Saving output filesâ€¦")
        month_label  = datetime.strptime(args.rebalance_date, "%Y-%m-%d").strftime("%Y-%m")
        json_path    = REPORTS_DIR / f"{month_label}_recommendations.json"
        csv_base_path = REPORTS_DIR / f"{month_label}_recommendations.csv"

        save_recommendations_json(recommendations, json_path)
        save_recommendations_csv(recommendations, csv_base_path)

        logger.info(f"\nOutput files:")
        logger.info(f"  JSON â†’ {json_path}")
        logger.info(f"  CSVs â†’ {REPORTS_DIR}/{month_label}_scenario*.csv (3 files)")

    # â”€â”€ Exit status â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    status = recommendations.get("status", "")
    if "HALTED" in status:
        logger.error("Script 11 completed with HALTED status. Trading suspended.")
        sys.exit(2)
    elif recommendations["circuit_breakers"].get("halt_entries") and not args.override_circuit_breaker:
        logger.warning("Script 11 completed â€” entries halted by circuit breaker.")
        sys.exit(1)
    else:
        logger.info("Script 11 completed successfully.")
        sys.exit(0)


if __name__ == "__main__":
    main()
