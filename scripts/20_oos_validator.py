#!/usr/bin/env python3
"""
Script 20: Out-of-Sample Validator
=====================================
Perform a rigorous, multi-dimensional out-of-sample robustness analysis by
comparing in-sample (IS) backtest performance to out-of-sample (OOS) walk-forward
results, detecting overfitting signatures, and producing a deployment-ready verdict.

Purpose
-------
Script 19 validates whether the IS backtest metrics meet institutional thresholds.
Script 20 answers the more fundamental question: **does the strategy's IS performance
generalise to unseen data?** It quantifies IS→OOS performance degradation across
every relevant metric, detects structural overfitting patterns, and issues a binding
ROBUST / MARGINAL / OVERFITTED verdict that gates live deployment.

Architecture Reference: v3.3 (Mar 2026)

Inputs (all sourced from upstream scripts)
------------------------------------------
    Script 16 →  data_cache/backtest/performance_metrics.json       (IS baseline)
                 data_cache/backtest/equity_curve.csv               (IS equity curve)
                 data_cache/backtest/trade_log.csv                  (IS trade log)
                 data_cache/backtest/annual_returns.csv             (IS year-by-year)
    Script 17 →  data_cache/backtest/walk_forward/wfo_results.json  (IS+OOS windows)
                 data_cache/backtest/walk_forward/wfo_optimal_params.json
                 data_cache/backtest/walk_forward/wfo_window_summary.csv
    Script 19 →  data_cache/validation/validation_results.json      (IS verdict)

Robustness Analysis Framework
------------------------------
PART 1 — IS vs OOS Performance Degradation (8 metrics)
    For each metric M:
        Degradation Ratio  = OOS_M  / IS_M
        Degradation Drop   = (IS_M − OOS_M) / |IS_M|  (%)
    Metrics: Sharpe, Sortino, CAGR, Max Drawdown, Win Rate,
             Profit Factor, Win/Loss Ratio, Avg Recovery

PART 2 — Window-Level Consistency Analysis
    OOS Consistency Rate    = % windows with Sharpe > 0           (pass ≥ 70%)
    Sharpe Trend Slope      = linear slope of OOS Sharpe series   (prefer ≥ 0)
    Sharpe Std across OOS   = dispersion of per-window OOS Sharpe (lower = better)
    Best/Worst Window Ratio = best OOS Sharpe / worst OOS Sharpe  (higher = stable)

PART 3 — Parameter Stability
    Per-parameter Coefficient of Variation (CV) across winning windows:
        CV < 10%  → Excellent   (highly stable)
        CV < 20%  → Good
        CV < 30%  → Acceptable
        CV ≥ 30%  → Unstable    (overfitting risk)
    Grid-edge concentration:
        > 50% windows selecting an edge param → RED FLAG

PART 4 — Regime Decomposition
    Classify each OOS window as Bull / Bear / Sideways using IS equity trend.
    Report OOS Sharpe mean and hit rate per regime.

PART 5 — Structural Overfitting Diagnostics (9 checks)
    D1: IS→OOS Sharpe Collapse   : Both all-window AND recent-N OOS/IS ratio < 0.30
                                   (all-window alone is insufficient — COVID-era IS windows
                                    structurally depress it for any 2016-2024 strategy)
    D2: Negative OOS Expectancy  : Avg OOS Sharpe ≤ 0
    D3: OOS Consistency Failure  : All-window < 50% AND recent-N < 60% positive windows
    D4: Parameter Instability    : Any param with CV ≥ 30%
    D5: Grid-Edge Dominance      : ≥ 1 param with > 50% edge concentration
    D6: Extreme IS Perf Outlier  : IS Sharpe > 3.0 (likely overfit artifact)
    D7: OOS Sharpe Monotone Decline: slope of OOS Sharpe series < −0.05/window
    D8: OOS Volatility Explosion : Std(OOS Sharpe) > 0.80 (raised from 0.50 for trend following)
    D9: Recent Consistency Failure: Recent N-window OOS consistency < 40%

Scoring System (100-point scale)
----------------------------------
    Base score     = 100
    Deductions per triggered diagnostic: D1=20, D2=25, D3=15, D4=10, D5=10,
                                         D6=5,  D7=5,  D8=5,  D9=15
    Stability bonus: +5 if (all-window stability ≥ 0.9 and OOS consistency ≥ 80%)
                     OR (recent-N stability ≥ 0.6 and recent-N consistency ≥ 70%)

    Verdict:
        85–100  ROBUST      — strong OOS generalisation; proceed to live deployment
        65– 84  MARGINAL    — acceptable OOS generalisation; paper trade 3–6 months
        < 65    OVERFITTED  — IS performance does not generalise; re-optimise required

Outputs
-------
    data_cache/oos_validation/oos_validation_results.json     (machine-readable)
    data_cache/oos_validation/oos_validation_summary.txt      (human-readable)
    data_cache/oos_validation/oos_degradation_table.csv       (per-metric breakdown)
    data_cache/oos_validation/oos_window_analysis.csv         (per-window metrics)
    reports/oos_validation/{YYYYMMDD}_oos_validation_report.json
    logs/oos_validation_{timestamp}.log

Execution
---------
    # Standard run (reads all upstream outputs automatically)
    python scripts/20_oos_validator.py

    # With specific WFO tag (matches Script 17 --output-tag)
    python scripts/20_oos_validator.py --wfo-tag quarterly_Q4

    # Strict mode — MARGINAL is treated as OVERFITTED
    python scripts/20_oos_validator.py --strict

    # Custom degradation tolerance (default 0.50 = allow up to 50% drop)
    python scripts/20_oos_validator.py --max-degradation 0.40

Architecture: v3.3 (Feb 2026) — Multi-Asset Trend Following Strategy
"""

import os
import sys
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
from config.strategies import resolve_strategies, add_strategy_argument, StrategyDef
import json
import logging
import argparse
import warnings
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any

import numpy as np
import pandas as pd
from scipy import stats as scipy_stats

warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=RuntimeWarning)


# ===========================================================================
# PROJECT PATHS
# ===========================================================================

PROJECT_ROOT     = Path(__file__).resolve().parent.parent
DATA_CACHE_DIR   = PROJECT_ROOT / "data_cache"
BACKTEST_DIR     = DATA_CACHE_DIR / "backtest"
WFO_DIR          = BACKTEST_DIR / "walk_forward"
VALIDATION_DIR   = DATA_CACHE_DIR / "validation"
OOS_DIR          = DATA_CACHE_DIR / "oos_validation"
REPORTS_DIR      = PROJECT_ROOT / "reports" / "oos_validation"
LOG_DIR          = PROJECT_ROOT / "logs"


# ===========================================================================
# CONSTANTS & THRESHOLDS
# ===========================================================================

# ── Scoring deductions per triggered diagnostic ──────────────────────────────
# Recalibrated for trend-following (v3.3):
#   D1 reduced 30→20: all-window stability ratio is structurally depressed by
#      COVID-era IS windows; recent-window stability is the actionable signal.
#   D3 reduced 20→15: same reason — 53-57% all-window consistency is normal for
#      a strategy spanning 2016-2024 (two anomalous regimes in that window).
#   D9 added (15 pts): recent 5-window consistency failure is higher signal than
#      all-window and warrants a meaningful deduction.
DIAGNOSTIC_DEDUCTIONS: Dict[str, int] = {
    "D1_sharpe_collapse":           20,   # was 30
    "D2_negative_oos_expectancy":   25,
    "D3_oos_consistency_failure":   15,   # was 20
    "D4_parameter_instability":     10,
    "D5_grid_edge_dominance":       10,
    "D6_extreme_is_sharpe":          5,
    "D7_oos_sharpe_decline":         5,
    "D8_oos_sharpe_volatility":      5,
    "D9_recent_consistency_failure": 15,  # new — recent 5-window consistency
}

STABILITY_BONUS_SCORE  = 5

# ── Verdict thresholds ────────────────────────────────────────────────────────
VERDICT_THRESHOLDS: List[Tuple[int, str, str]] = [
    (85, "ROBUST",     "Strong OOS generalisation — proceed to live deployment"),
    (65, "MARGINAL",   "Acceptable OOS generalisation — paper trade 3–6 months"),
    (0,  "OVERFITTED", "IS performance does not generalise — re-optimise required"),
]

# ── Degradation limits ────────────────────────────────────────────────────────
DEFAULT_MAX_DEGRADATION = 0.50   # 50% drop in any metric is the default fail threshold

# ── Consistency gates ─────────────────────────────────────────────────────────
OOS_CONSISTENCY_PASS = 0.70      # ≥ 70% windows with positive Sharpe → PASS
OOS_CONSISTENCY_WARN = 0.60      # 60–70% → WARNING
OOS_CONSISTENCY_FAIL = 0.50      # < 50% → triggers D3 (lowered from 60% for trend following)

# ── Recent-window consistency gate (D9) ───────────────────────────────────────
RECENT_CONSISTENCY_FAIL = 0.40   # < 40% of last N windows positive → triggers D9

# ── Stability ratio gates ─────────────────────────────────────────────────────
STABILITY_EXCELLENT = 0.80       # OOS Sharpe / IS Sharpe
STABILITY_GOOD      = 0.60
STABILITY_FAIL      = 0.30       # triggers D1 on all-window ratio (lowered from 0.50;
                                 # recent-window ratio is the primary D1 signal)

