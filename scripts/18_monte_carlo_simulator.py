#!/usr/bin/env python3
"""
Script 18: Monte Carlo Simulator
==================================
Assess forward-looking risk and outcome distributions of the trend-following
strategy via three complementary Monte Carlo methods, consuming the outputs of
Script 16 (Backtest Engine).

Architecture Reference: v3.2 (Feb 2026)

Monte Carlo Methodology
-----------------------
Three independent simulation methods are applied; all three must pass before
the strategy is approved for live deployment.

  METHOD 1 — Trade Shuffling  (primary)
    Randomly resample (with replacement) the empirical round-trip trade P&L
    sequence produced by Script 16. Preserves the true per-trade return
    distribution; destroys autocorrelation and clustering effects.

    N_paths  : 10 000
    N_trades : len(backtest_trade_log)  (same count, resampled)

  METHOD 2 — Block Bootstrap  (secondary)
    Resample overlapping blocks of daily portfolio returns (length L) with
    replacement to construct synthetic equity curves of the same length.
    Preserves short-term autocorrelation and volatility clustering in
    daily returns while still generating diverse paths.

    N_paths      : 10 000
    Block length : 21 trading days (≈ 1 month)

  METHOD 3 — Parametric  (stress-test)
    Fit a skewed-t (or normal as fallback) distribution to daily returns.
    Draw i.i.d. samples to build synthetic equity curves.  Deliberately
    ignores any mean-reversion or autocorrelation — conservative stress test.

    N_paths : 10 000

Metrics Computed Per Path (all three methods)
---------------------------------------------
  • Terminal equity  (absolute $)
  • CAGR             (annualised compound growth rate)
  • Sharpe ratio     (annualised, risk-free = 0.05)
  • Sortino ratio    (annualised, downside std only)
  • Max drawdown     (peak-to-trough %)
  • Calmar ratio     (CAGR / |Max DD|)
  • Time in drawdown (% of days below HWM)

Summary Statistics Reported
----------------------------
  Percentile table: 1st, 5th, 10th, 25th, 50th, 75th, 90th, 95th, 99th
  Ruin probability : Pr(terminal equity < 50% of initial) — "soft ruin"
  Blow-up probability: Pr(max drawdown > 40%)
  Confidence interval: 5th–95th percentile band for every metric

Pass / Fail Gates (any failure → CAUTION flag)
-----------------------------------------------
  MC-1 : Median CAGR (Method 1)   > 0%
  MC-2 : P5  terminal equity      > 50% of initial (soft-ruin < 5%)
  MC-3 : P95 max drawdown         < 50%
  MC-4 : Median Sharpe (Method 2) > 0.30
  MC-5 : Soft-ruin probability (M1)      < 5%
  MC-6 : Soft-ruin probability (M3)      < 10%  ← parametric stress-test gate

Inputs
------
  data_cache/backtest/equity_curve.csv      — daily portfolio equity series
  data_cache/backtest/trade_log.csv         — round-trip trade P&L log
  data_cache/backtest/performance_metrics.json — baseline metrics

Outputs
-------
  data_cache/monte_carlo/
    mc_summary_{tag}.json           — full simulation summary + gates
    mc_paths_method1_{tag}.csv      — terminal equity for each path (Method 1)
    mc_paths_method2_{tag}.csv      — terminal equity for each path (Method 2)
    mc_paths_method3_{tag}.csv      — terminal equity for each path (Method 3)
    mc_percentile_table_{tag}.csv   — percentile table for all metrics
    mc_equity_fan_{tag}.csv         — fan-chart data (selected percentile paths)
  reports/monte_carlo/
    {YYYYMMDD}_mc_report_{tag}.json — archived timestamped report
  logs/monte_carlo_{timestamp}.log

Execution
---------
  # Standard run (reads Script 16 outputs automatically)
  python scripts/18_monte_carlo_simulator.py

  # Custom simulation parameters
  python scripts/18_monte_carlo_simulator.py \\
      --n-paths 20000 \\
      --block-length 63 \\
      --risk-free-rate 0.05 \\
      --output-tag stress_2024

  # Use a specific backtest tag (if Script 16 was run with --output-tag)
  python scripts/18_monte_carlo_simulator.py \\
      --backtest-tag run_01 \\
      --output-tag mc_run_01

  # Fast mode (2 000 paths — CI reporting only, skip fan chart)
  python scripts/18_monte_carlo_simulator.py --fast-mode

Architecture: v3.2 (Feb 2026) — Multi-Asset Trend Following Strategy
"""

import os
import sys
import json
import logging
import argparse
import warnings
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=RuntimeWarning)

# ============================================================================
# PROJECT PATHS
# ============================================================================

PROJECT_ROOT    = Path(__file__).parent.parent
DATA_CACHE_DIR  = PROJECT_ROOT / "data_cache"
BACKTEST_DIR    = DATA_CACHE_DIR / "backtest"
MC_DIR          = DATA_CACHE_DIR / "monte_carlo"
REPORTS_DIR     = PROJECT_ROOT / "reports" / "monte_carlo"
LOG_DIR         = PROJECT_ROOT / "logs"

# ============================================================================
# DEFAULTS
# ============================================================================

DEFAULTS = dict(
    n_paths         = 10_000,
    block_length    = 21,          # trading days per block (Method 2)
    risk_free_rate  = 0.05,        # annual, for Sharpe/Sortino
    trading_days    = 252,         # assumed per year
    soft_ruin_level = 0.50,        # terminal equity < 50% initial → ruin
    hard_dd_limit   = 0.40,        # max DD > 40% → blow-up
    seed            = 42,
)

# Pass/fail thresholds
GATES = dict(
    mc1_median_cagr_min       =  0.00,
    mc2_p5_equity_pct_min     =  0.50,   # 50% of initial
    mc3_p95_maxdd_max         =  0.50,   # 50% max drawdown
    mc4_median_sharpe_min     =  0.30,
    mc5_ruin_prob_max         =  0.05,   # 5% ruin probability (Method 1)
    mc6_m3_ruin_prob_max      =  0.10,   # 10% ruin probability (Method 3 stress test)
)

# Percentiles to report
REPORT_PERCENTILES = [1, 5, 10, 25, 50, 75, 90, 95, 99]

# ============================================================================
# LOGGING
# ============================================================================

def setup_logging(tag: str = "") -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
    sfx = f"_{tag}" if tag else ""
    log_file = LOG_DIR / f"monte_carlo{sfx}_{ts}.log"
    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    logger = logging.getLogger(__name__)
    logger.info(f"Monte Carlo simulator started | log -> {log_file}")
    return logger


logger = logging.getLogger(__name__)

# ============================================================================
# DATA LOADERS
# ============================================================================

def load_equity_curve(tag: str = "") -> pd.DataFrame:
    """Load the daily equity curve produced by Script 16."""
    sfx  = f"_{tag}" if tag else ""
    path = BACKTEST_DIR / f"equity_curve{sfx}.csv"
    if not path.exists():
        # Fallback: try without tag suffix
        path = BACKTEST_DIR / "equity_curve.csv"
    if not path.exists():
        raise FileNotFoundError(
            f"Equity curve not found at {path}. "
            "Run Script 16 (backtest_engine.py) first."
        )
    df = pd.read_csv(path, parse_dates=["date"])
    df = df.sort_values("date").reset_index(drop=True)
    logger.info(f"Equity curve loaded: {len(df)} rows | {path}")
    return df


def load_trade_log(tag: str = "") -> pd.DataFrame:
    """Load the round-trip trade log produced by Script 16."""
    sfx  = f"_{tag}" if tag else ""
    path = BACKTEST_DIR / f"trade_log{sfx}.csv"
    if not path.exists():
        path = BACKTEST_DIR / "trade_log.csv"
    if not path.exists():
        raise FileNotFoundError(
            f"Trade log not found at {path}. "
            "Run Script 16 (backtest_engine.py) first."
        )
    df = pd.read_csv(path)
    logger.info(f"Trade log loaded: {len(df)} trades | {path}")
    return df


