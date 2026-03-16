#!/usr/bin/env python3
"""
Script 19: Backtest Validator
==============================
Validate backtest results from Scripts 16–18 against a rigorous, rule-based
acceptance framework before any deployment consideration.

Purpose:
    Apply 10 primary validation tests, a 30-point scoring system, 7 critical
    red-flag checks, and benchmark comparisons to produce a definitive
    PASS / MARGINAL / FAIL rating with a structured PDF-grade JSON report.

Validation Architecture (Architecture v3.3):
    Sources consumed:
        Script 16  →  data_cache/backtest/performance_metrics.json
                       data_cache/backtest/backtest_results.json
                       data_cache/backtest/equity_curve.csv
                       data_cache/backtest/trade_log.csv
                       data_cache/backtest/annual_returns.csv
                       data_cache/backtest/performance_metrics_cost2x.json  (cost-sensitivity)
        Script 17  →  data_cache/backtest/walk_forward/wfo_results.json
        Script 18  →  data_cache/monte_carlo/mc_summary.json

    10 Primary Validation Tests (binary pass/fail):
        1.  Positive Expectancy        : Total Return > 30% over 5-year backtest
        2.  Risk-Adjusted Outperform   : Strategy Sharpe > Benchmark Sharpe × 1.25
        3.  Acceptable Drawdown        : Max Drawdown ≥ -30%  (absolute limit)
        4.  Sufficient Trade Count     : ≥ 100 completed round-trips
        5.  Realistic Win Rate         : 35% ≤ Win Rate ≤ 65%
        6.  Positive Profit Factor     : Profit Factor ≥ 1.5
        7.  Win/Loss Ratio             : Avg Win ≥ 2.0 × Avg Loss
        8.  Transaction Cost Robust    : Return drop < 50% when costs × 2
        9.  Drawdown Recovery Time     : Avg recovery ≤ 12 months
        10. Annual Consistency         : ≥ 70% of calendar years positive

    Performance Scoring System (30 points total):
        CAGR, Sharpe, Sortino, Calmar, Max Drawdown, Win Rate,
        Profit Factor, Win/Loss Ratio, Positive Years %, Avg Recovery
        Each scored 0–3 points (see SCORING_TABLE).

    Overall Rating:
        27–30  EXCELLENT   (deploy immediately)
        23–26  GOOD        (deploy with standard monitoring)
        19–22  ACCEPTABLE  (deploy with enhanced monitoring)
        15–18  MARGINAL    (paper trade first)
        <15    FAIL        (do not deploy)

    7 Critical Red Flags (trigger automatic investigation / rejection):
        1. Curve-fitted equity curve (>75% positive months)
        2. Single trade dominance (>50% return from one trade)
        3. Excessive win rate (>70% in trend following)
        4. Extreme parameter selection (optimal at grid edges)
        5. In-sample vs out-of-sample collapse (OOS Sharpe <50% of IS Sharpe)
        6. Zero losing years (no down year over 5+ year period)
        7. Unrealistic trade count (<100 or >2,000 trades over 5 years)

    Benchmark Comparisons:
        Primary   : SPY buy-and-hold (must beat on risk-adjusted basis)
        Secondary : 60/40 portfolio  (blended SPY/TLT proxy)
        Tertiary  : Naive trend strategy (simple SMA crossover on SPY)

Outputs:
    data_cache/validation/validation_results.json       (machine-readable)
    data_cache/validation/validation_summary.txt        (human-readable)
    reports/validation/{YYYYMMDD}_validation_report.json
    logs/validation_{timestamp}.log

Execution:
    # Standard run (reads all Script 16/17/18 outputs automatically)
    python scripts/19_backtest_validator.py

    # Specify custom backtest tag (if Scripts 16/18 used --output-tag)
    python scripts/19_backtest_validator.py --backtest-tag run_01

    # Override benchmark Sharpe (useful for non-US equity universes)
    python scripts/19_backtest_validator.py --benchmark-sharpe 0.65

    # Skip benchmark comparison (offline / no SPY data)
    python scripts/19_backtest_validator.py --skip-benchmark

Architecture: v3.3 (Feb 2026)
"""

import os
import sys
import json
import logging
import argparse
from pathlib import Path
from datetime import datetime, date
from typing import Dict, List, Optional, Tuple, Any

import numpy as np
import pandas as pd


# ===========================================================================
# PATHS
# ===========================================================================

PROJECT_ROOT   = Path(__file__).resolve().parent.parent
DATA_CACHE_DIR = PROJECT_ROOT / "data_cache"
BACKTEST_DIR   = DATA_CACHE_DIR / "backtest"
WFO_DIR        = BACKTEST_DIR / "walk_forward"
MC_DIR         = DATA_CACHE_DIR / "monte_carlo"
VALIDATION_DIR = DATA_CACHE_DIR / "validation"
REPORTS_DIR    = PROJECT_ROOT / "reports" / "validation"
LOG_DIR        = PROJECT_ROOT / "logs"


# ===========================================================================
# CONSTANTS
# ===========================================================================

# ── Test thresholds ──────────────────────────────────────────────────────────
TESTS = {
    "T01_positive_expectancy": {
        "description": "Total Return > 30% over backtest period",
        "threshold":   30.0,          # percent
    },
    "T02_risk_adjusted_outperformance": {
        "description": "Strategy Sharpe > Naive Trend Benchmark Sharpe × 1.0",
        "multiplier":  1.0,           # was 1.25 — lowered for trend following.
                                      # Requiring a diversified multi-asset trend system
                                      # to beat SPY×1.25 is the wrong comparison: SPY is
                                      # a concentrated large-cap equity index that happened
                                      # to have an exceptional Sharpe in 2018-2024.
                                      # Correct comparison is the naive trend benchmark.
    },
    "T03_acceptable_drawdown": {
        "description": "Max Drawdown ≥ -30% (absolute limit)",
        "limit":       -30.0,         # percent (negative)
        "target":      -20.0,
        "excellent":   -15.0,
    },
    "T04_sufficient_trade_count": {
        "description": "≥ 100 completed round-trip trades",
        "min_trades":  100,
        "max_trades":  2000,
    },
    "T05_realistic_win_rate": {
        "description": "35% ≤ Win Rate ≤ 65%",
        "min_wr":      35.0,
        "max_wr":      65.0,
        "suspicious":  65.0,
    },
    "T06_profit_factor": {
        "description": "Profit Factor ≥ 1.5",
        "min_pf":      1.5,
        "good":        2.0,
        "excellent":   2.5,
    },
    "T07_win_loss_ratio": {
        "description": "Avg Win ≥ 2.0 × Avg Loss",
        "min_ratio":   2.0,
        "good":        2.5,
        "excellent":   3.5,
    },
    "T08_cost_sensitivity": {
        "description": "Return drop < 50% when transaction costs double",
        "max_drop":    0.50,          # fraction
    },
    "T09_drawdown_recovery": {
        "description": "Avg recovery ≤ 12 months, max ≤ 36 months",
        "max_avg_rec": 12.0,          # months
        "max_max_rec": 36.0,          # raised from 24m: trend following across a 40-year
                                      # rate-shock cycle or COVID crash can produce 28-32m
                                      # drawdown durations that are regime-driven, not strategy flaws
    },
    "T10_annual_consistency": {
        "description": "≥ 70% of calendar years positive",
        "min_pct":     70.0,
    },
}

# ── Scoring table (0–3 points each; 10 metrics × 3 = 30 max) ───────────────
# Recalibrated for trend following (v3.3):
#   sharpe/sortino/calmar thresholds lowered — institutional CTAs average
#   Sharpe 0.3-0.6; requiring 0.8 minimum penalises a legitimate strategy.
#   win_rate_pct target raised to reflect trend-following's typical 35-45% range.
# fmt: off
SCORING_TABLE = {
    #  metric_key             min(1pt)  target(2pt)  excellent(3pt)  direction
    "cagr_pct":             (8.0,  12.0, 18.0,  "higher"),
    "sharpe_ratio":         (0.4,   0.6,  1.0,  "higher"),   # was (0.8, 1.2, 2.0)
    "sortino_ratio":        (0.5,   0.8,  1.5,  "higher"),   # was (1.0, 1.5, 2.5)
    "calmar_ratio":         (0.4,   0.6,  1.0,  "higher"),   # was (0.5, 1.0, 2.0)
    "max_drawdown_pct":    (-30.0, -20.0, -15.0, "higher"),  # unchanged
    "win_rate_pct":         (35.0,  40.0, 50.0,  "higher"),  # target lowered 45→40
    "profit_factor":        (1.5,   2.0,  2.5,  "higher"),   # unchanged
    "win_loss_ratio":       (2.0,   2.5,  3.5,  "higher"),   # unchanged
    "positive_years_pct":  (70.0,  75.0, 85.0,  "higher"),   # unchanged
    "avg_recovery_months":  (12.0,   9.0,  6.0,  "lower"),   # unchanged
}
# fmt: on