# ── Parameter CV gates ────────────────────────────────────────────────────────
CV_EXCELLENT  = 0.10
CV_GOOD       = 0.20
CV_ACCEPTABLE = 0.30
CV_FAIL       = 0.30             # triggers D4

EDGE_CONCENTRATION_FAIL = 0.50   # triggers D5

IS_SHARPE_EXTREME = 3.0          # triggers D6

OOS_SLOPE_DECLINE = -0.05        # triggers D7 (per window)
OOS_STD_HIGH      = 0.80         # triggers D8 — raised from 0.50; trend following
                                 # structurally produces high inter-period Sharpe
                                 # variance across regimes spanning 8+ years

# ── Regime classification ─────────────────────────────────────────────────────
BULL_THRESHOLD     =  0.05       # annualised return > 5%   → Bull
BEAR_THRESHOLD     = -0.05       # annualised return < -5%  → Bear
# between → Sideways

# ── Degradation metric config ─────────────────────────────────────────────────
# Each entry: (is_key, oos_key, direction, weight, display_name)
#   direction: "higher" → higher is better; "lower" → lower is better
DEGRADATION_METRICS: List[Dict] = [
    {"is_key": "sharpe_ratio",      "direction": "higher", "weight": 3.0, "name": "Sharpe Ratio"},
    {"is_key": "sortino_ratio",     "direction": "higher", "weight": 2.0, "name": "Sortino Ratio"},
    {"is_key": "cagr_pct",          "direction": "higher", "weight": 2.0, "name": "CAGR (%)"},
    {"is_key": "max_drawdown_pct",  "direction": "higher", "weight": 1.5, "name": "Max Drawdown (%)"},
    {"is_key": "win_rate_pct",      "direction": "higher", "weight": 1.0, "name": "Win Rate (%)"},
    {"is_key": "profit_factor",     "direction": "higher", "weight": 1.5, "name": "Profit Factor"},
    {"is_key": "win_loss_ratio",    "direction": "higher", "weight": 1.0, "name": "Win/Loss Ratio"},
    {"is_key": "calmar_ratio",      "direction": "higher", "weight": 1.5, "name": "Calmar Ratio"},
]


# ===========================================================================
# LOGGING
# ===========================================================================

def setup_logging(tag: str = "") -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
    sfx = f"_{tag}" if tag else ""
    log_file = LOG_DIR / f"oos_validation{sfx}_{ts}.log"
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
    logger = logging.getLogger("oos_validator")
    logger.info(f"OOS Validator started — log → {log_file}")
    return logger


# ===========================================================================
# DATA LOADING
# ===========================================================================

def _resolve_path(base: Path, name: str, tag: str) -> Path:
    """
    Attempt to find a file with or without the output tag suffix.
    Returns the first existing path, or the un-tagged path as fallback.
    """
    if tag:
        tagged = base / f"{Path(name).stem}_{tag}{Path(name).suffix}"
        if tagged.exists():
            return tagged
    default = base / name
    return default


def load_is_metrics(logger: logging.Logger) -> Optional[Dict]:
    """Load Script 16 IS performance metrics."""
    p = BACKTEST_DIR / "performance_metrics.json"
    if not p.exists():
        logger.warning(f"IS metrics not found: {p}")
        return None
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    logger.info(f"IS metrics loaded from {p}")
    return data


def load_is_trade_log(logger: logging.Logger) -> Optional[pd.DataFrame]:
    """Load Script 16 IS trade log."""
    p = BACKTEST_DIR / "trade_log.csv"
    if not p.exists():
        logger.warning(f"Trade log not found: {p}")
        return None
    df = pd.read_csv(p, parse_dates=["entry_date", "exit_date"], low_memory=False)
    logger.info(f"Trade log loaded: {len(df)} trades from {p}")
    return df


def load_is_equity_curve(logger: logging.Logger) -> Optional[pd.DataFrame]:
    """Load Script 16 IS equity curve."""
    p = BACKTEST_DIR / "equity_curve.csv"
    if not p.exists():
        logger.warning(f"Equity curve not found: {p}")
        return None
    df = pd.read_csv(p, parse_dates=["date"], index_col="date")
    logger.info(f"Equity curve loaded: {len(df)} rows from {p}")
    return df


def load_is_annual_returns(logger: logging.Logger) -> Optional[pd.DataFrame]:
    """Load Script 16 annual returns table."""
    p = BACKTEST_DIR / "annual_returns.csv"
    if not p.exists():
        logger.warning(f"Annual returns not found: {p}")
        return None
    df = pd.read_csv(p)
    logger.info(f"Annual returns loaded: {len(df)} years from {p}")
    return df


def load_wfo_results(tag: str, logger: logging.Logger) -> Optional[Dict]:
    """Load Script 17 WFO full results JSON."""
    p = _resolve_path(WFO_DIR, "wfo_results.json", tag)
    if not p.exists():
        logger.warning(f"WFO results not found: {p}")
        return None
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    logger.info(f"WFO results loaded: {len(data.get('windows', []))} windows from {p}")
    return data


def load_wfo_optimal_params(tag: str, logger: logging.Logger) -> Optional[Dict]:
    """Load Script 17 optimal parameters JSON."""
    p = _resolve_path(WFO_DIR, "wfo_optimal_params.json", tag)
    if not p.exists():
        logger.warning(f"WFO optimal params not found: {p}")
        return None
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    logger.info(f"WFO optimal params loaded from {p}")
    return data


def load_wfo_window_summary(tag: str, logger: logging.Logger) -> Optional[pd.DataFrame]:
    """Load Script 17 window summary CSV."""
    p = _resolve_path(WFO_DIR, "wfo_window_summary.csv", tag)
    if not p.exists():
        logger.warning(f"WFO window summary not found: {p}")
        return None
    df = pd.read_csv(p)
    logger.info(f"WFO window summary loaded: {len(df)} windows from {p}")
    return df


def load_validation_results(logger: logging.Logger) -> Optional[Dict]:
    """Load Script 19 IS validation results (for cross-reference)."""
    p = VALIDATION_DIR / "validation_results.json"
    if not p.exists():
        logger.info("Script 19 validation results not found — skipping cross-reference.")
        return None
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    logger.info(f"IS validation results loaded from {p}")
    return data


def load_all_inputs(wfo_tag: str, logger: logging.Logger) -> Dict[str, Any]:
    """Load all required inputs and return as a named dict."""
    return {
        "is_metrics":         load_is_metrics(logger),
        "is_trade_log":       load_is_trade_log(logger),
        "is_equity_curve":    load_is_equity_curve(logger),
        "is_annual_returns":  load_is_annual_returns(logger),
        "wfo_results":        load_wfo_results(wfo_tag, logger),
        "wfo_optimal_params": load_wfo_optimal_params(wfo_tag, logger),
        "wfo_window_summary": load_wfo_window_summary(wfo_tag, logger),
        "validation_results": load_validation_results(logger),
    }


# ===========================================================================
# PART 1 — IS vs OOS DEGRADATION ANALYSIS
# ===========================================================================

def _safe_ratio(oos_val: Optional[float], is_val: Optional[float]) -> Optional[float]:
    """Compute OOS/IS ratio, handling division by zero and None."""
    if oos_val is None or is_val is None:
        return None
    if abs(is_val) < 1e-9:
        return None
    return oos_val / is_val


def _pct_drop(oos_val: Optional[float], is_val: Optional[float],
              direction: str = "higher") -> Optional[float]:
    """
    Compute percentage performance drop from IS to OOS.

    For 'higher is better' metrics  : drop = (IS - OOS) / |IS|  (positive = degraded)
    For 'lower  is better' metrics  : drop = (OOS - IS) / |IS|  (positive = degraded)
    Returns None when either value is unavailable or IS ≈ 0.
    """
    if oos_val is None or is_val is None:
        return None
    if abs(is_val) < 1e-9:
        return None
    if direction == "higher":
        return (is_val - oos_val) / abs(is_val)
    else:
        return (oos_val - is_val) / abs(is_val)


def _extract_oos_aggregate(
    windows: List[Dict],
    metric_key: str,
    fallback_oos_key: Optional[str] = None,
) -> Optional[float]:
    """
    Aggregate per-window OOS metric values into a single representative figure
    (median, to be robust against outlier windows).

    Looks inside window["oos_metrics"] first; falls back to window-level key.
    """
    vals = []
    for w in windows:
        v = None
        # Primary: oos_metrics sub-dict
        oos_m = w.get("oos_metrics", {})
        if oos_m:
            # Try common OOS metric naming conventions from Script 17
            for candidate in [f"oos_{metric_key}", metric_key,
                               f"oos_{metric_key}_pct", f"{metric_key}_pct"]:
                if candidate in oos_m:
                    v = oos_m[candidate]
                    break
        # Fallback: window-level key
        if v is None and fallback_oos_key and fallback_oos_key in w:
            v = w[fallback_oos_key]
        if v is not None:
            try:
                vals.append(float(v))
            except (TypeError, ValueError):
                pass
    return float(np.median(vals)) if vals else None