def load_performance_metrics(tag: str = "") -> Dict:
    """Load baseline performance metrics from Script 16."""
    sfx  = f"_{tag}" if tag else ""
    path = BACKTEST_DIR / f"performance_metrics{sfx}.json"
    if not path.exists():
        path = BACKTEST_DIR / "performance_metrics.json"
    if not path.exists():
        logger.warning("performance_metrics.json not found — baseline metrics unavailable")
        return {}
    with open(path) as f:
        metrics = json.load(f)
    logger.info(f"Baseline metrics loaded | {path}")
    return metrics


def extract_daily_returns(equity_df: pd.DataFrame) -> np.ndarray:
    """Convert equity levels to daily log returns."""
    eq = equity_df["equity"].values.astype(float)
    # Guard against zero / negative equity (shouldn't happen but be safe)
    eq = np.where(eq <= 0, np.nan, eq)
    returns = np.diff(np.log(eq))
    returns = returns[~np.isnan(returns)]
    return returns


def extract_trade_pnl_dollars(
    trade_df: pd.DataFrame,
    initial_equity: float,
) -> Tuple[np.ndarray, str]:
    """
    Extract per-trade dollar P&L for use in additive equity simulation.

    Method 1 (trade shuffling) must work at the PORTFOLIO level — i.e., each
    trade's dollar contribution to total equity — NOT the position-level
    percent return.  Using pnl_pct directly (position return ~5-15%) as if it
    were a portfolio return over-inflates compounded equity by 15-20x and
    produces astronomically wrong CAGRs when combined with the wrong time axis.

    Priority order
    --------------
    1. Direct dollar P&L column: net_pnl, pnl, profit, gross_pnl, net_profit,
       profit_loss, total_pnl
    2. Computed from pnl_pct × position_value columns (scaled to portfolio)
    3. Computed from pnl_pct × estimated position size (risk_per_trade × equity)
    4. pnl_pct treated as portfolio-level % (conservative: already fraction of equity)

    Returns
    -------
    (pnl_array, source_description)
    """
    cols = {c.lower(): c for c in trade_df.columns}

    # ── Priority 1: direct dollar P&L ────────────────────────────────────────
    dollar_candidates = [
        "net_pnl", "pnl", "profit", "gross_pnl", "net_profit",
        "profit_loss", "total_pnl", "dollar_pnl", "net_return_dollar",
    ]
    for dc in dollar_candidates:
        if dc in cols:
            vals = trade_df[cols[dc]].dropna().values.astype(float)
            vals = vals[np.isfinite(vals)]
            if len(vals) > 0:
                logger.info(
                    f"Trade P&L (dollar): column '{dc}' | "
                    f"{len(vals)} trades | "
                    f"mean=${vals.mean():+.2f}  total=${vals.sum():+.2f}"
                )
                return vals, f"dollar_pnl:{dc}"

    # ── Priority 2: pnl_pct × position_value → dollar P&L ───────────────────
    pct_cols = ["pnl_pct", "return_pct", "net_return", "trade_return"]
    val_cols = ["position_value", "trade_value", "entry_value",
                "cost_basis", "notional"]

    pct_col = next((p for p in pct_cols if p in cols), None)
    val_col = next((v for v in val_cols if v in cols), None)

    if pct_col and val_col:
        tmp = trade_df[[cols[pct_col], cols[val_col]]].dropna()
        pct_vals = tmp.iloc[:, 0].values.astype(float)
        pos_vals = tmp.iloc[:, 1].values.astype(float)
        # Normalise pct if in percent form
        if np.abs(pct_vals).max() > 1.5:
            pct_vals = pct_vals / 100.0
        pnl_dollars = pct_vals * pos_vals
        pnl_dollars = pnl_dollars[np.isfinite(pnl_dollars)]
        if len(pnl_dollars) > 0:
            logger.info(
                f"Trade P&L (pct×value): {pct_col}×{val_col} | "
                f"{len(pnl_dollars)} trades | mean=${pnl_dollars.mean():+.2f}"
            )
            return pnl_dollars, f"pct_times_value:{pct_col}x{val_col}"

    # ── Priority 3: pnl_pct × estimated position size ────────────────────────
    # Strategy uses 2% risk per trade, positions sized 0.5–8% of equity.
    # Median position ≈ 3% of equity is a conservative estimate.
    if pct_col:
        pct_vals = trade_df[cols[pct_col]].dropna().values.astype(float)
        pct_vals = pct_vals[np.isfinite(pct_vals)]
        if np.abs(pct_vals).max() > 1.5:
            pct_vals = pct_vals / 100.0
        # Scale by median position size fraction (3% of initial equity)
        est_pos_fraction = 0.03
        pnl_dollars = pct_vals * initial_equity * est_pos_fraction
        logger.warning(
            f"Trade P&L: no dollar column found — estimating from "
            f"'{pct_col}' × {est_pos_fraction:.0%} of equity = "
            f"${initial_equity * est_pos_fraction:,.0f} per trade. "
            "Provide a net_pnl column for more accurate simulation."
        )
        return pnl_dollars, f"estimated:{pct_col}×{est_pos_fraction:.0%}equity"

    # ── Fallback: empty array (Method 1 will be skipped) ─────────────────────
    logger.error(
        "No usable trade P&L column found. "
        "Expected: net_pnl, pnl, profit, OR pnl_pct with position_value."
    )
    return np.array([]), "unavailable"


def extract_trade_returns(trade_df: pd.DataFrame) -> np.ndarray:
    """
    Legacy helper — returns per-trade fractional return (not used by Method 1).
    Kept for backwards compatibility with external callers.
    """
    cols = [c.lower() for c in trade_df.columns]
    for sc in ["pnl_pct", "return_pct", "net_return", "trade_return"]:
        if sc in cols:
            vals = trade_df[sc].dropna().values.astype(float)
            vals = vals[np.isfinite(vals)]
            if len(vals) > 0:
                if np.abs(vals).max() > 1.5:
                    vals = vals / 100.0
                return vals
    return np.array([])


# ============================================================================
# METRICS COMPUTATION  (vectorised over N paths)
# ============================================================================