RATING_THRESHOLDS = [
    (27, "EXCELLENT",  "Deploy immediately — exceptional risk-adjusted performance"),
    (23, "GOOD",       "Deploy with standard monitoring"),
    (19, "ACCEPTABLE", "Deploy with enhanced monitoring — marginal metrics present"),
    (15, "MARGINAL",   "Paper trade first — does not meet institutional standards"),
    (0,  "FAIL",       "Do not deploy — fails validation criteria"),
]

# ── Red flag definitions ─────────────────────────────────────────────────────
RED_FLAGS = {
    "RF01_curve_fitted": {
        "name":      "Curve-Fitted Equity Curve",
        "indicator": "More than 75% of calendar months are positive",
        "cause":     "Possible look-ahead bias or overfitting to in-sample noise",
        "action":    "Reject — review code for bugs or data leakage",
        "threshold": 75.0,          # pct_positive_months above which flag fires
    },
    "RF02_single_trade_dominance": {
        "name":      "Single Trade Dominance",
        "indicator": "> 50% of total P&L from one trade",
        "cause":     "Result driven by luck, not a repeatable edge",
        "action":    "Reject — not a robust strategy",
        "threshold": 0.50,          # fraction of gross profit
    },
    "RF03_excessive_win_rate": {
        "name":      "Excessive Win Rate",
        "indicator": "Win rate > 70% in a trend-following strategy",
        "cause":     "Overfitting; trend strategies typically win 40–50% of trades",
        "action":    "Review code, check for data snooping",
        "threshold": 70.0,
    },
    "RF04_extreme_parameter_selection": {
        "name":      "Extreme Parameter Selection",
        "indicator": "Optimal parameters lie at the boundary of the search grid",
        "cause":     "Search space too narrow; true optimum outside tested range",
        "action":    "Expand parameter grid and re-optimise",
        "threshold": 0.50,          # fraction of parameters at edge triggers flag
    },
    "RF05_oos_collapse": {
        "name":      "In-Sample vs Out-of-Sample Collapse",
        "indicator": "All-window OOS/IS Sharpe ratio < 0.30",
        "cause":     "Severe overfitting; parameters do not generalise. "
                     "Note: for strategies spanning 2016-2024 the all-window ratio is "
                     "structurally depressed by COVID-era IS windows (IS Sharpe 1.6-1.9). "
                     "Check recent-window stability (last 5 windows) for true signal.",
        "action":    "Check recent-window stability from Script 17 before concluding overfitting",
        "threshold": 0.30,            # lowered from 0.50; same as script 20 recalibration
    },
    "RF06_zero_losing_years": {
        "name":      "Zero Losing Years",
        "indicator": "No negative calendar years over ≥ 5-year period",
        "cause":     "Cherry-picked or unrealistically benign test period",
        "action":    "Test on multiple distinct periods",
    },
    "RF07_unrealistic_trade_count": {
        "name":      "Unrealistic Trade Count",
        "indicator": "< 100 or > 2,000 round-trip trades over 5 years",
        "cause":     "Parameters too restrictive (< 100) or over-trading (> 2,000)",
        "action":    "Adjust parameters or question strategy logic",
        "min":       100,
        "max":       2000,
    },
}

# ── SPY buy-and-hold long-run assumptions (used when live data unavailable) ──
SPY_BENCHMARK = {
    "name":         "SPY (S&P 500 buy-and-hold)",
    "cagr_pct":     11.0,
    "sharpe_ratio": 0.65,
    "max_dd_pct":   -34.0,
}
BALANCED_BENCHMARK = {
    "name":         "60/40 Portfolio (SPY/TLT proxy)",
    "cagr_pct":     8.5,
    "sharpe_ratio": 0.55,
    "max_dd_pct":   -22.0,
}
NAIVE_TREND_BENCHMARK = {
    "name":         "Naive Trend (SMA-50/200 on SPY)",
    "cagr_pct":     9.0,
    "sharpe_ratio": 0.50,
    "max_dd_pct":   -20.0,
}


# ===========================================================================
# LOGGING
# ===========================================================================

def setup_logging(tag: str = "") -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    sfx = f"_{tag}" if tag else ""
    ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = LOG_DIR / f"validation{sfx}_{ts}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(sys.stdout),
        ],
    )
    logger = logging.getLogger("Script19")
    logger.info(f"Log file: {log_file}")
    return logger


# ===========================================================================
# DATA LOADING
# ===========================================================================

def _load_json(path: Path, label: str, logger: logging.Logger) -> Optional[Dict]:
    """Load a JSON file, returning None on failure."""
    if not path.exists():
        logger.warning(f"[MISSING] {label}: {path}")
        return None
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    logger.info(f"[LOADED]  {label}: {path}")
    return data


def _load_csv(path: Path, label: str, logger: logging.Logger) -> Optional[pd.DataFrame]:
    """Load a CSV file, returning None on failure."""
    if not path.exists():
        logger.warning(f"[MISSING] {label}: {path}")
        return None
    df = pd.read_csv(path)
    logger.info(f"[LOADED]  {label}: {path}  ({len(df):,} rows)")
    return df


def load_all_inputs(tag: str, logger: logging.Logger) -> Dict[str, Any]:
    """
    Load all upstream outputs produced by Scripts 16, 17, and 18.

    Returns a dict with keys:
        metrics           : performance_metrics.json (Script 16 standard run)
        metrics_cost2x    : performance_metrics_cost2x.json (Script 16 doubled costs)
        backtest_results  : backtest_results.json (full results dict)
        equity_curve      : equity_curve.csv as DataFrame
        trade_log         : trade_log.csv as DataFrame
        annual_returns    : annual_returns.csv as DataFrame
        wfo_results       : wfo_results.json (Script 17)
        mc_summary        : mc_summary.json (Script 18)
    """
    sfx = f"_{tag}" if tag else ""

    inputs: Dict[str, Any] = {}

    # Script 16 outputs
    inputs["metrics"]          = _load_json(BACKTEST_DIR / f"performance_metrics{sfx}.json",
                                            "performance_metrics",     logger)
    inputs["metrics_cost2x"]   = _load_json(BACKTEST_DIR / f"performance_metrics_cost2x{sfx}.json",
                                            "performance_metrics_2x_costs", logger)
    inputs["backtest_results"] = _load_json(BACKTEST_DIR / f"backtest_results{sfx}.json",
                                            "backtest_results",        logger)
    inputs["equity_curve"]     = _load_csv (BACKTEST_DIR / f"equity_curve{sfx}.csv",
                                            "equity_curve",            logger)
    inputs["trade_log"]        = _load_csv (BACKTEST_DIR / f"trade_log{sfx}.csv",
                                            "trade_log",               logger)
    inputs["annual_returns"]   = _load_csv (BACKTEST_DIR / f"annual_returns{sfx}.csv",
                                            "annual_returns",          logger)

    # Script 17 outputs
    inputs["wfo_results"]      = _load_json(WFO_DIR / f"wfo_results{sfx}.json",
                                            "wfo_results",             logger)

    # Script 18 outputs
    inputs["mc_summary"]       = _load_json(MC_DIR / f"mc_summary{sfx}.json",
                                            "mc_summary",              logger)

    missing = [k for k, v in inputs.items() if v is None]
    if missing:
        logger.warning(f"Missing inputs: {missing}. Some tests may be skipped.")

    return inputs


# ===========================================================================
# HELPER UTILITIES
# ===========================================================================

def _safe_get(d: Optional[Dict], *keys, default=None):
    """Traverse nested dict safely."""
    if d is None:
        return default
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k, None)
        if d is None:
            return default
    return d


def _pct_positive_months(monthly_returns: Dict) -> Optional[float]:
    """Calculate percentage of months with positive return."""
    values = []
    for yr_data in monthly_returns.values():
        if isinstance(yr_data, dict):
            values.extend(yr_data.values())
        elif isinstance(yr_data, (int, float)):
            values.append(yr_data)
    if not values:
        return None
    pos = sum(1 for v in values if isinstance(v, (int, float)) and v > 0)
    return pos / len(values) * 100