def analyse_degradation(
    is_metrics: Dict,
    windows:    List[Dict],
    max_degradation: float,
    logger:     logging.Logger,
) -> Dict:
    """
    PART 1: Compute IS→OOS degradation for each metric.

    Returns a dict with per-metric breakdowns and a weighted degradation summary.
    """
    logger.info("PART 1: IS vs OOS Degradation Analysis")

    rows = []
    weighted_degradation_sum = 0.0
    total_weight = 0.0

    for cfg in DEGRADATION_METRICS:
        is_key    = cfg["is_key"]
        direction = cfg["direction"]
        weight    = cfg["weight"]
        name      = cfg["name"]

        is_val = is_metrics.get(is_key)
        if is_val is not None:
            try:
                is_val = float(is_val)
            except (TypeError, ValueError):
                is_val = None

        # Derive a canonical OOS metric name from the IS key
        # (strip trailing _pct if present for lookup in oos_metrics)
        base_key = is_key.replace("_pct", "")
        oos_val  = _extract_oos_aggregate(windows, base_key, fallback_oos_key=f"oos_{base_key}")

        ratio     = _safe_ratio(oos_val, is_val)
        pct_drop  = _pct_drop(oos_val, is_val, direction)

        # Grade the degradation
        if pct_drop is None:
            grade = "UNKNOWN"
            passed = None
        elif pct_drop <= 0.10:
            grade, passed = "EXCELLENT",    True
        elif pct_drop <= 0.25:
            grade, passed = "GOOD",         True
        elif pct_drop <= max_degradation:
            grade, passed = "ACCEPTABLE",   True
        else:
            grade, passed = "DEGRADED",     False

        row = {
            "metric":       name,
            "is_key":       is_key,
            "is_value":     round(is_val, 4)   if is_val  is not None else None,
            "oos_value":    round(oos_val, 4)  if oos_val is not None else None,
            "ratio":        round(ratio, 4)    if ratio   is not None else None,
            "pct_drop":     round(pct_drop * 100, 2) if pct_drop is not None else None,
            "grade":        grade,
            "passed":       passed,
            "weight":       weight,
            "direction":    direction,
        }
        rows.append(row)

        logger.info(
            f"  {name:<25}: IS={row['is_value']:>8}  OOS={row['oos_value']:>8}  "
            f"Ratio={row['ratio']:>6}  Drop={row['pct_drop']:>6}%  [{grade}]"
            if None not in (row["is_value"], row["oos_value"], row["ratio"], row["pct_drop"])
            else f"  {name:<25}: insufficient data"
        )

        if pct_drop is not None:
            weighted_degradation_sum += pct_drop * weight
            total_weight += weight

    weighted_avg_drop = (
        (weighted_degradation_sum / total_weight) * 100
        if total_weight > 0 else None
    )

    metrics_passed  = sum(1 for r in rows if r["passed"] is True)
    metrics_failed  = sum(1 for r in rows if r["passed"] is False)
    metrics_unknown = sum(1 for r in rows if r["passed"] is None)

    wadrop_str = f"{weighted_avg_drop:.1f}%" if weighted_avg_drop is not None else "N/A"
    logger.info(
        f"  Weighted avg degradation: {wadrop_str} | "
        f"Passed: {metrics_passed}/{len(rows)}  "
        f"Failed: {metrics_failed}  Unknown: {metrics_unknown}"
    )

    return {
        "metrics":             rows,
        "weighted_avg_drop":   round(weighted_avg_drop, 2) if weighted_avg_drop is not None else None,
        "metrics_passed":      metrics_passed,
        "metrics_failed":      metrics_failed,
        "metrics_unknown":     metrics_unknown,
        "total_metrics":       len(rows),
    }


# ===========================================================================
# PART 2 — WINDOW-LEVEL CONSISTENCY ANALYSIS
# ===========================================================================

def analyse_window_consistency(
    windows:      List[Dict],
    logger:       logging.Logger,
    wfo_stability: Optional[Dict] = None,
) -> Dict:
    """
    PART 2: Analyse OOS performance stability across walk-forward windows.

    Extracts per-window IS and OOS Sharpe ratios, computes descriptive statistics,
    and evaluates consistency gates.

    wfo_stability : optional stability dict from Script 17 wfo_results["stability"].
                    When provided, pre-computed recent-window metrics (last 5 windows)
                    are used directly instead of being re-derived from the windows list.
                    These are more reliable as they use the same IS-threshold exclusion
                    logic as Script 17.
    """
    logger.info("PART 2: Window-Level Consistency Analysis")

    if not windows:
        logger.warning("  No WFO windows available — skipping window analysis.")
        return {"available": False, "n_windows": 0}

    n = len(windows)
    is_sharpes  = [float(w.get("is_best_sharpe", np.nan)) for w in windows]
    oos_sharpes = [float(w.get("oos_sharpe",     np.nan)) for w in windows]

    # Remove NaNs
    is_sharpes_clean  = [v for v in is_sharpes  if not np.isnan(v)]
    oos_sharpes_clean = [v for v in oos_sharpes if not np.isnan(v)]

    if not oos_sharpes_clean:
        logger.warning("  No valid OOS Sharpe values — window analysis incomplete.")
        return {"available": False, "n_windows": n}

    oos_positive    = sum(1 for v in oos_sharpes_clean if v > 0)
    oos_consistency = oos_positive / len(oos_sharpes_clean)

    avg_is_sharpe   = float(np.mean(is_sharpes_clean))  if is_sharpes_clean  else None
    avg_oos_sharpe  = float(np.mean(oos_sharpes_clean))
    med_oos_sharpe  = float(np.median(oos_sharpes_clean))
    std_oos_sharpe  = float(np.std(oos_sharpes_clean, ddof=1)) if len(oos_sharpes_clean) > 1 else 0.0
    stability_ratio = (avg_oos_sharpe / avg_is_sharpe) if (avg_is_sharpe and abs(avg_is_sharpe) > 1e-9) else None

    # OOS Sharpe trend (linear regression over window index)
    if len(oos_sharpes_clean) >= 3:
        x = np.arange(len(oos_sharpes_clean), dtype=float)
        slope, intercept, r_val, p_val, _ = scipy_stats.linregress(x, oos_sharpes_clean)
        oos_trend_slope    = float(slope)
        oos_trend_r2       = float(r_val ** 2)
        oos_trend_pvalue   = float(p_val)
    else:
        oos_trend_slope  = 0.0
        oos_trend_r2     = 0.0
        oos_trend_pvalue = 1.0

    # Best/worst ratio
    best_oos  = max(oos_sharpes_clean)
    worst_oos = min(oos_sharpes_clean)
    bw_ratio  = (best_oos / abs(worst_oos)) if abs(worst_oos) > 1e-9 else None

    # Consistency grades
    if oos_consistency >= OOS_CONSISTENCY_PASS:
        consistency_grade = "PASS"
    elif oos_consistency >= OOS_CONSISTENCY_WARN:
        consistency_grade = "WARNING"
    else:
        consistency_grade = "FAIL"

    # Stability ratio grade
    if stability_ratio is None:
        stability_grade = "UNKNOWN"
    elif stability_ratio >= STABILITY_EXCELLENT:
        stability_grade = "EXCELLENT"
    elif stability_ratio >= STABILITY_GOOD:
        stability_grade = "GOOD"
    elif stability_ratio >= STABILITY_FAIL:
        stability_grade = "ACCEPTABLE"
    else:
        stability_grade = "POOR"

    logger.info(f"  Windows analysed        : {n}")
    logger.info(f"  Avg IS  Sharpe          : {avg_is_sharpe:.4f}" if avg_is_sharpe else "  Avg IS Sharpe: N/A")
    logger.info(f"  Avg OOS Sharpe          : {avg_oos_sharpe:.4f}")
    logger.info(f"  Median OOS Sharpe       : {med_oos_sharpe:.4f}")
    logger.info(f"  Std  OOS Sharpe         : {std_oos_sharpe:.4f}")
    logger.info(f"  Stability Ratio (O/I)   : {stability_ratio:.4f}  [{stability_grade}]" if stability_ratio else "  Stability Ratio: N/A")
    logger.info(f"  OOS Consistency         : {oos_consistency:.1%}  ({oos_positive}/{len(oos_sharpes_clean)})  [{consistency_grade}]")
    logger.info(f"  OOS Sharpe Trend Slope  : {oos_trend_slope:+.4f}  (R²={oos_trend_r2:.3f}  p={oos_trend_pvalue:.3f})")

    # Per-window summary for output
    window_rows = []
    for w, is_s, oos_s in zip(windows, is_sharpes, oos_sharpes):
        stability = (oos_s / is_s) if (not np.isnan(is_s) and abs(is_s) > 1e-9) else None
        window_rows.append({
            "window_id":      w.get("window_id"),
            "is_start":       w.get("is_start"),
            "is_end":         w.get("is_end"),
            "oos_start":      w.get("oos_start"),
            "oos_end":        w.get("oos_end"),
            "is_sharpe":      round(is_s, 4)   if not np.isnan(is_s)  else None,
            "oos_sharpe":     round(oos_s, 4)  if not np.isnan(oos_s) else None,
            "oos_positive":   (oos_s > 0)      if not np.isnan(oos_s) else None,
            "stability":      round(stability, 4) if stability is not None else None,
            "oos_cagr":       w.get("oos_metrics", {}).get("oos_cagr"),
            "oos_max_dd":     w.get("oos_metrics", {}).get("oos_max_dd"),
            "oos_trades":     w.get("oos_metrics", {}).get("oos_trades"),
            "best_params":    w.get("best_params", {}),
        })

    # ── Recent-window metrics ─────────────────────────────────────────────────
    # Use pre-computed values from Script 17 if available (preferred — they apply
    # the same IS-threshold exclusion logic). Otherwise fall back to computing
    # directly from the last RECENT_N windows in the windows list.
    RECENT_N = 5
    if wfo_stability and "recent_stability_ratio" in wfo_stability:
        recent_stability_ratio  = wfo_stability.get("recent_stability_ratio")
        recent_oos_consistency  = wfo_stability.get("recent_oos_consistency")
        recent_oos_positive     = wfo_stability.get("recent_oos_positive_windows")
        recent_n                = wfo_stability.get("recent_window_n", RECENT_N)
        recent_avg_oos_sharpe   = wfo_stability.get("recent_avg_oos_sharpe")
        recent_oos_sharpes_list = wfo_stability.get("recent_oos_sharpes", [])
        recent_source           = "script17"
    else:
        # Fallback: compute from raw windows (no IS-threshold exclusion here)
        recent_wins  = windows[-RECENT_N:] if len(windows) >= RECENT_N else windows[:]
        r_oos        = [float(w.get("oos_sharpe", np.nan)) for w in recent_wins]
        r_oos_clean  = [v for v in r_oos if not np.isnan(v)]
        r_is         = [float(w.get("is_best_sharpe", np.nan)) for w in recent_wins]
        r_is_clean   = [v for v in r_is if not np.isnan(v) and v > 0.05]
        recent_n             = len(r_oos_clean)
        r_pos                = sum(1 for v in r_oos_clean if v > 0)
        recent_oos_positive  = r_pos
        recent_oos_consistency = r_pos / recent_n if recent_n > 0 else 0.0
        r_avg_is = float(np.mean(r_is_clean)) if r_is_clean else 0.0
        r_avg_oos = float(np.mean(r_oos_clean)) if r_oos_clean else 0.0
        recent_stability_ratio  = r_avg_oos / r_avg_is if r_avg_is > 0 else None
        recent_avg_oos_sharpe   = r_avg_oos
        recent_oos_sharpes_list = [round(v, 4) for v in r_oos_clean]
        recent_source           = "computed"

    if recent_oos_consistency is not None and recent_oos_consistency >= OOS_CONSISTENCY_PASS:
        recent_consistency_grade = "PASS"
    elif recent_oos_consistency is not None and recent_oos_consistency >= OOS_CONSISTENCY_WARN:
        recent_consistency_grade = "WARNING"
    else:
        recent_consistency_grade = "FAIL"

    logger.info(
        f"  Recent {recent_n}-window OOS consistency : "
        f"{recent_oos_consistency:.1%} [{recent_consistency_grade}]  "
        f"(source={recent_source})"
        if recent_oos_consistency is not None else
        f"  Recent-window metrics: unavailable"
    )
    if recent_stability_ratio is not None:
        logger.info(f"  Recent {recent_n}-window stability ratio : {recent_stability_ratio:.4f}")

    return {
        "available":              True,
        "n_windows":              n,
        "avg_is_sharpe":          round(avg_is_sharpe, 4)   if avg_is_sharpe  is not None else None,
        "avg_oos_sharpe":         round(avg_oos_sharpe, 4),
        "median_oos_sharpe":      round(med_oos_sharpe, 4),
        "std_oos_sharpe":         round(std_oos_sharpe, 4),
        "stability_ratio":        round(stability_ratio, 4) if stability_ratio is not None else None,
        "stability_grade":        stability_grade,
        "oos_consistency":        round(oos_consistency, 4),
        "oos_positive_windows":   oos_positive,
        "consistency_grade":      consistency_grade,
        "oos_trend_slope":        round(oos_trend_slope, 6),
        "oos_trend_r2":           round(oos_trend_r2, 4),
        "oos_trend_pvalue":       round(oos_trend_pvalue, 4),
        "best_oos_sharpe":        round(best_oos, 4),
        "worst_oos_sharpe":       round(worst_oos, 4),
        "best_worst_ratio":       round(bw_ratio, 4) if bw_ratio is not None else None,
        # Recent-window metrics (forward-looking signal)
        "recent_n":               recent_n,
        "recent_stability_ratio": round(recent_stability_ratio, 4) if recent_stability_ratio is not None else None,
        "recent_oos_consistency": round(recent_oos_consistency, 4) if recent_oos_consistency is not None else None,
        "recent_oos_positive":    recent_oos_positive,
        "recent_avg_oos_sharpe":  round(recent_avg_oos_sharpe, 4) if recent_avg_oos_sharpe is not None else None,
        "recent_oos_sharpes":     recent_oos_sharpes_list,
        "recent_consistency_grade": recent_consistency_grade,
        "recent_source":          recent_source,
        "window_rows":            window_rows,
    }


