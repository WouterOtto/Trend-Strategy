#!/usr/bin/env python3
"""
Script 14: Daily Portfolio Monitor
=====================================
Run every trading day (post-market close) to check portfolio health,
generate alerts, and produce a structured daily monitoring report.

Purpose:
    Provides a systematic, rule-based daily surveillance layer over the
    live portfolio.  It evaluates nine independent risk dimensions and
    produces machine-readable + human-readable outputs.  No trades are
    executed here.  The script recommends; the human acts.

    This script is designed to run AFTER Script 01 (--mode incremental)
    has refreshed market data for the day.

Monitoring Checks (evaluated in full every run):
âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
  CHECK 1 ââ Data Staleness        (CRITICAL if > 3 trading days)
  CHECK 2 ââ Stop-Loss Hits        (HIGH     if close <= stop_price)
  CHECK 3 ââ Stop Proximity        (HIGH     if distance to stop < 3%)
                                   (MEDIUM   if distance to stop < 5%)
  CHECK 4 ââ Portfolio Drawdown    (HIGH     if drawdown < â15%)
                                   (MEDIUM   if drawdown < â10%)
  CHECK 5 ââ Concentration Creep   (HIGH     if top-3 positions > 30%)
  CHECK 6 ââ Single Position Size  (MEDIUM   if any position > 10%)
  CHECK 7 ââ Trend Signal          (HIGH     if SMA-50 < SMA-200 death cross)
                                   (MEDIUM   if ADX-14 < 20 for 3 days)
  CHECK 8 ââ Correlation Risk      (MEDIUM   if max pairwise > 0.85)
  CHECK 9 ââ VIX Spike             (HIGH     if VIX > 40, MEDIUM if > 30)

Alert severity levels:
    CRITICAL  ââ Halt all trading immediately, manual intervention required
    HIGH      ââ Immediate action required (next session open)
    MEDIUM    ââ Action required within 1–2 sessions
    LOW       ââ Informational / watch-list

Position-level detail produced for every held symbol:
    - Current price, stop price, distance to stop (%)
    - Unrealized P&L (EUR and %)
    - Trend status: SMA-50 vs SMA-200, ADX-14 level
    - Days in trade
    - Stop type (initial / trailing)

Dependencies (must be current before running):
    01_download_eodhd_bulk.py         (--mode incremental, data fresh)
    05_calculate_indicators.py        (indicator parquet files current)
    08_calculate_stops.py             (stop_levels.json current)

Inputs:
    data/portfolio_state.json                         (current positions)
    data_cache/portfolio/stop_levels.json             (Script 08 output)
    data_cache/indicators/{symbol}_indicators.parquet (Script 05 output)
    data_cache/signals/exit_signals.json              (Script 10 output, optional)
    data_cache/metadata/last_update.json              (data freshness)
    config/strategy_parameters.json                   (optional overrides)

Outputs:
    reports/daily/{YYYY-MM-DD}_monitoring.json        (machine-readable full report)
    reports/daily/{YYYY-MM-DD}_monitoring.csv         (position-level detail table)
    logs/daily_monitoring_{timestamp}.log

Execution:
    # Standard daily run (uses today's date)
    python scripts/14_daily_monitoring.py

    # Run for a specific date (backtesting / backfill)
    python scripts/14_daily_monitoring.py --as-of-date 2026-02-10

    # Include VIX reading for spike check
    python scripts/14_daily_monitoring.py --vix 24.5

    # Dry run â compute all checks but write no output files
    python scripts/14_daily_monitoring.py --dry-run

    # Suppress console output (cron usage)
    python scripts/14_daily_monitoring.py --quiet

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
# PATH CONFIGURATION
# ============================================================================

PROJECT_ROOT   = Path(__file__).parent.parent
DATA_DIR       = PROJECT_ROOT / "data"
DATA_CACHE_DIR = PROJECT_ROOT / "data_cache"
INDICATORS_DIR = DATA_CACHE_DIR / "indicators"
PORTFOLIO_DIR  = DATA_CACHE_DIR / "portfolio"
SIGNALS_DIR    = DATA_CACHE_DIR / "signals"
METADATA_DIR   = DATA_CACHE_DIR / "metadata"
REPORTS_DIR    = PROJECT_ROOT / "reports" / "daily"
LOG_DIR        = PROJECT_ROOT / "logs"
CONFIG_DIR     = PROJECT_ROOT / "config"

# ---------------------------------------------------------------------------
# Load centralized parameters.
# ---------------------------------------------------------------------------
import sys as _sys
_sys.path.insert(0, str(PROJECT_ROOT))
from config.params import P, ConfigurationError

# ============================================================================
# STRATEGY CONSTANTS  (aligned with Architecture v3.2)
# ============================================================================

# Monitoring constants — sourced from config/strategy_parameters.json.
CB_MAX_DRAWDOWN_CRITICAL    = P.circuit_breakers.cb_drawdown_warn
CB_MAX_DRAWDOWN_WARNING     = P.circuit_breakers.cb_drawdown_watch
CB_MAX_TOP3_CONCENTRATION   = P.portfolio_constraints.max_top3_concentration_pct
CB_MAX_SINGLE_POSITION_PCT  = P.portfolio_constraints.max_single_position_pct
CB_MAX_PAIRWISE_CORRELATION = P.portfolio_constraints.max_pairwise_correlation
CB_MAX_DATA_STALENESS_DAYS  = P.circuit_breakers.cb_data_staleness_days
CB_VIX_HALT_LEVEL           = P.circuit_breakers.cb_vix_enter
CB_VIX_WARN_LEVEL           = P.circuit_breakers.cb_vix_resume
STOP_PROXIMITY_HIGH_PCT     = P.circuit_breakers.stop_proximity_high_pct
STOP_PROXIMITY_MEDIUM_PCT   = P.circuit_breakers.stop_proximity_medium_pct
ADX_WEAKNESS_EXIT           = P.trend_qualification.adx_weak
ADX_DETERIORATION_WARN      = P.trend_qualification.adx_threshold
ADX_CONSECUTIVE_DAYS        = P.trend_qualification.adx_weakness_days
CORRELATION_LOOKBACK_DAYS   = P.portfolio_constraints.correlation_lookback_days
# ============================================================================
# LOGGING
# ============================================================================

def setup_logging(as_of_date: str, quiet: bool = False) -> logging.Logger:
    """Configure dual-sink (file + stdout) logging."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file  = LOG_DIR / f"daily_monitoring_{as_of_date}_{timestamp}.log"

    logger = logging.getLogger("daily_monitoring")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    # File handler â full detail
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))
    logger.addHandler(fh)

    # Console handler â INFO and above (suppressed in quiet mode)
    if not quiet:
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(logging.INFO)
        ch.setFormatter(logging.Formatter("%(levelname)-8s | %(message)s"))
        logger.addHandler(ch)

    logger.info(f"Log file: {log_file}")
    return logger


logger: logging.Logger = logging.getLogger("daily_monitoring")


# ============================================================================
# CONFIGURATION LOADER
# ============================================================================

def load_strategy_params() -> Dict:
    """
    Load monitoring-relevant parameters from config/strategy_parameters.json.

    Falls back to module-level constants if file is absent.

    Returns:
        Dict with keys: max_data_staleness_days, max_drawdown_pct,
                        max_correlation, max_concentration_top3_pct,
                        max_vix, adx_weakness_threshold,
                        stop_proximity_high_pct, stop_proximity_medium_pct.
    """
    defaults = {
        "max_data_staleness_days":   CB_MAX_DATA_STALENESS_DAYS,
        "max_drawdown_pct":          CB_MAX_DRAWDOWN_CRITICAL * 100,
        "max_correlation":           CB_MAX_PAIRWISE_CORRELATION,
        "max_concentration_top3_pct": CB_MAX_TOP3_CONCENTRATION * 100,
        "max_vix":                   CB_VIX_HALT_LEVEL,
        "adx_weakness_threshold":    ADX_WEAKNESS_EXIT,
        "stop_proximity_high_pct":   STOP_PROXIMITY_HIGH_PCT * 100,
        "stop_proximity_medium_pct": STOP_PROXIMITY_MEDIUM_PCT * 100,
    }

    config_file = CONFIG_DIR / "strategy_parameters.json"
    if not config_file.exists():
        logger.debug("config/strategy_parameters.json not found â using built-in defaults.")
        return defaults

    try:
        with open(config_file, "r", encoding="utf-8") as f:
            config = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(f"Failed to parse strategy_parameters.json: {exc}. Using defaults.")
        return defaults

    cb = config.get("circuit_breakers", {})
    merged = {
        "max_data_staleness_days":    int(cb.get("max_data_staleness_days", defaults["max_data_staleness_days"])),
        "max_drawdown_pct":           float(cb.get("max_drawdown_pct",      defaults["max_drawdown_pct"])),
        "max_correlation":            float(cb.get("max_correlation",        defaults["max_correlation"])),
        "max_concentration_top3_pct": float(cb.get("max_concentration_top3_pct", defaults["max_concentration_top3_pct"])),
        "max_vix":                    float(cb.get("max_vix",                defaults["max_vix"])),
        "adx_weakness_threshold":     float(
            config.get("indicators", {}).get("adx_period", defaults["adx_weakness_threshold"])
        ),
        "stop_proximity_high_pct":    defaults["stop_proximity_high_pct"],
        "stop_proximity_medium_pct":  defaults["stop_proximity_medium_pct"],
    }
    logger.debug(f"Strategy params loaded from config: {merged}")
    return merged