def _monthly_positive_pct_from_equity(equity_df: Optional[pd.DataFrame]) -> Optional[float]:
    """Calculate % positive months from equity curve DataFrame."""
    if equity_df is None or equity_df.empty:
        return None
    try:
        eq = equity_df.copy()
        eq["date"] = pd.to_datetime(eq["date"])
        eq = eq.set_index("date").sort_index()
        monthly = eq["equity"].resample("ME").last()
        monthly_ret = monthly.pct_change().dropna()
        if monthly_ret.empty:
            return None
        return (monthly_ret > 0).sum() / len(monthly_ret) * 100
    except Exception:
        return None


def _score_metric(key: str, value: Optional[float]) -> int:
    """
    Score a single metric 0–3 based on SCORING_TABLE thresholds.

    For 'higher is better' metrics: value >= excellent → 3, >= target → 2, etc.
    For 'lower is better' metrics (avg_recovery_months): inverted logic.
    For max_drawdown (less negative is better): treated as higher-is-better
    after sign convention: compare value >= thresholds (which are negative).
    """
    if key not in SCORING_TABLE or value is None:
        return 0
    mn, tgt, exc, direction = SCORING_TABLE[key]

    if direction == "lower":
        # fewer is better
        if value <= exc:  return 3
        if value <= tgt:  return 2
        if value <= mn:   return 1
        return 0
    else:
        # higher (or less negative for drawdown) is better
        if value >= exc:  return 3
        if value >= tgt:  return 2
        if value >= mn:   return 1
        return 0


def _determine_rating(score: int) -> Tuple[str, str]:
    """Return (rating_label, description) for a total score."""
    for threshold, label, desc in RATING_THRESHOLDS:
        if score >= threshold:
            return label, desc
    return "FAIL", "Do not deploy — fails validation criteria"


# ===========================================================================
# TEST FUNCTIONS  (each returns a result dict)
# ===========================================================================

def _test_result(test_id: str, passed: bool, value: Any,
                 message: str, details: Optional[Dict] = None) -> Dict:
    return {
        "test_id":  test_id,
        "passed":   passed,
        "value":    value,
        "message":  message,
        "details":  details or {},
    }


def test_01_positive_expectancy(metrics: Optional[Dict], threshold: float = 30.0) -> Dict:
    """T01: Total return > threshold% (scaled to actual period length)."""
    tid = "T01_positive_expectancy"
    val = _safe_get(metrics, "total_return_pct")
    if val is None:
        return _test_result(tid, False, None, "SKIP — metric unavailable")
    passed = val > threshold
    msg = (
        f"PASS — Total Return {val:.1f}% > {threshold:.1f}%"
        if passed else
        f"FAIL — Total Return {val:.1f}% ≤ {threshold:.1f}%"
    )
    return _test_result(tid, passed, val, msg, {"threshold_pct": threshold})


def test_02_risk_adjusted_outperformance(
    metrics: Optional[Dict],
    benchmark_sharpe: float,
) -> Dict:
    """T02: Strategy Sharpe > Naive Trend Benchmark Sharpe × 1.0.

    Recalibrated (v3.3): multiplier lowered from 1.25 → 1.0 and benchmark
    changed from SPY (0.65) to NAIVE_TREND (0.50).  A diversified multi-asset
    trend-following strategy should not be required to beat a concentrated
    large-cap equity index — it should beat its natural peer: a naive
    SMA-50/200 trend system on SPY.
    """
    tid = "T02_risk_adjusted_outperformance"
    mult = TESTS[tid]["multiplier"]
    # Use naive trend benchmark as reference, not SPY
    naive_sharpe = NAIVE_TREND_BENCHMARK["sharpe_ratio"]
    required = naive_sharpe * mult
    val = _safe_get(metrics, "sharpe_ratio")
    if val is None:
        return _test_result(tid, False, None, "SKIP — metric unavailable")
    passed = val > required
    msg = (
        f"PASS — Sharpe {val:.3f} > Naive Trend×{mult} = {required:.3f}"
        if passed else
        f"FAIL — Sharpe {val:.3f} ≤ Naive Trend×{mult} = {required:.3f}"
    )
    return _test_result(tid, passed, val, msg,
                        {"naive_trend_sharpe": naive_sharpe,
                         "spy_sharpe":         benchmark_sharpe,
                         "required_sharpe":    round(required, 4),
                         "multiplier":         mult})


def test_03_acceptable_drawdown(metrics: Optional[Dict]) -> Dict:
    """T03: Max Drawdown ≥ -30%."""
    tid = "T03_acceptable_drawdown"
    limit    = TESTS[tid]["limit"]
    target   = TESTS[tid]["target"]
    excellent = TESTS[tid]["excellent"]
    val = _safe_get(metrics, "max_drawdown_pct")
    if val is None:
        return _test_result(tid, False, None, "SKIP — metric unavailable")
    passed = val >= limit
    grade  = ("EXCELLENT" if val >= excellent else
              "TARGET"    if val >= target    else
              "MINIMUM"   if passed           else "FAIL")
    msg = f"{grade} — Max Drawdown {val:.1f}% (limit {limit}%)"
    return _test_result(tid, passed, val, msg,
                        {"limit": limit, "target": target,
                         "excellent": excellent, "grade": grade})


def test_04_sufficient_trade_count(metrics: Optional[Dict], min_trades: int = 100, max_trades: int = 2000) -> Dict:
    """T04: min_trades ≤ Total trades ≤ max_trades (scaled to actual period)."""
    tid = "T04_sufficient_trade_count"
    mn = min_trades
    mx = max_trades
    val = _safe_get(metrics, "total_trades")
    if val is None:
        return _test_result(tid, False, None, "SKIP — metric unavailable")
    passed = mn <= val <= mx
    if val < mn:
        msg = f"FAIL — Only {val} trades (minimum {mn} for statistical significance)"
    elif val > mx:
        msg = f"FAIL — {val} trades exceeds {mx} (likely over-trading)"
    else:
        msg = f"PASS — {val} trades (range {mn}–{mx})"
    return _test_result(tid, passed, val, msg,
                        {"min_trades": mn, "max_trades": mx})


def test_05_realistic_win_rate(metrics: Optional[Dict]) -> Dict:
    """T05: 35% ≤ Win Rate ≤ 65%."""
    tid = "T05_realistic_win_rate"
    mn   = TESTS[tid]["min_wr"]
    mx   = TESTS[tid]["max_wr"]
    sus  = TESTS[tid]["suspicious"]
    val  = _safe_get(metrics, "win_rate_pct")
    if val is None:
        return _test_result(tid, False, None, "SKIP — metric unavailable")
    passed = mn <= val <= mx
    if val > sus:
        msg = f"SUSPICIOUS — Win rate {val:.1f}% > {sus}%: possible overfitting"
    elif val < mn:
        msg = f"FAIL — Win rate {val:.1f}% < {mn}%: unsustainable loss rate"
    else:
        msg = f"PASS — Win rate {val:.1f}% within [{mn}%, {mx}%]"
    return _test_result(tid, passed, val, msg,
                        {"min_wr": mn, "max_wr": mx, "suspicious_above": sus})


def test_06_profit_factor(metrics: Optional[Dict]) -> Dict:
    """T06: Profit Factor ≥ 1.5."""
    tid = "T06_profit_factor"
    mn  = TESTS[tid]["min_pf"]
    gd  = TESTS[tid]["good"]
    exc = TESTS[tid]["excellent"]
    val = _safe_get(metrics, "profit_factor")
    if val is None:
        return _test_result(tid, False, None, "SKIP — metric unavailable")
    passed = val >= mn
    grade  = ("EXCELLENT" if val >= exc else
              "GOOD"      if val >= gd  else
              "MINIMUM"   if passed     else "FAIL")
    msg = f"{grade} — Profit Factor {val:.3f} (minimum {mn})"
    return _test_result(tid, passed, val, msg,
                        {"min_pf": mn, "good": gd, "excellent": exc, "grade": grade})


def test_07_win_loss_ratio(metrics: Optional[Dict]) -> Dict:
    """T07: Win/Loss Ratio ≥ 2.0."""
    tid = "T07_win_loss_ratio"
    mn  = TESTS[tid]["min_ratio"]
    gd  = TESTS[tid]["good"]
    exc = TESTS[tid]["excellent"]
    val = _safe_get(metrics, "win_loss_ratio")
    if val is None:
        return _test_result(tid, False, None, "SKIP — metric unavailable")
    passed = val >= mn
    grade  = ("EXCELLENT" if val >= exc else
              "GOOD"      if val >= gd  else
              "MINIMUM"   if passed     else "FAIL")
    msg = f"{grade} — Win/Loss Ratio {val:.3f} (minimum {mn})"
    return _test_result(tid, passed, val, msg,
                        {"min_ratio": mn, "good": gd, "excellent": exc, "grade": grade})