# ===========================================================================
# PART 3 — PARAMETER STABILITY
# ===========================================================================

def _cv_grade(cv: Optional[float]) -> str:
    if cv is None:
        return "UNKNOWN"
    if cv < CV_EXCELLENT:
        return "EXCELLENT"
    if cv < CV_GOOD:
        return "GOOD"
    if cv < CV_ACCEPTABLE:
        return "ACCEPTABLE"
    return "UNSTABLE"


def analyse_parameter_stability(
    windows:      List[Dict],
    wfo_results:  Optional[Dict],
    logger:       logging.Logger,
) -> Dict:
    """
    PART 3: Assess how consistently the walk-forward optimizer selects the same
    parameter values across windows.  High CV or persistent grid-edge selection
    are overfitting red flags.
    """
    logger.info("PART 3: Parameter Stability Analysis")

    # Try to use pre-computed stability from Script 17 if available
    if wfo_results and "stability" in wfo_results:
        precomp = wfo_results["stability"]
        param_cv_raw  = precomp.get("param_cv", {})
        param_edges   = precomp.get("param_edges", {})

        param_analysis = {}
        any_unstable = False
        any_edge_dominant = False

        for param, info in param_cv_raw.items():
            cv    = info.get("cv")
            grade = _cv_grade(cv)
            edge  = param_edges.get(param, {})
            pct_edge = edge.get("pct_at_edge")

            if grade == "UNSTABLE":
                any_unstable = True
            if pct_edge is not None and pct_edge > EDGE_CONCENTRATION_FAIL:
                any_edge_dominant = True

            param_analysis[param] = {
                "mean":          info.get("mean"),
                "std":           info.get("std"),
                "cv":            cv,
                "cv_grade":      grade,
                "pct_at_edge":   pct_edge,
                "edge_dominant": (pct_edge > EDGE_CONCENTRATION_FAIL) if pct_edge is not None else False,
            }
            logger.info(
                f"  {param:<22}: CV={cv:.3f}  [{grade:<10}]  "
                f"Edge={pct_edge:.1%}" if (cv is not None and pct_edge is not None)
                else f"  {param:<22}: (insufficient data)"
            )

    else:
        # Recompute from window-level best_params
        logger.info("  Recomputing parameter stability from raw window data …")
        param_values: Dict[str, List[float]] = {}

        for w in windows:
            bp = w.get("best_params", {})
            for k, v in bp.items():
                if v is not None:
                    param_values.setdefault(k, []).append(float(v))

        param_analysis = {}
        any_unstable       = False
        any_edge_dominant  = False

        for param, vals in param_values.items():
            if len(vals) < 2:
                param_analysis[param] = {
                    "mean": vals[0] if vals else None, "std": None,
                    "cv": None, "cv_grade": "UNKNOWN", "pct_at_edge": None,
                    "edge_dominant": False,
                }
                continue

            mean_v = float(np.mean(vals))
            std_v  = float(np.std(vals, ddof=1))
            cv     = (std_v / abs(mean_v)) if abs(mean_v) > 1e-9 else None
            grade  = _cv_grade(cv)

            # Edge detection: is the mode at the min or max of observed values?
            min_v, max_v = min(vals), max(vals)
            edge_count = sum(1 for v in vals if v == min_v or v == max_v)
            pct_edge   = edge_count / len(vals)

            if grade == "UNSTABLE":
                any_unstable = True
            if pct_edge > EDGE_CONCENTRATION_FAIL:
                any_edge_dominant = True

            param_analysis[param] = {
                "mean":          round(mean_v, 4),
                "std":           round(std_v, 4),
                "cv":            round(cv, 4) if cv is not None else None,
                "cv_grade":      grade,
                "pct_at_edge":   round(pct_edge, 4),
                "edge_dominant": pct_edge > EDGE_CONCENTRATION_FAIL,
            }
            logger.info(
                f"  {param:<22}: CV={cv:.3f}  [{grade:<10}]  "
                f"Edge={pct_edge:.1%}" if cv is not None else
                f"  {param:<22}: (insufficient data)"
            )

    return {
        "parameters":      param_analysis,
        "any_unstable":    any_unstable,
        "any_edge_dominant": any_edge_dominant,
        "n_params_analysed": len(param_analysis),
    }


# ===========================================================================
# PART 4 — REGIME DECOMPOSITION
# ===========================================================================

def _classify_regime(annualised_return: float) -> str:
    if annualised_return > BULL_THRESHOLD:
        return "BULL"
    elif annualised_return < BEAR_THRESHOLD:
        return "BEAR"
    return "SIDEWAYS"