def compute_path_metrics(
    equity_matrix: np.ndarray,   # shape (N_paths, N_days+1), first col = initial equity
    initial_equity: float,
    trading_days: int,
    risk_free_rate: float,
    years_override: Optional[float] = None,
) -> Dict[str, np.ndarray]:
    """
    Compute per-path performance metrics for a matrix of equity curves.

    Parameters
    ----------
    equity_matrix   : (N_paths, T+1) array where equity_matrix[:, 0] = initial_equity
    initial_equity  : starting portfolio value
    trading_days    : trading days per year (252)
    risk_free_rate  : annualised risk-free rate
    years_override  : if provided, use this as the time horizon for CAGR/Sharpe/Sortino
                      instead of inferring from the number of equity columns.
                      Required when equity columns represent trades (not daily steps).

    Returns
    -------
    Dict of 1-D arrays of length N_paths
    """
    N, T = equity_matrix.shape
    T -= 1  # number of return periods

    # --- Daily log returns ---
    log_eq = np.log(np.maximum(equity_matrix, 1e-10))
    daily_ret = np.diff(log_eq, axis=1)  # (N, T)

    # --- Time horizon ---
    # If years_override is provided (e.g. for Method 1 where T = n_trades, not n_days),
    # use it directly.  Otherwise infer from step count.
    years     = years_override if years_override is not None else T / trading_days
    years     = max(years, 1e-6)

    # Annualisation factor and risk-free rate per step.
    #
    # Methods 2/3 operate on daily equity steps:
    #   ann_factor = sqrt(252),  rf_per_step = annual_rf / 252
    #
    # Method 1 operates on trade steps (each step = one round-trip trade).
    # Average days per trade = bt_years * 252 / n_trades.
    # steps_per_year = n_trades / bt_years  (e.g. 72 / 1.94 ≈ 37.1)
    # ann_factor = sqrt(steps_per_year)     (e.g. sqrt(37.1) ≈ 6.09)
    #
    # Using sqrt(252) for trade steps inflates Sharpe by sqrt(252/37.1) ≈ 2.6×.
    # The original code had an identical ternary on both branches — this is the fix.
    if years_override is not None:
        steps_per_year = T / years_override           # n_trades / bt_years ≈ 37.1
        ann_factor     = np.sqrt(steps_per_year)      # sqrt(37.1) ≈ 6.09
        daily_rf       = risk_free_rate / steps_per_year   # rf per trade-step
    else:
        ann_factor     = np.sqrt(trading_days)        # sqrt(252) for daily steps
        daily_rf       = risk_free_rate / trading_days

    # --- CAGR ---
    terminal  = equity_matrix[:, -1]
    cagr      = (terminal / initial_equity) ** (1.0 / years) - 1.0

    # --- Sharpe (annualised) ---
    mu        = daily_ret.mean(axis=1)
    sigma     = daily_ret.std(axis=1, ddof=1)
    sharpe    = np.where(
        sigma > 1e-9,
        (mu - daily_rf) / sigma * ann_factor,
        0.0,
    )

    # --- Sortino (annualised) ---
    excess = daily_ret - daily_rf
    neg    = np.where(excess < 0, excess, 0.0)
    downside_std = np.sqrt((neg ** 2).mean(axis=1))
    sortino = np.where(
        downside_std > 1e-9,
        (mu - daily_rf) / downside_std * ann_factor,
        0.0,
    )

    # --- Max Drawdown ---
    cum_max = np.maximum.accumulate(equity_matrix, axis=1)
    dd      = (equity_matrix - cum_max) / np.maximum(cum_max, 1e-10)
    max_dd  = dd.min(axis=1)  # most negative value = worst drawdown

    # --- Calmar ---
    calmar = np.where(
        np.abs(max_dd) > 1e-9,
        cagr / np.abs(max_dd),
        np.sign(cagr) * 10.0,   # cap at ±10 when drawdown ≈ 0
    )

    # --- Time in drawdown ---
    in_dd = (equity_matrix < cum_max).sum(axis=1) / T

    return {
        "terminal_equity": terminal,
        "cagr":            cagr,
        "sharpe":          sharpe,
        "sortino":         sortino,
        "max_drawdown":    max_dd,
        "calmar":          calmar,
        "time_in_drawdown": in_dd,
    }


# ============================================================================
# SIMULATION METHODS
# ============================================================================

def simulate_trade_shuffling(
    pnl_dollars: np.ndarray,
    initial_equity: float,
    n_paths: int,
    n_trades: int,
    rng: np.random.Generator,
    trading_days: int,
    risk_free_rate: float,
    bt_years: float,
    pnl_source: str = "",
) -> Dict:
    """
    Method 1 — Trade Shuffling  (additive dollar-P&L, correct time horizon).

    Randomly resample (with replacement) the empirical round-trip dollar P&L
    sequence and rebuild portfolio equity additively:

        E(t+1) = E(t) + shuffled_pnl(t)

    This is the standard textbook approach for trade-shuffling Monte Carlo.
    It works at the PORTFOLIO level and avoids the two critical bugs present
    when using position-level pnl_pct directly:

    Bug A — Wrong return type: position pnl_pct (~5-15%) ≠ portfolio contribution
             (~0.25-0.75%).  Raw multiplicative compounding overestimates by 15-20×.
    Bug B — Wrong time horizon: sequential compounding over n_trades steps then
             annualising as n_trades/252 years (0.286) instead of the actual
             backtest duration (n_bt_days/252 = 1.94) inflates CAGR exponent 6.8×.

    Both bugs combine multiplicatively → CAGR can be 100x too large.

    The fix: work in dollar terms (additive) and pass bt_years = n_bt_days/252
    explicitly to compute_path_metrics via years_override.
    """
    if len(pnl_dollars) == 0:
        logger.warning("Method 1: No trade P&L available — skipping")
        return {}

    actual_n_trades = len(pnl_dollars)
    n_draw = n_trades if n_trades > 0 else actual_n_trades

    logger.info(
        f"Method 1 (Trade Shuffle): {n_paths:,} paths × {n_draw} trades "
        f"| source: {pnl_source} | bt_years={bt_years:.2f}"
    )

    # Resample P&L: shape (n_paths, n_draw) — with replacement
    idx     = rng.integers(0, actual_n_trades, size=(n_paths, n_draw))
    sampled = pnl_dollars[idx]   # (n_paths, n_draw)

    # Build equity curves ADDITIVELY: equity at each trade step
    equity_matrix = np.empty((n_paths, n_draw + 1))
    equity_matrix[:, 0] = initial_equity
    # Vectorised cumulative sum: equity[t] = initial + cumsum(pnl)[t-1]
    equity_matrix[:, 1:] = initial_equity + np.cumsum(sampled, axis=1)

    # Floor equity at zero to prevent negative values on ruin paths
    equity_matrix = np.maximum(equity_matrix, 0.0)

    # Use actual backtest years for CAGR — NOT n_trades/252 (the old bug)
    metrics = compute_path_metrics(
        equity_matrix   = equity_matrix,
        initial_equity  = initial_equity,
        trading_days    = trading_days,
        risk_free_rate  = risk_free_rate,
        years_override  = bt_years,   # KEY FIX: use actual bt duration, not step count
    )

    return {
        "equity_matrix": equity_matrix,
        "metrics":       metrics,
        "method":        "trade_shuffling",
        "pnl_source":    pnl_source,
        "n_paths":       n_paths,
        "n_trades":      n_draw,
        "bt_years":      bt_years,
    }


def simulate_block_bootstrap(
    daily_returns: np.ndarray,
    initial_equity: float,
    n_paths: int,
    block_length: int,
    rng: np.random.Generator,
    trading_days: int,
    risk_free_rate: float,
) -> Dict:
    """
    Method 2 — Block Bootstrap.

    Draw overlapping blocks of length `block_length` from the empirical
    daily return series with replacement and concatenate until the desired
    path length is reached.  Preserves local autocorrelation structure
    (volatility clustering, run effects) while generating diverse paths.
    """
    T = len(daily_returns)
    if T < block_length * 2:
        logger.warning(
            f"Method 2: Return series too short ({T} days, block={block_length}) "
            "— falling back to i.i.d. bootstrap"
        )
        block_length = 1

    target_len = T   # same number of days as backtest

    logger.info(
        f"Method 2 (Block Bootstrap): {n_paths:,} paths × {target_len} days "
        f"| block={block_length}"
    )

    # Pre-compute number of blocks needed per path
    n_blocks_needed = int(np.ceil(target_len / block_length)) + 1

    # Valid starting positions (last block must not run off the end)
    max_start = T - block_length
    if max_start < 1:
        max_start = 1

    # Draw all block start indices at once: (n_paths, n_blocks_needed)
    starts = rng.integers(0, max_start, size=(n_paths, n_blocks_needed))

    # Build equity curves
    equity_matrix = np.empty((n_paths, target_len + 1))
    equity_matrix[:, 0] = initial_equity

    for i in range(n_paths):
        path_rets = []
        for j in range(n_blocks_needed):
            s = starts[i, j]
            block = daily_returns[s: s + block_length]
            path_rets.extend(block.tolist())
            if len(path_rets) >= target_len:
                break
        path_rets = np.array(path_rets[:target_len])
        # Build equity from log returns
        equity_matrix[i, 1:] = initial_equity * np.exp(np.cumsum(path_rets))

    metrics = compute_path_metrics(
        equity_matrix, initial_equity, trading_days, risk_free_rate
    )

    return {
        "equity_matrix": equity_matrix,
        "metrics":       metrics,
        "method":        "block_bootstrap",
        "n_paths":       n_paths,
        "block_length":  block_length,
        "n_days":        target_len,
    }