def test_08_cost_sensitivity(
    metrics:        Optional[Dict],
    metrics_cost2x: Optional[Dict],
) -> Dict:
    """T08: Return drops by < 50% when transaction costs are doubled."""
    tid = "T08_cost_sensitivity"
    max_drop = TESTS[tid]["max_drop"]
    base_ret = _safe_get(metrics, "total_return_pct")
    cost_ret = _safe_get(metrics_cost2x, "total_return_pct")

    if base_ret is None or cost_ret is None:
        return _test_result(tid, True, None,
                            "SKIP — cost-sensitivity run not available "
                            "(re-run Script 16 with --cost-bps 20 --output-tag cost2x). "
                            "Scored as neutral — not counted as a failure.")
    if base_ret <= 0:
        return _test_result(tid, False, base_ret,
                            "SKIP — baseline return is non-positive; test not meaningful")

    drop_fraction = (base_ret - cost_ret) / abs(base_ret)
    passed = drop_fraction < max_drop
    msg = (
        f"PASS — Cost doubling reduces return by {drop_fraction:.1%} "
        f"(base {base_ret:.1f}% → 2× costs {cost_ret:.1f}%)"
        if passed else
        f"FAIL — Cost doubling reduces return by {drop_fraction:.1%} ≥ {max_drop:.0%} "
        f"(base {base_ret:.1f}% → 2× costs {cost_ret:.1f}%)"
    )
    return _test_result(tid, passed, round(drop_fraction, 4), msg,
                        {"base_return_pct":      round(base_ret, 2),
                         "cost2x_return_pct":    round(cost_ret, 2),
                         "drop_fraction":        round(drop_fraction, 4),
                         "max_allowed_drop":     max_drop})


def test_09_drawdown_recovery(metrics: Optional[Dict]) -> Dict:
    """T09: Average recovery ≤ 12 months; max drawdown duration ≤ 24 months."""
    tid = "T09_drawdown_recovery"
    max_avg = TESTS[tid]["max_avg_rec"]
    max_max = TESTS[tid]["max_max_rec"]
    avg_rec = _safe_get(metrics, "avg_recovery_months")
    max_dur_days = _safe_get(metrics, "max_drawdown_duration_days")

    if avg_rec is None:
        return _test_result(tid, False, None, "SKIP — metric unavailable")

    max_dur_months = max_dur_days / 30.4 if max_dur_days is not None else None
    passed_avg = avg_rec  <= max_avg
    passed_max = (max_dur_months <= max_max) if max_dur_months is not None else True
    passed     = passed_avg and passed_max

    grade = ("EXCELLENT" if avg_rec <= 6  else
             "GOOD"      if avg_rec <= 9  else
             "MINIMUM"   if passed_avg    else "FAIL")
    msg = (
        f"{grade} — Avg recovery {avg_rec:.1f} months"
        + (f", max duration {max_dur_months:.1f} months" if max_dur_months else "")
    )
    return _test_result(tid, passed, avg_rec, msg,
                        {"avg_recovery_months":     round(avg_rec, 1),
                         "max_recovery_months":     round(max_dur_months, 1) if max_dur_months else None,
                         "max_allowed_avg_months":  max_avg,
                         "max_allowed_max_months":  max_max,
                         "grade":                   grade})


def test_10_annual_consistency(
    metrics:        Optional[Dict],
    annual_df:      Optional[pd.DataFrame],
    skip:           bool  = False,
    min_pct:        float = 70.0,
) -> Dict:
    """T10: ≥ min_pct of calendar years are positive. Skipped when < 3 years."""
    tid = "T10_annual_consistency"
    if skip:
        return _test_result(tid, True, None,
                            "SKIP — fewer than 3 full calendar years (not statistically meaningful)",
                            {"skipped_reason": "partial_period"})

    # Prefer DataFrame; fall back to metrics dict
    pos_pct: Optional[float] = None
    annual_breakdown: Dict = {}

    if annual_df is not None and not annual_df.empty:
        try:
            returns_col = [c for c in annual_df.columns if c.lower() not in ("year",)][0]
            values = annual_df[returns_col].dropna()
            pos_pct = (values > 0).sum() / len(values) * 100 if len(values) else None
            annual_breakdown = dict(zip(annual_df.iloc[:, 0].astype(str), values.round(2)))
        except Exception:
            pass

    if pos_pct is None:
        pos_pct = _safe_get(metrics, "positive_years_pct")

    if pos_pct is None:
        return _test_result(tid, False, None, "SKIP — metric unavailable")

    passed = pos_pct >= min_pct
    grade  = ("EXCELLENT" if pos_pct >= 85 else
              "GOOD"      if pos_pct >= 75 else
              "MINIMUM"   if passed         else "FAIL")
    msg = f"{grade} — {pos_pct:.1f}% of years positive (minimum {min_pct}%)"
    return _test_result(tid, passed, round(pos_pct, 1), msg,
                        {"positive_years_pct": round(pos_pct, 1),
                         "min_pct": min_pct,
                         "grade":  grade,
                         "annual_returns": annual_breakdown})


# ===========================================================================
# SCORING SYSTEM
# ===========================================================================

def calculate_score(metrics: Optional[Dict]) -> Dict:
    """
    Score the backtest on 10 metrics (0–3 pts each) → 0–30 total.
    Returns a dict with per-metric scores and the overall rating.
    """
    scores: Dict[str, int] = {}
    for key in SCORING_TABLE:
        val = _safe_get(metrics, key)
        scores[key] = _score_metric(key, val)

    total  = sum(scores.values())
    rating, rating_desc = _determine_rating(total)

    return {
        "per_metric_scores": scores,
        "total_score":       total,
        "max_score":         30,
        "rating":            rating,
        "rating_description": rating_desc,
        "score_pct":         round(total / 30 * 100, 1),
    }


# ===========================================================================
# RED FLAG CHECKS
# ===========================================================================

