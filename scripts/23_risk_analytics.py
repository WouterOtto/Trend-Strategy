#!/usr/bin/env python3
"""
Script 23: Risk Analytics
=========================
Compute comprehensive risk metrics for the live trend-following portfolio.

Purpose:
    This script is the primary risk measurement tool for the strategy.  It
    answers six fundamental risk questions:

        1. HOW MUCH CAN WE LOSE?  (VaR / CVaR at 95% and 99%)
        2. HOW CORRELATED ARE WE TO THE MARKET?  (Beta vs SPY / ACWI)
        3. HOW DIFFERENT ARE WE FROM THE BENCHMARK?  (Tracking Error / IR)
        4. HOW CONCENTRATED ARE WE?  (Herfindahl-Hirschman Index)
        5. HOW CORRELATED ARE OUR POSITIONS?  (Pairwise correlation matrix)
        6. WHAT FACTOR RISKS DO WE CARRY?  (Size, Value, Momentum, Quality)

    Risk metrics are computed using 252 trading days of daily returns
    (one calendar year of data) as the primary lookback window.  Shorter
    63-day (quarterly) windows are also provided for recency weighting.

Methodology:
    ─────────────────────────────────────────────────────────────────
    VaR   (Historical Simulation)
          VaR_p = Portfolio_Value × |Percentile(R_daily, 1-p)|
          Scaled to T-day horizon via square-root-of-time: VaR_T = VaR_1 × √T
          Confidence levels: 95%, 99%
          Horizons: 1-day, 5-day (week), 20-day (month)

    CVaR  (Expected Shortfall)
          CVaR_p = –E[R_daily | R_daily < –VaR_p/V]
          Mean of all daily returns below the VaR threshold.

    Beta  OLS regression: R_portfolio = α + β × R_benchmark + ε
          β = Cov(R_p, R_b) / Var(R_b)

    Tracking Error
          TE_daily = StdDev(R_portfolio – R_benchmark)
          TE_ann   = TE_daily × √252

    Information Ratio
          IR = Active_Return_ann / TE_ann
          where Active_Return_ann = (Portfolio_Return – Benchmark_Return)
                                    annualised from the lookback window

    Herfindahl-Hirschman Index (Concentration)
          HHI = Σ w_i²   (sum of squared portfolio weights)
          Effective N = 1 / HHI   (equivalent equal-weight positions)

    Factor Exposures  (proxy-based, no external data required)
          Size:     portfolio weighted-avg log(market_cap)  vs benchmark log(market_cap)
          Momentum: portfolio weighted-avg momentum_score   (from Script 07)
          Quality:  portfolio weighted-avg sector proxy     (healthcare/tech ↑, energy ↓)
          Value:    N/A without P/E data → flagged as "unavailable"
    ─────────────────────────────────────────────────────────────────

Inputs:
    data/portfolio_state.json                       (Script 13)
    data/trade_ledger.jsonl                         (Script 13)
    data_cache/consolidated/{symbol}.parquet        (Script 03)
    data_cache/fundamentals/company_info.json       (Script 02)

Outputs:
    data/performance/risk/{date}_risk_metrics.json
    reports/performance/risk_{YYYY-MM-DD}.pdf
    reports/performance/risk_{YYYY-MM-DD}.csv
    logs/risk_analytics_{timestamp}.log

CLI Reference:
    --as-of         YYYY-MM-DD  Valuation date (default: today)
    --lookback      INT         Trading days for return history (default: 252)
    --benchmark     SPY.US | ACWI.US  (default: SPY.US)
    --account-equity FLOAT      Override portfolio equity in EUR
    --top-n-corr    INT         Positions in correlation matrix (default: 10)
    --dry-run                   Compute and print; do not write output files
    --no-pdf                    Skip PDF generation (faster)
    --demo                      Generate synthetic portfolio data and run a full
                                demonstration without any upstream files

Execution:
    # Standard daily risk run
    python scripts/23_risk_analytics.py

    # End-of-month risk snapshot
    python scripts/23_risk_analytics.py --as-of 2026-01-31

    # Quick check, no files written
    python scripts/23_risk_analytics.py --dry-run

    # Full demo without any data pipeline files
    python scripts/23_risk_analytics.py --demo

Architecture: v3.2 (Feb 2026)
"""

import sys
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
from config.strategies import resolve_strategies, add_strategy_argument, StrategyDef
import json
import logging
import argparse
import math
from dataclasses import dataclass, field, asdict
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from scipy import stats as scipy_stats

# ReportLab for PDF generation (optional dependency)
try:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        HRFlowable, KeepTogether, PageBreak, Paragraph,
        SimpleDocTemplate, Spacer, Table, TableStyle,
    )
    REPORTLAB_AVAILABLE = True
except ImportError:
    REPORTLAB_AVAILABLE = False

# ============================================================================
# PATH CONFIGURATION
# ============================================================================

PROJECT_ROOT      = Path(__file__).parent.parent
DATA_DIR          = PROJECT_ROOT / "data"
DATA_CACHE_DIR    = PROJECT_ROOT / "data_cache"
DATA_LOAD_DIR     = PROJECT_ROOT.parent / "data_load" / "data_cache"
CONSOLIDATED_DIR  = DATA_LOAD_DIR / "consolidated"
FUNDAMENTALS_DIR  = DATA_LOAD_DIR / "fundamentals"
INDICATORS_DIR    = DATA_CACHE_DIR / "indicators"
RISK_DIR          = DATA_DIR / "performance" / "risk"
REPORTS_DIR       = PROJECT_ROOT / "reports" / "performance"
LOG_DIR           = PROJECT_ROOT / "logs"

PORTFOLIO_STATE_FILE = DATA_DIR / "portfolio_state.json"
TRADE_LEDGER_FILE    = DATA_DIR / "trade_ledger.jsonl"
COMPANY_INFO_FILE    = FUNDAMENTALS_DIR / "company_info.json"

# ============================================================================
# DESIGN TOKENS  (matches Script 12 / 22 palette)
# ============================================================================

C_NAVY    = colors.HexColor("#1B2A47") if REPORTLAB_AVAILABLE else None
C_ACCENT  = colors.HexColor("#2E86DE") if REPORTLAB_AVAILABLE else None
C_SUCCESS = colors.HexColor("#27AE60") if REPORTLAB_AVAILABLE else None
C_DANGER  = colors.HexColor("#E74C3C") if REPORTLAB_AVAILABLE else None
C_ORANGE  = colors.HexColor("#E67E22") if REPORTLAB_AVAILABLE else None
C_WARN    = colors.HexColor("#F39C12") if REPORTLAB_AVAILABLE else None
C_LIGHT   = colors.HexColor("#F8F9FA") if REPORTLAB_AVAILABLE else None
C_WHITE   = colors.white               if REPORTLAB_AVAILABLE else None

# ============================================================================
# CONSTANTS
# ============================================================================

TRADING_DAYS_PER_YEAR = 252
RISK_FREE_RATE_ANN    = 0.02   # 2% annual; ECB main rate proxy
BENCHMARK_NAMES: Dict[str, str] = {
    "SPY.US":    "S&P 500 (SPY)",
    "SPY.NYSE":  "S&P 500 (SPY)",
    "SPY.NASDAQ":"S&P 500 (SPY)",
    "ACWI.US":   "MSCI ACWI (ACWI)",
    "ACWI.NYSE": "MSCI ACWI (ACWI)",
    "ACWI.NASDAQ":"MSCI ACWI (ACWI)",
    "QQQ.US":    "NASDAQ 100 (QQQ)",
    "QQQ.NASDAQ":"NASDAQ 100 (QQQ)",
}

# HHI interpretation thresholds
HHI_DIVERSIFIED   = 0.05   # ≤ 20 equal-weight positions
HHI_MODERATE      = 0.10   # ≤ 10 equal-weight positions
# > HHI_MODERATE   → High concentration

# VaR alert thresholds (as fraction of portfolio value)
VAR_95_WARN_PCT  = 0.03   # Warn if 1-day 95% VaR > 3% of portfolio
VAR_99_WARN_PCT  = 0.05   # Warn if 1-day 99% VaR > 5% of portfolio

# Correlation alert threshold
CORR_HIGH = 0.80           # Pairwise correlation considered "high"

# Symbol exchange suffixes used by the pipeline ({TICKER}.{EXCHANGE_CODE}).
# Both the full exchange-code format (NASDAQ/XETRA/LSE) and legacy .US/.DE/.L
# variants are listed so the code works regardless of which convention is used.
SUFFIX_TO_GEO: Dict[str, str] = {
    ".NYSE":   "US",
    ".NASDAQ": "US",
    ".US":     "US",
    ".XETRA":  "EU",
    ".PA":     "EU",
    ".AS":     "EU",
    ".DE":     "EU",
    ".LSE":    "UK",
    ".L":      "UK",
    ".CC":     "Crypto",
    ".V":      "Crypto",
}

# Benchmark symbols excluded from portfolio metrics.
# Include both legacy .US format and exchange-code variants.
BENCHMARK_SYMBOLS = {
    "SPY.US",  "SPY.NYSE",  "SPY.NASDAQ",
    "ACWI.US", "ACWI.NYSE", "ACWI.NASDAQ",
    "QQQ.US",  "QQQ.NASDAQ",
    "IWM.US",  "IWM.NYSE",
    "AGG.US",  "AGG.NYSE",
}

# Candidate benchmark filenames in order of lookup preference.
# The pipeline stores ETFs as {TICKER}.{EXCHANGE_CODE}, so SPY lives at
# SPY.NYSE.parquet or SPY.NASDAQ.parquet, not SPY.US.parquet.
BENCHMARK_CANDIDATES: Dict[str, List[str]] = {
    "SPY.US":   ["SPY.NYSE",  "SPY.NASDAQ",  "SPY.US"],
    "ACWI.US":  ["ACWI.NASDAQ", "ACWI.NYSE", "ACWI.US"],
    "QQQ.US":   ["QQQ.NASDAQ", "QQQ.NYSE",   "QQQ.US"],
    "ACWI.NYSE":["ACWI.NYSE", "ACWI.NASDAQ", "ACWI.US"],
}

# Quality proxy: sector → quality score (0-10; higher = higher quality)
SECTOR_QUALITY_SCORE: Dict[str, float] = {
    "Technology":             8.5,
    "Healthcare":             8.0,
    "Consumer Defensive":     7.5,
    "Financial Services":     6.5,
    "Industrials":            6.5,
    "Communication Services": 6.0,
    "Consumer Cyclical":      5.5,
    "Basic Materials":        4.5,
    "Real Estate":            4.5,
    "Energy":                 4.0,
    "Utilities":              4.0,
    "Unknown":                5.0,
}

# ============================================================================
# LOGGING
# ============================================================================

logger = logging.getLogger("risk_analytics")


