#!/usr/bin/env python3
"""
Script 17: Walk-Forward Optimizer
===================================
Systematically optimize strategy parameters on in-sample data and validate
robustness on out-of-sample data — eliminating look-ahead bias and curve-fitting.

Architecture Reference: v3.7 (Mar 2026)

Walk-Forward Methodology
------------------------
  In-Sample  : 24 months  — optimize parameters (find highest Sharpe)
  Out-Sample : 12 months  — validate with locked parameters (true test)
                            Extended from 6m to reduce single-regime OOS noise.
                            A 6-month OOS window can be dominated by one market
                            regime; 12 months captures at least one full cycle.
  Roll       : 6  months  — shift window forward and repeat

  Example timeline (72-month dataset = 7 windows):
    Window 1 : IS Jan 2019–Dec 2020  |  OOS Jan 2021–Dec 2021
    Window 2 : IS Jul 2019–Jun 2021  |  OOS Jul 2021–Jun 2022
    Window 3 : IS Jan 2020–Dec 2021  |  OOS Jan 2022–Dec 2022
    Window 4 : IS Jul 2020–Jun 2022  |  OOS Jul 2022–Jun 2023
    Window 5 : IS Jan 2021–Dec 2022  |  OOS Jan 2023–Dec 2023
    Window 6 : IS Jul 2021–Jun 2023  |  OOS Jul 2023–Jun 2024
    Window 7 : IS Jan 2022–Dec 2023  |  OOS Jan 2024–Dec 2024

Parameter Grid  (2,880 combinations)
------------------------------------
  SMA fast          : [30, 50, 100]
  SMA slow          : [150, 200, 300, 350]
  ADX threshold     : [15, 20, 25]
  Initial stop mult : [2.0, 2.5, 3.0, 3.5]   ← extended; 2.5 was at grid min in W2
  Trailing stop mult: [2.5, 3.0, 3.5, 4.0, 4.5]  ← extended; 3.5 was at grid min in W1+W2
  Position count    : [10, 15, 20, 25]

Stability Metrics
-----------------
  Stability Ratio   = Avg OOS Sharpe / Avg IS Sharpe
    > 0.8 : Excellent (robust)
    0.7-0.8 : Good
    0.6-0.7 : Acceptable
    < 0.6  : Overfitted — REJECT

  OOS Consistency   = % of OOS windows with positive Sharpe
    >= 70% : Pass
    < 60%  : Fail

  Parameter CV      = Std(param) / Mean(param)  across windows
    < 10%  : Excellent — very stable
    10-20% : Good
    > 20%  : Unstable — no clear optimum

Final Parameter Selection
--------------------------
  1. Highest median OOS Sharpe across all windows
  2. Stability ratio > 0.8 (preferred) or > 0.6 (acceptable)
  3. Parameter CV < 20% (consistent across windows)
  4. Never at grid edge (no extreme parameter selection red flag)

Performance Optimisations (v3.3)
----------------------------------
  OPT-1  Indicator caching by (sma_fast, sma_slow) key.
         Only these two parameters affect what compute_indicators() produces.
         adx_threshold, init_stop_mult, trail_stop_mult, max_positions do not
         change any indicator value — they only affect signal thresholds and
         portfolio construction.
         Result: 12 indicator computations per window instead of 1,296 (~108x
         reduction in the most expensive per-symbol work).

  OPT-2  ProcessPoolExecutor wired to --n-workers (was declared but inert).
         Indicator buckets distributed across workers. Each worker computes
         its share of indicator sets and runs all combos within each set.
         Scales linearly with cores up to min(n_workers, 12) effective buckets.

  OPT-3  Price data pre-sliced once per window to IS+warmup range before
         the combo loop. Eliminates 1,296 redundant date-filter operations per
         window (one per backtest call inside run_backtest_from_data).

  OPT-4  OOS indicators always recomputed fresh from a warmup-padded price
         slice (oos_start - max_sma*2 calendar days).  v3.3 reused the IS
         indicator cache (cached_ind_data), but passing mismatched price_data /
         indicator_data pairs to run_backtest_from_data() caused silent index-
         alignment bugs that produced spurious MaxDD=-100% and zero-trade OOS
         results.  The extra cost is one precompute_indicators() call per window
         (seconds) vs IS optimisation (hours), so correctness wins.

  OPT-5  Early-exit detection. If the entire first indicator bucket returns
         Sharpe=-99 (all combos failed), the IS window is too short for
         indicator warmup. The remaining 8 buckets (864 backtests) are skipped
         immediately with a clear diagnostic message.

Inputs
-------
  data_cache/consolidated/{SYMBOL}.parquet
  data_cache/qualified/qualified_symbols.json

Outputs
--------
  data_cache/backtest/walk_forward/
    wfo_results_{tag}.json          — complete results with all windows
    wfo_parameter_stability_{tag}.csv — per-param CV across windows
    wfo_window_summary_{tag}.csv    — IS/OOS metrics per window
    wfo_optimal_params_{tag}.json   — final recommended parameters
  reports/backtest/
    {YYYYMMDD}_wfo_report_{tag}.json
  logs/wfo_{timestamp}.log

Execution
----------
  # Standard 5-year walk-forward
  python scripts/17_walk_forward_optimizer.py \\
      --start-date 2019-01-01 --end-date 2024-12-31 \\
      --initial-equity 50000

  # Parallel (8 workers — distributes 12 indicator buckets)
  python scripts/17_walk_forward_optimizer.py \\
      --start-date 2019-01-01 --end-date 2024-12-31 \\
      --n-workers 8

  # Fast mode (reduced grid for quick iteration)
  python scripts/17_walk_forward_optimizer.py \\
      --start-date 2019-01-01 --end-date 2024-12-31 \\
      --fast-mode

  # Custom window sizes
  python scripts/17_walk_forward_optimizer.py \\
      --start-date 2018-01-01 --end-date 2024-12-31 \\
      --is-months 24 --oos-months 6 --roll-months 6

  # Single window re-optimization (quarterly trigger from Script 0)
  python scripts/17_walk_forward_optimizer.py \\
      --start-date 2022-01-01 --end-date 2024-12-31 \\
      --output-tag quarterly_review_2024Q4

Architecture: v3.7 (Mar 2026) — Multi-Asset Trend Following Strategy
"""

import os
import sys
import json
import logging
import argparse
import warnings
import itertools
import importlib.util
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any
from concurrent.futures import ProcessPoolExecutor, as_completed
import multiprocessing

import pandas as pd
import numpy as np

# Suppress noisy warnings during high-volume optimization loops
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=RuntimeWarning)

# ---------------------------------------------------------------------------
# Project paths — resolve relative to this file (works from any cwd)
# ---------------------------------------------------------------------------
PROJECT_ROOT   = Path(__file__).resolve().parent.parent
DATA_CACHE_DIR = PROJECT_ROOT / "data_cache"
DATA_LOAD_DIR  = PROJECT_ROOT.parent / "data_load" / "data_cache"
# Input
CONSOL_DIR     = DATA_LOAD_DIR / "consolidated"
QUALIFIED_DIR  = DATA_CACHE_DIR / "qualified"
# Output
BACKTEST_DIR   = DATA_CACHE_DIR / "backtest"
WFO_DIR        = BACKTEST_DIR / "walk_forward"
REPORTS_DIR    = PROJECT_ROOT / "reports" / "backtest"
LOG_DIR        = PROJECT_ROOT / "logs"

# ---------------------------------------------------------------------------
# Script 16 path (needed by subprocess workers for dynamic import)
# ---------------------------------------------------------------------------
_SCRIPT16_DIR  = Path(__file__).resolve().parent
_SCRIPT16_PATH = str(_SCRIPT16_DIR / "16_backtest_engine.py")

# ---------------------------------------------------------------------------
# Import Script 16 public API (must be on PYTHONPATH or same directory)
# ---------------------------------------------------------------------------
sys.path.insert(0, str(_SCRIPT16_DIR))

try:
    from backtest_engine_16 import (          # renamed import alias
        run_backtest_from_data,
        precompute_indicators,
        load_price_data,
        load_qualified_universe,
        load_vix_data,
        DEFAULTS as BT_DEFAULTS,
        compute_indicators,
    )
    _SCRIPT16_ALIAS = "backtest_engine_16"
except ModuleNotFoundError:
    try:
        _spec = importlib.util.spec_from_file_location(
            "backtest_engine",
            _SCRIPT16_DIR / "16_backtest_engine.py",
        )
        _mod = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        run_backtest_from_data  = _mod.run_backtest_from_data
        precompute_indicators   = _mod.precompute_indicators
        load_price_data         = _mod.load_price_data
        load_qualified_universe = _mod.load_qualified_universe
        load_vix_data           = _mod.load_vix_data
        BT_DEFAULTS             = _mod.DEFAULTS
        compute_indicators      = _mod.compute_indicators
        _SCRIPT16_ALIAS         = "16_backtest_engine"
    except Exception as exc:
        print(f"[ERROR] Cannot import Script 16 backtest engine: {exc}")
        print("Ensure 16_backtest_engine.py is in the same directory as this script.")
        sys.exit(1)


# ===========================================================================
# PARAMETER GRID  (Architecture v3.8 — 3,600 combinations)
#
# Grid evolution log:
#   v3.3  1,296 combos — original grid (multiple params at edge)
#   v3.5  2,880 combos — expanded stop multipliers downward
#   v3.7  2,880 combos — OOS 6m→12m, stability bug fixes
#   v3.8  3,600 combos — grid edges resolved per WFO run 3 red flags:
#           adx_threshold  : added 10 (optimizer hit 15-min in 5/7 windows)
#           init_stop_mult : added 1.5 (optimizer hit 2.0-min in 7/7 windows)
#           trail_stop_mult: added 4.5, 5.0 (optimizer hit 4.0-max in 7/7 windows)
#           max_positions  : added 30 (optimizer hit 25-max in 7/7 windows)
#           sma_slow       : restored 150, 350 (narrowing in v3.7 was premature)
#           sma_fast       : unchanged [30,50,100] — still unstable CV=0.374
# ===========================================================================