# ============================================================================
# DATA LOADERS
# ============================================================================

def load_portfolio_state() -> Dict:
    """
    Load current portfolio holdings from data/portfolio_state.json.

    Supports canonical layout {symbol: {...}} and nested {"positions": {...}}.
    Returns empty dict if file is absent (no positions held).

    Expected position keys (all optional â missing keys handled gracefully):
        entry_price, entry_date, shares, current_value, unrealized_pnl,
        unrealized_pnl_pct, current_stop_price, initial_stop_price,
        trailing_stop_price, stop_type, stop_last_update_date,
        data_last_update, is_new_entry
    """
    state_file = DATA_DIR / "portfolio_state.json"

    if not state_file.exists():
        logger.info("portfolio_state.json not found â assuming empty portfolio.")
        return {}

    try:
        with open(state_file, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error(f"Cannot parse portfolio_state.json: {exc}")
        sys.exit(1)

    if not isinstance(raw, dict):
        logger.error("portfolio_state.json has unexpected structure.")
        sys.exit(1)

    # Unwrap nested "positions" key if present
    if "positions" in raw and isinstance(raw.get("positions"), dict):
        raw = raw["positions"]

    # Normalise: skip _meta keys, coerce non-dict values to empty dicts
    normalised: Dict = {}
    for key, value in raw.items():
        if key.startswith("_"):
            continue
        normalised[key] = value if isinstance(value, dict) else {}

    logger.info(f"â Loaded {len(normalised)} positions from portfolio_state.json")
    return normalised


def load_stop_levels() -> Dict:
    """
    Load stop-loss levels from Script 09 output.

    File: data_cache/portfolio/stop_levels.json
    Format: {"metadata": {...}, "stops": {symbol: {"stop_price": float, ...}}}

    Returns: stops dict keyed by symbol.
    Logs warning (not fatal) if missing â checks that need stop levels
    will be skipped.
    """
    stops_file = PORTFOLIO_DIR / "stop_levels.json"

    if not stops_file.exists():
        logger.warning(
            "stop_levels.json not found. Run Script 08 to enable stop-loss checks. "
            "Stop-related checks will be skipped."
        )
        return {}

    try:
        with open(stops_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error(f"Failed to load stop_levels.json: {exc}")
        return {}

    stops = data.get("stops", data)
    if not isinstance(stops, dict):
        logger.warning("stop_levels.json has unexpected structure. Stop checks skipped.")
        return {}

    logger.info(f"â Loaded {len(stops)} stop records from stop_levels.json")
    return stops


def load_exit_signals() -> Dict:
    """
    Load active exit signals from Script 10 output (optional input).

    File: data_cache/signals/exit_signals.json
    Format: {"metadata": {...}, "exit_signals": {symbol: {"exit": bool, ...}}}

    Returns: exit_signals dict keyed by symbol (only symbols with exit=True).
    Returns empty dict if file absent (monitoring proceeds without pre-computed signals).
    """
    signals_file = SIGNALS_DIR / "exit_signals.json"

    if not signals_file.exists():
        logger.debug("exit_signals.json not found â pre-computed signals not loaded.")
        return {}

    try:
        with open(signals_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(f"Failed to load exit_signals.json: {exc}")
        return {}

    all_signals = data.get("exit_signals", {})
    # Filter to only active signals
    active = {s: v for s, v in all_signals.items() if v.get("exit", False)}
    logger.info(f"â Loaded {len(active)} active exit signals from exit_signals.json")
    return active


def load_last_update_dates() -> Dict:
    """
    Load the last data download date per exchange.

    File: data_cache/metadata/last_update.json
    Format: {"NYSE": "2026-02-10", "NASDAQ": "2026-02-10", ...}

    Returns: dict keyed by exchange name.
    """
    update_file = METADATA_DIR / "last_update.json"

    if not update_file.exists():
        logger.warning("last_update.json not found â data staleness check will be skipped.")
        return {}

    try:
        with open(update_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(f"Failed to load last_update.json: {exc}")
        return {}

    logger.debug(f"Last update dates: {data}")
    return data


def load_peak_equity() -> Optional[float]:
    """
    Load peak equity from portfolio_state.json _meta section.

    Returns: float peak equity in EUR, or None if unavailable.
    """
    state_file = DATA_DIR / "portfolio_state.json"
    if not state_file.exists():
        return None

    try:
        with open(state_file, "r", encoding="utf-8") as f:
            state = json.load(f)
        if isinstance(state, dict) and "_meta" in state:
            return state["_meta"].get("peak_equity")
    except (json.JSONDecodeError, OSError):
        pass

    return None


def load_latest_indicators(symbol: str, as_of_date: str) -> Optional[pd.DataFrame]:
    """
    Load indicator time series for a symbol, filtered to â¤ as_of_date.

    File: data_cache/indicators/{symbol}_indicators.parquet

    Required columns (Script 05 output):
        close, sma_fast, sma_slow, adx

    Optional columns (used if present):
        atr_20, atr_pct, volume

    Returns:
        Filtered DataFrame sorted ascending by date, or None if unavailable.
    """
    ind_file = INDICATORS_DIR / f"{symbol}_indicators.parquet"

    if not ind_file.exists():
        logger.debug(f"No indicator file for {symbol}")
        return None

    try:
        df = pd.read_parquet(ind_file)
    except Exception as exc:
        logger.warning(f"Failed to read indicators for {symbol}: {exc}")
        return None

    # Ensure DatetimeIndex
    if not isinstance(df.index, pd.DatetimeIndex):
        try:
            df.index = pd.to_datetime(df.index)
        except Exception:
            logger.warning(f"Cannot parse index for {symbol}")
            return None

    df.sort_index(inplace=True)

    # Filter to as_of_date
    cutoff = pd.Timestamp(as_of_date)
    df = df[df.index <= cutoff]

    if df.empty:
        logger.debug(f"No data on or before {as_of_date} for {symbol}")
        return None

    return df


def load_return_series_for_correlation(
    symbols: List[str],
    as_of_date: str,
    lookback_days: int = CORRELATION_LOOKBACK_DAYS,
) -> Optional[pd.DataFrame]:
    """
    Build a return matrix for correlation estimation.

    Loads 'close' series from indicator parquet files for each symbol,
    trims to the lookback window ending on as_of_date, and returns
    a DataFrame of daily pct_change returns.

    Returns: DataFrame (dates Ã symbols) or None if < 2 symbols available.
    """
    end_dt  = pd.Timestamp(as_of_date)
    series: Dict[str, pd.Series] = {}

    for sym in symbols:
        df = load_latest_indicators(sym, as_of_date)
        if df is None or "close" not in df.columns:
            continue

        df_window = df.tail(lookback_days + 1)
        if len(df_window) < 20:
            continue

        returns = df_window["close"].pct_change().dropna()
        series[sym] = returns

    if len(series) < 2:
        return None

    return pd.DataFrame(series)


# ============================================================================
# CHECK 1 â DATA STALENESS
# ============================================================================

def check_data_staleness(as_of_date: str, last_updates: Dict, max_stale_days: int) -> List[Dict]:
    """
    Flag any exchange whose last download is more than max_stale_days old.

    Severity:
        CRITICAL  if any exchange is stale (all trading should halt)

    Returns: list of alert dicts.
    """
    if not last_updates:
        return [{
            "check":     "data_staleness",
            "severity":  "CRITICAL",
            "detail":    "last_update.json missing â cannot verify data freshness. "
                         "Run Script 01 (--mode incremental) immediately.",
            "action":    "halt_all_trading",
        }]

    as_of_dt = datetime.strptime(as_of_date, "%Y-%m-%d")
    alerts   = []

    for exchange, last_date_str in last_updates.items():
        try:
            last_dt    = datetime.strptime(last_date_str, "%Y-%m-%d")
            days_stale = (as_of_dt - last_dt).days
        except ValueError:
            continue

        if days_stale > max_stale_days:
            alerts.append({
                "check":        "data_staleness",
                "severity":     "CRITICAL",
                "exchange":     exchange,
                "last_updated": last_date_str,
                "days_stale":   days_stale,
                "threshold":    max_stale_days,
                "detail":       (
                    f"{exchange} data is {days_stale} calendar days stale "
                    f"(last update: {last_date_str}, threshold: {max_stale_days} days). "
                    "Run Script 01 (--mode incremental) before acting on any signals."
                ),
                "action":       "halt_all_trading_update_data_first",
            })

    if not alerts:
        logger.info("â CHECK 1: Data freshness â all exchanges up to date.")
    else:
        for a in alerts:
            logger.warning(f"â  CHECK 1 DATA STALENESS [{a['severity']}]: {a['detail']}")

    return alerts


# ============================================================================
# CHECK 2 â STOP-LOSS HITS
# ============================================================================

def check_stop_loss_hits(
    positions: Dict,
    stop_levels: Dict,
    latest_prices: Dict,
) -> List[Dict]:
    """
    Check whether any current position's close price breached its stop.

    Condition: close_price <= stop_price

    Severity: HIGH â immediate action required at next session open.

    Returns: list of alert dicts, one per breached stop.
    """
    if not stop_levels:
        logger.debug("CHECK 2: Stop levels unavailable â stop-loss check skipped.")
        return []

    alerts = []

    for symbol, position in positions.items():
        price_data = latest_prices.get(symbol)
        if price_data is None:
            continue

        current_price = price_data.get("close")
        if current_price is None:
            continue

        # Resolve stop price: prefer stop_levels.json (most current),
        # fall back to portfolio_state.json for backward compatibility
        stop_data  = stop_levels.get(symbol, {})
        stop_price = stop_data.get("stop_price") or stop_data.get("effective_stop_price")

        if stop_price is None:
            stop_price = position.get("current_stop_price")

        if stop_price is None:
            continue

        if current_price <= stop_price:
            entry_price = position.get("entry_price", 0) or 0
            shares      = position.get("shares", 0) or 0
            loss_per_share = current_price - entry_price if entry_price else None
            loss_eur       = loss_per_share * shares if (loss_per_share and shares) else None

            alerts.append({
                "check":         "stop_loss_hit",
                "severity":      "HIGH",
                "symbol":        symbol,
                "current_price": round(current_price, 4),
                "stop_price":    round(stop_price, 4),
                "stop_type":     stop_data.get("stop_type", position.get("stop_type", "unknown")),
                "breach_amount": round(stop_price - current_price, 4),
                "entry_price":   round(entry_price, 4) if entry_price else None,
                "loss_eur":      round(loss_eur, 2) if loss_eur is not None else None,
                "detail":        (
                    f"{symbol} close {current_price:.4f} â¤ stop {stop_price:.4f} â "
                    "stop-loss BREACHED. Exit at next session open (market order)."
                ),
                "action":        "exit_market_order_next_open",
            })

    if not alerts:
        logger.info("â CHECK 2: Stop-loss hits â no breaches detected.")
    else:
        for a in alerts:
            logger.warning(f"ð¨ CHECK 2 STOP-LOSS HIT [{a['severity']}]: {a['detail']}")

    return alerts


# ============================================================================
# CHECK 3 â STOP PROXIMITY
# ============================================================================

def check_stop_proximity(
    positions: Dict,
    stop_levels: Dict,
    latest_prices: Dict,
    high_pct: float,
    medium_pct: float,
) -> List[Dict]:
    """
    Alert when a position's price is dangerously close to its stop-loss.

    Severity:
        HIGH    if distance = (price â stop) / price < high_pct
        MEDIUM  if distance < medium_pct

    Only flags positions NOT already flagged as stop-loss hits.

    Returns: list of alert dicts.
    """
    if not stop_levels:
        return []

    alerts = []

    for symbol, position in positions.items():
        price_data = latest_prices.get(symbol)
        if price_data is None:
            continue

        current_price = price_data.get("close")
        if current_price is None or current_price <= 0:
            continue

        stop_data  = stop_levels.get(symbol, {})
        stop_price = stop_data.get("stop_price") or stop_data.get("effective_stop_price")
        if stop_price is None:
            stop_price = position.get("current_stop_price")
        if stop_price is None or stop_price <= 0:
            continue

        # Skip already-breached stops (handled by CHECK 2)
        if current_price <= stop_price:
            continue

        distance_pct = (current_price - stop_price) / current_price

        if distance_pct < high_pct:
            severity = "HIGH"
        elif distance_pct < medium_pct:
            severity = "MEDIUM"
        else:
            continue

        alerts.append({
            "check":          "stop_proximity",
            "severity":       severity,
            "symbol":         symbol,
            "current_price":  round(current_price, 4),
            "stop_price":     round(stop_price, 4),
            "distance_pct":   round(distance_pct * 100, 2),
            "stop_type":      stop_data.get("stop_type", position.get("stop_type", "unknown")),
            "detail":         (
                f"{symbol} is {distance_pct:.1%} above stop {stop_price:.4f} "
                f"(current: {current_price:.4f}) â high stop-loss risk."
            ),
            "action":         "monitor_closely" if severity == "MEDIUM" else "prepare_exit_order",
        })

    if not alerts:
        logger.info("â CHECK 3: Stop proximity â all positions have adequate buffer.")
    else:
        for a in alerts:
            logger.warning(
                f"â  CHECK 3 STOP PROXIMITY [{a['severity']}]: {a['detail']}"
            )

    return alerts


# ============================================================================
# CHECK 4 â PORTFOLIO DRAWDOWN
# ============================================================================

def check_portfolio_drawdown(
    current_equity: float,
    peak_equity: Optional[float],
    critical_threshold: float,
    warning_threshold: float,
) -> List[Dict]:
    """
    Check portfolio drawdown against critical and warning thresholds.

    Condition: drawdown = (current_equity / peak_equity) â 1

    Severity:
        HIGH    if drawdown < critical_threshold  (â15%)
        MEDIUM  if drawdown < warning_threshold   (â10%)

    Returns: list with 0 or 1 alert dicts.
    """
    if peak_equity is None or peak_equity <= 0:
        logger.debug("CHECK 4: Drawdown check skipped â peak_equity not available in portfolio_state.json._meta.")
        return []

    drawdown = (current_equity / peak_equity) - 1.0

    if drawdown >= warning_threshold:
        logger.info(
            f"â CHECK 4: Drawdown {drawdown:.1%} â within acceptable range "
            f"(warning threshold: {warning_threshold:.0%})."
        )
        return []

    severity = "HIGH" if drawdown < critical_threshold else "MEDIUM"
    threshold = critical_threshold if severity == "HIGH" else warning_threshold

    alert = {
        "check":              "portfolio_drawdown",
        "severity":           severity,
        "current_equity_eur": round(current_equity, 2),
        "peak_equity_eur":    round(peak_equity, 2),
        "drawdown_pct":       round(drawdown * 100, 2),
        "threshold_pct":      round(threshold * 100, 2),
        "detail":             (
            f"Portfolio drawdown {drawdown:.1%} vs peak â¬{peak_equity:,.0f}. "
            f"Current equity â¬{current_equity:,.0f}. "
            f"Threshold: {threshold:.0%}."
        ),
        "action":             (
            "halt_entries_review_risk_immediately"
            if severity == "HIGH"
            else "review_positions_reduce_risk"
        ),
    }

    logger.warning(f"â  CHECK 4 PORTFOLIO DRAWDOWN [{severity}]: {alert['detail']}")
    return [alert]


# ============================================================================
# CHECK 5 â CONCENTRATION CREEP (TOP-3)
# ============================================================================

def check_concentration_risk(
    positions: Dict,
    current_equity: float,
    latest_prices: Dict,
    max_top3_pct: float,
) -> List[Dict]:
    """
    Check if the three largest positions together exceed the concentration limit.

    Condition: sum(top-3 position values) / current_equity > max_top3_pct

    Current value is taken from the latest price Ã shares (most accurate),
    falling back to portfolio_state.json current_value if unavailable.

    Severity: HIGH

    Returns: list with 0 or 1 alert dict.
    """
    if current_equity <= 0 or not positions:
        return []

    # Compute current market value for each position
    position_values: List[Tuple[str, float]] = []

    for symbol, pos in positions.items():
        price_data  = latest_prices.get(symbol)
        shares      = pos.get("shares", 0) or 0

        if price_data and price_data.get("close") and shares:
            value = price_data["close"] * shares
        else:
            value = pos.get("current_value", 0) or 0

        if value > 0:
            position_values.append((symbol, value))

    position_values.sort(key=lambda x: x[1], reverse=True)
    top3         = position_values[:3]
    top3_value   = sum(v for _, v in top3)
    top3_pct     = top3_value / current_equity
    top3_symbols = [s for s, _ in top3]

    if top3_pct <= max_top3_pct:
        logger.info(
            f"â CHECK 5: Top-3 concentration {top3_pct:.1%} â within limit ({max_top3_pct:.0%})."
        )
        return []

    alert = {
        "check":          "concentration_creep",
        "severity":       "HIGH",
        "top3_symbols":   top3_symbols,
        "top3_pct":       round(top3_pct * 100, 2),
        "top3_value_eur": round(top3_value, 2),
        "threshold_pct":  round(max_top3_pct * 100, 2),
        "detail":         (
            f"Top-3 positions ({', '.join(top3_symbols)}) represent "
            f"{top3_pct:.1%} of equity (limit: {max_top3_pct:.0%}). "
            "Concentration has crept above threshold â consider trimming."
        ),
        "action":         "halt_new_entries_trim_largest_positions",
    }

    logger.warning(f"â  CHECK 5 CONCENTRATION CREEP [HIGH]: {alert['detail']}")
    return [alert]


# ============================================================================
# CHECK 6 â SINGLE POSITION SIZE
# ============================================================================

def check_single_position_size(
    positions: Dict,
    current_equity: float,
    latest_prices: Dict,
    max_single_pct: float,
) -> List[Dict]:
    """
    Flag any individual position that exceeds the single-position cap.

    Condition: position_value / current_equity > max_single_pct

    Severity: MEDIUM

    Returns: list of alert dicts (one per oversized position).
    """
    if current_equity <= 0:
        return []

    alerts = []

    for symbol, pos in positions.items():
        price_data = latest_prices.get(symbol)
        shares     = pos.get("shares", 0) or 0

        if price_data and price_data.get("close") and shares:
            value = price_data["close"] * shares
        else:
            value = pos.get("current_value", 0) or 0

        if value <= 0:
            continue

        position_pct = value / current_equity

        if position_pct > max_single_pct:
            alerts.append({
                "check":          "single_position_size",
                "severity":       "MEDIUM",
                "symbol":         symbol,
                "position_pct":   round(position_pct * 100, 2),
                "position_eur":   round(value, 2),
                "threshold_pct":  round(max_single_pct * 100, 2),
                "detail":         (
                    f"{symbol} represents {position_pct:.1%} of equity "
                    f"(â¬{value:,.0f}) â exceeds {max_single_pct:.0%} single-position cap. "
                    "Consider trimming at next rebalancing."
                ),
                "action":         "trim_at_next_rebalancing",
            })

    if not alerts:
        logger.info(
            f"â CHECK 6: Single-position sizes â all within {max_single_pct:.0%} limit."
        )
    else:
        for a in alerts:
            logger.warning(f"â  CHECK 6 SINGLE POSITION [{a['severity']}]: {a['detail']}")

    return alerts


# ============================================================================
# CHECK 7 â TREND SIGNALS (DEATH CROSS + ADX WEAKNESS)
# ============================================================================

def check_trend_signals(
    positions: Dict,
    latest_prices: Dict,
    adx_weakness_threshold: float,
    adx_consecutive_days: int,
) -> List[Dict]:
    """
    Detect intra-month trend deterioration for currently held positions.

    Two conditions checked independently:

    7a) Death Cross:
        SMA_50 < SMA_200 on latest bar → uptrend structurally lost.
        Severity: HIGH â mandatory exit at next session open.

    7b) ADX Weakness:
        ADX_14 < adx_weakness_threshold for â¥ adx_consecutive_days consecutive bars.
        Severity: MEDIUM â trend momentum dissipating.

    Note: These checks complement Script 10 (which produces the formal
    exit signal file). This check operates directly on indicator data
    and is therefore valid even if Script 10 has not been run today.

    Returns: list of alert dicts.
    """
    alerts = []

    for symbol in positions:
        ind = latest_prices.get(symbol)
        if ind is None:
            continue

        df_full = ind.get("_df_full")  # Set during price loading; may be absent
        if df_full is None:
            continue

        # â 7a: Death Cross âââââââââââââââââââââââââââââââââââââââââââââââââââ
        sma50  = ind.get("sma_fast")
        sma200 = ind.get("sma_slow")

        if sma50 is not None and sma200 is not None:
            if sma50 < sma200:
                alerts.append({
                    "check":     "trend_reversal_death_cross",
                    "severity":  "HIGH",
                    "symbol":    symbol,
                    "sma_fast":    round(sma50, 4),
                    "sma_slow":   round(sma200, 4),
                    "detail":    (
                        f"{symbol} SMA-50 ({sma50:.2f}) crossed below SMA-200 ({sma200:.2f}) "
                        "â death cross detected. Primary uptrend lost. "
                        "Exit at next session open (market order)."
                    ),
                    "action":    "exit_market_order_next_open",
                })

        # â 7b: ADX Weakness ââââââââââââââââââââââââââââââââââââââââââââââââââ
        if "adx" in df_full.columns and len(df_full) >= adx_consecutive_days:
            recent_adx = df_full["adx"].dropna().tail(adx_consecutive_days)

            if len(recent_adx) == adx_consecutive_days:
                if (recent_adx < adx_weakness_threshold).all():
                    current_adx = float(recent_adx.iloc[-1])
                    alerts.append({
                        "check":            "trend_weakness_adx",
                        "severity":         "MEDIUM",
                        "symbol":           symbol,
                        "adx":           round(current_adx, 2),
                        "consecutive_days": adx_consecutive_days,
                        "threshold":        adx_weakness_threshold,
                        "detail":           (
                            f"{symbol} ADX-14 has been below {adx_weakness_threshold} "
                            f"for {adx_consecutive_days} consecutive days "
                            f"(latest: {current_adx:.1f}). "
                            "Trend momentum dissipated â consider exit."
                        ),
                        "action":           "exit_market_order_next_open",
                    })
                elif (recent_adx < ADX_DETERIORATION_WARN).all():
                    # Approaching weakness threshold â early warning
                    current_adx = float(recent_adx.iloc[-1])
                    alerts.append({
                        "check":            "trend_deterioration_warning",
                        "severity":         "LOW",
                        "symbol":           symbol,
                        "adx":           round(current_adx, 2),
                        "consecutive_days": adx_consecutive_days,
                        "threshold":        ADX_DETERIORATION_WARN,
                        "detail":           (
                            f"{symbol} ADX-14 {current_adx:.1f} below {ADX_DETERIORATION_WARN} "
                            f"â trend weakening. Monitor closely."
                        ),
                        "action":           "monitor_closely",
                    })

    high_count   = sum(1 for a in alerts if a["severity"] == "HIGH")
    medium_count = sum(1 for a in alerts if a["severity"] == "MEDIUM")
    low_count    = sum(1 for a in alerts if a["severity"] == "LOW")

    if not any(a["severity"] in {"HIGH", "MEDIUM"} for a in alerts):
        logger.info("â CHECK 7: Trend signals â no critical or medium trend alerts.")
    else:
        if high_count:
            logger.warning(f"â  CHECK 7 TREND SIGNALS: {high_count} HIGH alert(s) "
                           f"(death cross), {medium_count} MEDIUM (ADX weakness).")

    return alerts


# ============================================================================
# CHECK 8 â CORRELATION RISK
# ============================================================================

def check_correlation_risk(
    positions: Dict,
    as_of_date: str,
    max_correlation: float,
) -> List[Dict]:
    """
    Check if any pair of top-10 positions is excessively correlated.

    Uses 60-day rolling daily returns.  Requires â¥ 2 position symbols
    with at least 20 observations each.

    Severity: MEDIUM

    Returns: list with 0 or 1 alert dict.
    """
    if len(positions) < 2:
        logger.debug("CHECK 8: Correlation check skipped â fewer than 2 positions.")
        return []

    # Use top-10 positions by current_value
    sorted_positions = sorted(
        positions.items(),
        key=lambda kv: kv[1].get("current_value", 0) if isinstance(kv[1], dict) else 0,
        reverse=True,
    )
    top10_symbols = [sym for sym, _ in sorted_positions[:10]]

    returns_df = load_return_series_for_correlation(top10_symbols, as_of_date)

    if returns_df is None or len(returns_df.columns) < 2:
        logger.debug(
            "CHECK 8: Correlation check skipped â insufficient return data "
            "for top positions."
        )
        return []

    corr_matrix = returns_df.corr()
    corr_vals = corr_matrix.to_numpy().copy()          # writable copy — .values can be read-only in NumPy ≥1.24
    np.fill_diagonal(corr_vals, np.nan)
    max_corr = float(np.nanmax(corr_vals))

    if max_corr <= max_correlation:
        logger.info(
            f"â CHECK 8: Max pairwise correlation {max_corr:.3f} â within limit ({max_correlation})."
        )
        return []

    # Identify the highest-correlated pair
    corr_upper = corr_matrix.where(
        np.triu(np.ones(corr_matrix.shape, dtype=bool), k=1)
    )
    pair_idx = corr_upper.stack().idxmax()

    alert = {
        "check":           "correlation_risk",
        "severity":        "MEDIUM",
        "max_correlation": round(max_corr, 4),
        "threshold":       max_correlation,
        "highest_pair":    list(pair_idx),
        "detail":          (
            f"Max pairwise correlation {max_corr:.3f} among top-10 positions "
            f"exceeds {max_correlation} threshold. "
            f"Highest correlated pair: {pair_idx[0]} / {pair_idx[1]}. "
            "Portfolio may lack diversification â review at next rebalancing."
        ),
        "action":          "review_at_next_rebalancing",
    }

    logger.warning(f"â  CHECK 8 CORRELATION [{alert['severity']}]: {alert['detail']}")
    return [alert]


# ============================================================================
# CHECK 9 â VIX SPIKE
# ============================================================================

def check_vix(
    vix_level: Optional[float],
    halt_level: float,
    warn_level: float,
) -> List[Dict]:
    """
    Check VIX for elevated volatility regime.

    Severity:
        HIGH    if VIX > halt_level (40)
        MEDIUM  if VIX > warn_level (30)

    Returns: list with 0 or 1 alert dict, or empty if no VIX provided.
    """
    if vix_level is None:
        logger.debug("CHECK 9: VIX check skipped â no VIX value provided (use --vix).")
        return []

    if vix_level > halt_level:
        severity = "HIGH"
        action   = f"halt_new_entries_until_vix_below_{warn_level}_for_3_days"
        detail   = (
            f"VIX {vix_level:.1f} exceeds halt level {halt_level}. "
            "Extreme volatility regime â halt all new entries. "
            f"Resume when VIX < {warn_level} for 3 consecutive trading days."
        )
    elif vix_level > warn_level:
        severity = "MEDIUM"
        action   = "reduce_position_sizing_caution_on_new_entries"
        detail   = (
            f"VIX {vix_level:.1f} in elevated range (above {warn_level}). "
            "Consider reduced position sizing for any new entries."
        )
    else:
        logger.info(f"â CHECK 9: VIX {vix_level:.1f} â within normal range.")
        return []

    alert = {
        "check":     "vix_spike",
        "severity":  severity,
        "vix_level": vix_level,
        "threshold": halt_level if severity == "HIGH" else warn_level,
        "detail":    detail,
        "action":    action,
    }

    logger.warning(f"â  CHECK 9 VIX [{severity}]: {detail}")
    return [alert]


# ============================================================================
# LOAD ALL LATEST PRICES + INDICATORS  (single pass for performance)
# ============================================================================

def load_all_latest_prices(
    symbols: List[str],
    as_of_date: str,
) -> Dict[str, Dict]:
    """
    Load the latest bar's key values for all symbols in a single pass.

    Caches the full DataFrame as "_df_full" in the returned dict so that
    trend checks (CHECK 7) can inspect multiple bars without re-reading
    the parquet files.

    Returns:
        {
            "AAPL.US": {
                "close":    153.42,
                "sma_fast":   148.21,
                "sma_slow":  142.50,
                "adx":   28.3,
                "atr_20":   3.41,
                "atr_pct": 2.23,
                "volume":   52_000_000,
                "date":     "2026-02-10",
                "_df_full": <DataFrame>,   # for multi-bar trend checks
            },
            ...
        }
    """
    result: Dict[str, Dict] = {}

    for symbol in symbols:
        df = load_latest_indicators(symbol, as_of_date)
        if df is None or df.empty:
            continue

        latest = df.iloc[-1]
        row: Dict = {
            "_df_full": df,
            "date":     df.index[-1].strftime("%Y-%m-%d"),
        }

        for col in ["close", "sma_fast", "sma_slow", "adx",
                    "atr_20", "atr_pct", "volume"]:
            val = latest.get(col) if hasattr(latest, "get") else (
                latest[col] if col in df.columns else None
            )
            if val is not None and not (isinstance(val, float) and np.isnan(val)):
                row[col] = float(val)
            else:
                row[col] = None

        result[symbol] = row

    logger.info(f"â Price snapshot loaded for {len(result)}/{len(symbols)} symbols.")
    return result


# ============================================================================
# PORTFOLIO METRICS CALCULATION
# ============================================================================

def calculate_current_equity(
    positions: Dict,
    latest_prices: Dict,
    account_equity_override: Optional[float] = None,
) -> float:
    """
    Compute total portfolio market value from live prices.

    If account_equity_override is provided (from --account-equity CLI arg),
    use that directly (accounts for cash + positions not in our data).

    Otherwise, sum position values from latest_prices Ã shares.
    Falls back to portfolio_state.json current_value where prices are unavailable.
    """
    if account_equity_override is not None and account_equity_override > 0:
        return account_equity_override

    total = 0.0
    for symbol, pos in positions.items():
        price_data = latest_prices.get(symbol)
        shares     = pos.get("shares", 0) or 0

        if price_data and price_data.get("close") and shares:
            total += price_data["close"] * shares
        else:
            total += pos.get("current_value", 0) or 0

    return total


def build_position_detail(
    positions: Dict,
    stop_levels: Dict,
    latest_prices: Dict,
    as_of_date: str,
) -> List[Dict]:
    """
    Build a per-position detail table for the monitoring report.

    Each row includes:
        symbol, current_price, entry_price, shares, position_value_eur,
        unrealized_pnl_eur, unrealized_pnl_pct, stop_price, stop_type,
        distance_to_stop_pct, trend_status (bull/bear/neutral), sma_fast,
        sma_slow, adx, atr_pct, days_in_trade, data_date.

    Returns: list of dicts, sorted by unrealized_pnl_pct descending.
    """
    rows = []
    today = datetime.strptime(as_of_date, "%Y-%m-%d").date()

    for symbol, pos in positions.items():
        price_data = latest_prices.get(symbol, {})
        stop_data  = stop_levels.get(symbol, {})

        current_price = price_data.get("close")
        entry_price   = pos.get("entry_price")
        shares        = pos.get("shares", 0) or 0

        # Position value and P&L
        position_value = (current_price * shares) if (current_price and shares) else pos.get("current_value", 0)
        if current_price and entry_price and shares:
            unrealized_pnl     = (current_price - entry_price) * shares
            unrealized_pnl_pct = ((current_price / entry_price) - 1.0) * 100
        else:
            unrealized_pnl     = pos.get("unrealized_pnl")
            unrealized_pnl_pct = pos.get("unrealized_pnl_pct")

        # Stop price
        stop_price = (
            stop_data.get("stop_price")
            or stop_data.get("effective_stop_price")
            or pos.get("current_stop_price")
        )
        stop_type = stop_data.get("stop_type") or pos.get("stop_type", "unknown")

        # Distance to stop
        distance_to_stop_pct = None
        if current_price and stop_price and current_price > 0:
            distance_to_stop_pct = round(
                ((current_price - stop_price) / current_price) * 100, 2
            )

        # Trend status
        sma_fast  = price_data.get("sma_fast")
        sma_slow = price_data.get("sma_slow")
        if sma_fast and sma_slow:
            if sma_fast > sma_slow and (current_price or 0) > sma_fast:
                trend_status = "BULL"
            elif sma_fast < sma_slow:
                trend_status = "BEAR"
            else:
                trend_status = "NEUTRAL"
        else:
            trend_status = "UNKNOWN"

        # Days in trade
        entry_date_str = pos.get("entry_date")
        days_in_trade  = None
        if entry_date_str:
            try:
                entry_dt      = datetime.strptime(entry_date_str[:10], "%Y-%m-%d").date()
                days_in_trade = (today - entry_dt).days
            except ValueError:
                pass

        rows.append({
            "symbol":               symbol,
            "data_date":            price_data.get("date", "N/A"),
            "current_price":        round(current_price, 4) if current_price else None,
            "entry_price":          round(entry_price, 4) if entry_price else None,
            "shares":               shares,
            "position_value_eur":   round(position_value, 2) if position_value else None,
            "unrealized_pnl_eur":   round(unrealized_pnl, 2) if unrealized_pnl is not None else None,
            "unrealized_pnl_pct":   round(unrealized_pnl_pct, 2) if unrealized_pnl_pct is not None else None,
            "stop_price":           round(stop_price, 4) if stop_price else None,
            "stop_type":            stop_type,
            "distance_to_stop_pct": distance_to_stop_pct,
            "trend_status":         trend_status,
            "sma_fast":               round(sma_fast, 4) if sma_fast else None,
            "sma_slow":              round(sma_slow, 4) if sma_slow else None,
            "adx":               round(price_data.get("adx"), 2) if price_data.get("adx") else None,
            "atr_pct":           round(price_data.get("atr_pct"), 2) if price_data.get("atr_pct") else None,
            "days_in_trade":        days_in_trade,
            "entry_date":           entry_date_str,
        })

    # Sort: winners first (highest unrealized P&L %)
    rows.sort(
        key=lambda r: r.get("unrealized_pnl_pct") or -9999,
        reverse=True,
    )
    return rows


# ============================================================================
# PORTFOLIO SUMMARY
# ============================================================================

def build_portfolio_summary(
    positions: Dict,
    latest_prices: Dict,
    current_equity: float,
    peak_equity: Optional[float],
    position_detail: List[Dict],
) -> Dict:
    """
    Compute high-level portfolio summary statistics.

    Returns:
        {
            "position_count":   int,
            "current_equity_eur": float,
            "peak_equity_eur":  float or null,
            "drawdown_pct":     float or null,
            "total_position_value_eur": float,
            "total_unrealized_pnl_eur": float,
            "total_unrealized_pnl_pct": float,
            "bull_positions":   int,
            "bear_positions":   int,
            "neutral_positions": int,
            "stops_loaded":     int,
            "avg_distance_to_stop_pct": float or null,
        }
    """
    total_value = sum(
        r.get("position_value_eur") or 0
        for r in position_detail
    )
    total_pnl = sum(
        r.get("unrealized_pnl_eur") or 0
        for r in position_detail
    )
    total_pnl_pct = (total_pnl / (total_value - total_pnl)) * 100 if (total_value - total_pnl) > 0 else 0.0

    drawdown = None
    if peak_equity and peak_equity > 0:
        drawdown = round(((current_equity / peak_equity) - 1.0) * 100, 2)

    stop_distances = [
        r["distance_to_stop_pct"]
        for r in position_detail
        if r.get("distance_to_stop_pct") is not None
    ]
    avg_distance = round(sum(stop_distances) / len(stop_distances), 2) if stop_distances else None

    trend_counts = {"BULL": 0, "BEAR": 0, "NEUTRAL": 0, "UNKNOWN": 0}
    for r in position_detail:
        ts = r.get("trend_status", "UNKNOWN")
        trend_counts[ts] = trend_counts.get(ts, 0) + 1

    return {
        "position_count":              len(positions),
        "current_equity_eur":          round(current_equity, 2),
        "peak_equity_eur":             round(peak_equity, 2) if peak_equity else None,
        "drawdown_pct":                drawdown,
        "total_position_value_eur":    round(total_value, 2),
        "total_unrealized_pnl_eur":    round(total_pnl, 2),
        "total_unrealized_pnl_pct":    round(total_pnl_pct, 2),
        "bull_positions":              trend_counts["BULL"],
        "bear_positions":              trend_counts["BEAR"],
        "neutral_positions":           trend_counts["NEUTRAL"] + trend_counts["UNKNOWN"],
        "stops_loaded":                len(stop_distances),
        "avg_distance_to_stop_pct":    avg_distance,
    }


# ============================================================================
# REPORT ASSEMBLY
# ============================================================================

def classify_alerts_by_severity(all_alerts: List[Dict]) -> Dict:
    """Group alerts by severity for quick O(1) lookup."""
    classified = {"CRITICAL": [], "HIGH": [], "MEDIUM": [], "LOW": []}
    for a in all_alerts:
        sev = a.get("severity", "LOW")
        classified.setdefault(sev, []).append(a)
    return classified


def determine_portfolio_health(classified: Dict) -> str:
    """
    Compute a single portfolio health label from classified alerts.

    Returns: "CRITICAL" | "HIGH" | "MEDIUM" | "LOW" | "OK"
    """
    if classified["CRITICAL"]:
        return "CRITICAL"
    if classified["HIGH"]:
        return "HIGH"
    if classified["MEDIUM"]:
        return "MEDIUM"
    if classified["LOW"]:
        return "LOW"
    return "OK"


def generate_monitoring_report(
    as_of_date:           str,
    params:               Dict,
    positions:            Dict,
    stop_levels:          Dict,
    exit_signals:         Dict,
    latest_prices:        Dict,
    current_equity:       float,
    peak_equity:          Optional[float],
    vix_level:            Optional[float],
    last_updates:         Dict,
    position_detail:      List[Dict],
    portfolio_summary:    Dict,
) -> Dict:
    """
    Run all nine checks and assemble the full monitoring report.

    Returns: complete report dict ready for JSON serialisation.
    """
    all_alerts: List[Dict] = []

    # ââ Check 1: Data Staleness âââââââââââââââââââââââââââââââââââââââââââââ
    all_alerts.extend(check_data_staleness(
        as_of_date   = as_of_date,
        last_updates = last_updates,
        max_stale_days = params["max_data_staleness_days"],
    ))

    # ââ Check 2: Stop-Loss Hits âââââââââââââââââââââââââââââââââââââââââââââ
    all_alerts.extend(check_stop_loss_hits(
        positions    = positions,
        stop_levels  = stop_levels,
        latest_prices = latest_prices,
    ))

    # ââ Check 3: Stop Proximity âââââââââââââââââââââââââââââââââââââââââââââ
    all_alerts.extend(check_stop_proximity(
        positions    = positions,
        stop_levels  = stop_levels,
        latest_prices = latest_prices,
        high_pct     = params["stop_proximity_high_pct"] / 100,
        medium_pct   = params["stop_proximity_medium_pct"] / 100,
    ))

    # ââ Check 4: Portfolio Drawdown âââââââââââââââââââââââââââââââââââââââââ
    all_alerts.extend(check_portfolio_drawdown(
        current_equity     = current_equity,
        peak_equity        = peak_equity,
        critical_threshold = params["max_drawdown_pct"] / 100,
        warning_threshold  = CB_MAX_DRAWDOWN_WARNING,
    ))

    # ââ Check 5: Concentration Creep ââââââââââââââââââââââââââââââââââââââââ
    all_alerts.extend(check_concentration_risk(
        positions      = positions,
        current_equity = current_equity,
        latest_prices  = latest_prices,
        max_top3_pct   = params["max_concentration_top3_pct"] / 100,
    ))

    # ââ Check 6: Single Position Size âââââââââââââââââââââââââââââââââââââââ
    all_alerts.extend(check_single_position_size(
        positions       = positions,
        current_equity  = current_equity,
        latest_prices   = latest_prices,
        max_single_pct  = CB_MAX_SINGLE_POSITION_PCT,
    ))

    # ââ Check 7: Trend Signals âââââââââââââââââââââââââââââââââââââââââââââââ
    all_alerts.extend(check_trend_signals(
        positions               = positions,
        latest_prices           = latest_prices,
        adx_weakness_threshold  = params["adx_weakness_threshold"],
        adx_consecutive_days    = ADX_CONSECUTIVE_DAYS,
    ))

    # ââ Check 8: Correlation Risk ââââââââââââââââââââââââââââââââââââââââââââ
    all_alerts.extend(check_correlation_risk(
        positions        = positions,
        as_of_date       = as_of_date,
        max_correlation  = params["max_correlation"],
    ))

    # ââ Check 9: VIX Spike âââââââââââââââââââââââââââââââââââââââââââââââââââ
    all_alerts.extend(check_vix(
        vix_level   = vix_level,
        halt_level  = params["max_vix"],
        warn_level  = CB_VIX_WARN_LEVEL,
    ))

    # ââ Pre-computed exit signals (Script 10) ââââââââââââââââââââââââââââââââ
    # Surface these as separate HIGH alerts if not already flagged above
    for sym, sig in exit_signals.items():
        if sym in positions:
            # Only add if not already caught by CHECK 2 or CHECK 7
            already_flagged = any(
                a.get("symbol") == sym and a["check"] in {
                    "stop_loss_hit", "trend_reversal_death_cross"
                }
                for a in all_alerts
            )
            if not already_flagged:
                all_alerts.append({
                    "check":    "precomputed_exit_signal",
                    "severity": "HIGH",
                    "symbol":   sym,
                    "reason":   sig.get("reason"),
                    "priority": sig.get("priority"),
                    "detail":   sig.get("detail", f"Script 10 exit signal for {sym}: {sig.get('reason')}"),
                    "action":   sig.get("exit_type", "exit_at_next_session"),
                })

    # ââ Assemble report ââââââââââââââââââââââââââââââââââââââââââââââââââââââ
    classified      = classify_alerts_by_severity(all_alerts)
    portfolio_health = determine_portfolio_health(classified)

    report = {
        "_meta": {
            "as_of_date":          as_of_date,
            "generated_at":        datetime.now().isoformat(),
            "script":              "14_daily_monitoring.py",
            "architecture_version": "3.2",
            "portfolio_health":    portfolio_health,
        },
        "portfolio_summary": portfolio_summary,
        "alert_summary": {
            "total_alerts":    len(all_alerts),
            "critical_count":  len(classified["CRITICAL"]),
            "high_count":      len(classified["HIGH"]),
            "medium_count":    len(classified["MEDIUM"]),
            "low_count":       len(classified["LOW"]),
            "portfolio_health": portfolio_health,
        },
        "alerts_by_severity": classified,
        "all_alerts":         all_alerts,
        "position_detail":    position_detail,
        "data_freshness":     last_updates,
    }

    return report


# ============================================================================
# OUTPUT WRITERS
# ============================================================================

def save_json_report(report: Dict, as_of_date: str, dry_run: bool) -> Optional[Path]:
    """
    Save the full monitoring report as JSON.

    Returns: output file path, or None in dry-run mode.
    """
    if dry_run:
        logger.info("DRY RUN â JSON report not written.")
        return None

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = REPORTS_DIR / f"{as_of_date}_monitoring.json"

    # Remove non-serialisable _df_full objects from position_detail before saving
    clean_report = _clean_for_json(report)

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(clean_report, f, indent=2, default=str)

    logger.info(f"â JSON report saved: {out_path}")
    return out_path


def _clean_for_json(obj):
    """Recursively strip non-JSON-serialisable objects (DataFrames, etc.)."""
    if isinstance(obj, dict):
        return {k: _clean_for_json(v) for k, v in obj.items() if k != "_df_full"}
    if isinstance(obj, list):
        return [_clean_for_json(i) for i in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    return obj


def save_csv_report(
    position_detail: List[Dict],
    as_of_date: str,
    dry_run: bool,
) -> Optional[Path]:
    """
    Save the position-level detail table as CSV for easy review.

    Returns: output file path, or None in dry-run mode.
    """
    if dry_run or not position_detail:
        if dry_run:
            logger.info("DRY RUN â CSV report not written.")
        return None

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = REPORTS_DIR / f"{as_of_date}_monitoring.csv"

    fieldnames = [
        "symbol", "data_date", "current_price", "entry_price", "shares",
        "position_value_eur", "unrealized_pnl_eur", "unrealized_pnl_pct",
        "stop_price", "stop_type", "distance_to_stop_pct",
        "trend_status", "sma_fast", "sma_slow", "adx", "atr_pct",
        "days_in_trade", "entry_date",
    ]

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(position_detail)

    logger.info(f"â CSV position detail saved: {out_path}")
    return out_path


# ============================================================================
# CONSOLE SUMMARY PRINTER
# ============================================================================

SEVERITY_EMOJI = {"CRITICAL": "ð´", "HIGH": "ð ", "MEDIUM": "ð¡", "LOW": "ðµ"}
HEALTH_EMOJI   = {"OK": "â", "LOW": "ðµ", "MEDIUM": "ð¡", "HIGH": "ð ", "CRITICAL": "ð´"}


def print_summary(report: Dict) -> None:
    """
    Print a structured, human-readable summary to stdout.
    """
    meta     = report.get("_meta", {})
    summary  = report.get("portfolio_summary", {})
    alerts_s = report.get("alert_summary", {})
    health   = meta.get("portfolio_health", "UNKNOWN")

    divider = "â" * 70

    print(f"\n{'â' * 70}")
    print(f"  ð DAILY PORTFOLIO MONITOR â {meta.get('as_of_date', 'N/A')}")
    print(f"  Generated: {meta.get('generated_at', 'N/A')}")
    print(f"{'â' * 70}")

    # Portfolio health
    print(f"\n  Portfolio Health: {HEALTH_EMOJI.get(health, 'â')} {health}")
    print(divider)

    # Portfolio summary
    print(f"\n  PORTFOLIO SUMMARY")
    print(f"  {'Positions':<30} {summary.get('position_count', 'N/A')}")
    print(f"  {'Current Equity':<30} â¬{summary.get('current_equity_eur', 0):>12,.2f}")
    if summary.get("peak_equity_eur"):
        print(f"  {'Peak Equity':<30} â¬{summary['peak_equity_eur']:>12,.2f}")
    if summary.get("drawdown_pct") is not None:
        print(f"  {'Drawdown from Peak':<30} {summary['drawdown_pct']:>11.2f}%")
    print(f"  {'Total Unrealised P&L':<30} â¬{summary.get('total_unrealized_pnl_eur', 0):>12,.2f}  "
          f"({summary.get('total_unrealized_pnl_pct', 0):.2f}%)")
    print(f"  {'Trend Distribution':<30} "
          f"Bull:{summary.get('bull_positions', 0)}  "
          f"Bear:{summary.get('bear_positions', 0)}  "
          f"Neutral:{summary.get('neutral_positions', 0)}")
    if summary.get("avg_distance_to_stop_pct") is not None:
        print(f"  {'Avg Distance to Stop':<30} {summary['avg_distance_to_stop_pct']:>10.2f}%")

    # Alert summary
    print(f"\n{divider}")
    print(f"\n  ALERTS  ({alerts_s.get('total_alerts', 0)} total)")
    print(f"  {'CRITICAL':<12} {alerts_s.get('critical_count', 0)}")
    print(f"  {'HIGH':<12} {alerts_s.get('high_count', 0)}")
    print(f"  {'MEDIUM':<12} {alerts_s.get('medium_count', 0)}")
    print(f"  {'LOW':<12} {alerts_s.get('low_count', 0)}")

    # Print each alert
    if alerts_s.get("total_alerts", 0) > 0:
        print(f"\n{divider}")
        print("\n  ALERT DETAIL")

        for severity in ["CRITICAL", "HIGH", "MEDIUM", "LOW"]:
            alerts = report.get("alerts_by_severity", {}).get(severity, [])
            for a in alerts:
                emoji = SEVERITY_EMOJI.get(severity, "â")
                symbol = a.get("symbol", "")
                sym_str = f"[{symbol}] " if symbol else ""
                check  = a.get("check", "").replace("_", " ").upper()
                print(f"\n  {emoji} [{severity}] {check} {sym_str}")
                print(f"     {a.get('detail', 'No detail')}")
                print(f"     Action → {a.get('action', 'N/A')}")

    # Position table
    position_detail = report.get("position_detail", [])
    if position_detail:
        print(f"\n{divider}")
        print(f"\n  POSITION DETAIL")
        print(f"\n  {'Symbol':<12} {'Price':>8} {'Stop':>8} {'Gap%':>6} "
              f"{'PnL%':>7} {'ADX':>6} {'Trend':>8} {'Days':>5}")
        print(f"  {'-'*12} {'-'*8} {'-'*8} {'-'*6} {'-'*7} {'-'*6} {'-'*8} {'-'*5}")

        for row in position_detail:
            pnl_pct  = row.get("unrealized_pnl_pct")
            gap_pct  = row.get("distance_to_stop_pct")
            adx      = row.get("adx")
            days     = row.get("days_in_trade")
            price    = row.get("current_price")
            stop     = row.get("stop_price")

            pnl_str   = f"{pnl_pct:+.1f}%" if pnl_pct is not None else "  N/A"
            gap_str   = f"{gap_pct:.1f}%"  if gap_pct is not None else "  N/A"
            adx_str   = f"{adx:.1f}"       if adx is not None     else "  N/A"
            price_str = f"{price:.2f}"     if price is not None   else "   N/A"
            stop_str  = f"{stop:.2f}"      if stop is not None    else "   N/A"
            days_str  = str(days)          if days is not None    else "N/A"
            trend     = row.get("trend_status", "?")

            print(f"  {row['symbol']:<12} {price_str:>8} {stop_str:>8} {gap_str:>6} "
                  f"{pnl_str:>7} {adx_str:>6} {trend:>8} {days_str:>5}")

    print(f"\n{'â' * 70}\n")


# ============================================================================
# ARGUMENT PARSER
# ============================================================================

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Script 14 â Daily Portfolio Monitor (Architecture v3.2)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Standard daily run
  python scripts/14_daily_monitoring.py

  # Specify as-of date (historical / backfill)
  python scripts/14_daily_monitoring.py --as-of-date 2026-02-10

  # Include VIX reading
  python scripts/14_daily_monitoring.py --vix 24.5

  # Override account equity (if portfolio_state.json has no live values)
  python scripts/14_daily_monitoring.py --account-equity 52000

  # Dry run (no files written)
  python scripts/14_daily_monitoring.py --dry-run

  # Suppress console output (cron usage)
  python scripts/14_daily_monitoring.py --quiet
        """,
    )
    parser.add_argument(
        "--as-of-date",
        type=str,
        default=None,
        help="Evaluation date YYYY-MM-DD (default: today)",
    )
    parser.add_argument(
        "--vix",
        type=float,
        default=None,
        help="Current VIX level for volatility regime check (e.g. 24.5)",
    )
    parser.add_argument(
        "--account-equity",
        type=float,
        default=None,
        help="Total account equity in EUR (overrides live price calculation)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Run all checks but do not write output files",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        default=False,
        help="Suppress console output (log file is always written)",
    )
    return parser


# ============================================================================
# MAIN
# ============================================================================

def main() -> None:
    """
    Entry point.

    Execution order:
        1. Parse arguments, set up logging.
        2. Load strategy parameters.
        3. Load all inputs in parallel (portfolio state, stops, signals, prices).
        4. Run all nine monitoring checks.
        5. Build position detail table and portfolio summary.
        6. Assemble report.
        7. Write JSON + CSV outputs.
        8. Print console summary.
        9. Exit with code 0 (all clear) or 1 (CRITICAL/HIGH alerts present).
    """
    parser  = build_arg_parser()
    args    = parser.parse_args()

    # ââ Resolve as-of date ââââââââââââââââââââââââââââââââââââââââââââââââ
    as_of_date = args.as_of_date or date.today().strftime("%Y-%m-%d")

    try:
        datetime.strptime(as_of_date, "%Y-%m-%d")
    except ValueError:
        print(f"ERROR: Invalid --as-of-date format '{as_of_date}'. Use YYYY-MM-DD.")
        sys.exit(1)

    # ââ Logging âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
    global logger
    logger = setup_logging(as_of_date, quiet=args.quiet)
    logger.info(f"{'=' * 60}")
    logger.info(f"Script 14: Daily Portfolio Monitor")
    logger.info(f"As-of date : {as_of_date}")
    logger.info(f"VIX input  : {args.vix or 'not provided'}")
    logger.info(f"Equity     : {f'â¬{args.account_equity:,.0f}' if args.account_equity else 'auto-calculated'}")
    logger.info(f"Dry run    : {args.dry_run}")
    logger.info(f"{'=' * 60}")

    # ââ Load strategy parameters âââââââââââââââââââââââââââââââââââââââââ
    params = load_strategy_params()

    # ââ Load inputs âââââââââââââââââââââââââââââââââââââââââââââââââââââââ
    positions    = load_portfolio_state()
    stop_levels  = load_stop_levels()
    exit_signals = load_exit_signals()
    last_updates = load_last_update_dates()
    peak_equity  = load_peak_equity()

    if not positions:
        logger.info("No open positions found. Monitoring limited to data freshness check.")

    # ââ Load latest prices + indicators (single pass) ââââââââââââââââââââ
    symbols       = list(positions.keys())
    latest_prices = load_all_latest_prices(symbols, as_of_date) if symbols else {}

    # ââ Calculate current equity ââââââââââââââââââââââââââââââââââââââââââ
    current_equity = calculate_current_equity(
        positions              = positions,
        latest_prices          = latest_prices,
        account_equity_override = args.account_equity,
    )
    logger.info(f"Current equity: â¬{current_equity:,.2f}")

    # ââ Build position detail table âââââââââââââââââââââââââââââââââââââââ
    position_detail = build_position_detail(
        positions     = positions,
        stop_levels   = stop_levels,
        latest_prices = latest_prices,
        as_of_date    = as_of_date,
    )

    # ââ Build portfolio summary âââââââââââââââââââââââââââââââââââââââââââ
    portfolio_summary = build_portfolio_summary(
        positions       = positions,
        latest_prices   = latest_prices,
        current_equity  = current_equity,
        peak_equity     = peak_equity,
        position_detail = position_detail,
    )

    # ââ Generate full monitoring report âââââââââââââââââââââââââââââââââââ
    report = generate_monitoring_report(
        as_of_date        = as_of_date,
        params            = params,
        positions         = positions,
        stop_levels       = stop_levels,
        exit_signals      = exit_signals,
        latest_prices     = latest_prices,
        current_equity    = current_equity,
        peak_equity       = peak_equity,
        vix_level         = args.vix,
        last_updates      = last_updates,
        position_detail   = position_detail,
        portfolio_summary = portfolio_summary,
    )

    # ââ Write outputs âââââââââââââââââââââââââââââââââââââââââââââââââââââ
    json_path = save_json_report(report, as_of_date, args.dry_run)
    csv_path  = save_csv_report(position_detail, as_of_date, args.dry_run)

    # ââ Print console summary âââââââââââââââââââââââââââââââââââââââââââââ
    if not args.quiet:
        print_summary(report)

    # ââ Final log summary âââââââââââââââââââââââââââââââââââââââââââââââââ
    health     = report["_meta"]["portfolio_health"]
    alert_s    = report["alert_summary"]
    logger.info(f"{'â' * 60}")
    logger.info(f"Portfolio health : {health}")
    logger.info(
        f"Alerts           : {alert_s['total_alerts']} total "
        f"(CRITICAL:{alert_s['critical_count']} "
        f"HIGH:{alert_s['high_count']} "
        f"MEDIUM:{alert_s['medium_count']} "
        f"LOW:{alert_s['low_count']})"
    )
    if json_path:
        logger.info(f"Report JSON      : {json_path}")
    if csv_path:
        logger.info(f"Report CSV       : {csv_path}")
    logger.info(f"{'â' * 60}")

    # ââ Exit code: non-zero if action is required âââââââââââââââââââââââââ
    if health in {"CRITICAL", "HIGH"}:
        logger.warning(
            "Exiting with code 1 â CRITICAL or HIGH alerts require immediate attention."
        )
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