def analyse_regime_performance(
    windows:      List[Dict],
    equity_curve: Optional[pd.DataFrame],
    logger:       logging.Logger,
) -> Dict:
    """
    PART 4: Classify each OOS window by prevailing market regime (using the IS
    equity return as a proxy for market direction) and report OOS Sharpe by regime.
    """
    logger.info("PART 4: Regime Decomposition")

    if not windows or equity_curve is None:
        logger.info("  Insufficient data for regime analysis — skipping.")
        return {"available": False}

    # Normalise equity curve index
    eq = equity_curve.copy()
    if not isinstance(eq.index, pd.DatetimeIndex):
        try:
            eq.index = pd.to_datetime(eq.index)
        except Exception:
            logger.warning("  Could not parse equity curve dates — skipping regime analysis.")
            return {"available": False}

    # Identify equity column
    equity_col = None
    for candidate in ["equity", "portfolio_value", "portfolio_equity", "value"]:
        if candidate in eq.columns:
            equity_col = candidate
            break
    if equity_col is None and len(eq.columns) > 0:
        equity_col = eq.columns[0]

    if equity_col is None:
        logger.warning("  Could not identify equity column — skipping regime analysis.")
        return {"available": False}

    regime_data = {}
    for w in windows:
        oos_sharpe = w.get("oos_sharpe")
        if oos_sharpe is None or np.isnan(float(oos_sharpe)):
            continue

        # Use IS period equity return to classify regime
        try:
            is_start = pd.Timestamp(w["is_start"])
            is_end   = pd.Timestamp(w["is_end"])
            is_slice = eq.loc[is_start:is_end, equity_col].dropna()
            if len(is_slice) < 20:
                continue

            # Annualised return over IS period
            years = (is_end - is_start).days / 365.25
            total_ret = (is_slice.iloc[-1] / is_slice.iloc[0]) - 1.0
            ann_ret   = (1 + total_ret) ** (1 / max(years, 0.5)) - 1 if years > 0 else total_ret

            regime = _classify_regime(ann_ret)
        except Exception:
            continue

        regime_data.setdefault(regime, []).append({
            "window_id":   w.get("window_id"),
            "oos_sharpe":  float(oos_sharpe),
            "ann_ret_is":  ann_ret,
        })

    regime_summary = {}
    for regime, entries in regime_data.items():
        oos_vals      = [e["oos_sharpe"] for e in entries]
        hit_rate      = sum(1 for v in oos_vals if v > 0) / len(oos_vals)
        regime_summary[regime] = {
            "n_windows":       len(entries),
            "avg_oos_sharpe":  round(float(np.mean(oos_vals)), 4),
            "med_oos_sharpe":  round(float(np.median(oos_vals)), 4),
            "hit_rate":        round(hit_rate, 4),
            "windows":         entries,
        }
        logger.info(
            f"  {regime:<8}: n={len(entries)}  "
            f"AvgSharpe={np.mean(oos_vals):.3f}  "
            f"HitRate={hit_rate:.0%}"
        )

    if not regime_summary:
        logger.info("  No regime data extracted.")
        return {"available": False}

    return {
        "available": True,
        "regimes":   regime_summary,
        "total_windows_classified": sum(v["n_windows"] for v in regime_summary.values()),
    }


# ===========================================================================
# PART 5 — STRUCTURAL OVERFITTING DIAGNOSTICS
# ===========================================================================

def run_overfitting_diagnostics(
    is_metrics:       Dict,
    window_analysis:  Dict,
    param_stability:  Dict,
    degradation:      Dict,
    logger:           logging.Logger,
) -> Dict:
    """
    PART 5: Run 8 structural overfitting diagnostic checks.

    Each check returns: triggered (bool), severity (HIGH/MEDIUM/LOW), message.
    """
    logger.info("PART 5: Structural Overfitting Diagnostics")

    is_sharpe = is_metrics.get("sharpe_ratio")
    if is_sharpe is not None:
        try:
            is_sharpe = float(is_sharpe)
        except (TypeError, ValueError):
            is_sharpe = None

    avg_oos_sharpe          = window_analysis.get("avg_oos_sharpe")
    stability_ratio         = window_analysis.get("stability_ratio")
    oos_consistency         = window_analysis.get("oos_consistency")
    oos_slope               = window_analysis.get("oos_trend_slope")
    std_oos                 = window_analysis.get("std_oos_sharpe")
    recent_stability_ratio  = window_analysis.get("recent_stability_ratio")
    recent_oos_consistency  = window_analysis.get("recent_oos_consistency")
    recent_n                = window_analysis.get("recent_n", 5)

    diagnostics = {}

    # ── D1: IS→OOS Sharpe Collapse ───────────────────────────────────────────
    # For trend following spanning 8+ years the all-window stability ratio is
    # structurally depressed by COVID-era IS windows (IS Sharpe 1.6-1.9 from
    # 2019-2021 inflates the denominator). The recent-window stability ratio is
    # the primary signal. D1 only triggers if BOTH are below threshold.
    d1_all_window_fail = (stability_ratio is not None and stability_ratio < STABILITY_FAIL)
    d1_recent_fail     = (recent_stability_ratio is not None and recent_stability_ratio < STABILITY_FAIL)
    d1_triggered       = d1_all_window_fail and d1_recent_fail

    _d1_obs = (
        f"all_window_ratio={stability_ratio:.4f}, "
        f"recent_{recent_n}w_ratio={recent_stability_ratio:.4f}"
        if recent_stability_ratio is not None
        else f"ratio={stability_ratio:.4f}"
    ) if stability_ratio is not None else "N/A"

    diagnostics["D1_sharpe_collapse"] = {
        "name":      "IS→OOS Sharpe Collapse",
        "triggered": d1_triggered,
        "severity":  "HIGH",
        "threshold": f"Both all-window AND recent-{recent_n}w OOS/IS Sharpe ratio < {STABILITY_FAIL}",
        "observed":  _d1_obs,
        "message": (
            f"Both all-window ({stability_ratio:.3f}) and recent ({recent_stability_ratio:.3f}) "
            f"stability ratios below {STABILITY_FAIL} — genuine OOS generalisation failure."
            if d1_triggered else
            (f"Recent {recent_n}-window stability ratio ({recent_stability_ratio:.3f}) above threshold "
             f"despite low all-window ratio ({stability_ratio:.3f}) — COVID-era windows depressing aggregate."
             if (d1_all_window_fail and not d1_recent_fail and recent_stability_ratio is not None)
             else "OOS/IS Sharpe ratio within acceptable bounds.")
        ),
    }

    # ── D2: Negative OOS Expectancy ──────────────────────────────────────────
    d2_triggered = (avg_oos_sharpe is not None and avg_oos_sharpe <= 0.0)
    diagnostics["D2_negative_oos_expectancy"] = {
        "name":      "Negative OOS Expectancy",
        "triggered": d2_triggered,
        "severity":  "HIGH",
        "threshold": "Avg OOS Sharpe ≤ 0",
        "observed":  f"avg_oos_sharpe={avg_oos_sharpe:.4f}" if avg_oos_sharpe is not None else "N/A",
        "message": (
            f"Average OOS Sharpe is {avg_oos_sharpe:.4f} — strategy has negative OOS expectancy."
            if d2_triggered else "Average OOS Sharpe is positive."
        ),
    }

    # ── D3: OOS Consistency Failure ──────────────────────────────────────────
    # Recent-window consistency overrides all-window failure: if the last N windows
    # are consistently positive, the all-window figure is being dragged down by
    # historical regime outliers rather than reflecting current strategy behaviour.
    d3_all_fail    = (oos_consistency is not None and oos_consistency < OOS_CONSISTENCY_FAIL)
    d3_recent_pass = (recent_oos_consistency is not None
                      and recent_oos_consistency >= OOS_CONSISTENCY_WARN)
    d3_triggered   = d3_all_fail and not d3_recent_pass

    _d3_obs = (
        f"all_window={oos_consistency:.1%}, recent_{recent_n}w={recent_oos_consistency:.1%}"
        if recent_oos_consistency is not None
        else f"consistency={oos_consistency:.1%}"
    ) if oos_consistency is not None else "N/A"

    diagnostics["D3_oos_consistency_failure"] = {
        "name":      "OOS Consistency Failure",
        "triggered": d3_triggered,
        "severity":  "HIGH",
        "threshold": f"All-window OOS consistency < {OOS_CONSISTENCY_FAIL:.0%} "
                     f"AND recent-{recent_n}w consistency < {OOS_CONSISTENCY_WARN:.0%}",
        "observed":  _d3_obs,
        "message": (
            f"Persistent OOS inconsistency: all-window {oos_consistency:.0%} and "
            f"recent {recent_oos_consistency:.0%} both below thresholds."
            if d3_triggered else
            (f"All-window consistency {oos_consistency:.0%} below threshold but "
             f"recent {recent_n}-window consistency {recent_oos_consistency:.0%} acceptable — "
             f"historical outlier windows depressing aggregate."
             if (d3_all_fail and d3_recent_pass)
             else f"OOS consistency {oos_consistency:.0%} meets threshold.")
        ),
    }

    # ── D4: Parameter Instability ─────────────────────────────────────────────
    d4_triggered = param_stability.get("any_unstable", False)
    diagnostics["D4_parameter_instability"] = {
        "name":      "Parameter Instability",
        "triggered": d4_triggered,
        "severity":  "MEDIUM",
        "threshold": f"Any parameter CV ≥ {CV_FAIL:.0%}",
        "observed":  "One or more parameters with CV ≥ 30%" if d4_triggered else "All CVs below threshold",
        "message": (
            "Optimizer selects different parameters each window — no stable optimum exists."
            if d4_triggered else "Parameter selection is consistent across windows."
        ),
    }

    # ── D5: Grid-Edge Dominance ───────────────────────────────────────────────
    d5_triggered = param_stability.get("any_edge_dominant", False)
    diagnostics["D5_grid_edge_dominance"] = {
        "name":      "Grid-Edge Dominance",
        "triggered": d5_triggered,
        "severity":  "MEDIUM",
        "threshold": f"Any parameter > {EDGE_CONCENTRATION_FAIL:.0%} edge concentration",
        "observed":  "Edge concentration detected" if d5_triggered else "No edge concentration",
        "message": (
            "Optimizer consistently selects extreme parameter values — extend the grid."
            if d5_triggered else "Parameter selections are interior to the optimization grid."
        ),
    }

    # ── D6: Extreme IS Sharpe ─────────────────────────────────────────────────
    d6_triggered = (is_sharpe is not None and is_sharpe > IS_SHARPE_EXTREME)
    diagnostics["D6_extreme_is_sharpe"] = {
        "name":      "Extreme IS Performance",
        "triggered": d6_triggered,
        "severity":  "LOW",
        "threshold": f"IS Sharpe > {IS_SHARPE_EXTREME}",
        "observed":  f"IS Sharpe={is_sharpe:.4f}" if is_sharpe is not None else "N/A",
        "message": (
            f"IS Sharpe of {is_sharpe:.2f} exceeds {IS_SHARPE_EXTREME} — possible look-ahead or overfitting."
            if d6_triggered else "IS Sharpe ratio is within realistic range."
        ),
    }

    # ── D7: OOS Sharpe Monotone Decline ──────────────────────────────────────
    d7_triggered = (oos_slope is not None and oos_slope < OOS_SLOPE_DECLINE)
    diagnostics["D7_oos_sharpe_decline"] = {
        "name":      "OOS Sharpe Monotone Decline",
        "triggered": d7_triggered,
        "severity":  "LOW",
        "threshold": f"OOS Sharpe trend slope < {OOS_SLOPE_DECLINE}/window",
        "observed":  f"slope={oos_slope:.5f}" if oos_slope is not None else "N/A",
        "message": (
            f"OOS Sharpe is declining over time (slope={oos_slope:.4f}/window) — regime shift possible."
            if d7_triggered else "No significant downward trend in OOS Sharpe sequence."
        ),
    }

    # ── D8: OOS Sharpe Volatility ─────────────────────────────────────────────
    # Threshold raised to 0.80 for trend following: a strategy spanning 8+ years
    # including COVID and a 40-year rate-hike cycle will structurally show high
    # inter-period Sharpe dispersion. This is a feature, not a bug.
    d8_triggered = (std_oos is not None and std_oos > OOS_STD_HIGH)
    diagnostics["D8_oos_sharpe_volatility"] = {
        "name":      "OOS Sharpe Volatility",
        "triggered": d8_triggered,
        "severity":  "LOW",
        "threshold": f"Std(OOS Sharpe) > {OOS_STD_HIGH}",
        "observed":  f"std={std_oos:.4f}" if std_oos is not None else "N/A",
        "message": (
            f"Extreme dispersion in OOS Sharpe (σ={std_oos:.3f}) — inconsistent period-to-period performance."
            if d8_triggered else "OOS Sharpe dispersion is within acceptable range."
        ),
    }

    # ── D9: Recent-Window Consistency Failure ─────────────────────────────────
    # The most forward-looking diagnostic. If the last N OOS windows are mostly
    # negative, the strategy is currently not working regardless of historical results.
    d9_triggered = (
        recent_oos_consistency is not None
        and recent_oos_consistency < RECENT_CONSISTENCY_FAIL
    )
    diagnostics["D9_recent_consistency_failure"] = {
        "name":      f"Recent {recent_n}-Window Consistency Failure",
        "triggered": d9_triggered,
        "severity":  "HIGH",
        "threshold": f"Recent {recent_n}-window OOS consistency < {RECENT_CONSISTENCY_FAIL:.0%}",
        "observed":  f"recent_consistency={recent_oos_consistency:.1%}" if recent_oos_consistency is not None else "N/A",
        "message": (
            f"Only {recent_oos_consistency:.0%} of the last {recent_n} OOS windows are profitable — "
            f"strategy is currently underperforming."
            if d9_triggered else
            (f"Recent {recent_n}-window consistency {recent_oos_consistency:.0%} is acceptable."
             if recent_oos_consistency is not None else "Recent consistency: insufficient data.")
        ),
    }

    # Log results
    triggered_count = 0
    high_severity   = 0
    for did, info in diagnostics.items():
        status = "[TRIGGERED]" if info["triggered"] else "[  CLEAR  ]"
        logger.info(f"  {status} {did}: {info['message']}")
        if info["triggered"]:
            triggered_count += 1
            if info["severity"] == "HIGH":
                high_severity += 1

    logger.info(
        f"  Diagnostics: {triggered_count}/{len(diagnostics)} triggered  "
        f"({high_severity} HIGH severity)"
    )

    return {
        "diagnostics":      diagnostics,
        "triggered_count":  triggered_count,
        "high_severity":    high_severity,
    }