PARAM_GRID = {
    "sma_fast":          [30, 50, 100],           # CV=0.374 unstable — keep full range
    "sma_slow":          [200, 250, 300],          # CV=0.092 excellent — centred on mean=264
    "adx_threshold":     [10, 15, 20, 25],         # expanded down: hit 15-min in 5/7 windows
    "init_stop_mult":    [1.5, 2.0, 2.5, 3.0, 3.5], # expanded down: hit 2.0-min in 7/7 windows
    "trail_stop_mult":   [3.0, 3.5, 4.0, 4.5, 5.0], # expanded up: hit 4.0-max in 7/7 windows
    "max_positions":     [15, 20, 25, 30],         # expanded up: hit 25-max in 7/7 windows
}
# Grid stats: 3×3×4×5×5×4 = 3,600 combinations | 3×3 = 9 indicator buckets (400 combos/bucket)
# Optimal --n-workers 9 (all buckets complete in one parallel round)

PARAM_GRID_FAST = {
    "sma_fast":          [50, 100],                # representative subset of full range
    "sma_slow":          [200, 250, 300],           # matches full grid sma_slow
    "adx_threshold":     [10, 20],                  # expanded: include new lower bound
    "init_stop_mult":    [1.5, 2.0, 2.5],           # expanded: include new lower bound
    "trail_stop_mult":   [3.5, 4.0, 4.5],           # centred on current optimum range
    "max_positions":     [20, 25],                  # centred on current optimum range
}
# Fast grid stats: 2×3×2×3×3×2 = 216 combinations | 2×3 = 6 indicator buckets (36 combos/bucket)
# Fast grid stats: 2×3×2×3×3×2 = 216 combinations | 2×3 = 6 indicator buckets (36 combos/bucket)

# Fixed params not in the optimization grid
FIXED_PARAMS = {
    "adx_weak":          15,
    "trail_activation":  0.15,
    "risk_per_trade":    0.02,
    "cost_bps":          10,
    "pos_floor_pct":     0.005,
    "pos_ceil_pct":      0.08,
}

# Walk-forward window defaults
WFO_IS_MONTHS   = 24   # in-sample period (months)
WFO_OOS_MONTHS  = 12   # out-of-sample period (months) — extended from 6 to reduce regime noise
WFO_ROLL_MONTHS = 6    # roll-forward step (months)

# Stability thresholds
STABILITY_EXCELLENT   = 0.8
STABILITY_GOOD        = 0.7
STABILITY_ACCEPTABLE  = 0.6
OOS_CONSISTENCY_PASS  = 0.70
OOS_CONSISTENCY_WARN  = 0.60
PARAM_CV_EXCELLENT    = 0.10
PARAM_CV_GOOD         = 0.20

# ---------------------------------------------------------------------------
# OPT-1: Indicator-affecting parameter set.
# Only sma_fast and sma_slow drive compute_indicators() output.
# adx_threshold is a filter threshold, not an indicator parameter.
# init_stop_mult, trail_stop_mult, max_positions are portfolio-construction
# parameters only. This reduces unique indicator sets from 1,296 → 12.
# ---------------------------------------------------------------------------
INDICATOR_PARAMS = frozenset({"sma_fast", "sma_slow"})


# ===========================================================================
# LOGGING
# ===========================================================================

def setup_logging(tag: str = "") -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
    sfx = f"_{tag}" if tag else ""
    log_file = LOG_DIR / f"wfo{sfx}_{ts}.log"
    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
        force=True,
    )
    logger = logging.getLogger("wfo")
    logger.info(f"Walk-Forward Optimizer started | log -> {log_file}")
    return logger


# ===========================================================================
# HELPER: MONTH ARITHMETIC
# ===========================================================================

def add_months(ts: pd.Timestamp, months: int) -> pd.Timestamp:
    """Add an integer number of months to a Timestamp."""
    month = ts.month - 1 + months
    year  = ts.year + month // 12
    month = month % 12 + 1
    day   = min(ts.day, pd.Timestamp(year=year, month=month, day=1).days_in_month)
    return pd.Timestamp(year=year, month=month, day=day)


def _total_months(start: pd.Timestamp, end: pd.Timestamp) -> int:
    """Return the number of complete calendar months between two timestamps."""
    return (end.year - start.year) * 12 + (end.month - start.month)


def adapt_windows_to_data(
    data_start:  pd.Timestamp,
    data_end:    pd.Timestamp,
    is_months:   int,
    oos_months:  int,
    roll_months: int,
    min_oos_months:   int = 3,
    min_is_months:    int = 6,
) -> Tuple[int, int, int, str]:
    """
    Auto-scale IS/OOS/roll sizes to fit the available data range.

    Rules (applied in order):
      1. If data >= IS + OOS months  → use requested sizes as-is.
      2. If data < IS + OOS but >= min_is + min_oos → scale IS down
         proportionally while keeping OOS fixed at oos_months; if that
         still doesn't fit, reduce OOS to min_oos_months too.
      3. Roll is capped at max(1, oos_months) to guarantee at least 1 window.

    Returns
    -------
    (adj_is, adj_oos, adj_roll, warning_msg)
    warning_msg is "" when no adjustment was necessary.
    """
    available    = _total_months(data_start, data_end)
    warnings_out = []

    adj_is   = is_months
    adj_oos  = oos_months
    adj_roll = roll_months

    if available >= adj_is + adj_oos:
        return adj_is, adj_oos, adj_roll, ""

    # Try keeping OOS fixed, shrink IS
    candidate_is = available - adj_oos
    if candidate_is >= min_is_months:
        warnings_out.append(
            f"Data range is {available} months (< requested IS={is_months} + OOS={oos_months} = "
            f"{is_months + oos_months}m). Auto-reduced IS from {is_months}m → {candidate_is}m "
            f"while keeping OOS={adj_oos}m. Use --is-months / --oos-months to override."
        )
        adj_is = candidate_is
    else:
        # Also shrink OOS to minimum
        adj_oos      = min_oos_months
        candidate_is = available - adj_oos
        if candidate_is < min_is_months:
            return adj_is, adj_oos, adj_roll, (
                f"Data range {available}m is too short even for minimum windows "
                f"(IS>={min_is_months}m + OOS>={min_oos_months}m = "
                f"{min_is_months + min_oos_months}m required). "
                f"Provide at least {min_is_months + min_oos_months} months of data, "
                f"or extend --end-date / shorten --is-months."
            )
        warnings_out.append(
            f"Data range is {available} months. Auto-reduced IS={is_months}m → {candidate_is}m "
            f"AND OOS={oos_months}m → {adj_oos}m to fit available data."
        )
        adj_is = candidate_is

    # Cap roll so we always get ≥1 window
    adj_roll = min(adj_roll, max(1, adj_oos))
    if adj_roll != roll_months:
        warnings_out.append(
            f"Roll-forward step auto-reduced from {roll_months}m → {adj_roll}m "
            f"to guarantee at least 1 walk-forward window."
        )

    return adj_is, adj_oos, adj_roll, " | ".join(warnings_out)


def build_windows(
    data_start:  pd.Timestamp,
    data_end:    pd.Timestamp,
    is_months:   int,
    oos_months:  int,
    roll_months: int,
) -> List[Dict]:
    """
    Generate walk-forward windows.

    Each window dict contains:
        is_start, is_end   — in-sample bounds (inclusive)
        oos_start, oos_end — out-of-sample bounds (inclusive)
        window_id          — integer (1-based)
    """
    windows  = []
    is_start = data_start
    wid      = 1

    while True:
        is_end    = add_months(is_start, is_months) - pd.Timedelta(days=1)
        oos_start = is_end + pd.Timedelta(days=1)
        oos_end   = add_months(oos_start, oos_months) - pd.Timedelta(days=1)

        if oos_end > data_end:
            break

        windows.append({
            "window_id":  wid,
            "is_start":   is_start,
            "is_end":     is_end,
            "oos_start":  oos_start,
            "oos_end":    oos_end,
        })

        is_start = add_months(is_start, roll_months)
        wid += 1

    return windows


# ===========================================================================
# HELPER: PARAMETER COMBINATIONS
# ===========================================================================

def build_param_combinations(grid: Dict) -> List[Dict]:
    """Return all parameter combinations from the grid."""
    keys   = list(grid.keys())
    values = list(grid.values())
    combos = []
    for combo in itertools.product(*values):
        params = dict(zip(keys, combo))
        params.update(FIXED_PARAMS)
        combos.append(params)
    return combos


# ===========================================================================
# OPT-1: INDICATOR KEY AND BUCKET GROUPING
# ===========================================================================

def _ind_key(params: Dict) -> Tuple:
    """
    OPT-1: Cache key for indicator computation.
    Only sma_fast and sma_slow affect compute_indicators() output.
    """
    return (params["sma_fast"], params["sma_slow"])


def _group_combos_by_indicator(param_combos: List[Dict]) -> Dict[Tuple, List[Dict]]:
    """
    OPT-1: Group 1,296 combos into 12 indicator buckets.
    Full grid: 3 sma_fast × 4 sma_slow = 12 unique indicator sets.
    Each bucket contains 972 / 9 = 108 combos sharing the same indicator data.
    """
    groups: Dict[Tuple, List[Dict]] = {}
    for p in param_combos:
        k = _ind_key(p)
        groups.setdefault(k, []).append(p)
    return groups


# ===========================================================================
# HELPER: METRIC EXTRACTION
# ===========================================================================

def extract_sharpe(result: Dict) -> float:
    """Safely extract Sharpe ratio from backtest result dict."""
    try:
        m = result.get("metrics", {})
        s = m.get("sharpe_ratio", m.get("sharpe", None))
        if s is None:
            eq = result.get("equity_curve", [])
            if len(eq) < 20:
                return -99.0
            returns = pd.Series([r["equity"] for r in eq]).pct_change().dropna()
            if returns.std() == 0:
                return 0.0
            return float((returns.mean() / returns.std()) * np.sqrt(252))
        return float(s) if np.isfinite(float(s)) else -99.0
    except Exception:
        return -99.0