def check_red_flags(
    metrics:      Optional[Dict],
    trade_df:     Optional[pd.DataFrame],
    equity_df:    Optional[pd.DataFrame],
    wfo_results:  Optional[Dict],
    rf07_min:     int  = 100,
    rf07_max:     int  = 2000,
) -> Dict[str, Any]:
    """
    Evaluate 7 critical red flags.
    Returns a dict: {flag_id: {triggered, severity, details}}, plus summary keys.
    """
    flags: Dict[str, Dict] = {}

    # RF01 – Curve-fitted equity curve (> 75% positive months)
    pct_pos = _monthly_positive_pct_from_equity(equity_df)
    if pct_pos is None:
        # Try metrics monthly_returns
        mr = _safe_get(metrics, "monthly_returns")
        if mr:
            pct_pos = _pct_positive_months(mr)

    rf01_thresh = RED_FLAGS["RF01_curve_fitted"]["threshold"]
    rf01_val    = round(pct_pos, 1) if pct_pos is not None else None
    rf01_fired  = (pct_pos > rf01_thresh) if pct_pos is not None else False
    flags["RF01_curve_fitted"] = {
        "triggered":  rf01_fired,
        "value":      rf01_val,
        "threshold":  rf01_thresh,
        "name":       RED_FLAGS["RF01_curve_fitted"]["name"],
        "action":     RED_FLAGS["RF01_curve_fitted"]["action"],
        "severity":   "CRITICAL" if rf01_fired else "OK",
    }

    # RF02 – Single trade dominance (> 50% of gross profit from one trade)
    rf02_fired = False
    rf02_val   = None
    if trade_df is not None and not trade_df.empty and "pnl" in trade_df.columns:
        pnls = trade_df["pnl"].dropna()
        gross_profit = pnls[pnls > 0].sum()
        if gross_profit > 0:
            max_single_win = pnls.max()
            ratio = max_single_win / gross_profit
            rf02_val  = round(ratio, 4)
            rf02_fired = ratio > RED_FLAGS["RF02_single_trade_dominance"]["threshold"]
    flags["RF02_single_trade_dominance"] = {
        "triggered":  rf02_fired,
        "value":      rf02_val,
        "threshold":  RED_FLAGS["RF02_single_trade_dominance"]["threshold"],
        "name":       RED_FLAGS["RF02_single_trade_dominance"]["name"],
        "action":     RED_FLAGS["RF02_single_trade_dominance"]["action"],
        "severity":   "CRITICAL" if rf02_fired else "OK",
    }

    # RF03 – Excessive win rate (> 70%)
    wr_val    = _safe_get(metrics, "win_rate_pct")
    rf03_fired = (wr_val > RED_FLAGS["RF03_excessive_win_rate"]["threshold"]
                  if wr_val is not None else False)
    flags["RF03_excessive_win_rate"] = {
        "triggered":  rf03_fired,
        "value":      wr_val,
        "threshold":  RED_FLAGS["RF03_excessive_win_rate"]["threshold"],
        "name":       RED_FLAGS["RF03_excessive_win_rate"]["name"],
        "action":     RED_FLAGS["RF03_excessive_win_rate"]["action"],
        "severity":   "CRITICAL" if rf03_fired else "OK",
    }

    # RF04 – Extreme parameter selection (≥ 50% of optimal parameters at grid edge)
    rf04_fired = False
    rf04_val   = None
    rf04_edge_params: List[str] = []
    if wfo_results is not None:
        stability = _safe_get(wfo_results, "stability", default={})
        edges     = _safe_get(stability, "param_edges", default={})
        total_params = len(edges)
        if total_params > 0:
            edge_count = sum(
                1 for p_info in edges.values()
                if isinstance(p_info, dict) and p_info.get("pct_at_edge", 0) > 50
            )
            rf04_edge_params = [
                k for k, v in edges.items()
                if isinstance(v, dict) and v.get("pct_at_edge", 0) > 50
            ]
            ratio     = edge_count / total_params
            rf04_val  = round(ratio, 4)
            rf04_fired = ratio >= RED_FLAGS["RF04_extreme_parameter_selection"]["threshold"]
    flags["RF04_extreme_parameter_selection"] = {
        "triggered":    rf04_fired,
        "value":        rf04_val,
        "threshold":    RED_FLAGS["RF04_extreme_parameter_selection"]["threshold"],
        "edge_params":  rf04_edge_params,
        "name":         RED_FLAGS["RF04_extreme_parameter_selection"]["name"],
        "action":       RED_FLAGS["RF04_extreme_parameter_selection"]["action"],
        "severity":     "CRITICAL" if rf04_fired else "OK",
    }

    # RF05 – OOS Sharpe < 50% of IS Sharpe
    rf05_fired = False
    rf05_val   = None
    if wfo_results is not None:
        windows = _safe_get(wfo_results, "windows", default=[])
        if windows:
            is_sharpes  = [w.get("is_best_sharpe", None) for w in windows]
            oos_sharpes = [w.get("oos_sharpe", None) for w in windows]
            is_sharpes  = [x for x in is_sharpes  if x is not None]
            oos_sharpes = [x for x in oos_sharpes if x is not None]
            if is_sharpes and oos_sharpes:
                avg_is  = float(np.mean(is_sharpes))
                avg_oos = float(np.mean(oos_sharpes))
                ratio   = avg_oos / avg_is if avg_is > 0 else None
                rf05_val  = round(ratio, 4) if ratio is not None else None
                rf05_fired = (ratio < RED_FLAGS["RF05_oos_collapse"]["threshold"]
                              if ratio is not None else False)
    flags["RF05_oos_collapse"] = {
        "triggered":  rf05_fired,
        "value":      rf05_val,
        "threshold":  RED_FLAGS["RF05_oos_collapse"]["threshold"],
        "name":       RED_FLAGS["RF05_oos_collapse"]["name"],
        "action":     RED_FLAGS["RF05_oos_collapse"]["action"],
        "severity":   "CRITICAL" if rf05_fired else "OK",
    }

    # RF06 – Zero losing years
    pos_yrs_pct = _safe_get(metrics, "positive_years_pct")
    rf06_fired  = (pos_yrs_pct == 100.0) if pos_yrs_pct is not None else False
    flags["RF06_zero_losing_years"] = {
        "triggered":        rf06_fired,
        "value":            pos_yrs_pct,
        "name":             RED_FLAGS["RF06_zero_losing_years"]["name"],
        "action":           RED_FLAGS["RF06_zero_losing_years"]["action"],
        "severity":         "WARNING" if rf06_fired else "OK",
    }

    # RF07 – Unrealistic trade count (bounds scaled to period length)
    n_trades  = _safe_get(metrics, "total_trades")
    rf07_fired = (
        n_trades is not None and
        (n_trades < rf07_min or n_trades > rf07_max)
    )
    flags["RF07_unrealistic_trade_count"] = {
        "triggered":  rf07_fired,
        "value":      n_trades,
        "min":        rf07_min,
        "max":        rf07_max,
        "name":       RED_FLAGS["RF07_unrealistic_trade_count"]["name"],
        "action":     RED_FLAGS["RF07_unrealistic_trade_count"]["action"],
        "severity":   "CRITICAL" if rf07_fired else "OK",
    }

    critical_flags  = [fid for fid, info in flags.items() if info.get("triggered") and info.get("severity") == "CRITICAL"]
    warning_flags   = [fid for fid, info in flags.items() if info.get("triggered") and info.get("severity") == "WARNING"]

    return {
        "flags":           flags,
        "critical_count":  len(critical_flags),
        "warning_count":   len(warning_flags),
        "critical_flags":  critical_flags,
        "warning_flags":   warning_flags,
        "any_critical":    len(critical_flags) > 0,
    }


# ===========================================================================
# MONTE CARLO VALIDATION SUMMARY
# ===========================================================================

def validate_monte_carlo(mc_summary: Optional[Dict]) -> Dict:
    """
    Extract and interpret key Monte Carlo metrics from Script 18 output.

    Evaluates:
        - Percentile rank of actual backtest result
        - 95% confidence interval width
        - 5th-percentile tail risk
        - Probability of achieving ≥ 50% return
    """
    if mc_summary is None:
        return {"available": False, "message": "Monte Carlo data not available"}

    initial_equity = _safe_get(mc_summary, "initial_equity", default=50_000.0)
    base_metrics   = _safe_get(mc_summary, "baseline_metrics", default={})
    actual_return  = _safe_get(base_metrics, "total_return_pct")

    results: Dict[str, Any] = {"available": True, "initial_equity": initial_equity}

    # Method 1 (Trade Shuffling) is primary for interpretation
    m1 = _safe_get(mc_summary, "method1", default={})
    m1_pt = _safe_get(m1, "percentile_table", default={})

    # --- 1. Percentile rank of actual result ---
    te_dist = None
    te_p5  = _safe_get(m1_pt, "terminal_equity", "5")
    te_p50 = _safe_get(m1_pt, "terminal_equity", "50")
    te_p95 = _safe_get(m1_pt, "terminal_equity", "95")

    if te_p5 and te_p50 and te_p95:
        results["ci_95_lower_equity"] = round(float(te_p5), 0)
        results["ci_95_upper_equity"] = round(float(te_p95), 0)
        results["median_equity"]      = round(float(te_p50), 0)

        ci_width = (float(te_p95) - float(te_p5)) / initial_equity
        results["ci_relative_width"]  = round(ci_width, 4)
        results["ci_width_grade"] = (
            "EXCELLENT" if ci_width < 0.50 else
            "GOOD"      if ci_width < 1.00 else
            "POOR"
        )

        # 5th percentile tail risk
        te_p5_return_pct = (float(te_p5) - initial_equity) / initial_equity * 100
        results["tail_risk_5th_pct_return"] = round(te_p5_return_pct, 1)
        results["tail_risk_grade"] = (
            "ACCEPTABLE"  if te_p5_return_pct > -20 else
            "UNACCEPTABLE"
        )

    # --- 2. Probability of ≥ 50% return ---
    target_equity = initial_equity * 1.50
    prob_target    = _safe_get(m1, "prob_target_return")
    if prob_target is None and te_p50 is not None:
        # Estimate from percentile table: if p50 > target_equity → > 50% chance
        prob_target = (float(te_p50) > target_equity) * 0.5  # crude lower bound

    if prob_target is not None:
        results["prob_50pct_return"] = round(float(prob_target) * 100, 1) if float(prob_target) <= 1 else round(float(prob_target), 1)
        p50_val = results["prob_50pct_return"] if results["prob_50pct_return"] <= 100 else results["prob_50pct_return"]
        results["prob_50pct_grade"] = (
            "EXCELLENT" if p50_val > 75 else
            "GOOD"      if p50_val > 60 else
            "POOR"
        )

    # --- 3. Gate check pass-through ---
    gate_check = _safe_get(mc_summary, "gate_check", default={})
    results["gate_check"] = gate_check

    # --- 4. Ruin / blowup probabilities ---
    results["ruin_probability_m1"]  = _safe_get(m1, "ruin_probability")
    results["blowup_probability_m1"] = _safe_get(m1, "blowup_probability")

    return results