def setup_logging(as_of_label: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = LOG_DIR / f"risk_analytics_{as_of_label}_{ts}.log"
    fmt      = "%(asctime)s [%(levelname)s] %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


# ============================================================================
# DATA STRUCTURES
# ============================================================================

@dataclass
class VaRResult:
    """VaR and CVaR at a specific confidence level and horizon."""
    confidence_pct:   float     # e.g. 95.0 or 99.0
    horizon_days:     int       # 1, 5, or 20
    var_eur:          float = 0.0   # Value at Risk (EUR)
    var_pct:          float = 0.0   # VaR as % of portfolio
    cvar_eur:         float = 0.0   # Conditional VaR / Expected Shortfall
    cvar_pct:         float = 0.0   # CVaR as % of portfolio
    tail_obs:         int   = 0     # Number of daily returns in tail
    is_alert:         bool  = False # Exceeds warning threshold

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BenchmarkMetrics:
    """Beta, tracking error, and information ratio vs a benchmark."""
    benchmark_symbol:       str   = "SPY.US"
    benchmark_name:         str   = "S&P 500 (SPY)"
    lookback_days:          int   = 252

    # Returns over the lookback window
    portfolio_return_pct:   float = 0.0
    benchmark_return_pct:   float = 0.0
    active_return_pct:      float = 0.0   # portfolio − benchmark

    # Regression
    beta:                   float = 0.0
    alpha_ann_pct:          float = 0.0   # Jensen's alpha, annualised
    r_squared:              float = 0.0

    # Volatility
    portfolio_vol_ann_pct:  float = 0.0
    benchmark_vol_ann_pct:  float = 0.0
    tracking_error_ann_pct: float = 0.0
    information_ratio:      float = 0.0

    # Sharpe
    sharpe_ratio:           float = 0.0   # portfolio
    benchmark_sharpe:       float = 0.0

    # Data quality
    shared_trading_days:    int   = 0


@dataclass
class ConcentrationMetrics:
    """Herfindahl-Hirschman Index and related concentration diagnostics."""
    hhi:                    float = 0.0   # raw HHI (sum of w²)
    effective_n:            float = 0.0   # 1 / HHI equivalent positions
    top1_weight_pct:        float = 0.0
    top3_weight_pct:        float = 0.0
    top5_weight_pct:        float = 0.0
    position_count:         int   = 0
    regime:                 str   = "Unknown"   # Diversified / Moderate / High
    is_alert:               bool  = False


@dataclass
class CorrelationResult:
    """Pairwise correlation matrix summary for top-N positions."""
    symbols:                List[str]             = field(default_factory=list)
    matrix:                 List[List[float]]     = field(default_factory=list)   # row-major
    avg_pairwise_corr:      float = 0.0
    max_pairwise_corr:      float = 0.0
    high_corr_pairs:        List[Tuple[str, str, float]] = field(default_factory=list)
    lookback_days:          int   = 63
    is_alert:               bool  = False


@dataclass
class FactorExposure:
    """Proxy factor exposures for the current portfolio."""
    # Size factor (log market cap; positive = large-cap tilt)
    size_score:             float = 0.0   # portfolio weighted avg log(mktcap)
    size_label:             str   = "Unknown"

    # Momentum factor (momentum score from Script 07 / price vs SMA200)
    momentum_score_avg:     float = 0.0
    momentum_label:         str   = "Unknown"

    # Quality factor (sector-proxy quality score)
    quality_score_avg:      float = 0.0
    quality_label:          str   = "Unknown"

    # Value factor
    value_available:        bool  = False
    value_note:             str   = "P/E data not available; use Script 02 PE ratio when present"

    # Geographic tilt
    us_weight_pct:          float = 0.0
    eu_weight_pct:          float = 0.0
    crypto_weight_pct:      float = 0.0
    other_weight_pct:       float = 0.0


@dataclass
class DrawdownMetrics:
    """Rolling drawdown and underwater statistics."""
    lookback_days:          int   = 252
    max_drawdown_pct:       float = 0.0
    current_drawdown_pct:   float = 0.0
    avg_drawdown_pct:       float = 0.0
    longest_dd_days:        int   = 0   # longest drawdown duration in trading days
    recovery_days:          Optional[int] = None   # None if still in drawdown
    calmar_ratio:           float = 0.0   # annualised return / |max drawdown|


@dataclass
class RiskReport:
    """Complete risk analytics report."""
    as_of_date:             str
    generated_at:           str  = field(default_factory=lambda: datetime.now().isoformat())
    lookback_days:          int  = 252
    account_equity_eur:     float = 0.0
    position_count:         int   = 0

    # Individual metric blocks
    var_results:            List[VaRResult]   = field(default_factory=list)
    benchmark_metrics:      Optional[BenchmarkMetrics] = None
    concentration:          Optional[ConcentrationMetrics] = None
    correlation:            Optional[CorrelationResult] = None
    factor_exposure:        Optional[FactorExposure] = None
    drawdown:               Optional[DrawdownMetrics] = None

    # Alert summary
    alerts:                 List[str] = field(default_factory=list)
    risk_score:             int  = 0    # 0-10 composite risk score (10 = highest risk)
    risk_label:             str  = "Unknown"

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


# ============================================================================
# DATA LOADING HELPERS
# ============================================================================

def load_portfolio_state() -> dict:
    """
    Load current portfolio state from JSON and normalise to a consistent format.

    Script 13 writes portfolio_state.json as a FLAT dict:
        { "AAPL.US": {entry_price, shares, current_value, ...}, "MSFT.US": {...} }

    Some wrapper scripts may nest it under a "positions" key.  This function
    handles both layouts and always returns:
        { "positions": { SYMBOL: {...} } }
    """
    if not PORTFOLIO_STATE_FILE.exists():
        raise FileNotFoundError(
            f"Portfolio state not found: {PORTFOLIO_STATE_FILE}\n"
            "Run Script 13 (Execution Logger) to initialise portfolio state."
        )
    with open(PORTFOLIO_STATE_FILE, "r", encoding="utf-8") as fh:
        raw = json.load(fh)

    if not isinstance(raw, dict):
        raise ValueError("portfolio_state.json must be a JSON object.")

    # Script 13 writes a flat {SYMBOL: {...}} dict.
    # Detect nested format (some wrappers add a "positions" key).
    if "positions" in raw and isinstance(raw.get("positions"), dict):
        positions = {k: v for k, v in raw["positions"].items() if not k.startswith("_")}
    else:
        # Flat format: every key that is not a metadata key is a symbol
        positions = {k: v for k, v in raw.items()
                     if not k.startswith("_") and isinstance(v, dict)}

    logger.info("Loaded portfolio state: %d positions", len(positions))
    return {"positions": positions}


def load_company_info() -> dict:
    """Load company fundamentals (sector, market cap, etc.)."""
    if not COMPANY_INFO_FILE.exists():
        logger.warning("company_info.json not found; factor exposures will be partial.")
        return {}
    with open(COMPANY_INFO_FILE, "r", encoding="utf-8") as fh:
        info = json.load(fh)
    return info


def load_latest_sma200(symbol: str, as_of: date) -> Optional[float]:
    """
    Return the most recent sma_slow value for a symbol from the indicators parquet.

    Script 05 writes: data_cache/indicators/{symbol}_indicators.parquet
    with columns including sma_slow (indexed by date, same as consolidated/).

    Returns None if the indicators file is unavailable or sma_slow is NaN.
    """
    ind_path = INDICATORS_DIR / f"{symbol}_indicators.parquet"
    if not ind_path.exists():
        return None
    try:
        df = pd.read_parquet(ind_path, columns=["sma_slow"])
        if not isinstance(df.index, pd.DatetimeIndex):
            df.index = pd.to_datetime(df.index)
        df.index = df.index.date
        df = df[df.index <= as_of].sort_index()
        if df.empty:
            return None
        val = df["sma_slow"].dropna()
        return float(val.iloc[-1]) if not val.empty else None
    except Exception as exc:
        logger.debug("  Could not load sma_slow for %s: %s", symbol, exc)
        return None


def load_price_series(symbol: str, as_of: date, lookback: int) -> Optional[pd.Series]:
    """
    Load daily adjusted-close prices for a symbol from consolidated parquet.

    Script 03 writes parquet with DATE as the index (via .set_index("date")).
    The preferred price column is "adjusted_close" (EODHD-adjusted for splits /
    dividends); "close" is used as a fallback.

    Returns a pd.Series indexed by date.date objects (ascending), truncated to
    the lookback window ending on as_of.  Returns None if data unavailable.
    """
    parquet_path = CONSOLIDATED_DIR / f"{symbol}.parquet"
    if not parquet_path.exists():
        logger.warning("  No parquet for %s; skipping.", symbol)
        return None

    try:
        # Date is the index — do NOT request it as a column.
        df = pd.read_parquet(parquet_path)

        # Normalise index to date objects
        if not isinstance(df.index, pd.DatetimeIndex):
            df.index = pd.to_datetime(df.index)
        df.index = df.index.date          # convert to date objects

        df = df.sort_index()

        # Prefer adjusted_close (split/dividend corrected); fallback to close
        if "adjusted_close" in df.columns:
            price_col = "adjusted_close"
        elif "close" in df.columns:
            price_col = "close"
        else:
            logger.warning("  %s: no close/adjusted_close column in parquet.", symbol)
            return None

        # Truncate to lookback window (add buffer for weekends / holidays)
        start_cutoff = as_of - timedelta(days=int(lookback * 1.6))
        mask = (df.index <= as_of) & (df.index >= start_cutoff)
        series = df.loc[mask, price_col].dropna()

        if series.empty:
            return None

        # Keep only the last `lookback` trading-day observations
        return series.iloc[-lookback:] if len(series) > lookback else series
    except Exception as exc:
        logger.warning("  Failed to load %s: %s", symbol, exc)
        return None


def compute_daily_returns(prices: pd.Series) -> pd.Series:
    """Compute log returns from a price series."""
    return np.log(prices / prices.shift(1)).dropna()


# ============================================================================
# PORTFOLIO RETURN RECONSTRUCTION
# ============================================================================

def _get_current_market_value(symbol: str, pos: dict, as_of: date, lookback: int) -> float:
    """
    Compute current mark-to-market position value.

    Script 13 stores "current_value" = fill_price × shares at entry time (stale).
    We recompute using: shares × latest_adjusted_close from parquet.
    Falls back to entry-value if parquet is unavailable.
    """
    shares = pos.get("shares", 0)
    if shares <= 0:
        return 0.0

    prices = load_price_series(symbol, as_of, lookback)
    if prices is not None and len(prices) > 0:
        latest_price = float(prices.iloc[-1])
        return shares * latest_price

    # Fallback: use entry_price × shares (stale but non-zero)
    entry_price = pos.get("entry_price", 0.0)
    if entry_price > 0:
        logger.debug("  %s: parquet unavailable, using entry_price×shares for weight.", symbol)
        return shares * entry_price

    # Last resort: raw current_value field from Script 13
    return pos.get("current_value", 0.0)


def build_portfolio_returns(
    positions: Dict[str, dict],
    as_of: date,
    lookback: int,
) -> Tuple[pd.Series, Dict[str, pd.Series]]:
    """
    Reconstruct daily portfolio returns from constituent price history.

    Each position is weighted by its CURRENT MARK-TO-MARKET value weight.
    The portfolio return on day t is:
        R_p[t] = Σ_i  w_i × R_i[t]

    where w_i = (shares_i × price_i_today) / Σ_j (shares_j × price_j_today)

    Script 13 stores "current_value" = fill_price × shares at ENTRY (stale).
    We recompute market values from the latest adjusted_close in parquet.

    Returns:
        portfolio_returns:  pd.Series of daily portfolio log-returns
        constituent_returns: dict {symbol: pd.Series} of individual log-returns
    """
    if not positions:
        return pd.Series(dtype=float), {}

    constituent_returns: Dict[str, pd.Series] = {}
    market_values:       Dict[str, float]     = {}

    for symbol, pos in positions.items():
        if symbol in BENCHMARK_SYMBOLS:
            continue
        prices = load_price_series(symbol, as_of, lookback)
        if prices is None or len(prices) < 5:
            continue
        rets = compute_daily_returns(prices)
        if rets.empty:
            continue
        # Compute current market value = shares × latest price
        mv = _get_current_market_value(symbol, pos, as_of, lookback)
        if mv <= 0:
            logger.debug("  %s: market value is zero; skipping from portfolio returns.", symbol)
            continue
        constituent_returns[symbol] = rets
        market_values[symbol] = mv

    if not constituent_returns:
        logger.warning("No constituent return data available.")
        return pd.Series(dtype=float), {}

    total_value = sum(market_values.values())
    if total_value <= 0:
        logger.warning("Total portfolio market value is zero; cannot compute returns.")
        return pd.Series(dtype=float), {}

    weights = {sym: mv / total_value for sym, mv in market_values.items()}

    # Align all return series to common dates
    df = pd.DataFrame(constituent_returns)
    df = df.dropna(how="all")

    # Weighted portfolio return per day (re-normalise if symbols missing on a day)
    port_returns = pd.Series(0.0, index=df.index)
    weight_sums  = pd.Series(0.0, index=df.index)

    for sym, w in weights.items():
        if sym not in df.columns:
            continue
        col   = df[sym]
        valid = col.notna()
        port_returns[valid] += w * col[valid]
        weight_sums[valid]  += w

    valid_weights = weight_sums > 0
    port_returns[valid_weights] /= weight_sums[valid_weights]
    port_returns = port_returns[valid_weights]

    logger.info(
        "Portfolio returns: %d trading days, %d symbols, total MtM value: €%.0f",
        len(port_returns), len(weights), total_value,
    )
    return port_returns, constituent_returns


# ============================================================================
# VAR / CVAR
# ============================================================================

def compute_var_cvar(
    portfolio_returns: pd.Series,
    portfolio_value_eur: float,
    confidence_pct: float,
    horizon_days: int,
) -> VaRResult:
    """
    Historical simulation VaR and CVaR.

    VaR_1 = portfolio_value × |q_{1-p}(R_daily)|
    VaR_T = VaR_1 × √T  (square-root-of-time scaling)
    CVaR  = portfolio_value × |mean(R | R < q_{1-p})|
    """
    if portfolio_returns.empty or portfolio_value_eur <= 0:
        return VaRResult(confidence_pct=confidence_pct, horizon_days=horizon_days)

    alpha      = 1.0 - (confidence_pct / 100.0)  # e.g. 0.05 for 95% VaR
    r          = portfolio_returns.values

    # 1-day VaR
    var_threshold = np.percentile(r, alpha * 100)          # typically negative
    var_1d_pct    = abs(min(var_threshold, 0.0))            # flip sign
    var_Td_pct    = var_1d_pct * math.sqrt(horizon_days)   # scale to horizon

    var_eur = portfolio_value_eur * var_Td_pct

    # CVaR (Expected Shortfall): mean of returns WORSE than VaR threshold
    tail_returns = r[r < var_threshold]
    tail_obs     = len(tail_returns)
    cvar_1d_pct  = abs(tail_returns.mean()) if tail_obs > 0 else var_1d_pct
    cvar_Td_pct  = cvar_1d_pct * math.sqrt(horizon_days)
    cvar_eur     = portfolio_value_eur * cvar_Td_pct

    # Alert check (only for 1-day horizon)
    is_alert = False
    if horizon_days == 1:
        threshold = VAR_95_WARN_PCT if confidence_pct == 95.0 else VAR_99_WARN_PCT
        is_alert = var_Td_pct > threshold

    return VaRResult(
        confidence_pct=confidence_pct,
        horizon_days=horizon_days,
        var_eur=round(var_eur, 2),
        var_pct=round(var_Td_pct * 100, 4),
        cvar_eur=round(cvar_eur, 2),
        cvar_pct=round(cvar_Td_pct * 100, 4),
        tail_obs=tail_obs,
        is_alert=is_alert,
    )


def compute_all_var(
    portfolio_returns: pd.Series,
    portfolio_value_eur: float,
) -> List[VaRResult]:
    """Compute VaR/CVaR for all standard confidence levels and horizons."""
    results = []
    for conf in [95.0, 99.0]:
        for horizon in [1, 5, 20]:
            results.append(
                compute_var_cvar(portfolio_returns, portfolio_value_eur, conf, horizon)
            )
    return results


# ============================================================================
# BETA / ALPHA / TRACKING ERROR / INFORMATION RATIO
# ============================================================================

def _resolve_benchmark_prices(benchmark_symbol: str, as_of: date, lookback: int) -> Optional[pd.Series]:
    """
    Load benchmark price series, probing multiple candidate filenames.

    The pipeline stores ETFs as {TICKER}.{EXCHANGE_CODE} (e.g. SPY.NYSE),
    not {TICKER}.US.  We probe the BENCHMARK_CANDIDATES list in order and
    return the first series found with sufficient history.
    """
    candidates = BENCHMARK_CANDIDATES.get(benchmark_symbol, [benchmark_symbol])
    # Always add the symbol itself as a final fallback
    if benchmark_symbol not in candidates:
        candidates = list(candidates) + [benchmark_symbol]

    for candidate in candidates:
        prices = load_price_series(candidate, as_of, lookback)
        if prices is not None and len(prices) >= 20:
            if candidate != benchmark_symbol:
                logger.info(
                    "Benchmark '%s' resolved via candidate '%s'.", benchmark_symbol, candidate
                )
            return prices

    logger.warning(
        "Benchmark '%s': no price data found. Tried: %s", benchmark_symbol, candidates
    )
    return None


def compute_benchmark_metrics(
    portfolio_returns: pd.Series,
    benchmark_symbol: str,
    as_of: date,
    lookback: int,
) -> BenchmarkMetrics:
    """
    OLS regression of portfolio daily returns against benchmark daily returns.

    β   = Cov(R_p, R_b) / Var(R_b)
    α   = mean(R_p) – β × mean(R_b)      [daily]
    α_ann = α × 252
    TE_daily = std(R_p – R_b)
    TE_ann   = TE_daily × √252
    IR  = Active_Return_ann / TE_ann

    The benchmark is resolved via BENCHMARK_CANDIDATES to handle the pipeline's
    {TICKER}.{EXCHANGE_CODE} naming convention (e.g. SPY.NYSE not SPY.US).
    """
    bm_name = BENCHMARK_NAMES.get(benchmark_symbol, benchmark_symbol)
    result   = BenchmarkMetrics(benchmark_symbol=benchmark_symbol, benchmark_name=bm_name,
                                lookback_days=lookback)

    if portfolio_returns.empty:
        return result

    bm_prices = _resolve_benchmark_prices(benchmark_symbol, as_of, lookback)
    if bm_prices is None or len(bm_prices) < 5:
        logger.warning("Benchmark %s: insufficient price data after probing all candidates.", benchmark_symbol)
        return result

    bm_returns = compute_daily_returns(bm_prices)

    # Align on common dates
    common = portfolio_returns.index.intersection(bm_returns.index)
    if len(common) < 20:
        logger.warning("Only %d common trading days with benchmark; skipping.", len(common))
        return result

    Rp = portfolio_returns.loc[common].values
    Rb = bm_returns.loc[common].values

    result.shared_trading_days = len(common)

    # OLS
    slope, intercept, r_value, _p, _se = scipy_stats.linregress(Rb, Rp)
    result.beta       = round(slope, 4)
    result.r_squared  = round(r_value**2, 4)
    result.alpha_ann_pct = round(intercept * TRADING_DAYS_PER_YEAR * 100, 4)

    # Cumulative returns over the window
    result.portfolio_return_pct = round((np.exp(Rp.sum()) - 1) * 100, 4)
    result.benchmark_return_pct = round((np.exp(Rb.sum()) - 1) * 100, 4)
    result.active_return_pct    = round(
        result.portfolio_return_pct - result.benchmark_return_pct, 4
    )

    # Volatility (annualised)
    result.portfolio_vol_ann_pct = round(Rp.std() * math.sqrt(TRADING_DAYS_PER_YEAR) * 100, 4)
    result.benchmark_vol_ann_pct = round(Rb.std() * math.sqrt(TRADING_DAYS_PER_YEAR) * 100, 4)

    # Tracking error
    active_daily = Rp - Rb
    te_daily     = active_daily.std()
    result.tracking_error_ann_pct = round(te_daily * math.sqrt(TRADING_DAYS_PER_YEAR) * 100, 4)

    # Information ratio
    # Active return annualised = active_return over window scaled to 1 year
    years_covered = len(common) / TRADING_DAYS_PER_YEAR
    if years_covered > 0 and result.tracking_error_ann_pct > 0:
        active_return_ann = result.active_return_pct / years_covered
        result.information_ratio = round(
            active_return_ann / result.tracking_error_ann_pct, 4
        )

    # Sharpe ratios
    rfr_daily = RISK_FREE_RATE_ANN / TRADING_DAYS_PER_YEAR
    if Rp.std() > 0:
        result.sharpe_ratio = round(
            (Rp.mean() - rfr_daily) / Rp.std() * math.sqrt(TRADING_DAYS_PER_YEAR), 4
        )
    if Rb.std() > 0:
        result.benchmark_sharpe = round(
            (Rb.mean() - rfr_daily) / Rb.std() * math.sqrt(TRADING_DAYS_PER_YEAR), 4
        )

    logger.info(
        "Beta: %.3f  Alpha(ann): %.2f%%  TE: %.2f%%  IR: %.3f  Sharpe: %.3f",
        result.beta, result.alpha_ann_pct, result.tracking_error_ann_pct,
        result.information_ratio, result.sharpe_ratio,
    )
    return result


# ============================================================================
# CONCENTRATION (HHI)
# ============================================================================

def compute_concentration(
    positions: Dict[str, dict],
    market_values: Optional[Dict[str, float]] = None,
) -> ConcentrationMetrics:
    """
    Compute Herfindahl-Hirschman Index and related concentration metrics.

    HHI = Σ w_i²    (where w_i are portfolio weight fractions summing to 1)
    Effective N = 1 / HHI   (equivalent number of equal-weight positions)

    Args:
        positions:     raw position dicts from portfolio_state.json
        market_values: optional pre-computed {symbol: market_value_eur} dict.
                       If provided, these are used instead of the stale
                       "current_value" field from portfolio_state.json.
    """
    result = ConcentrationMetrics()

    values = []
    for sym, pos in positions.items():
        if sym in BENCHMARK_SYMBOLS:
            continue
        # Prefer pre-computed MtM value; fall back to Script 13's stale field
        if market_values and sym in market_values:
            v = market_values[sym]
        else:
            v = pos.get("current_value", pos.get("current_value_eur", 0.0))
        if v > 0:
            values.append(v)

    if not values:
        return result

    total       = sum(values)
    weights     = sorted([v / total for v in values], reverse=True)
    n           = len(weights)

    hhi         = sum(w**2 for w in weights)
    effective_n = 1.0 / hhi if hhi > 0 else 0.0

    result.hhi              = round(hhi, 6)
    result.effective_n      = round(effective_n, 2)
    result.position_count   = n
    result.top1_weight_pct  = round(weights[0] * 100, 2) if n >= 1 else 0.0
    result.top3_weight_pct  = round(sum(weights[:3]) * 100, 2) if n >= 3 else 0.0
    result.top5_weight_pct  = round(sum(weights[:5]) * 100, 2) if n >= 5 else 0.0

    if hhi <= HHI_DIVERSIFIED:
        result.regime   = "Diversified"
        result.is_alert = False
    elif hhi <= HHI_MODERATE:
        result.regime   = "Moderate"
        result.is_alert = False
    else:
        result.regime   = "High"
        result.is_alert = True

    logger.info(
        "HHI: %.4f  Effective N: %.1f  Regime: %s  Top3: %.1f%%",
        result.hhi, result.effective_n, result.regime, result.top3_weight_pct,
    )
    return result


# ============================================================================
# CORRELATION MATRIX
# ============================================================================

def compute_correlation_matrix(
    constituent_returns: Dict[str, pd.Series],
    positions: Dict[str, dict],
    top_n: int,
    lookback: int = 63,
    market_values: Optional[Dict[str, float]] = None,
) -> CorrelationResult:
    """
    Compute pairwise correlation matrix for the top-N positions by value.

    Uses the shorter 63-day (quarterly) lookback for recency relevance.
    Positions are ranked by mark-to-market value (from market_values dict),
    falling back to the stale "current_value" from portfolio_state.json.
    """
    result = CorrelationResult(lookback_days=lookback)

    # Select top-N symbols by current mark-to-market portfolio value
    ranked = sorted(
        [
            (sym, (market_values or {}).get(sym,
                   pos.get("current_value", pos.get("current_value_eur", 0.0))))
            for sym, pos in positions.items()
            if sym not in BENCHMARK_SYMBOLS
        ],
        key=lambda x: x[1],
        reverse=True,
    )[:top_n]

    symbols = [sym for sym, _ in ranked if sym in constituent_returns]

    if len(symbols) < 2:
        logger.warning("Fewer than 2 symbols with return data; skipping correlation.")
        return result

    df = pd.DataFrame({sym: constituent_returns[sym] for sym in symbols})
    df = df.dropna(how="all")
    # Use only the most recent `lookback` rows
    df = df.iloc[-lookback:]

    # Pearson correlation on log-returns
    corr_matrix = df.corr()

    result.symbols = list(corr_matrix.columns)
    result.matrix  = corr_matrix.values.tolist()

    # Summary statistics (upper triangle only)
    upper_vals = []
    high_pairs = []
    n = len(result.symbols)
    for i in range(n):
        for j in range(i + 1, n):
            c = corr_matrix.iloc[i, j]
            if not math.isnan(c):
                upper_vals.append(c)
                if abs(c) >= CORR_HIGH:
                    high_pairs.append((result.symbols[i], result.symbols[j], round(c, 4)))

    result.avg_pairwise_corr = round(float(np.mean(upper_vals)), 4) if upper_vals else 0.0
    result.max_pairwise_corr = round(float(np.max(upper_vals)), 4) if upper_vals else 0.0
    result.high_corr_pairs   = sorted(high_pairs, key=lambda x: abs(x[2]), reverse=True)
    result.is_alert          = len(high_pairs) > 0

    logger.info(
        "Correlation matrix: %d symbols  Avg pairwise: %.3f  High-corr pairs: %d",
        n, result.avg_pairwise_corr, len(high_pairs),
    )
    return result


# ============================================================================
# FACTOR EXPOSURES (PROXY-BASED)
# ============================================================================

def compute_factor_exposures(
    positions: Dict[str, dict],
    company_info: dict,
    market_values: Optional[Dict[str, float]] = None,
    as_of: Optional[date] = None,
) -> FactorExposure:
    """
    Compute size, momentum, quality, and geographic factor exposures.

    All factors are portfolio-weighted averages (weight = current MtM value).

    Size factor:     log₁₀(market_cap)  — proxy for large/small cap tilt
    Momentum factor: (close – SMA200) / SMA200 × 100  — read from indicators
                     parquet (Script 05) when not present in portfolio_state
    Quality factor:  sector → quality score lookup (SECTOR_QUALITY_SCORE table)
    Geographic:      symbol suffix → geographic region (SUFFIX_TO_GEO table)
    """
    result = FactorExposure()

    mv = market_values or {}
    total_value = sum(
        mv.get(sym, pos.get("current_value", pos.get("current_value_eur", 0.0)))
        for sym, pos in positions.items()
        if sym not in BENCHMARK_SYMBOLS
    )
    if total_value <= 0:
        return result

    size_weighted    = 0.0
    momentum_weighted = 0.0
    quality_weighted = 0.0
    geo_us = geo_eu = geo_crypto = geo_other = 0.0
    weight_size = weight_mom = weight_qual = 0.0

    for sym, pos in positions.items():
        if sym in BENCHMARK_SYMBOLS:
            continue
        pv = mv.get(sym, pos.get("current_value", pos.get("current_value_eur", 0.0)))
        if pv <= 0:
            continue
        w = pv / total_value

        info = company_info.get(sym, {})

        # --- Size ---
        mkt_cap = info.get("market_cap", None) or info.get("marketCap", None)
        if mkt_cap and mkt_cap > 0:
            size_weighted += w * math.log10(mkt_cap)
            weight_size   += w

        # --- Momentum ---
        # Priority 1: portfolio_state has pre-computed momentum_score (Script 07)
        # Priority 2: compute from current_price and sma200 field in portfolio_state
        # Priority 3: look up sma_slow from indicators parquet (Script 05 output)
        mom_score = pos.get("momentum_score", None)
        if mom_score is not None:
            momentum_weighted += w * float(mom_score)
            weight_mom        += w
        else:
            current_price = pos.get("current_price", 0.0)
            # Try portfolio_state sma200 field first
            sma200 = pos.get("sma200", 0.0) or pos.get("sma_slow", 0.0)
            # Fallback: read from indicators parquet
            if (sma200 == 0.0 or sma200 is None) and as_of is not None:
                sma200 = load_latest_sma200(sym, as_of) or 0.0
            # Fallback: derive from latest price series if current_price missing
            if current_price == 0.0:
                prices = load_price_series(sym, as_of, 10) if as_of else None
                if prices is not None and len(prices) > 0:
                    current_price = float(prices.iloc[-1])
            if sma200 > 0 and current_price > 0:
                calc_mom = (current_price - sma200) / sma200 * 100
                momentum_weighted += w * calc_mom
                weight_mom        += w

        # --- Quality (sector proxy) ---
        sector = info.get("sector", "Unknown") or "Unknown"
        quality_weighted += w * SECTOR_QUALITY_SCORE.get(sector, 5.0)
        weight_qual      += w

        # --- Geography ---
        # Use SUFFIX_TO_GEO to handle both legacy (.US/.DE/.L) and pipeline
        # exchange-code suffixes (.NYSE/.NASDAQ/.XETRA/.LSE).
        geo = "Other"
        for suffix, region in SUFFIX_TO_GEO.items():
            if sym.endswith(suffix):
                geo = region
                break
        if geo == "Crypto":
            geo_crypto += w
        elif geo == "US":
            geo_us += w
        elif geo in ("EU", "UK"):
            geo_eu += w
        else:
            geo_other += w

    # Finalise factor scores
    if weight_size > 0:
        result.size_score = round(size_weighted / weight_size, 4)
        if result.size_score > 10:
            result.size_label = "Mega/Large-Cap"
        elif result.size_score > 9:
            result.size_label = "Large-Cap"
        elif result.size_score > 8:
            result.size_label = "Mid-Cap"
        else:
            result.size_label = "Small-Cap"

    if weight_mom > 0:
        result.momentum_score_avg = round(momentum_weighted / weight_mom, 4)
        if result.momentum_score_avg > 20:
            result.momentum_label = "High Momentum"
        elif result.momentum_score_avg > 5:
            result.momentum_label = "Moderate Momentum"
        elif result.momentum_score_avg >= 0:
            result.momentum_label = "Weak Momentum"
        else:
            result.momentum_label = "Negative Momentum"

    if weight_qual > 0:
        result.quality_score_avg = round(quality_weighted / weight_qual, 4)
        if result.quality_score_avg >= 7:
            result.quality_label = "High Quality"
        elif result.quality_score_avg >= 5:
            result.quality_label = "Average Quality"
        else:
            result.quality_label = "Low Quality"

    result.us_weight_pct     = round(geo_us     * 100, 2)
    result.eu_weight_pct     = round(geo_eu     * 100, 2)
    result.crypto_weight_pct = round(geo_crypto * 100, 2)
    result.other_weight_pct  = round(geo_other  * 100, 2)

    logger.info(
        "Factor exposures — Size: %.2f (%s)  Momentum: %.2f (%s)  Quality: %.2f (%s)",
        result.size_score, result.size_label,
        result.momentum_score_avg, result.momentum_label,
        result.quality_score_avg, result.quality_label,
    )
    return result


# ============================================================================
# DRAWDOWN ANALYSIS
# ============================================================================

def compute_drawdown_metrics(
    portfolio_returns: pd.Series,
    lookback: int,
) -> DrawdownMetrics:
    """
    Compute rolling drawdown statistics from daily portfolio returns.

    Max Drawdown = max((equity_peak – equity_trough) / equity_peak)
    Calmar Ratio = Annualised_Return / |Max_Drawdown|
    """
    result = DrawdownMetrics(lookback_days=lookback)

    if portfolio_returns.empty:
        return result

    # Build equity curve (start at 1.0)
    equity = np.exp(portfolio_returns.cumsum())
    equity = np.concatenate([[1.0], equity.values])

    running_max = np.maximum.accumulate(equity)
    drawdowns   = (equity - running_max) / running_max * 100  # in percent

    result.max_drawdown_pct     = round(float(drawdowns.min()), 4)
    result.current_drawdown_pct = round(float(drawdowns[-1]), 4)
    result.avg_drawdown_pct     = round(float(drawdowns[drawdowns < 0].mean())
                                        if (drawdowns < 0).any() else 0.0, 4)

    # Longest drawdown duration
    in_dd = False
    dd_start = 0
    longest = 0
    for i, d in enumerate(drawdowns):
        if d < 0 and not in_dd:
            in_dd    = True
            dd_start = i
        elif d >= 0 and in_dd:
            in_dd   = False
            duration = i - dd_start
            if duration > longest:
                longest = duration
    if in_dd:
        duration = len(drawdowns) - dd_start
        if duration > longest:
            longest = duration
    result.longest_dd_days = longest

    # Recovery: None if still in drawdown
    result.recovery_days = None if drawdowns[-1] < 0 else None  # refined below
    # (Full recovery detection: last zero crossing)
    for i in range(len(drawdowns) - 1, -1, -1):
        if drawdowns[i] >= 0:
            result.recovery_days = 0  # recovered
            break

    # Calmar ratio
    n_days   = len(portfolio_returns)
    ann_ret  = (float(np.exp(portfolio_returns.sum())) - 1) * (TRADING_DAYS_PER_YEAR / n_days)
    max_dd   = abs(result.max_drawdown_pct / 100)
    result.calmar_ratio = round(ann_ret / max_dd, 4) if max_dd > 0 else 0.0

    logger.info(
        "Drawdown — Max: %.2f%%  Current: %.2f%%  Longest: %d days  Calmar: %.3f",
        result.max_drawdown_pct, result.current_drawdown_pct,
        result.longest_dd_days, result.calmar_ratio,
    )
    return result


# ============================================================================
# COMPOSITE RISK SCORE
# ============================================================================

def compute_risk_score(report: RiskReport) -> Tuple[int, str, List[str]]:
    """
    Compute a composite risk score (0-10) and generate alerts.

    Scoring rubric (each component contributes 0-2 points to risk):
        VaR:           0 = VaR_95_1d < 1.5%,  1 = 1.5-3%,  2 = >3%
        Drawdown:      0 = MDD > -10%,          1 = -10 to -20%,  2 = < -20%
        Concentration: 0 = Diversified,         1 = Moderate,     2 = High
        Correlation:   0 = avg < 0.50,          1 = 0.50-0.70,    2 = >0.70
        Beta:          0 = beta < 0.80,         1 = 0.80-1.10,    2 = >1.10
    """
    score  = 0
    alerts = []

    # VaR
    var_95_1d = next(
        (v for v in report.var_results if v.confidence_pct == 95.0 and v.horizon_days == 1),
        None,
    )
    if var_95_1d:
        vp = var_95_1d.var_pct
        if vp > 3.0:
            score += 2
            alerts.append(f"CRITICAL: 1-day 95% VaR is {vp:.2f}% of portfolio (threshold: 3%)")
        elif vp > 1.5:
            score += 1
            alerts.append(f"WARN: 1-day 95% VaR is {vp:.2f}% of portfolio (threshold: 1.5%)")

    # Drawdown
    if report.drawdown:
        mdd = abs(report.drawdown.max_drawdown_pct)
        if mdd > 20.0:
            score += 2
            alerts.append(f"CRITICAL: Max drawdown {-mdd:.2f}% exceeds -20% threshold")
        elif mdd > 10.0:
            score += 1
            alerts.append(f"WARN: Max drawdown {-mdd:.2f}% exceeds -10% threshold")

    # Concentration
    if report.concentration:
        if report.concentration.regime == "High":
            score += 2
            alerts.append(
                f"CRITICAL: HHI {report.concentration.hhi:.4f} indicates High concentration "
                f"(effective N={report.concentration.effective_n:.1f})"
            )
        elif report.concentration.regime == "Moderate":
            score += 1
            alerts.append(
                f"WARN: HHI {report.concentration.hhi:.4f} indicates Moderate concentration"
            )
        if report.concentration.top3_weight_pct > 40.0:
            alerts.append(
                f"WARN: Top 3 positions represent {report.concentration.top3_weight_pct:.1f}% "
                "of portfolio"
            )

    # Correlation
    if report.correlation:
        avg_corr = report.correlation.avg_pairwise_corr
        if avg_corr > 0.70:
            score += 2
            alerts.append(
                f"CRITICAL: Average pairwise correlation {avg_corr:.3f} exceeds 0.70"
            )
        elif avg_corr > 0.50:
            score += 1
            alerts.append(
                f"WARN: Average pairwise correlation {avg_corr:.3f} exceeds 0.50"
            )
        if report.correlation.high_corr_pairs:
            pairs_str = ", ".join(
                f"{a}/{b} ({c:.2f})"
                for a, b, c in report.correlation.high_corr_pairs[:3]
            )
            alerts.append(f"WARN: High-correlation pairs: {pairs_str}")

    # Beta
    if report.benchmark_metrics:
        b = report.benchmark_metrics.beta
        if b > 1.10:
            score += 2
            alerts.append(f"WARN: Portfolio beta {b:.3f} exceeds 1.10 (high market sensitivity)")
        elif b > 0.80:
            score += 1

    score = min(score, 10)

    if score <= 2:
        label = "Low"
    elif score <= 4:
        label = "Moderate"
    elif score <= 6:
        label = "Elevated"
    elif score <= 8:
        label = "High"
    else:
        label = "Critical"

    return score, label, alerts


# ============================================================================
# PDF REPORT GENERATION
# ============================================================================

def _style_table(table: "Table", header_bg=None) -> "Table":
    """Apply standard table styling."""
    if header_bg is None:
        header_bg = C_NAVY
    table.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0), header_bg),
        ("TEXTCOLOR",     (0, 0), (-1, 0), C_WHITE),
        ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",      (0, 0), (-1, 0), 9),
        ("ALIGN",         (0, 0), (-1, 0), "CENTER"),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("FONTNAME",      (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE",      (0, 1), (-1, -1), 8),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [C_WHITE, C_LIGHT]),
        ("GRID",          (0, 0), (-1, -1), 0.25, colors.HexColor("#DEE2E6")),
        ("LEFTPADDING",   (0, 0), (-1, -1), 4),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 4),
        ("TOPPADDING",    (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return table


def _metric_color(value: float, good_above: Optional[float] = None,
                  warn_above: Optional[float] = None) -> object:
    """Return colour based on thresholds."""
    if good_above is not None and value >= good_above:
        return C_SUCCESS
    if warn_above is not None and value >= warn_above:
        return C_ORANGE
    return C_DANGER


def generate_pdf_report(report: RiskReport, output_path: Path) -> None:
    """Generate a professional PDF risk report."""
    if not REPORTLAB_AVAILABLE:
        logger.warning("ReportLab not installed; skipping PDF generation.")
        return

    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=A4,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
    )
    styles = getSampleStyleSheet()

    # Custom styles
    title_style = ParagraphStyle(
        "RiskTitle",
        parent=styles["Heading1"],
        textColor=C_WHITE,
        fontSize=16,
        alignment=TA_CENTER,
        spaceAfter=4,
    )
    h2_style = ParagraphStyle(
        "RiskH2",
        parent=styles["Heading2"],
        textColor=C_NAVY,
        fontSize=11,
        spaceBefore=10,
        spaceAfter=4,
    )
    body_style = ParagraphStyle(
        "RiskBody",
        parent=styles["Normal"],
        fontSize=8,
        leading=12,
    )
    alert_style = ParagraphStyle(
        "RiskAlert",
        parent=styles["Normal"],
        textColor=C_DANGER,
        fontSize=8,
        leading=11,
    )
    small_style = ParagraphStyle(
        "RiskSmall",
        parent=styles["Normal"],
        fontSize=7,
        textColor=colors.HexColor("#6C757D"),
    )

    story = []

    # ── Title block ──────────────────────────────────────────────────────────
    title_table = Table(
        [[Paragraph(
            f"<font size=16><b>RISK ANALYTICS REPORT</b></font><br/>"
            f"<font size=9>As of {report.as_of_date}  |  "
            f"Generated {report.generated_at[:16]}  |  "
            f"{report.lookback_days}-day lookback</font>",
            title_style,
        )]],
        colWidths=[180 * mm],
    )
    title_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), C_NAVY),
        ("LEFTPADDING",  (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING",   (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 8),
    ]))
    story.append(title_table)
    story.append(Spacer(1, 5 * mm))

    # ── Risk Score Banner ─────────────────────────────────────────────────────
    score_colour = {
        "Low": C_SUCCESS, "Moderate": C_SUCCESS, "Elevated": C_ORANGE,
        "High": C_DANGER, "Critical": C_DANGER,
    }.get(report.risk_label, C_ORANGE)

    score_table = Table(
        [[
            Paragraph(
                f"<b>Portfolio Risk Score: {report.risk_score}/10 — {report.risk_label}</b>",
                ParagraphStyle("ScorePara", parent=styles["Normal"],
                               textColor=C_WHITE, fontSize=11, alignment=TA_CENTER),
            )
        ]],
        colWidths=[180 * mm],
    )
    score_table.setStyle(TableStyle([
        ("BACKGROUND",  (0, 0), (-1, -1), score_colour),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING",  (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(score_table)
    story.append(Spacer(1, 4 * mm))

    # ── Alerts ────────────────────────────────────────────────────────────────
    if report.alerts:
        story.append(Paragraph("⚠ Risk Alerts", h2_style))
        for alert in report.alerts:
            story.append(Paragraph(f"• {alert}", alert_style))
        story.append(Spacer(1, 3 * mm))

    # ── VaR / CVaR Table ──────────────────────────────────────────────────────
    story.append(Paragraph("1. Value at Risk (VaR) &amp; Expected Shortfall (CVaR)", h2_style))
    story.append(Paragraph(
        "Historical simulation using the last 252 trading days of daily log-returns.  "
        "Multi-day VaR scaled via square-root-of-time.",
        body_style,
    ))
    story.append(Spacer(1, 2 * mm))

    var_data = [["Confidence", "Horizon", "VaR (€)", "VaR (%)", "CVaR (€)", "CVaR (%)", "Tail Obs", "Alert"]]
    for v in report.var_results:
        alert_text = "⚠ YES" if v.is_alert else "–"
        var_data.append([
            f"{v.confidence_pct:.0f}%",
            f"{v.horizon_days}d",
            f"€{v.var_eur:,.2f}",
            f"{v.var_pct:.2f}%",
            f"€{v.cvar_eur:,.2f}",
            f"{v.cvar_pct:.2f}%",
            str(v.tail_obs),
            alert_text,
        ])

    col_w = [20*mm, 14*mm, 28*mm, 18*mm, 28*mm, 18*mm, 18*mm, 16*mm]
    var_table = _style_table(Table(var_data, colWidths=col_w, repeatRows=1))
    story.append(var_table)
    story.append(Spacer(1, 4 * mm))

    # ── Benchmark Metrics ─────────────────────────────────────────────────────
    if report.benchmark_metrics:
        bm = report.benchmark_metrics
        story.append(Paragraph(
            f"2. Benchmark Metrics vs {bm.benchmark_name}", h2_style
        ))
        bm_data = [
            ["Metric", "Value", "Interpretation"],
            ["Beta", f"{bm.beta:.4f}",
             "Market sensitivity (1.0 = moves with market)"],
            ["Alpha (ann.)", f"{bm.alpha_ann_pct:+.2f}%",
             "Return attributable to skill vs market exposure"],
            ["R-Squared", f"{bm.r_squared:.4f}",
             "Fraction of portfolio variance explained by benchmark"],
            ["Portfolio Return", f"{bm.portfolio_return_pct:+.2f}%",
             f"Cumulative over {bm.lookback_days} trading days"],
            ["Benchmark Return", f"{bm.benchmark_return_pct:+.2f}%",
             f"Cumulative over {bm.lookback_days} trading days"],
            ["Active Return", f"{bm.active_return_pct:+.2f}%",
             "Portfolio minus benchmark"],
            ["Portfolio Vol (ann.)", f"{bm.portfolio_vol_ann_pct:.2f}%",
             "Annualised standard deviation of daily returns"],
            ["Benchmark Vol (ann.)", f"{bm.benchmark_vol_ann_pct:.2f}%",
             "Annualised standard deviation of benchmark returns"],
            ["Tracking Error (ann.)", f"{bm.tracking_error_ann_pct:.2f}%",
             "StdDev of active returns × √252"],
            ["Information Ratio", f"{bm.information_ratio:.4f}",
             "Active return / Tracking Error (target: >0.50)"],
            ["Portfolio Sharpe", f"{bm.sharpe_ratio:.4f}",
             "Risk-adjusted return (rf=2%; target: >1.0)"],
            ["Benchmark Sharpe", f"{bm.benchmark_sharpe:.4f}",
             "Benchmark risk-adjusted return"],
        ]
        col_w_bm = [50*mm, 30*mm, 100*mm]
        bm_table = _style_table(Table(bm_data, colWidths=col_w_bm, repeatRows=1))
        story.append(bm_table)
        story.append(Spacer(1, 4 * mm))

    # ── Concentration ─────────────────────────────────────────────────────────
    if report.concentration:
        c = report.concentration
        story.append(Paragraph("3. Concentration Risk (Herfindahl-Hirschman Index)", h2_style))
        conc_data = [
            ["Metric", "Value", "Interpretation"],
            ["HHI", f"{c.hhi:.6f}", f"Raw index (target: ≤{HHI_DIVERSIFIED})"],
            ["Effective N", f"{c.effective_n:.1f}",
             "Equivalent equal-weight positions"],
            ["Position Count", str(c.position_count), "Total open positions"],
            ["Top 1 Weight", f"{c.top1_weight_pct:.1f}%", "Largest single position"],
            ["Top 3 Weight", f"{c.top3_weight_pct:.1f}%",
             "Alert if >40% (trigger from Script 14)"],
            ["Top 5 Weight", f"{c.top5_weight_pct:.1f}%", ""],
            ["Regime", f"{'⚠ ' if c.is_alert else ''}{c.regime}",
             "Diversified / Moderate / High"],
        ]
        col_w_conc = [50*mm, 30*mm, 100*mm]
        conc_table = _style_table(Table(conc_data, colWidths=col_w_conc, repeatRows=1))
        story.append(conc_table)
        story.append(Spacer(1, 4 * mm))

    # ── Correlation ───────────────────────────────────────────────────────────
    if report.correlation and report.correlation.symbols:
        cr = report.correlation
        story.append(Paragraph(
            f"4. Correlation Matrix – Top {len(cr.symbols)} Positions "
            f"({cr.lookback_days}-day window)",
            h2_style,
        ))
        story.append(Paragraph(
            f"Average pairwise: {cr.avg_pairwise_corr:.3f}  |  "
            f"Max pairwise: {cr.max_pairwise_corr:.3f}  |  "
            f"High-corr pairs (≥{CORR_HIGH}): {len(cr.high_corr_pairs)}",
            body_style,
        ))
        story.append(Spacer(1, 2 * mm))

        # Build matrix table
        header_row = [""] + cr.symbols
        matrix_data = [header_row]
        for i, sym in enumerate(cr.symbols):
            row = [sym]
            for j in range(len(cr.symbols)):
                v = cr.matrix[i][j]
                if i == j:
                    row.append("1.000")
                else:
                    row.append(f"{v:.3f}")
            matrix_data.append(row)

        sym_count = len(cr.symbols)
        col_w_mat = [22*mm] + [min(16*mm, (158*mm) / max(sym_count, 1))] * sym_count
        mat_table = Table(matrix_data, colWidths=col_w_mat, repeatRows=1)
        ts_cmds = [
            ("BACKGROUND",    (0, 0), (-1, 0), C_NAVY),
            ("TEXTCOLOR",     (0, 0), (-1, 0), C_WHITE),
            ("BACKGROUND",    (0, 0), (0, -1), C_NAVY),
            ("TEXTCOLOR",     (0, 0), (0, -1), C_WHITE),
            ("FONTNAME",      (0, 0), (-1, -1), "Helvetica"),
            ("FONTSIZE",      (0, 0), (-1, -1), 7),
            ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
            ("GRID",          (0, 0), (-1, -1), 0.25, colors.HexColor("#DEE2E6")),
            ("LEFTPADDING",   (0, 0), (-1, -1), 2),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 2),
        ]
        # Colour-code high correlations
        for i in range(1, sym_count + 1):
            for j in range(1, sym_count + 1):
                if i == j:
                    ts_cmds.append(("BACKGROUND", (j, i), (j, i), C_LIGHT))
                else:
                    val = cr.matrix[i - 1][j - 1]
                    if val >= CORR_HIGH:
                        ts_cmds.append(("BACKGROUND", (j, i), (j, i),
                                        colors.HexColor("#FFDADA")))
                    elif val >= 0.50:
                        ts_cmds.append(("BACKGROUND", (j, i), (j, i),
                                        colors.HexColor("#FFF3CD")))
        mat_table.setStyle(TableStyle(ts_cmds))
        story.append(mat_table)
        story.append(Spacer(1, 4 * mm))

    # ── Factor Exposures ──────────────────────────────────────────────────────
    if report.factor_exposure:
        fe = report.factor_exposure
        story.append(Paragraph("5. Factor Exposures (Proxy-Based)", h2_style))
        fe_data = [
            ["Factor", "Score / Weight", "Label", "Notes"],
            ["Size",
             f"log₁₀ mktcap = {fe.size_score:.2f}",
             fe.size_label,
             "Large-cap tilt ↑ reduces liquidity risk"],
            ["Momentum",
             f"{fe.momentum_score_avg:+.2f}% vs SMA200",
             fe.momentum_label,
             "High positive = strong trend confirmation"],
            ["Quality",
             f"{fe.quality_score_avg:.2f}/10",
             fe.quality_label,
             "Sector-proxy; not P/E based"],
            ["Value",
             "N/A",
             "Unavailable",
             fe.value_note],
            ["Geography – US",    f"{fe.us_weight_pct:.1f}%",  "—", ""],
            ["Geography – EU",    f"{fe.eu_weight_pct:.1f}%",  "—", ""],
            ["Geography – Crypto",f"{fe.crypto_weight_pct:.1f}%","—","Max 20% strategy limit"],
            ["Geography – Other", f"{fe.other_weight_pct:.1f}%","—", ""],
        ]
        col_w_fe = [40*mm, 42*mm, 35*mm, 63*mm]
        fe_table = _style_table(Table(fe_data, colWidths=col_w_fe, repeatRows=1))
        story.append(fe_table)
        story.append(Spacer(1, 4 * mm))

    # ── Drawdown ──────────────────────────────────────────────────────────────
    if report.drawdown:
        dd = report.drawdown
        story.append(Paragraph("6. Drawdown Analysis", h2_style))
        dd_data = [
            ["Metric", "Value", "Notes"],
            ["Max Drawdown",
             f"{dd.max_drawdown_pct:.2f}%",
             "Worst peak-to-trough over lookback window"],
            ["Current Drawdown",
             f"{dd.current_drawdown_pct:.2f}%",
             "Today's drawdown from recent peak"],
            ["Average Drawdown",
             f"{dd.avg_drawdown_pct:.2f}%",
             "Mean of all negative drawdown days"],
            ["Longest DD Duration",
             f"{dd.longest_dd_days} trading days",
             ""],
            ["Recovery Status",
             "Recovered" if dd.recovery_days is not None else "In Drawdown",
             "Whether portfolio has returned to prior peak"],
            ["Calmar Ratio",
             f"{dd.calmar_ratio:.4f}",
             "Annualised return / |Max Drawdown| (target: >0.50)"],
        ]
        col_w_dd = [50*mm, 40*mm, 90*mm]
        dd_table = _style_table(Table(dd_data, colWidths=col_w_dd, repeatRows=1))
        story.append(dd_table)
        story.append(Spacer(1, 4 * mm))

    # ── Footer ────────────────────────────────────────────────────────────────
    story.append(HRFlowable(width="100%", thickness=0.5, color=C_NAVY))
    story.append(Spacer(1, 2 * mm))
    story.append(Paragraph(
        f"Risk Analytics Script 23 | Architecture v3.2 | {report.generated_at[:16]} | "
        f"Lookback: {report.lookback_days} trading days | Benchmark: "
        f"{report.benchmark_metrics.benchmark_symbol if report.benchmark_metrics else 'N/A'}",
        small_style,
    ))

    doc.build(story)
    logger.info("PDF report written: %s", output_path)


# ============================================================================
# CSV EXPORT
# ============================================================================

def export_csv(report: RiskReport, output_path: Path) -> None:
    """Export key risk metrics as a flat CSV for downstream consumption."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    rows = []

    for v in report.var_results:
        rows.append({
            "category":    "VaR",
            "metric":      f"VaR_{v.confidence_pct:.0f}pct_{v.horizon_days}d",
            "value_eur":   v.var_eur,
            "value_pct":   v.var_pct,
            "description": f"{v.confidence_pct:.0f}% VaR {v.horizon_days}-day",
        })
        rows.append({
            "category":    "CVaR",
            "metric":      f"CVaR_{v.confidence_pct:.0f}pct_{v.horizon_days}d",
            "value_eur":   v.cvar_eur,
            "value_pct":   v.cvar_pct,
            "description": f"{v.confidence_pct:.0f}% CVaR {v.horizon_days}-day",
        })

    if report.benchmark_metrics:
        bm = report.benchmark_metrics
        for metric, val in [
            ("beta",                bm.beta),
            ("alpha_ann_pct",       bm.alpha_ann_pct),
            ("r_squared",           bm.r_squared),
            ("tracking_error_ann",  bm.tracking_error_ann_pct),
            ("information_ratio",   bm.information_ratio),
            ("sharpe_ratio",        bm.sharpe_ratio),
            ("portfolio_return_pct",bm.portfolio_return_pct),
            ("active_return_pct",   bm.active_return_pct),
        ]:
            rows.append({"category": "Benchmark", "metric": metric,
                         "value_eur": None, "value_pct": val, "description": ""})

    if report.concentration:
        c = report.concentration
        for metric, val in [
            ("hhi",              c.hhi),
            ("effective_n",      c.effective_n),
            ("top1_weight_pct",  c.top1_weight_pct),
            ("top3_weight_pct",  c.top3_weight_pct),
            ("top5_weight_pct",  c.top5_weight_pct),
        ]:
            rows.append({"category": "Concentration", "metric": metric,
                         "value_eur": None, "value_pct": val, "description": ""})

    if report.drawdown:
        dd = report.drawdown
        for metric, val in [
            ("max_drawdown_pct",     dd.max_drawdown_pct),
            ("current_drawdown_pct", dd.current_drawdown_pct),
            ("calmar_ratio",         dd.calmar_ratio),
            ("longest_dd_days",      dd.longest_dd_days),
        ]:
            rows.append({"category": "Drawdown", "metric": metric,
                         "value_eur": None, "value_pct": val, "description": ""})

    rows.append({
        "category": "Summary", "metric": "risk_score",
        "value_eur": None, "value_pct": report.risk_score,
        "description": report.risk_label,
    })

    pd.DataFrame(rows).to_csv(output_path, index=False)
    logger.info("CSV export written: %s", output_path)


# ============================================================================
# MAIN ORCHESTRATOR
# ============================================================================

def run_risk_analytics(
    as_of: date,
    lookback: int,
    benchmark_symbol: str,
    account_equity_override: Optional[float],
    top_n_corr: int,
    dry_run: bool,
    generate_pdf: bool,
) -> RiskReport:
    """
    Full risk analytics pipeline.

    1. Load portfolio state
    2. Build portfolio returns from constituent prices
    3. Compute VaR / CVaR
    4. Compute benchmark metrics (beta, TE, IR)
    5. Compute concentration (HHI)
    6. Compute correlation matrix
    7. Compute factor exposures
    8. Compute drawdown statistics
    9. Score overall risk level
    10. Export JSON, CSV, PDF
    """
    logger.info("=" * 68)
    logger.info("RISK ANALYTICS  |  as_of=%s  lookback=%d  benchmark=%s",
                as_of, lookback, benchmark_symbol)
    logger.info("=" * 68)

    # ── 1. Load inputs ───────────────────────────────────────────────────────
    state        = load_portfolio_state()
    positions    = state.get("positions", {})
    company_info = load_company_info()

    # Compute account equity as Σ (shares × latest_price) — mark-to-market.
    # Script 13 stores stale "current_value" (entry-price × shares); we recompute.
    if account_equity_override and account_equity_override > 0:
        account_equity = account_equity_override
    else:
        mtm_total = sum(
            _get_current_market_value(sym, pos, as_of, lookback)
            for sym, pos in positions.items()
            if sym not in BENCHMARK_SYMBOLS
        )
        account_equity = mtm_total if mtm_total > 0 else 50_000.0
        if mtm_total <= 0:
            logger.warning(
                "Could not compute mark-to-market equity from parquet; "
                "defaulting to €50,000. Pass --account-equity for override."
            )
    logger.info("Account equity: €%,.2f  |  Positions: %d", account_equity, len(positions))

    # Initialise report
    report = RiskReport(
        as_of_date=str(as_of),
        lookback_days=lookback,
        account_equity_eur=account_equity,
        position_count=len([s for s in positions if s not in BENCHMARK_SYMBOLS]),
    )

    # ── 2. Build portfolio returns ───────────────────────────────────────────
    logger.info("Step 2: Building portfolio returns...")
    portfolio_returns, constituent_returns = build_portfolio_returns(
        positions, as_of, lookback
    )

    # Compute mark-to-market values once (reused by concentration, factor, equity)
    market_values: Dict[str, float] = {
        sym: _get_current_market_value(sym, pos, as_of, lookback)
        for sym, pos in positions.items()
        if sym not in BENCHMARK_SYMBOLS
    }

    # ── 3. VaR / CVaR ────────────────────────────────────────────────────────
    logger.info("Step 3: Computing VaR / CVaR...")
    report.var_results = compute_all_var(portfolio_returns, account_equity)
    for v in report.var_results:
        logger.info(
            "  %d%% VaR %dd: €%.2f (%.4f%%)  CVaR: €%.2f (%.4f%%)",
            v.confidence_pct, v.horizon_days,
            v.var_eur, v.var_pct, v.cvar_eur, v.cvar_pct,
        )

    # ── 4. Benchmark metrics ──────────────────────────────────────────────────
    logger.info("Step 4: Computing benchmark metrics (%s)...", benchmark_symbol)
    report.benchmark_metrics = compute_benchmark_metrics(
        portfolio_returns, benchmark_symbol, as_of, lookback
    )

    # ── 5. Concentration ──────────────────────────────────────────────────────
    logger.info("Step 5: Computing concentration metrics...")
    report.concentration = compute_concentration(positions, market_values)

    # ── 6. Correlation matrix ─────────────────────────────────────────────────
    logger.info("Step 6: Computing correlation matrix (top %d positions)...", top_n_corr)
    report.correlation = compute_correlation_matrix(
        constituent_returns, positions, top_n=top_n_corr, lookback=min(63, lookback),
        market_values=market_values,
    )

    # ── 7. Factor exposures ───────────────────────────────────────────────────
    logger.info("Step 7: Computing factor exposures...")
    report.factor_exposure = compute_factor_exposures(positions, company_info, market_values, as_of=as_of)

    # ── 8. Drawdown ───────────────────────────────────────────────────────────
    logger.info("Step 8: Computing drawdown metrics...")
    report.drawdown = compute_drawdown_metrics(portfolio_returns, lookback)

    # ── 9. Composite risk score ───────────────────────────────────────────────
    logger.info("Step 9: Computing composite risk score...")
    score, label, alerts = compute_risk_score(report)
    report.risk_score = score
    report.risk_label = label
    report.alerts     = alerts

    logger.info("Composite risk score: %d/10 — %s", score, label)
    for alert in alerts:
        logger.warning("  ALERT: %s", alert)

    # ── 10. Export ────────────────────────────────────────────────────────────
    if not dry_run:
        date_str = str(as_of)

        # JSON
        json_path = RISK_DIR / f"{date_str}_risk_metrics.json"
        json_path.parent.mkdir(parents=True, exist_ok=True)
        with open(json_path, "w", encoding="utf-8") as fh:
            json.dump(report.to_dict(), fh, indent=2, default=str)
        logger.info("JSON written: %s", json_path)

        # CSV
        csv_path = REPORTS_DIR / f"risk_{date_str}.csv"
        export_csv(report, csv_path)

        # PDF
        if generate_pdf:
            pdf_path = REPORTS_DIR / f"risk_{date_str}.pdf"
            generate_pdf_report(report, pdf_path)
    else:
        logger.info("DRY RUN: No files written.")

    logger.info("=" * 68)
    logger.info("RISK ANALYTICS COMPLETE")
    logger.info("=" * 68)
    return report


# ============================================================================
# DEMO MODE  —  synthetic portfolio without upstream data files
# ============================================================================

# Realistic 15-position trend-following portfolio used for demonstration.
# Values calibrated to represent a €52,000 account in strong uptrend regime.
_DEMO_POSITIONS_SPEC = [
    # (symbol, sector, mkt_cap_usd, value_eur, momentum_pct, current_price, sma200)
    ("NVDA.US",  "Technology",             2_800_000_000_000, 4_160, 85.3,  865.0, 465.0),
    ("MSFT.US",  "Technology",             3_100_000_000_000, 3_900, 42.1,  415.0, 292.0),
    ("META.US",  "Communication Services", 1_450_000_000_000, 3_640, 61.8,  580.0, 358.0),
    ("LLY.US",   "Healthcare",               750_000_000_000, 3_380, 38.4,  820.0, 592.0),
    ("ASML.AS",  "Technology",               310_000_000_000, 3_120, 28.7,  930.0, 722.0),
    ("SAP.DE",   "Technology",               230_000_000_000, 2_860, 31.2,  208.0, 159.0),
    ("NOVO.DE",  "Healthcare",               460_000_000_000, 2_600, 22.9,  126.0, 102.0),
    ("AVGO.US",  "Technology",               760_000_000_000, 2_340, 55.7,  155.0,  99.6),
    ("MA.US",    "Financial Services",        430_000_000_000, 2_080, 18.3,  487.0, 411.0),
    ("UNH.US",   "Healthcare",               490_000_000_000, 1_820, 12.1,  532.0, 475.0),
    ("AAPL.US",  "Technology",             2_600_000_000_000, 1_560, 15.6,  185.0, 160.0),
    ("AMZN.US",  "Consumer Cyclical",       2_000_000_000_000, 1_300,  9.8,  196.0, 178.6),
    ("V.US",     "Financial Services",        540_000_000_000, 1_040, 11.4,  275.0, 246.8),
    ("BTC-EUR.CC","Cryptocurrency",           940_000_000_000,   780, 47.2,  58_500.0, 39_700.0),
    ("ETH-EUR.CC","Cryptocurrency",           360_000_000_000,   520, 31.5,   3_150.0,  2_400.0),
]


def _generate_demo_returns(
    rng: np.random.Generator,
    n_days: int,
    annual_return: float,
    annual_vol: float,
    beta_to_market: float,
    market_returns: np.ndarray,
) -> np.ndarray:
    """
    Generate synthetic daily log-returns for one instrument.
        R_i = beta_i × R_market + alpha_daily + idiosyncratic
    """
    alpha_daily   = annual_return / TRADING_DAYS_PER_YEAR
    idio_vol      = annual_vol / math.sqrt(TRADING_DAYS_PER_YEAR)
    idio          = rng.normal(0, idio_vol, n_days)
    returns       = beta_to_market * market_returns + alpha_daily + idio
    return returns


def build_demo_state(as_of: date, lookback: int, benchmark_symbol: str) -> Tuple[
    dict,               # portfolio_state  (positions dict)
    dict,               # company_info
    pd.Series,          # portfolio_returns
    Dict[str, pd.Series],  # constituent_returns
    pd.Series,          # benchmark_returns
]:
    """
    Generate a fully synthetic but realistic portfolio state and return history.

    The portfolio is a 15-position trend-following portfolio with:
    - 11 US equities (large-cap tech / healthcare / financials)
    - 2  EU equities (XETRA / Euronext Amsterdam)
    - 2  crypto positions (BTC / ETH, total 2.5% of portfolio)

    Returns are generated via a factor model:
        R_i = β_i × R_market + α_daily + ε_idio
    where R_market is a GBM-based synthetic benchmark return series.
    """
    logger.info("DEMO MODE: generating synthetic portfolio data (%d trading days)", lookback)

    rng = np.random.default_rng(42)  # deterministic seed for reproducibility

    # ── Market (benchmark) returns ────────────────────────────────────────────
    market_ann_ret = 0.14    # 14% annualised (bull market scenario)
    market_ann_vol = 0.16    # 16% vol
    market_daily   = rng.normal(
        market_ann_ret / TRADING_DAYS_PER_YEAR,
        market_ann_vol / math.sqrt(TRADING_DAYS_PER_YEAR),
        lookback,
    )

    # Build date index (business days ending as_of).
    # bdate_range may produce fewer periods if as_of is a weekend; extend to be safe.
    all_dates_raw = pd.bdate_range(end=as_of, periods=lookback + 5)
    all_dates     = all_dates_raw[-lookback:]   # take exactly lookback days

    # ── Per-symbol returns ────────────────────────────────────────────────────
    constituent_returns: Dict[str, pd.Series] = {}

    # Betas and annual return/vol by symbol (calibrated to look realistic)
    _symbol_params = {
        "NVDA.US":    (1.8, 0.62, 0.55),   # (beta, ann_ret, ann_vol)
        "MSFT.US":    (1.1, 0.32, 0.22),
        "META.US":    (1.3, 0.48, 0.35),
        "LLY.US":     (0.7, 0.28, 0.24),
        "ASML.AS":    (1.2, 0.24, 0.28),
        "SAP.DE":     (0.9, 0.22, 0.20),
        "NOVO.DE":    (0.8, 0.18, 0.22),
        "AVGO.US":    (1.4, 0.44, 0.30),
        "MA.US":      (1.0, 0.18, 0.18),
        "UNH.US":     (0.6, 0.14, 0.16),
        "AAPL.US":    (1.1, 0.20, 0.22),
        "AMZN.US":    (1.2, 0.22, 0.26),
        "V.US":       (0.9, 0.16, 0.18),
        "BTC-EUR.CC": (1.5, 0.55, 0.75),
        "ETH-EUR.CC": (1.6, 0.45, 0.85),
    }

    for sym, _, val, _, mom, price, sma200 in _DEMO_POSITIONS_SPEC:
        beta, ann_ret, ann_vol = _symbol_params.get(sym, (1.0, 0.15, 0.25))
        rets = _generate_demo_returns(rng, lookback, ann_ret, ann_vol, beta, market_daily)
        constituent_returns[sym] = pd.Series(rets, index=all_dates)

    # ── Portfolio returns (value-weighted) ────────────────────────────────────
    total_value = sum(v for _, _, _, v, _, _, _ in _DEMO_POSITIONS_SPEC)
    port_returns_arr = np.zeros(lookback)
    for sym, _, _, val, _, _, _ in _DEMO_POSITIONS_SPEC:
        w = val / total_value
        port_returns_arr += w * constituent_returns[sym].values

    portfolio_returns = pd.Series(port_returns_arr, index=all_dates)

    # ── Benchmark returns (add small noise vs raw market so beta ≠ 1.000) ────
    bm_noise        = rng.normal(0, 0.001, lookback)
    benchmark_rets  = pd.Series(market_daily + bm_noise, index=all_dates)

    # ── Portfolio state dict (mirrors Script 13 format) ───────────────────────
    positions = {}
    for sym, sector, mkt_cap, val_eur, mom, price, sma200 in _DEMO_POSITIONS_SPEC:
        shares    = max(1, int(val_eur / price))
        entry_px  = price * rng.uniform(0.70, 0.88)  # bought in at a lower price
        positions[sym] = {
            "symbol":               sym,
            "current_value_eur":    float(val_eur),
            "market_value_eur":     float(val_eur),
            "current_price":        float(price),
            "entry_price":          float(entry_px),
            "shares":               shares,
            "sma200":               float(sma200),
            "momentum_score":       float(mom),
        }

    portfolio_state = {
        "account_equity_eur": float(total_value),
        "positions":          positions,
    }

    # ── Company info dict (mirrors Script 02 format) ──────────────────────────
    company_info = {}
    for sym, sector, mkt_cap, val_eur, mom, price, sma200 in _DEMO_POSITIONS_SPEC:
        company_info[sym] = {
            "sector":    sector,
            "marketCap": float(mkt_cap),
        }

    # Store benchmark returns so benchmark metrics can use them directly
    # (inject into constituent_returns under the benchmark key)
    constituent_returns[benchmark_symbol] = benchmark_rets

    logger.info("DEMO MODE: synthetic data ready — %d positions, €%.0f total value",
                len(positions), total_value)
    return portfolio_state, company_info, portfolio_returns, constituent_returns, benchmark_rets


def run_demo(
    as_of: date,
    lookback: int,
    benchmark_symbol: str,
    top_n_corr: int,
    dry_run: bool,
    generate_pdf: bool,
) -> "RiskReport":
    """
    Execute the full risk analytics pipeline using synthetic demo data.

    Bypasses all file I/O for inputs; outputs are written normally
    (unless --dry-run is set).
    """
    logger.info("=" * 68)
    logger.info("RISK ANALYTICS  [DEMO MODE]  |  as_of=%s  lookback=%d", as_of, lookback)
    logger.info("=" * 68)

    # ── Generate synthetic data ───────────────────────────────────────────────
    (
        portfolio_state,
        company_info,
        portfolio_returns,
        constituent_returns,
        _benchmark_rets,
    ) = build_demo_state(as_of, lookback, benchmark_symbol)

    positions      = portfolio_state["positions"]
    account_equity = portfolio_state["account_equity_eur"]

    report = RiskReport(
        as_of_date=str(as_of),
        lookback_days=lookback,
        account_equity_eur=account_equity,
        position_count=len(positions),
    )

    # ── VaR / CVaR ────────────────────────────────────────────────────────────
    logger.info("Step 3: Computing VaR / CVaR...")
    report.var_results = compute_all_var(portfolio_returns, account_equity)

    # ── Benchmark metrics ─────────────────────────────────────────────────────
    logger.info("Step 4: Computing benchmark metrics (%s) [demo]...", benchmark_symbol)
    # Use the pre-generated benchmark series directly (no parquet lookup needed)
    bm_rets    = _benchmark_rets.loc[portfolio_returns.index.intersection(_benchmark_rets.index)]
    port_align = portfolio_returns.loc[bm_rets.index]

    bm_name = BENCHMARK_NAMES.get(benchmark_symbol, benchmark_symbol)
    bm      = BenchmarkMetrics(
        benchmark_symbol=benchmark_symbol,
        benchmark_name=bm_name,
        lookback_days=lookback,
    )
    common       = port_align.index
    Rp           = port_align.values
    Rb           = bm_rets.values
    bm.shared_trading_days = len(common)

    slope, intercept, r_value, _p, _se = scipy_stats.linregress(Rb, Rp)
    bm.beta       = round(slope, 4)
    bm.r_squared  = round(r_value**2, 4)
    bm.alpha_ann_pct = round(intercept * TRADING_DAYS_PER_YEAR * 100, 4)

    bm.portfolio_return_pct = round((math.exp(float(Rp.sum())) - 1) * 100, 4)
    bm.benchmark_return_pct = round((math.exp(float(Rb.sum())) - 1) * 100, 4)
    bm.active_return_pct    = round(bm.portfolio_return_pct - bm.benchmark_return_pct, 4)

    bm.portfolio_vol_ann_pct  = round(Rp.std() * math.sqrt(TRADING_DAYS_PER_YEAR) * 100, 4)
    bm.benchmark_vol_ann_pct  = round(Rb.std() * math.sqrt(TRADING_DAYS_PER_YEAR) * 100, 4)

    active_daily              = Rp - Rb
    te_daily                  = active_daily.std()
    bm.tracking_error_ann_pct = round(te_daily * math.sqrt(TRADING_DAYS_PER_YEAR) * 100, 4)

    years_covered = len(common) / TRADING_DAYS_PER_YEAR
    if years_covered > 0 and bm.tracking_error_ann_pct > 0:
        ar_ann = bm.active_return_pct / years_covered
        bm.information_ratio = round(ar_ann / bm.tracking_error_ann_pct, 4)

    rfr_daily = RISK_FREE_RATE_ANN / TRADING_DAYS_PER_YEAR
    if Rp.std() > 0:
        bm.sharpe_ratio = round(
            (Rp.mean() - rfr_daily) / Rp.std() * math.sqrt(TRADING_DAYS_PER_YEAR), 4
        )
    if Rb.std() > 0:
        bm.benchmark_sharpe = round(
            (Rb.mean() - rfr_daily) / Rb.std() * math.sqrt(TRADING_DAYS_PER_YEAR), 4
        )
    report.benchmark_metrics = bm

    # Remove benchmark from constituent_returns so it doesn't pollute correlation
    corr_constituents = {k: v for k, v in constituent_returns.items()
                         if k not in BENCHMARK_SYMBOLS}

    # ── Concentration ─────────────────────────────────────────────────────────
    logger.info("Step 5: Computing concentration metrics...")
    report.concentration = compute_concentration(positions)

    # ── Correlation ───────────────────────────────────────────────────────────
    logger.info("Step 6: Computing correlation matrix (top %d positions)...", top_n_corr)
    report.correlation = compute_correlation_matrix(
        corr_constituents, positions, top_n=top_n_corr, lookback=min(63, lookback)
    )

    # ── Factor exposures ──────────────────────────────────────────────────────
    logger.info("Step 7: Computing factor exposures...")
    report.factor_exposure = compute_factor_exposures(positions, company_info)

    # ── Drawdown ──────────────────────────────────────────────────────────────
    logger.info("Step 8: Computing drawdown metrics...")
    report.drawdown = compute_drawdown_metrics(portfolio_returns, lookback)

    # ── Composite risk score ──────────────────────────────────────────────────
    logger.info("Step 9: Computing composite risk score...")
    score, label, alerts = compute_risk_score(report)
    report.risk_score = score
    report.risk_label = label
    report.alerts     = alerts

    logger.info("Composite risk score: %d/10 — %s", score, label)

    # ── Export ────────────────────────────────────────────────────────────────
    if not dry_run:
        date_str = str(as_of)
        json_path = RISK_DIR / f"{date_str}_risk_metrics_demo.json"
        json_path.parent.mkdir(parents=True, exist_ok=True)
        with open(json_path, "w", encoding="utf-8") as fh:
            json.dump(report.to_dict(), fh, indent=2, default=str)
        logger.info("JSON written: %s", json_path)

        csv_path = REPORTS_DIR / f"risk_{date_str}_demo.csv"
        export_csv(report, csv_path)

        if generate_pdf:
            pdf_path = REPORTS_DIR / f"risk_{date_str}_demo.pdf"
            generate_pdf_report(report, pdf_path)
    else:
        logger.info("DRY RUN: No files written.")

    logger.info("=" * 68)
    logger.info("RISK ANALYTICS DEMO COMPLETE")
    logger.info("=" * 68)
    return report


# ============================================================================
# CLI
# ============================================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Script 23: Risk Analytics — VaR, CVaR, Beta, TE, HHI, Correlation, Factors",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--as-of",
        default=str(date.today()),
        help="Valuation date YYYY-MM-DD (default: today)",
    )
    parser.add_argument(
        "--lookback",
        type=int,
        default=252,
        help="Trading-day lookback for return history (default: 252)",
    )
    parser.add_argument(
        "--benchmark",
        default="SPY.US",
        choices=list(BENCHMARK_NAMES.keys()),
        help="Benchmark symbol (default: SPY.US)",
    )
    parser.add_argument(
        "--account-equity",
        type=float,
        default=None,
        help="Account equity in EUR (overrides portfolio_state.json)",
    )
    parser.add_argument(
        "--top-n-corr",
        type=int,
        default=10,
        help="Number of top positions in correlation matrix (default: 10)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute metrics but do not write output files",
    )
    parser.add_argument(
        "--no-pdf",
        action="store_true",
        help="Skip PDF generation (faster)",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help=(
            "Generate a synthetic 15-position portfolio and run full analytics. "
            "Use when upstream data pipeline (Scripts 1-13) has not yet been executed."
        ),
    )
    add_strategy_argument(parser)
    return parser.parse_args()


def _run_for_strategy(strategy: "StrategyDef", args) -> int:
    """Run Script 23 for one strategy with namespaced I/O paths."""
    global REPORTS_DIR

    strat_signals   = strategy.signals_dir(DATA_CACHE_DIR)
    strat_portfolio = strategy.portfolio_dir(DATA_CACHE_DIR)
    strat_reports   = strategy.reports_dir(PROJECT_ROOT, 'performance')
    strat_signals.mkdir(parents=True, exist_ok=True)
    strat_portfolio.mkdir(parents=True, exist_ok=True)
    strat_reports.mkdir(parents=True, exist_ok=True)

    logger.info(f"\n[{strategy.name}] -- {strategy.label} ({'LIVE' if strategy.deployed else 'PAPER'}) --")
    logger.info(f"[{strategy.name}] Signals   : {strat_signals}")
    logger.info(f"[{strategy.name}] Portfolio : {strat_portfolio}")
    logger.info(f"[{strategy.name}] Reports   : {strat_reports}")

    _orig_reports = REPORTS_DIR
    REPORTS_DIR = strat_reports
    try:
        rc = _run_core(args, strategy.name)
        return rc if isinstance(rc, int) else 0
    finally:
        REPORTS_DIR = _orig_reports


def _run_core(args, strategy_name: str = '') -> None:

    try:
        as_of = date.fromisoformat(args.as_of)
    except ValueError:
        print(f"ERROR: Invalid date format '{args.as_of}'. Use YYYY-MM-DD.", file=sys.stderr)
        return 1

    setup_logging(str(as_of))

    if not REPORTLAB_AVAILABLE and not args.no_pdf:
        logger.warning(
            "ReportLab not installed.  Install with: pip install reportlab\n"
            "PDF generation will be skipped."
        )

    if args.demo:
        logger.info("Running in DEMO mode — synthetic portfolio data will be used.")
        report = run_demo(
            as_of=as_of,
            lookback=args.lookback,
            benchmark_symbol=args.benchmark,
            top_n_corr=args.top_n_corr,
            dry_run=args.dry_run,
            generate_pdf=(not args.no_pdf),
        )
    else:
        report = run_risk_analytics(
            as_of=as_of,
            lookback=args.lookback,
            benchmark_symbol=args.benchmark,
            account_equity_override=args.account_equity,
            top_n_corr=args.top_n_corr,
            dry_run=args.dry_run,
            generate_pdf=(not args.no_pdf),
        )

    # Console summary
    print("\n" + "=" * 68)
    print(f"RISK SUMMARY  |  {report.as_of_date}  |  Score: {report.risk_score}/10 ({report.risk_label})")
    print("=" * 68)

    var_95_1d = next(
        (v for v in report.var_results if v.confidence_pct == 95.0 and v.horizon_days == 1),
        None,
    )
    if var_95_1d:
        print(f"  1-day 95% VaR:   €{var_95_1d.var_eur:>10,.2f}  ({var_95_1d.var_pct:.2f}%)")
        print(f"  1-day 95% CVaR:  €{var_95_1d.cvar_eur:>10,.2f}  ({var_95_1d.cvar_pct:.2f}%)")

    var_99_1d = next(
        (v for v in report.var_results if v.confidence_pct == 99.0 and v.horizon_days == 1),
        None,
    )
    if var_99_1d:
        print(f"  1-day 99% VaR:   €{var_99_1d.var_eur:>10,.2f}  ({var_99_1d.var_pct:.2f}%)")

    if report.benchmark_metrics:
        bm = report.benchmark_metrics
        print(f"  Beta:            {bm.beta:>10.4f}")
        print(f"  Tracking Error:  {bm.tracking_error_ann_pct:>10.2f}%  (ann)")
        print(f"  Information Ratio:{bm.information_ratio:>9.4f}")
        print(f"  Sharpe Ratio:    {bm.sharpe_ratio:>10.4f}")

    if report.concentration:
        c = report.concentration
        print(f"  HHI:             {c.hhi:>10.6f}  (Effective N: {c.effective_n:.1f})")
        print(f"  Concentration:   {c.regime}")

    if report.drawdown:
        dd = report.drawdown
        print(f"  Max Drawdown:    {dd.max_drawdown_pct:>10.2f}%")
        print(f"  Calmar Ratio:    {dd.calmar_ratio:>10.4f}")

    if report.alerts:
        print("\n  ALERTS:")
        for a in report.alerts:
            print(f"    ⚠  {a}")

    print("=" * 68 + "\n")


def main() -> int:
    args = parse_args()
    logger.info("=" * 70)
    logger.info("Script 23 -- Architecture v3.9 (Mar 2026)")
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