def extract_cagr(result: Dict) -> float:
    try:
        m = result.get("metrics", {})
        # Script 16 emits "cagr_pct" and "annualized_return_pct"; keep legacy aliases
        c = m.get("cagr_pct",
            m.get("cagr",
            m.get("annualized_return_pct",
            m.get("annualized_return", None))))
        return float(c) if c is not None and np.isfinite(float(c)) else 0.0
    except Exception:
        return 0.0


def extract_max_dd(result: Dict) -> float:
    """
    Extract max drawdown from backtest result.

    Returns the drawdown as a negative percentage value (e.g. -15.3 for -15.3%).
    Returns -999.0 as a sentinel when the key is absent or the value is not finite.
    Sentinel is -999.0 (not -1.0) to avoid collision with real drawdowns > 1%,
    which are negative numbers that would incorrectly satisfy '<= -1.0'.
    """
    try:
        m  = result.get("metrics", {})
        # Script 16 emits "max_drawdown_pct"; keep legacy aliases for safety
        dd = m.get("max_drawdown_pct",
             m.get("max_drawdown",
             m.get("maximum_drawdown", None)))
        return float(dd) if dd is not None and np.isfinite(float(dd)) else -999.0
    except Exception:
        return -999.0


def extract_total_trades(result: Dict) -> int:
    try:
        t = result.get("trades", [])
        if isinstance(t, list):
            return len(t)
        m = result.get("metrics", {})
        return int(m.get("total_trades", 0))
    except Exception:
        return 0


# ===========================================================================
# OPT-2: MODULE-LEVEL WORKER (picklable by ProcessPoolExecutor)
# ===========================================================================

def _bucket_worker(args: tuple) -> tuple:
    """
    OPT-2: ProcessPoolExecutor worker — must be at module level for pickle.

    Receives one indicator bucket (all combos sharing the same sma_fast/sma_slow).
    Imports Script 16 dynamically so subprocesses don't depend on sys.path state.

    Args (positional tuple for pickling efficiency):
        script16_path  : str  — absolute path to 16_backtest_engine.py
        price_slice    : dict {sym -> DataFrame} — IS-sliced price data (OPT-3)
        bucket_combos  : list of param dicts (all same sma_fast, sma_slow)
        is_start_str   : str  — IS window start (YYYY-MM-DD)
        is_end_str     : str  — IS window end   (YYYY-MM-DD)
        metadata       : dict or None
        vix_dict       : dict {str(date): float} or None  (Series → dict for pickle)
        window_id      : int

    Returns:
        (results_list, best_sharpe, best_params)
        results_list : [{params, is_sharpe, is_cagr, is_max_dd, is_trades}]
        best_sharpe  : float
        best_params  : dict or None
    """
    (script16_path, price_slice, bucket_combos,
     is_start_str, is_end_str,
     metadata, vix_dict, window_id) = args

    # Dynamic import of Script 16 in subprocess context
    _spec = importlib.util.spec_from_file_location("_be16_worker", script16_path)
    _mod  = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)

    _run_bt = _mod.run_backtest_from_data
    _prec   = _mod.precompute_indicators

    is_start = pd.Timestamp(is_start_str)
    is_end   = pd.Timestamp(is_end_str)

    # Reconstitute VIX series if provided
    vix_data: Optional[pd.Series] = None
    if vix_dict:
        vix_data = pd.Series(vix_dict)
        vix_data.index = pd.to_datetime(vix_data.index)

    # OPT-1: precompute indicators once for this bucket (all combos share same key)
    try:
        ind_data = _prec(price_slice, bucket_combos[0])
    except Exception:
        ind_data = None

    results     = []
    best_sharpe = -np.inf
    best_params = None

    for params in bucket_combos:
        try:
            result = _run_bt(
                price_data     = price_slice,
                indicator_data = ind_data,
                start_date     = is_start,
                end_date       = is_end,
                params         = params,
                metadata       = metadata,
                vix_data       = vix_data,
                log_level      = logging.CRITICAL,
            )
            sharpe = _extract_sharpe_simple(result)
            cagr   = _extract_cagr_simple(result)
            max_dd = _extract_max_dd_simple(result)
            trades = _extract_trades_simple(result)
        except Exception:
            sharpe, cagr, max_dd, trades = -99.0, 0.0, -999.0, 0

        results.append({
            "params":    {k: v for k, v in params.items() if k in PARAM_GRID},
            "is_sharpe": round(sharpe, 4),
            "is_cagr":   round(cagr,   4),
            "is_max_dd": round(max_dd,  4),
            "is_trades": trades,
        })

        if sharpe > best_sharpe:
            best_sharpe = sharpe
            best_params = params

    return results, best_sharpe, best_params


# Lightweight extraction helpers used inside the worker
# (avoids importing script-17-level functions in subprocess)

def _extract_sharpe_simple(result: Dict) -> float:
    try:
        m = result.get("metrics", {})
        s = m.get("sharpe_ratio", m.get("sharpe"))
        if s is None:
            eq = result.get("equity_curve", [])
            if len(eq) < 20:
                return -99.0
            rets = pd.Series([r["equity"] for r in eq]).pct_change().dropna()
            return float((rets.mean() / rets.std()) * np.sqrt(252)) if rets.std() > 0 else 0.0
        return float(s) if np.isfinite(float(s)) else -99.0
    except Exception:
        return -99.0


def _extract_cagr_simple(result: Dict) -> float:
    try:
        m = result.get("metrics", {})
        c = m.get("cagr_pct",
            m.get("cagr",
            m.get("annualized_return_pct",
            m.get("annualized_return"))))
        return float(c) if c is not None and np.isfinite(float(c)) else 0.0
    except Exception:
        return 0.0


def _extract_max_dd_simple(result: Dict) -> float:
    try:
        m  = result.get("metrics", {})
        dd = m.get("max_drawdown_pct",
             m.get("max_drawdown",
             m.get("maximum_drawdown")))
        return float(dd) if dd is not None and np.isfinite(float(dd)) else -999.0
    except Exception:
        return -999.0
    except Exception:
        return -1.0


def _extract_trades_simple(result: Dict) -> int:
    try:
        t = result.get("trades", [])
        if isinstance(t, list):
            return len(t)
        return int(result.get("metrics", {}).get("total_trades", 0))
    except Exception:
        return 0


# ===========================================================================
# IN-SAMPLE OPTIMIZATION: grid search for single window
# ===========================================================================