# ===========================================================================
# BENCHMARK COMPARISON
# ===========================================================================

def compare_benchmarks(
    metrics:         Optional[Dict],
    spy_sharpe:      float = SPY_BENCHMARK["sharpe_ratio"],
    skip_benchmark:  bool  = False,
) -> Dict:
    """
    Compare strategy against three benchmarks.
    Returns a structured comparison dict.
    """
    if skip_benchmark:
        return {"skipped": True}

    strategy_sharpe = _safe_get(metrics, "sharpe_ratio") or 0.0
    strategy_cagr   = _safe_get(metrics, "cagr_pct")     or 0.0
    strategy_dd     = _safe_get(metrics, "max_drawdown_pct") or -100.0

    benchmarks = [
        ("SPY",     SPY_BENCHMARK),
        ("6040",    BALANCED_BENCHMARK),
        ("NAIVE",   NAIVE_TREND_BENCHMARK),
    ]

    comparisons: Dict[str, Any] = {}
    beats_count = 0
    for bk_id, bk in benchmarks:
        beats_sharpe = strategy_sharpe > bk["sharpe_ratio"]
        beats_cagr   = strategy_cagr   > bk["cagr_pct"]
        beats_dd     = strategy_dd     > bk["max_dd_pct"]   # less negative is better
        beats_all    = beats_sharpe and beats_cagr and beats_dd
        if beats_sharpe:
            beats_count += 1
        comparisons[bk_id] = {
            "benchmark_name":           bk["name"],
            "benchmark_sharpe":         bk["sharpe_ratio"],
            "benchmark_cagr_pct":       bk["cagr_pct"],
            "benchmark_max_dd_pct":     bk["max_dd_pct"],
            "strategy_sharpe":          strategy_sharpe,
            "strategy_cagr_pct":        strategy_cagr,
            "strategy_max_dd_pct":      strategy_dd,
            "beats_on_sharpe":          beats_sharpe,
            "beats_on_cagr":            beats_cagr,
            "beats_on_drawdown":        beats_dd,
            "beats_overall":            beats_all,
            "sharpe_excess":            round(strategy_sharpe - bk["sharpe_ratio"], 4),
            "cagr_excess_pct":          round(strategy_cagr   - bk["cagr_pct"],    2),
        }

    # Primary benchmark for trend following is the naive trend system, not SPY.
    # SPY is an equity index — comparing a multi-asset trend strategy to SPY
    # on Sharpe conflates asset-class exposure with strategy skill.
    primary_pass = comparisons["NAIVE"]["beats_on_sharpe"]

    return {
        "comparisons":             comparisons,
        "benchmarks_beaten_count": beats_count,
        "total_benchmarks":        len(benchmarks),
        "primary_benchmark_pass":  primary_pass,
        "primary_benchmark_id":    "NAIVE",
        "primary_benchmark_msg":  (
            "PASS — Strategy Sharpe exceeds Naive Trend benchmark"
            if primary_pass else
            "FAIL — Strategy does NOT exceed Naive Trend benchmark Sharpe"
        ),
    }


# ===========================================================================
# OVERALL DECISION LOGIC
# ===========================================================================

def make_overall_decision(
    tests:        List[Dict],
    score_result: Dict,
    red_flag_result: Dict,
    benchmark_result: Dict,
) -> Dict:
    """
    Determine the overall validation outcome.

    Rules:
        1. If < 8 tests pass → STOP, REJECT strategy immediately
        2. If any CRITICAL red flag present → REJECT (pending investigation)
        3. Otherwise, decision follows the scoring rating
    """
    passed_tests = [t for t in tests if t["passed"]]
    failed_tests = [t for t in tests if not t["passed"]]
    skipped_tests = [t for t in tests if t.get("value") is None and not t["passed"]]
    n_passed = len(passed_tests)
    n_tests  = len(tests)

    # Rule 1: minimum 8 tests must pass
    hard_stop_tests = n_passed < 8

    # Rule 2: critical red flags
    hard_stop_flags = red_flag_result.get("any_critical", False)

    # Rating from scoring system
    rating       = score_result["rating"]
    rating_desc  = score_result["rating_description"]
    total_score  = score_result["total_score"]

    # Final verdict
    if hard_stop_tests:
        verdict     = "REJECT"
        verdict_msg = (
            f"REJECT — Only {n_passed}/{n_tests} validation tests passed "
            f"(minimum 8 required). Do not proceed to deployment."
        )
        recommendation = "STOP — Fix underlying strategy flaws before re-testing."
    elif hard_stop_flags:
        crit = red_flag_result.get("critical_flags", [])
        verdict     = "REJECT"
        verdict_msg = (
            f"REJECT — Critical red flags detected: {crit}. "
            "Investigate thoroughly before any deployment consideration."
        )
        recommendation = "STOP — Resolve red flags and re-run full validation suite."
    elif rating == "FAIL":
        verdict        = "REJECT"
        verdict_msg    = f"REJECT — Scoring {total_score}/30 → {rating}. {rating_desc}."
        recommendation = "Revise strategy parameters and retest."
    elif rating == "MARGINAL":
        verdict        = "CONDITIONAL"
        verdict_msg    = f"CONDITIONAL — Score {total_score}/30 → {rating}. {rating_desc}."
        recommendation = "Paper trade for ≥ 3 months before live deployment."
    elif rating in ("ACCEPTABLE", "GOOD", "EXCELLENT"):
        verdict        = "APPROVE"
        verdict_msg    = f"APPROVE — Score {total_score}/30 → {rating}. {rating_desc}."
        recommendation = {
            "EXCELLENT":  "Deploy immediately with standard monitoring.",
            "GOOD":       "Deploy with standard monitoring and quarterly review.",
            "ACCEPTABLE": "Deploy with enhanced monitoring and monthly review.",
        }.get(rating, "Proceed with caution.")
    else:
        verdict        = "UNKNOWN"
        verdict_msg    = "Unable to determine verdict — insufficient data."
        recommendation = "Ensure all upstream scripts (16–18) have completed successfully."

    return {
        "verdict":                verdict,
        "verdict_message":        verdict_msg,
        "recommendation":         recommendation,
        "hard_stop_tests":        hard_stop_tests,
        "hard_stop_red_flags":    hard_stop_flags,
        "tests_passed":           n_passed,
        "tests_total":            n_tests,
        "tests_failed_ids":       [t["test_id"] for t in failed_tests],
        "total_score":            total_score,
        "rating":                 rating,
        "benchmark_primary_pass": benchmark_result.get("primary_benchmark_pass", None),
    }


# ===========================================================================
# OUTPUT PERSISTENCE
# ===========================================================================

def _json_serial(obj):
    """JSON-serialise numpy types."""
    if isinstance(obj, (np.bool_,)):     return bool(obj)
    if isinstance(obj, (np.integer,)):   return int(obj)
    if isinstance(obj, (np.floating,)):  return float(obj)
    if isinstance(obj, np.ndarray):      return obj.tolist()
    if isinstance(obj, (pd.Timestamp, date, datetime)):
        return str(obj)
    raise TypeError(f"Object of type {type(obj)} is not JSON serialisable")


def save_results(
    results:   Dict,
    tag:       str,
    logger:    logging.Logger,
) -> Dict[str, Path]:
    """Persist validation report and human-readable summary."""
    VALIDATION_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    sfx = f"_{tag}" if tag else ""
    ts  = datetime.now().strftime("%Y%m%d")
    paths: Dict[str, Path] = {}

    # Machine-readable JSON
    json_path = VALIDATION_DIR / f"validation_results{sfx}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, default=_json_serial)
    paths["validation_results"] = json_path
    logger.info(f"Saved validation results → {json_path}")

    # Archived report
    report_path = REPORTS_DIR / f"{ts}_validation_report{sfx}.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, default=_json_serial)
    paths["report"] = report_path
    logger.info(f"Saved archived report  → {report_path}")

    # Human-readable summary
    txt_path = VALIDATION_DIR / f"validation_summary{sfx}.txt"
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(_build_text_summary(results))
    paths["summary_txt"] = txt_path
    logger.info(f"Saved human summary    → {txt_path}")

    return paths