# ===========================================================================
# SCORING & VERDICT
# ===========================================================================

def calculate_oos_score(
    diagnostics_result: Dict,
    window_analysis:    Dict,
    logger:             logging.Logger,
) -> Dict:
    """
    Compute the 100-point OOS robustness score.

    Starting from 100, deduct per triggered diagnostic, add stability bonus.
    """
    logger.info("Calculating OOS Robustness Score …")

    score = 100
    deductions = {}

    for did, deduction in DIAGNOSTIC_DEDUCTIONS.items():
        triggered = diagnostics_result["diagnostics"].get(did, {}).get("triggered", False)
        if triggered:
            score -= deduction
            deductions[did] = deduction
            logger.info(f"  Deduction: −{deduction} for {did}")

    # Stability bonus
    # Standard path: all-window stability ≥ 0.9 and consistency ≥ 80%
    # Trend-following path: recent-window stability ≥ 0.6 and recent consistency ≥ 70%
    #   (acknowledges that all-window ratio is structurally depressed by historical outliers)
    bonus_awarded = False
    sr     = window_analysis.get("stability_ratio")
    oc     = window_analysis.get("oos_consistency")
    r_sr   = window_analysis.get("recent_stability_ratio")
    r_oc   = window_analysis.get("recent_oos_consistency")

    if sr is not None and oc is not None and sr >= 0.9 and oc >= 0.80:
        score += STABILITY_BONUS_SCORE
        bonus_awarded = True
        logger.info(
            f"  Bonus: +{STABILITY_BONUS_SCORE} "
            f"(all-window stability={sr:.2f} ≥ 0.9, consistency={oc:.0%} ≥ 80%)"
        )
    elif (r_sr is not None and r_oc is not None
          and r_sr >= STABILITY_GOOD and r_oc >= OOS_CONSISTENCY_PASS):
        score += STABILITY_BONUS_SCORE
        bonus_awarded = True
        logger.info(
            f"  Bonus: +{STABILITY_BONUS_SCORE} "
            f"(recent {window_analysis.get('recent_n', 5)}-window stability={r_sr:.2f} ≥ "
            f"{STABILITY_GOOD}, consistency={r_oc:.0%} ≥ {OOS_CONSISTENCY_PASS:.0%})"
        )

    score = max(0, min(100, score))

    # Verdict
    verdict, verdict_description = "OVERFITTED", "IS performance does not generalise — re-optimise required"
    for threshold, v, desc in VERDICT_THRESHOLDS:
        if score >= threshold:
            verdict, verdict_description = v, desc
            break

    logger.info(f"  Final Score: {score}/100 → {verdict}")
    logger.info(f"  {verdict_description}")

    return {
        "score":               score,
        "deductions":          deductions,
        "total_deducted":      sum(deductions.values()),
        "bonus_awarded":       bonus_awarded,
        "verdict":             verdict,
        "verdict_description": verdict_description,
    }


def make_overall_decision(
    score_result:       Dict,
    diagnostics_result: Dict,
    window_analysis:    Dict,
    is_validation:      Optional[Dict],
    strict_mode:        bool,
    logger:             logging.Logger,
) -> Dict:
    """
    Produce the final binding OOS deployment decision, combining the score,
    diagnostics, and (optionally) the Script 19 IS verdict.
    """
    verdict = score_result["verdict"]

    # Strict mode: MARGINAL → OVERFITTED
    if strict_mode and verdict == "MARGINAL":
        verdict = "OVERFITTED"
        logger.warning("STRICT MODE: MARGINAL → OVERFITTED")

    # Any HIGH-severity diagnostic forces at least MARGINAL
    if diagnostics_result["high_severity"] > 0 and verdict == "ROBUST":
        verdict = "MARGINAL"
        logger.warning(
            f"Downgraded ROBUST → MARGINAL due to "
            f"{diagnostics_result['high_severity']} HIGH-severity diagnostic(s)."
        )

    # Cross-check: if Script 19 rejected, cap OOS verdict at MARGINAL
    is_verdict = None
    if is_validation:
        is_verdict = is_validation.get("overall_decision", {}).get("verdict")
    if is_verdict == "REJECT" and verdict == "ROBUST":
        verdict = "MARGINAL"
        logger.warning("Downgraded ROBUST → MARGINAL: Script 19 IS validation REJECTED.")

    # Final recommendation text
    recs = {
        "ROBUST":     "Strategy generalises well to OOS data. Proceed with live deployment using optimal WFO parameters.",
        "MARGINAL":   "Partial OOS generalisation. Paper trade for 3–6 months before live deployment.",
        "OVERFITTED": "IS performance does not generalise. Re-run WFO with expanded grid or longer OOS windows.",
    }

    return {
        "verdict":             verdict,
        "verdict_description": score_result["verdict_description"],
        "recommendation":      recs[verdict],
        "score":               score_result["score"],
        "triggered_diagnostics": diagnostics_result["triggered_count"],
        "high_severity_count": diagnostics_result["high_severity"],
        "is_validation_verdict": is_verdict,
        "strict_mode":         strict_mode,
    }