def optimize_in_sample(
    price_data:     Dict,
    param_combos:   List[Dict],
    is_start:       pd.Timestamp,
    is_end:         pd.Timestamp,
    initial_equity: float,
    vix_data:       Optional[pd.Series],
    metadata:       Optional[Dict],
    window_id:      int,
    logger:         logging.Logger,
    n_workers:      int = 1,
) -> Tuple[Dict, float, List[Dict], Optional[Dict]]:
    """
    Run all parameter combinations on the in-sample period.

    OPT-1: Groups 1,296 combos into 12 indicator buckets by (sma_fast, sma_slow).
           precompute_indicators() called once per bucket (9×) not per combo (972×).

    OPT-2: With n_workers > 1, distributes indicator buckets across processes.
           Each worker independently computes its indicator set and runs backtests.

    OPT-3: Price data pre-sliced to IS window + warmup before the combo loop.
           Eliminates 1,296 redundant date-filter operations inside each backtest.

    OPT-5: Early exit if the first indicator bucket returns all Sharpe=-99.
           Indicates IS window too short for indicator warmup; skips remaining
           buckets to avoid hundreds of wasted backtests.

    Returns
    -------
    best_params    : parameter dict with highest IS Sharpe
    best_is_sharpe : float
    all_results    : [{params, sharpe, cagr, max_dd, trades}] for every combo
    best_ind_data  : pre-computed indicator dict for best_params (for OPT-4)
    """
    n_combos = len(param_combos)

    # OPT-3: Pre-slice price data to IS window + warmup (done once, not per combo)
    max_sma = max(p.get("sma_slow", 200) for p in param_combos)
    warmup  = pd.DateOffset(days=max_sma * 2)
    price_slice: Dict[str, pd.DataFrame] = {
        sym: df[df.index >= (is_start - warmup)]
        for sym, df in price_data.items()
    }

    # Guard: check we have enough trading days in the IS window
    _sample_sym  = next(iter(price_slice))
    _sample_df   = price_slice[_sample_sym]
    _is_bars     = _sample_df[(_sample_df.index >= is_start) & (_sample_df.index <= is_end)]
    _max_sma     = max(PARAM_GRID.get("sma_slow", [200]))
    _min_bars    = _max_sma + 20
    if len(_is_bars) < _min_bars:
        logger.warning(
            f"  Window {window_id}: IS period has only {len(_is_bars)} trading bars "
            f"(need >= {_min_bars} for SMA_{_max_sma} warmup). "
            f"All {n_combos} combos may return Sharpe=-99. "
            f"Consider: --is-months larger, earlier --start-date, or --fast-mode."
        )

    logger.info(
        f"  Window {window_id} | IS {is_start.date()} → {is_end.date()} | "
        f"Testing {n_combos} combinations "
        f"({'parallel ' + str(n_workers) + ' workers' if n_workers > 1 else 'serial'}) ..."
    )

    # OPT-1: Group combos into indicator buckets
    buckets       = _group_combos_by_indicator(param_combos)
    n_buckets     = len(buckets)
    bucket_list   = list(buckets.items())   # [(ind_key, [combos]), ...]

    logger.info(
        f"  Window {window_id}: {n_combos} combos in {n_buckets} indicator buckets "
        f"({n_combos // n_buckets} combos/bucket)"
    )

    all_results:  List[Dict]      = []
    best_sharpe:  float           = -np.inf
    best_params:  Optional[Dict]  = None
    best_ind_data: Optional[Dict] = None
    n_errors:     int             = 0
    first_error:  Optional[str]   = None

    # ------------------------------------------------------------------
    # PARALLEL PATH  (OPT-2)
    # ------------------------------------------------------------------
    if n_workers > 1:
        effective_workers = min(n_workers, n_buckets)
        logger.info(
            f"  Window {window_id}: launching {effective_workers} workers "
            f"for {n_buckets} indicator buckets ..."
        )

        # Serialise VIX as dict for pickle safety (pd.Series w/ DatetimeIndex)
        vix_dict: Optional[Dict] = None
        if vix_data is not None:
            vix_dict = {str(k): float(v) for k, v in vix_data.items() if not pd.isna(v)}

        # Build work items — one per indicator bucket
        work_items = [
            (
                _SCRIPT16_PATH,
                price_slice,
                bucket_combos,
                is_start.strftime("%Y-%m-%d"),
                is_end.strftime("%Y-%m-%d"),
                metadata,
                vix_dict,
                window_id,
            )
            for _, bucket_combos in bucket_list
        ]

        with ProcessPoolExecutor(max_workers=effective_workers) as pool:
            futures = {
                pool.submit(_bucket_worker, item): idx
                for idx, item in enumerate(work_items)
            }
            completed = 0
            for future in as_completed(futures):
                bucket_results, bucket_best_sharpe, bucket_best_params = future.result()
                all_results.extend(bucket_results)
                if bucket_best_sharpe > best_sharpe:
                    best_sharpe = bucket_best_sharpe
                    best_params = bucket_best_params
                completed += 1
                if completed % max(1, n_buckets // 3) == 0 or completed == n_buckets:
                    logger.info(
                        f"    [{completed}/{n_buckets} buckets done] "
                        f"best IS Sharpe: {best_sharpe:.4f}"
                    )

        # OPT-4: For OOS reuse, compute the indicator set for best params (1 of 9, cheap)
        if best_params is not None:
            try:
                best_ind_data = precompute_indicators(price_slice, best_params)
            except Exception:
                best_ind_data = None

    # ------------------------------------------------------------------
    # SERIAL PATH  (OPT-1 + OPT-5 applied)
    # ------------------------------------------------------------------
    else:
        for bucket_idx, (ind_key_val, bucket_combos) in enumerate(bucket_list):
            # OPT-1: precompute indicators once per bucket
            try:
                ind_data = precompute_indicators(price_slice, bucket_combos[0])
            except Exception as exc:
                logger.warning(
                    f"  Window {window_id}: indicator precompute failed "
                    f"for key={ind_key_val}: {exc}"
                )
                ind_data = None

            bucket_results      = []
            bucket_best_sharpe  = -np.inf
            bucket_best_params  = None

            for params in bucket_combos:
                try:
                    result = run_backtest_from_data(
                        price_data     = price_slice,
                        indicator_data = ind_data,
                        start_date     = is_start,
                        end_date       = is_end,
                        params         = params,
                        metadata       = metadata,
                        vix_data       = vix_data,
                        log_level      = logging.CRITICAL,
                    )
                    sharpe = extract_sharpe(result)
                    cagr   = extract_cagr(result)
                    max_dd = extract_max_dd(result)
                    trades = extract_total_trades(result)
                except Exception as exc:
                    n_errors += 1
                    if first_error is None:
                        first_error = f"Bucket {bucket_idx}, combo: {type(exc).__name__}: {exc}"
                        logger.warning(
                            f"  Window {window_id} | first backtest error: "
                            f"{type(exc).__name__}: {exc}"
                        )
                    sharpe, cagr, max_dd, trades = -99.0, 0.0, -999.0, 0

                r = {
                    "params":    {k: v for k, v in params.items() if k in PARAM_GRID},
                    "is_sharpe": round(sharpe, 4),
                    "is_cagr":   round(cagr,   4),
                    "is_max_dd": round(max_dd,  4),
                    "is_trades": trades,
                }
                bucket_results.append(r)

                if sharpe > bucket_best_sharpe:
                    bucket_best_sharpe = sharpe
                    bucket_best_params = params

            all_results.extend(bucket_results)

            # Update global best; track indicator data for OPT-4
            if bucket_best_sharpe > best_sharpe:
                best_sharpe   = bucket_best_sharpe
                best_params   = bucket_best_params
                best_ind_data = ind_data

            logger.info(
                f"    Bucket {bucket_idx + 1}/{n_buckets} "
                f"(sma_fast={ind_key_val[0]}, sma_slow={ind_key_val[1]}) | "
                f"best Sharpe: {bucket_best_sharpe:.4f} | "
                f"global best: {best_sharpe:.4f}"
            )

            # OPT-5: Early exit if all combos in the first bucket failed
            # This indicates IS window is too short for indicator warmup.
            # No point running remaining 8 buckets.
            if bucket_idx == 0 and all(r["is_sharpe"] <= -99.0 for r in bucket_results):
                logger.error(
                    f"  Window {window_id}: ALL {len(bucket_results)} combos in bucket 1 "
                    f"returned Sharpe=-99. IS window likely too short for indicator warmup "
                    f"(SMA_{ind_key_val[1]} needs ~{ind_key_val[1] + 20} bars). "
                    f"Skipping remaining {n_buckets - 1} buckets "
                    f"({(n_buckets - 1) * len(bucket_results)} backtests saved). "
                    f"Falling back to production defaults for OOS."
                )
                break

    # ------------------------------------------------------------------
    # Post-loop diagnostics
    # ------------------------------------------------------------------
    if n_errors > 0:
        logger.warning(
            f"  Window {window_id}: {n_errors}/{n_combos} combos raised exceptions. "
            f"First: {first_error}"
        )

    if best_params is None or best_sharpe <= -99.0:
        logger.error(
            f"  Window {window_id}: ALL combos returned Sharpe=-99. "
            f"Likely causes:\n"
            f"    1. IS window too short for indicator warmup\n"
            f"    2. run_backtest_from_data() raising exceptions\n"
            f"    3. No symbols pass trend qualification in this IS period\n"
            f"  Falling back to production defaults."
        )
        best_params   = {**BT_DEFAULTS, **FIXED_PARAMS}
        best_sharpe   = -99.0
        best_ind_data = None

    # Sort by IS Sharpe descending
    all_results.sort(key=lambda x: x["is_sharpe"], reverse=True)

    logger.info(
        f"  Window {window_id} IS best: "
        f"Sharpe={best_sharpe:.4f} | "
        f"params={_format_params(best_params)}"
    )

    return best_params, best_sharpe, all_results, best_ind_data


# ===========================================================================
# OUT-OF-SAMPLE VALIDATION: single locked parameter set
# ===========================================================================

def validate_out_of_sample(
    price_data:       Dict,
    best_params:      Dict,
    oos_start:        pd.Timestamp,
    oos_end:          pd.Timestamp,
    initial_equity:   float,
    vix_data:         Optional[pd.Series],
    metadata:         Optional[Dict],
    window_id:        int,
    logger:           logging.Logger,
    cached_ind_data:  Optional[Dict] = None,  # kept for API compat; see note below
) -> Tuple[float, Dict]:
    """
    Run the best IS parameters on the OOS period.

    OPT-4 NOTE (v3.4 revision):
        The IS indicator cache (cached_ind_data) was computed from a price slice
        that starts at is_start - warmup and runs to the end of price_data.
        In principle this covers the OOS period, but passing a different
        price_data/indicator_data pair to run_backtest_from_data risks subtle
        index-alignment bugs when the backtest engine reconciles the two.
        To guarantee correctness, OOS indicators are always recomputed fresh
        from the full price_data with an explicit warmup pre-slice.  The extra
        cost is one precompute_indicators() call per window (~seconds vs ~hours
        for IS optimisation), so the trade-off strongly favours correctness.
        cached_ind_data is accepted but intentionally ignored.

    OOS warmup pre-slice:
        Price data fed to the OOS backtest is sliced from
        (oos_start - max_sma_slow * 2 calendar days) onward.  This ensures
        SMA_350 (the longest moving average in the grid) has sufficient bars
        at the very first OOS bar.

    Returns
    -------
    oos_sharpe  : float
    oos_metrics : dict
    """
    logger.info(
        f"  Window {window_id} | OOS {oos_start.date()} → {oos_end.date()} | "
        f"Validating locked params ..."
    )

    # OOS warmup: always pre-slice so SMA warmup is satisfied at oos_start
    max_sma = max(best_params.get("sma_slow", 200),
                  max(PARAM_GRID.get("sma_slow", [200])))
    oos_warmup    = pd.DateOffset(days=max_sma * 2)
    oos_warmup_start = oos_start - oos_warmup
    price_oos: Dict[str, pd.DataFrame] = {
        sym: df[df.index >= oos_warmup_start]
        for sym, df in price_data.items()
        if not df[df.index >= oos_warmup_start].empty
    }

    n_symbols_oos = len(price_oos)
    logger.debug(
        f"  Window {window_id} OOS: {n_symbols_oos} symbols in price slice "
        f"(warmup from {oos_warmup_start.date()})"
    )

    if n_symbols_oos == 0:
        logger.error(
            f"  Window {window_id} OOS: No symbols remain after warmup pre-slice "
            f"(oos_warmup_start={oos_warmup_start.date()}). "
            f"Check that price data extends far enough before {oos_start.date()}."
        )
        return -99.0, {
            "oos_sharpe": -99.0, "oos_cagr": 0.0,
            "oos_max_dd": -1.0,  "oos_trades": 0,
            "oos_diagnostic": "NO_SYMBOLS_AFTER_WARMUP_SLICE",
        }

    try:
        # Always recompute fresh indicators for OOS (see OPT-4 NOTE above)
        ind_data = precompute_indicators(price_oos, best_params)

        result = run_backtest_from_data(
            price_data     = price_oos,
            indicator_data = ind_data,
            start_date     = oos_start,
            end_date       = oos_end,
            params         = best_params,
            metadata       = metadata,
            vix_data       = vix_data,
            log_level      = logging.CRITICAL,
        )
        sharpe = extract_sharpe(result)
        cagr   = extract_cagr(result)
        max_dd = extract_max_dd(result)
        trades = extract_total_trades(result)

    except Exception as exc:
        logger.warning(
            f"  Window {window_id} OOS backtest raised exception: "
            f"{type(exc).__name__}: {exc}"
        )
        sharpe, cagr, max_dd, trades = -99.0, 0.0, -999.0, 0

    # -----------------------------------------------------------------
    # Sentinel / diagnostic detection
    # -----------------------------------------------------------------
    diagnostics: List[str] = []

    if trades == 0:
        diagnostics.append("ZERO_TRADES")
        logger.warning(
            f"  Window {window_id} OOS DIAGNOSTIC: 0 trades executed. "
            f"Likely causes: (1) no symbols pass trend/ADX filter in this period, "
            f"(2) SMA warmup insufficient at OOS start, "
            f"(3) all qualified positions hit stop on day 1."
        )

    # Detect open-but-unclosed positions (force-closed at window end by backtest engine).
    # These contribute to the equity curve but never appear as completed round-trips in
    # trade stats until _close_all_positions fires.  If the count is high relative to
    # total trades, the OOS window is too short for the chosen SMA parameters.
    try:
        raw_trades     = result.get("trades", [])
        force_closed   = sum(1 for t in raw_trades if t.get("exit_reason") == "backtest_end")
        normal_exits   = trades - force_closed
        if force_closed > 0:
            diagnostics.append(f"FORCE_CLOSED={force_closed}")
            logger.info(
                f"  Window {window_id} OOS: {force_closed}/{trades} trades force-closed "
                f"at window end (exit_reason='backtest_end'). "
                f"{normal_exits} closed via stop/signal during OOS. "
                f"High force-close ratio means OOS window ({(oos_end - oos_start).days}d) "
                f"is short relative to sma_slow={best_params.get('sma_slow')} — "
                f"these positions are marked-to-market correctly in the equity curve."
            )
    except Exception:
        pass

    if max_dd <= -999.0:
        # -999.0 is the sentinel returned by extract_max_dd when "max_drawdown_pct"
        # is absent from the metrics dict or is non-finite.
        # Changed from -1.0 to -999.0 to avoid collision with real drawdowns
        # (any drawdown > 1% is a negative number that would satisfy <= -1.0).
        diagnostics.append("MAXDD_SENTINEL_NO_DATA")
        logger.warning(
            f"  Window {window_id} OOS DIAGNOSTIC: MaxDD sentinel (-999). "
            f"'max_drawdown_pct' key absent or non-finite in backtest metrics. "
            f"Typical cause: equity curve is empty or all-NaN (zero trading days)."
        )

    if sharpe < -5.0:
        diagnostics.append(f"EXTREME_NEGATIVE_SHARPE={sharpe:.4f}")
        logger.warning(
            f"  Window {window_id} OOS DIAGNOSTIC: Extreme negative Sharpe ({sharpe:.4f}). "
            f"Check equity curve in backtest output for this window."
        )

    # Display MaxDD as N/A when it is the sentinel rather than a real drawdown.
    # Format as plain float (value is already in pct, e.g. -15.3 means -15.3%).
    max_dd_display = f"{max_dd:.2f}%" if max_dd > -999.0 else "N/A (no equity data)"

    logger.info(
        f"  Window {window_id} OOS: "
        f"Sharpe={sharpe:.4f} | CAGR={cagr:.2f}% | MaxDD={max_dd_display} | "
        f"Trades={trades}"
        + (f" | DIAGNOSTICS: {', '.join(diagnostics)}" if diagnostics else "")
    )

    oos_metrics = {
        "oos_sharpe":     round(sharpe, 4),
        "oos_cagr":       round(cagr,   4),
        "oos_max_dd":     round(max_dd,  4),
        "oos_trades":     trades,
        "oos_diagnostics": diagnostics,
    }
    return sharpe, oos_metrics


# ===========================================================================
# STABILITY ANALYSIS
# ===========================================================================

def compute_stability_metrics(windows: List[Dict]) -> Dict:
    """
    Compute aggregate stability metrics across all walk-forward windows.

    Stability Ratio uses only windows where IS Sharpe > 0.05 (meaningful positive
    in-sample result).  Windows where IS <= 0.05 are "inverted" — the optimizer
    found no in-sample edge (hard regime) — and including them in a ratio calculation
    produces misleading or undefined values.  Their OOS results still count toward
    OOS Consistency and parameter selection.
    """
    _IS_MEANINGFUL_THRESHOLD = 0.05

    is_sharpes  = [w["is_best_sharpe"] for w in windows]
    oos_sharpes = [w["oos_sharpe"]     for w in windows]

    # Partition windows: normal (IS > threshold) vs inverted (IS <= threshold)
    normal_windows   = [w for w in windows if w["is_best_sharpe"] > _IS_MEANINGFUL_THRESHOLD]
    inverted_windows = [w for w in windows if w["is_best_sharpe"] <= _IS_MEANINGFUL_THRESHOLD]
    n_inverted       = len(inverted_windows)

    # Aggregate ratio: only over normal windows
    if normal_windows:
        avg_is_normal  = float(np.mean([w["is_best_sharpe"] for w in normal_windows]))
        avg_oos_normal = float(np.mean([w["oos_sharpe"]     for w in normal_windows]))
        stability_ratio = avg_oos_normal / avg_is_normal if avg_is_normal > 0 else -99.0
    else:
        avg_is_normal   = float(np.mean(is_sharpes))
        avg_oos_normal  = float(np.mean(oos_sharpes))
        stability_ratio = -99.0

    # Overall averages (all windows, for reporting)
    avg_is  = float(np.mean(is_sharpes))
    avg_oos = float(np.mean(oos_sharpes))

    # OOS Consistency counts ALL windows (inverted windows can still have positive OOS)
    oos_positive    = sum(1 for s in oos_sharpes if s > 0)
    oos_n           = len(oos_sharpes)
    oos_consistency = oos_positive / oos_n if oos_n > 0 else 0.0

    if stability_ratio > STABILITY_EXCELLENT:
        stability_grade = "EXCELLENT"
    elif stability_ratio > STABILITY_GOOD:
        stability_grade = "GOOD"
    elif stability_ratio > STABILITY_ACCEPTABLE:
        stability_grade = "ACCEPTABLE"
    else:
        stability_grade = "FAIL"

    if oos_consistency >= OOS_CONSISTENCY_PASS:
        consistency_grade = "PASS"
    elif oos_consistency >= OOS_CONSISTENCY_WARN:
        consistency_grade = "WARNING"
    else:
        consistency_grade = "FAIL"

    # Parameter stability (coefficient of variation per parameter)
    param_cv   = {}
    all_params = PARAM_GRID.keys()
    for param in all_params:
        vals = [w["best_params"].get(param) for w in windows if w.get("best_params")]
        vals = [v for v in vals if v is not None]
        mean = float(np.mean(vals)) if vals else None
        std  = float(np.std(vals, ddof=1)) if len(vals) > 1 else None
        if mean is not None and std is not None and mean != 0:
            cv = std / abs(mean)
        else:
            cv = 0.0
        if cv < PARAM_CV_EXCELLENT:
            cv_grade = "EXCELLENT"
        elif cv < PARAM_CV_GOOD:
            cv_grade = "GOOD"
        else:
            cv_grade = "UNSTABLE"
        param_cv[param] = {
            "mean":  round(mean, 4) if mean is not None else None,
            "std":   round(std,  4) if std  is not None else None,
            "cv":    round(cv,   4),
            "grade": cv_grade,
        }

    # Grid-edge red flags
    param_edges = {}
    for param, pvals in PARAM_GRID.items():
        edge_hits = 0
        for w in windows:
            val = (w.get("best_params") or {}).get(param)
            if val in (pvals[0], pvals[-1]):
                edge_hits += 1
        param_edges[param] = {
            "edge_hits":     edge_hits,
            "total_windows": oos_n,
            "pct_at_edge":   round(edge_hits / oos_n, 4) if oos_n > 0 else 0.0,
        }

    red_flags = []
    for param, info in param_edges.items():
        if info["pct_at_edge"] > 0.6:
            red_flags.append(
                f"EXTREME_PARAM: '{param}' at grid edge in "
                f"{info['edge_hits']}/{info['total_windows']} windows "
                f"({info['pct_at_edge']:.0%}) — expand search space"
            )

    # OOS performance trend (slope)
    if len(oos_sharpes) >= 3:
        x     = np.arange(len(oos_sharpes))
        slope, _ = np.polyfit(x, oos_sharpes, 1)
        if slope < -0.05:
            red_flags.append(
                f"DECLINING_OOS: OOS Sharpe trend slope={slope:.4f} "
                f"(strategy may be decaying)"
            )
    else:
        slope = 0.0

    return {
        "n_windows":              oos_n,
        "n_normal_windows":       len(normal_windows),
        "n_inverted_windows":     n_inverted,
        "avg_is_sharpe":          round(avg_is, 4),
        "avg_oos_sharpe":         round(avg_oos, 4),
        "avg_is_sharpe_normal":   round(avg_is_normal, 4),
        "avg_oos_sharpe_normal":  round(avg_oos_normal, 4),
        "stability_ratio":        round(stability_ratio, 4),
        "stability_grade":        stability_grade,
        "oos_positive_windows":   oos_positive,
        "oos_consistency":        round(oos_consistency, 4),
        "oos_consistency_grade":  consistency_grade,
        "oos_sharpe_trend_slope": round(float(slope), 6),
        "param_cv":               param_cv,
        "param_edges":            param_edges,
        "red_flags":              red_flags,
    }


# ===========================================================================
# FINAL PARAMETER SELECTION
# ===========================================================================

def select_final_parameters(windows: List[Dict], stability: Dict) -> Dict:
    """
    Select final recommended parameters for live deployment.

    Logic:
      1. For each unique parameter set that appeared as best-IS across windows,
         compute its average OOS Sharpe.
      2. Prefer parameters NOT at grid edges.
      3. Return the set with the highest median OOS Sharpe.
    """
    param_key_fn = lambda p: json.dumps(
        {k: p[k] for k in sorted(PARAM_GRID.keys()) if k in p}, sort_keys=True
    )

    candidate_oos:    Dict[str, List[float]] = {}
    candidate_params: Dict[str, Dict]        = {}

    for w in windows:
        bp = w.get("best_params")
        if bp is None:
            continue
        key = param_key_fn(bp)
        candidate_oos.setdefault(key, []).append(w["oos_sharpe"])
        candidate_params[key] = bp

    if not candidate_params:
        return {**BT_DEFAULTS, **FIXED_PARAMS}

    def score(key: str) -> Tuple[float, float]:
        oos_vals = candidate_oos[key]
        params   = candidate_params[key]
        median   = float(np.median(oos_vals))
        edge_pen = 0.0
        for param, pvals in PARAM_GRID.items():
            if params.get(param) in (pvals[0], pvals[-1]):
                edge_pen += 0.05
        return median - edge_pen, median

    best_key  = max(candidate_params.keys(), key=lambda k: score(k)[0])
    best_full = {**BT_DEFAULTS, **FIXED_PARAMS, **candidate_params[best_key]}

    scored = sorted(
        [(k, score(k)) for k in candidate_params],
        key=lambda x: x[1][0],
        reverse=True,
    )

    top5 = []
    for k, (adj_score, median) in scored[:5]:
        top5.append({
            "params":              {pk: candidate_params[k][pk] for pk in PARAM_GRID if pk in candidate_params[k]},
            "median_oos_sharpe":   round(median, 4),
            "adj_score":           round(adj_score, 4),
            "n_windows_selected":  len(candidate_oos[k]),
        })

    return {
        "recommended":       {k: v for k, v in candidate_params[best_key].items() if k in PARAM_GRID},
        "full_params":       best_full,
        "median_oos_sharpe": round(score(best_key)[1], 4),
        "top5_candidates":   top5,
        "selection_rationale": (
            f"Highest median OOS Sharpe after edge penalty adjustment. "
            f"Stability ratio={stability['stability_ratio']:.3f} "
            f"({stability['stability_grade']}), "
            f"OOS consistency={stability['oos_consistency']:.1%} "
            f"({stability['oos_consistency_grade']})."
        ),
    }


# ===========================================================================
# MAIN WALK-FORWARD LOOP
# ===========================================================================

def run_walk_forward(
    price_data:     Dict,
    metadata:       Dict,
    vix_data:       Optional[pd.Series],
    data_start:     pd.Timestamp,
    data_end:       pd.Timestamp,
    initial_equity: float,
    is_months:      int,
    oos_months:     int,
    roll_months:    int,
    param_combos:   List[Dict],
    logger:         logging.Logger,
    n_workers:      int = 1,
) -> List[Dict]:
    """
    Execute the full walk-forward loop.

    For each window:
      1. Optimize on IS data  → find best Sharpe parameter set  (OPT-1/2/3/5)
      2. Validate on OOS data → record OOS performance           (OPT-4)
      3. Record stability ratio for the window

    Returns list of window result dicts.
    """
    adj_is, adj_oos, adj_roll, warn_msg = adapt_windows_to_data(
        data_start  = data_start,
        data_end    = data_end,
        is_months   = is_months,
        oos_months  = oos_months,
        roll_months = roll_months,
    )
    if warn_msg:
        logger.warning(f"Window auto-adjustment: {warn_msg}")

    windows = build_windows(data_start, data_end, adj_is, adj_oos, adj_roll)

    if not windows:
        available = _total_months(data_start, data_end)
        earlier   = (data_start - pd.DateOffset(
                        months=max(0, is_months + oos_months - available)
                     )).strftime("%Y-%m-%d")
        raise ValueError(
            "\n"
            f"  Cannot build any walk-forward windows.\n"
            f"  Data range : {data_start.date()} -> {data_end.date()} "
            f"({available} months)\n"
            f"  Requested  : IS={is_months}m + OOS={oos_months}m = "
            f"{is_months + oos_months}m minimum\n"
            "\n"
            "  Solutions:\n"
            f"    1. Extend your date range  : --start-date {earlier}\n"
            "    2. Reduce IS window        : --is-months 12\n"
            "    3. Reduce OOS window       : --oos-months 3\n"
            "    4. Short-range preset      : --is-months 12 --oos-months 3 --roll-months 3\n"
        )

    logger.info(
        f"Walk-forward windows: {len(windows)} "
        f"(IS={adj_is}m, OOS={adj_oos}m, roll={adj_roll}m) | "
        f"n_workers={n_workers}"
    )

    window_results = []

    for w in windows:
        wid = w["window_id"]
        logger.info(
            f"\n{'='*70}\n"
            f"WINDOW {wid}/{len(windows)}\n"
            f"  IS : {w['is_start'].date()} → {w['is_end'].date()}\n"
            f"  OOS: {w['oos_start'].date()} → {w['oos_end'].date()}\n"
            f"{'='*70}"
        )

        t_win = datetime.now()

        # --- IS optimization (OPT-1, 2, 3, 5) ---
        best_params, is_sharpe, all_combos, best_ind_data = optimize_in_sample(
            price_data     = price_data,
            param_combos   = param_combos,
            is_start       = w["is_start"],
            is_end         = w["is_end"],
            initial_equity = initial_equity,
            vix_data       = vix_data,
            metadata       = metadata,
            window_id      = wid,
            logger         = logger,
            n_workers      = n_workers,
        )

        # --- Immediate grid-edge check on IS best params ---
        _edge_params = []
        for _p, _pvals in PARAM_GRID.items():
            _v = (best_params or {}).get(_p)
            if _v in (_pvals[0], _pvals[-1]):
                _edge_params.append(f"{_p}={_v} ({'min' if _v == _pvals[0] else 'max'})")
        if _edge_params:
            logger.warning(
                f"  Window {wid} GRID-EDGE WARNING: IS best params at grid boundary — "
                f"{', '.join(_edge_params)}. "
                f"True optimum may lie outside current search space. "
                f"Consider expanding grid or this window's result may be unreliable."
            )

        # --- OOS validation (OPT-4: pass cached indicator data) ---
        oos_sharpe, oos_metrics = validate_out_of_sample(
            price_data      = price_data,
            best_params     = best_params,
            oos_start       = w["oos_start"],
            oos_end         = w["oos_end"],
            initial_equity  = initial_equity,
            vix_data        = vix_data,
            metadata        = metadata,
            window_id       = wid,
            logger          = logger,
            cached_ind_data = best_ind_data,  # OPT-4
        )

        # --- Per-window stability ---
        # Stability = OOS/IS only meaningful when IS > 0 (positive IS Sharpe).
        # When IS <= 0 the optimizer found no edge in-sample; the ratio is
        # undefined or misleading and must NOT be hard-coded to -99 (which
        # contaminates the aggregate average and discards good OOS results).
        #   is_sharpe > 0.05 : normal — compute ratio
        #   is_sharpe <= 0.05: inverted/near-zero IS — store None, exclude from aggregate
        _IS_MEANINGFUL_THRESHOLD = 0.05
        if is_sharpe > _IS_MEANINGFUL_THRESHOLD:
            window_stability      = oos_sharpe / is_sharpe
            window_stability_flag = "normal"
        else:
            window_stability      = None   # excluded from aggregate ratio
            window_stability_flag = "inverted_or_zero_is"
        win_elapsed = (datetime.now() - t_win).total_seconds()

        w_result = {
            "window_id":             wid,
            "is_start":              w["is_start"].strftime("%Y-%m-%d"),
            "is_end":                w["is_end"].strftime("%Y-%m-%d"),
            "oos_start":             w["oos_start"].strftime("%Y-%m-%d"),
            "oos_end":               w["oos_end"].strftime("%Y-%m-%d"),
            "is_best_sharpe":        round(is_sharpe, 4),
            "oos_sharpe":            round(oos_sharpe, 4),
            "window_stability":      round(window_stability, 4) if window_stability is not None else None,
            "window_stability_flag": window_stability_flag,
            "best_params":           {k: best_params[k] for k in PARAM_GRID if k in best_params},
            "oos_metrics":           oos_metrics,
            "n_combos_tested":       len(all_combos),
            "top10_is_combos":       all_combos[:10],
            "elapsed_seconds":       round(win_elapsed, 1),
        }

        _stab_display = (
            f"{window_stability:.4f}" if window_stability is not None
            else f"N/A (IS={'negative' if is_sharpe <= 0 else 'near-zero'})"
        )
        logger.info(
            f"  Window {wid} SUMMARY: "
            f"IS Sharpe={is_sharpe:.4f} | OOS Sharpe={oos_sharpe:.4f} | "
            f"Stability={_stab_display} | "
            f"elapsed={win_elapsed:.1f}s"
        )

        window_results.append(w_result)

    return window_results


# ===========================================================================
# OUTPUT FORMATTING AND SAVING
# ===========================================================================

def _format_params(params: Optional[Dict]) -> str:
    if params is None:
        return "N/A"
    keys = list(PARAM_GRID.keys())
    return " | ".join(f"{k}={params.get(k, '?')}" for k in keys)


def build_window_summary_df(windows: List[Dict]) -> pd.DataFrame:
    rows = []
    for w in windows:
        bp  = w.get("best_params", {})
        row = {
            "window_id":             w["window_id"],
            "is_start":              w["is_start"],
            "is_end":                w["is_end"],
            "oos_start":             w["oos_start"],
            "oos_end":               w["oos_end"],
            "is_best_sharpe":        w["is_best_sharpe"],
            "oos_sharpe":            w["oos_sharpe"],
            "oos_cagr":              w["oos_metrics"].get("oos_cagr"),
            "oos_max_dd":            w["oos_metrics"].get("oos_max_dd"),
            "oos_trades":            w["oos_metrics"].get("oos_trades"),
            "window_stability":      w["window_stability"],  # None for inverted IS windows
            "window_stability_flag": w.get("window_stability_flag", "normal"),
            "elapsed_seconds":       w.get("elapsed_seconds"),
        }
        for k in PARAM_GRID:
            row[f"param_{k}"] = bp.get(k)
        rows.append(row)
    return pd.DataFrame(rows)


def build_param_stability_df(stability: Dict) -> pd.DataFrame:
    rows = []
    for param, info in stability["param_cv"].items():
        edge_info = stability["param_edges"].get(param, {})
        rows.append({
            "parameter":        param,
            "mean":             info.get("mean"),
            "std":              info.get("std"),
            "cv":               info.get("cv"),
            "cv_grade":         info.get("grade"),
            "pct_at_grid_edge": edge_info.get("pct_at_edge"),
            "edge_hits":        edge_info.get("edge_hits"),
            "total_windows":    edge_info.get("total_windows"),
            "note":             "single window — CV not meaningful" if info.get("std") is None else "",
        })
    return pd.DataFrame(rows)


def save_results(
    windows:      List[Dict],
    stability:    Dict,
    final_params: Dict,
    run_meta:     Dict,
    tag:          str,
    logger:       logging.Logger,
) -> None:
    WFO_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    sfx = f"_{tag}" if tag else ""
    ts  = datetime.now().strftime("%Y%m%d_%H%M%S")

    full_results = {
        "meta":         run_meta,
        "stability":    stability,
        "final_params": final_params,
        "windows":      windows,
        "generated_at": datetime.now().isoformat(),
    }

    json_path = WFO_DIR / f"wfo_results{sfx}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(full_results, f, indent=2, default=str)
    logger.info(f"Full results         -> {json_path}")

    df_windows = build_window_summary_df(windows)
    csv_win    = WFO_DIR / f"wfo_window_summary{sfx}.csv"
    df_windows.to_csv(csv_win, index=False)
    logger.info(f"Window summary       -> {csv_win}")

    df_params = build_param_stability_df(stability)
    csv_param = WFO_DIR / f"wfo_parameter_stability{sfx}.csv"
    df_params.to_csv(csv_param, index=False)
    logger.info(f"Parameter stability  -> {csv_param}")

    opt_path = WFO_DIR / f"wfo_optimal_params{sfx}.json"
    with open(opt_path, "w", encoding="utf-8") as f:
        json.dump(final_params, f, indent=2, default=str)
    logger.info(f"Optimal params       -> {opt_path}")

    report_path = REPORTS_DIR / f"{ts}_wfo_report{sfx}.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(full_results, f, indent=2, default=str)
    logger.info(f"Report archive       -> {report_path}")


def print_summary(
    windows:      List[Dict],
    stability:    Dict,
    final_params: Dict,
    logger:       logging.Logger,
) -> None:
    sep  = "=" * 70
    sep2 = "-" * 70

    lines = [
        "",
        sep,
        "WALK-FORWARD OPTIMIZER  —  RESULTS SUMMARY",
        sep,
        "",
        f"  Windows completed      : {stability['n_windows']} "
            f"({stability['n_normal_windows']} normal IS, "
            f"{stability['n_inverted_windows']} inverted/zero IS)",
        f"  Avg IS  Sharpe         : {stability['avg_is_sharpe']:.4f}  "
            f"(normal windows only: {stability['avg_is_sharpe_normal']:.4f})",
        f"  Avg OOS Sharpe         : {stability['avg_oos_sharpe']:.4f}  "
            f"(normal windows only: {stability['avg_oos_sharpe_normal']:.4f})",
        f"  Stability Ratio        : {stability['stability_ratio']:.4f}  [{stability['stability_grade']}]"
            f"  (computed on {stability['n_normal_windows']} normal-IS windows only)",
        f"  OOS Consistency        : {stability['oos_consistency']:.1%}  "
            f"({stability['oos_positive_windows']}/{stability['n_windows']} windows profitable)"
            f"  [{stability['oos_consistency_grade']}]",
        f"  OOS Sharpe Trend Slope : {stability['oos_sharpe_trend_slope']:+.4f}",
        "",
        sep2,
        "WINDOW-BY-WINDOW RESULTS",
        sep2,
        f"  {'WID':>3}  {'IS Period':<23}  {'OOS Period':<23}  "
            f"{'IS Sharpe':>9}  {'OOS Sharpe':>10}  {'Stability':>12}  {'Elapsed':>8}",
    ]
    for w in windows:
        stab = w["window_stability"]
        stab_str = f"{stab:>9.4f}" if stab is not None else f"{'N/A(IS≤0)':>9}"
        flag = " *" if w.get("window_stability_flag") == "inverted_or_zero_is" else "  "
        lines.append(
            f"  {w['window_id']:>3}  "
            f"{w['is_start']} → {w['is_end']}  "
            f"{w['oos_start']} → {w['oos_end']}  "
            f"{w['is_best_sharpe']:>9.4f}  "
            f"{w['oos_sharpe']:>10.4f}  "
            f"{stab_str}  "
            f"{w.get('elapsed_seconds', 0):>7.1f}s{flag}"
        )
    if any(w.get("window_stability_flag") == "inverted_or_zero_is" for w in windows):
        lines.append("  * Window excluded from Stability Ratio (IS Sharpe ≤ 0.05)")

    lines += [
        "",
        sep2,
        "PARAMETER STABILITY (CV = Coefficient of Variation)",
        sep2,
    ]
    for p, info in stability["param_cv"].items():
        if info["mean"] is not None and info["std"] is not None:
            vals_str = f"mean={info['mean']:>7.3f}  std={info['std']:>6.3f}"
        elif info["mean"] is not None:
            vals_str = f"mean={info['mean']:>7.3f}  std=N/A (1 window)"
        else:
            vals_str = "insufficient data"
        lines.append(
            f"  {p:<22}  CV={info['cv']:.3f}  "
            f"[{info['grade']:<10}]  {vals_str}"
        )

    lines += [
        "",
        sep2,
        "RECOMMENDED PARAMETERS",
        sep2,
    ]
    for k, v in (final_params.get("recommended") or {}).items():
        lines.append(f"  {k:<24}: {v}")
    lines.append(
        f"\n  Median OOS Sharpe: {final_params.get('median_oos_sharpe', 'N/A'):.4f}"
    )

    if stability["red_flags"]:
        lines += [
            "",
            sep2,
            f"RED FLAGS  ({len(stability['red_flags'])} detected)",
            sep2,
        ]
        for rf in stability["red_flags"]:
            lines.append(f"  [!] {rf}")

    # Deployment recommendation
    sr = stability["stability_ratio"]
    oc = stability["oos_consistency"]
    if sr >= STABILITY_EXCELLENT and oc >= OOS_CONSISTENCY_PASS:
        deploy_rec = "DEPLOY  — Parameters robust; proceed with production deployment"
    elif sr >= STABILITY_GOOD and oc >= OOS_CONSISTENCY_PASS:
        deploy_rec = "DEPLOY WITH MONITORING  — Good stability; enhanced monitoring advised"
    elif sr >= STABILITY_ACCEPTABLE and oc >= OOS_CONSISTENCY_WARN:
        deploy_rec = "PAPER TRADE FIRST  — Marginal stability; validate 3-6 months in paper mode"
    else:
        deploy_rec = "REJECT / RE-OPTIMIZE  — Poor stability; risk of overfitting detected"

    lines += [
        "",
        sep,
        f"DEPLOYMENT RECOMMENDATION: {deploy_rec}",
        sep,
        "",
    ]

    output = "\n".join(lines)
    print(output)
    for line in lines:
        logger.info(line) if line.strip() else None


# ===========================================================================
# ARGUMENT PARSER
# ===========================================================================

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Script 17: Walk-Forward Optimizer v3.7 — Multi-Asset Trend Following",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Performance notes (v3.7):
  Indicator caching:  2,880 combos → 12 indicator buckets (~240× fewer precompute calls)
  Parallel workers:   --n-workers N distributes buckets across N processes
                      Maximum effective parallelism = 12 (one per indicator bucket)
                      Recommended: --n-workers 12 (or nproc if fewer cores available)

Examples:
  # Standard serial run (OPT-1/3/4/5 active, OPT-2 off)
  python scripts/17_walk_forward_optimizer.py --start-date 2019-01-01 --end-date 2024-12-31

  # Parallel run (all optimisations active)
  python scripts/17_walk_forward_optimizer.py --start-date 2019-01-01 --end-date 2024-12-31 --n-workers 8

  # Fast mode (~64 combos, 4 indicator buckets)
  python scripts/17_walk_forward_optimizer.py --start-date 2019-01-01 --end-date 2024-12-31 --fast-mode

  # Quarterly re-optimization
  python scripts/17_walk_forward_optimizer.py --start-date 2022-01-01 --end-date 2024-12-31 --output-tag quarterly_Q4
        """
    )
    p.add_argument("--start-date",      default="2019-01-01",
                   help="Backtest dataset start date (YYYY-MM-DD)")
    p.add_argument("--end-date",        default="2024-12-31",
                   help="Backtest dataset end date   (YYYY-MM-DD)")
    p.add_argument("--initial-equity",  type=float, default=50_000.0,
                   help="Starting portfolio equity (default 50,000)")
    p.add_argument("--is-months",       type=int,   default=WFO_IS_MONTHS,
                   help=f"In-sample window length in months (default {WFO_IS_MONTHS})")
    p.add_argument("--oos-months",      type=int,   default=WFO_OOS_MONTHS,
                   help=f"Out-of-sample window length in months (default {WFO_OOS_MONTHS})")
    p.add_argument("--roll-months",     type=int,   default=WFO_ROLL_MONTHS,
                   help=f"Roll-forward step in months (default {WFO_ROLL_MONTHS})")
    p.add_argument("--fast-mode",       action="store_true",
                   help="Use reduced parameter grid (~64 combos, 4 indicator buckets)")
    p.add_argument("--n-workers",       type=int,   default=1,
                   help="Parallel workers (default 1 = serial). Max effective = 12 indicator buckets.")
    p.add_argument("--output-tag",      default="",
                   help="Tag appended to output filenames (e.g. 'quarterly_Q4')")
    p.add_argument("--verbose",         action="store_true",
                   help="Enable DEBUG logging")
    return p


# ===========================================================================
# ENTRY POINT
# ===========================================================================

def main() -> None:
    # Required on Windows/macOS (spawn start method) to prevent recursive spawning
    multiprocessing.freeze_support()

    parser = build_arg_parser()
    args   = parser.parse_args()

    logger = setup_logging(args.output_tag)
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    data_start = pd.Timestamp(args.start_date)
    data_end   = pd.Timestamp(args.end_date)

    # -----------------------------------------------------------------------
    # Select parameter grid and report optimisation configuration
    # -----------------------------------------------------------------------
    grid         = PARAM_GRID_FAST if args.fast_mode else PARAM_GRID
    param_combos = build_param_combinations(grid)
    n_ind_buckets = len(_group_combos_by_indicator(param_combos))

    logger.info(
        f"Parameter grid      : {'FAST' if args.fast_mode else 'FULL'} | "
        f"{len(param_combos)} combinations | "
        f"{n_ind_buckets} indicator buckets"
    )
    logger.info(
        f"Indicator caching   : {len(param_combos)} → {n_ind_buckets} precompute calls "
        f"({len(param_combos) // n_ind_buckets} combos/bucket) [OPT-1]"
    )
    logger.info(
        f"Parallel workers    : {args.n_workers} "
        f"{'[OPT-2 active]' if args.n_workers > 1 else '[serial]'}"
    )
    if args.n_workers > n_ind_buckets:
        logger.info(
            f"  Note: --n-workers {args.n_workers} > {n_ind_buckets} buckets; "
            f"effective parallelism capped at {n_ind_buckets}"
        )

    # -----------------------------------------------------------------------
    # Load data (once, shared across all windows)
    # -----------------------------------------------------------------------
    logger.info("Loading qualified universe ...")
    metadata = load_qualified_universe()
    if not metadata:
        logger.error("No symbols found — check data_cache/qualified/qualified_symbols.json")
        sys.exit(1)
    logger.info(f"Universe: {len(metadata)} symbols")

    logger.info("Loading price data ...")
    warm_up = pd.DateOffset(days=max(PARAM_GRID["sma_slow"]) * 2)  # 350 * 2 = 700 calendar days
    price_data: Dict[str, pd.DataFrame] = {}
    skipped = 0
    for sym in metadata:
        df = load_price_data(sym)
        if df is None or df.empty:
            skipped += 1
            continue
        df_full = df[df.index >= (data_start - warm_up)]
        if df_full.empty:
            skipped += 1
            continue
        price_data[sym] = df_full
    logger.info(f"Loaded {len(price_data)} symbols ({skipped} skipped)")

    if not price_data:
        logger.error("No price data loaded — check data_cache/consolidated/")
        sys.exit(1)

    vix_data = load_vix_data()
    logger.info(
        "VIX loaded for circuit breakers"
        if vix_data is not None
        else "VIX unavailable — circuit breaker CB2 inactive"
    )

    # -----------------------------------------------------------------------
    # Pre-flight: warn if data range is shorter than recommended
    # -----------------------------------------------------------------------
    available_months = _total_months(data_start, data_end)
    required_months  = args.is_months + args.oos_months
    max_sma_slow     = max(PARAM_GRID.get("sma_slow", [200]) if not args.fast_mode
                          else PARAM_GRID_FAST.get("sma_slow", [200]))
    min_is_trading_days     = max_sma_slow + 20
    min_is_months_for_warmup = int(np.ceil(min_is_trading_days / 21))

    if available_months < required_months:
        adj_is, adj_oos, adj_roll, _ = adapt_windows_to_data(
            data_start, data_end,
            args.is_months, args.oos_months, args.roll_months,
        )
        logger.warning(
            f"\n"
            f"  *** SHORT DATA RANGE DETECTED ***\n"
            f"  Requested IS={args.is_months}m + OOS={args.oos_months}m = {required_months}m, "
            f"but data spans only {available_months} months "
            f"({data_start.date()} to {data_end.date()}).\n"
            f"  Auto-adjusting: IS={adj_is}m | OOS={adj_oos}m | roll={adj_roll}m\n"
            f"  NOTE: With {available_months}m of data you will get "
            f"{max(0, (available_months - adj_is) // adj_roll)} walk-forward window(s).\n"
            f"  For full 6-window analysis use at least "
            f"{args.is_months + args.oos_months + (5 * args.roll_months)}m of data "
            f"(e.g. --start-date "
            f"{(data_end - pd.DateOffset(months=args.is_months + args.oos_months + 5*args.roll_months)).strftime('%Y-%m-%d')}).\n"
        )
        if adj_is < min_is_months_for_warmup:
            logger.warning(
                f"  *** SMA WARMUP WARNING ***\n"
                f"  Adapted IS={adj_is}m may be too short for indicator warmup.\n"
                f"  SMA_{max_sma_slow} needs ~{min_is_months_for_warmup}m of IS bars — "
                f"OPT-5 early-exit will trigger and skip remaining buckets.\n"
                f"  Recommended fixes:\n"
                f"    a) Use fast mode: --fast-mode\n"
                f"    b) Extend data range: --start-date "
                f"{(data_start - pd.DateOffset(months=6)).strftime('%Y-%m-%d')}\n"
                f"    c) Reduce windows: --is-months {min_is_months_for_warmup} "
                f"--oos-months 3 --roll-months 3\n"
            )

    # -----------------------------------------------------------------------
    # Walk-forward optimization
    # -----------------------------------------------------------------------
    t0      = datetime.now()
    windows = run_walk_forward(
        price_data     = price_data,
        metadata       = metadata,
        vix_data       = vix_data,
        data_start     = data_start,
        data_end       = data_end,
        initial_equity = args.initial_equity,
        is_months      = args.is_months,
        oos_months     = args.oos_months,
        roll_months    = args.roll_months,
        param_combos   = param_combos,
        logger         = logger,
        n_workers      = args.n_workers,
    )
    elapsed = (datetime.now() - t0).total_seconds()
    logger.info(f"Walk-forward completed in {elapsed:.1f}s")

    # -----------------------------------------------------------------------
    # Stability analysis + final parameter selection
    # -----------------------------------------------------------------------
    stability    = compute_stability_metrics(windows)
    final_params = select_final_parameters(windows, stability)

    # -----------------------------------------------------------------------
    # Run metadata
    # -----------------------------------------------------------------------
    _adj_is, _adj_oos, _adj_roll, _ = adapt_windows_to_data(
        data_start, data_end, args.is_months, args.oos_months, args.roll_months
    )

    run_meta = {
        "script":                "17_walk_forward_optimizer.py",
        "architecture":          "v3.7",
        "start_date":            args.start_date,
        "end_date":              args.end_date,
        "available_months":      _total_months(data_start, data_end),
        "initial_equity":        args.initial_equity,
        "is_months_requested":   args.is_months,
        "oos_months_requested":  args.oos_months,
        "roll_months_requested": args.roll_months,
        "is_months_actual":      _adj_is,
        "oos_months_actual":     _adj_oos,
        "roll_months_actual":    _adj_roll,
        "n_windows":             len(windows),
        "grid_mode":             "FAST" if args.fast_mode else "FULL",
        "n_combinations":        len(param_combos),
        "n_indicator_buckets":   n_ind_buckets,
        "n_workers":             args.n_workers,
        "elapsed_seconds":       round(elapsed, 1),
        "output_tag":            args.output_tag,
        "param_grid":            grid,
        "fixed_params":          FIXED_PARAMS,
        "optimisations":         ["OPT-1 indicator_cache", "OPT-3 price_preslice",
                                  "OPT-4 oos_ind_reuse",  "OPT-5 early_exit"]
                                 + (["OPT-2 parallel"] if args.n_workers > 1 else []),
    }

    # -----------------------------------------------------------------------
    # Print & save
    # -----------------------------------------------------------------------
    print_summary(windows, stability, final_params, logger)
    save_results(
        windows      = windows,
        stability    = stability,
        final_params = final_params,
        run_meta     = run_meta,
        tag          = args.output_tag,
        logger       = logger,
    )

    logger.info("Script 17 finished.")


# ===========================================================================
# PUBLIC API  (imported by Scripts 18, 19, 20, 21)
# ===========================================================================

def run_walk_forward_optimization(
    price_data:     Dict[str, pd.DataFrame],
    data_start:     pd.Timestamp,
    data_end:       pd.Timestamp,
    initial_equity: float      = 50_000.0,
    is_months:      int        = WFO_IS_MONTHS,
    oos_months:     int        = WFO_OOS_MONTHS,
    roll_months:    int        = WFO_ROLL_MONTHS,
    fast_mode:      bool       = False,
    metadata:       Optional[Dict]      = None,
    vix_data:       Optional[pd.Series] = None,
    log_level:      int        = logging.WARNING,
    n_workers:      int        = 1,
) -> Dict:
    """
    Programmatic entry point for Scripts 19/20/21.

    Parameters
    ----------
    price_data     : {symbol -> OHLCV DataFrame}
    data_start     : start of the full dataset window
    data_end       : end   of the full dataset window
    initial_equity : starting portfolio equity
    is_months      : in-sample window length (months)
    oos_months     : out-of-sample window length (months)
    roll_months    : roll-forward step (months)
    fast_mode      : use reduced grid for testing
    metadata       : optional universe metadata
    vix_data       : optional VIX Series for circuit breakers
    log_level      : logging level (suppress with logging.WARNING)
    n_workers      : parallel workers for IS grid search (default 1 = serial)

    Returns
    -------
    dict with keys:
        windows      : list of per-window results
        stability    : aggregate stability metrics
        final_params : recommended parameter set
    """
    _log = logging.getLogger("wfo.api")
    _log.setLevel(log_level)
    if not _log.handlers:
        _log.addHandler(logging.NullHandler())

    grid         = PARAM_GRID_FAST if fast_mode else PARAM_GRID
    param_combos = build_param_combinations(grid)

    windows = run_walk_forward(
        price_data     = price_data,
        metadata       = metadata or {},
        vix_data       = vix_data,
        data_start     = data_start,
        data_end       = data_end,
        initial_equity = initial_equity,
        is_months      = is_months,
        oos_months     = oos_months,
        roll_months    = roll_months,
        param_combos   = param_combos,
        logger         = _log,
        n_workers      = n_workers,
    )

    stability    = compute_stability_metrics(windows)
    final_params = select_final_parameters(windows, stability)

    return {
        "windows":      windows,
        "stability":    stability,
        "final_params": final_params,
    }


if __name__ == "__main__":
    main()