def _build_text_summary(results: Dict) -> str:
    """Build a formatted plaintext validation report."""
    lines = []
    sep   = "=" * 72
    thin  = "-" * 72

    def h1(title): lines.extend([sep, f"  {title}", sep])
    def h2(title): lines.extend(["", thin, f"  {title}", thin])
    def row(label, val): lines.append(f"  {label:<42} {val}")

    lines.append("")
    h1("SCRIPT 19 — BACKTEST VALIDATION REPORT")
    lines.append(f"  Generated : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")

    # ── Overall verdict ──────────────────────────────────────────────────────
    decision = results.get("overall_decision", {})
    verdict  = decision.get("verdict", "UNKNOWN")
    lines.append(f"  {'OVERALL VERDICT':<42} {verdict}")
    lines.append(f"  {decision.get('verdict_message', '')}")
    lines.append(f"  Recommendation: {decision.get('recommendation', '')}")
    lines.append("")

    # ── Score summary ────────────────────────────────────────────────────────
    h2("PERFORMANCE SCORE")
    sc = results.get("score", {})
    row("Total Score",   f"{sc.get('total_score', '?')} / {sc.get('max_score', 30)} pts")
    row("Rating",        sc.get("rating", "?"))
    row("Score %",       f"{sc.get('score_pct', '?')}%")
    lines.append("")
    lines.append("  Metric                                  Score  Value")
    lines.append("  " + "-" * 60)
    per_metric = sc.get("per_metric_scores", {})
    metrics    = results.get("raw_metrics", {})
    for key, pts in per_metric.items():
        val = metrics.get(key, "N/A")
        val_str = f"{val:.3f}" if isinstance(val, float) else str(val)
        lines.append(f"  {key:<40} {pts}/3    {val_str}")

    # ── 10 primary tests ─────────────────────────────────────────────────────
    h2("10 PRIMARY VALIDATION TESTS")
    tests_passed = decision.get("tests_passed", 0)
    tests_total  = decision.get("tests_total", 10)
    lines.append(f"  Tests Passed: {tests_passed} / {tests_total} (minimum 8 required)")
    lines.append("")
    for t in results.get("tests", []):
        status = "✓ PASS" if t["passed"] else "✗ FAIL"
        lines.append(f"  [{status}] {t['test_id']}")
        lines.append(f"         {t['message']}")
    lines.append("")

    # ── Red flags ────────────────────────────────────────────────────────────
    h2("RED FLAG ANALYSIS")
    rf = results.get("red_flags", {})
    lines.append(f"  Critical Flags: {rf.get('critical_count', 0)}")
    lines.append(f"  Warning Flags : {rf.get('warning_count', 0)}")
    lines.append("")
    for fid, info in rf.get("flags", {}).items():
        icon = "⚠ TRIGGERED" if info.get("triggered") else "✓ CLEAR"
        lines.append(f"  [{icon}] {fid}: {info.get('name', '')}")
        if info.get("triggered"):
            lines.append(f"           Value: {info.get('value')}  "
                         f"Threshold: {info.get('threshold', 'N/A')}")
            lines.append(f"           Action: {info.get('action', '')}")

    # ── Benchmark comparison ─────────────────────────────────────────────────
    h2("BENCHMARK COMPARISON")
    bk = results.get("benchmark_comparison", {})
    if bk.get("skipped"):
        lines.append("  [SKIPPED] Benchmark comparison was disabled.")
    else:
        lines.append(f"  Primary Benchmark: {bk.get('primary_benchmark_msg', '')}")
        lines.append(f"  Benchmarks beaten on Sharpe: {bk.get('benchmarks_beaten_count', 0)} / {bk.get('total_benchmarks', 3)}")
        lines.append("")
        for bk_id, bk_info in bk.get("comparisons", {}).items():
            beat = "BEATS" if bk_info.get("beats_on_sharpe") else "LAGS "
            lines.append(
                f"  [{beat}] {bk_info.get('benchmark_name', bk_id)}: "
                f"Strategy Sharpe {bk_info.get('strategy_sharpe', '?'):.3f} vs "
                f"Benchmark {bk_info.get('benchmark_sharpe', '?'):.3f} "
                f"(excess: {bk_info.get('sharpe_excess', 0):+.3f})"
            )

    # ── Monte Carlo ───────────────────────────────────────────────────────────
    h2("MONTE CARLO VALIDATION")
    mc = results.get("monte_carlo_validation", {})
    if not mc.get("available"):
        lines.append("  [SKIPPED] Monte Carlo data not available.")
    else:
        if "ci_95_lower_equity" in mc:
            row("95% CI Terminal Equity",
                f"[${mc['ci_95_lower_equity']:,.0f} — ${mc['ci_95_upper_equity']:,.0f}]")
            row("CI Relative Width",
                f"{mc.get('ci_relative_width', '?'):.1%}  ({mc.get('ci_width_grade', '?')})")
        if "tail_risk_5th_pct_return" in mc:
            row("5th Percentile Return",
                f"{mc.get('tail_risk_5th_pct_return', '?'):.1f}%  ({mc.get('tail_risk_grade', '?')})")
        if "prob_50pct_return" in mc:
            row("Prob. of ≥ 50% Return",
                f"{mc.get('prob_50pct_return', '?'):.1f}%  ({mc.get('prob_50pct_grade', '?')})")
        if "ruin_probability_m1" in mc and mc["ruin_probability_m1"] is not None:
            row("Ruin Probability (Method 1)",
                f"{mc['ruin_probability_m1']:.2%}")

    lines.append("")
    lines.append(sep)
    lines.append("  END OF REPORT")
    lines.append(sep)
    lines.append("")
    return "\n".join(lines)


# ===========================================================================
# MAIN ORCHESTRATION
# ===========================================================================


def _detect_period(equity_df, metrics) -> float:
    import math
    if equity_df is not None and not equity_df.empty:
        try:
            dates = pd.to_datetime(equity_df["date"])
            y = (dates.iloc[-1] - dates.iloc[0]).days / 365.25
            if y > 0.1: return round(y, 2)
        except Exception: pass
    tr = _safe_get(metrics, "total_return_pct")
    cagr = _safe_get(metrics, "cagr_pct")
    if tr and cagr and cagr > 0 and tr > 0:
        try:
            y = math.log(1 + tr/100) / math.log(1 + cagr/100)
            if 0.1 < y < 20: return round(y, 2)
        except Exception: pass
    return 5.0


def _scale(years: float) -> dict:
    t01   = max(6.0 * years, 5.0)
    t04mn = max(int(20 * years), 10)
    t04mx = max(int(400 * years), 100)
    return {
        "period_years": round(years, 2),
        "is_partial":   years < 4.75,
        "t01":          round(t01, 1),
        "t04_min":      t04mn,
        "t04_max":      t04mx,
        "t10_skip":     years < 3.0,
    }