def simulate_parametric(
    daily_returns: np.ndarray,
    initial_equity: float,
    n_paths: int,
    rng: np.random.Generator,
    trading_days: int,
    risk_free_rate: float,
) -> Dict:
    """
    Method 3 — Parametric (Student-t or Gaussian fallback).

    Fit parameters to the empirical daily return distribution, then draw
    i.i.d. samples.  This method ignores all autocorrelation and is used
    as a conservative, distribution-based stress test.

    Two key stability fixes vs the original implementation
    -------------------------------------------------------
    Fix 1 — Minimum df = 4.0  (was 2.5):
        df < 4 means undefined kurtosis (power-law tail, infinite 4th moment).
        With df=2.5 and 10,000×489 draws, the resulting simulated returns can
        include extreme values that cause Sharpe/Sortino to collapse to 0 via
        the sigma=nan → np.where(..., 0.0) fallback in compute_path_metrics.
        df=4.0 ensures finite kurtosis and stable simulation while still
        capturing fat tails heavier than Gaussian.

    Fix 2 — Hard clip to ±10σ per draw:
        Even with df=4.0, extreme tail draws can dominate cumulative paths.
        Clipping each simulated log return to ±10× empirical daily std removes
        only 1-in-10M events that are not meaningful for 2-year horizon testing
        while preventing cumulative sum overflow and metrics collapse.
    """
    T   = len(daily_returns)
    mu  = daily_returns.mean()
    std = daily_returns.std(ddof=1)
    skew_val = _compute_skewness(daily_returns)
    kurt_val = _compute_excess_kurtosis(daily_returns)

    logger.info(
        f"Method 3 (Parametric): {n_paths:,} paths × {T} days | "
        f"μ={mu:.6f}  σ={std:.6f}  skew={skew_val:.3f}  kurt={kurt_val:.3f}"
    )

    # Max single-draw log return: ±10σ — removes only 1-in-10M events
    clip_bound = 10.0 * std

    # Attempt Student-t fit
    try:
        from scipy.stats import t as student_t
        df_est, loc_est, scale_est = student_t.fit(daily_returns)

        # Enforce minimum df = 4.0:
        #   df < 4  → undefined kurtosis, extreme tail events, metrics collapse
        #   df = 4  → finite kurtosis (=inf at boundary, very fat tails)
        #   df > 4  → finite kurtosis = 6/(df-4), stabilises at df ≈ 30 → Gaussian
        #
        # CRITICAL FIX: when MLE df < 4.0 we must re-fit loc and scale with df
        # fixed at 4.0.  Simply bumping df while keeping the original loc/scale
        # produces inconsistent parameters: the MLE location for a df<4 fit
        # down-weights outliers aggressively and often converges near zero, causing
        # every equity path to flat-line at initial equity and all CAGR/Sharpe
        # metrics to collapse to ~0 (displayed as "0.0%" in the report).
        orig_df_est = df_est
        if df_est < 4.0:
            df_est = 4.0
            _, loc_est, scale_est = student_t.fit(daily_returns, f0=4.0)
            logger.info(
                f"  MLE df={orig_df_est:.2f} < 4.0 — re-fitted with df fixed=4.0: "
                f"loc={loc_est:.6f}  scale={scale_est:.6f}"
            )
        else:
            df_est = max(df_est, 4.0)

        sim_rets = student_t.rvs(
            df=df_est, loc=loc_est, scale=scale_est,
            size=(n_paths, T),
            random_state=int(rng.integers(0, 2**31)),
        )
        # Clip extreme draws BEFORE cumsum to prevent metrics collapse
        sim_rets = np.clip(sim_rets, -clip_bound, clip_bound)
        dist_name = f"Student-t (df={df_est:.1f})"

    except ImportError:
        # Gaussian fallback — inherently stable
        sim_rets = rng.normal(loc=mu, scale=std, size=(n_paths, T))
        sim_rets = np.clip(sim_rets, -clip_bound, clip_bound)
        dist_name = "Gaussian"

    logger.info(f"  Distribution fitted: {dist_name} | clip=±{clip_bound:.4f}")

    # ── Post-simulation degeneracy guard ─────────────────────────────────────
    # Validate that the simulated paths are non-trivial even after the re-fit.
    # If the median cumulative log return deviates too far from the empirical
    # expectation (mu × T), the distribution parameters are still inconsistent.
    # Fallback: plain Gaussian using empirical mu/sigma — always realistic.
    _expected_cum = mu * T                               # expected drift over T steps
    _med_cum      = float(np.nanmedian(sim_rets.sum(axis=1)))
    _tol          = max(0.50 * abs(_expected_cum), 0.05) # 50% of expected or abs 0.05
    if not np.isfinite(_med_cum) or abs(_med_cum - _expected_cum) > _tol:
        logger.warning(
            f"Method 3: Simulated cumulative return degenerate "
            f"(median={_med_cum:.4f}, expected≈{_expected_cum:.4f}, tol=±{_tol:.4f}). "
            f"Distribution '{dist_name}' produced inconsistent location parameter. "
            "Falling back to Gaussian with empirical μ/σ."
        )
        sim_rets  = rng.normal(loc=mu, scale=std, size=(n_paths, T))
        sim_rets  = np.clip(sim_rets, -clip_bound, clip_bound)
        dist_name = f"Gaussian-fallback (empirical μ={mu:.6f} σ={std:.6f})"
        logger.info(f"  Fallback distribution: {dist_name}")

    # Build equity matrix from cumulative log returns
    init_col      = np.full((n_paths, 1), initial_equity)
    cum_rets      = np.exp(np.cumsum(sim_rets, axis=1))
    equity_matrix = np.hstack([init_col, initial_equity * cum_rets])

    metrics = compute_path_metrics(
        equity_matrix, initial_equity, trading_days, risk_free_rate
    )

    return {
        "equity_matrix": equity_matrix,
        "metrics":       metrics,
        "method":        "parametric",
        "distribution":  dist_name,
        "n_paths":       n_paths,
        "n_days":        T,
        "fit_mu":        float(mu),
        "fit_sigma":     float(std),
        "fit_skew":      float(skew_val),
        "fit_kurt":      float(kurt_val),
        "clip_bound":    float(clip_bound),
    }


# ============================================================================
# HELPER STATISTICS
# ============================================================================

def _compute_skewness(x: np.ndarray) -> float:
    m = x - x.mean()
    n = len(m)
    if n < 3:
        return 0.0
    s3 = (m ** 3).mean()
    s2 = (m ** 2).mean()
    return float(s3 / (s2 ** 1.5 + 1e-30))


def _compute_excess_kurtosis(x: np.ndarray) -> float:
    m = x - x.mean()
    n = len(m)
    if n < 4:
        return 0.0
    s4 = (m ** 4).mean()
    s2 = (m ** 2).mean()
    return float(s4 / (s2 ** 2 + 1e-30)) - 3.0


def percentile_table(
    metrics_dict: Dict[str, np.ndarray],
    percentiles: List[int],
) -> pd.DataFrame:
    """Build a percentile table from a metrics dictionary."""
    rows = {}
    for metric, values in metrics_dict.items():
        finite = values[np.isfinite(values)]
        if len(finite) == 0:
            rows[metric] = {f"p{p}": np.nan for p in percentiles}
            rows[metric]["mean"] = np.nan
            rows[metric]["std"]  = np.nan
        else:
            row = {f"p{p}": float(np.percentile(finite, p)) for p in percentiles}
            row["mean"] = float(finite.mean())
            row["std"]  = float(finite.std(ddof=1))
            rows[metric] = row
    df = pd.DataFrame(rows).T
    df.index.name = "metric"
    return df