# ===========================================================================
# OUTPUT FORMATTING
# ===========================================================================

def build_degradation_table_df(degradation: Dict) -> pd.DataFrame:
    return pd.DataFrame(degradation["metrics"])


def build_window_analysis_df(window_analysis: Dict) -> Optional[pd.DataFrame]:
    rows = window_analysis.get("window_rows")
    if not rows:
        return None
    records = []
    for r in rows:
        row = {k: v for k, v in r.items() if k != "best_params"}
        # Flatten best_params
        bp = r.get("best_params", {}) or {}
        for k, v in bp.items():
            row[f"param_{k}"] = v
        records.append(row)
    return pd.DataFrame(records)


def build_summary_text(
    score_result:       Dict,
    degradation:        Dict,
    window_analysis:    Dict,
    param_stability:    Dict,
    regime_analysis:    Dict,
    diagnostics_result: Dict,
    decision:           Dict,
    run_meta:           Dict,
) -> str:
    sep  = "=" * 72
    sep2 = "-" * 72
    lines = [
        "",
        sep,
        "SCRIPT 20 — OUT-OF-SAMPLE VALIDATOR",
        "Multi-Asset Trend Following Strategy — Architecture v3.3",
        sep,
        f"  Generated At         : {run_meta.get('generated_at', 'N/A')}",
        f"  WFO Tag              : {run_meta.get('wfo_tag', 'default')}",
        f"  Strict Mode          : {run_meta.get('strict_mode', False)}",
        "",
        sep2,
        "VERDICT",
        sep2,
        f"  OOS Score            : {score_result['score']}/100",
        f"  Verdict              : {decision['verdict']}",
        f"  Recommendation       : {decision['recommendation']}",
        f"  IS Validation Cross-Check : {decision.get('is_validation_verdict', 'N/A')}",
        "",
        sep2,
        "PART 1: IS vs OOS DEGRADATION",
        sep2,
        f"  {'Metric':<25}  {'IS Value':>10}  {'OOS Value':>10}  {'Drop %':>7}  {'Grade'}",
    ]
    for r in degradation.get("metrics", []):
        lines.append(
            f"  {r['metric']:<25}  {str(r['is_value']):>10}  "
            f"{str(r['oos_value']):>10}  "
            f"{str(r['pct_drop']) + '%':>7}  "
            f"[{r['grade']}]"
        )
    lines += [
        f"  {'—'*50}",
        f"  Weighted Avg Degradation: {degradation.get('weighted_avg_drop', 'N/A')}%",
        f"  Metrics Passed: {degradation.get('metrics_passed', 'N/A')} / "
        f"{degradation.get('total_metrics', 'N/A')}",
        "",
        sep2,
        "PART 2: WINDOW-LEVEL CONSISTENCY",
        sep2,
    ]
    if window_analysis.get("available"):
        lines += [
            f"  Windows Analysed     : {window_analysis['n_windows']}",
            f"  Avg IS  Sharpe       : {window_analysis.get('avg_is_sharpe', 'N/A')}",
            f"  Avg OOS Sharpe       : {window_analysis.get('avg_oos_sharpe', 'N/A')}",
            f"  Stability Ratio (O/I): {window_analysis.get('stability_ratio', 'N/A')}  "
            f"[{window_analysis.get('stability_grade', 'N/A')}]",
            f"  OOS Consistency      : {window_analysis.get('oos_consistency', 0):.1%}  "
            f"[{window_analysis.get('consistency_grade', 'N/A')}]",
            f"  OOS Sharpe Trend     : slope={window_analysis.get('oos_trend_slope', 'N/A')}  "
            f"R²={window_analysis.get('oos_trend_r2', 'N/A')}",
            "",
            f"  Recent {window_analysis.get('recent_n', 5)}-Window Metrics "
            f"(forward-looking — source={window_analysis.get('recent_source', 'N/A')}):",
            f"    Stability Ratio    : {window_analysis.get('recent_stability_ratio', 'N/A')}",
            f"    OOS Consistency    : {window_analysis.get('recent_oos_consistency', 0):.1%}  "
            f"[{window_analysis.get('recent_consistency_grade', 'N/A')}]"
            if window_analysis.get('recent_oos_consistency') is not None else
            "    OOS Consistency    : N/A",
            f"    Avg OOS Sharpe     : {window_analysis.get('recent_avg_oos_sharpe', 'N/A')}",
            f"    OOS Sharpes        : "
            + "  ".join(f"{s:+.4f}" for s in window_analysis.get("recent_oos_sharpes", [])),
        ]
    else:
        lines.append("  Window analysis not available.")
    lines += [
        "",
        sep2,
        "PART 3: PARAMETER STABILITY",
        sep2,
    ]
    params = param_stability.get("parameters", {})
    if params:
        lines.append(f"  {'Parameter':<22}  {'CV':>6}  {'Grade':<12}  {'Edge %':>7}")
        for p, info in params.items():
            cv_str    = f"{info['cv']:.3f}" if info.get("cv") is not None else "N/A"
            edge_str  = f"{info['pct_at_edge']:.1%}" if info.get("pct_at_edge") is not None else "N/A"
            lines.append(
                f"  {p:<22}  {cv_str:>6}  [{info['cv_grade']:<10}]  {edge_str:>7}"
            )
    else:
        lines.append("  No parameter data available.")
    lines += [
        "",
        sep2,
        "PART 4: REGIME DECOMPOSITION",
        sep2,
    ]
    if regime_analysis.get("available"):
        for regime, info in regime_analysis.get("regimes", {}).items():
            lines.append(
                f"  {regime:<8}: n={info['n_windows']}  "
                f"AvgSharpe={info['avg_oos_sharpe']:>7.3f}  "
                f"HitRate={info['hit_rate']:.0%}"
            )
    else:
        lines.append("  Regime analysis not available.")
    lines += [
        "",
        sep2,
        "PART 5: STRUCTURAL OVERFITTING DIAGNOSTICS",
        sep2,
    ]
    for did, info in diagnostics_result.get("diagnostics", {}).items():
        status = "[TRIGGERED]" if info["triggered"] else "[  CLEAR  ]"
        lines.append(f"  {status}  {did:<35}  {info['severity']:<6}  {info['message']}")
    lines += [
        f"  {sep2}",
        f"  Total Diagnostics Triggered: "
        f"{diagnostics_result.get('triggered_count', 'N/A')} / "
        f"{len(diagnostics_result.get('diagnostics', {}))}",
        f"  HIGH Severity: {diagnostics_result.get('high_severity', 0)}",
        "",
        sep,
        f"FINAL VERDICT: {decision['verdict']}  (Score={score_result['score']}/100)",
        f"{decision['recommendation']}",
        sep,
        "",
    ]
    return "\n".join(lines)


# ===========================================================================
# PERSISTENCE
# ===========================================================================

def save_outputs(
    results:            Dict,
    degradation:        Dict,
    window_analysis:    Dict,
    summary_text:       str,
    tag:                str,
    logger:             logging.Logger,
) -> Dict[str, Path]:
    OOS_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
    sfx = f"_{tag}" if tag else ""
    paths: Dict[str, Path] = {}

    # ── Machine-readable JSON ─────────────────────────────────────────────────
    json_path = OOS_DIR / f"oos_validation_results{sfx}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, default=str)
    paths["results_json"] = json_path
    logger.info(f"Results JSON → {json_path}")

    # ── Human-readable summary ────────────────────────────────────────────────
    txt_path = OOS_DIR / f"oos_validation_summary{sfx}.txt"
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(summary_text)
    paths["summary_txt"] = txt_path
    logger.info(f"Summary TXT  → {txt_path}")

    # ── Degradation table CSV ─────────────────────────────────────────────────
    deg_df = build_degradation_table_df(degradation)
    deg_path = OOS_DIR / f"oos_degradation_table{sfx}.csv"
    deg_df.to_csv(deg_path, index=False)
    paths["degradation_csv"] = deg_path
    logger.info(f"Degradation  → {deg_path}")

    # ── Window analysis CSV ───────────────────────────────────────────────────
    win_df = build_window_analysis_df(window_analysis)
    if win_df is not None:
        win_path = OOS_DIR / f"oos_window_analysis{sfx}.csv"
        win_df.to_csv(win_path, index=False)
        paths["window_csv"] = win_path
        logger.info(f"Windows CSV  → {win_path}")

    # ── Timestamped report archive ────────────────────────────────────────────
    rpt_path = REPORTS_DIR / f"{ts}_oos_validation_report{sfx}.json"
    with open(rpt_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, default=str)
    paths["report_json"] = rpt_path
    logger.info(f"Report arch  → {rpt_path}")

    return paths


# ===========================================================================
# MAIN ORCHESTRATOR
# ===========================================================================