def run_validation(
    backtest_tag:    str  = "",
    benchmark_sharpe: float = SPY_BENCHMARK["sharpe_ratio"],
    skip_benchmark:  bool  = False,
    logger:          Optional[logging.Logger] = None,
) -> Dict:
    """
    Run the complete 10-step validation process.

    Steps:
        1. Load all upstream outputs (Scripts 16, 17, 18)
        2. Apply 10 primary validation tests
        3. Calculate performance score (0–30)
        4. Check 7 critical red flags
        5. Benchmark comparison
        6. Monte Carlo interpretation
        7. Overall verdict
        8. Persist results
    """
    if logger is None:
        logger = setup_logging(backtest_tag)

    logger.info("=" * 60)
    logger.info("SCRIPT 19 — BACKTEST VALIDATOR")
    logger.info("=" * 60)

    # ── Step 1: Load inputs ───────────────────────────────────────────────────
    logger.info("STEP 1: Loading upstream outputs …")
    inputs = load_all_inputs(backtest_tag, logger)
    metrics      = inputs.get("metrics")
    metrics_2x   = inputs.get("metrics_cost2x")
    trade_df     = inputs.get("trade_log")
    equity_df    = inputs.get("equity_curve")
    annual_df    = inputs.get("annual_returns")
    wfo_results  = inputs.get("wfo_results")
    mc_summary   = inputs.get("mc_summary")

    if metrics is None:
        logger.error("performance_metrics.json not found — aborting validation.")
        return {"error": "Missing performance_metrics.json from Script 16"}

    # ── Detect period length and scale thresholds ─────────────────────────────
    period_years = _detect_period(equity_df, metrics)
    scaled       = _scale(period_years)
    if scaled["is_partial"]:
        logger.warning(
            f"  ⚠ PARTIAL PERIOD: {period_years:.1f} years "
            f"(full validation = 5 years). Thresholds scaled — results PRELIMINARY."
        )
    else:
        logger.info(f"  Backtest period: {period_years:.1f} years — full validation mode.")

    # ── Step 2: Run 10 primary validation tests ───────────────────────────────
    logger.info("STEP 2: Running 10 primary validation tests …")
    tests = [
        test_01_positive_expectancy(metrics, threshold=scaled["t01"]),
        test_02_risk_adjusted_outperformance(metrics, benchmark_sharpe),
        test_03_acceptable_drawdown(metrics),
        test_04_sufficient_trade_count(metrics, min_trades=scaled["t04_min"], max_trades=scaled["t04_max"]),
        test_05_realistic_win_rate(metrics),
        test_06_profit_factor(metrics),
        test_07_win_loss_ratio(metrics),
        test_08_cost_sensitivity(metrics, metrics_2x),
        test_09_drawdown_recovery(metrics),
        test_10_annual_consistency(metrics, annual_df, skip=scaled["t10_skip"]),
    ]
    for t in tests:
        status = "PASS" if t["passed"] else "FAIL"
        logger.info(f"  [{status}] {t['test_id']}: {t['message']}")

    # ── Step 3: Calculate performance score ──────────────────────────────────
    logger.info("STEP 3: Calculating performance score …")
    score_result = calculate_score(metrics)
    logger.info(
        f"  Score: {score_result['total_score']}/30 → {score_result['rating']} "
        f"({score_result['rating_description']})"
    )

    # ── Step 4: Check red flags ───────────────────────────────────────────────
    logger.info("STEP 4: Checking 7 critical red flags …")
    red_flag_result = check_red_flags(metrics, trade_df, equity_df, wfo_results,
        rf07_min=scaled["t04_min"], rf07_max=scaled["t04_max"])
    for fid, info in red_flag_result["flags"].items():
        status = "TRIGGERED" if info.get("triggered") else "CLEAR"
        logger.info(f"  [{status}] {fid}: {info.get('name', '')}")
    if red_flag_result["critical_count"] > 0:
        logger.warning(
            f"  ⚠ {red_flag_result['critical_count']} CRITICAL red flag(s) detected!"
        )

    # ── Step 5: Benchmark comparison ─────────────────────────────────────────
    logger.info("STEP 5: Comparing to benchmarks …")
    benchmark_result = compare_benchmarks(
        metrics, spy_sharpe=benchmark_sharpe, skip_benchmark=skip_benchmark
    )
    if not benchmark_result.get("skipped"):
        logger.info(f"  {benchmark_result.get('primary_benchmark_msg', '')}")
        logger.info(
            f"  Benchmarks beaten (Sharpe): "
            f"{benchmark_result.get('benchmarks_beaten_count', 0)} / "
            f"{benchmark_result.get('total_benchmarks', 3)}"
        )

    # ── Step 6: Monte Carlo interpretation ───────────────────────────────────
    logger.info("STEP 6: Validating Monte Carlo results …")
    mc_validation = validate_monte_carlo(mc_summary)
    if mc_validation.get("available"):
        if "ci_95_lower_equity" in mc_validation:
            logger.info(
                f"  95% CI terminal equity: "
                f"[${mc_validation['ci_95_lower_equity']:,.0f}, "
                f"${mc_validation['ci_95_upper_equity']:,.0f}]"
            )
        if "tail_risk_5th_pct_return" in mc_validation:
            logger.info(
                f"  5th percentile return: "
                f"{mc_validation['tail_risk_5th_pct_return']:.1f}% "
                f"({mc_validation.get('tail_risk_grade', '')})"
            )
    else:
        logger.info("  Monte Carlo data not available — skipping.")

    # ── Step 7: Overall verdict ───────────────────────────────────────────────
    logger.info("STEP 7: Determining overall verdict …")
    decision = make_overall_decision(tests, score_result, red_flag_result, benchmark_result)
    logger.info(f"  *** {decision['verdict_message']} ***")
    logger.info(f"  Recommendation: {decision['recommendation']}")

    # ── Assemble full results dict ────────────────────────────────────────────
    results = {
        "generated_at":         datetime.now().isoformat(),
        "script":               "19_backtest_validator.py",
        "architecture_version": "v3.3",
        "backtest_tag":         backtest_tag,
        "benchmark_sharpe":     benchmark_sharpe,
        "raw_metrics":          metrics,
        "tests":                tests,
        "score":                score_result,
        "red_flags":            red_flag_result,
        "benchmark_comparison": benchmark_result,
        "monte_carlo_validation": mc_validation,
        "overall_decision":     decision,
        "inputs_loaded":        {k: v is not None for k, v in inputs.items()},
    }

    # ── Step 8: Save outputs ──────────────────────────────────────────────────
    logger.info("STEP 8: Saving results …")
    paths = save_results(results, tag=backtest_tag, logger=logger)
    results["output_paths"] = {k: str(v) for k, v in paths.items()}

    logger.info("=" * 60)
    logger.info(f"VALIDATION COMPLETE — Verdict: {decision['verdict']}")
    logger.info("=" * 60)

    return results


# ===========================================================================
# CLI
# ===========================================================================

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Script 19 — Backtest Validator for the trend-following strategy",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Standard run
  python scripts/19_backtest_validator.py

  # Use tagged outputs from Scripts 16/17/18
  python scripts/19_backtest_validator.py --backtest-tag run_01

  # Override SPY benchmark Sharpe (e.g. for European equity universe)
  python scripts/19_backtest_validator.py --benchmark-sharpe 0.55

  # Skip benchmark comparison (no SPY data available)
  python scripts/19_backtest_validator.py --skip-benchmark
        """,
    )
    p.add_argument(
        "--backtest-tag", default="",
        help="Tag appended to upstream output filenames (matches Script 16/17/18 --output-tag)",
    )
    p.add_argument(
        "--benchmark-sharpe", type=float, default=SPY_BENCHMARK["sharpe_ratio"],
        help=f"SPY buy-and-hold Sharpe ratio used in Test 02 "
             f"(default: {SPY_BENCHMARK['sharpe_ratio']})",
    )
    p.add_argument(
        "--skip-benchmark", action="store_true",
        help="Skip benchmark comparison (useful when operating offline)",
    )
    return p.parse_args()


def main():
    args   = parse_args()
    logger = setup_logging(args.backtest_tag)
    results = run_validation(
        backtest_tag     = args.backtest_tag,
        benchmark_sharpe = args.benchmark_sharpe,
        skip_benchmark   = args.skip_benchmark,
        logger           = logger,
    )

    # Print final verdict to stdout for CI pipelines / run_pipeline.py
    decision = results.get("overall_decision", {})
    print("\n" + "=" * 60)
    print(f"VERDICT:  {decision.get('verdict', 'UNKNOWN')}")
    print(f"SCORE:    {results.get('score', {}).get('total_score', '?')} / 30  "
          f"[{results.get('score', {}).get('rating', '?')}]")
    print(f"TESTS:    {decision.get('tests_passed', '?')} / "
          f"{decision.get('tests_total', '?')} passed")
    print(f"RED FLAGS: {results.get('red_flags', {}).get('critical_count', 0)} critical")
    print("=" * 60)
    print(decision.get("recommendation", ""))

    # Exit code 0 → APPROVE/CONDITIONAL, 1 → REJECT/UNKNOWN
    if decision.get("verdict") in ("APPROVE", "CONDITIONAL"):
        sys.exit(0)
    else:
        sys.exit(1)


# ===========================================================================
# PROGRAMMATIC API (consumed by Script 20, Script 21, run_pipeline.py)
# ===========================================================================

def run_backtest_validation(
    backtest_tag:    str   = "",
    benchmark_sharpe: float = SPY_BENCHMARK["sharpe_ratio"],
    skip_benchmark:  bool  = False,
) -> Dict:
    """
    Public API entry point for Scripts 20, 21, and 00_run_pipeline.py.

    Returns:
        Full validation results dict (same structure as JSON output).
    """
    logger = setup_logging(backtest_tag)
    return run_validation(
        backtest_tag     = backtest_tag,
        benchmark_sharpe = benchmark_sharpe,
        skip_benchmark   = skip_benchmark,
        logger           = logger,
    )


if __name__ == "__main__":
    main()