def compute_ruin_probability(
    terminal_equity: np.ndarray,
    initial_equity: float,
    ruin_level: float,
) -> float:
    """Pr(terminal equity < ruin_level × initial_equity)."""
    threshold = initial_equity * ruin_level
    return float((terminal_equity < threshold).mean())


def compute_blowup_probability(
    max_drawdowns: np.ndarray,
    hard_dd_limit: float,
) -> float:
    """Pr(max_drawdown < -hard_dd_limit)."""
    return float((max_drawdowns < -hard_dd_limit).mean())


def fan_chart_data(
    equity_matrix: np.ndarray,
    n_days: int,
    percentiles: List[int],
    initial_equity: float,
    max_paths_stored: int = 50,
) -> pd.DataFrame:
    """
    Build fan chart data: percentile bands of equity curve over time.

    Returns DataFrame with columns: day, mean, and one column per percentile.
    Also stores a sample of individual paths for plotting.
    """
    T    = min(equity_matrix.shape[1], n_days + 1)
    days = np.arange(T)
    data = {"day": days}

    slice_eq = equity_matrix[:, :T]

    for p in percentiles:
        data[f"p{p}"] = np.percentile(slice_eq, p, axis=0)
    data["mean"] = slice_eq.mean(axis=0)

    # Store a random sample of paths for individual path plotting
    n_sample = min(max_paths_stored, equity_matrix.shape[0])
    sample_idx = np.random.choice(equity_matrix.shape[0], n_sample, replace=False)
    for k, i in enumerate(sample_idx):
        data[f"path_{k}"] = equity_matrix[i, :T]

    return pd.DataFrame(data)


# ============================================================================
# PASS / FAIL GATES
# ============================================================================

def evaluate_gates(
    method1_metrics: Dict,
    method2_metrics: Dict,
    method3_metrics: Dict,
    initial_equity: float,
    params: Dict,
) -> Dict:
    """
    Evaluate the six MC pass/fail gates.

    Returns a dict with gate results and an overall pass/fail verdict.
    """
    gates_def = {
        "MC-1": {
            "description": "Median CAGR (Method 1) > 0%",
            "threshold":   params["gates"]["mc1_median_cagr_min"],
        },
        "MC-2": {
            "description": "5th-percentile terminal equity > 50% of initial (soft-ruin < 5%)",
            "threshold":   params["gates"]["mc2_p5_equity_pct_min"],
        },
        "MC-3": {
            "description": "95th-percentile max drawdown < 50%",
            "threshold":   params["gates"]["mc3_p95_maxdd_max"],
        },
        "MC-4": {
            "description": "Median Sharpe (Method 2) > 0.30",
            "threshold":   params["gates"]["mc4_median_sharpe_min"],
        },
        "MC-5": {
            "description": "Soft-ruin probability (Method 1) < 5%",
            "threshold":   params["gates"]["mc5_ruin_prob_max"],
        },
        "MC-6": {
            "description": "Soft-ruin probability (Method 3 stress test) < 10%",
            "threshold":   params["gates"]["mc6_m3_ruin_prob_max"],
        },
    }

    results = {}
    passed  = 0

    # MC-1: Median CAGR from Method 1
    if method1_metrics:
        median_cagr = float(np.nanmedian(method1_metrics["cagr"]))
        gate_val    = median_cagr
        gate_pass   = gate_val > gates_def["MC-1"]["threshold"]
        results["MC-1"] = {
            **gates_def["MC-1"],
            "value":  round(gate_val, 4),
            "passed": gate_pass,
        }
        passed += int(gate_pass)
    else:
        results["MC-1"] = {**gates_def["MC-1"], "value": None, "passed": False}

    # MC-2: P5 terminal equity as % of initial
    if method1_metrics:
        p5_equity = float(np.nanpercentile(method1_metrics["terminal_equity"], 5))
        gate_val  = p5_equity / initial_equity
        gate_pass = gate_val > gates_def["MC-2"]["threshold"]
        results["MC-2"] = {
            **gates_def["MC-2"],
            "value":  round(gate_val, 4),
            "passed": gate_pass,
        }
        passed += int(gate_pass)
    else:
        results["MC-2"] = {**gates_def["MC-2"], "value": None, "passed": False}

    # MC-3: P95 max drawdown (most negative → largest drawdown across paths)
    if method1_metrics:
        # max_drawdown values are stored as signed negatives (e.g. −0.15 = −15% DD).
        # The 5th percentile of signed values = the 95th-worst drawdown magnitude.
        # Variable named p95_worst_drawdown to match the gate description.
        p95_worst_drawdown = float(np.nanpercentile(method1_metrics["max_drawdown"], 5))
        gate_val           = abs(p95_worst_drawdown)
        gate_pass = gate_val < gates_def["MC-3"]["threshold"]
        results["MC-3"] = {
            **gates_def["MC-3"],
            "value":  round(gate_val, 4),
            "passed": gate_pass,
        }
        passed += int(gate_pass)
    else:
        results["MC-3"] = {**gates_def["MC-3"], "value": None, "passed": False}

    # MC-4: Median Sharpe from Method 2 (block bootstrap — most realistic)
    if method2_metrics:
        median_sharpe = float(np.nanmedian(method2_metrics["sharpe"]))
        gate_val      = median_sharpe
        gate_pass     = gate_val > gates_def["MC-4"]["threshold"]
        results["MC-4"] = {
            **gates_def["MC-4"],
            "value":  round(gate_val, 4),
            "passed": gate_pass,
        }
        passed += int(gate_pass)
    else:
        results["MC-4"] = {**gates_def["MC-4"], "value": None, "passed": False}

    # MC-5: Soft-ruin probability (Method 1 — primary)
    if method1_metrics:
        ruin_prob = compute_ruin_probability(
            method1_metrics["terminal_equity"],
            initial_equity,
            params["soft_ruin_level"],
        )
        gate_val  = ruin_prob
        gate_pass = gate_val < gates_def["MC-5"]["threshold"]
        results["MC-5"] = {
            **gates_def["MC-5"],
            "value":  round(gate_val, 4),
            "passed": gate_pass,
        }
        passed += int(gate_pass)
    else:
        results["MC-5"] = {**gates_def["MC-5"], "value": None, "passed": False}

    # MC-6: Soft-ruin probability (Method 3 — parametric stress test)
    # Method 3 deliberately ignores autocorrelation and uses fat-tailed draws.
    # A strategy can pass MC-5 (Method 1 ruin = 0%) yet fail here if the
    # parametric stress distribution reveals tail fragility. Threshold is
    # relaxed vs MC-5 (10% vs 5%) to reflect Method 3's conservative bias.
    if method3_metrics:
        ruin_prob_m3 = compute_ruin_probability(
            method3_metrics["terminal_equity"],
            initial_equity,
            params["soft_ruin_level"],
        )
        gate_val  = ruin_prob_m3
        gate_pass = gate_val < gates_def["MC-6"]["threshold"]
        results["MC-6"] = {
            **gates_def["MC-6"],
            "value":  round(gate_val, 4),
            "passed": gate_pass,
        }
        passed += int(gate_pass)
    else:
        results["MC-6"] = {**gates_def["MC-6"], "value": None, "passed": False}
    total = len(results)
    verdict = "PASS" if passed == total else ("CAUTION" if passed >= total - 1 else "FAIL")

    return {
        "gates":       results,
        "passed":      passed,
        "total":       total,
        "verdict":     verdict,
        "recommendation": (
            "Strategy cleared Monte Carlo stress tests — proceed with live deployment."
            if verdict == "PASS"
            else (
                "Minor concern — review failed gate and monitor closely post-launch."
                if verdict == "CAUTION"
                else "Strategy failed Monte Carlo review — do not deploy without parameter revision."
            )
        ),
    }