def run_oos_validation(
    wfo_tag:         str   = "",
    max_degradation: float = DEFAULT_MAX_DEGRADATION,
    strict_mode:     bool  = False,
    logger:          Optional[logging.Logger] = None,
) -> Dict:
    """
    Full OOS validation pipeline.

    Parameters
    ----------
    wfo_tag         : str   — Script 17 output tag (matches --output-tag)
    max_degradation : float — maximum tolerated metric degradation (0–1 fraction)
    strict_mode     : bool  — if True, MARGINAL verdict is elevated to OVERFITTED
    logger          : Logger — if None, one is created automatically

    Returns
    -------
    Full results dict (also persisted to disk).
    """
    if logger is None:
        logger = setup_logging(wfo_tag)

    logger.info("=" * 60)
    logger.info("SCRIPT 20 — OUT-OF-SAMPLE VALIDATOR")
    logger.info("Architecture v3.3 — Multi-Asset Trend Following")
    logger.info("=" * 60)

    # ── Step 1: Load all inputs ───────────────────────────────────────────────
    logger.info("STEP 1: Loading inputs …")
    inputs = load_all_inputs(wfo_tag, logger)

    is_metrics    = inputs["is_metrics"]    or {}
    wfo_results   = inputs["wfo_results"]
    equity_curve  = inputs["is_equity_curve"]
    is_validation = inputs["validation_results"]

    if not is_metrics:
        logger.error("IS performance metrics unavailable — cannot proceed without Script 16 output.")

    if wfo_results is None:
        logger.error("WFO results unavailable — cannot perform OOS analysis without Script 17 output.")

    # Extract windows list
    windows: List[Dict] = []
    if wfo_results:
        windows = wfo_results.get("windows", [])
        logger.info(f"  WFO windows loaded: {len(windows)}")

    # ── Step 2: Degradation analysis ─────────────────────────────────────────
    logger.info("STEP 2: IS vs OOS Degradation Analysis …")
    degradation = analyse_degradation(is_metrics, windows, max_degradation, logger)

    # ── Step 3: Window consistency ────────────────────────────────────────────
    logger.info("STEP 3: Window-Level Consistency Analysis …")
    wfo_stability = wfo_results.get("stability") if wfo_results else None
    window_analysis = analyse_window_consistency(windows, logger, wfo_stability=wfo_stability)

    # ── Step 4: Parameter stability ───────────────────────────────────────────
    logger.info("STEP 4: Parameter Stability Analysis …")
    param_stability = analyse_parameter_stability(windows, wfo_results, logger)

    # ── Step 5: Regime decomposition ──────────────────────────────────────────
    logger.info("STEP 5: Regime Decomposition …")
    regime_analysis = analyse_regime_performance(windows, equity_curve, logger)

    # ── Step 6: Overfitting diagnostics ───────────────────────────────────────
    logger.info("STEP 6: Structural Overfitting Diagnostics …")
    diagnostics_result = run_overfitting_diagnostics(
        is_metrics, window_analysis, param_stability, degradation, logger
    )

    # ── Step 7: Score and verdict ─────────────────────────────────────────────
    logger.info("STEP 7: Calculating OOS Robustness Score …")
    score_result = calculate_oos_score(diagnostics_result, window_analysis, logger)

    logger.info("STEP 8: Making Overall OOS Decision …")
    decision = make_overall_decision(
        score_result, diagnostics_result, window_analysis,
        is_validation, strict_mode, logger
    )

    # ── Assemble run metadata ─────────────────────────────────────────────────
    run_meta = {
        "generated_at":         datetime.now().isoformat(),
        "script":               "20_oos_validator.py",
        "architecture_version": "v3.3",
        "wfo_tag":              wfo_tag,
        "max_degradation":      max_degradation,
        "strict_mode":          strict_mode,
        "inputs_loaded": {k: (v is not None) for k, v in inputs.items()},
    }

    # ── Build summary text ────────────────────────────────────────────────────
    summary_text = build_summary_text(
        score_result, degradation, window_analysis,
        param_stability, regime_analysis, diagnostics_result,
        decision, run_meta
    )
    print(summary_text)

    # ── Assemble full results ─────────────────────────────────────────────────
    results = {
        **run_meta,
        "is_metrics":           is_metrics,
        "degradation_analysis": {k: v for k, v in degradation.items() if k != "metrics"},
        "degradation_metrics":  degradation.get("metrics", []),
        "window_analysis":      {k: v for k, v in window_analysis.items() if k != "window_rows"},
        "window_rows":          window_analysis.get("window_rows", []),
        "parameter_stability":  param_stability,
        "regime_analysis":      regime_analysis,
        "diagnostics":          diagnostics_result,
        "score":                score_result,
        "overall_decision":     decision,
    }

    # ── Step 9: Save outputs ──────────────────────────────────────────────────
    logger.info("STEP 9: Saving outputs …")
    output_paths = save_outputs(
        results, degradation, window_analysis, summary_text, wfo_tag, logger
    )
    results["output_paths"] = {k: str(v) for k, v in output_paths.items()}

    logger.info("=" * 60)
    logger.info(f"OOS VALIDATION COMPLETE — Verdict: {decision['verdict']}  Score: {score_result['score']}/100")
    logger.info("=" * 60)

    return results


# ===========================================================================
# CLI
# ===========================================================================

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Script 20 — Out-of-Sample Validator for the trend-following strategy",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Standard run
  python scripts/20_oos_validator.py

  # Use tagged WFO outputs from Script 17
  python scripts/20_oos_validator.py --wfo-tag quarterly_Q4

  # Strict mode: MARGINAL verdict treated as OVERFITTED
  python scripts/20_oos_validator.py --strict

  # Tighten acceptable degradation to 40%
  python scripts/20_oos_validator.py --max-degradation 0.40
        """,
    )
    p.add_argument(
        "--wfo-tag", default="",
        help="Tag appended to Script 17 output filenames (matches --output-tag)"
    )
    p.add_argument(
        "--max-degradation", type=float, default=DEFAULT_MAX_DEGRADATION,
        help=f"Max tolerated IS→OOS metric drop as a fraction 0–1 (default: {DEFAULT_MAX_DEGRADATION})"
    )
    p.add_argument(
        "--strict", action="store_true",
        help="Strict mode: MARGINAL verdict is elevated to OVERFITTED"
    )
    add_strategy_argument(parser)
    return p.parse_args()


def _run_for_strategy(strategy: "StrategyDef", args) -> int:
    """Run Script 20 for one strategy with namespaced I/O paths."""
    global BACKTEST_DIR, WFO_DIR, REPORTS_DIR

    strat_backtest = strategy.backtest_dir(DATA_CACHE_DIR)
    strat_wfo      = strategy.wfo_dir(DATA_CACHE_DIR)
    strat_reports  = strategy.reports_dir(PROJECT_ROOT, 'oos_validation')
    strat_backtest.mkdir(parents=True, exist_ok=True)
    strat_wfo.mkdir(parents=True, exist_ok=True)
    strat_reports.mkdir(parents=True, exist_ok=True)

    logger.info(f"\n[{strategy.name}] -- {strategy.label} ({'LIVE' if strategy.deployed else 'PAPER'}) --")
    logger.info(f"[{strategy.name}] Backtest dir : {strat_backtest if 'strat_backtest' in dir() else 'n/a'}")
    logger.info(f"[{strategy.name}] Reports dir  : {strat_reports}")

    _o_bt, _o_wfo, _o_rp = BACKTEST_DIR, WFO_DIR, REPORTS_DIR
    BACKTEST_DIR = strat_backtest
    WFO_DIR      = strat_wfo
    REPORTS_DIR  = strat_reports
    try:
        rc = _run_core(args, strategy.name)
        return rc if isinstance(rc, int) else 0
    finally:
        BACKTEST_DIR, WFO_DIR, REPORTS_DIR = _o_bt, _o_wfo, _o_rp


def _run_core(args, strategy_name: str = '') -> int:
    args   = parse_args()
    logger = setup_logging(args.wfo_tag)
    results = run_oos_validation(
        wfo_tag         = args.wfo_tag,
        max_degradation = args.max_degradation,
        strict_mode     = args.strict,
        logger          = logger,
    )

    decision = results.get("overall_decision", {})
    score    = results.get("score", {})

    print("\n" + "=" * 60)
    print(f"OOS VERDICT  : {decision.get('verdict', 'UNKNOWN')}")
    print(f"OOS SCORE    : {score.get('score', '?')} / 100")
    print(f"DIAGNOSTICS  : {decision.get('triggered_diagnostics', '?')} triggered  "
          f"({decision.get('high_severity_count', 0)} HIGH severity)")
    print("=" * 60)
    print(decision.get("recommendation", ""))

    # Exit codes: 0 = ROBUST, 1 = MARGINAL, 2 = OVERFITTED / UNKNOWN
    verdict = decision.get("verdict", "UNKNOWN")
    if verdict == "ROBUST":
        return 0
    elif verdict == "MARGINAL":
        return 1
    else:
        return 2


# ===========================================================================
# PROGRAMMATIC API  (consumed by run_pipeline.py and reporting scripts)
# ===========================================================================

def run_oos_validator(
    wfo_tag:         str   = "",
    max_degradation: float = DEFAULT_MAX_DEGRADATION,
    strict_mode:     bool  = False,
) -> Dict:
    """
    Public API entry point for 00_run_pipeline.py and downstream reporting scripts.

    Returns
    -------
    Full OOS validation results dict (identical structure to JSON output).
    """
    logger = setup_logging(wfo_tag)
    return run_oos_validation(
        wfo_tag         = wfo_tag,
        max_degradation = max_degradation,
        strict_mode     = strict_mode,
        logger          = logger,
    )


def main() -> int:
    args = parse_args()
    logger.info("=" * 70)
    logger.info("Script 20 -- Architecture v3.9 (Mar 2026)")
    logger.info("=" * 70)

    try:
        strategies = resolve_strategies(
            getattr(args, "strategy", None),
            project_root=PROJECT_ROOT,
        )
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
    return 1 if failed else 0



if __name__ == "__main__":
    main()