# ============================================================================
# FLAT SUMMARY STATS  (contract with Script 21)
# ============================================================================

def _compute_flat_summary_stats(
    m1:              Dict,
    m2:              Dict,
    m3:              Dict,
    initial_equity:  float,
    base_metrics:    Dict,
    n_paths:         int,
) -> Dict:
    """
    Compute the flat top-level keys that Script 21 reads directly from
    mc_summary.json.  Script 21 calls s18_mc.get("<key>") at the top level
    of the dict — it does NOT traverse the nested method/percentile_table
    structure.

    All return values are in PERCENT (e.g. 40.0 = +40% total return).
    Probability values are also 0-100 (e.g. 65.0 = 65% of paths).

    Source selection
    ----------------
    Method 2 (block bootstrap) is used as the primary source because it
    operates on daily equity returns over the full backtest horizon.  This
    yields a total-return distribution comparable in scale to the actual
    backtest return.

    Method 1 (trade shuffling) is additive over individual trades and
    captures only trade-level P&L variation.  Its terminal equity range
    is typically much narrower than the full equity curve and should NOT
    be used for the CI / percentile keys that Script 21 relies on.

    Keys produced
    -------------
    n_simulations         : total paths run across all methods (n_paths × 3)
    pct_05_return         : 5th-pct total return (%) — primary source: M2
    pct_95_return         : 95th-pct total return (%) — primary source: M2
    prob_50pct_return     : % of M2 paths with total return ≥ 50% (0-100)
    actual_total_return   : actual backtest total return % (from baseline_metrics)
    actual_percentile_rank: where actual result sits in the M2 distribution
    """
    # Count only methods that produced valid, non-degenerate metrics.
    # Counting a failed/degenerate method inflates n_simulations and misleads
    # Script 21 (e.g. reporting 30,000 when Method 3 produced all-zero metrics).
    _valid_methods = sum(
        1 for m in (m1, m2, m3)
        if m and "metrics" in m
        and len(m["metrics"].get("cagr", [])) > 0
        and np.any(np.isfinite(m["metrics"]["cagr"]))
    )
    out: Dict = {
        "n_simulations":          n_paths * max(_valid_methods, 1),
        "pct_05_return":          None,
        "pct_95_return":          None,
        "prob_50pct_return":      None,
        "actual_total_return":    None,
        "actual_percentile_rank": None,
    }

    # ── Resolve actual backtest total return from baseline_metrics ────────────
    actual_tr: Optional[float] = None
    for key in ("total_return_pct", "total_return", "cumulative_return_pct"):
        if key in base_metrics:
            actual_tr = float(base_metrics[key])
            break
    if actual_tr is None:
        fe = base_metrics.get("final_equity") or base_metrics.get("terminal_equity")
        ie = base_metrics.get("initial_equity") or initial_equity
        if fe is not None and ie and float(ie) > 0:
            actual_tr = (float(fe) / float(ie) - 1.0) * 100.0
    if actual_tr is not None:
        out["actual_total_return"] = float(actual_tr)

    # ── Primary: Method 2 block bootstrap (full-horizon daily simulation) ─────
    # M2 runs over the same number of days as the backtest so its terminal
    # equity has the same time scale as the actual strategy result.
    primary_te: Optional[np.ndarray] = None

    if m2 and "metrics" in m2:
        te = m2["metrics"]["terminal_equity"]
        finite_te = te[np.isfinite(te) & (te > 0)]
        if len(finite_te) >= 100:          # require at least 100 valid paths
            primary_te = finite_te
            primary_label = "method2_block_bootstrap"

    # ── Fallback: Method 1 if M2 unavailable ─────────────────────────────────
    if primary_te is None and m1 and "metrics" in m1:
        te = m1["metrics"]["terminal_equity"]
        finite_te = te[np.isfinite(te) & (te > 0)]
        if len(finite_te) >= 10:
            primary_te = finite_te
            primary_label = "method1_trade_shuffling"

    if primary_te is not None:
        total_returns_pct = (primary_te / initial_equity - 1.0) * 100.0

        out["pct_05_return"]     = float(np.percentile(total_returns_pct,  5))
        out["pct_95_return"]     = float(np.percentile(total_returns_pct, 95))
        out["prob_50pct_return"] = float(np.mean(total_returns_pct >= 50.0) * 100.0)

        if actual_tr is not None:
            out["actual_percentile_rank"] = float(
                np.mean(total_returns_pct <= actual_tr) * 100.0
            )

        logger.info(
            f"Flat stats (Script 21 integration): source={primary_label} | "
            f"n_paths={len(primary_te):,} | "
            f"p05_return={out['pct_05_return']:.1f}% | "
            f"p95_return={out['pct_95_return']:.1f}% | "
            f"prob_50pct={out['prob_50pct_return']:.1f}% | "
            f"actual_return={out['actual_total_return']} | "
            f"actual_rank={out['actual_percentile_rank']}"
        )

    return out


# ============================================================================
# OUTPUT HELPERS
# ============================================================================

def save_outputs(
    summary: Dict,
    m1: Dict,
    m2: Dict,
    m3: Dict,
    fan1: Optional[pd.DataFrame],
    fan2: Optional[pd.DataFrame],
    pt1: pd.DataFrame,
    pt2: pd.DataFrame,
    pt3: pd.DataFrame,
    tag: str,
) -> None:
    """Persist all Monte Carlo outputs to disk."""
    MC_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    sfx = f"_{tag}" if tag else ""
    ts  = datetime.now().strftime("%Y%m%d")

    # --- Summary JSON ---
    summary_path = MC_DIR / f"mc_summary{sfx}.json"
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2, default=_json_serial)
    logger.info(f"Saved summary → {summary_path}")

    # --- Archived report ---
    report_path = REPORTS_DIR / f"{ts}_mc_report{sfx}.json"
    with open(report_path, "w") as f:
        json.dump(summary, f, indent=2, default=_json_serial)
    logger.info(f"Saved archive → {report_path}")

    # --- Terminal equity per path ---
    for method_tag, res in [("method1", m1), ("method2", m2), ("method3", m3)]:
        if not res:
            continue
        path = MC_DIR / f"mc_paths_{method_tag}{sfx}.csv"
        te = res["metrics"]["terminal_equity"]
        pd.Series(te, name="terminal_equity").to_csv(path, index=False)
        logger.info(f"Saved path terminals ({method_tag}) → {path}")

    # --- Percentile tables ---
    for method_tag, pt in [("method1", pt1), ("method2", pt2), ("method3", pt3)]:
        if pt is None or pt.empty:
            continue
        path = MC_DIR / f"mc_percentile_table_{method_tag}{sfx}.csv"
        pt.to_csv(path)
        logger.info(f"Saved percentile table ({method_tag}) → {path}")

    # --- Fan chart data ---
    for method_tag, fan in [("method1", fan1), ("method2", fan2)]:
        if fan is None or fan.empty:
            continue
        path = MC_DIR / f"mc_equity_fan_{method_tag}{sfx}.csv"
        fan.to_csv(path, index=False)
        logger.info(f"Saved fan chart ({method_tag}) → {path}")


def _json_serial(obj):
    """JSON serialiser for numpy types."""
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, pd.Timestamp):
        return obj.isoformat()
    raise TypeError(f"Type {type(obj)} not serialisable")


def print_summary(summary: Dict) -> None:
    """Print a human-readable summary to stdout."""
    w = 72
    print()
    print("=" * w)
    print("  MONTE CARLO SIMULATION RESULTS  —  Script 18")
    print("=" * w)
    print(f"  Paths per method  : {summary['n_paths_per_method']:,}")
    print(f"  Seed              : {summary['seed']}")
    print(f"  Initial equity    : ${summary['initial_equity']:,.0f}")
    print(f"  Backtest days     : {summary['n_backtest_days']}")
    print()

    # Per-method summaries
    for m_key, m_label in [
        ("method1", "Method 1 — Trade Shuffling"),
        ("method2", "Method 2 — Block Bootstrap"),
        ("method3", "Method 3 — Parametric    "),
    ]:
        ms = summary.get(m_key, {})
        if not ms:
            continue
        pt = ms.get("percentile_table", {})
        cagr   = pt.get("cagr", {})
        sharpe = pt.get("sharpe", {})
        mdd    = pt.get("max_drawdown", {})

        # Warn if all CAGR percentiles are near zero — indicates degenerate
        # simulation (e.g. Method 3 Student-t fit with near-zero location).
        _c5, _c50, _c95 = (cagr.get("p5"), cagr.get("p50"), cagr.get("p95"))
        _degenerate = (
            _c5  is not None and abs(_c5)  < 1e-6 and
            _c50 is not None and abs(_c50) < 1e-6 and
            _c95 is not None and abs(_c95) < 1e-6
        )

        print(f"  {m_label}")
        if _degenerate:
            print(f"    ⚠️  WARNING: all CAGR percentiles ≈ 0 — "
                  "simulation may be degenerate. Check logs for 'degenerate' entries.")
        print(f"    CAGR   p5={cagr.get('p5', float('nan')):.1%}  "
              f"p50={cagr.get('p50', float('nan')):.1%}  "
              f"p95={cagr.get('p95', float('nan')):.1%}")
        print(f"    Sharpe p5={sharpe.get('p5', float('nan')):.2f}  "
              f"p50={sharpe.get('p50', float('nan')):.2f}  "
              f"p95={sharpe.get('p95', float('nan')):.2f}")
        print(f"    MaxDD  p5={mdd.get('p5', float('nan')):.1%}  "
              f"p50={mdd.get('p50', float('nan')):.1%}  "
              f"p95={mdd.get('p95', float('nan')):.1%}")
        print(f"    Ruin prob : {ms.get('ruin_probability', float('nan')):.2%}")
        print(f"    Blow-up   : {ms.get('blowup_probability', float('nan')):.2%}")
        print()

    # Script 21 integration stats
    print(f"  ── Script 21 Integration Keys ──────────────────────────────")
    print(f"  n_simulations         : {summary.get('n_simulations', 'N/A'):,}" if isinstance(summary.get('n_simulations'), int) else f"  n_simulations         : {summary.get('n_simulations', 'N/A')}")
    atr = summary.get("actual_total_return")
    apr = summary.get("actual_percentile_rank")
    p05 = summary.get("pct_05_return")
    p95 = summary.get("pct_95_return")
    p50 = summary.get("prob_50pct_return")
    print(f"  actual_total_return   : {f'{atr:.2f}%' if atr is not None else 'N/A'}")
    print(f"  actual_percentile_rank: {f'{apr:.1f}th pct' if apr is not None else 'N/A'}")
    print(f"  pct_05_return         : {f'{p05:.2f}%' if p05 is not None else 'N/A'}")
    print(f"  pct_95_return         : {f'{p95:.2f}%' if p95 is not None else 'N/A'}")
    print(f"  prob_50pct_return     : {f'{p50:.1f}%' if p50 is not None else 'N/A'} of paths")
    print()

    # Gate results
    gc = summary.get("gate_check", {})
    verdict = gc.get("verdict", "UNKNOWN")
    colour_map = {"PASS": "✅", "CAUTION": "⚠️", "FAIL": "❌"}
    icon = colour_map.get(verdict, "?")
    print(f"  GATE VERDICT : {icon}  {verdict}  ({gc.get('passed', '?')}/{gc.get('total', 6)} gates passed)")
    for gid, gr in gc.get("gates", {}).items():
        icon_g = "✅" if gr["passed"] else "❌"
        val_str = f"{gr['value']:.4f}" if gr["value"] is not None else "N/A"
        print(f"    {icon_g} {gid}: {gr['description']}  [value={val_str}]")
    print()
    print(f"  RECOMMENDATION: {gc.get('recommendation', '')}")
    print("=" * w)
    print()


# ============================================================================
# MAIN SIMULATOR CLASS
# ============================================================================

class MonteCarloSimulator:
    """
    Orchestrates all three MC simulation methods, computes metrics, evaluates
    gates, and persists outputs.
    """

    def __init__(self, params: Dict):
        self.params = params
        self.rng    = np.random.default_rng(params["seed"])

    def run(
        self,
        equity_df:   pd.DataFrame,
        trade_df:    pd.DataFrame,
        base_metrics: Dict,
        tag:         str = "",
        fast_mode:   bool = False,
    ) -> Dict:
        """
        Run full Monte Carlo simulation suite.

        Parameters
        ----------
        equity_df    : daily equity curve from Script 16
        trade_df     : round-trip trade log from Script 16
        base_metrics : baseline performance metrics from Script 16
        tag          : output file suffix
        fast_mode    : use fewer paths (2 000) and skip fan chart storage

        Returns
        -------
        Full simulation summary dict
        """
        p = self.params
        n_paths     = 2_000 if fast_mode else p["n_paths"]
        trading_days = p["trading_days"]
        rf           = p["risk_free_rate"]
        initial_eq   = float(equity_df["equity"].iloc[0])
        n_bt_days    = len(equity_df) - 1

        logger.info(f"Initial equity: ${initial_eq:,.0f}")
        logger.info(f"Backtest days : {n_bt_days}")
        logger.info(f"Paths/method  : {n_paths:,}")

        # --- Extract empirical series ---
        daily_rets  = extract_daily_returns(equity_df)
        pnl_dollars, pnl_source = extract_trade_pnl_dollars(trade_df, initial_eq)
        n_trades    = len(trade_df)
        bt_years    = n_bt_days / trading_days  # correct time horizon for Method 1

        # ── METHOD 1: Trade Shuffling ────────────────────────────────────────
        logger.info("Running Method 1 — Trade Shuffling ...")
        m1 = simulate_trade_shuffling(
            pnl_dollars    = pnl_dollars,
            initial_equity = initial_eq,
            n_paths        = n_paths,
            n_trades       = n_trades,
            rng            = self.rng,
            trading_days   = trading_days,
            risk_free_rate = rf,
            bt_years       = bt_years,
            pnl_source     = pnl_source,
        )

        # ── METHOD 2: Block Bootstrap ────────────────────────────────────────
        logger.info("Running Method 2 — Block Bootstrap ...")
        m2 = simulate_block_bootstrap(
            daily_returns  = daily_rets,
            initial_equity = initial_eq,
            n_paths        = n_paths,
            block_length   = p["block_length"],
            rng            = self.rng,
            trading_days   = trading_days,
            risk_free_rate = rf,
        )

        # ── METHOD 3: Parametric ─────────────────────────────────────────────
        logger.info("Running Method 3 — Parametric ...")
        m3 = simulate_parametric(
            daily_returns  = daily_rets,
            initial_equity = initial_eq,
            n_paths        = n_paths,
            rng            = self.rng,
            trading_days   = trading_days,
            risk_free_rate = rf,
        )

        # ── Percentile Tables ────────────────────────────────────────────────
        pcts = REPORT_PERCENTILES
        pt1 = percentile_table(m1["metrics"], pcts) if m1 else pd.DataFrame()
        pt2 = percentile_table(m2["metrics"], pcts) if m2 else pd.DataFrame()
        pt3 = percentile_table(m3["metrics"], pcts) if m3 else pd.DataFrame()

        # ── Ruin / Blow-up ───────────────────────────────────────────────────
        def ruin_blowup(res):
            if not res:
                return float("nan"), float("nan")
            ruin   = compute_ruin_probability(
                res["metrics"]["terminal_equity"], initial_eq, p["soft_ruin_level"]
            )
            blowup = compute_blowup_probability(
                res["metrics"]["max_drawdown"], p["hard_dd_limit"]
            )
            return ruin, blowup

        r1, b1 = ruin_blowup(m1)
        r2, b2 = ruin_blowup(m2)
        r3, b3 = ruin_blowup(m3)

        # ── Fan Charts ───────────────────────────────────────────────────────
        fan_pcts = [5, 25, 50, 75, 95]
        fan1, fan2 = None, None
        if not fast_mode:
            if m1:
                fan1 = fan_chart_data(m1["equity_matrix"], n_trades, fan_pcts, initial_eq)
            if m2:
                fan2 = fan_chart_data(m2["equity_matrix"], n_bt_days, fan_pcts, initial_eq)

        # ── Gate Evaluation ──────────────────────────────────────────────────
        gate_result = evaluate_gates(
            method1_metrics = m1["metrics"] if m1 else {},
            method2_metrics = m2["metrics"] if m2 else {},
            method3_metrics = m3["metrics"] if m3 else {},
            initial_equity  = initial_eq,
            params          = {**p, "gates": GATES},
        )

        # ── Build Summary Dict ───────────────────────────────────────────────
        def pt_to_dict(pt: pd.DataFrame) -> Dict:
            if pt is None or pt.empty:
                return {}
            return {
                metric: {col: row[col] for col in pt.columns}
                for metric, row in pt.iterrows()
            }

        def method_summary(res, ruin, blowup, pt):
            if not res:
                return {}
            info = {k: v for k, v in res.items() if k not in ("equity_matrix", "metrics")}
            info["ruin_probability"]  = float(ruin)  if np.isfinite(ruin)  else None
            info["blowup_probability"] = float(blowup) if np.isfinite(blowup) else None
            info["percentile_table"]  = pt_to_dict(pt)
            return info

        # ── Flat convenience stats — consumed directly by Script 21 ─────────
        flat_stats = _compute_flat_summary_stats(
            m1             = m1,
            m2             = m2,
            m3             = m3,
            initial_equity = initial_eq,
            base_metrics   = base_metrics,
            n_paths        = n_paths,
        )

        summary = {
            "generated_at":        datetime.now().isoformat(),
            "backtest_tag":        tag,
            "n_paths_per_method":  n_paths,
            # Script 21 reads these flat keys directly at top level:
            #   n_simulations, pct_05_return, pct_95_return, prob_50pct_return,
            #   actual_total_return, actual_percentile_rank
        }
        summary.update(flat_stats)  # inject flat stats without nesting
        summary.update({
            "seed":                p["seed"],
            "initial_equity":      float(initial_eq),
            "n_backtest_days":     n_bt_days,
            "n_trades":            n_trades,
            "risk_free_rate":      rf,
            "block_length":        p["block_length"],
            "soft_ruin_level":     p["soft_ruin_level"],
            "hard_dd_limit":       p["hard_dd_limit"],
            "method1":             method_summary(m1, r1, b1, pt1),
            "method2":             method_summary(m2, r2, b2, pt2),
            "method3":             method_summary(m3, r3, b3, pt3),
            "gate_check":          gate_result,
            "baseline_metrics":    base_metrics,
        })

        # ── Persist ──────────────────────────────────────────────────────────
        save_outputs(
            summary=summary, m1=m1, m2=m2, m3=m3,
            fan1=fan1, fan2=fan2,
            pt1=pt1, pt2=pt2, pt3=pt3,
            tag=tag,
        )

        return summary


# ============================================================================
# CLI
# ============================================================================

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Script 18 — Monte Carlo Simulator for the trend-following strategy",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--backtest-tag", default="",
        help="Backtest output tag used by Script 16 (e.g. 'run_01')",
    )
    p.add_argument(
        "--output-tag", default="",
        help="Tag appended to all Monte Carlo output filenames",
    )
    p.add_argument(
        "--n-paths", type=int, default=DEFAULTS["n_paths"],
        help="Number of Monte Carlo paths per simulation method",
    )
    p.add_argument(
        "--block-length", type=int, default=DEFAULTS["block_length"],
        help="Block length (days) for Method 2 block bootstrap",
    )
    p.add_argument(
        "--risk-free-rate", type=float, default=DEFAULTS["risk_free_rate"],
        help="Annualised risk-free rate (decimal) for Sharpe/Sortino",
    )
    p.add_argument(
        "--soft-ruin-level", type=float, default=DEFAULTS["soft_ruin_level"],
        help="Terminal equity below this fraction of initial = soft ruin (default 0.50)",
    )
    p.add_argument(
        "--hard-dd-limit", type=float, default=DEFAULTS["hard_dd_limit"],
        help="Max drawdown above this level = blow-up (default 0.40)",
    )
    p.add_argument(
        "--seed", type=int, default=DEFAULTS["seed"],
        help="Random seed for reproducibility",
    )
    p.add_argument(
        "--fast-mode", action="store_true",
        help="Use 2 000 paths and skip fan chart generation (for CI or quick checks)",
    )
    p.add_argument(
        "--verbose", action="store_true",
        help="Enable DEBUG-level logging",
    )
    return p.parse_args()


# ============================================================================
# MAIN
# ============================================================================

def main() -> None:
    args = parse_args()

    global logger
    logger = setup_logging(args.output_tag)
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    params = {
        "n_paths":         args.n_paths,
        "block_length":    args.block_length,
        "risk_free_rate":  args.risk_free_rate,
        "trading_days":    DEFAULTS["trading_days"],
        "soft_ruin_level": args.soft_ruin_level,
        "hard_dd_limit":   args.hard_dd_limit,
        "seed":            args.seed,
    }

    # Load Script 16 outputs
    logger.info("Loading backtest outputs from Script 16 ...")
    equity_df    = load_equity_curve(args.backtest_tag)
    trade_df     = load_trade_log(args.backtest_tag)
    base_metrics = load_performance_metrics(args.backtest_tag)

    # Run simulation
    simulator = MonteCarloSimulator(params)
    summary   = simulator.run(
        equity_df    = equity_df,
        trade_df     = trade_df,
        base_metrics = base_metrics,
        tag          = args.output_tag,
        fast_mode    = args.fast_mode,
    )

    # Print results
    print_summary(summary)
    logger.info("Script 18 finished.")


# ============================================================================
# PUBLIC API  (imported by Script 0 / test harnesses)
# ============================================================================

def run_monte_carlo(
    equity_df:    pd.DataFrame,
    trade_df:     pd.DataFrame,
    base_metrics: Optional[Dict] = None,
    params:       Optional[Dict] = None,
    tag:          str = "",
    fast_mode:    bool = False,
) -> Dict:
    """
    Programmatic entry-point for external callers (pipeline orchestrator,
    test harnesses, notebooks).

    Parameters
    ----------
    equity_df    : daily equity curve (columns: date, equity, [drawdown])
    trade_df     : round-trip trade log from Script 16
    base_metrics : optional baseline metrics dict from Script 16
    params       : optional parameter overrides (merged with DEFAULTS)
    tag          : output file tag
    fast_mode    : use 2 000 paths

    Returns
    -------
    Full simulation summary dict
    """
    _params = {**DEFAULTS, **(params or {})}
    sim     = MonteCarloSimulator(_params)
    return sim.run(
        equity_df    = equity_df,
        trade_df     = trade_df,
        base_metrics = base_metrics or {},
        tag          = tag,
        fast_mode    = fast_mode,
    )


if __name__ == "__main__":
    main()
